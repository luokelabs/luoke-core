"""任务层的客户端（标准库）：给测试用，也是配件接入的参考实现（V1 §2 线上格式）。

    from luoke_core.client import HubClient
    c = HubClient("luoke-mail/0.9", socket_path=None)      # 不给路径就按 LUOKE_HUB_SOCKET 和默认位置找
    c.tasks()
    for kind, data in c.chat("luoke-mail.triage", {...}, priority="background"): ...
    c.score("luoke-ime.rerank", "前文", ["候选一", "候选二"])
    c.embed("digup.embed", ["title: none | text: 一段话"])                      # 建索引（后台）
    c.embed("digup.embed", ["task: search result | query: 搜索词"], priority="user-waiting")
"""
from __future__ import annotations

import http.client
import json
import os
import socket
from typing import Iterator, Optional

DEFAULT_SOCKET = os.path.expanduser("~/Library/Application Support/luoke-core/run/hub.sock")
LAB_SOCKET = os.path.expanduser("~/Library/Application Support/luoke-core-lab/run/hub.sock")


class HubError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after=None, extra: Optional[dict] = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.retry_after, self.extra = status, code, message, retry_after, extra or {}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 600):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def find_socket(explicit: Optional[str] = None) -> str:
    """配件的找法（V1 §2）：显式给的 > 环境变量 LUOKE_HUB_SOCKET > 默认位置。
    开发和测试工具不要用退回：一律显式传 socket 路径，不存在就报错（顶层 CLAUDE.md「不碰日常在用的实例」）。"""
    if explicit:
        if not os.path.exists(explicit):
            raise FileNotFoundError(f"主控 socket 不存在：{explicit}（不会退回默认位置）")
        return explicit
    return os.environ.get("LUOKE_HUB_SOCKET") or DEFAULT_SOCKET


