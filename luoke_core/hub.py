"""主控本体（开发版）：Unix socket 上的任务层（V1 §2），把派发（§3）、引擎（§5）、打分工作进程串起来。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Optional

from collections import deque

from . import jobs as idle_jobs, CONTRACT, __version__, accessories, machine
from . import apps, download, procs
from . import endpoint as endpoint_mod
from .dispatch import Dispatcher
from .engine import Engine, EngineError
from .proxy import STATUS, BadRequest, build_engine_payload, error_body, estimate_secs
from .recipe import Recipe
from .scorer import EmbedWorker, ScoreWorker, ScorerError

SAMPLE_SECS = 5
# 专用模型工作进程（打分、联想、嵌入）的生命周期（issue #11，和 luoke-ime 10-20 商定）：空闲这么久就停，下次用到再起（重起时答
# not_ready、retry_after 2 秒，约等于加载时间）；内核报内存压力 critical 时停掉、这段时间里不起；起之前看这台机器还剩的内存够不够，
# 不够答 not_ready、retry_after 30 秒。配件收到 not_ready 照 retry_after 退避，这期间不重排（打字照常）。
WORKER_IDLE_SECS = 600
WORKER_HOLD_SECS = 30
# "较慢"标记（V1 §7、DESIGN §7）：不用线上流量算——配件一收到 slow 就少给候选，用时随之变短，会来回翻（luoke-ime 2026-10-14）。
# 改成打分进程就绪后用固定形状的探针测一次（像打字：前文逐字变长、16 个候选），p90 对配方阈值；阈值是实验室定的分界，各档相同；
# 带滞回：标了以后要低于阈值的 80% 才清。结果记在 state.json 的 probe 里，重启沿用，工作进程重启再测。
PROBE_SCORE_RUNS = 25
PROBE_PREDICT_RUNS = 10
PROBE_WARMUP = 5
PROBE_QUIET_SECS = 10     # 最近这么久没有打分才测（25 次打分在 M5 上不到 1 s、M1 上约 3 s）
SLOW_CLEAR_RATIO = 0.8
# 探针的形状（前文、从第几个字开始逐字变长、候选）来自配方里打分 / 联想任务参数的 probe 块，由配件定（顶层 ARCHITECTURE §2：
# 主控不替配件写前文和候选；2026-10-16 越界自查改）。没有 probe 块的任务不测、不标慢。


def decide_slow(prev: bool, p90_ms: float, threshold_ms: float) -> bool:
    """滞回：没标过就看超不超阈值；标过了要低于阈值的 SLOW_CLEAR_RATIO 才清。"""
    return p90_ms > threshold_ms if not prev else p90_ms > threshold_ms * SLOW_CLEAR_RATIO


IMAGE_BUDGETS = (70, 140, 280, 560, 1120)       # mlx-vlm 的 Gemma 4 图片处理器支持的 token 预算


def media_kinds(params: dict) -> set:
    """这一档的嵌入任务收哪些媒体（和 digup 商定，草案 docs/research/embed-multimodal-draft.md）：
    任务参数里 image_batch > 0 收图片、audio_batch > 0 收声音；都没有就只收文字。"""
    out = set()
    for kind in ("image", "audio"):
        try:
            if int(params.get(f"{kind}_batch") or 0) > 0:
                out.add(kind)
        except (TypeError, ValueError):
            pass
    return out


def b64_fits(s: str, max_bytes) -> bool:
    """base64 串解码后不超过 max_bytes（按长度算，不解码；主控不读媒体内容）。没有上限就算过。"""
    return not max_bytes or len(s) <= (int(max_bytes) * 4 + 2) // 3 + 4


def precise_wait(d: Dispatcher, reason: Optional[str], lane: Optional[str] = None) -> dict:
    """后台请求被挡回时错误体里多带的精确等待 retry_after_ms（只加不改，2026-10-17）；算不出来就不带。"""
    ms = int(round(d.lane_wait(lane) * 1000)) if reason == "lane_busy" and lane else d.retry_after_ms(reason)
    return {"retry_after_ms": ms} if ms is not None else {}


def p90_of(xs: list, drop: int = 0) -> Optional[float]:
    xs = sorted(xs[drop:])
    return xs[min(len(xs) - 1, int(0.9 * len(xs)))] if xs else None
PROGRESS_SECS = 2
IDLE_JOB_REST_HOLD_SECS = 240      # 空闲任务的请求最多挂着等这么久的功耗间歇（配件的请求超时一般 300 秒）
BREAKER_CRASHES = 3          # 熔断：这么多次崩溃……
BREAKER_WINDOW_SECS = 600    # ……发生在这么长时间内
# 冒烟检查的测量方法版本：量法变了就加一，旧记录（没有 method 或更低）在下次启动时重测，不再按组合沿用。
# 2：量速度前先空跑一条（2026-10-19；之前的 prefill 被首条请求现编内核拖慢，一台 M3 Pro 36 GB（beta0）上 9B 记成 50，配方是 230）
SMOKE_METHOD = 2


def engine_app_matching(app: str, version: str) -> Optional[str]:
    """app（某个 oMLX.app）在、且 Info.plist 的 CFBundleShortVersionString 等于配方引擎版本去掉 "omlx-" 前缀的部分时返回它，否则 None。"""
    plist = os.path.join(app, "Contents", "Info.plist")
    if not app or not os.path.isfile(plist):
        return None
    try:
        import plistlib
        with open(plist, "rb") as f:
            have = str(plistlib.load(f).get("CFBundleShortVersionString") or "")
    except Exception:
        return None
    want = version[5:] if version.startswith("omlx-") else version
    return app if have == want else None


class Hub:
    def __init__(self, recipe: Recipe, env_dir: str, model_dir: str, lab: bool = False,
                 accessory_dirs: Optional[list] = None, mem_gb: Optional[float] = None, smoke: bool = True,
                 tier: Optional[str] = None,
                 release: Optional[str] = None):
        self.recipe = recipe
        self.smoke = smoke
        self.release_path = release          # 发布清单 release.json（V1 §4），没有就不能下载
        self._release_tag: Optional[str] = None
        self._local_allow: tuple = (None, [])
        self._local_allow_used: set = set()
        # 给高级用户的本机端点（顶层 ADR 0014 修订）：引擎一次只留一个生成模型，谁在用记在这里；换模型要等用着旧的请求做完
        self.embed_lane = threading.Condition()       # 嵌入的优先通道（#5 审阅）：有查询在等，后台批的下一份先让
        self.embed_priority_waiting = 0
        self.model_cv = threading.Condition()
        self.model_users: dict = {}
        self.extra_users: dict = {}     # 和底座一起放着的用户模型，谁在用（闲了才卸）
        self.user_model_last_use = 0.0
        self.downloads: dict = {}            # 清单项名 -> {"state": queued/downloading/done/failed, "done", "total", "file", "error"}
        self.download_thread: Optional[threading.Thread] = None
        self.env_dir = env_dir
        self.model_dir = model_dir
        self.lab = lab
        self.accessory_dirs = accessory_dirs or []
        os.makedirs(os.path.join(env_dir, "run"), mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(env_dir, "logs"), exist_ok=True)
        os.makedirs(os.path.join(env_dir, "engine"), exist_ok=True)
        self.logf = open(os.path.join(env_dir, "logs", "hub.log"), "a", encoding="utf-8")
        self.loglock = threading.Lock()
        self.mem_gb = mem_gb or machine.mem_gb()
        self.settings_path = os.path.join(env_dir, "settings.json")
        self.settings = {"power_mode": "balanced", "background_on_battery": False, "tier": None,
                         "endpoint_enabled": False, "endpoint_port": endpoint_mod.DEFAULT_PORT}   # 给高级用户的本机端点，默认关
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                self.settings.update({k: v for k, v in json.load(f).items() if k in self.settings})
        except (OSError, ValueError):
            pass
        self.memory_tier = recipe.tier_for(self.mem_gb)       # 内存算出来的档
        if tier and tier not in recipe.tiers:
            raise RuntimeError(f"配方里没有硬件档 {tier}")
        chosen = self.valid_chosen_tier(self.settings.get("tier"))   # 用户选的档（省空间：大内存机器也可以按 32 GB 档跑 9B；issue luoke-core#2，2026-10-16 定）
        self.pending_tier: Optional[str] = None
        want = tier or chosen or self.memory_tier                # tier：实验室评测按某一档的策略跑（内存仍按真机算）
        # 选的档的底座还没下好（换档时下载失败、重启了）：先用底座在的那一档跑，等补下好了再换（审查 2026-10-17：之前重启后没有底座、任务全停）
        if not tier and want and not self._base_present(recipe, model_dir, want):
            for alt in (self.memory_tier, *sorted(recipe.tiers, key=lambda t: -int(t))):
                if alt and alt != want and self.valid_chosen_tier(alt) and self._base_present(recipe, model_dir, alt):
                    self.pending_tier, want = want, alt
                    break
        self.tier_name = want
        if not self.tier_name:
            raise RuntimeError(f"没有适合 {self.mem_gb:.0f} GB 的硬件档")
        self.tier = recipe.tier(self.tier_name)
        self.dispatcher = Dispatcher(time.time, self.tier)
        self.base_name = recipe.base_for(self.tier_name)
        eng = recipe.engine
        # 引擎：按清单装上的那份优先（<env>/engines/<版本>/oMLX.app）；没有就用配方写的那份（开发版：本机的 oMLX.app），但只认版本和配方一致的
        self.engine_app = self.installed_engine_app() or self.recipe_engine_app() or eng["app"]
        self.engine = Engine(self.engine_app, os.path.join(env_dir, "engine", eng["version"]), model_dir,
                             self.tier["guard_gb"], eng.get("log_level", "warning"), self.log)
        self.engine_python = os.path.join(self.engine_app, eng["python"])
        self.engine_site = os.path.join(self.engine_app, eng["site"])
        # 配件装到哪：正式 ~/Applications（输入法 ~/Library/Input Methods）；实验室构建装在自己的环境目录里，不碰本机用户的
        # 实验室构建的目录带 .noindex、装完也不登记 LaunchServices：不然 Spotlight 会把测试副本按 bundle id 登记进去，别的配件按 id 打开时可能落到它
        self.apps_dir = os.path.join(env_dir, "Applications.noindex") if lab else os.path.expanduser("~/Applications")
        self.input_methods_dir = os.path.join(env_dir, "Input Methods.noindex") if lab else os.path.expanduser("~/Library/Input Methods")
        if lab:
            self.accessory_dirs = list(self.accessory_dirs) + [self.apps_dir, self.input_methods_dir]
        self.base_id: Optional[str] = None
        self.scorers: dict = {}      # 模型名 -> ScoreWorker / EmbedWorker
        self.scorers_lock = threading.Lock()   # 只护 scorer_for 里的建、起
        self.worker_hold_until = 0.0           # 内存压力 critical 之后这段时间不起专用模型工作进程（issue #11）
        self.last_base_use = time.time()
        self.smoke_running = False      # 冒烟检查进行中：闲置卸载不许动底座
        self.unload_on_next_score = False
        self.manifests = self.scan_accessories()
        self.stop_event = threading.Event()
        self.started = time.time()
        self.phase = "starting"       # starting / running / stopped：启动期间任务层只答 /status，别的 503
        self.state_path = os.path.join(env_dir, "state.json")
        self.state = self._load_state()
        self.user_models = endpoint_mod.UserModels(env_dir, model_dir, self.log)
        self.endpoint = endpoint_mod.Endpoint(self)
        self.socket_path: Optional[str] = None      # serve() 起来后由 __main__ 填，空闲任务的环境里给 LUOKE_HUB_SOCKET
        self.jobs = idle_jobs.IdleJobs(now=self.dispatcher.now, quiet=self.dispatcher.quiet, background_state=self.dispatcher.background_state,
                                       busy=self._jobs_busy,
                                       on_change=lambda accessory: setattr(self.dispatcher, "idle_job_accessory", accessory),
                                       state=self.state, save_state=self._save_state, log=self.log,
                                       socket_path=lambda: self.socket_path, logs_dir=os.path.join(env_dir, "logs", "jobs"))
        self._manifest_rescan_at = 0.0
        self.dispatcher.power_mode = self.settings["power_mode"]
        self.dispatcher.background_on_battery = bool(self.settings["background_on_battery"])
        self.score_ms: dict = {}      # 任务名 -> 最近的打分耗时（毫秒），只作诊断（state.json 的 tasks）
        self.score_full_ms: dict = {} # 同上，只记候选数 >= 8 的（接近全量，和探针可比）
        self._embed_refusals: dict = {}   # (任务, 原因码) -> (上次记日志的时刻, 其间省掉的条数)，见 _log_embed_refusal
        self.smoke_failed: Optional[str] = None
        self.engine_crashes: deque = deque(maxlen=20)   # 崩溃时刻；打断不算
        self.breaker: Optional[str] = None               # 熔断原因；熔断后不再重启引擎
        self.restarting = threading.Lock()
        self.download_lock = threading.Lock()
        self.download_queue: list = []

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def set_resident(self, accessory: str, flag: bool) -> None:
        """配件最近一次的常驻声明，持久化；主控本体登录时按它拉起（清单里的 resident 只是没声明过时的默认值）。"""
        self.state.setdefault("resident", {})[accessory] = flag
        self._save_state()
        self.log(f"resident {accessory}={flag}")

    def _jobs_busy(self) -> bool:
        """空闲任务的"忙"：没在跑空闲任务时，引擎有后台工作或正在加载就不起新的；正在跑的时候，只有别的配件占着引擎才算忙。
        之前不分是谁的——空闲任务自己发的后台请求（以及它引起的底座加载）也算忙，采样线程下一次（约 5 秒后）就让它收尾：
        luoke-mem.learn 从 10-19 起每次跑到第三条 topics 就被收掉，反刍永远到不了（开发机 hub.log，luoke-top 10-20 报）。"""
        if self.phase != "running":
            return True
        mine = self.dispatcher.idle_job_accessory
        job = self.dispatcher.job
        if mine is None:
            return job is not None or self.engine.loading
        return job is not None and (self.recipe.task(job.task) or {}).get("accessory") != mine

    def scan_accessories(self) -> dict:
        """已装配件的清单。实验室实例没有显式的 LUOKE_HOME 时不加载主控里带的螺壳记忆：不然实验室的 learn、记忆界面会落到本机用户在用的
        ~/.luoke/mem.db（审查 2026-10-17；顶层 CLAUDE.md"测试环境拒绝真实目录"）。
        实验室实例也**只扫自己的配件目录**（accessory_dirs，含环境目录里的 Applications.noindex）：不然它扫到本机用户 ~/Applications 里
        真实装着的配件——还没迁走的旧「螺壳记忆.app」带着 learn 空闲任务、{memory} 指真实的 mem.db，安静时刻就会被实验室拉起
        （2026-10-16 gateway 集成测试发现，10-19 用 main 复现）；邮件助手、输入法的清单也会混进来。"""
        bundled = not (self.lab and not os.environ.get("LUOKE_HOME"))
        if self.lab:
            return accessories.installed(only=self.accessory_dirs, hub=accessories.hub_app(), bundled=bundled)
        found = accessories.installed(self.accessory_dirs, bundled=bundled)
        self._note_verified(found)
        return found

    # ---- 验证记录（配件回滚，issue #12，10-20 定"认它当时的验证"）：每一版主控只认它评测过的那一个配件版本；换回上一版的配件，
    # 主控按"在这台 Mac 上哪一版发布验证过它、验证了哪些任务"认它，只给当时验证过的任务。记录在装着的配件的版本被本版配方认时写下 ----
    VERIFIED_HISTORY = "verified-history.json"
    HISTORY_KEEP = 3          # 每个配件留最近这么多个版本

    def _history(self) -> dict:
        try:
            with open(os.path.join(self.env_dir, self.VERIFIED_HISTORY), encoding="utf-8") as f:
                h = json.load(f)
            return h if isinstance(h, dict) else {}
        except (OSError, ValueError):
            return {}

    def _note_verified(self, found: dict) -> None:
        h = self._history()
        changed = False
        for m in found.values():
            name, version = m.get("accessory"), m.get("version")
            if not name or not version or m.get("_bundled") or not self.recipe.version_verified(name, version):
                continue
            # 记本版当时实际放行的任务：评测过、这个版本硬性条目过了的，加上配方里这个配件根本没有评测记录的（草稿、每日摘要这类，
            # 本版对认的版本不逐项查评测就给）。只记前一种的话，主控先升级、应用还没升级时这几项就用不了（issue #32）
            v = self.recipe.verified.get(name) or {}
            evals = v.get("tasks") or {}
            tasks = sorted({t for t, entries in evals.items() if any(e.get("hard_ok") and e.get("accessory_version") == version for e in entries)}
                           | {t for t, spec in (self.recipe.tasks or {}).items() if spec.get("accessory") == name and not evals.get(t)})
            entry = {"release": self.release_tag() or "", "tasks": tasks}
            per = h.setdefault(name, {})
            if {k: per.get(version, {}).get(k) for k in entry} == entry:
                continue
            per[version] = dict(entry, at=time.time())
            for old in sorted(per, key=lambda k: per[k].get("at", 0))[:-self.HISTORY_KEEP]:
                del per[old]
            changed = True
        if changed:
            try:
                p = os.path.join(self.env_dir, self.VERIFIED_HISTORY)
                with open(p + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(h, f, ensure_ascii=False, indent=1)
                os.replace(p + ".tmp", p)
            except OSError as e:
                self.log(f"verified history not saved: {e}")

    def history_entry(self, name: str, version: str) -> Optional[dict]:
        """装着的正是这个版本、本版配方不认它、但这台 Mac 上以前的发布验证过它：答那条记录。"""
        if self.lab or self.recipe.version_verified(name, version):
            return None
        m = accessories.by_accessory(self.manifests or {}, name)
        if not m or m.get("version") != version:
            return None
        return (self._history().get(name) or {}).get(version)

    def version_accepted(self, name: str, version: str) -> bool:
        return (self.lab or self.recipe.version_verified(name, version) or self.locally_allowed(name, version)
                or self.history_entry(name, version) is not None)

    def check_uninstall_data(self, data: bool, dry_run: bool) -> None:
        """实验室实例在 HOME 没隔离时不删配件数据：实验室装的配件清单里声明的 ~/.luoke-mail 之类就是本机用户真实的数据
        （data_dirs 只挡记忆，挡不住这些）。只算计划（dry_run）、不删数据的卸载照常。"""
        if self.lab and data and not dry_run and accessories.home_is_real():
            raise BadRequest("forbidden", "实验室实例不删配件数据：HOME 是你真实的主目录，配件声明的数据就是你自己的。"
                                          "要测删数据，把 HOME 和 LUOKE_HOME 指向测试目录再起实验室主控")

    def release_accessory(self, accessory: str) -> None:
        """配件退出或被卸载：只有它在用的打分进程停掉，预算重算。"""
        for model, w in list(self.scorers.items()):
            users = {t["accessory"] for tn, t in self.recipe.tasks.items()
                     if t["kind"] in ("score", "predict", "embed") and self.recipe.model_for_task(tn, self.tier_name) == model}
            if users == {accessory}:
                w.stop()
        self.apply_budget()

    def note_exiting(self, accessory: str) -> None:
        """配件正常退出前调用：本体据此不把这次退出当异常（不重启）。"""
        self.state.setdefault("exited", {})[accessory] = time.time()
        self._save_state()
        self.log(f"exiting {accessory}")

    @staticmethod
    def _base_present(recipe: Recipe, model_dir: str, tier: str) -> bool:
        base = recipe.base_for(tier)
        return bool(base) and os.path.isdir(os.path.join(model_dir, recipe.model(base)["dir"]))

    def valid_chosen_tier(self, name) -> Optional[str]:
        """用户选的档合不合法：配方里有、而且这台 Mac 的内存够它的下限（只能往省的选，不能往上选）。"""
        if not name or name not in self.recipe.tiers:
            return None
        return name if float(self.recipe.tiers[name]["min_gb"]) <= self.mem_gb else None

    def apply_tier(self, name: str) -> None:
        """换档（用户在首次运行或设置里选了「省空间」/「完整」）：换底座；底座模型没下就按清单下（下完自动重启引擎、做冒烟检查），
        有就直接重启引擎。任务参数的版本随之变，配件会重读 GET /tasks。"""
        self.log(f"tier {self.tier_name} -> {name} (user choice; memory tier {self.memory_tier})")
        if not self._base_present(self.recipe, self.model_dir, name):
            # 新底座还没有：先按清单下，这期间照旧用现在的档和底座（不让"新档参数配旧底座"这种没验证过的组合跑），下好了在下载线程里换
            # （审查 2026-10-17：之前名字先改了、下载正忙时新底座不下也不报错）
            self.pending_tier = name
            base = self.recipe.base_for(name)
            try:
                self.start_downloads([base] + [m for m in (self.recipe.assignment.get(name, {}).get("special") or {}).values()
                                               if not os.path.isdir(self.model_dir_of(m))])
            except BadRequest as e:
                self.log(f"tier change: cannot download {base}: {e.message}")
            return
        self.pending_tier = None
        self.tier_name = name
        self.tier = self.recipe.tier(name)
        self.dispatcher.tier = self.tier
        self.base_name = self.recipe.base_for(name)
        if not self.engine_missing():
            threading.Thread(target=self.restart_engine, daemon=True).start()

    def restart_engine(self) -> None:
        """底座变了就重启引擎（它只在启动时发现模型），再做冒烟检查。串行：连着换两次档时，第二次等第一次做完再按当时的底座重启
        （10-16 实验室实例上两个线程交错，后一个停了引擎、前一个的冒烟检查连不上，base_id 也成了旧的）。"""
        with self.restarting:      # 和崩溃重启共用一把锁：有意重启的空档，采样线程不会当成崩溃再起一个（审查 2026-10-17）
            self._restart_engine_locked()

    def _restart_engine_locked(self) -> None:
        self.log("restarting engine")
        try:
            self.engine.stop()
            want = [self.recipe.model(self.base_name)["dir"]] if self.base_name else []
            want = [w for w in want if os.path.isdir(os.path.join(self.model_dir, w))]
            self.engine.start(wait_models=want)
            self.base_id = self.engine.resolve(want[0]) if want else None
            self.apply_budget()
            if self.smoke and self.base_id:
                self.smoke_check(force=True)
        except Exception as e:  # noqa: BLE001
            self.log(f"engine restart failed: {e}")

    def update_settings(self, patch: dict) -> dict:
        """主控本体（菜单栏）改设置：功耗档、用电池时是否运行后台、用户选的档。立即生效并落盘。"""
        if "tier" in patch:
            want = patch["tier"] or None
            if want is not None:
                if want not in self.recipe.tiers:
                    raise BadRequest("invalid_request", f"配方里没有硬件档 {want}")
                if float(self.recipe.tiers[want]["min_gb"]) > self.mem_gb:
                    raise BadRequest("invalid_request", f"这台 Mac 的内存（{self.mem_gb:.0f} GB）不够用 {want} GB 档")
            self.settings["tier"] = want
            new = want or self.memory_tier
            if new != self.tier_name:
                self.apply_tier(new)
        if "power_mode" in patch:
            if patch["power_mode"] not in ("full", "balanced", "quiet"):
                raise BadRequest("invalid_request", "power_mode 只能是 full / balanced / quiet")
            self.settings["power_mode"] = patch["power_mode"]
            self.dispatcher.power_mode = patch["power_mode"]
        if "background_on_battery" in patch:
            self.settings["background_on_battery"] = bool(patch["background_on_battery"])
            self.dispatcher.background_on_battery = bool(patch["background_on_battery"])
        if "endpoint_key" in patch:
            # 用户自己设的密钥（任意字符串，空 = 不校验）：只写进 0600 的密钥文件，不进 settings.json（10-19 定）
            if not isinstance(patch["endpoint_key"], str):
                raise BadRequest("invalid_request", "endpoint_key 要是字符串（空字符串 = 不要密钥）")
            try:
                self.endpoint.set_key(patch["endpoint_key"])
            except ValueError as e:
                raise BadRequest("invalid_request", str(e))
        if "endpoint_port" in patch:
            port = patch["endpoint_port"]
            if not isinstance(port, int) or not 1024 <= port <= 65535:
                raise BadRequest("invalid_request", "端口要在 1024–65535 之间")
            self.settings["endpoint_port"] = port
        if "endpoint_enabled" in patch or "endpoint_port" in patch:
            self.settings["endpoint_enabled"] = bool(patch.get("endpoint_enabled", self.settings.get("endpoint_enabled")))
            if self.settings["endpoint_enabled"] and self.phase == "running":
                got = self.endpoint.start(int(self.settings["endpoint_port"]))
                if got is None:
                    self.settings["endpoint_enabled"] = False
                    raise BadRequest("busy", f"{self.settings['endpoint_port']} 往后 {endpoint_mod.PORT_TRIES} 个端口都被占了，换一个")
            else:
                self.endpoint.stop()
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.settings_path)
        self.log(f"settings {self.settings}")
        return dict(self.settings)

    # ---- 下载（V1 §6 第 4 步）：按发布清单，后台线程一项一项下，完了重启引擎让它看见新模型 ----------------------
    def installed_engine_app(self) -> Optional[str]:
        p = os.path.join(self.env_dir, "engines", self.recipe.engine["version"], "oMLX.app")
        return p if os.path.isdir(os.path.join(p, "Contents")) else None

    def recipe_engine_app(self) -> Optional[str]:
        """配方 engine.app 指的那份（开发版用本机的 oMLX.app）：在、而且 Info.plist 的版本和配方的引擎版本一致才算（"omlx-0.7.0" ↔ "0.7.0"）。
        用户自己装的别的版本不拿来用：引擎包照样下（2026-10-15 16 GB 测试机实测时定）。"""
        if not self.lab and self.release_path and os.path.exists(self.release_path):
            return None     # 发布版只用清单里装的引擎包（钉了哈希、我们签的），不用本机另装的 oMLX（审查 2026-10-17，ADR 0001）
        return engine_app_matching(self.recipe.engine.get("app") or "", self.recipe.engine["version"])

    def needed_downloads(self) -> list:
        """本档（和换档等着的那一档）配方用到、模型目录里还没有的模型名；引擎包没装、配方写的引擎也不在时还要引擎（清单项 kind=engine，名字 engine）。
        配件（kind=accessory）不在这里：首次运行按用户勾选的传给 start_downloads。"""
        names = []
        # 换档等着的那一档也算（luoke-core#25：换档下到一半重启，这里只看当前档、返回空，菜单栏就不再补下，一直停在原来的档）
        for t in (self.tier_name, self.pending_tier):
            a = (self.recipe.assignment.get(t) or {}) if t else {}
            names += ([a["base"]] if a.get("base") else []) + list((a.get("special") or {}).values())
        items = self._manifest_items()
        def missing(n):
            item = items.get(n)
            if item is not None:      # 有清单就比摘要（审查 2026-10-17：之前只看目录在不在；没摘要的旧目录下载时会按哈希复用，不重下）
                return not download.item_installed(item, self.model_dir_of(n))
            return not os.path.isdir(self.model_dir_of(n))
        need = [n for n in dict.fromkeys(names) if missing(n)]
        if not self.installed_engine_app() and not self.recipe_engine_app():
            need.append("engine")
        return need

    def _manifest_items(self) -> dict:
        if not self.release_path or not os.path.exists(self.release_path):
            return {}
        if getattr(self, "_manifest_cache", None) is None:
            try:
                self._manifest_cache = download.load_manifest(self.release_path)["items"]
            except Exception:  # noqa: BLE001
                self._manifest_cache = {}
        return self._manifest_cache

    def install_dir_for(self, item: dict) -> str:
        if item.get("kind") == "engine":
            return os.path.join(self.env_dir, "engines", item.get("version") or self.recipe.engine["version"])
        return self.input_methods_dir if item.get("type") == "input-method" else self.apps_dir

    def start_downloads(self, names: Optional[list] = None) -> dict:
        if not self.release_path or not os.path.exists(self.release_path):
            # 源码构建（没有 Resources/release.json）：说清缺什么、该怎么准备（luokelabs/luoke-core#1）
            need = names or self.needed_downloads()
            want = self.recipe.engine["version"]
            hint = []
            if "engine" in need:
                hint.append(f"引擎：装官方 oMLX {want.replace('omlx-', '')} 到 {self.recipe.engine.get('app') or '/Applications/oMLX.app'}（版本要一样）")
            models = [n for n in need if n != "engine"]
            if models:
                hint.append("模型：把 " + "、".join(self.recipe.model(n)["dir"] for n in models) + " 放进模型目录 " + self.model_dir)
            raise BadRequest("unsupported", "这是开发构建，没有发布清单，不能下载。" + ("要自己准备——" + "；".join(hint) + "。见 README「开发版怎么跑」" if hint else "也没有缺的"))
        manifest = download.load_manifest(self.release_path)
        names = names or self.needed_downloads()
        missing = [n for n in names if n not in manifest["items"]]
        if missing:
            raise BadRequest("invalid_request", f"发布清单里没有：{', '.join(missing)}")
        for n in names:
            item = manifest["items"][n]
            self.downloads[n] = {"state": "queued", "done": 0, "total": download.item_total(item), "file": None, "error": None}
        with self.download_lock:
            if self.download_thread and self.download_thread.is_alive():
                # 正在下别的：排进队，当前这轮做完接着下（审查 2026-10-17：之前直接返回 started=False，调用方不看）
                self.download_queue.extend(n for n in names if n not in self.download_queue)
                self.log(f"downloads queued behind the running batch: {names}")
                return {"started": True, "queued": True, "downloads": self.downloads}
            self.download_thread = threading.Thread(target=self._download_worker, args=(manifest, names), daemon=True)
            self.download_thread.start()
        self.log(f"downloads queued: {names}")
        return {"started": True, "downloads": self.downloads}

    def _download_worker(self, manifest: dict, names: list) -> None:
        done_names: list = []
        while True:
            self._download_batch(manifest, names)
            done_names += names
            with self.download_lock:
                names = [n for n in self.download_queue if n not in names]
                self.download_queue = []
                if not names:
                    break
        self._after_downloads(manifest, done_names)

    def _download_batch(self, manifest: dict, names: list) -> None:
        for n in names:
            item = manifest["items"][n]
            d = self.downloads[n]
            d["state"] = "downloading"

            def prog(path, done, total, d=d):
                d["done"], d["total"], d["file"] = done, total, path
            try:
                kind = item.get("kind", "model")
                if kind == "model":
                    download.fetch_item(item, self.model_dir, prog, log=self.log)
                else:
                    # 配件 .app 或引擎包：zip 下到暂存目录（模型目录之外），核对后装到位
                    staged = download.fetch_item(item, os.path.join(self.env_dir, "downloads", "apps"), prog, log=self.log)
                    zips = [f["path"] for f in item["files"] if f["path"].endswith(".zip")]
                    if len(zips) != 1:
                        raise download.DownloadError(f"{n} 应当只有一个 zip")
                    dest = self.install_dir_for(item)
                    if kind == "engine":
                        app = download.install_app_zip(os.path.join(staged, zips[0]), dest, expect_name="oMLX.app", rename_if_bundle="app.omlx", register=False,
                                                       verify=not self.lab)
                    else:
                        old = accessories.by_accessory(self.manifests, item.get("accessory") or n)
                        if old and old.get("_bundled"):
                            raise download.DownloadError(f"{n} 在主控里（螺壳记忆并进主控后不再单独装），清单不该有这一项")
                        im = item.get("type") == "input-method"
                        if old:   # 升级：旧 .app 里还在跑的进程先结束（本体已按 bundle id 结束过，这里兜住启动器 exec 出的 python3），再整个替换
                            # 输入法不在这时结束：系统会马上从旧包再拉起一个，换好以后再结束（accessories.after_swap）
                            killed = 0 if im else accessories.terminate_app_processes(old["_app"])
                            self.log(f"upgrade {n}: replacing {old['_app']} ({old.get('version')} -> {item.get('version')}), stopped {killed} process(es)")
                        # 旧版挪进 previous.noindex 当回退；旧 .app 名字不同也一起挪（审查 2026-10-17）
                        app = download.install_app_zip(os.path.join(staged, zips[0]), dest, expect_bundle_id=item.get("bundle_id"),
                                                       register=not self.lab, verify=not self.lab,
                                                       backup_dir=os.path.join(self.env_dir, "previous.noindex") if old else None,
                                                       replace=old["_app"] if old else None)
                    shutil.rmtree(staged, ignore_errors=True)
                    if kind != "engine" and old and not self.lab:
                        stale = accessories.after_swap(app, os.path.join(self.env_dir, "previous.noindex", os.path.basename(old["_app"])), im)
                        self.log(f"upgrade {n}: stopped {stale} process(es) still running the replaced copy" + ("; relaunched input method" if im else ""))
                    d["installed"] = app
                    self.log(f"installed {n} -> {app}")
                d["state"] = "done"
                self.log(f"downloaded {n} ({d['total'] / 2**30:.1f} GB)")
            except Exception as e:  # noqa: BLE001
                d["state"] = "failed"
                d["error"] = str(e)[:200]
                self.log(f"download failed {n}: {d['error']}")

    def _after_downloads(self, manifest: dict, names: list) -> None:
        self.manifests = self.scan_accessories()
        if self.pending_tier and self._base_present(self.recipe, self.model_dir, self.pending_tier):
            name, self.pending_tier = self.pending_tier, None     # 换档等的底座下好了：现在换（下面会重启引擎）
            self.log(f"tier {self.tier_name} -> {name} (base downloaded)")
            self.tier_name, self.tier = name, self.recipe.tier(name)
            self.dispatcher.tier = self.tier
            self.base_name = self.recipe.base_for(name)
        if "engine" in names and self.downloads.get("engine", {}).get("state") == "done" and self.installed_engine_app():
            # 换用刚装上的引擎包：新的 Engine 对象，下面的重启用它。只看这一批：之前那批引擎的 done 一直留在 downloads 里，
            # 不看 names 的话之后每装一个应用都会停掉引擎、又不重启，守护线程按崩溃记，三次熔断（luoke-core#34）
            self.engine_app = self.installed_engine_app()
            self.engine_python = os.path.join(self.engine_app, self.recipe.engine["python"])
            self.engine_site = os.path.join(self.engine_app, self.recipe.engine["site"])
            try:
                self.engine.stop()
            except Exception:  # noqa: BLE001
                pass
            self.engine = Engine(self.engine_app, os.path.join(self.env_dir, "engine", self.recipe.engine["version"]), self.model_dir,
                                 self.engine.guard_gb, self.recipe.engine.get("log_level", "warning"), self.log)
            self.log(f"engine package installed: {self.engine_app}")
        # 引擎只在启动时发现模型：下了模型或引擎包才重启它，再做冒烟检查（只装了配件不用；引擎包还没装上就等下次）
        if any(self.downloads[n]["state"] == "done" and manifest["items"][n].get("kind", "model") in ("model", "engine") for n in names) \
                and not self.engine_missing():
            self.log("restarting engine after downloads")
            self.restart_engine()

    # ---- 预算：专用模型先扣，剩下的给 oMLX ------------------------------------------------------------------
    def specials_running_gb(self) -> float:
        total = 0.0
        for model, w in self.scorers.items():   # 工作进程按模型共用，键是模型名
            if w.state in ("loading", "ready"):
                total += float(self.recipe.model(model)["wired_gb"])
        return total

    def apply_budget(self) -> None:
        """天花板 = 本档预算 − 正在跑的专用模型。降之前先确认底座仍放得下，放不下就不降（oMLX 会把底座卸掉）。"""
        if not self.engine.alive():
            return
        want = round(self.tier["guard_gb"] - self.specials_running_gb(), 1)
        if abs(want - self.engine.guard_gb) < 0.05:
            return
        if want < self.engine.guard_gb and self.base_name:
            usable = (want - 2) * 0.95
            if self.recipe.model(self.base_name)["wired_gb"] > usable:
                self.log(f"budget {want} GB would evict the base; keeping {self.engine.guard_gb} GB")
                return
        self.engine.set_ceiling(want)

    # ---- 首次冒烟检查（V1 §6 第 5 步的开发版）-----------------------------------------------------------------
    def smoke_check(self, force: bool = False) -> dict:
        """能加载、能出合法 JSON、本机速度。结果记进 state.json，同一组合、同一测量方法（SMOKE_METHOD）不重复做。"""
        combo = f"{self.recipe.engine['version']}+{self.base_name}" if self.base_name else None
        prev = (self.state.get("smoke") or {})
        if not force and prev.get("combo") == combo and prev.get("ok"):
            if prev.get("method") == SMOKE_METHOD:
                self.log(f"smoke check cached ok for {combo}")
                return prev
            self.log(f"smoke check re-measuring {combo}: method {prev.get('method')} -> {SMOKE_METHOD}")
        rec = {"combo": combo, "method": SMOKE_METHOD, "ok": False, "date": time.strftime("%Y-%m-%d %H:%M"), "error": None}
        if not combo or not self.base_id:
            rec["error"] = "没有底座"
            self.smoke_failed = rec["error"]
            self.state["smoke"] = rec
            self._save_state()
            return rec
        if not self.fits_memory(self.base_name):
            # 这台机器现在被别的引擎占着（例如用户自己的 oMLX 加载着大模型）：不算失败，先不做，空出来再做
            rec["deferred"] = True
            rec["error"] = "内存被别的引擎占着，稍后再检查"
            self.smoke_failed = None
            self.state["smoke"] = rec
            self._save_state()
            self.log("smoke check deferred: not enough free memory now")
            return rec
        # 2026-10-16 16 GB 测试机实测：下载花了 72 分钟，底座的闲置时钟从引擎启动起算早已过期，冒烟检查刚把底座加载好，
        # 闲置卸载就把它卸了，请求挂到超时。检查期间闲置卸载不许动，时钟从加载那一刻重新起算。
        self.smoke_running = True
        holding = self.acquire_model(self.base_id, timeout=600)     # 本机端点正在用别的模型时，等它做完再量
        self.last_base_use = time.time()
        self.state["smoke"] = dict(rec, running=True)   # 界面据此显示"正在检查"，而不是上一次失败的旧结果（16 GB 测试机 2026-10-16：旧的 FAILED 还挂着就被点了继续）
        self._save_state()
        try:
            rec["load_secs"] = self.load_base()
            self.last_base_use = time.time()
            # 先空跑一条极短的：oMLX 0.7.1 起第一条生成要现编内核（rc1 实测首个 token 慢 2 s 多），不先跑掉，量出来的 prefill
            # 只有实际的几十分之一（0.8B 上 116 对 3900 tok/s），而后台估时、打断判断都用这个数（2026-10-19）
            self.engine.stream_chat({"model": self.base_id, "stream": True, "temperature": 0, "max_tokens": 1,
                                     "chat_template_kwargs": {"enable_thinking": False},
                                     "messages": [{"role": "user", "content": "你好"}]}, lambda l: True, threading.Event(), 120, 300)
            payload = {"model": self.base_id, "stream": True, "stream_options": {"include_usage": True}, "temperature": 0,
                       "max_tokens": 120, "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": "用 JSON 回答：{\"ok\": true, \"text\": \"<一句 60 字左右的中文问候>\"}。" * 8}],
                       "response_format": {"type": "json_schema", "json_schema": {"name": "smoke", "schema": {
                           "type": "object", "properties": {"ok": {"type": "boolean"}, "text": {"type": "string"}}, "required": ["ok", "text"]}}}}
            chunks = []
            t0 = time.time()
            info = self.engine.stream_chat(payload, lambda l: (chunks.append(l) or True), threading.Event(), 120, 300)
            wall = time.time() - t0
            if info["error"] or info["status"] != 200:
                raise RuntimeError(info["error"] or f"status {info['status']}")
            text = "".join((ch.get("delta") or {}).get("content") or ""
                           for l in chunks if l.startswith("data: ") and l[6:] != "[DONE]"
                           for ch in (json.loads(l[6:]).get("choices") or []))
            parsed = json.loads(text)
            if "ok" not in parsed:
                raise RuntimeError("JSON 里没有 ok")
            u = info.get("usage") or {}
            pt, ct, ttft = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0, info["first_token_secs"] or wall
            rec["prefill_tok_s"] = round(pt / ttft, 1) if ttft > 0 else None
            rec["decode_tok_s"] = round((ct - 1) / max(wall - ttft, 1e-3), 1) if ct > 1 else None
            rec["ok"] = True
            self.smoke_failed = None
            self.last_base_use = time.time()
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"[:200]
            self.smoke_failed = rec["error"]
        finally:
            self.smoke_running = False
            if holding:
                self.release_model(self.base_id)
        self.state["smoke"] = rec
        self._save_state()
        self.log(f"smoke check {combo}: {'ok' if rec['ok'] else 'FAILED ' + str(rec['error'])} "
                 f"load={rec.get('load_secs')} prefill={rec.get('prefill_tok_s')} decode={rec.get('decode_tok_s')}")
        if rec["ok"]:
            self.prune_old_engines()
        if not self.tier.get("base_resident", True):
            self.engine.unload()
        return rec

    def prune_old_engines(self) -> list:
        """新引擎冒烟检查过了：engines/ 里别的版本删掉（每份约 1.6 GB，换一次引擎留一份，10-27 定进 beta1.1）。
        只在发布版、正跑着清单装的引擎包时做；开发版和实验室实例不动。旧版主控（previous.noindex）缺引擎时照常重新下载。"""
        cur = self.installed_engine_app()
        if self.lab or not self.release_path or not os.path.exists(self.release_path) or not cur:
            return []
        root = os.path.join(self.env_dir, "engines")
        removed = []
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if name == self.recipe.engine["version"] or not name.startswith("omlx-") or os.path.islink(p) or not os.path.isdir(p):
                continue
            shutil.rmtree(p, ignore_errors=True)
            removed.append(name)
        if removed:
            self.log(f"removed old engines: {', '.join(removed)}")
        return removed

    def measured_speed(self, key: str, default: float) -> float:
        sm = self.state.get("smoke") or {}
        v = sm.get(key)
        # 旧量法的数不用（重测之前按配方的值）：旧的 prefill 偏低好几倍，后台估时、打断判断会跟着偏
        return float(v) if sm.get("ok") and v and sm.get("method") == SMOKE_METHOD else default

    def threshold_ms(self, task: str) -> Optional[float]:
        lat = (self.recipe.task(task) or {}).get("latency_ms") or {}
        v = lat.get(self.tier_name, lat.get("*"))
        return float(v) if v else None

    def live_p90(self, task: str) -> Optional[dict]:
        xs = list(self.score_full_ms.get(task) or [])
        return {"p90_ms": round(p90_of(xs), 1), "n": len(xs)} if len(xs) >= 20 else None

    def slow_info(self, task: str) -> Optional[dict]:
        """探针的 p90 对阈值，带滞回；没探过（或配方没写阈值）就不标。"""
        t = self.recipe.task(task)
        if not t or t["kind"] not in ("score", "predict"):
            return None
        thr = self.threshold_ms(task)
        model = self.recipe.model_for_task(task, self.tier_name)
        probe = (self.state.get("probe") or {}).get(model or "")
        if not thr or not probe:
            return None
        p90 = probe.get("predict_p90_ms" if t["kind"] == "predict" else "score_p90_ms")
        if p90 is None:
            return None
        prev = bool((self.state.get("slow") or {}).get(task))
        slow = decide_slow(prev, float(p90), thr)
        if slow != prev:
            self.state.setdefault("slow", {})[task] = slow
            self._save_state()
            self.log(f"slow {task}: {'set' if slow else 'cleared'} (probe p90 {p90} ms, threshold {thr} ms)")
        return {"measured_ms": round(float(p90), 1), "threshold_ms": thr, "source": "probe", "date": probe.get("date")} if slow else None

    def probe_shape(self, kind: str, model: str) -> Optional[dict]:
        """本档上用这个模型的某个 kind 任务的参数里的 probe 块：{"context", "start", "candidates"}（candidates 只打分任务要）。坏的当没有。"""
        for name, t in self.recipe.tasks.items():
            if t["kind"] != kind or self.recipe.model_for_task(name, self.tier_name) != model:
                continue
            pr = (self.recipe.params(name, self.tier_name) or {}).get("probe")
            if not isinstance(pr, dict) or not isinstance(pr.get("context"), str) or len(pr["context"]) < 8:
                continue
            start = int(pr.get("start") or 0)
            if not 0 < start < len(pr["context"]):
                continue
            cands = pr.get("candidates")
            if kind == "score" and not (isinstance(cands, list) and cands and all(isinstance(c, str) and c for c in cands)):
                continue
            stop = pr.get("stop") if isinstance(pr.get("stop"), list) else []
            return {"context": pr["context"], "start": start, "candidates": list(cands) if kind == "score" else [], "stop": [x for x in stop if isinstance(x, str) and x]}
        return None

    def task_cap(self, kind: str, model: str, key: str, default: int) -> int:
        for name, t in self.recipe.tasks.items():
            if t["kind"] == kind and self.recipe.model_for_task(name, self.tier_name) == model:
                try:
                    return int((self.recipe.params(name, self.tier_name) or {}).get(key) or default)
                except (TypeError, ValueError):
                    return default
        return default

    def _probe_scorers(self) -> None:
        """打分进程就绪后、机器不忙时，用固定形状测一次打分和联想的 p90（见文件头）。"""
        d = self.dispatcher
        if d.job or self.engine.loading or d.now() - d.last_score < PROBE_QUIET_SECS:
            return      # 别和打字抢：最近几秒有打分就等下一轮
        for model, w in list(self.scorers.items()):
            if w.kind == "embed" or w.state != "ready" or w.probed or w.probe_attempts >= 3:
                continue      # 嵌入进程不探（没有延迟阈值，也不标慢）
            sp, pp = self.probe_shape("score", model), self.probe_shape("predict", model)
            if not sp and not pp:
                w.probed = True
                self.log(f"probe {model}: no probe shape in the recipe, not measured")
                continue
            w.probe_attempts += 1
            try:
                ms = [float(w.score(sp["context"][:sp["start"] + i], sp["candidates"], timeout=10.0).get("ms") or 0)
                      for i in range(PROBE_SCORE_RUNS)] if sp else []
                # 联想探针走通用续写（输入法真实用的那条路；k、max_tokens 是任务参数的上限，stop 是配件在 probe 块里给的标点表）
                pm = [float(w.continue_(pp["context"][:pp["start"] + i], self.task_cap("predict", model, "k", 8), self.task_cap("predict", model, "max_tokens", 6),
                                        pp["stop"], timeout=30.0).get("ms") or 0) for i in range(PROBE_PREDICT_RUNS)] if pp else []
            except ScorerError as e:
                self.log(f"probe {model} failed ({e}), attempt {w.probe_attempts}")
                return
            rec = {"combo": f"{self.recipe.engine['version']}+{model}", "score_p90_ms": round(p90_of(ms, PROBE_WARMUP), 1) if ms else None,
                   "predict_p90_ms": round(p90_of(pm, 2), 1) if pm else None, "runs": [len(ms), len(pm)],
                   "candidates": len(sp["candidates"]) if sp else 0, "date": time.strftime("%Y-%m-%d %H:%M"), "base_loaded": bool(self.engine.loaded)}
            self.state.setdefault("probe", {})[model] = rec
            self._save_state()
            w.probed = True
            self.log(f"probe {model}: score p90 {rec['score_p90_ms']} ms, predict p90 {rec['predict_p90_ms']} ms (base loaded: {rec['base_loaded']})")

    def _snapshot_tasks(self) -> None:
        """各任务的状态、slow、线上 p90 写进 state.json（只读检查用，例如试用期 luoke-top 每天看）。变了才写。"""
        snap = {}
        for name in self.recipe.tasks:
            st = self.task_status(name)
            snap[name] = {"status": st["status"], "reason": st["reason"], "slow": self.slow_info(name), "live": self.live_p90(name)}
        if snap != self.state.get("tasks"):
            self.state["tasks"] = snap
            self.state["tasks_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save_state()

    # ---- 日志：只记元数据 ------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        with self.loglock:
            self.logf.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
            self.logf.flush()

    # ---- 生命周期 ------------------------------------------------------------------------------------
    def start(self) -> None:
        self.log(f"hub {__version__} tier={self.tier_name} mem={self.mem_gb:.0f}GB base={self.base_name} recipe={self.recipe.version}")
        if not self.lab:
            try:   # 螺壳记忆并进主控（V1 §6）：旧的单独 螺壳记忆.app 迁走；mem.db 不动
                if accessories.migrate_old_memory_app(log=self.log):
                    self.manifests = self.scan_accessories()
            except Exception as e:  # noqa: BLE001
                self.log(f"migrate memory failed: {type(e).__name__}: {e}")
            # 回滚备份目录里的旧版：只留着，不该登记在 LaunchServices 里（luoke-ime 10-22：开发机上 previous.noindex 的输入法是登记着的）
            gone = apps.unregister_dir(accessories._previous_dir())
            if gone:
                self.log(f"unregistered {len(gone)} kept previous copy(ies)")
        want = [self.recipe.model(self.base_name)["dir"]] if self.base_name else []
        want = [w for w in want if os.path.isdir(os.path.join(self.model_dir, w))]   # 还没下载的不等它出现
        if self.engine_missing():
            # 干净的 Mac：引擎包还没下载。主控照常起来接下载和状态，任务答"需下载"，装上后再起引擎（_download_worker）
            self.log(f"engine not installed yet ({self.engine_app}); waiting for the engine package")
        else:
            ids = self.engine.start(wait_models=want)
            if self.base_name:
                self.base_id = self.engine.resolve(self.recipe.model(self.base_name)["dir"])
                if not self.base_id:
                    self.log(f"base model dir missing: {want} (have {ids})")
            if self.smoke and self.base_id:
                self.smoke_check()
        threading.Thread(target=self._sampler, daemon=True).start()
        self.phase = "running"
        if self.settings.get("endpoint_enabled") and not self.lab:
            self.endpoint.start(int(self.settings.get("endpoint_port") or endpoint_mod.DEFAULT_PORT))

    # ---- 给高级用户的本机端点（顶层 ADR 0014 修订，2026-10-19 定；luoke_core/endpoint.py）-------------------------
    def acquire_model(self, model_id: str, timeout: float = 600) -> bool:
        """要用 model_id 生成：没人在用别的模型才放行（之后由调用方 engine.load，它会先卸掉别的）。
        只看"有没有人在用别的"，不看引擎里现在是谁：放行了一个底座请求、它还没来得及加载时，引擎里还是旧模型，按"引擎里正是它"放行就会被卸掉。"""
        with self.model_cv:
            ok = self.model_cv.wait_for(lambda: not any(n for m, n in self.model_users.items() if m != model_id), timeout)
            if ok:
                self.model_users[model_id] = self.model_users.get(model_id, 0) + 1
            return ok

    def release_model(self, model_id: str) -> None:
        with self.model_cv:
            self.model_users[model_id] = max(0, self.model_users.get(model_id, 0) - 1)
            self.model_cv.notify_all()

    def load_base(self) -> float:
        """装底座（会先卸掉独占的用户模型），再把引擎的天花板还原成本档的预算（独占用户模型时放大过）。"""
        secs = self.engine.load(self.base_id)
        self.apply_budget()
        return secs

    def exclusive_ceiling_gb(self) -> float:
        """用户模型独占引擎时给它的上限：照 oMLX 自己的 balanced 档给系统和别的应用留约 8% 内存（3–8 GB），
        再扣掉常驻的专用模型（打分、嵌入），留 1 GB 余量。"""
        reserve = min(8.0, max(3.0, 0.08 * self.mem_gb))
        return round(max(0.0, self.mem_gb - reserve - self.specials_running_gb() - 1.0), 1)

    def fits_alongside(self, size_gb: float) -> bool:
        """一个用户模型能不能和现在内存里的（底座 + 已经一起放着的）同时放下：配方给引擎的上限算一道，这台机器真剩下的内存算一道。
        放得下就一起加载、不挤底座（luoke-top 10-19 实测：3 GB 的模型把 26B 底座挤掉了，不该）。"""
        usable = (self.tier["guard_gb"] - 2) * 0.95
        used = 0.0
        if self.engine.loaded and self.engine.loaded == self.base_id and self.base_name:
            m = self.recipe.model(self.base_name)
            used += float(m.get("size_gb", m["wired_gb"]))
        for x in self.engine.extras:
            um = next((u for u in self.user_models.list() if x == u["dir"] or x.endswith("/" + u["dir"])), None)
            used += float(um["size_gb"]) if um else 0.0
        if used + size_gb > usable:
            return False
        wired = machine.memory_sample().get("wired_gb")
        if wired is None:
            return True
        return size_gb + 1.0 <= machine.metal_cap_gb(self.mem_gb) - wired

    def is_user_model(self, model_id: Optional[str]) -> bool:
        return bool(model_id) and any(model_id == m["dir"] or model_id.endswith("/" + m["dir"]) for m in self.user_models.list())

    def endpoint_models(self) -> list:
        own = [self.base_name] if self.base_name else []
        return own + [endpoint_mod.USER_PREFIX + m["name"] for m in self.user_models.list()]

    def endpoint_status(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        return {"enabled": bool(self.settings.get("endpoint_enabled")), "port": self.endpoint.port,
                "want_port": self.settings.get("endpoint_port"), "models": self.endpoint_models(),
                "user_models": self.user_models.list(),
                "usage_today": (self.state.get("endpoint_usage") or {}).get(today, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})}

    def _endpoint_usage(self, usage: Optional[dict]) -> None:
        today = time.strftime("%Y-%m-%d")
        u = self.state.setdefault("endpoint_usage", {})
        for d in [k for k in u if k < time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))]:
            u.pop(d, None)             # 只留 30 天
        day = u.setdefault(today, {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})
        day["requests"] += 1
        for k in ("prompt_tokens", "completion_tokens"):
            day[k] += int((usage or {}).get(k) or 0)
        self._save_state()

    def endpoint_chat(self, handler, body: dict) -> None:
        """端点的一条 chat/completions。算"用户在等"：打断后台，等手上用别的模型的请求做完，换模型（放不下答 507），转给引擎。"""
        name = body["model"]
        t0 = time.time()
        if self.phase != "running" or not self.engine.serving():
            return handler._err(503, "not_ready", "螺壳还没准备好")
        if name.startswith(endpoint_mod.USER_PREFIX):
            m = self.user_models.get(name[len(endpoint_mod.USER_PREFIX):])
            if not m:
                return handler._err(404, "model_not_found", f"没有登记 {name}")
            ceiling = self.exclusive_ceiling_gb()
            if m["size_gb"] > ceiling:
                return handler._err(507, "too_large", f"{name} 有 {m['size_gb']} GB，这台 Mac 上独占时最多能给引擎 {ceiling:.0f} GB")
            model_id = self.engine.resolve(m["dir"])
            if not model_id:
                return handler._err(503, "not_ready", "引擎还没发现这个模型（刚登记的要等引擎重启完）")
        elif name in (self.base_name, self.base_id):
            model_id = self.base_id
        else:
            return handler._err(404, "model_not_found", f"没有 {name}；GET /v1/models 看能用的")
        # 功耗档管端点（10-22：功耗档是全局的，打字和配件里手动点的除外）：上一条做完按它的用时歇，歇够了再开始。
        # 连着发很多请求的（pi 的代理循环）整体就慢下来；单条请求生成到一半没法慢（oMLX 不能限速）
        rested = 0.0
        while True:
            wait = self.dispatcher.endpoint_try_begin()     # 0 = 记上"正在做"了；之后每条路都要 endpoint_end
            if wait <= 0:
                break
            if self.phase != "running":
                return handler._err(503, "not_ready", "螺壳正在退出")
            rested += min(wait, 1.0)
            time.sleep(min(wait, 1.0))       # 一秒一看：中途切到全速就不再等
        interrupted = self.dispatcher.user_waiting_arrived()
        if interrupted:
            self.log(f"background {interrupted.task} interrupted for endpoint")
        user = name.startswith(endpoint_mod.USER_PREFIX)
        # 用户模型和底座一起放得下（或已经一起放着）：一起加载，不挤底座、不占换模型的那一格；放不下才走独占：等、卸底座、换
        alongside = user and (model_id in self.engine.extras or (self.engine.loaded != model_id and self.fits_alongside(m["size_gb"])))
        if alongside:
            with self.model_cv:
                self.extra_users[model_id] = self.extra_users.get(model_id, 0) + 1
        elif not self.acquire_model(model_id):
            self.dispatcher.endpoint_end(0.0)
            return handler._err(503, "busy", "别的请求还在用另一个模型，稍后再试")
        cancel = threading.Event()
        outcome = "ok"
        usage = None
        gen_secs = 0.0
        try:
            try:
                if user and not alongside:
                    # 独占用户模型：天花板放到和用户自己 oMLX 的 balanced 档一样（留约 8% 内存），由 oMLX 的守卫最后把关；
                    # 换回底座时 apply_budget 还原（10-19 负责人的 Flash-Next 111.55 GB 被按 93 GB 的底座预算挡了）
                    self.engine.set_ceiling(self.exclusive_ceiling_gb())
                (self.engine.load_extra if alongside else self.engine.load)(model_id)
            except EngineError as e:
                outcome = "load_failed"
                return handler._err(507 if str(e) == "too_large" else 503, "too_large" if str(e) == "too_large" else "engine_failed",
                                    "放不下这个模型" if str(e) == "too_large" else f"加载失败：{e}")
            if self.is_user_model(model_id):
                self.user_model_last_use = time.time()
            else:
                self.last_base_use = time.time()
            stream = bool(body.get("stream"))
            payload = {k: body[k] for k in endpoint_mod.PASS_FIELDS if k in body}
            payload["model"] = model_id
            if stream:
                payload["stream_options"] = dict(payload.get("stream_options") or {}, include_usage=True)
            t_gen = time.time()
            info = endpoint_mod.forward(self.engine, payload, handler, stream, cancel)
            gen_secs = time.time() - t_gen
            usage = info.get("usage")
            outcome = info.get("error") or (f"status_{info['status']}" if info.get("status") != 200 else "ok")
        finally:
            if self.is_user_model(model_id):
                self.user_model_last_use = time.time()
            if alongside:
                with self.model_cv:
                    self.extra_users[model_id] = max(0, self.extra_users.get(model_id, 0) - 1)
            else:
                self.release_model(model_id)
            self._endpoint_usage(usage)
            rest = self.dispatcher.endpoint_end(gen_secs if outcome == "ok" else 0.0)
            self.log(f"endpoint chat model={name} stream={bool(body.get('stream'))} prompt_tokens={(usage or {}).get('prompt_tokens')} "
                     f"completion_tokens={(usage or {}).get('completion_tokens')} secs={time.time() - t0:.1f} outcome={outcome} "
                     f"waited={rested:.0f} rest_next={rest:.0f} power={self.dispatcher.power_mode}")

    def register_user_model(self, path: str, name: Optional[str] = None) -> dict:
        try:
            m = self.user_models.add(path, name)
        except (ValueError, OSError) as e:
            raise BadRequest("invalid_request", f"登记不了：{e}")
        threading.Thread(target=self.restart_engine, daemon=True).start()   # oMLX 只在启动时发现模型
        return m

    def remove_user_model(self, name: str) -> bool:
        m = self.user_models.get(name)
        if not m:
            return False
        model_id = self.engine.resolve(m["dir"]) if self.engine.serving() else None
        if model_id and (self.engine.loaded == model_id or model_id in self.engine.extras):
            with self.model_cv:
                self.model_cv.wait_for(lambda: not self.model_users.get(model_id) and not self.extra_users.get(model_id), 600)
            self.engine.unload(model_id)
        ok = self.user_models.remove(name)
        threading.Thread(target=self.restart_engine, daemon=True).start()
        return ok

    def engine_missing(self) -> bool:
        return not os.path.exists(self.engine.cli)

    def stop(self) -> None:
        self.phase = "stopped"
        self.endpoint.stop()
        self.jobs.stop()
        self.stop_event.set()
        for w in self.scorers.values():
            w.stop()
        self.engine.stop()
        self.log("hub stopped")

    def _sampler(self) -> None:
        while not self.stop_event.wait(SAMPLE_SECS):
            d = self.dispatcher
            try:
                self._check_engine()
                d.on_ac = machine.on_ac()
                d.low_power = machine.low_power_mode()
                d.thermal = machine.thermal_level()
                # 内存压力看内核的等级，不猜。16 GB 档上我们自己加载底座就会把它顶到 warn（这是定下时接受的代价），
                # 所以底座在内存里时 warn 不算数，只有 critical 才动；底座不在时的 warn 是别人在挤，照规则办。
                level = machine.pressure_level()
                self._idle_workers(level)
                if level == "warn" and (self.engine.loaded or self.engine.loading):
                    level = "normal"      # 正在加载时的 warn 也是我们自己造成的
                before = d.background_state()[0]
                d.set_memory_pressure(level)
                after, reasons = d.background_state()
                if before == "running" and after == "paused":
                    job = d.pause_reason_changed(reasons)
                    if job:
                        self.log(f"background interrupted: {','.join(reasons)}")
                self._idle_unload()
                self._retry_smoke()
                self._probe_scorers()
                if time.time() - self._manifest_rescan_at > 60:        # 新装、换装的配件：一分钟内看到它的 jobs
                    self.manifests = self.scan_accessories()
                    self._manifest_rescan_at = time.time()
                self.jobs.tick(self.manifests)
                self._snapshot_tasks()
            except Exception as e:  # noqa: BLE001
                self.log(f"sampler error: {e}")

    def _retry_smoke(self) -> None:
        sm = self.state.get("smoke") or {}
        if not sm.get("deferred") or self.dispatcher.job is not None or self.dispatcher.interactive():
            return
        if time.time() - getattr(self, "_last_smoke_try", 0) < 300:
            return
        self._last_smoke_try = time.time()
        if self.base_name and self.fits_memory(self.base_name):
            self.log("retrying deferred smoke check")
            self.smoke_check(force=True)

    def _check_engine(self) -> None:
        if self.engine_missing():
            return
        """引擎进程没了：记一次崩溃；短时间内反复崩就熔断，否则重启。"""
        if self.engine.alive() or self.breaker or self.stop_event.is_set():
            return
        if not self.restarting.acquire(blocking=False):
            return
        try:
            now = time.time()
            self.engine_crashes.append(now)
            recent = [t for t in self.engine_crashes if now - t < BREAKER_WINDOW_SECS]
            job = self.dispatcher.job
            if job:
                self.dispatcher.end_background(job, 0)
            if len(recent) >= BREAKER_CRASHES:
                self.breaker = f"引擎 {BREAKER_WINDOW_SECS // 60} 分钟内崩溃 {len(recent)} 次，已熔断"
                self.log("engine BREAKER: " + self.breaker)
                return
            self.log(f"engine crashed (rc={self.engine.proc.returncode if self.engine.proc else '?'}), restarting ({len(recent)}/{BREAKER_CRASHES})")
            want = [self.recipe.model(self.base_name)["dir"]] if self.base_name else []
            want = [w for w in want if os.path.isdir(os.path.join(self.model_dir, w))]
            try:
                self.engine.start(wait_models=want)
                self.base_id = self.engine.resolve(want[0]) if want else None
                self.apply_budget()
            except EngineError as e:
                self.log(f"engine restart failed: {e}")
        finally:
            self.restarting.release()

    def _idle_unload(self) -> None:
        for x in list(self.engine.extras):      # 和底座一起放着的用户模型：闲了就卸，底座不动
            if time.time() - self.user_model_last_use > endpoint_mod.USER_MODEL_IDLE_SECS and not self.extra_users.get(x):
                if self.engine.unload(x):
                    self.log(f"endpoint: user model {x} unloaded (idle, was alongside the base)")
        if not self.engine.loaded or self.dispatcher.job is not None or getattr(self, "smoke_running", False):
            return
        if self.is_user_model(self.engine.loaded):
            # 本机端点的用户模型闲了就卸掉、换回底座（ADR 0014 修订）；还有请求在用就不动
            loaded = self.engine.loaded
            if time.time() - self.user_model_last_use > endpoint_mod.USER_MODEL_IDLE_SECS and not self.model_users.get(loaded):
                if self.engine.unload(loaded):
                    self.log(f"endpoint: user model {loaded} unloaded (idle)")
                if self.tier.get("base_resident", True) and self.base_id:
                    threading.Thread(target=self._reload_base, daemon=True).start()
            return
        # 16 GB 档：用户回来打字了，底座必须让路——预热租约也拦不住（打字的延迟比预热要紧；要用时再冷加载）
        if self.unload_on_next_score and not self.tier.get("base_resident", True):
            self.unload_on_next_score = False
            if self.engine.unload():   # 同一秒几条打分各起一个线程来卸：只有真卸了的那个记日志（16 GB 测试机 2026-10-16 记了三条）
                self.log("base unloaded: user typing on a non-resident tier")
            return
        if self.dispatcher.leased():
            return
        idle = time.time() - self.last_base_use
        limit = self.tier.get("idle_unload_secs", 1800)
        if idle > limit:
            self.engine.unload()
            self.log(f"base unloaded (idle {idle:.0f}s, resident={self.tier.get('base_resident')})")

    def _reload_base(self) -> None:
        if not self.acquire_model(self.base_id, timeout=5):
            return
        try:
            self.load_base()
            self.last_base_use = time.time()
        except EngineError as e:
            self.log(f"base reload failed: {e}")
        finally:
            self.release_model(self.base_id)

    # ---- 配件与任务 ------------------------------------------------------------------------------------
    def check_accessory(self, header: Optional[str], task: Optional[str] = None) -> tuple:
        if not header or "/" not in header:
            raise BadRequest("unknown_accessory", "缺 X-Luoke-Accessory: <配件标识>/<版本>")
        name, version = header.split("/", 1)
        if not self.version_accepted(name, version):
            raise BadRequest("unknown_accessory", f"{name}/{version} 不在已验证版本里")
        if task is not None:
            t = self.recipe.task(task)
            if not t:
                raise BadRequest("task_unavailable", f"没有任务 {task}", )
            if t["accessory"] != name:
                raise BadRequest("unknown_accessory", f"任务 {task} 不归 {name}")
            past = None if self.lab or self.recipe.version_verified(name, version) or self.locally_allowed(name, version) \
                else self.history_entry(name, version)
            if past is not None and task not in past.get("tasks", []):
                raise BadRequest("task_unavailable", f"{task} 要{name}升级到这一版螺壳验证过的版本才能用（{version} 当时在 "
                                                     f"{past.get('release') or '上一版'}里没验证过这一项），到「应用」里升级就好")
        return name, version

    # ---- 本机放行（试用期，2026-10-17 定）：内部版 / 本机版主控上，项目会话把自己签名公证过的新版配件追加进来，不用等下一版主控 ----
    LOCAL_ALLOW = "local-verified.json"

    def release_tag(self) -> Optional[str]:
        if self._release_tag is None:
            try:
                with open(self.release_path or "", encoding="utf-8") as f:
                    self._release_tag = str(json.load(f).get("release") or "")
            except (OSError, ValueError):
                self._release_tag = ""
        return self._release_tag or None

    def local_allow_enabled(self) -> bool:
        """只有内部版（internal-*）和本机版（local-*）主控认本机放行；beta 和公开发布的校验一点不放松。"""
        return not self.lab and bool(re.match(r"(internal|local)-", self.release_tag() or ""))

    def local_allow_list(self) -> list:
        """local-verified.json 里还有效的条目：配件得已经在配方里（只放行新版本，不放行新配件），版本不在配方里（在了就不算绕过）。"""
        if not self.local_allow_enabled():
            return []
        p = os.path.join(self.env_dir, self.LOCAL_ALLOW)
        try:
            mtime = os.stat(p).st_mtime
        except OSError:
            self._local_allow = (None, [])
            return []
        if self._local_allow[0] != mtime:
            try:
                with open(p, encoding="utf-8") as f:
                    raw = json.load(f).get("allowed") or []
            except (OSError, ValueError) as e:
                self.log(f"local allow: {p} unreadable ({e}), ignored")
                raw = []
            ok = [e for e in raw if isinstance(e, dict) and isinstance(e.get("accessory"), str) and isinstance(e.get("version"), str)
                  and e["accessory"] in self.recipe.verified and e["accessory"] != "luoke-core"
                  and not self.recipe.version_verified(e["accessory"], e["version"])]
            self._local_allow = (mtime, ok)
            self.log("local allow: " + ("; ".join(f"{e['accessory']} {e['version']} (by {e.get('by', '?')}: {e.get('reason', '')})" for e in ok) or "none"))
        return self._local_allow[1]

    def locally_allowed(self, name: str, version: str) -> bool:
        hit = any(e["accessory"] == name and e["version"] == version for e in self.local_allow_list())
        if hit and (name, version) not in self._local_allow_used:
            self._local_allow_used.add((name, version))
            self.log(f"local allow used: {name}/{version} accepted without release evidence")
        return hit

    def model_dir_of(self, model_name: str) -> str:
        return os.path.join(self.model_dir, self.recipe.model(model_name)["dir"])

    def task_status(self, task: str) -> dict:
        t = self.recipe.task(task)
        if t.get("tiers") and self.tier_name not in t["tiers"]:       # 发布脚本按证据填：硬性条目没过的档不支持
            return {"status": "unsupported", "reason": "这一档没通过验证", "combo": None}
        if self.engine_missing():
            d = self.downloads.get("engine")
            return {"status": "downloading" if d and d.get("state") in ("queued", "downloading") else "needs_download",
                    "reason": "引擎包正在下载" if d and d.get("state") == "downloading" else "引擎包还没下载", "combo": None}
        model = self.recipe.model_for_task(task, self.tier_name)
        if not model:
            return {"status": "unsupported", "reason": "本档没有这个任务用的模型", "combo": None}
        combo = f"{self.recipe.engine['version']}+{model}"
        if not os.path.isdir(self.model_dir_of(model)):
            d = self.downloads.get(model)
            if d and d["state"] in ("queued", "downloading"):
                return {"status": "downloading", "reason": "正在下载" if d["state"] == "downloading" else "排队等下载",
                        "progress": (d["done"] / d["total"]) if d.get("total") else None, "combo": combo}
            if d and d["state"] == "failed":
                return {"status": "needs_download", "reason": "下载失败：" + str(d.get("error"))[:120], "combo": combo}
            return {"status": "needs_download", "reason": "模型还没下载", "combo": combo}
        if t["kind"] == "generate":
            if self.breaker:
                return {"status": "paused", "reason": self.breaker, "combo": combo}
            if not self.engine.serving():
                return {"status": "paused", "reason": "引擎正在启动或重启" if self.engine.alive() else "引擎没在运行", "combo": combo}
            if not self.base_id:
                return {"status": "paused", "reason": "引擎里找不到底座", "combo": combo}
            if self.smoke_failed:
                return {"status": "paused", "reason": "本机检查未通过：" + self.smoke_failed, "combo": combo}
        else:
            w = self.scorers.get(model)
            if w and w.failures >= 3:
                return {"status": "paused", "reason": "专用模型的工作进程反复失败（熔断）", "combo": combo}
        return {"status": "available", "reason": None, "combo": combo}

    def tasks_version(self) -> str:
        """任务参数的版本：配方、档位、模型分配、各任务参数、引擎版本里任何一样变了它就变。配件看到它变就重读 GET /tasks。"""
        basis = {"recipe": self.recipe.version, "tier": self.tier_name, "assignment": self.recipe.assignment.get(self.tier_name),
                 "engine": self.recipe.engine.get("version"),
                 "params": {n: self.recipe.params(n, self.tier_name) for n in self.recipe.tasks}}
        spaces = {n: self.recipe.vector_space(n, self.tier_name) for n, t in self.recipe.tasks.items() if t["kind"] == "embed"}
        if spaces:      # 只在有嵌入任务时进摘要：没有嵌入任务的配方，版本串和以前一样
            basis["vector_spaces"] = spaces
        return hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]

    def tasks_payload(self, accessory: str) -> dict:
        state, reasons = self.dispatcher.background_state()
        tasks = {}
        for name, t in self.recipe.tasks_for(accessory).items():
            st = self.task_status(name)
            tasks[name] = {"kind": t["kind"], "main": bool(t.get("main")), "status": st["status"], "reason": st["reason"],
                           "progress": st.get("progress"), "slow": self.slow_info(name), "combo": st["combo"],
                           "params": self.recipe.params(name, self.tier_name)}
            m = self.recipe.model_for_task(name, self.tier_name)
            # 给人看的模型名（10-20 应 luoke-mail 加，只加不改）：设置页要显示"用的是哪个模型"，combo 是不透明标签、不该拆；本档没模型时 null
            tasks[name]["model_name"] = (self.recipe.model(m).get("label") or os.path.basename(self.recipe.model(m)["dir"])) if m else None
            if t["kind"] == "embed":      # 向量空间（2026-10-16 应 digup 加，只加不改）：只比相等，不解析；本档没模型时 null
                tasks[name]["vector_space"] = self.recipe.vector_space(name, self.tier_name)
        out = {"contract": CONTRACT, "hub_version": __version__, "tier": self.tier_name, "tasks_version": self.tasks_version(),
               "background": {"state": state, "reasons": reasons}, "tasks": tasks}
        lib = None if self.lab else accessories.bundled_memory_lib()
        if lib:
            out["luoke_mem"] = lib      # 配件用主控里这份记忆库（10-19 定）；每次新起进程时问一次，主控升级后路径会变
        return out

    # ---- 内存 -----------------------------------------------------------------------------------------
    def fits_memory(self, model_name: str) -> bool:
        """两道关：配方预算装得下；这台机器现在真的还剩这么多（别的引擎占着的 wired 内存算进去，V1 §3"从不硬挤"）。"""
        m = self.recipe.model(model_name)
        size = float(m.get("size_gb", m["wired_gb"]))     # 引擎按文件大小算（oMLX 的守卫看的是这个）
        usable = (self.tier["guard_gb"] - 2) * 0.95
        if size > usable:
            return False
        if self.engine.loaded and self.engine.loaded in (model_name, self.base_id):
            return True                            # 已经在内存里了，不用再算
        sample = machine.memory_sample()
        wired = sample.get("wired_gb")
        if wired is None:
            return True
        cap = machine.metal_cap_gb(self.mem_gb)
        free_for_us = cap - wired                  # Metal 上限减去现在所有人（别的引擎、系统、我们的打分进程）占着的
        margin = 0.0 if not self.tier.get("base_resident", True) else 1.0   # 16 GB 档本来就是挤着用（安静时刻、用完就卸）
        if size + margin > free_for_us:
            self.log(f"memory: need {size} GB (+{margin:.0f}), metal cap {cap} - wired {wired} leaves {free_for_us:.1f} GB; refusing to load")
            return False
        return True

    def occupants(self) -> list:
        mine = {self.engine.proc.pid} if self.engine.proc else set()
        for w in self.scorers.values():
            if w.proc:
                mine.add(w.proc.pid)
        return machine.other_engines(mine)

    # ---- 打分 -----------------------------------------------------------------------------------------
    def worker_hold_reason(self, model: str) -> Optional[str]:
        """要起专用模型工作进程之前：内存压力刚到 critical 不起；这台机器还剩的（Metal 上限减去现在 wired 的，引擎里我们自己的模型
        算作能让出来的：预算重算时引擎会给它让位，16 GB 档打分时底座会卸）装不下它常驻的占用也不起。答原因，能起答 None。"""
        if time.time() < self.worker_hold_until:
            return "内存紧张，专用模型先不起"
        need = float(self.recipe.model(model).get("wired_gb") or 0)
        sample = machine.memory_sample()
        wired = sample.get("wired_gb")
        if not need or wired is None:
            return None
        ours = (float(self.recipe.model(self.base_name).get("size_gb") or 0)
                if self.base_name and self.base_id and self.engine.loaded == self.base_id else 0.0)
        free = machine.metal_cap_gb(self.mem_gb) - wired + ours
        if need > free:
            self.log(f"memory: worker {model} needs {need} GB, metal cap leaves {free:.1f} GB; not starting it")
            return "内存不够，专用模型先不起"
        return None

    def _idle_workers(self, pressure: str) -> None:
        """空闲 WORKER_IDLE_SECS 的工作进程停掉；内存压力 critical 时全部停掉，WORKER_HOLD_SECS 内不起（issue #11）。"""
        now = time.time()
        if pressure == "critical":
            self.worker_hold_until = now + WORKER_HOLD_SECS
        stopped = []
        with self.scorers_lock:
            for model, w in self.scorers.items():
                if w.state not in ("loading", "ready"):
                    continue
                if pressure == "critical" or now - w.last_used > WORKER_IDLE_SECS:
                    w.idle_stopped = pressure != "critical"
                    w.stop()
                    stopped.append(model)
        if stopped:
            self.log(f"stopped worker(s) {','.join(stopped)}: " + ("memory pressure critical" if pressure == "critical" else f"idle {WORKER_IDLE_SECS}s"))
            self.apply_budget()

    def scorer_for(self, task: str):
        """专用模型的工作进程（按模型分，self.scorers 里打分进程和嵌入进程都在）：打分和联想共用一个 ScoreWorker
        （前缀快照也就共用）；嵌入是 EmbedWorker（无状态）。按任务 kind 决定建哪种。"""
        model = self.recipe.model_for_task(task, self.tier_name)
        # 建进程、起进程在锁里：两个请求同时第一次进来时只建一个（不然多出的那个不计预算，stop() 也停不到它；#5 审阅）。
        # 重算预算在锁外：它可能要等引擎调天花板，别让打分这类要快的请求在锁上排队
        with self.scorers_lock:
            w = self.scorers.get(model)
            if w is None:
                params = self.recipe.params(task, self.tier_name)
                cache_mb = self.recipe.model(model).get("cache_mb")   # MLX 缓冲上限，见 score_worker.py
                if self.recipe.task(task)["kind"] == "embed":
                    w = EmbedWorker(self.engine_python, self.engine_site, self.model_dir_of(model), self.log,
                                    cache_mb=int(cache_mb if cache_mb is not None else 512),
                                    media=media_kinds(params) != set())     # 任务参数说收图片 / 声音才连视觉、声音一起加载
                else:
                    w = ScoreWorker(self.engine_python, self.engine_site, self.model_dir_of(model),
                                    int(params.get("prefix_snapshots", 16)), self.log,
                                    cache_mb=int(cache_mb if cache_mb is not None else 1024))
                self.scorers[model] = w
            started = w.state in ("stopped", "failed") and w.failures < 3
            if started:
                held = self.worker_hold_reason(model)
                if held:
                    raise BadRequest("not_ready", held, WORKER_HOLD_SECS)
                w.start()
        if started:
            self.apply_budget()
        return w

    def predict(self, task: str, body: dict) -> dict:
        params = self.recipe.params(task, self.tier_name)
        ctx = body.get("context", "")
        if not isinstance(ctx, str):
            raise BadRequest("invalid_request", "要 context（字符串）")
        if len(ctx) > int(params.get("max_chars", 2000)):
            raise BadRequest("input_too_long", f"前文 {len(ctx)} 字符，超过上限 {params.get('max_chars')}")
        try:
            k = int(body.get("k", params.get("k", 8)) or 1)
            n = int(body.get("n", params.get("n", 6)) or 1)
        except (TypeError, ValueError):
            raise BadRequest("invalid_request", "k、n 要是整数")   # 别让请求值经 handler error 进日志（2026-10-16 自查）
        if k > int(params.get("k", 8)) or n > int(params.get("n", 6)):
            raise BadRequest("invalid_request", f"k、n 不能超过任务参数（k {params.get('k')}，n {params.get('n')}）")
        st = self.task_status(task)
        if st["status"] != "available":
            raise BadRequest("task_unavailable" if st["status"] != "unsupported" else "unsupported", st["reason"] or st["status"])
        w = self.scorer_for(task)
        job = self.dispatcher.user_waiting_arrived()
        if job:
            self.log(f"background interrupted by predict ({task})")
        self.dispatcher.note_score()
        if not self.tier.get("base_resident", True) and self.engine.loaded and not self.unload_on_next_score:
            self.unload_on_next_score = True
            threading.Thread(target=self._idle_unload, daemon=True).start()
        try:
            rep = w.predict(ctx, k, n, timeout=10.0)
        except ScorerError as e:
            code = str(e)
            raise BadRequest(code, "联想模型正在加载" if code == "not_ready" else "联想进程失败", 2 if code == "not_ready" else 30)
        self.score_ms.setdefault(task, deque(maxlen=200)).append(float(rep.get("ms") or 0))
        return {"continuations": rep["cont"], "model": st["combo"], "cache_hit": bool(rep.get("hit")), "ms": rep.get("ms")}

    CONTINUE_STOP_MAX, CONTINUE_STOP_CHARS = 64, 8

    def continue_(self, task: str, body: dict) -> dict:
        """通用续写（V1 §2 /v1/continue，2026-10-16 定）：主控只给 k 个开头各续最多 max_tokens 个 token 的逐 token 文字和 log 概率，
        停在调用方给的停止串；怎么截、挑哪几个、什么算有把握由调用方定。缺省和上限是任务参数 k / max_tokens（没写 max_tokens 就沿用 n）。"""
        params = self.recipe.params(task, self.tier_name)
        ctx = body.get("context", "")
        if not isinstance(ctx, str):
            raise BadRequest("invalid_request", "要 context（字符串）")
        if len(ctx) > int(params.get("max_chars", 2000)):
            raise BadRequest("input_too_long", f"前文 {len(ctx)} 字符，超过上限 {params.get('max_chars')}")
        k_max = int(params.get("k", 8))
        mt_max = int(params.get("max_tokens", params.get("n", 6)))
        try:
            k = int(body["k"]) if "k" in body else k_max
            max_tokens = int(body["max_tokens"]) if "max_tokens" in body else mt_max
        except (TypeError, ValueError):
            raise BadRequest("invalid_request", "k、max_tokens 要是整数")
        if k < 1 or max_tokens < 1 or k > k_max or max_tokens > mt_max:
            raise BadRequest("invalid_request", f"k 要在 1–{k_max}、max_tokens 要在 1–{mt_max} 之间（任务参数）")
        stop = body.get("stop", [])
        if not isinstance(stop, list) or not all(isinstance(x, str) for x in stop):
            raise BadRequest("invalid_request", "stop 要是字符串列表")
        if len(stop) > self.CONTINUE_STOP_MAX or any(len(x) > self.CONTINUE_STOP_CHARS or not x for x in stop):
            raise BadRequest("invalid_request", f"stop 最多 {self.CONTINUE_STOP_MAX} 个、每个 1–{self.CONTINUE_STOP_CHARS} 个字")
        st = self.task_status(task)
        if st["status"] != "available":
            raise BadRequest("task_unavailable" if st["status"] != "unsupported" else "unsupported", st["reason"] or st["status"])
        w = self.scorer_for(task)
        job = self.dispatcher.user_waiting_arrived()
        if job:
            self.log(f"background interrupted by continue ({task})")
        self.dispatcher.note_score()
        if not self.tier.get("base_resident", True) and self.engine.loaded and not self.unload_on_next_score:
            self.unload_on_next_score = True
            threading.Thread(target=self._idle_unload, daemon=True).start()
        try:
            rep = w.continue_(ctx, k, max_tokens, stop, timeout=10.0)
        except ScorerError as e:
            code = str(e)
            raise BadRequest(code, "联想模型正在加载" if code == "not_ready" else "联想进程失败", 2 if code == "not_ready" else 30)
        self.score_ms.setdefault(task, deque(maxlen=200)).append(float(rep.get("ms") or 0))
        return {"branches": rep["branches"], "model": st["combo"], "cache_hit": bool(rep.get("hit")), "ms": rep.get("ms")}

    def score(self, task: str, body: dict) -> dict:
        params = self.recipe.params(task, self.tier_name)
        ctx = body.get("context", "")
        cands = body.get("candidates")
        if not isinstance(ctx, str) or not isinstance(cands, list) or not all(isinstance(c, str) for c in cands):
            raise BadRequest("invalid_request", "要 context（字符串）和 candidates（字符串列表）")
        if len(cands) > int(params.get("batch", 16)):
            raise BadRequest("invalid_request", f"候选数 {len(cands)} 超过批大小 {params.get('batch')}")
        if len(ctx) > int(params.get("max_chars", 2000)):
            raise BadRequest("input_too_long", f"前文 {len(ctx)} 字符，超过上限 {params.get('max_chars')}")
        st = self.task_status(task)
        if st["status"] != "available":
            raise BadRequest("task_unavailable" if st["status"] != "unsupported" else "unsupported", st["reason"] or st["status"])
        w = self.scorer_for(task)
        job = self.dispatcher.user_waiting_arrived()
        if job:
            self.log(f"background interrupted by score ({task})")
        self.dispatcher.note_score()
        if not self.tier.get("base_resident", True) and self.engine.loaded and not self.unload_on_next_score:
            self.unload_on_next_score = True   # 16 GB：用户回来打字了，底座立刻卸掉（不等采样线程）
            threading.Thread(target=self._idle_unload, daemon=True).start()
        try:
            rep = w.score(ctx, cands, timeout=10.0)
        except ScorerError as e:
            code = str(e)
            raise BadRequest(code, "打分模型正在加载" if code == "not_ready" else "打分进程失败", 2 if code == "not_ready" else 30)
        self.score_ms.setdefault(task, deque(maxlen=200)).append(float(rep.get("ms") or 0))
        if len(cands) >= 8:
            self.score_full_ms.setdefault(task, deque(maxlen=200)).append(float(rep.get("ms") or 0))
        return {"logprobs": rep["lp"], "model": st["combo"], "cache_hit": bool(rep.get("hit")), "ms": rep.get("ms")}

    def embed(self, task: str, body: dict, priority: Optional[str] = None, allow_battery: bool = False,
              accessory: Optional[str] = None) -> dict:
        """算向量（V1 §2 /v1/embed，2026-10-16 提议、10-19 认可，顶层 ADR 0018：全生态推理统一归主控，首个消费方是 digup 搜索）。
        请求 {"texts": [...]}：每条是调用方加好检索提示前缀的整段文字；每条按 token 截到任务参数 max_tokens（和 digup 一样 2048），
        答复每条一个满维（768）、L2 归一化、float16 精度的向量。专用模型直连、不走引擎（ADR 0006），无前缀缓存。
        请求等级看 X-Luoke-Priority（gateway 同一个头）：不带按后台——digup 大批建索引时不该挡住邮件、记忆的后台；
        后台按后台的规矩接纳（交互中、刚打过字、用电池、过热、功耗档间歇都会 busy / paused），算完按用时歇；
        user-waiting（搜索时的查询）算交互、打断后台，不进打分的 K 分钟窗口。"""
        priority = priority or "background"
        if priority not in ("user-waiting", "background"):
            raise BadRequest("invalid_request", "X-Luoke-Priority 只能是 user-waiting 或 background")
        params = self.recipe.params(task, self.tier_name)
        texts, inputs = body.get("texts"), body.get("inputs")
        max_chars = int(params.get("max_chars", 32000))      # 只防过大的请求；真正的截断按 token，在工作进程里
        if texts is not None and inputs is not None:
            raise BadRequest("invalid_request", "texts 和 inputs 只能给一个")
        if inputs is not None:
            self._check_embed_inputs(params, inputs, max_chars)
        else:
            if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
                raise BadRequest("invalid_request", "要 texts（非空字符串列表）")
            if not all(texts):      # 空串没有意义（算出来只是 <bos><eos> 的向量），多半是调用方的错
                raise BadRequest("invalid_request", f"texts 里第 {texts.index('') + 1} 条是空串")
            if len(texts) > int(params.get("batch", 32)):
                raise BadRequest("invalid_request", f"文本数 {len(texts)} 超过批大小 {params.get('batch')}")
            over = next((len(t) for t in texts if len(t) > max_chars), None)
            if over is not None:
                raise BadRequest("input_too_long", f"有文本 {over} 字符，超过上限 {max_chars}")
        # 一次请求的文字总量（#5 审阅加）：单条上限 × 批大小太宽，整批都是长文时请求体和工作进程的内存都跟着涨
        max_total = int(params.get("max_total_chars", 320000))
        total = sum(len(t) for t in texts) if inputs is None else \
            sum(len(it["text"]) for it in inputs if isinstance(it, dict) and isinstance(it.get("text"), str))
        if total > max_total:
            raise BadRequest("input_too_long", f"这次请求的文字一共 {total} 字符，超过上限 {max_total}；请分成几次")
        st = self.task_status(task)
        if st["status"] != "available":
            raise BadRequest("task_unavailable" if st["status"] != "unsupported" else "unsupported", st["reason"] or st["status"])
        d = self.dispatcher
        if priority == "background":
            # 单独记账（#5 审阅 2026-10-19）：嵌入走自己的进程、不用引擎，不占引擎的后台名额、不受功耗档间歇管，
            # 所以 digup 大批建索引不会把邮件、记忆的后台挤到抢不到名额；只照全机的后台状态和交互让路
            ok, code, retry, reason = d.admit_side_background(allow_battery, lane=task)
            if not ok:
                self.log(f"refused embed task={task} prio=background accessory={accessory} code={code} reason={reason} retry_after={retry}")
                raise BadRequest(code, (f"后台请求现在不接：{reason}" if code != "paused" else f"全机后台已暂停：{reason}"), retry, reason=reason,
                                 **precise_wait(d, reason, lane=task))
        # user-waiting（搜索时的查询）走优先通道：不打断引擎上的后台、不让全机后台停 60 秒；只在嵌入进程里插到后台批的前面
        w = None
        t0 = time.time()
        try:
            # 建进程也在 try 里：Popen 的 OSError、apply_budget 出错时，finally 照样还后台名额，
            # 不然全机后台一直答 engine_busy 到主控重启（#5 审阅，2026-10-19）
            w = self.scorer_for(task)
            max_tokens = int(params.get("max_tokens", 2048))
            if inputs is not None:
                rep = w.embed_inputs(inputs, max_tokens, int(params.get("image_batch") or 8), int(params.get("audio_batch") or 2),
                                     float(params.get("max_audio_secs") or 30), timeout=30.0 + 5.0 * len(inputs))
            else:
                rep = self._embed_lane(w, texts, max_tokens, priority)
            if priority == "background":
                d.lane_rested(task, time.time() - t0)       # 这条通道按功耗档歇，别的通道和引擎不受影响
        except ScorerError as e:
            code = str(e)
            self._log_embed_refusal(task, code, priority, accessory, w.state if w else None)
            raise BadRequest(code, "嵌入模型正在加载" if code == "not_ready" else "嵌入进程失败", 2 if code == "not_ready" else 30)
        finally:
            pass
        self.score_ms.setdefault(task, deque(maxlen=200)).append(float(rep.get("ms") or 0))
        out = {"vectors": rep["vectors"], "dims": rep.get("dims"), "model": st["combo"],
               "vector_space": self.recipe.vector_space(task, self.tier_name), "ms": rep.get("ms")}
        if inputs is not None:
            out["errors"] = rep.get("errors") or [None] * len(inputs)      # 逐条出错：坏的那条 vector 是 null
        return out

    EMBED_CHUNK = 8      # 后台批切成几条一份：查询最多等一份算完（#5 审阅：之前要等一整批 32 × 2048 token）

    def _embed_lane(self, w, texts: list, max_tokens: int, priority: str) -> dict:
        """嵌入的优先通道：user-waiting 的查询插到后台批的前面。后台批切成 EMBED_CHUNK 条一份，每份之前看有没有查询在等，
        有就先让它。答复和整批一次算的一样（向量按原顺序拼回去，ms 加起来）。"""
        if priority == "user-waiting":
            with self.embed_lane:
                self.embed_priority_waiting += 1
            try:
                return w.embed(texts, max_tokens, timeout=30.0 + 2.0 * len(texts))
            finally:
                with self.embed_lane:
                    self.embed_priority_waiting -= 1
                    self.embed_lane.notify_all()
        vectors, ms, dims = [], 0.0, None
        for i in range(0, len(texts), self.EMBED_CHUNK):
            with self.embed_lane:
                self.embed_lane.wait_for(lambda: self.embed_priority_waiting == 0, 30)
            chunk = texts[i:i + self.EMBED_CHUNK]
            rep = w.embed(chunk, max_tokens, timeout=30.0 + 2.0 * len(chunk))
            vectors += rep["vectors"]
            ms += float(rep.get("ms") or 0)
            dims = rep.get("dims", dims)
        return {"vectors": vectors, "dims": dims, "ms": round(ms, 1)}

    def _check_embed_inputs(self, params: dict, inputs, max_chars: int) -> None:
        """多模态 inputs 的校验（草案 docs/research/embed-multimodal-draft.md）。错误信息只带长度和条数，不带内容。
        只收文字的档不收 inputs（用 texts）；每种媒体的条数、每条的字节数按任务参数；图片的 budget 只能取 image_budgets 里的。"""
        from .worker import mediaops
        kinds = media_kinds(params)
        if not kinds:
            raise BadRequest("invalid_request", "这一档只收文字：请用 texts")
        if not isinstance(inputs, list) or not inputs:
            raise BadRequest("invalid_request", "要 inputs（非空列表）")
        budgets = params.get("image_budgets") or list(IMAGE_BUDGETS)
        counts = {"text": 0, "image": 0, "audio": 0}
        for it in inputs:
            k = mediaops.kind_of(it)
            if k is None:
                raise BadRequest("invalid_request", "inputs 的每一条要恰好有 text、image、audio 之一")
            if not isinstance(it[k], str) or not it[k]:
                raise BadRequest("invalid_request", f"{k} 要是非空字符串")
            counts[k] += 1
            if k == "text":
                if len(it[k]) > max_chars:
                    raise BadRequest("input_too_long", f"有文本 {len(it[k])} 字符，超过上限 {max_chars}")
                continue
            name = "图片" if k == "image" else "声音"
            if k not in kinds:
                raise BadRequest("invalid_request", f"这一档不收{name}（任务参数里没有 {k}_batch）")
            if k == "image":
                try:
                    budget = int(it.get("budget"))
                except (TypeError, ValueError):
                    raise BadRequest("invalid_request", "图片要带整数的 budget")
                if budget not in budgets:
                    raise BadRequest("invalid_request", f"budget 只能是 {list(budgets)} 之一（任务参数 image_budgets）")
            limit = params.get(f"max_{k}_bytes")
            if not b64_fits(it[k], limit):
                raise BadRequest("input_too_long", f"有一条{name}超过 {limit} 字节（任务参数 max_{k}_bytes）")
        for k, cap in (("text", params.get("batch", 32)), ("image", params.get("image_batch")), ("audio", params.get("audio_batch"))):
            if counts[k] and counts[k] > int(cap or 0):
                raise BadRequest("invalid_request", f"{k} 有 {counts[k]} 条，超过本档上限 {cap}")

    EMBED_REFUSAL_LOG_SECS = 10.0

    def _log_embed_refusal(self, task: str, code: str, priority: str, accessory: Optional[str], state: str) -> None:
        """嵌入进程答 not_ready / engine_failed 时也记进 hub.log（只有元数据；2026-10-16 digup 实测时发现，busy、paused 都记、
        它却不记，排查不便）。重新加载那几秒里请求可能一条接一条：同一任务同一原因 EMBED_REFUSAL_LOG_SECS 内只记一条，
        其间被省掉的条数记在下一条里。"""
        key = (task, code)
        now = time.time()
        last, skipped = self._embed_refusals.get(key, (0.0, 0))
        if now - last < self.EMBED_REFUSAL_LOG_SECS:
            self._embed_refusals[key] = (last, skipped + 1)
            return
        self.log(f"refused embed task={task} prio={priority} accessory={accessory} code={code} worker={state}"
                 + (f" (+{skipped} more since the last line)" if skipped else ""))
        self._embed_refusals[key] = (now, 0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: Hub = None  # type: ignore[assignment]

    # Unix socket 上 client_address 是空串，父类的日志会出错；而且主控日志只记元数据
    def address_string(self) -> str:
        return "uds"

    def log_message(self, fmt, *args) -> None:  # noqa: D401
        pass

    # ---- 小工具 ---------------------------------------------------------------------------------------
    def _json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Luoke-Contract", str(CONTRACT))
        self.send_header("X-Luoke-Tasks-Version", self.hub.tasks_version())
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: str, message: str, retry_after=None, **extra) -> None:
        body = error_body(code, message, retry_after, **extra)
        self.send_response(STATUS.get(code, 500))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Luoke-Contract", str(CONTRACT))
        self.send_header("X-Luoke-Tasks-Version", self.hub.tasks_version())
        if retry_after:
            self.send_header("Retry-After", str(int(retry_after)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 32 * 1024 * 1024:
            raise BadRequest("invalid_request", "请求体超过 32 MB")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise BadRequest("invalid_request", "请求体不是 JSON")

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Luoke-Contract", str(CONTRACT))
        self.send_header("X-Luoke-Tasks-Version", self.hub.tasks_version())
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, line: str) -> bool:
        try:
            self.wfile.write((line + "\n").encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _event(self, name: str, obj: dict) -> bool:
        return self._sse(f"event: {name}") and self._sse("data: " + json.dumps(obj, ensure_ascii=False)) and self._sse("")

    # ---- 路由 -----------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def _route(self, method: str) -> None:
        hub = self.hub
        path = self.path.split("?", 1)[0]
        acc = self.headers.get("X-Luoke-Accessory")
        try:
            if method == "GET" and path == "/tasks":
                name, _ = hub.check_accessory(acc)
                return self._json(200, hub.tasks_payload(name))
            if method == "GET" and path == "/status":
                return self._json(200, self._status())
            if hub.phase != "running" and path != "/downloads":
                raise BadRequest("busy" if hub.phase == "starting" else "engine_failed",
                                 "螺壳正在启动" if hub.phase == "starting" else "螺壳正在退出", 5)
            if path == "/downloads" and method in ("GET", "POST"):
                self.require_hub(acc, "下载只有主控本体能发起")
                if method == "POST":
                    b = self._body()
                    return self._json(200, hub.start_downloads(b.get("names") or None))
                return self._json(200, {"needed": hub.needed_downloads(), "downloads": hub.downloads})
            if path == "/settings" and method in ("GET", "PUT"):
                # 主控本体自己用（菜单栏、设置页）；别的调用方不该碰
                self.require_hub(acc, "设置只有主控本体能改", allow_cli=True)
                if method == "PUT":
                    return self._json(200, hub.update_settings(self._body()))
                return self._json(200, dict(hub.settings))
            if path.startswith("/endpoint"):
                # 本机端点的管理（设置 → 高级）：只有主控本体能用；密钥只从这里给出去
                self.require_hub(acc, "只有主控本体能管本机端点", allow_cli=True)
                if method == "GET" and path == "/endpoint":
                    return self._json(200, dict(hub.endpoint_status(), key=endpoint_mod.load_key(hub.env_dir)))
                if method == "POST" and path == "/endpoint/key":
                    return self._json(200, {"key": hub.endpoint.rotate_key()})
                if method == "POST" and path == "/endpoint/models":
                    b = self._body()
                    if not isinstance(b.get("path"), str):
                        raise BadRequest("invalid_request", "要有 path")
                    return self._json(200, hub.register_user_model(b["path"], b.get("name")))
                if method == "POST" and path == "/endpoint/models/remove":
                    b = self._body()
                    if not hub.remove_user_model(str(b.get("name") or "")):
                        raise BadRequest("invalid_request", "没有这个登记的模型")
                    return self._json(200, {})
                raise BadRequest("invalid_request", f"没有 {method} {path}")
            if path.startswith("/tasks/") and path.endswith("/v1/chat/completions") and method == "POST":
                task = path[len("/tasks/"):-len("/v1/chat/completions")]
                self.accessory_name = hub.check_accessory(acc, task)[0]
                if hub.recipe.task(task)["kind"] != "generate":
                    raise BadRequest("invalid_request", f"任务 {task} 不是生成任务")
                return self._chat(task)
            if path.startswith("/tasks/") and path.endswith("/v1/score") and method == "POST":
                task = path[len("/tasks/"):-len("/v1/score")]
                hub.check_accessory(acc, task)
                if hub.recipe.task(task)["kind"] != "score":
                    raise BadRequest("invalid_request", f"任务 {task} 不是打分任务")
                return self._json(200, hub.score(task, self._body()))
            if path.startswith("/tasks/") and path.endswith("/v1/embed") and method == "POST":
                task = path[len("/tasks/"):-len("/v1/embed")]
                name, _ = hub.check_accessory(acc, task)
                if hub.recipe.task(task)["kind"] != "embed":
                    raise BadRequest("invalid_request", f"任务 {task} 不是嵌入任务")
                return self._json(200, hub.embed(task, self._body(), priority=(self.headers.get("X-Luoke-Priority") or "").strip() or None,
                                                 allow_battery=self.headers.get("X-Luoke-Allow-Battery") == "1", accessory=name))
            if path.startswith("/tasks/") and path.endswith("/v1/continue") and method == "POST":
                task = path[len("/tasks/"):-len("/v1/continue")]
                hub.check_accessory(acc, task)
                if hub.recipe.task(task)["kind"] != "predict":
                    raise BadRequest("invalid_request", f"任务 {task} 不是联想任务")
                return self._json(200, hub.continue_(task, self._body()))
            if path.startswith("/tasks/") and path.endswith("/v1/predict") and method == "POST":
                task = path[len("/tasks/"):-len("/v1/predict")]
                hub.check_accessory(acc, task)
                if hub.recipe.task(task)["kind"] != "predict":
                    raise BadRequest("invalid_request", f"任务 {task} 不是联想任务")
                return self._json(200, hub.predict(task, self._body()))
            if method == "POST" and path == "/warmup":
                hub.check_accessory(acc)
                b = self._body()
                task = b.get("task")
                if not task or not hub.recipe.task(task):
                    raise BadRequest("task_unavailable", "没有这个任务")
                st = hub.task_status(task)
                if st["status"] != "available":
                    return self._json(200, {"accepted": False, "code": "task_unavailable" if st["status"] != "unsupported" else "unsupported"})
                r = hub.dispatcher.warmup(task, b.get("lease_id"))
                if r["accepted"] and hub.recipe.task(task)["kind"] == "generate" and hub.base_id and not hub.engine.loaded:
                    threading.Thread(target=self._load_base_quietly, daemon=True).start()
                if r["accepted"] and hub.recipe.task(task)["kind"] in ("score", "predict", "embed"):
                    try:
                        hub.scorer_for(task)
                    except BadRequest:
                        pass          # 内存紧张先不起：预热照样答应，真用到时答 not_ready 让配件退避
                return self._json(200, r)
            if method == "PUT" and path == "/accessory/resident":
                name, _ = hub.check_accessory(acc)
                b = self._body()
                hub.set_resident(name, bool(b.get("resident")))
                return self._json(200, {})
            if method == "POST" and path == "/jobs/run":
                # 试用期（2026-10-17）：内部版 / 本机版上让某个空闲任务在下一个安静时刻就跑，不等两小时间隔；
                # 安静、电源、后台暂停这些条件照旧。beta 和公开发布没有这条路
                if not (hub.lab or hub.local_allow_enabled()):
                    raise BadRequest("unsupported", "只有内部版和本机版能提前跑空闲任务")
                b = self._body()
                job = b.get("job") if isinstance(b, dict) else None
                if not isinstance(job, str) or not hub.jobs.run_soon(job, hub.manifests):
                    raise BadRequest("invalid_request", f"没有空闲任务 {job}")
                return self._json(200, {"job": job, "queued": True})
            if method == "POST" and path == "/accessory/exiting":
                name, _ = hub.check_accessory(acc)
                hub.note_exiting(name)
                hub.release_accessory(name)
                return self._json(200, {})
            if hub.lab and path.startswith("/lab/") and method == "POST":
                # 只有实验室构建才有：给标记测试和实测制造故障，正式构建不编译这些路径（V1 §1）
                b = self._body()
                if path == "/lab/ceiling":
                    return self._json(200, {"ok": hub.engine.set_ceiling(float(b.get("gb", hub.tier["guard_gb"])))})
                if path == "/lab/engine/kill":
                    p = hub.engine.proc
                    if p and p.poll() is None:
                        p.kill()
                    return self._json(200, {"killed": bool(p)})
                raise BadRequest("invalid_request", f"没有这个实验室端点：{path}")
            if method == "GET" and path == "/accessories":
                hub.check_accessory(acc)
                hub.manifests = hub.scan_accessories()
                return self._json(200, {"contract": 1, "accessories": accessories.public_list(hub.manifests)})
            if method == "POST" and path == "/smoke":          # 首次运行 / 设置页的「重新检查」：只有主控自己的界面能调；在后台做，界面轮询 /status 的 smoke
                self.require_hub(acc, "只有主控能发起冒烟检查")
                if getattr(hub, "smoke_running", False):
                    return self._json(200, {"started": False, "running": True})
                threading.Thread(target=lambda: hub.smoke_check(force=True), daemon=True).start()
                return self._json(200, {"started": True})
            if method == "POST" and path == "/accessories/reregister":   # 输入法切不过去时的「重新登记」（luoke-ime#15）：只有主控自己的界面能调
                self.require_hub(acc, "只有主控能重新登记配件")
                hub.manifests = hub.scan_accessories()
                r = accessories.reregister(hub.manifests, str(self._body().get("accessory") or ""))
                if r.get("code"):
                    raise BadRequest(r["code"], r.get("error") or r["code"])
                hub.log(f"reregistered {r['accessory']} (unregistered {r['unregistered_others']} other copies)")
                return self._json(200, r)
            if method == "POST" and path == "/accessories/restart":   # 输入法变灰时的「重新启动」（luoke-ime#18）：只有主控自己的界面能调
                self.require_hub(acc, "只有主控能重新启动配件")
                hub.manifests = hub.scan_accessories()
                r = accessories.restart_input_method(hub.manifests, str(self._body().get("accessory") or ""))
                if r.get("code"):
                    raise BadRequest(r["code"], r.get("error") or r["code"])
                hub.log(f"restarted {r['accessory']} (stopped {r['killed']} process(es))")
                return self._json(200, r)
            if path in ("/accessories/previous", "/accessories/rollback"):   # 配件回滚（issue #12）：同卸载，只有主控自己的界面能调
                self.require_hub(acc, "只有主控能换回配件的上一版")
                hub.manifests = hub.scan_accessories()
                previous = os.path.join(hub.env_dir, "previous.noindex")
                if method == "GET" and path == "/accessories/previous":
                    out = []
                    for c in accessories.rollback_candidates(hub.manifests, previous):
                        ok = hub.recipe.version_verified(c["accessory"], c["previous"]) or hub.locally_allowed(c["accessory"], c["previous"])
                        past = (hub._history().get(c["accessory"]) or {}).get(c["previous"])
                        out.append({k: c[k] for k in ("accessory", "name", "type", "bundle_id", "current", "previous")} |
                                   {"verified": bool(ok or past), "release": (past or {}).get("release")})
                    return self._json(200, {"accessories": out})
                if method == "POST":
                    target = str(self._body().get("accessory") or "")
                    c = next((x for x in accessories.rollback_candidates(hub.manifests, previous) if x["accessory"] == target), None)
                    if not c:
                        raise BadRequest("unsupported", f"{target} 没有可以换回的上一版")
                    if not (hub.lab or hub.recipe.version_verified(target, c["previous"]) or hub.locally_allowed(target, c["previous"])
                            or (hub._history().get(target) or {}).get(c["previous"])):
                        raise BadRequest("unsupported", f"{c['name']} {c['previous']} 在这台 Mac 上没有验证记录，换回去螺壳也用不了它")
                    hub.jobs.stop_accessory(target)
                    hub.release_accessory(target)
                    r = accessories.rollback(hub.manifests, target, previous, verify=not hub.lab)
                    if r.get("code"):
                        raise BadRequest(r["code"], r.get("error") or r["code"])
                    hub.manifests = hub.scan_accessories()
                    hub.log(f"rolled back {target}: {r['from']} -> {r['to']}, stopped {r['killed']} process(es)")
                    return self._json(200, {"accessory": target, "from": r["from"], "to": r["to"], "type": r["type"],
                                            "bundle_id": r.get("bundle_id")})
            if method == "POST" and path == "/uninstall":      # 只有主控自己的界面能调（配件不能卸别的配件，V1 §6）
                self.require_hub(acc, "只有主控能卸载配件")
                b = self._body()
                hub.manifests = hub.scan_accessories()
                target = str(b.get("accessory") or "")
                dry_run = bool(b.get("dry_run", False))
                hub.check_uninstall_data(bool(b.get("data", False)), dry_run)   # 实验室实例 HOME 没隔离时不删数据（先于停进程）
                previous = os.path.join(hub.env_dir, "previous.noindex")
                plan = accessories.uninstall_plan(hub.manifests, target, preview_forget=False, previous_dir=previous)   # 先校验，再停它的任务
                if plan.get("code"):
                    raise BadRequest(plan["code"], plan.get("error") or plan["code"])
                if not dry_run:
                    hub.jobs.stop_accessory(target)   # 它的空闲任务（learn 等）正在跑就先停，别在删目录的时候写
                    hub.release_accessory(target)
                r = accessories.uninstall(hub.manifests, target, data=bool(b.get("data", False)),
                                          forget=bool(b.get("forget", False)), dry_run=dry_run, previous_dir=previous)
                if r.get("code"):
                    raise BadRequest(r["code"], r.get("error") or r["code"])
                if not dry_run:
                    hub.manifests = hub.scan_accessories()
                    hub.log(f"uninstalled {target}: removed {len(r['removed'])} path(s)" + (f", errors {r['errors']}" if r['errors'] else ""))
                return self._json(200, r)
            self._error("invalid_request", f"没有这个端点：{method} {path}")
        except BadRequest as e:
            self._error(e.code, e.message, e.retry_after, **dict(getattr(e, "extra", {}) or {}, **({"occupants": hub.occupants()} if e.code == "needs_user" else {})))
        except Exception as e:  # noqa: BLE001
            hub.log(f"handler error: {type(e).__name__}: {e}")
            try:
                self._error("engine_failed", f"螺壳内部错误：{type(e).__name__}")
            except Exception:
                pass

    def require_hub(self, acc: Optional[str], message: str, allow_cli: bool = False) -> None:
        """只给主控本体用的接口：自报身份是 luoke-core，而且对端进程确实是本体（allow_cli 时也认主控自带的命令行 luoke）。
        不是就答 forbidden（403）。之前 /downloads、/settings、/endpoint 只核自报的身份（luoke-top 10-20 系统文档核查）。"""
        name, _ = self.hub.check_accessory(acc)
        if name != "luoke-core" or not self.peer_is_hub_app(allow_cli):
            raise BadRequest("forbidden", message)

    def _peer_pid(self) -> Optional[int]:
        try:
            import struct as _st
            raw = self.request.getsockopt(0, 0x002, 4)    # SOL_LOCAL, LOCAL_PEERPID（macOS）
            return _st.unpack("i", raw)[0]
        except OSError:
            return None

    def peer_is_hub_app(self, allow_cli: bool = False) -> bool:
        """对端是不是主控本体（菜单栏 app）：本体是主控进程的父进程，用 LOCAL_PEERPID 核对，不只信请求头里自报的身份
        （2026-10-16 审查：配件可以自称 luoke-core）。allow_cli：主控自带的命令行（app/Resources/luoke：用主控同一个 Python 跑
        `-m luoke_core.cli`）也算，按内核报的可执行文件路径和参数核对。实验室 / 开发时主控不是本体拉起的，放行。"""
        if self.hub.lab or os.getppid() == 1:
            return True
        pid = self._peer_pid()
        if pid is None:
            return False
        if pid == os.getppid():
            return True
        if not allow_cli:
            return False
        exe = procs.exe_path(pid)
        args = procs.argv(pid)
        return (bool(exe) and os.path.realpath(exe) == os.path.realpath(sys.executable)
                and any(a == "-m" and b == "luoke_core.cli" for a, b in zip(args, args[1:])))

    def _status(self) -> dict:
        hub = self.hub
        d = hub.dispatcher
        state, reasons = d.background_state()
        return {"hub_version": __version__, "phase": hub.phase, "tier": hub.tier_name, "pending_tier": hub.pending_tier, "memory_tier": hub.memory_tier, "tasks_version": hub.tasks_version(),
                "base": hub.base_id, "loaded": hub.engine.loaded,
                "engine_alive": hub.engine.alive(), "engine_ready": hub.engine.serving(), "engine_port_ok": hub.engine.owns_port(),
                "interactive": d.interactive(), "quiet": d.quiet(), "background": {"state": state, "reasons": reasons},
                "job": d.job.task if d.job else None, "scorers": {k: v.state for k, v in hub.scorers.items()},
                "ceiling_gb": hub.engine.guard_gb, "smoke": hub.state.get("smoke"), "settings": dict(hub.settings),
                "power": {"on_ac": d.on_ac, "low_power": d.low_power, "thermal": d.thermal},
                "breaker": hub.breaker, "engine_crashes": len(hub.engine_crashes),
                "resident": hub.state.get("resident", {}), "exited": hub.state.get("exited", {}),
                "downloads": hub.downloads, "needed_downloads": hub.needed_downloads(), "jobs": hub.jobs.status(),
                "release": hub.release_tag(), "local_allowed": hub.local_allow_list(), "endpoint": hub.endpoint_status(),
                "uptime": round(time.time() - hub.started)}

    def _load_base_quietly(self) -> None:
        if not self.hub.acquire_model(self.hub.base_id, timeout=5):     # 本机端点正在用别的模型：预热不抢
            return
        try:
            self.hub.load_base()
            self.hub.last_base_use = time.time()
        except EngineError as e:
            self.hub.log(f"warmup load failed: {e}")
        finally:
            self.hub.release_model(self.hub.base_id)

    # ---- 生成 -----------------------------------------------------------------------------------------
    def _chat(self, task: str) -> None:
        hub = self.hub
        d = hub.dispatcher
        body = self._body()
        priority = (self.headers.get("X-Luoke-Priority") or "").strip()
        if priority not in ("user-waiting", "background"):
            raise BadRequest("invalid_request", "生成请求要带 X-Luoke-Priority: user-waiting 或 background")
        allow_battery = self.headers.get("X-Luoke-Allow-Battery") == "1"
        st = hub.task_status(task)
        if st["status"] != "available":
            raise BadRequest("task_unavailable" if st["status"] != "unsupported" else "unsupported", st["reason"] or st["status"])
        params = hub.recipe.params(task, hub.tier_name)
        base_params = hub.recipe.base_params(task)
        model_name = hub.base_name
        model_meta = hub.recipe.model(model_name)
        payload = build_engine_payload(body, params, base_params, hub.base_id, model_meta.get("special_tokens", []))
        prefill = hub.measured_speed("prefill_tok_s", hub.recipe.speed(model_name, hub.tier_name, "prefill_tok_s", 100))
        decode = hub.measured_speed("decode_tok_s", hub.recipe.speed(model_name, hub.tier_name, "decode_tok_s", 10))
        est = estimate_secs(payload, prefill, decode)
        fits = hub.engine.loaded == hub.base_id or hub.fits_memory(model_name)
        job = None
        if priority == "background":
            acc_name = getattr(self, "accessory_name", None)
            if hub.is_user_model(hub.engine.loaded):
                # 引擎在给本机端点的用户模型用（ADR 0014 修订）：后台等它闲下来、换回底座
                raise BadRequest("busy", "引擎在给本机端点用，后台稍后再来", 60, reason="endpoint")
            # 空闲任务的请求碰上功耗间歇：挂着等它过去（最多 IDLE_JOB_REST_HOLD_SECS），不拒绝——拒了它就退出（10-15 learn 遇到过），
            # 免了间歇静音档又管不住它（10-25 负责人：静音档风扇猛转，是邮件助手的 clean 连着发）
            if acc_name is not None and acc_name == d.idle_job_accessory:
                left = d.cooldown_left()
                if 0 < left <= IDLE_JOB_REST_HOLD_SECS:
                    deadline = time.time() + left
                    while time.time() < deadline and d.idle_job_accessory == acc_name and d.cooldown_left() > 0:
                        time.sleep(min(1.0, d.cooldown_left()))
            ok, code, retry, reason = d.admit_background(allow_battery, fits, accessory=acc_name)
            if not ok:
                hub.log(f"refused chat task={task} prio=background accessory={acc_name} code={code} reason={reason} retry_after={retry}")   # 门口的回绝也记（元数据）
                raise BadRequest(code, (f"后台请求现在不接：{reason}" if code != "paused" else f"全机后台已暂停：{reason}"), retry, reason=reason,
                                 **precise_wait(d, reason))
            cancel = threading.Event()
            job = d.start_background(task, est + (0 if hub.engine.loaded == hub.base_id else model_meta.get("load_secs", 15)), cancel.set)
        else:
            interrupted = d.user_waiting_arrived()
            if interrupted:
                hub.log(f"background {interrupted.task} interrupted for user-waiting {task}")
            if not fits:
                raise BadRequest("needs_user", "内存不够加载底座", 60)
            cancel = threading.Event()
        # 从这里开始流式：排队、冷加载都发进度事件
        self._sse_start()
        t0 = time.time()
        gpu_secs = 0.0
        outcome = "ok"
        holding = False
        try:
            while not hub.acquire_model(hub.base_id, timeout=PROGRESS_SECS):     # 本机端点正在用别的模型：等它做完再换回底座
                if not self._event("luoke.progress", {"stage": "queued", "message": "等本机端点的请求做完", "eta": None}):
                    cancel.set()
                    outcome = "client_gone"
                    return
            holding = True
            if hub.engine.loaded != hub.base_id:
                err = {}
                th = threading.Thread(target=lambda: self._load_or_record(err), daemon=True)
                th.start()
                while th.is_alive():
                    if not self._event("luoke.progress", {"stage": "loading", "message": "正在加载底座", "eta": model_meta.get("load_secs")}):
                        cancel.set()
                        outcome = "client_gone"
                        th.join()
                        return
                    th.join(PROGRESS_SECS)
                if err:
                    outcome = "load_failed"
                    code = "needs_user" if err.get("code") == "too_large" else "engine_failed"
                    self._event("luoke.error", {"code": code, "message": "底座加载失败：" + err.get("msg", ""), "retry_after": 60})
                    return
            if not self._event("luoke.progress", {"stage": "running", "message": "开始生成", "eta": round(est)}):
                outcome = "client_gone"
                return
            hub.last_base_use = time.time()
            t_run = time.time()
            sent = {"any": False}

            combo = st["combo"]

            def on_line(line: str) -> bool:
                if job is not None:
                    job.progress_secs = time.time() - t_run
                if line.startswith("data: "):
                    if line != "data: [DONE]":
                        # V1 §2：答复的 model 写实际用的组合，不是引擎里的模型 id
                        try:
                            obj = json.loads(line[6:])
                            if isinstance(obj, dict) and any(
                                    (ch.get("delta") or {}).get(k) for ch in obj.get("choices") or [] if isinstance(ch, dict)
                                    for k in ("content", "reasoning_content", "reasoning", "tool_calls")):
                                # 只有带内容的块才算"已经发出输出"：开头只带 role 的空块不算，否则它后面的内存中止就不重试了
                                # （luoke-top 10-23 核查）。重试时配件会再收到一个只带 role 的块，无害
                                sent["any"] = True
                            if isinstance(obj, dict) and "model" in obj:
                                obj["model"] = combo
                                line = "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                        except ValueError:
                            pass
                return self._sse(line)

            info = hub.engine.stream_chat(payload, on_line, cancel,
                                          first_token_timeout=max(60, est * 3), total_timeout=max(120, est * 4))
            if info["error"] in ("read_failed", "engine_closed") and not sent["any"] and not cancel.is_set() and hub.engine.alive():
                hub.log(f"engine failed before any output ({info['error']}); retrying once")
                if self._event("luoke.progress", {"stage": "running", "message": "引擎出错，重试一次", "eta": round(est)}):
                    cancel = threading.Event()
                    info = hub.engine.stream_chat(payload, on_line, cancel,
                                                  first_token_timeout=max(60, est * 3), total_timeout=max(120, est * 4))
            mem_refused = memory_refused(info)
            if mem_refused and (sent["any"] or cancel.is_set() or hub.engine.loaded != hub.base_id):
                # internal-1020 在 16 GB 测试机上这一步没触发、原因不明：记下是哪个条件没满足（只有元数据）
                hub.log(f"engine refused {task} for memory; not retrying: sent={sent['any']} cancelled={cancel.is_set()} "
                        f"loaded={hub.engine.loaded!r} base={hub.base_id!r}")
            if mem_refused and not sent["any"] and not cancel.is_set() and hub.engine.loaded == hub.base_id:
                # oMLX 的预填内存守卫把 MLX 留着的缓冲池（最多约上限的 1/3）也算作占用：一条大请求过后，后面稍长的提示就被拒，
                # 而且一直拒（10-19 16 GB 测试机 topics、thread 被拒了 40 分钟）。卸掉底座再装回能把缓冲池放掉（实测 9.5 → 0.55 GB），
                # 然后重试一次；还是放不下才照实答"内存不够"
                hub.log(f"engine refused {task} for memory before any output; reloading the base to free MLX's buffer pool, retrying once")
                if self._event("luoke.progress", {"stage": "loading", "message": "腾出内存，重新加载底座", "eta": model_meta.get("load_secs")}):
                    try:
                        hub.engine.unload(hub.base_id)
                        hub.load_base()
                        cancel = threading.Event()
                        info = hub.engine.stream_chat(payload, on_line, cancel,
                                                      first_token_timeout=max(60, est * 3), total_timeout=max(120, est * 4))
                    except EngineError as e:
                        hub.log(f"base reload after memory refusal failed: {e}")
            gpu_secs = time.time() - t_run
            if info["error"]:
                outcome = info["error"]
                if info["error"] == "cancelled" and job is not None and d.job is not job:
                    self._event("luoke.error", {"code": "interrupted", "message": "后台请求被打断", "retry_after": 30})
                elif info["status"] == 507 or memory_refused(info):
                    # oMLX 内存保护的拒绝按"内存不够"处理（V1 §5），不算引擎故障
                    code = "paused" if priority == "background" else "needs_user"
                    self._event("luoke.error", {"code": code, "message": "内存不够：" + (info.get("detail") or "")[:200], "retry_after": 60,
                                                **({"occupants": hub.occupants()} if code == "needs_user" else {})})
                elif info["error"] not in ("client_gone",):
                    self._event("luoke.error", {"code": "engine_failed", "message": f"引擎出错：{info['error']}", "retry_after": 30})
            hub.last_base_use = time.time()
            usage = info.get("usage") or {}
            hub.log(f"chat task={task} prio={priority} combo={st['combo']} prompt_tokens={usage.get('prompt_tokens')} "
                    f"completion_tokens={usage.get('completion_tokens')} secs={time.time() - t0:.1f} outcome={outcome}")
        finally:
            if holding:
                hub.release_model(hub.base_id)
            if job is not None:
                d.end_background(job, gpu_secs)

    def _load_or_record(self, err: dict) -> None:
        try:
            self.hub.load_base()
        except EngineError as e:
            err["code"] = str(e)
            err["msg"] = str(e)


def memory_refused(info: dict) -> bool:
    """引擎因为内存拒绝了请求：开始前答 400（预填内存守卫），或者开始流式以后在流里报内存中止（stream_chat 记 memory）。"""
    return bool(info.get("memory")) or (info.get("status") == 400 and "memory" in (info.get("detail") or "").lower())


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(hub: Hub, socket_path: str) -> UnixServer:
    if os.path.exists(socket_path):
        os.remove(socket_path)
    Handler.hub = hub
    srv = UnixServer(socket_path, Handler)
    os.chmod(socket_path, 0o600)
    return srv
