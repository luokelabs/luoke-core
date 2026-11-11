#!/usr/bin/env python3
"""螺壳的薄引擎 v3（对照实验，luoke-core#42）：v2（连续批处理）加上按消息边界的前缀缓存，预填充块大小可调。
两样都是从 mlx-serve 学的，做法用 mlx-lm 0.32.2 自带的 LRUPromptCache 和 BatchGenerator 的分段插入：

- 前缀缓存：提示词按「系统提示 / 其余 / 思考尾巴」切段（同 mlx-lm server 的 _tokenize），系统提示和其余两段的段尾、
  以及每条做完时，把状态存进内存里的 LRU；下一条开头一样的请求从最长能接上的那一段往下算。Qwen3.5 这类混合模型的
  线性注意力状态截不短，所以只能整段接（mlx-serve：hybrid 只认能恢复的检查点）。只在内存里，不落盘。
  存进去的是自己的一份：从批里切出来的状态和整批共用一块内存，不复制会把整批一起留住（mlx-serve 踩过，多占 3 倍多）。
  上限按字节（--prefix-cache-mb，默认 512，0 关掉），超了先扔做完的、再扔中间段、最后扔系统提示。
  oMLX 自己的缓存对混合模型按 2048 token 一块，分拣的系统提示不足一块，螺壳实测命中 0%（DESIGN §5），所以主控关了它。
- 预填充块（--prefill-step-size，默认 2048 同 oMLX）：mlx-serve 发现块切得碎是它预填充落后的最大原因，这里留个口子量。

    thin_engine.py serve --base-path B --model-dir M --host 127.0.0.1 --port P [--memory-guard-gb G]
                          [--max-concurrent-requests N] [--prefix-cache-mb 512] [--prefill-step-size 2048]
    （oMLX 的其他参数照收不用：主控把它当 omlx-cli 起，一个字不改）

以下是 v2 的说明，照旧成立。

v2：v1 加上连续批处理。生成交给 mlx-lm 的 BatchGenerator
（主控用 --no-cache 起 oMLX 0.7.2 时，它的批处理也是这个），一个调度线程独占 GPU，HTTP 线程只管分词、语法和流式。

和 v1 一样：密钥只从 B/settings.json 读、只听本机、只认 M 里的本地目录；/v1/models、load/unload、放不下回 507；
流式、断开就停；json_schema 用 xgrammar 约束；预填充超上限中止（prefill_memory_aborted）；/admin/api/global-settings。

不一样的：
- 同时最多 N 条一起解码（默认 8，和 oMLX 一样），多的排队。BatchGenerator 每轮先给在解码的各解一个 token，
  再给新来的那条算一块（≤2048 token）预填充；一次只预填充一条，也和 oMLX 一样。N=1 就是一条接一条。
- 加载、卸载、生成都在调度线程里（mlx-lm 的生成流是线程局部的）。有请求在跑时卸载，等它们做完再卸。
- 不认 seed（批里各条共用一个随机源）；主控不发 seed。
- mlx-lm 0.32.2 的批处理在 Qwen3.5 这类线性注意力混合模型上有两处毛病，oMLX 0.7.2 打了补丁，这里同样补：
  ArraysCache.extract 碰到空槽会崩；ArraysCache 的 left_padding/lengths 每步减一次，只有一层被求值，
  别的层的惰性图越积越长（久了撞 Metal 的缓冲上限）——调度线程每 32 步把它们求值一次。
"""
from __future__ import annotations

import argparse
import gc
import glob
import http.server
import json
import os
import queue
import secrets
import select
import socket
import sys
import threading
import time
import urllib.parse
import uuid

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import mlx.core as mx  # noqa: E402
import numpy as np  # noqa: E402
from mlx_lm import load as mlx_load  # noqa: E402
from mlx_lm.generate import BatchGenerator  # noqa: E402
from mlx_lm.models.cache import ArraysCache, KVCache, LRUPromptCache, make_prompt_cache  # noqa: E402
from mlx_lm.sample_utils import make_sampler  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import torch_stub  # noqa: E402  （xgrammar 导入时要 torch，从不真用；oMLX 0.7.2 的同一份桩）

