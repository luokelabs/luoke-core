"""主控这边的打分工作进程监管：起子进程（引擎包的 Python + worker/score_worker.py）、按行协议打分、看门狗。

父子之间只有继承的管道；子进程不监听端口，父进程消失它就退出（stdin 关闭）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Callable, Optional

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker", "score_worker.py")
EMBED_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker", "embed_worker.py")


class ScorerError(RuntimeError):
    pass


class _Worker:
    """专用模型工作进程的监管骨架：起子进程、等就绪、按行协议请求、看门狗。
    打分（ScoreWorker）和嵌入（EmbedWorker）共用，差别只在子进程脚本和启动参数（_argv）、以及名字（kind）。"""
    kind = "worker"
    log_name = "worker"

    def __init__(self, python: str, site: str, model_dir: str, log: Optional[Callable[[str], None]] = None,
                 cache_mb: int = 1024):
        self.python, self.site, self.model_dir = python, site, model_dir
        self.cache_mb = cache_mb          # 工作进程里 MLX 缓冲的上限（配方专用模型的 cache_mb）
        self.log = log or (lambda s: None)
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.state = "stopped"   # stopped / loading / ready / failed
        self.probed = False      # 这次起来以后主控测过探针没有（slow 标记用；每次重启重测。嵌入不探，恒 True 效果）
        self.probe_attempts = 0
        self.load_secs: Optional[float] = None
        self.failures = 0
        self.ready_info: dict = {}   # 子进程 ready 行里带的元数据（嵌入的维度等）
        self.last_used = time.time() # 最近一次起进程或请求的时刻：主控据此空闲停（issue #11）
        self.idle_stopped = False    # 上次是空闲停的：重新起来时沿用探针结果，不再测一遍

    def _argv(self) -> list:
        raise NotImplementedError

    def start(self) -> None:
        with self.lock:
            if self.state in ("loading", "ready"):
                return
            self.state = "loading"
            self.last_used = time.time()
            if not self.idle_stopped:   # 空闲停后按需重起：同一个模型、同一台机器，探针结果不变
                self.probed, self.probe_attempts = False, 0
            self.idle_stopped = False
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/"), "PYTHONPATH": self.site,
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1"}     # 和引擎一样不去 Hugging Face 找（luoke-core#35）
        t0 = time.time()
        try:
            self.proc = subprocess.Popen(self._argv(), env=env, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True)
        except OSError:
            with self.lock:      # 起不来就记失败：不然一直停在 loading，下次请求也不会重起
                self.state = "failed"
                self.failures += 1
            raise

        def wait_ready():
            line = self.proc.stdout.readline() if self.proc.stdout else ""
            with self.lock:
                if line.startswith("{") and json.loads(line).get("ready"):
                    self.state = "ready"
                    self.load_secs = round(time.time() - t0, 1)
                    self.ready_info = json.loads(line)
                    self.log(f"{self.log_name} ready in {self.load_secs}s ({self.model_dir})")
                else:
                    self.state = "failed"
                    self.failures += 1
                    self.log(f"{self.log_name} failed to start: {line.strip()[:200]!r}")
        threading.Thread(target=wait_ready, daemon=True).start()

    def stop(self) -> None:
        with self.lock:
            p, self.proc = self.proc, None
            self.state = "stopped"
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(10)
            except Exception:
                p.kill()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _ask(self, req: dict, timeout: float) -> dict:
        with self.lock:
            self.last_used = time.time()
            if self.state != "ready" or not self.alive():
                raise ScorerError("not_ready" if self.state == "loading" else "engine_failed")
            p = self.proc
            try:
                p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                p.stdin.flush()
            except (OSError, ValueError):
                self.state = "failed"
                raise ScorerError("engine_failed")
            result: dict = {}

            def read():
                try:
                    result["line"] = p.stdout.readline()
                except Exception as e:  # noqa: BLE001
                    result["err"] = str(e)
            th = threading.Thread(target=read, daemon=True)
            th.start()
            th.join(timeout)
            if th.is_alive() or not result.get("line"):
                self.state = "failed"
                self.failures += 1
                try:
                    p.kill()
                except Exception:
                    pass
                raise ScorerError("engine_failed")
            rep = json.loads(result["line"])
            if "error" in rep:
                raise ScorerError("engine_failed")
            return rep


class ScoreWorker(_Worker):
    kind = "score"
    log_name = "scorer"

    def __init__(self, python: str, site: str, model_dir: str, snapshots: int, log: Optional[Callable[[str], None]] = None,
                 cache_mb: int = 1024):
        super().__init__(python, site, model_dir, log, cache_mb)
        self.snapshots = snapshots

    def _argv(self) -> list:
        return [self.python, "-B", WORKER, self.model_dir, "--snapshots", str(self.snapshots), "--cache-mb", str(self.cache_mb)]

    def score(self, ctx: str, cands: list, timeout: float) -> dict:
        """返回 {"lp", "ms", "hit"}；没就绪抛 ScorerError("not_ready")，超时或崩溃抛 ScorerError("engine_failed")。"""
        return self._ask({"ctx": ctx, "cands": cands}, timeout)

    def predict(self, ctx: str, k: int, n: int, timeout: float) -> dict:
        """联想：返回 {"cont", "ms", "hit"}。"""
        return self._ask({"op": "predict", "ctx": ctx, "k": k, "n": n}, timeout)

    def continue_(self, ctx: str, k: int, max_tokens: int, stop: list, timeout: float) -> dict:
        """通用续写（/v1/continue）：返回 {"branches", "ms", "hit"}。"""
        return self._ask({"op": "continue", "ctx": ctx, "k": k, "max_tokens": max_tokens, "stop": stop}, timeout)


class EmbedWorker(_Worker):
    """嵌入模型（EmbeddingGemma 2）的工作进程：无状态，一进一出（worker/embed_worker.py）。"""
    kind = "embed"
    log_name = "embedder"

    def __init__(self, python: str, site: str, model_dir: str, log: Optional[Callable[[str], None]] = None,
                 cache_mb: int = 512, media: bool = False):
        super().__init__(python, site, model_dir, log, cache_mb)
        self.media = media        # 连视觉、声音一起加载（任务参数里有 image_batch / audio_batch 时）

    def _argv(self) -> list:
        return [self.python, "-B", EMBED_WORKER, self.model_dir, "--cache-mb", str(self.cache_mb)] + (["--media"] if self.media else [])

    def embed(self, texts: list, max_tokens: int, timeout: float) -> dict:
        """texts 已带调用方的检索提示前缀；每条按 token 截到 max_tokens。返回 {"vectors", "dims", "ms"}；
        没就绪抛 ScorerError("not_ready")，超时或崩溃抛 ScorerError("engine_failed")。"""
        return self._ask({"op": "embed", "texts": texts, "max_tokens": max_tokens}, timeout)

    def embed_inputs(self, inputs: list, max_tokens: int, image_batch: int, audio_batch: int, max_audio_secs: float,
                     timeout: float) -> dict:
        """多模态（草案，docs/research/embed-multimodal-draft.md）：返回 {"vectors", "errors", "dims", "ms"}，都和 inputs 对齐。"""
        return self._ask({"op": "embed_inputs", "inputs": inputs, "max_tokens": max_tokens, "image_batch": image_batch,
                          "audio_batch": audio_batch, "max_audio_secs": max_audio_secs}, timeout)
