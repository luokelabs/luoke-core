"""打分工作进程（V1 §2、§5）：在引擎包的 Python 里跑，用 mlx-lm 以库方式加载专用模型，按行协议打分。

    in:  {"ctx": "前文", "cands": ["候选一", ...]}
    out: {"lp": [log 概率之和, ...], "ms": 耗时, "hit": 前缀缓存是否命中}
    in:  {"op": "ping"}            out: {"ok": true}
    in:  {"op": "predict", "ctx": "前文", "k": 8, "n": 6}   out: {"cont": [[text, 总 log 概率, 首 token 文字, 它的 log 概率], ...], "ms", "hit"}
    in:  {"op": "continue", "ctx": "前文", "k": 8, "max_tokens": 6, "stop": ["，", "。"]}
         out: {"branches": [{"tokens": [{"text", "logprob"}, ...], "finish": "stop"|"length"|"eos"}, ...], "ms", "hit"}（通用续写，不截不挑）

按配件保留最近几条前文的 KV 快照（条数由 --snapshots 给，是任务参数），新请求的前文以其中一条为前缀时只算增量。
快照只在内存里，进程退出即消失。父进程消失（stdin 关闭）就退出，不监听任何端口。

打分算法来自 luoke-ime 的 engines/personal/scorer.py（同一个实验室，同一份口径）：所有候选从一个共享的 token
边界起、一次 padded batch 取分；Qwen3.5 的 Gated DeltaNet 状态不能回退，但能沿 batch 轴复制。
"""
import argparse
import json
import sys
import threading
import time
from collections import OrderedDict

import mlx.core as mx
from mlx_lm import load
from mlx_lm.models.cache import ArraysCache, KVCache, make_prompt_cache

try:
    from luoke_core.worker.textops import hit_stop, token_texts
except ImportError:      # 工作进程跑在引擎包的 Python 里，luoke_core 不在它的路径上：按文件名读旁边的 textops.py
    import importlib.util as _ilu
    import os as _os
    _spec = _ilu.spec_from_file_location("luoke_textops", _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "textops.py"))
    _textops = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_textops)
    hit_stop, token_texts = _textops.hit_stop, _textops.token_texts

PREFIX = "\n"


def tokenize(tok, ctx, cands):
    ctx = PREFIX + ctx
    ctx_ids = tok.encode(ctx)
    seqs, shared = [], []
    for c in cands:
        full = tok.encode(ctx + c)
        k = 0
        while k < min(len(ctx_ids), len(full)) and ctx_ids[k] == full[k]:
            k += 1
        seqs.append(full)
        shared.append(k)
    return seqs, max(min(shared), 1)


class PrefixStore:
    def __init__(self, model, size=16):
        self.model, self.size, self.store = model, size, OrderedDict()

    @staticmethod
    def _snap(cache):
        out = []
        for c in cache:
            if isinstance(c, KVCache):
                out.append(("kv", c.keys[..., :c.offset, :], c.values[..., :c.offset, :], c.offset))
            else:
                out.append(("arr", list(c.cache)))
        return out

    def _restore(self, snap):
        cache = make_prompt_cache(self.model)
        for c, s in zip(cache, snap):
            if s[0] == "kv":
                c.keys, c.values, c.offset = s[1], s[2], s[3]
            else:
                c.cache = list(s[1])
        return cache

    def prefix(self, tokens):
        """(定位到 tokens 之后的 cache, 下一个 token 的 log 概率, 是否命中了快照)。"""
        key = tuple(tokens)
        best = max((k for k in self.store if len(k) <= len(key) and key[:len(k)] == k), key=len, default=None)
        hit = best is not None
        if best is None:
            cache, done, last = make_prompt_cache(self.model), 0, None
        else:
            self.store.move_to_end(best)
            cache, done, last = self._restore(self.store[best][0]), len(best), self.store[best][1]
        if done < len(key):
            logits = self.model(mx.array([list(key[done:])]), cache=cache)[:, -1, :].astype(mx.float32)
            last = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
            mx.eval(last)
            self.store[key] = (self._snap(cache), last)
            while len(self.store) > self.size:
                self.store.popitem(last=False)
        return cache, last, hit


def score(model, tok, ctx, cands, store):
    seqs, k0 = tokenize(tok, ctx, cands)
    cache, first, hit = store.prefix(seqs[0][:k0])
    n = len(seqs)
    for c in cache:
        if isinstance(c, KVCache):
            c.keys, c.values = mx.repeat(c.keys, n, axis=0), mx.repeat(c.values, n, axis=0)
        elif isinstance(c, ArraysCache):
            c.cache = [None if a is None else mx.repeat(a, n, axis=0) for a in c.cache]
    rest = [s[k0:] for s in seqs]
    width = max(len(r) for r in rest)
    if width == 0:
        return [float("-inf")] * n, hit
    pad = tok.pad_token_id if tok.pad_token_id is not None else 0
    tgt = mx.array([r + [pad] * (width - len(r)) if r else [pad] * width for r in rest])
    total = first[0][tgt[:, 0]]
    if width > 1:
        logits = model(tgt[:, :-1], cache=cache).astype(mx.float32)
        picked = mx.take_along_axis(logits, tgt[:, 1:, None], axis=-1)[..., 0] - mx.logsumexp(logits, axis=-1)
        mask = mx.array([[float(i < len(r) - 1) for i in range(width - 1)] for r in rest])
        total = total + (picked * mask).sum(axis=1)
    out = total.tolist()
    return [x if r else float("-inf") for x, r in zip(out, rest)], hit


