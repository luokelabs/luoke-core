"""空闲任务（顶层 ADR 0014，V1 §8）：配件在清单里登记"空闲时跑"的命令，主控只决定什么时候跑——安静时刻、后台状态允许
（电源、低电量、过热、内存）、没有别的后台任务和空闲任务——到时用配件自带的 Python 拉起，用户回来就让它收尾（SIGTERM，
给它十几秒），超时就结束；主控不知道任务做什么，只记元数据（开始、结束、退出码、原因）到 state.json 的 jobs。

清单字段（luoke-accessory.json 的 jobs，一个配件可以登记几个）：
  {"name": "learn", "command": ["-m", "luoke_mem", "--path", "{memory}", "learn"],   # 给自带 Python 的参数；占位符同 commands
   "min_interval_minutes": 120, "uses_model": true, "memory_mb": 100, "allow_battery": false, "timeout_minutes": 30}
命令的环境：PATH、HOME、LUOKE_HUB_SOCKET（经主控用模型就连它，请求用 background 优先级）、PYTHONDONTWRITEBYTECODE=1。
约定：收到 busy / paused / interrupted 或 SIGTERM 就停在做完的那一步、退出码 0；没事可做也是 0；起不来是非 0，原因写标准错误（不含内容）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Callable, Optional

from . import accessories

WRAP_UP_SECS = 15          # 用户回来后给任务收尾的时间，超过就 SIGKILL
STDERR_KEEP = 4096         # state.json 里留的标准错误尾巴（配件保证里面没有内容）
DEFAULTS = {"min_interval_minutes": 120, "uses_model": True, "memory_mb": 100, "allow_battery": False, "timeout_minutes": 30}
RETRY_MINUTES = 10         # 上次是被用户打断或被暂停的（没做完），下一个安静时刻就可以接着跑，不用等满 min_interval
INTERRUPTED = ("wrapped_up", "paused")


def parse_jobs(manifest: dict) -> list:
    """清单里的 jobs 规范化成 [{key, accessory, name, command, ...}]；坏的条目跳过。"""
    out = []
    for j in manifest.get("jobs") or []:
        if not isinstance(j, dict) or not j.get("name") or not isinstance(j.get("command"), list):
            continue
        exe = j.get("exec")
        if exe is not None and (not isinstance(exe, str) or not exe or "/" in exe or exe.startswith(".")):
            continue          # exec 只能是 Contents/MacOS/ 下的一个文件名
        spec = dict(DEFAULTS, **{k: j[k] for k in DEFAULTS if k in j})
        spec.update({"key": f"{manifest['accessory']}.{j['name']}", "accessory": manifest["accessory"], "name": j["name"],
                     "command": list(j["command"]), "exec": exe, "bundle_id": manifest.get("bundle_id"), "_app": manifest.get("_app")})
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
            why = due(spec, self.now(), None if spec["key"] in self.soon else rec.get("last_start"), self.quiet(),
                      self.background_state, self.busy(), rec.get("reason"))
            if why:
                self.last_skip[spec["key"]] = why
                continue
            self.soon.discard(spec["key"])
            self._launch(spec)
            return

    def run_soon(self, key: str, manifests: dict) -> bool:
        if not any(s["key"] == key for m in manifests.values() for s in parse_jobs(m)):
            return False
        self.soon.add(key)
        self.log(f"job {key}: run requested, next quiet moment ignores min_interval")
        return True

    def _launch(self, spec: dict) -> None:
        fill = {"memory": accessories.default_memory_path(), "tool": spec["accessory"]}
        args = [a.format(**fill) if isinstance(a, str) else a for a in spec["command"]]
        if spec.get("exec"):   # 不带 Python 的配件（Swift 写的，例如 luoke-search；2026-10-20 应 luoke-core#16 加，只加不改）
            argv = [os.path.join(spec["_app"], "Contents", "MacOS", spec["exec"])] + args
        else:
            argv = [os.path.join(spec["_app"], accessories.PYTHON_REL), "-B"] + args
        env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
        if self.socket_path():
            env["LUOKE_HUB_SOCKET"] = self.socket_path()
        if os.environ.get("LUOKE_HOME"):
            env["LUOKE_HOME"] = os.environ["LUOKE_HOME"]
        os.makedirs(self.logs_dir, exist_ok=True)
        stderr_path = os.path.join(self.logs_dir, f"{spec['key']}.stderr")
        try:
            err = open(stderr_path, "wb")
            proc = subprocess.Popen(argv, env=env, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
            err.close()
        except OSError as e:
            self.state["jobs"][spec["key"]] = {"last_start": self.now(), "last_end": self.now(), "exit": None, "reason": f"launch_failed: {type(e).__name__}"}
            self.save_state()
            self.log(f"job {spec['key']}: launch failed ({type(e).__name__})")
            return
        self.running = Running(spec, proc, self.now(), stderr_path)
        self.on_change(spec["accessory"])
        self.state["jobs"][spec["key"]] = {"last_start": self.now(), "last_end": None, "exit": None, "reason": "running", "pid": proc.pid}
        self.save_state()
        self.log(f"job {spec['key']}: started pid={proc.pid}")

    def _watch(self) -> None:
        r = self.running
        code = r.proc.poll()
        now = self.now()
        if code is None:
            if r.term_sent_at is None:
                if not self.quiet() or self.busy():
                    self._terminate(r, "wrapped_up")          # 用户回来了：让它收尾
                elif now - r.started > float(r.spec["timeout_minutes"]) * 60:
                    self._terminate(r, "timeout")
                elif self.background_state(bool(r.spec.get("allow_battery")))[0] != "running":
                    self._terminate(r, "paused")
            elif now - r.term_sent_at > WRAP_UP_SECS:
                try:
                    r.proc.kill()
                except OSError:
                    pass
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

    def stop_accessory(self, accessory: str) -> None:
        """要卸载某个配件：它的空闲任务正在跑就结束掉（正常收尾的机会照给）。"""
        if self.running and self.running.spec["accessory"] == accessory and self.running.proc.poll() is None:
            self._terminate(self.running, "uninstall")
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

    def stop(self) -> None:
        """主控退出：正在跑的任务也让它收尾。"""
        if self.running and self.running.proc.poll() is None:
            self._terminate(self.running, "hub_stopping")
            try:
                self.running.proc.wait(WRAP_UP_SECS)
            except subprocess.TimeoutExpired:
                self.running.proc.kill()
            self._watch()

    def status(self) -> dict:
        # /status 给任何已验证的配件看：不带 stderr_tail（那是某个工具自己的输出，别的工具不该读到；留在 state.json 给诊断导出）
        records = {k: {kk: vv for kk, vv in v.items() if kk != "stderr_tail"} for k, v in self.state.get("jobs", {}).items()}
        return {"running": self.running.spec["key"] if self.running else None, "records": records, "skipped": self.last_skip}