torch_stub.install()
import xgrammar as xgr  # noqa: E402
from xgrammar.kernels.apply_token_bitmask_mlx import apply_token_bitmask_mlx  # noqa: E402

GiB = 1024 ** 3
JSON_MAX_WHITESPACE = 32      # 和 oMLX 一样：空白不限会把约束解码困在早结束的字符串后面
SETTLE_EVERY = 32             # 每多少步把 ArraysCache 的填充元数据求值一次
PREFIX_ENTRIES = 32           # 前缀缓存最多几条（字节上限另算）
CACHED_TYPES = ("system", "user")   # 段尾存哪几种（思考尾巴那段不存：每条都不一样，做完时整条另存）


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def _extract(self, idx):
    """ArraysCache.extract，空槽留空（同 oMLX 的 patches/arrays_cache_extract.py）。"""
    c = ArraysCache(len(self.cache))
    c.cache = [None if a is None else a[idx: idx + 1] for a in self.cache]
    return c


ArraysCache.extract = _extract


def owned(cache: list) -> list:
    """缓存条目要自己的一份内存：extract 切出来的是整批状态里的一片，留着它就把整批留住了（mlx-serve 的
    materializedOwnedCopy）。做一次加法得到新的一块（copy、contiguous 可能还是共用）。"""
    arrs = []
    for c in cache:
        if isinstance(c, ArraysCache):
            c.cache = [None if a is None else a + 0 for a in c.cache]
            arrs += [a for a in c.cache if a is not None]
        elif isinstance(c, KVCache) and c.keys is not None:
            c.keys, c.values = c.keys[..., :c.offset, :] + 0, c.values[..., :c.offset, :] + 0
            arrs += [c.keys, c.values]
    mx.eval(arrs)
    return cache


def segment(tok, messages: list, prompt: list, ctk: dict) -> tuple:
    """切段（同 mlx-lm 0.32.2 server 的 _tokenize）：系统提示（到第一条用户消息的正文前）、其余、末尾 ≤11 个 token 的思考尾巴。
    块不跨段，段尾就是能存、能接的地方。"""
    if not messages or messages[-1].get("role") != "user":
        return [prompt], ["assistant"]
    segs, types = [], []
    n_sys = 0
    for x in messages:
        if x.get("role") != "system":
            break
        n_sys += 1
    sys_end = 0
    if n_sys:
        text = tok.apply_chat_template(messages[:n_sys] + [{"role": "user", "content": ""}], add_generation_prompt=False,
                                       tokenize=False, **ctk)
        for i, (a, b) in enumerate(zip(tok.encode(text, add_special_tokens=False), prompt)):
            if a != b:
                sys_end = i
                break
        if 0 < sys_end < len(prompt):
            segs.append(prompt[:sys_end])
            types.append("system")
        else:
            sys_end = 0
    tail = len(prompt)
    if tok.has_thinking:
        t = tok.rfind_think_start(prompt, start=tail - 11)
        if t >= 0:
            tail = t
    if sys_end < tail:
        segs.append(prompt[sys_end:tail])
        types.append("user")
    if tail < len(prompt):
        segs.append(prompt[tail:])
        types.append("assistant")
    return (segs, types) if segs else ([prompt], ["assistant"])


class Cancelled(Exception):
    pass


class Model:
    def __init__(self, mid: str, path: str, E: "Engine"):
        self.id, self.path = mid, path
        self.size = sum(os.path.getsize(f) for f in glob.glob(os.path.join(path, "*.safetensors")))
        t0 = time.time()
        self.model, self.tok = mlx_load(path)
        mx.eval(self.model.parameters())
        if not all(hasattr(c, "merge") for c in make_prompt_cache(self.model)):
            raise ValueError("这个模型的缓存不支持批处理")
        self.gen = E.generator(self)
        self.pc = LRUPromptCache(max_size=PREFIX_ENTRIES, max_bytes=E.prefix_bytes) if E.prefix_bytes else None
        self.load_secs = time.time() - t0
        cfg = json.load(open(os.path.join(path, "config.json")))
        self.vocab = cfg.get("vocab_size") or (cfg.get("text_config") or {}).get("vocab_size")
        self.compiler = None
        self.compile_lock = threading.Lock()      # 几条请求同时到，各自的 HTTP 线程都要编语法
        conv = self.tok._tokenizer.convert_tokens_to_ids
        self.think_open, self.think_close = conv("<think>"), conv("</think>")
        self.reqs: dict = {}                      # uid -> Req（只有调度线程碰）

    def grammar(self, schema, active: bool) -> "Grammar":
        with self.compile_lock:
            if self.compiler is None:
                info = xgr.TokenizerInfo.from_huggingface(self.tok._tokenizer, vocab_size=self.vocab)
                self.compiler = xgr.GrammarCompiler(info)
            if not schema:
                compiled = self.compiler.compile_builtin_json_grammar()
            else:
                s = schema if isinstance(schema, str) else json.dumps(schema)
                compiled = self.compiler.compile_json_schema(s, max_whitespace_cnt=JSON_MAX_WHITESPACE)
        return Grammar(compiled, self.vocab, self.think_close, active)


