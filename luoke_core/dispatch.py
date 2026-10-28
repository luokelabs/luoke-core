"""派发（V1 §3、DESIGN §6）：一条请求什么时候跑、后台工作要不要为"用户在等"让路。

只做判定，不碰引擎；时间从外面注入（now），便于测试。所有数字都有名字，上线后按实测调。
"""
from __future__ import annotations

import secrets
import threading
from typing import Callable, Optional

INTERACTIVE_SECS = 60          # N：最近 N 秒有"用户在等"的请求或预热发起，就算交互中
QUIET_MINUTES = 5              # M：连续 M 分钟不交互，才算安静时刻
SCORE_WINDOW_SECS = 180        # K：最近 K 分钟有过打分请求，不接新的后台请求
INTERRUPT_COST_SECS = 11.0     # 实测 0：断开检测约 2 s + 一个 prefill 块，M1 上共约 11 s
LEASE_SECS = 600               # 预热租约
POWER_COOLDOWN = {"full": 0.0, "balanced": 1.0, "quiet": 3.0}   # 后台工作、本机端点的请求结束后，按占用时长的倍数歇一歇
ENDPOINT_MAX_REST_SECS = 600   # 端点一次最多歇这么久（单个特别长的请求之后，不让下一个请求等到客户端超时）
MEMORY_RECOVER_SECS = 180      # 内存压力恢复后再等几分钟才重新加载


class BackgroundJob:
    def __init__(self, task: str, started: float, est_total_secs: float, cancel: Callable[[], None]):
        self.task = task
        self.started = started
        self.est_total_secs = est_total_secs
        self.cancel = cancel
        self.progress_secs = 0.0  # 由执行方更新：已经做了多少秒的"有效工作"（prefill 已算的 token 折算）


