"""引擎监管（V1 §5）：oMLX 子进程按启动清单起、按 PID 核对、加载/卸载底座、流式转发一条生成请求。

oMLX 一行不改：只传命令行参数和环境变量。主控和它之间只有 127.0.0.1 上的随机端口加随机 API key。
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from typing import Callable, Optional

from . import procs


class EngineError(RuntimeError):
    pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Engine:
    def __init__(self, app: str, base_path: str, model_dir: str, guard_gb: float, log_level: str = "warning",
                 log: Optional[Callable[[str], None]] = None):
        self.cli = os.path.join(app, "Contents/MacOS/omlx-cli")
        self.base_path = base_path
        self.model_dir = model_dir
        self.guard_gb = guard_gb
        self.log_level = log_level
        self.log = log or (lambda s: None)
        self.port = _free_port()
        self.key = secrets.token_hex(16)
        self.proc: Optional[subprocess.Popen] = None
        self.ready = False          # 进程在跑且 /v1/models 已经能应答（alive 只说进程还在）
        self.loaded: Optional[str] = None
        self.extras: set = set()      # 和底座一起放得下、跟着加载的模型（本机端点登记的小模型）；不占"当前模型"那一格
        self.loading = False
        self.lock = threading.Lock()
        self.load_secs: Optional[float] = None
        self.port_check: tuple = (None, None)     # ((pid, port), 核对结果)

    # ---- 进程 ---------------------------------------------------------------------------------------
    def start(self, wait_models: Optional[list] = None, timeout: float = 180) -> list:
        os.makedirs(self.base_path, exist_ok=True)
        self.ready = False
        if self.proc is not None:           # 重启：换个端口和 key，别和没死透的旧进程撞上
            self.port = _free_port()
            self.key = secrets.token_hex(16)
            self.loaded = None
            self.extras.clear()
        for name in ("settings.json", "model_settings.json"):
            # oMLX 会把命令行写进 settings.json，每次整份重置；model_settings.json 存每个模型的设置（含 trust_remote_code），
            # 主控从不需要它，一起清掉，免得被人改成 true 后带着跑（审查 2026-10-17）
            sp = os.path.join(self.base_path, name)
            if os.path.exists(sp):
                os.remove(sp)
        # 密钥不进命令行，也不进环境变量（同账户的程序用 ps 就能读到这两处，再拿它借 oMLX 的 /v1/web/fetch 连外网，luoke-core#35）：
        # 写进只有本账户能读的 settings.json，oMLX 从这里读 auth.api_key，之后自己存回去也不改权限（_save_data 用 0600）
        fd = os.open(os.path.join(self.base_path, "settings.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"auth": {"api_key": self.key}}, f)
        self._reap_orphan()
        cmd = [self.cli, "serve", "--base-path", self.base_path, "--model-dir", self.model_dir,
               "--host", "127.0.0.1", "--port", str(self.port),
               "--no-cache", "--no-hf-cache", "--memory-guard-gb", str(self.guard_gb),
               "--log-level", self.log_level]
        env = {k: v for k, v in os.environ.items() if not (k.startswith("HF_") or k.startswith("OMLX_"))}
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        env["HF_HUB_OFFLINE"] = "1"     # 模型都在本机，下载归主控：找不到就报错，不去 Hugging Face 找
        logf = open(os.path.join(self.base_path, "engine.log"), "a")
        self.proc = subprocess.Popen(cmd, env=env, stdout=logf, stderr=subprocess.STDOUT, cwd="/")
        self._write_pidfile()
        t0 = time.time()
        ids: list = []
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise EngineError(f"oMLX 没起来（退出码 {self.proc.returncode}），看 {logf.name}")
            try:
                ids = self.models()
                # 模型发现比服务就绪晚：大模型要再等它出现
                if not wait_models or all(any(w == i or w in i for i in ids) for w in wait_models):
                    ok = self.check_port()
                    self.port_check = ((self.proc.pid, self.port), ok)
                    if ok is False:
                        self.log(f"engine port {self.port} is not held by pid {self.proc.pid}")
                    self.ready = True
                    self.log(f"engine ready port={self.port} models={ids} in {time.time() - t0:.1f}s")
                    return ids
            except Exception:
                pass
            time.sleep(0.5)
        raise EngineError("oMLX 启动超时")

    def stop(self) -> None:
        self.extras.clear()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
        self.loaded = None
        try:
            os.remove(self.pidfile)
        except OSError:
            pass

    # ---- 孤儿引擎（DESIGN §10）：按 PID + 启动时间记下自己拉起的，下次启动只结束正是那一个 ---------------------------
    @property
    def pidfile(self) -> str:
        return os.path.join(self.base_path, "engine.pid.json")

    def _write_pidfile(self) -> None:
        rec = {"pid": self.proc.pid, "start": procs.start_time(self.proc.pid), "exe": procs.exe_path(self.proc.pid)}
        tmp = self.pidfile + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, self.pidfile)

    def _reap_orphan(self) -> None:
        """上次没收好的引擎（主控崩溃、被强退）：PID、启动时间、可执行文件都对得上才结束它；任何一样对不上就是别的进程，不碰。"""
        try:
            with open(self.pidfile) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            return
        pid, start = rec.get("pid"), rec.get("start")
        mine = self.proc is not None and self.proc.pid == pid
        if isinstance(pid, int) and start is not None and not mine and procs.start_time(pid) == start \
                and procs.exe_path(pid) == rec.get("exe"):
            self.log(f"stopping orphaned engine pid={pid}")
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(100):
                    if procs.start_time(pid) != start:
                        break
                    time.sleep(0.1)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.remove(self.pidfile)
        except OSError:
            pass

    def alive(self) -> bool:
        up = self.proc is not None and self.proc.poll() is None
        if not up:
            self.ready = False
        return up

    def serving(self) -> bool:
        return self.alive() and self.ready

    def check_port(self) -> Optional[bool]:
        """监听这个端口的确实是自己拉起的子进程（lsof 只看 PID）。只在 start() 就绪后核一次（luoke-core#4：之前 /status 每次都跑，
        文件描述符软上限很大时 lsof 启动要几分钟、又没超时，每 10 秒堆一个）。lsof 的耗时和软上限成正比，所以在子进程里先压到 1024，
        再加超时；没核成（超时、出错）是 None，不当成"不是自己的"。"""
        if not self.alive():
            return False
        try:
            out = subprocess.run(["/bin/sh", "-c", 'ulimit -n 1024 2>/dev/null; exec /usr/sbin/lsof "$@"', "lsof",
                                  "-a", "-p", str(self.proc.pid), "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (subprocess.TimeoutExpired, OSError):
            return None
        return f":{self.port} " in out or f":{self.port}\n" in out

    def owns_port(self) -> Optional[bool]:
        """start() 时核过的结果（按 PID + 端口记）；进程换了就是还没核。"""
        if not self.alive():
            return False
        key, ok = self.port_check
        return ok if key == (self.proc.pid, self.port) else None

    # ---- HTTP ---------------------------------------------------------------------------------------
    def conn(self, timeout: float = 600) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def headers(self) -> dict:
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}

    def models(self) -> list:
        c = self.conn(10)
        c.request("GET", "/v1/models", headers=self.headers())
        r = c.getresponse()
        body = r.read()
        if r.status != 200:
            raise EngineError(f"/v1/models {r.status}")
        return [m["id"] for m in json.loads(body)["data"]]

    def resolve(self, want: str) -> Optional[str]:
        for m in self.models():
            if m == want or m.endswith("/" + want) or want in m:
                return m
        return None

    def _model_op(self, model: str, op: str, timeout: float = 600) -> int:
        c = self.conn(timeout)
        c.request("POST", f"/v1/models/{urllib.parse.quote(model, safe='')}/{op}", headers=self.headers())
        r = c.getresponse()
        r.read()
        return r.status

    def load(self, model: str) -> float:
        """阻塞到加载完；返回秒数。507 = 内存放不下。
        一次只留一个生成模型（底座或用户登记的本机模型，ADR 0014 修订"给高级用户的本机端点"）：别的先卸掉，内存只由主控一处管。"""
        with self.lock:
            if self.loaded == model:
                return 0.0
            for x in list(self.extras):          # 独占加载：一起放着的也先卸
                try:
                    self._model_op(x, "unload", timeout=60)
                except Exception as e:  # noqa: BLE001
                    self.log(f"unload {x} failed: {e}")
                self.extras.discard(x)
                self.log(f"unloaded {x} (making room for {model})")
            if self.loaded:
                prev = self.loaded
                try:
                    self._model_op(prev, "unload", timeout=60)
                except Exception as e:  # noqa: BLE001
                    self.log(f"unload {prev} failed: {e}")
                self.loaded = None
                self.log(f"unloaded {prev} (making room for {model})")
            self.loading = True
            t0 = time.time()
            try:
                st = self._model_op(model, "load")
            finally:
                self.loading = False
            if st == 507:
                raise EngineError("too_large")
            if st != 200:
                raise EngineError(f"load {st}")
            self.loaded = model
            self.load_secs = round(time.time() - t0, 1)
            self.log(f"loaded {model} in {self.load_secs}s")
            return self.load_secs

    def load_extra(self, model: str) -> float:
        """和现在的模型一起加载（调用方先算好放得下）：不卸任何东西。返回秒数；507 = 放不下。"""
        with self.lock:
            if model == self.loaded or model in self.extras:
                return 0.0
            self.loading = True
            t0 = time.time()
            try:
                st = self._model_op(model, "load")
            finally:
                self.loading = False
            if st == 507:
                raise EngineError("too_large")
            if st != 200:
                raise EngineError(f"load {st}")
            self.extras.add(model)
            secs = round(time.time() - t0, 1)
            self.log(f"loaded {model} alongside {self.loaded} in {secs}s")
            return secs

    def unload(self, model: Optional[str] = None) -> bool:
        """卸掉模型；真的卸了返回 True（并发时第二个调用等到锁已经没东西可卸，返回 False，调用方据此别重复记日志）。"""
        with self.lock:
            m = model or self.loaded
            if not m:
                return False
            try:
                self._model_op(m, "unload", timeout=60)
            except Exception as e:
                self.log(f"unload {m} failed: {e}")
            if self.loaded == m:
                self.loaded = None
            self.extras.discard(m)
            self.log(f"unloaded {m}")
            return True

    # ---- 管理接口（只用来改自己这个实例的天花板）----------------------------------------------------------
    def _admin_cookie(self) -> Optional[str]:
        c = self.conn(10)
        c.request("POST", "/admin/api/login", body=json.dumps({"api_key": self.key}).encode("utf-8"), headers=self.headers())
        r = c.getresponse()
        r.read()
        return r.getheader("Set-Cookie", "").split(";")[0] or None

    def set_ceiling(self, gb: float) -> bool:
        """运行中改内存天花板（实测 2026-10-12：立即生效；降到底座放不下会把底座卸掉，调用方先算好）。"""
        try:
            cookie = self._admin_cookie()
            if not cookie:
                return False
            c = self.conn(30)
            c.request("POST", "/admin/api/global-settings",
                      body=json.dumps({"memory_guard_tier": "custom", "memory_guard_custom_ceiling_gb": float(gb)}).encode("utf-8"),
                      headers={"Cookie": cookie, "Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            ok = r.status == 200
            if ok:
                self.guard_gb = gb
            self.log(f"ceiling -> {gb} GB ({'ok' if ok else r.status})")
            return ok
        except Exception as e:  # noqa: BLE001
            self.log(f"set_ceiling failed: {e}")
            return False

    # ---- 流式生成 ------------------------------------------------------------------------------------
    def stream_chat(self, payload: dict, on_line: Callable[[str], bool], cancel: threading.Event,
                    first_token_timeout: float, total_timeout: float) -> dict:
        """发一条流式请求，把每个 SSE 行交给 on_line（返回 False 表示停）。cancel 置位就关掉连接（oMLX 靠断开取消）。
        返回 {"status", "error", "first_token_secs", "usage"}。"""
        info = {"status": None, "error": None, "detail": None, "first_token_secs": None, "usage": None}
        finished = threading.Event()
        c = self.conn(timeout=max(first_token_timeout, 5))
        t0 = time.time()
        timer: Optional[threading.Timer] = None

        held = {"sock": None}

        def cut():
            # 从别的线程关连接：先 shutdown 才能把卡在 recv 里的读唤醒，单 close 不行
            s = held["sock"] or c.sock
            try:
                s and s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s and s.close()
            except Exception:
                pass

        try:
            c.request("POST", "/v1/chat/completions", body=json.dumps(payload).encode("utf-8"), headers=self.headers())
            held["sock"] = c.sock         # 先拿住：答复要求关连接时（HTTP/1.0、Connection: close）getresponse 会把 c.sock 置空
            r = c.getresponse()
            info["status"] = r.status
            if r.status != 200:
                # 错误体可能引用请求文字：不进 error（会进日志），单独放 detail，只给调用方看
                info["error"] = f"status_{r.status}"
                info["detail"] = r.read().decode("utf-8", "replace")[:500]
                return info
            # 等首个 token 的超时由 socket timeout 管；之后放宽到总超时
            held["sock"].settimeout(total_timeout)
            def watch():
                while not finished.is_set():          # 调用方取消就切断连接；请求结束就退出，不动调用方的 cancel
                    if cancel.wait(0.5):
                        cut()
                        return
            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            done_seen = False
            while True:
                try:
                    raw = r.readline()
                except (OSError, ValueError):
                    if cancel.is_set():
                        info["error"] = "cancelled"
                    else:
                        info["error"] = "read_failed"
                    break
                if not raw:
                    if cancel.is_set() and not done_seen:
                        info["error"] = "cancelled"   # 被切断：对端 EOF，不是正常结束
                    elif not done_seen:
                        info["error"] = "engine_closed"
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        done_seen = True
                    else:
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict) and obj.get("error"):
                            # oMLX 在流里报的错（HTTP 已经是 200）：例如开始流式以后内存守卫把请求中止了，发一行
                            # {"error": {..., "omlx_code": "prefill_memory_aborted"}}。不交给调用方（之前当成答复转给配件，
                            # 配件拿到空答复，10-22 16 GB 测试机 topics 第一题就这样空了），按出错结束
                            err = obj["error"] if isinstance(obj["error"], dict) else {"message": str(obj["error"])}
                            info["error"] = "stream_error"
                            info["detail"] = json.dumps(err, ensure_ascii=False)[:500]
                            info["memory"] = str(err.get("omlx_code", "")).startswith("prefill_memory") or "memory" in str(err.get("message", "")).lower()
                            cut()
                            break
                        if obj:
                            if info["first_token_secs"] is None and any((ch.get("delta") or {}).get("content") for ch in obj.get("choices", [])):
                                info["first_token_secs"] = round(time.time() - t0, 3)
                            if obj.get("usage"):
                                info["usage"] = obj["usage"]
                if not on_line(line):
                    cut()
                    info["error"] = "client_gone"
                    break
                if time.time() - t0 > total_timeout:
                    cut()
                    info["error"] = "timeout"
                    break
        except socket.timeout:
            info["error"] = "timeout"
        except Exception as e:  # noqa: BLE001
            info["error"] = "cancelled" if cancel.is_set() else f"{type(e).__name__}: {e}"[:200]
        finally:
            finished.set()   # 让 watcher 退出。之前是 cancel.set()：调用方之后看 cancel 永远是"已取消"，主控那两处"重试一次"
            # （引擎出错、内存拒绝）从来没触发过（internal-1021 在 16 GB 测试机上：cancelled=True）
            if timer:
                timer.cancel()
            try:
                c.close()
            except Exception:
                pass
        return info