class HubClient:
    def __init__(self, accessory: str, socket_path: Optional[str] = None, timeout: float = 600):
        self.accessory = accessory
        self.path = find_socket(socket_path)
        self.timeout = timeout
        self.tasks_version: Optional[str] = None   # 最近一次答复里的 X-Luoke-Tasks-Version
        self.tasks_changed = False                 # 变过就置 True；调用方重读 tasks() 后清掉

    def _note_version(self, r) -> None:
        v = r.getheader("X-Luoke-Tasks-Version")
        if v and v != self.tasks_version:
            if self.tasks_version is not None:
                self.tasks_changed = True
            self.tasks_version = v

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"X-Luoke-Accessory": self.accessory, "Content-Type": "application/json"}
        h.update(extra or {})
        return h

    def _call(self, method: str, path: str, body: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
        c = UnixConnection(self.path, self.timeout)
        c.request(method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=self._headers(headers))
        r = c.getresponse()
        self._note_version(r)
        raw = r.read()
        c.close()
        data = json.loads(raw.decode("utf-8")) if raw else {}
        if r.status >= 400:
            raise HubError(r.status, data.get("code", "?"), data.get("message") or data.get("error", ""), data.get("retry_after"), data)
        return data

    def tasks(self) -> dict:
        d = self._call("GET", "/tasks")
        self.tasks_changed = False
        return d

    def status(self) -> dict:
        return self._call("GET", "/status")

    def warmup(self, task: str, lease_id: Optional[str] = None) -> dict:
        b = {"task": task}
        if lease_id:
            b["lease_id"] = lease_id
        return self._call("POST", "/warmup", b)

    def resident(self, flag: bool) -> dict:
        return self._call("PUT", "/accessory/resident", {"resident": flag})

    def exiting(self) -> dict:
        return self._call("POST", "/accessory/exiting", {})

    def settings(self, patch: Optional[dict] = None) -> dict:
        return self._call("PUT", "/settings", patch) if patch is not None else self._call("GET", "/settings")

    def downloads(self, names: Optional[list] = None, start: bool = False) -> dict:
        return self._call("POST", "/downloads", {"names": names} if names else {}) if start else self._call("GET", "/downloads")

    def smoke(self) -> dict:
        """重新做冒烟检查（只认主控身份；后台做，看 status()["smoke"]）。"""
        return self._call("POST", "/smoke", {})

    def uninstall(self, accessory: str, data: bool = False, forget: bool = False, dry_run: bool = False) -> dict:
        """只有主控自己的界面能调（配件身份会被拒）。"""
        return self._call("POST", "/uninstall", {"accessory": accessory, "data": data, "forget": forget, "dry_run": dry_run})

    def predict(self, task: str, context: str, k: Optional[int] = None, n: Optional[int] = None) -> dict:
        b = {"context": context}
        if k is not None:
            b["k"] = k
        if n is not None:
            b["n"] = n
        return self._call("POST", f"/tasks/{task}/v1/predict", b)

    def continue_(self, task: str, context: str, k: Optional[int] = None, max_tokens: Optional[int] = None, stop: Optional[list] = None) -> dict:
        """通用续写（V1 §2）：k 个分支各续到 max_tokens 或 stop；截法、挑法归调用方。"""
        b = {"context": context}
        for key, v in (("k", k), ("max_tokens", max_tokens), ("stop", stop)):
            if v is not None:
                b[key] = v
        return self._call("POST", f"/tasks/{task}/v1/continue", b)

    def score(self, task: str, context: str, candidates: list) -> dict:
        return self._call("POST", f"/tasks/{task}/v1/score", {"context": context, "candidates": candidates})

    def embed(self, task: str, texts: list, priority: str = "background", allow_battery: bool = False) -> dict:
        """算向量（V1 §2 /v1/embed）：texts 由调用方加好检索提示前缀（"task: search result | query: …"、"title: … | text: …"）。
        建索引用默认的 background；搜索时的查询传 priority="user-waiting"。
        答复 {"vectors": [[...], ...], "dims", "model", "vector_space", "ms"}：满维、L2 归一化（点积即余弦相似度）、float16 精度。
        vector_space 只比相等：和索引里记的不一样，就只重算向量（GET /tasks 的嵌入任务也带它）。"""
        h = {"X-Luoke-Priority": priority}
        if allow_battery:
            h["X-Luoke-Allow-Battery"] = "1"
        return self._call("POST", f"/tasks/{task}/v1/embed", {"texts": texts}, headers=h)

    def embed_inputs(self, task: str, inputs: list, priority: str = "background", allow_battery: bool = False) -> dict:
        """图片和声音（草案 docs/research/embed-multimodal-draft.md，还不是契约；只在任务参数有 image_batch / audio_batch 的档可用）：
        inputs 是 [{"text": …} | {"image": "<base64>", "budget": 70|140|280|…} | {"audio": "<base64 WAV>"}]；
        答复比 embed 多 "errors"（和 inputs 对齐，坏的那条 vector 是 null）。"""
        h = {"X-Luoke-Priority": priority}
        if allow_battery:
            h["X-Luoke-Allow-Battery"] = "1"
        return self._call("POST", f"/tasks/{task}/v1/embed", {"inputs": inputs}, headers=h)

    def chat(self, task: str, body: dict, priority: str = "user-waiting", allow_battery: bool = False) -> Iterator[tuple]:
        """流式生成。逐个产出 ("progress", dict) / ("chunk", dict) / ("error", dict) / ("done", None)。
        开始前的拒绝抛 HubError；收到 error 事件后调用方丢弃已收到的部分输出。"""
        body = dict(body, stream=True)
        h = {"X-Luoke-Priority": priority}
        if allow_battery:
            h["X-Luoke-Allow-Battery"] = "1"
        c = UnixConnection(self.path, self.timeout)
        c.request("POST", f"/tasks/{task}/v1/chat/completions", body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                  headers=self._headers(h))
        r = c.getresponse()
        self._note_version(r)
        if r.status != 200:
            raw = r.read()
            c.close()
            data = json.loads(raw.decode("utf-8")) if raw else {}
            raise HubError(r.status, data.get("code", "?"), data.get("message", ""), data.get("retry_after"), data)
        event = None
        try:
            while True:
                raw = r.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
                    if event:
                        yield ("progress" if event == "luoke.progress" else "error"), json.loads(data)
                        if event == "luoke.error":
                            return
                        event = None
                    elif data == "[DONE]":
                        yield "done", None
                        return
                    else:
                        yield "chunk", json.loads(data)
                elif line == "":
                    event = None if event is None else event
        finally:
            c.close()