class Dispatcher:
    def __init__(self, now: Callable[[], float], tier: dict, power_mode: str = "balanced",
                 background_on_battery: bool = False):
        self.now = now
        self.tier = tier
        self.power_mode = power_mode
        self.background_on_battery = background_on_battery
        self.lock = threading.Lock()
        self.started_at = now()
        self.last_user_waiting = -1e9
        self.last_score = -1e9
        self.last_warmup = -1e9
        self.job: Optional[BackgroundJob] = None
        self.cooldown_until = 0.0
        self.lane_cooldown: dict = {}     # 专用模型通道各自的功耗档间歇（luoke-core#9 Q1）
        self.endpoint_cooldown_until = 0.0   # 本机端点的功耗档间歇（10-22：功耗档是全局的，只有打字和配件里手动点的不管）
        self.endpoint_in_flight = 0          # 正在做的端点请求（均衡、静音下一次只做一条）
        self.idle_job_accessory: Optional[str] = None   # 正在跑的空闲任务属于哪个配件（V1 §8）：它的后台请求优先、免功耗间歇
        self.leases: dict = {}  # lease_id -> (task, expires)
        self.memory_ok_since: Optional[float] = now() - MEMORY_RECOVER_SECS   # None = 内存压力还没恢复；启动时视为早已正常
        self.memory_pressure = "normal"                  # normal / warn / critical
        # 外面每次刷新的环境
        self.on_ac = True
        self.low_power = False
        self.thermal = "nominal"

    # ---- 交互判定 ------------------------------------------------------------------------------------
    def note_user_waiting(self) -> None:
        self.last_user_waiting = self.now()

    def note_score(self) -> None:
        t = self.now()
        self.last_score = t
        self.last_user_waiting = t

    def interactive(self) -> bool:
        t = self.now()
        return (t - self.last_user_waiting < INTERACTIVE_SECS) or (t - self.last_warmup < INTERACTIVE_SECS)

    def quiet(self) -> bool:
        t = self.now()
        last = max(self.last_user_waiting, self.last_warmup, self.started_at)
        return t - last >= QUIET_MINUTES * 60

    def scored_recently(self) -> bool:
        return self.now() - self.last_score < SCORE_WINDOW_SECS

    # ---- 全机后台状态 ----------------------------------------------------------------------------------
    def background_state(self, allow_battery: bool = False) -> tuple:
        """(state, reasons)。state 是 running 或 paused；reasons 是暂停原因列表。"""
        reasons = []
        if not self.on_ac and not self.background_on_battery and not allow_battery:
            reasons.append("battery")
        if self.low_power:
            reasons.append("low_power")
        if self.thermal in ("serious", "critical") or (self.power_mode == "quiet" and self.thermal == "fair"):
            reasons.append("thermal")
        if self.memory_pressure != "normal" or self.memory_ok_since is None or \
                self.now() - self.memory_ok_since < MEMORY_RECOVER_SECS:
            reasons.append("memory")
        return ("paused" if reasons else "running"), reasons

    def set_memory_pressure(self, level: str) -> None:
        if level == "normal":
            if self.memory_pressure != "normal":
                self.memory_ok_since = self.now()
            elif self.memory_ok_since is None:
                self.memory_ok_since = self.now()
        else:
            self.memory_ok_since = None
        self.memory_pressure = level

    # ---- 后台接纳 ------------------------------------------------------------------------------------
    def admit_background(self, allow_battery: bool, fits_memory: bool, accessory: Optional[str] = None) -> tuple:
        """返回 (ok, code, retry_after, reason)。不排队：不满足就立即拒绝。
        空闲任务跑着的时候（idle_job_accessory），别的配件的后台请求等它（busy, idle_job）。它自己的请求也受功耗间歇管
        （10-25：静音档下邮件助手的 clean 一条接一条、风扇猛转；10-15 曾让它免间歇），只是主控挂着等间歇过去、不拒绝（hub）。"""
        with self.lock:
            t = self.now()
            state, reasons = self.background_state(allow_battery)
            if state == "paused":
                return False, "paused", 60, ",".join(reasons)
            if not fits_memory:
                return False, "paused", 120, "memory"
            if self.interactive():
                return False, "busy", max(5, int(INTERACTIVE_SECS - (t - self.last_user_waiting))), "interactive"
            if self.scored_recently():
                return False, "busy", max(5, int(SCORE_WINDOW_SECS - (t - self.last_score))), "scored_recently"
            if not self.tier.get("base_resident", True) and not self.quiet():
                return False, "busy", 60, "not_quiet"   # 16 GB 档：后台只在安静时刻做
            mine = self.idle_job_accessory is not None and accessory == self.idle_job_accessory
            if self.idle_job_accessory is not None and not mine:
                return False, "busy", 30, "idle_job"
            if self.job is not None:
                return False, "busy", max(5, int(self.job.est_total_secs - self.job.progress_secs)), "engine_busy"
            if t < self.cooldown_until:
                return False, "busy", max(1, int(self.cooldown_until - t)), "power_cooldown"
            return True, None, None, None

    def cooldown_left(self) -> float:
        """后台的功耗间歇还剩几秒"""
        with self.lock:
            return max(0.0, self.cooldown_until - self.now())

    def lane_rested(self, lane: str, busy_secs: float) -> None:
        """专用模型通道的后台算完一块：这条通道按功耗档歇（各条通道各自歇，不碰引擎的间歇；luoke-core#9 的 Q1）。"""
        with self.lock:
            self.lane_cooldown[lane] = self.now() + POWER_COOLDOWN.get(self.power_mode, 1.0) * max(busy_secs, 0.0)

    def endpoint_try_begin(self) -> float:
        """端点的一条请求要开始：可以开始就记上"正在做"、返回 0；不行返回还要等几秒。均衡、静音下一次只放一条
        （luoke-top 10-23 核查：之前几条同时到的会在间歇结束时一起放行，能耗模式只管住一条接一条的）；全速不限。"""
        with self.lock:
            if POWER_COOLDOWN.get(self.power_mode, 1.0) == 0:
                self.endpoint_in_flight += 1
                return 0.0
            wait = self.endpoint_cooldown_until - self.now()
            if wait > 0:
                return wait
            if self.endpoint_in_flight > 0:
                return 1.0          # 前一条还在做：做完会定下一段间歇，一秒后再看
            self.endpoint_in_flight += 1
            return 0.0

    def endpoint_end(self, busy_secs: float) -> float:
        """端点的一条请求结束（成没成都要调）：放掉"正在做"，按用时定下一条的间歇（没成的传 0）。返回间歇秒数。"""
        with self.lock:
            self.endpoint_in_flight = max(0, self.endpoint_in_flight - 1)
        return self.endpoint_rested(busy_secs)

    def endpoint_rested(self, busy_secs: float) -> float:
        """本机端点的一条请求做完（pi 这类工具，10-22 定）：下一条要等它用时的功耗档倍数（全速 0、均衡 1、静音 3），最多
        ENDPOINT_MAX_REST_SECS。oMLX 不能限速，只能在请求之间歇。返回这次定的间歇秒数。"""
        with self.lock:
            rest = min(ENDPOINT_MAX_REST_SECS, POWER_COOLDOWN.get(self.power_mode, 1.0) * max(busy_secs, 0.0))
            self.endpoint_cooldown_until = max(self.endpoint_cooldown_until, self.now() + rest)
            return rest

    def endpoint_wait(self) -> float:
        """端点的下一条请求还要歇多少秒；切到全速立即清零。"""
        with self.lock:
            if POWER_COOLDOWN.get(self.power_mode, 1.0) == 0:
                return 0.0
            return max(0.0, self.endpoint_cooldown_until - self.now())

    def lane_wait(self, lane: str) -> float:
        with self.lock:
            return max(0.0, self.lane_cooldown.get(lane, 0.0) - self.now())

    def admit_side_background(self, allow_battery: bool, lane: Optional[str] = None) -> tuple:
        """不走引擎的后台活（嵌入这类专用模型进程，#5 审阅 2026-10-19）：只看全机的后台状态、交互中、刚打过字、16 GB 档的安静时刻；
        不占引擎的后台名额（job）、不受功耗档间歇管（它们有自己的记账），所以不挤邮件、记忆的后台。"""
        with self.lock:
            t = self.now()
            state, reasons = self.background_state(allow_battery)
            if state == "paused":
                return False, "paused", 60, ",".join(reasons)
            if self.interactive():
                return False, "busy", max(5, int(INTERACTIVE_SECS - (t - self.last_user_waiting))), "interactive"
            if self.scored_recently():
                return False, "busy", max(5, int(SCORE_WINDOW_SECS - (t - self.last_score))), "scored_recently"
            if not self.tier.get("base_resident", True) and not self.quiet():
                return False, "busy", 60, "not_quiet"
            if lane and t < self.lane_cooldown.get(lane, 0.0):
                return False, "busy", max(1, int(self.lane_cooldown[lane] - t)), "lane_busy"
            return True, None, None, None

    def retry_after_ms(self, reason: Optional[str]) -> Optional[int]:
        """busy 的精确等待（毫秒，2026-10-17 应 digup 加，只加不改）。retry_after 是取整的秒（HTTP 的 Retry-After 只能是整数秒），
        短的间歇照它等会白等一截：均衡档一批 32 段嵌入做约 0.36 s、歇 0.36 s，却答 1 s，后台吞吐少一半。
        只对算得出来的原因给：功耗档间歇、交互中（含预热）、刚打过字、引擎被别的后台工作占着（按估算）；固定估计的
        （idle_job、not_quiet、paused）给 None。"""
        with self.lock:
            t = self.now()
            if reason == "power_cooldown":
                wait = self.cooldown_until - t
            elif reason == "interactive":
                wait = INTERACTIVE_SECS - (t - max(self.last_user_waiting, self.last_warmup))
            elif reason == "scored_recently":
                wait = SCORE_WINDOW_SECS - (t - self.last_score)
            elif reason == "engine_busy" and self.job is not None:
                wait = self.job.est_total_secs - self.job.progress_secs
            else:
                return None
        return max(0, int(round(wait * 1000)))

    def start_background(self, task: str, est_total_secs: float, cancel: Callable[[], None]) -> BackgroundJob:
        with self.lock:
            self.job = BackgroundJob(task, self.now(), est_total_secs, cancel)
            return self.job

    def end_background(self, job: BackgroundJob, gpu_secs: float) -> None:
        with self.lock:
            if self.job is job:
                self.job = None
            self.cooldown_until = self.now() + POWER_COOLDOWN.get(self.power_mode, 1.0) * max(gpu_secs, 0.0)

    # ---- 打断 ---------------------------------------------------------------------------------------
    def user_waiting_arrived(self) -> Optional[BackgroundJob]:
        """一条"用户在等"的请求到了：后台工作剩得比打断代价还短就让它跑完，否则打断。返回被打断的工作。"""
        with self.lock:
            self.note_user_waiting()
            job = self.job
            if job is None:
                return None
            remaining = job.est_total_secs - job.progress_secs
            if remaining <= INTERRUPT_COST_SECS:
                return None
            self.job = None
        job.cancel()
        return job

    def pause_reason_changed(self, reasons: list) -> Optional[BackgroundJob]:
        """全机后台转为已暂停：过热或等内存时打断正在跑的；电池、低电量则让它跑完。"""
        if any(r in ("thermal", "memory") for r in reasons):
            with self.lock:
                job, self.job = self.job, None
            if job:
                job.cancel()
            return job
        return None

    # ---- 预热租约 ------------------------------------------------------------------------------------
    def warmup(self, task: str, lease_id: Optional[str]) -> dict:
        with self.lock:
            t = self.now()
            self.leases = {k: v for k, v in self.leases.items() if v[1] > t}
            if lease_id and lease_id in self.leases:
                self.leases[lease_id] = (task, t + LEASE_SECS)
                return {"accepted": True, "lease_id": lease_id, "expires_in": LEASE_SECS}
            if self.low_power or self.memory_pressure != "normal":
                return {"accepted": False, "code": "deferred"}
            new = secrets.token_hex(8)
            self.leases[new] = (task, t + LEASE_SECS)
            self.last_warmup = t   # 只有被接受的发起算交互事件
            return {"accepted": True, "lease_id": new, "expires_in": LEASE_SECS}

    def leased(self, task: Optional[str] = None) -> bool:
        t = self.now()
        return any(v[1] > t and (task is None or v[0] == task) for v in self.leases.values())