STOP = set("，。！？、；：,.!?;:\n\r\t 　“”‘’\"'（）()《》【】[]")


def continuations(model, tok, ctx, store, k=8, steps=6, chars=6):
    """联想（算法来自 luoke-ime 的 scorer.py）：前文后面最可能的 k 个开头，各贪心续到标点、steps 个 token 或 chars 个字。
    返回 [(text, 总 log 概率, 第一个 token 的文字, 它的 log 概率), ...]，最好的在前；开头就是标点的、空的丢掉。"""
    ids = tok.encode(PREFIX + ctx)
    cache, first, hit = store.prefix(ids)
    top = mx.argsort(-first[0])[:k]
    lp0 = first[0][top]
    mx.eval(top, lp0)
    for c in cache:
        if isinstance(c, KVCache):
            c.keys, c.values = mx.repeat(c.keys, k, axis=0), mx.repeat(c.values, k, axis=0)
        elif isinstance(c, ArraysCache):
            c.cache = [None if a is None else mx.repeat(a, k, axis=0) for a in c.cache]
    seqs = [[t] for t in top.tolist()]
    totals = lp0.tolist()
    heads = list(zip([tok.decode([t]).replace("\ufffd", "") for t in top.tolist()], lp0.tolist()))
    alive = [True] * k

    def text_of(seq):
        t = tok.decode(seq).replace("\ufffd", "")
        for i, ch in enumerate(t):
            if ch in STOP:
                return t[:i], True
        if len(t) > chars:
            # 超过字数上限：退回上一个词元的边界，不在词元中间硬切（"结论和方法建" → "结论和方法"；luoke-ime 6235e3f 的做法，2026-10-16）
            return (tok.decode(seq[:-1]).replace("\ufffd", "") if len(seq) > 1 else t[:chars]), True
        return t, len(t) >= chars

    for i in range(k):
        if text_of(seqs[i])[1]:
            alive[i] = False
    toks = top[:, None]
    for _ in range(steps - 1):
        if not any(alive):
            break
        logits = model(toks, cache=cache)[:, -1, :].astype(mx.float32)
        lp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
        nxt = mx.argmax(lp, axis=-1)
        step = mx.take_along_axis(lp, nxt[:, None], axis=-1)[:, 0]
        mx.eval(nxt, step)
        for i, (t, l) in enumerate(zip(nxt.tolist(), step.tolist())):
            if alive[i]:
                seqs[i].append(t)
                totals[i] += l
                if text_of(seqs[i])[1]:
                    alive[i] = False
        toks = nxt[:, None]
    out, seen = [], set()
    for seq, total, (head, head_lp) in sorted(zip(seqs, totals, heads), key=lambda x: -x[1]):
        text = text_of(seq)[0]
        if text and text not in seen:
            seen.add(text)
            out.append((text, total, head, head_lp))
    return out, hit