class Grammar:
    """logits 处理器（在调度线程里跑）：先把上一步按我们的遮罩抽出的 token 喂给 matcher，再按语法把不许出的遮掉。
    提示词停在思考里时先不约束，看到 </think> 才开始（v1 由 HTTP 线程切换；批处理下处理器跑在别的线程，自己看）。"""

    def __init__(self, compiled, vocab: int, think_close: int, active: bool):
        self.m = xgr.GrammarMatcher(compiled)
        self.vocab = vocab
        self.mask = np.full((1, (vocab + 31) // 32), -1, dtype=np.int32)
        self.think_close = think_close
        self.active = active
        self.started = False

    def __call__(self, tokens, logits):
        last = int(tokens[-1])
        if self.active and self.started:
            self.m.accept_token(last)
        self.started = True
        if not self.active:
            if last != self.think_close:
                return logits
            self.active = True
        if self.m.is_terminated():
            return logits
        self.mask.fill(-1)
        self.m.fill_next_token_bitmask(self.mask)
        return apply_token_bitmask_mlx(mx.array(self.mask), logits, self.vocab)


class Req:
    def __init__(self, rid: str, mid: str, prompt: list, segs: tuple, sampler, procs: list, max_tokens: int):
        self.rid, self.mid, self.prompt = rid, mid, prompt
        self.segs, self.types = segs                  # 切好的段和各段的类型
        self.left: list = []                          # 还没到段尾的那几段的类型，倒着放（段尾到一次 pop 一个）
        self.cached = 0                               # 从前缀缓存接上的 token 数
        self.sampler, self.procs, self.max_tokens = sampler, procs, max_tokens
        self.events: queue.Queue = queue.Queue()   # ("progress", done, total) / ("tok", id, finish) / ("err", code, msg)
        self.cancelled = False                     # HTTP 线程置上，调度线程下一轮从批里拿掉
        self.uid = None
        self.base = 0                              # 插进批时的 active 内存（预填充推算用）


class Scheduler(threading.Thread):
    """唯一碰 GPU 的线程：收命令（load、unload、chat），有活就一轮轮 BatchGenerator.next()。"""

    def __init__(self, E: "Engine"):
        super().__init__(daemon=True, name="scheduler")
        self.E = E
        self.inbox: queue.Queue = queue.Queue()
        self.unloading: list = []                  # [(mid, reply)]：等它的请求做完再卸
        self.steps = 0

    def call(self, op: str, mid: str) -> tuple:
        reply: queue.Queue = queue.Queue()
        self.inbox.put((op, mid, reply))
        return reply.get()

    def run(self) -> None:
        while True:
            busy = any(m.reqs for m in self.E.models.values())
            block = not busy and not self.unloading
            while True:
                try:
                    cmd = self.inbox.get(block=block)
                except queue.Empty:
                    break
                block = False
                self.handle(cmd)
            for m in list(self.E.models.values()):
                if m.reqs:
                    self.step(m)
            for mid, reply in list(self.unloading):
                m = self.E.models.get(mid)
                if m is None or not m.reqs:
                    self.unloading.remove((mid, reply))
                    reply.put(self.E.unload(mid))

    def handle(self, cmd) -> None:
        if cmd[0] == "load":
            cmd[2].put(self.E.load(cmd[1]))
        elif cmd[0] == "unload":
            self.unloading.append((cmd[1], cmd[2]))
        else:
            r: Req = cmd[1]
            m = self.E.models.get(r.mid)
            if m is None or any(mid == r.mid for mid, _ in self.unloading):
                r.events.put(("err", 400, f"模型 {r.mid} 没加载"))
                return
            if r.cancelled:
                return
            r.base = mx.get_active_memory()
            cache, n = None, 0
            if m.pc is not None:
                cache, rest = m.pc.fetch_nearest_cache(m.id, r.prompt)
                n = len(r.prompt) - len(rest)
                if cache is None or not rest:         # 整条都命中也不用：总得剩一个 token 去算，混合模型的缓存又截不短
                    cache, n = None, 0
            segs, types, k = [list(s) for s in r.segs], list(r.types), n
            while k > 0:                              # 接上的那部分从段里去掉
                if k >= len(segs[0]):
                    k -= len(segs.pop(0))
                    types.pop(0)
                else:
                    segs[0] = segs[0][k:]
                    break
            r.cached, r.left = n, types[::-1]
            (r.uid,) = m.gen.insert_segments([segs], max_tokens=[r.max_tokens], caches=[cache], all_tokens=[r.prompt[:n]],
                                             samplers=[r.sampler], logits_processors=[r.procs])
            m.reqs[r.uid] = r

    def step(self, m: Model) -> None:
        gone = [u for u, r in m.reqs.items() if r.cancelled]
        if gone:
            m.gen.remove(gone)
            for u in gone:
                m.reqs.pop(u)
            if not m.reqs:
                return self.idle()
        try:
            prompt_resps, gen_resps = m.gen.next()
        except Exception as e:  # noqa: BLE001  一条出错整批作废，换一个新的生成器
            log(f"batch on {m.id} failed: {type(e).__name__}: {e}")
            for r in m.reqs.values():
                r.events.put(("err", 500, f"{type(e).__name__}: {e}"[:300]))
            m.reqs.clear()
            m.gen.close()
            m.gen = self.E.generator(m)
            return self.idle()
        seg_ends = {}
        for p in prompt_resps:
            r = m.reqs.get(p.uid)
            if r is None:
                continue
            done, total = p.progress
            r.events.put(("progress", done, total))
            why = self.over(r, done, total)
            if why:
                m.gen.remove([p.uid])
                m.reqs.pop(p.uid)
                r.events.put(("err", "prefill_memory_aborted", why))
            elif p.end_of_segment and not p.end_of_prompt and r.left:
                t = r.left.pop()
                if t in CACHED_TYPES and m.pc is not None:
                    seg_ends[p.uid] = t
        if seg_ends:                                  # 段尾：这一段算完的状态存一份（mlx-lm server 同样做）
            for uid, (cache, toks) in m.gen.extract_cache(list(seg_ends)).items():
                m.pc.insert_cache(m.id, list(toks), owned(cache), cache_type=seg_ends[uid])
        for g in gen_resps:
            r = m.reqs.get(g.uid)
            if r is None:
                continue
            r.events.put(("tok", g.token, g.finish_reason))
            if g.finish_reason:
                m.reqs.pop(g.uid)
                if m.pc is not None and g.prompt_cache is not None:   # 整条（提示 + 回答）也存：多轮对话下一轮能接上
                    m.pc.insert_cache(m.id, list(g.all_tokens), owned(g.prompt_cache), cache_type="assistant")
        self.steps += 1
        if self.steps % SETTLE_EVERY == 0:
            meta = [a for c in m.gen._generation_batch.prompt_cache if isinstance(c, ArraysCache)
                    for a in (c.left_padding, c.lengths) if a is not None]
            if meta:
                mx.eval(meta)
        if not m.reqs:
            self.idle()

    def over(self, r: Req, done: int, total: int):
        """预填充时内存（active）超过上限，或按已走的块推算会超过：返回原因（同 v1 的推算，批里别的请求的增长也算进去，偏保守）。"""
        ceiling = self.E.ceiling
        if not ceiling or done <= 0 or done >= total:
            return None
        now = mx.get_active_memory()
        projected = r.base + (now - r.base) / done * total
        if now > ceiling or projected > ceiling:
            return f"预填充要 {projected / GiB:.1f} GB，上限 {ceiling / GiB:.1f} GB（已走 {done}/{total}）"
        return None

    def idle(self) -> None:
        if not any(m.reqs for m in self.E.models.values()):
            mx.clear_cache()


class Engine:
    def __init__(self, a):
        self.model_dir = a.model_dir
        self.ceiling = float(a.memory_guard_gb or 0) * GiB or None
        self.max_seqs = max(1, int(a.max_concurrent_requests or 8))
        self.prefix_bytes = int(max(0.0, a.prefix_cache_mb) * 1024 ** 2)
        self.prefill_step = max(256, int(a.prefill_step_size))
        with open(os.path.join(a.base_path, "settings.json")) as f:
            self.key = json.load(f)["auth"]["api_key"]
        self.models: dict[str, Model] = {}
        self.sessions: set = set()
        self.samplers: dict = {}               # 同样的采样参数共用一个，批里就能一次采完
        self.sched = Scheduler(self)

    def available(self) -> dict:
        out = {}
        for name in sorted(os.listdir(self.model_dir)):
            p = os.path.realpath(os.path.join(self.model_dir, name))
            if os.path.isfile(os.path.join(p, "config.json")):
                out[name] = p
        return out

    def generator(self, m: "Model") -> BatchGenerator:
        """在调度线程里调。"""
        return BatchGenerator(m.model, stop_tokens=[[t] for t in m.tok.eos_token_ids], completion_batch_size=self.max_seqs,
                              prefill_batch_size=1, prefill_step_size=self.prefill_step)

    def sampler(self, temp: float, top_p: float):
        k = (temp, top_p)
        if k not in self.samplers:
            self.samplers[k] = make_sampler(temp=temp, top_p=top_p if top_p < 1.0 else 0.0)
        return self.samplers[k]

    def load(self, mid: str) -> tuple[int, dict]:
        """在调度线程里跑。"""
        avail = self.available()
        if mid not in avail:
            return 404, {"error": {"message": f"没有模型 {mid}"}}
        if mid in self.models:
            return 200, {"id": mid, "loaded": True}
        size = sum(os.path.getsize(f) for f in glob.glob(os.path.join(avail[mid], "*.safetensors")))
        held = sum(m.size for m in self.models.values())
        if self.ceiling and held + size > self.ceiling:
            log(f"load {mid} refused: {(held + size) / GiB:.1f} GB > ceiling {self.ceiling / GiB:.1f} GB")
            return 507, {"detail": f"放不下：已加载 {held / GiB:.1f} GB，加上它 {(held + size) / GiB:.1f} GB，上限 {self.ceiling / GiB:.1f} GB"}
        try:
            m = Model(mid, avail[mid], self)
        except Exception as e:  # noqa: BLE001
            log(f"load {mid} failed: {type(e).__name__}: {e}")
            return 500, {"detail": f"{type(e).__name__}: {e}"[:300]}
        self.models[mid] = m
        log(f"loaded {mid} in {m.load_secs:.1f}s ({m.size / GiB:.2f} GB, active {mx.get_active_memory() / GiB:.2f} GB, batch ≤{self.max_seqs}, "
            f"prefill step {self.prefill_step}, prefix cache {self.prefix_bytes / 1024 ** 2:.0f} MB)")
        return 200, {"id": mid, "loaded": True, "secs": round(m.load_secs, 1)}

    def unload(self, mid: str) -> tuple[int, dict]:
        """在调度线程里跑，那个模型已经没有请求。"""
        m = self.models.pop(mid, None)
        if m is not None:
            m.gen.close()
        del m
        gc.collect()
        mx.clear_cache()
        log(f"unloaded {mid} (active {mx.get_active_memory() / GiB:.2f} GB)")
        return 200, {"id": mid, "loaded": False}

    def set_ceiling(self, gb: float) -> None:
        self.ceiling = gb * GiB
        log(f"ceiling -> {gb} GB")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "luoke-thin-engine/3"
    E: Engine = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # 访问日志不要：请求内容不进日志
        pass

    # ---- 小工具 ----
    def _json(self, code: int, obj: dict, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if self.headers.get("Authorization", "") == "Bearer " + self.E.key:
            return True
        self._json(401, {"error": {"message": "unauthorized"}})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _gone(self) -> bool:
        """对端关了连接（主控取消就是这样）：可读而读到 0 字节。"""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    # ---- 路由 ----
    def do_GET(self):
        if not self._authed():
            return
        if self.path.rstrip("/") == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": k, "object": "model", "owned_by": "local"} for k in self.E.available()]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        p = self.path.split("?")[0].rstrip("/")
        if p == "/admin/api/login":
            b = self._body()
            if b.get("api_key") != self.E.key:
                return self._json(401, {"error": {"message": "unauthorized"}})
            sid = secrets.token_hex(16)
            self.E.sessions.add(sid)
            return self._json(200, {"ok": True}, {"Set-Cookie": f"thin_session={sid}; Path=/; HttpOnly"})
        if p == "/admin/api/global-settings":
            sid = (self.headers.get("Cookie") or "").partition("thin_session=")[2].split(";")[0]
            if sid not in self.E.sessions:
                return self._json(401, {"error": {"message": "unauthorized"}})
            b = self._body()
            if "memory_guard_custom_ceiling_gb" in b:
                self.E.set_ceiling(float(b["memory_guard_custom_ceiling_gb"]))
            return self._json(200, {"ok": True})
        if not self._authed():
            return
        if p.startswith("/v1/models/") and p.endswith(("/load", "/unload")):
            mid = urllib.parse.unquote(p[len("/v1/models/"):].rsplit("/", 1)[0])
            code, obj = self.E.sched.call("load" if p.endswith("/load") else "unload", mid)
            return self._json(code, obj)
        if p == "/v1/chat/completions":
            return self.chat(self._body())
        self._json(404, {"error": {"message": "not found"}})

    # ---- 生成 ----
    def chat(self, b: dict) -> None:
        E = self.E
        mid = b.get("model")
        stream = bool(b.get("stream"))
        usage_wanted = bool((b.get("stream_options") or {}).get("include_usage"))
        rid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        t0 = time.time()
        m = E.models.get(mid)
        if m is None:
            return self._json(400, {"error": {"message": f"模型 {mid} 没加载"}})
        ctk = dict(b.get("chat_template_kwargs") or {})
        text = m.tok.apply_chat_template(b.get("messages") or [], add_generation_prompt=True, tokenize=False, **ctk)
        prompt = m.tok.encode(text, add_special_tokens=False)
        in_think = text.rfind("<think>") > text.rfind("</think>")
        rf = b.get("response_format") or {}
        grammar = None
        if rf.get("type") == "json_schema":
            js = rf.get("json_schema") or {}
            grammar = m.grammar(js.get("schema", js) if isinstance(js, dict) else js, not in_think)
        elif rf.get("type") == "json_object":
            grammar = m.grammar(None, not in_think)
        temp = float(b.get("temperature", 1.0) or 0.0)
        top_p = float(b.get("top_p", 1.0) or 1.0)
        if b.get("seed") is not None:
            log(f"chat {rid[-6:]} seed ignored (batched)")
        max_tokens = int(b.get("max_tokens") or b.get("max_completion_tokens") or 2048)
        stops = [s for s in (b.get("stop") or []) if isinstance(s, str) and s] if isinstance(b.get("stop"), list) else \
            ([b["stop"]] if isinstance(b.get("stop"), str) and b["stop"] else [])
        r = Req(rid, mid, prompt, segment(m.tok, b.get("messages") or [], prompt, ctk), E.sampler(temp, top_p),
                [grammar] if grammar else [], max_tokens)
        inflight = sum(len(x.reqs) for x in list(E.models.values()))   # 只记日志，不求准
        E.sched.inbox.put(("chat", r))

        started = {"headers": False}

        def head() -> None:
            if stream and not started["headers"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                started["headers"] = True
                send(chunk({"role": "assistant", "content": ""}))

        def send(obj) -> None:
            data = ("data: " + (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) + "\n\n").encode("utf-8")
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise Cancelled()

        def chunk(delta: dict, finish=None) -> dict:
            return {"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        detok = m.tok.detokenizer
        content, reasoning, n_out, finish, ttft, outcome = [], [], 0, None, None, "ok"

        def emit(piece: str) -> None:
            if not piece:
                return
            (reasoning if in_think else content).append(piece)
            if stream:
                send(chunk({"reasoning_content": piece} if in_think else {"content": piece}))

        try:
            while True:
                try:
                    ev = r.events.get(timeout=0.25)
                except queue.Empty:
                    if self._gone():
                        raise Cancelled()
                    continue
                if ev[0] == "err":
                    _, code, msg = ev
                    if code == "prefill_memory_aborted":
                        outcome = code
                        err = {"message": msg, "type": "server_error", "omlx_code": code}
                        if started["headers"]:
                            send({"error": err})
                        else:
                            self._json(507, {"error": err})
                    else:
                        outcome = "error"
                        if started["headers"]:
                            send({"error": {"message": msg, "type": "server_error"}})
                        else:
                            self._json(code, {"error": {"message": msg}})
                    return
                head()
                if ev[0] == "progress":
                    if self._gone():
                        raise Cancelled()
                    continue
                _, tok, fin = ev
                n_out += 1
                if ttft is None:
                    ttft = time.time() - t0
                if fin == "stop":                   # 停止 token 本身不出字
                    detok.finalize()
                    emit(detok.last_segment)
                    finish = "stop"
                    break
                if tok == m.think_open:
                    in_think = True
                elif tok == m.think_close:
                    in_think = False
                else:
                    detok.add_token(tok)
                    if fin == "length":
                        detok.finalize()
                    emit(detok.last_segment)
                if fin == "length":
                    finish = "length"
                    break
                if stops and not in_think and any("".join(content).endswith(s) for s in stops):
                    r.cancelled = True
                    finish = "stop"
                    break
                if n_out % 8 == 0 and self._gone():
                    raise Cancelled()
            usage = {"prompt_tokens": len(prompt), "completion_tokens": n_out, "total_tokens": len(prompt) + n_out,
                     "prompt_tokens_details": {"cached_tokens": r.cached}}
            if stream:
                head()
                send(chunk({}, finish or "stop"))
                if usage_wanted:
                    send({"id": rid, "object": "chat.completion.chunk", "created": created, "model": mid, "choices": [], "usage": usage})
                send("[DONE]")
            else:
                msg = {"role": "assistant", "content": "".join(content)}
                if reasoning:
                    msg["reasoning_content"] = "".join(reasoning)
                self._json(200, {"id": rid, "object": "chat.completion", "created": created, "model": mid,
                                 "choices": [{"index": 0, "message": msg, "finish_reason": finish or "stop"}], "usage": usage})
        except Cancelled:
            r.cancelled = True
            outcome = "cancelled"
        finally:
            wall = time.time() - t0
            pc = m.pc
            log(f"chat {rid[-6:]} model={mid} prompt={len(prompt)} cached={r.cached} out={n_out} ttft={(ttft or 0):.2f}s wall={wall:.2f}s "
                f"pcache={len(pc) if pc else 0}/{(pc.nbytes if pc else 0) / 1024 ** 2:.0f}MB "
                f"decode={(n_out - 1) / max(wall - (ttft or wall), 1e-3) if n_out > 1 else 0:.1f}tok/s "
                f"inflight={inflight} active={mx.get_active_memory() / GiB:.2f}GB json={'y' if grammar else 'n'} outcome={outcome}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["serve"])
    ap.add_argument("--base-path", required=True)
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--memory-guard-gb", type=float, default=0)
    ap.add_argument("--max-concurrent-requests", type=int, default=8)
    ap.add_argument("--prefix-cache-mb", type=float, default=512)
    ap.add_argument("--prefill-step-size", type=int, default=2048)
    a, rest = ap.parse_known_args()
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("只听本机地址")
    Handler.E = Engine(a)
    Handler.E.sched.start()
    from importlib.metadata import version as _v
    log(f"thin engine 3 (mlx {mx.__version__}, mlx-lm {_v('mlx-lm')}, xgrammar {_v('xgrammar')}) on {a.host}:{a.port}, models in {a.model_dir}, "
        f"ceiling {a.memory_guard_gb} GB, batch ≤{Handler.E.max_seqs}, prefill step {Handler.E.prefill_step}, "
        f"prefix cache {a.prefix_cache_mb:.0f} MB; ignored {rest}")
    srv = http.server.ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
