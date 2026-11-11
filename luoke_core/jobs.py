"""空闲任务（顶层 ADR 0014，V1 §8）：配件在清单里登记"空闲时跑"的命令，主控只决定什么时候跑——安静时刻、后台状态允许
（电源、低电量、过热、内存）、没有别的后台任务和空闲任务——到时用配件自带的 Python 拉起，用户回来就让它收尾（SIGTERM，
给它十几秒），超时就结束；主控不知道任务做什么，只记元数据（开始、结束、退出码、原因）到 state.json 的 jobs。

清单字段（luoke-accessory.json 的 jobs，一个配件可以登记几个）：
  {"name": "learn", "command": ["-m", "luoke_mem", "--path", "{memory}", "learn"],   # 给自带 Python 的参数；占位符同 commands
   "min_interval_minutes": 120, "uses_model": true, "memory_mb": 100, "allow_battery": false, "timeout_minutes": 30}
命令的环境：PATH、HOME、LUOKE_HUB_SOCKET（经主控用模型就连它，请求用 background 优先级）、PYTHONDONTWRITEBYTECODE=1。
约定：收到 busy / paused / interrupted 或 SIGTERM 就停在做完的那一步、退出码 0；没事可做也是 0；起不来是非 0，原因写标准错误（不含内容）。

螺壳里带的程序（Contents/Helpers，例如螺壳扫描；清单 jobs 写 exec）：主控不直接起它，起螺壳自己的
`--run-tool <配件> <参数>`（ToolLauncher.swift）：本体停着起、核过真正在跑的进程的签名再放行，退出码照传，被信号结束是 128+信号，
本体自己的错是 120–123。找不到螺壳的可执行文件就不跑。--run-tool 和工具、工具起的东西都在一个新的进程组里，硬杀时一起杀。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Callable, Optional

from . import accessories, apps, spawn

WRAP_UP_SECS = 15          # 用户回来后给任务收尾的时间，超过就 SIGKILL
STDERR_KEEP = 4096         # state.json 里留的标准错误尾巴（配件保证里面没有内容）
DEFAULTS = {"min_interval_minutes": 120, "uses_model": True, "memory_mb": 100, "allow_battery": False, "timeout_minutes": 30}
RETRY_MINUTES = 10         # 上次是被用户打断、被暂停或螺壳退出了的（没做完），下一个安静时刻就可以接着跑，不用等满 min_interval
INTERRUPTED = ("wrapped_up", "paused", "hub_stopping")


def parse_jobs(manifest: dict) -> list:
    """清单里的 jobs 规范化成 [{key, accessory, name, command, ...}]；坏的条目跳过。"""
    out = []
    for j in manifest.get("jobs") or []:
        if not isinstance(j, dict) or not j.get("name") or not isinstance(j.get("command"), list):
            continue
        exe = j.get("exec")
        if exe is not None and (not isinstance(exe, str) or not exe or "/" in exe or exe.startswith(".")):
            continue          # exec 只能是 Contents/MacOS/ 下的一个文件名
        tool = bool(manifest.get("_helper")) and exe is not None      # 螺壳里带的程序（收进来的 Python 应用不写 exec，照旧用自带 Python）
        spec = dict(DEFAULTS, **{k: j[k] for k in DEFAULTS if k in j})
        spec.update({"key": f"{manifest['accessory']}.{j['name']}", "accessory": manifest["accessory"], "name": j["name"],
                     "command": list(j["command"]), "exec": exe, "bundle_id": manifest.get("bundle_id"), "_app": manifest.get("_app"),
                     "_env": manifest.get("_env"), "tool": tool, "first_use": j.get("first_use") is True})
        out.append(spec)
    return out


def due(spec: dict, now: float, last_start: Optional[float], quiet: bool, background_state: Callable[[bool], tuple],
        busy: bool, last_reason: Optional[str] = None) -> Optional[str]:
    """能不能现在跑：返回 None 表示能，否则是不能的原因（元数据，进日志）。上次没做完（被打断、被暂停）的，间隔缩成 RETRY_MINUTES。"""
    if busy:
        return "busy"
    if not quiet:
        return "not_quiet"
    state, reasons = background_state(bool(spec.get("allow_battery")))
    if state != "running":
        return ",".join(reasons) or "paused"
    interval = float(spec["min_interval_minutes"])
    if last_reason in INTERRUPTED:
        interval = min(interval, RETRY_MINUTES)
    if last_start is not None and now - last_start < interval * 60:
        return "interval"
    return None


def tool_launcher(app: Optional[str] = None) -> Optional[str]:
    """螺壳自己的可执行文件（主控和工具都在这个 .app 里，`--run-tool` 由它起工具）。读不到是 None"""
    app = app or accessories.hub_app()
    if not app:
        return None
    exe = (apps.read_plist(os.path.join(app, "Contents", "Info.plist")) or {}).get("CFBundleExecutable")
    if not isinstance(exe, str) or not exe or "/" in exe:
        return None
    path = os.path.join(app, "Contents", "MacOS", exe)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


class Running:
    def __init__(self, spec: dict, proc: subprocess.Popen, started: float, stderr_path: str):
        self.spec, self.proc, self.started, self.stderr_path = spec, proc, started, stderr_path
        self.term_sent_at: Optional[float] = None
        self.reason: Optional[str] = None


class IdleJobs:
    def __init__(self, now: Callable[[], float], quiet: Callable[[], bool], background_state: Callable[[bool], tuple],
                 busy: Callable[[], bool], state: dict, save_state: Callable[[], None], log: Callable[[str], None],
                 socket_path: Callable[[], Optional[str]], logs_dir: str, on_change: Optional[Callable[[Optional[str]], None]] = None):
        self.now, self.quiet, self.background_state, self.busy = now, quiet, background_state, busy
        self.on_change = on_change or (lambda accessory: None)   # 告诉派发器：哪个配件的空闲任务在跑（None 表示没有）
        self.state, self.save_state, self.log, self.socket_path, self.logs_dir = state, save_state, log, socket_path, logs_dir
        self.running: Optional[Running] = None
        self.last_skip: dict = {}
        self.right_now: set = set()     # 用户刚点了要它跑（开启应用后的第一次）：下一个采样就起，安静、电源这些都不看
        self.soon: set = set()          # 要求"下一个安静时刻就跑"的任务：不等 min_interval，其余条件照旧（内部版 / 本机版的 /jobs/run）

    # ---- 每个采样周期调一次 ----------------------------------------------------------------------------
    def tick(self, manifests: dict) -> None:
        if self.running:
            self._watch()
            return
        specs = [s for m in manifests.values() for s in parse_jobs(m)]
        records = self.state.setdefault("jobs", {})
        for spec in specs:
            rec = records.get(spec["key"]) or {}
            if rec.pop("asked", None) and rec.get("reason") == "hub_stopping":
                self.right_now.add(spec["key"])     # 用户点起来的这一次被退出打断（macOS 给完权限会退出重开）：接着跑
            why = None if spec["key"] in self.right_now else due(
                spec, self.now(), None if spec["key"] in self.soon else rec.get("last_start"), self.quiet(),
                self.background_state, self.busy(), rec.get("reason"))
            if why:
                self.last_skip[spec["key"]] = why
                continue
            self.soon.discard(spec["key"])
            if spec["key"] in self.right_now:
                self.right_now.discard(spec["key"])
                spec = dict(spec, asked=True)
            self._launch(spec)
            return

    def run_soon(self, key: str, manifests: dict) -> bool:
        if not any(s["key"] == key for m in manifests.values() for s in parse_jobs(m)):
            return False
        self.soon.add(key)
        self.log(f"job {key}: run requested, next quiet moment ignores min_interval")
        return True

    def first_use(self, manifest: dict) -> list:
        """用户开启应用或刚确认了权限：清单里写了 `first_use` 的任务马上跑，权限第一次用到就紧跟着用户的操作。"""
        keys = [s["key"] for s in parse_jobs(manifest) if s["first_use"]]
        for key in keys:
            self.right_now.add(key)
            self.log(f"job {key}: first use, starts at the next sample")
        return keys

    def _launch(self, spec: dict) -> None:
        fill = {"memory": accessories.default_memory_path(), "tool": spec["accessory"]}
        args = [a.format(**fill) if isinstance(a, str) else a for a in spec["command"]]
        launcher = tool_launcher(spec["_app"]) if spec.get("tool") else None
        if spec.get("tool") and not launcher:
            self.state["jobs"][spec["key"]] = {"last_start": self.now(), "last_end": self.now(), "exit": None, "reason": "launch_failed: no_launcher"}
            self.save_state()
            self.log(f"job {spec['key']}: no 螺壳 executable to start it with")
            return
        if spec.get("tool"):   # 螺壳里带的程序：经螺壳起，本体核签名
            argv = [launcher, "--run-tool", spec["accessory"]] + args
        elif spec.get("exec"):   # 不带 Python 的配件（Swift 写的，例如 luoke-search；2026-10-20 应 luoke-core#16 加，只加不改）
            argv = [os.path.join(spec["_app"], "Contents", "MacOS", spec["exec"])] + args
        else:
            argv = [os.path.join(spec["_app"], accessories.PYTHON_REL), "-B"] + args
        if not spec.get("tool"):    # 只有经螺壳起的工具（扫描）用螺壳的授权；别的 disclaim（spawn.py）
            argv = spawn.disclaimed(argv)
        env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        if self.socket_path():
            env["LUOKE_HUB_SOCKET"] = self.socket_path()
        if os.environ.get("LUOKE_HOME"):
            env["LUOKE_HOME"] = os.environ["LUOKE_HOME"]
        env.update(spec.get("_env") or {})
        os.makedirs(self.logs_dir, exist_ok=True)
        stderr_path = os.path.join(self.logs_dir, f"{spec['key']}.stderr")
        try:
            err = open(stderr_path, "wb")
            proc = subprocess.Popen(argv, env=env, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                    start_new_session=bool(spec.get("tool")))
            err.close()
        except OSError as e:
            self.state["jobs"][spec["key"]] = {"last_start": self.now(), "last_end": self.now(), "exit": None, "reason": f"launch_failed: {type(e).__name__}"}
            self.save_state()
            self.log(f"job {spec['key']}: launch failed ({type(e).__name__})")
            return
        self.running = Running(spec, proc, self.now(), stderr_path)
        self.on_change(spec["accessory"])
        self.state["jobs"][spec["key"]] = {"last_start": self.now(), "last_end": None, "exit": None, "reason": "running", "pid": proc.pid}
        if spec.get("asked"):
            self.state["jobs"][spec["key"]]["asked"] = True
        self.save_state()
        self.log(f"job {spec['key']}: started pid={proc.pid}" + (" via --run-tool" if launcher else ""))

    def _watch(self) -> None:
        r = self.running
        code = r.proc.poll()
        now = self.now()
        if code is None:
            if r.term_sent_at is None:
                asked = r.spec.get("asked")      # 用户亲自点起来的：用户就在跟前，不算回来了，只看超时
                if not asked and (not self.quiet() or self.busy()):
                    self._terminate(r, "wrapped_up")          # 用户回来了：让它收尾
                elif now - r.started > float(r.spec["timeout_minutes"]) * 60:
                    self._terminate(r, "timeout")
                elif not asked and self.background_state(bool(r.spec.get("allow_battery")))[0] != "running":
                    self._terminate(r, "paused")
            elif now - r.term_sent_at > WRAP_UP_SECS:
                self._kill(r)
                self.log(f"job {r.spec['key']}: killed after {WRAP_UP_SECS}s ({r.reason})")
            return
        rec = self.state["jobs"].get(r.spec["key"]) or {}
        rec.update({"last_end": now, "exit": code, "reason": r.reason or ("done" if code == 0 else "failed"), "secs": round(now - r.started, 1)})
        rec.pop("pid", None)
        try:
            with open(r.stderr_path, "rb") as f:
                tail = f.read()[-STDERR_KEEP:].decode("utf-8", "replace").strip()
            if tail:
                rec["stderr_tail"] = tail
        except OSError:
            pass
        self.state["jobs"][r.spec["key"]] = rec
        self.save_state()
        self.log(f"job {r.spec['key']}: {rec['reason']} exit={code} secs={rec['secs']}")
        self.running = None
        self.on_change(None)

    def stop_accessory(self, accessory: str, reason: str = "uninstall") -> None:
        """要卸载或停用某个配件：它的空闲任务正在跑就结束掉（正常收尾的机会照给）。"""
        if self.running and self.running.spec["accessory"] == accessory and self.running.proc.poll() is None:
            self._terminate(self.running, reason)
            try:
                self.running.proc.wait(timeout=5)   # 等它退出再删目录；不退就交给 tick 的硬杀
            except subprocess.TimeoutExpired:
                pass

    def _terminate(self, r: Running, reason: str) -> None:
        r.reason = reason
        r.term_sent_at = self.now()
        try:
            r.proc.send_signal(signal.SIGTERM)
        except OSError:
            pass
        self.log(f"job {r.spec['key']}: SIGTERM ({reason})")

    @staticmethod
    def _kill(r: Running) -> None:
        """硬杀。--run-tool 连同它起的工具、工具起的程序（clamscan 之类）一起：它们在一个新的进程组里"""
        try:
            if r.spec.get("tool"):
                os.killpg(r.proc.pid, signal.SIGKILL)
            else:
                r.proc.kill()
        except OSError:
            pass

    def stop(self) -> None:
        """主控退出：正在跑的任务也让它收尾。"""
        if self.running and self.running.proc.poll() is None:
            self._terminate(self.running, "hub_stopping")
            try:
                self.running.proc.wait(WRAP_UP_SECS)
            except subprocess.TimeoutExpired:
                self._kill(self.running)
                self.running.proc.wait()
            self._watch()

    def status(self) -> dict:
        # /status 给任何已验证的配件看：不带 stderr_tail（那是某个工具自己的输出，别的工具不该读到；留在 state.json 给诊断导出）
        records = {k: {kk: vv for kk, vv in v.items() if kk != "stderr_tail"} for k, v in self.state.get("jobs", {}).items()}
        return {"running": self.running.spec["key"] if self.running else None, "records": records, "skipped": self.last_skip}