def continue_branches(model, tok, ctx, store, k=8, max_tokens=6, stop=None):
    """通用续写（V1 §2 /v1/continue，2026-10-16 定：怎么截、挑哪几个、什么算有把握归调用方）：前文后面最可能的 k 个开头
    （第一个 token 取概率最高的 k 个），各自贪心续到 max_tokens 个 token，或解码文字里出现了调用方给的停止串，或模型结束。
    返回 ([{"tokens": [{"text", "logprob"}, ...], "finish": "stop"|"length"|"eos"}, ...] 按第一个 token 的 log 概率从高到低, cache_hit)。
    不丢、不并、不截任何一支；停止串所在的那个 token 照样交回。"""
    stop = [s for s in (stop or []) if s]
    eos = getattr(tok, "eos_token_id", None)
    ids = tok.encode(PREFIX + ctx)
    cache, first, hit = store.prefix(ids)
    top = mx.argsort(-first[0])[:k]
    lp0 = first[0][top]
    mx.eval(top, lp0)
    for c in cache:
        if isinstance(c, KVCache):
            c.keys, c.values = mx.repeat(c.keys, k, axis=0), mx.repeat(c.values, k, axis=0)
        elif isinstance(c, ArraysCache):
            c.cache = [None if a is None else mx.repeat(a, k, axis=0) for a in c.cache]
    seqs = [[t] for t in top.tolist()]
    lps = [[l] for l in lp0.tolist()]
    finish = [None] * k

    def settle(i):
        if eos is not None and seqs[i][-1] == eos:
            finish[i] = "eos"
        elif stop and hit_stop(tok.decode(seqs[i]).replace("\ufffd", ""), stop):
            finish[i] = "stop"
        elif len(seqs[i]) >= max_tokens:
            finish[i] = "length"

    for i in range(k):
        settle(i)
    toks = top[:, None]
    for _ in range(max_tokens - 1):
        if all(finish):
            break
        logits = model(toks, cache=cache)[:, -1, :].astype(mx.float32)
        lp = logits - mx.logsumexp(logits, axis=-1, keepdims=True)
        nxt = mx.argmax(lp, axis=-1)
        step = mx.take_along_axis(lp, nxt[:, None], axis=-1)[:, 0]
        mx.eval(nxt, step)
        for i, (t, l) in enumerate(zip(nxt.tolist(), step.tolist())):
            if finish[i] is None:
                seqs[i].append(t)
                lps[i].append(l)
                settle(i)
        toks = nxt[:, None]
    branches = []
    for seq, lp_list, fin in zip(seqs, lps, finish):
        texts = token_texts(tok.decode, seq)
        branches.append({"tokens": [{"text": tx, "logprob": lv} for tx, lv in zip(texts, lp_list)], "finish": fin or "length"})
    return branches, hit


IDLE_CLEAR_SECS = 30     # 这么久没有请求就把 MLX 的缓冲还给系统


def mem_mb() -> dict:
    return {"active_mb": round(mx.get_active_memory() / 2**20), "cache_mb": round(mx.get_cache_memory() / 2**20),
            "peak_mb": round(mx.get_peak_memory() / 2**20)}


def drop_cache_when_idle(lock: threading.Lock, last: list) -> None:
    """后台线程：IDLE_CLEAR_SECS 没有请求就 mx.clear_cache()（加锁，不和打分同时）。"""
    while True:
        time.sleep(IDLE_CLEAR_SECS / 3)
        with lock:
            if last[0] is not None and time.monotonic() - last[0] > IDLE_CLEAR_SECS:
                mx.clear_cache()
                last[0] = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir")
    ap.add_argument("--snapshots", type=int, default=16)
    ap.add_argument("--cache-mb", type=int, default=1024,
                    help="MLX 缓冲上限（MB）。默认上限大到不限（本机约 130 GB），而每次打分的 logits 形状都不同，缓冲只增不减："
                         "开发机上 20 小时涨到 8 GB（2026-10-14，luoke-ime 发现）；1 GB 时连续打分约 2.5 GB、慢 1–2 ms")
    a = ap.parse_args()
    mx.set_cache_limit(max(64, a.cache_mb) * 2**20)
    model, tok = load(a.model_dir)
    store = PrefixStore(model, size=max(1, a.snapshots))
    score(model, tok, "预热", ["一下", "已下"], store)
    print(json.dumps({"ready": True, "snapshots": a.snapshots, "cache_limit_mb": a.cache_mb, **mem_mb()}), flush=True)
    lock, last = threading.Lock(), [time.monotonic()]
    threading.Thread(target=drop_cache_when_idle, args=(lock, last), daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            print(json.dumps({"error": "bad json"}), flush=True)
            continue
        if req.get("op") == "ping":
            print(json.dumps({"ok": True, **mem_mb()}), flush=True)
            continue
        with lock:
            last[0] = time.monotonic()
            handle(req, model, tok, store)


def handle(req: dict, model, tok, store) -> None:
    t = time.perf_counter()
    if req.get("op") == "continue":
        try:
            br, hit = continue_branches(model, tok, req.get("ctx", ""), store, k=int(req.get("k", 8)),
                                        max_tokens=int(req.get("max_tokens", 6)), stop=req.get("stop") or [])
            print(json.dumps({"branches": br, "ms": round((time.perf_counter() - t) * 1000, 2), "hit": hit}, ensure_ascii=False), flush=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {e}"[:200]}), flush=True)
        return
    if req.get("op") == "predict":
        try:
            cont, hit = continuations(model, tok, req.get("ctx", ""), store, k=int(req.get("k", 8)), steps=int(req.get("n", 6)))
            print(json.dumps({"cont": cont, "ms": round((time.perf_counter() - t) * 1000, 2), "hit": hit}, ensure_ascii=False), flush=True)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {e}"[:200]}), flush=True)
        return
    try:
        cands = req.get("cands") or []
        lp, hit = score(model, tok, req.get("ctx", ""), cands, store) if cands else ([], False)
        print(json.dumps({"lp": lp, "ms": round((time.perf_counter() - t) * 1000, 2), "hit": hit}), flush=True)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"[:200]}), flush=True)


if __name__ == "__main__":
    main()
