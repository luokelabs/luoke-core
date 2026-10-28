"""给高级用户的本机端点（顶层 ADR 0014 修订，2026-10-19 定）：OpenAI 兼容，只听 127.0.0.1，带密钥，默认关。

让 pi 这类工具改 base_url 和 key 就能用主控的引擎，不再单独跑一个 oMLX（"体系里不应该有脱离主控的 LLM 运行实例"）。
- 模型：主控自己这一档的底座（配方里的名字），和用户登记的本机模型（写成 user/<名字>）。登记的只读、只克隆进主控的模型目录，
  不改用户的文件和设置；不进评测和支持台账，质量由用户自己负责。
- 让路：请求算"用户在等"，排在后台前面，打字照旧最优先（打分是单独的进程）。引擎一次只留一个生成模型：要用户的模型就先打断后台、
  等手上用底座的请求做完、卸掉底座再加载；放不下答 507。用户模型闲了 USER_MODEL_IDLE_SECS 就卸掉，换回底座。
- 安全（ADR 0013 保留的）：只听本机；Host 只认 127.0.0.1 / localhost；带 Origin 的（网页）一律拒绝；请求体有上限；
  请求文字不落盘、不进日志（日志只记模型、token 数、耗时、结束原因）。
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import threading
import time
from typing import Callable, Optional

DEFAULT_PORT = 17860            # 避开邮件助手页面的 8765、oMLX 的 8000、Ollama 的 11434、LM Studio 的 1234
PORT_TRIES = 20                 # 默认的被占了，往后找空的
MAX_BODY = 32 << 20
USER_MODEL_IDLE_SECS = 300
USER_PREFIX = "user/"
REGISTRY = "user-models.json"
KEYFILE = "endpoint-key"
PASS_FIELDS = ("messages", "max_tokens", "temperature", "top_p", "top_k", "stop", "seed", "presence_penalty", "frequency_penalty",
               "repetition_penalty", "response_format", "tools", "tool_choice", "parallel_tool_calls", "logprobs", "top_logprobs",
               "stream", "stream_options", "chat_template_kwargs", "min_p")


def port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_port(want: int) -> Optional[int]:
    for p in range(want, want + PORT_TRIES):
        if port_free(p):
            return p
    return None


# ---- 用户登记的本机模型 ----------------------------------------------------------------------------------------

def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return s[:80] or "model"


def check_model_dir(path: str) -> tuple:
    """是不是一个能用的 MLX 模型目录：有 config.json 和至少一个 .safetensors。返回 (总字节数, 问题说明)。"""
    if not os.path.isdir(path):
        return 0, "不是文件夹"
    if not os.path.isfile(os.path.join(path, "config.json")):
        return 0, "里面没有 config.json，不像一个 MLX 模型"
    size, weights = 0, 0
    for root, _d, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            if os.path.isfile(p):
                size += os.path.getsize(p)
                weights += f.endswith(".safetensors")
    if not weights:
        return 0, "里面没有 .safetensors 权重"
    return size, None


class UserModels:
    """主控环境里的 user-models.json：[{name, source, dir, size_gb, how: clone/link, at}]。模型放在主控的模型目录下 user-<名字>。"""

    def __init__(self, env_dir: str, model_dir: str, log: Callable[[str], None]):
        self.path = os.path.join(env_dir, REGISTRY)
        self.model_dir = model_dir
        self.log = log
        self.lock = threading.Lock()

    def list(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as f:
                items = json.load(f).get("models") or []
        except (OSError, ValueError):
            return []
        return [m for m in items if isinstance(m, dict) and m.get("name") and m.get("dir")]

    def _save(self, items: list) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"models": items}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def get(self, name: str) -> Optional[dict]:
        return next((m for m in self.list() if m["name"] == name), None)

    def add(self, source: str, name: Optional[str] = None) -> dict:
        """登记：只读、只克隆。同一个卷上用 APFS 克隆（不多占空间，原文件以后删了也不影响）；不在同一个卷上就放一个指向原目录的链接。"""
        from . import download
        source = os.path.realpath(os.path.expanduser(source))
        size, problem = check_model_dir(source)
        if problem:
            raise ValueError(problem)
        name = slug(name or os.path.basename(source))
        with self.lock:
            items = [m for m in self.list() if m["name"] != name]
            dest = os.path.join(self.model_dir, "user-" + name)
            if os.path.lexists(dest):
                self._remove_dir(dest)
            os.makedirs(self.model_dir, exist_ok=True)
            how = "clone"
            same_volume = os.stat(source).st_dev == os.stat(self.model_dir).st_dev
            if same_volume:
                staging = dest + ".partial"
                shutil.rmtree(staging, ignore_errors=True)
                try:
                    for root, _d, files in os.walk(source):
                        for f in files:
                            rel = os.path.relpath(os.path.join(root, f), source)
                            if not download.clone_file(os.path.join(root, f), os.path.join(staging, rel)):
                                raise OSError(f"克隆不了 {rel}")
                    os.replace(staging, dest)
                except OSError:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            else:
                os.symlink(source, dest)
                how = "link"
            m = {"name": name, "source": source, "dir": os.path.basename(dest), "size_gb": round(size / 1e9, 2), "how": how,
                 "at": time.strftime("%Y-%m-%d %H:%M")}
            items.append(m)
            self._save(items)
        self.log(f"endpoint: registered user model {name} ({m['size_gb']} GB, {how}; source untouched)")
        return m

    @staticmethod
    def _remove_dir(p: str) -> None:
        if os.path.islink(p):
            os.remove(p)                 # 只删链接，不碰用户的原目录
        else:
            shutil.rmtree(p)

    def remove(self, name: str) -> bool:
        with self.lock:
            items = self.list()
            m = next((x for x in items if x["name"] == name), None)
            if not m:
                return False
            p = os.path.join(self.model_dir, m["dir"])
            if os.path.lexists(p) and os.path.basename(p).startswith("user-"):
                self._remove_dir(p)
            self._save([x for x in items if x["name"] != name])
        self.log(f"endpoint: removed user model {name} (its clone in the hub's model dir; the source is untouched)")
        return True


# ---- 密钥 ------------------------------------------------------------------------------------------------

def _write_key(p: str, k: str) -> None:
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(k)
    os.replace(p + ".tmp", p)


def load_key(env_dir: str, rotate: bool = False) -> str:
    """端点的密钥。第一次用时生成一把；用户可以自己设（set_key，任意字符串，例如沿用自己 oMLX 那把，工具的配置就不用改），
    也可以设成空字符串：空就不校验密钥（10-19 定）。挡网页攻击的是 Host 校验和拒绝 Origin，那两道照旧。"""
    p = os.path.join(env_dir, KEYFILE)
    if not rotate:
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()       # 文件在就以它为准，空也算（= 不要密钥）
        except OSError:
            pass
    k = "lk-" + secrets.token_urlsafe(32)
    _write_key(p, k)
    return k


def set_key(env_dir: str, key: str) -> str:
    key = key.strip()
    if "\n" in key or "\r" in key or len(key) > 512:
        raise ValueError("密钥不能有换行，最长 512 个字符")
    _write_key(os.path.join(env_dir, KEYFILE), key)
    return key


# ---- HTTP ------------------------------------------------------------------------------------------------

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(hub, key_ref: dict):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "luoke-core"

        def log_message(self, fmt, *args):
            pass                             # 不记请求行（路径不含内容，但也没必要）；元数据由 hub.endpoint_chat 记

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _err(self, code: int, kind: str, msg: str) -> None:
            self._json(code, {"error": {"type": kind, "message": msg}})

        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
            if host not in ("127.0.0.1", "localhost"):
                self._err(403, "forbidden", "只认 127.0.0.1 / localhost")       # 防 DNS rebinding
                return False
            if self.headers.get("Origin"):
                self._err(403, "forbidden", "不接受网页里发来的请求")
                return False
            auth = self.headers.get("Authorization") or ""
            if key_ref["key"] and not secrets.compare_digest(auth, "Bearer " + key_ref["key"]):     # 用户把密钥设成空：不校验
                self._err(401, "unauthorized", "密钥不对（在螺壳的设置 → 高级里复制）")
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            if self.path.rstrip("/") == "/v1/models":
                return self._json(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "luoke-core"}
                                                                   for m in hub.endpoint_models()]})
            self._err(404, "not_found", "没有这个接口")

        def do_POST(self):
            if not self._guard():
                return
            if self.path.rstrip("/") != "/v1/chat/completions":
                return self._err(404, "not_found", "没有这个接口（现在只有 /v1/chat/completions 和 /v1/models）")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._err(413, "too_large", "请求体为空或太大")
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._err(400, "invalid_request", "不是 JSON")
            if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not isinstance(body.get("model"), str):
                return self._err(400, "invalid_request", "要有 model 和 messages")
            hub.endpoint_chat(self, body)

    return H


class Endpoint:
    """开、关、换端口。主控启动时按设置开；设置页改了就重开。"""

    def __init__(self, hub):
        self.hub = hub
        self.server: Optional[Server] = None
        self.port: Optional[int] = None
        self.key_ref = {"key": ""}

    def start(self, want_port: int) -> Optional[int]:
        self.stop()
        port = pick_port(want_port)
        if port is None:
            self.hub.log(f"endpoint: no free port from {want_port}")
            return None
        self.key_ref["key"] = load_key(self.hub.env_dir)
        self.server = Server(("127.0.0.1", port), make_handler(self.hub, self.key_ref))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = port
        self.hub.log(f"endpoint: listening on 127.0.0.1:{port}")
        return port

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.hub.log(f"endpoint: stopped (was 127.0.0.1:{self.port})")
        self.server = None
        self.port = None

    def set_key(self, key: str) -> str:
        self.key_ref["key"] = set_key(self.hub.env_dir, key)
        self.hub.log("endpoint: key set by the user" + ("" if self.key_ref["key"] else " (empty: no key check)"))
        return self.key_ref["key"]

    def rotate_key(self) -> str:
        self.key_ref["key"] = load_key(self.hub.env_dir, rotate=True)
        self.hub.log("endpoint: key rotated")
        return self.key_ref["key"]


def forward(engine, payload: dict, handler, stream: bool, cancel: threading.Event, timeout: float = 1800) -> dict:
    """把请求转给引擎，原样把答复交回去（流式就一行一行转）。返回 {"status", "usage", "error"}（只有元数据）。"""
    c = http.client.HTTPConnection("127.0.0.1", engine.port, timeout=timeout)
    info = {"status": None, "usage": None, "error": None}
    try:
        c.request("POST", "/v1/chat/completions", body=json.dumps(payload).encode("utf-8"), headers=engine.headers())
        r = c.getresponse()
        info["status"] = r.status
        if not stream or r.status != 200:
            data = r.read()
            if r.status == 200:
                try:
                    info["usage"] = json.loads(data).get("usage")
                except ValueError:
                    pass
            handler.send_response(r.status)
            handler.send_header("Content-Type", r.getheader("Content-Type") or "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return info
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        while not cancel.is_set():
            line = r.readline()
            if not line:
                break
            if line.startswith(b"data: {") and b'"usage"' in line:
                try:
                    u = json.loads(line[6:]).get("usage")
                    info["usage"] = u or info["usage"]
                except ValueError:
                    pass
            try:
                handler.wfile.write(line)
                handler.wfile.flush()
            except OSError:
                info["error"] = "client_gone"
                break
        if cancel.is_set():
            info["error"] = "interrupted"
    except (OSError, http.client.HTTPException) as e:
        info["error"] = type(e).__name__
    finally:
        c.close()
    return info
