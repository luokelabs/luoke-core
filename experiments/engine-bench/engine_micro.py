#!/usr/bin/env python3
"""引擎微基准（luoke-core#42）：几个引擎同时起、同时把同一个模型放着，每一项轮流做（每遍换先后，拉丁方），
宿主上别的活对各边差不多。引擎按主控的方式起（luoke_core/engine.py 的参数和环境）。整套邮件评测见 email_eval.py。

    python engine_micro.py --apps <放 <边>.app 的目录> --models <模型目录> --out <目录>
        [--arms omlx,thin2,thin3] [--only 1,2,3,4,5] [--guard 40] [--pause-while 用户:进程模式]

--arms 的每一边是 --apps 下的 <边>.app（omlx 指 oMLX.app）；omlxc 是 oMLX.app 开自己的缓存（只放内存，1 GB）。
薄引擎做成 .app 的办法见 docs/experiments/engine-bench.md。

1 长输入预填充：约 1k / 4k / 16k / 32k token，max_tokens 1，量首字（≈ 预填充时间），每档 3 遍
2 生成速度：约 500 token 的输入，自由写 300 token，4 遍
3 连续批处理：2 / 4 / 8 条同时到（约 700 token 的邮件 + json_schema），每次紧挨着先单发一条当基准；
  批处理效率 = N × 单发用时 ÷ 这一批的总用时（1 = 一条接一条，越接近 N 越好），每档 4 轮
4 打断：16k token 的输入发出 2 秒后断开，马上发一条短的，看它多久出首字，3 遍（快慢看断在预填充块的哪儿，
  要比打断就扫不同的断开时刻）
5 前缀复用：同一段约 3000 token 的系统提示，换不同的短问题连发 4 条，看第 2–4 条首字；
5b 多轮对话：同一段系统提示，带上自己上一轮的回答连问 4 轮，看第 2–4 轮首字
6 （要 --payloads，capture 出来的主控请求体）真实请求一条接一条；一般用 email_eval.py
每条请求开头带一个编号，防止引擎之间、遍之间串缓存（第 5 项除外）。结果里的 cached 是引擎回的
prompt_tokens_details.cached_tokens（没回就是 null）。汇总：python3 analyze_micro.py <out>/bench3.json
"""
import argparse
import http.client
import json
import os
import re
import secrets
import socket
import statistics as st
import subprocess
import tempfile
import threading
import time

BASE = "Qwen3.5-9B-4bit"
SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["reply", "schedule", "read_later", "archive", "unsubscribe", "trash"]},
    "priority": {"type": "string", "enum": ["high", "normal", "low"]},
    "suspicious": {"type": "boolean"}, "due_date": {"type": ["string", "null"]},
    "summary": {"type": "string"}}, "required": ["action", "priority", "suspicious", "due_date", "summary"]}
EMAIL = ("发件人：王老师 <wang@school.example>\n主题：下周三家长会安排\n\n各位家长好：\n下周三（11 月 5 日）下午四点在学校三楼会议室开家长会，"
         "主要说明期中考试的情况和下学期的课外活动安排。请提前十分钟到场签到，如果不能参加，请在周一之前回复这封邮件说明原因。"
         "会上还会介绍新开的科学社团和图书馆的借书规则，有兴趣的家长可以留下来和老师交流。另外，学校食堂从下个月起调整午餐价格，"
         "具体方案附在通知后面，请大家看一下有没有意见。谢谢配合！\n")
SYSTEM = "你是邮件分拣助手。读一封邮件，用 JSON 回答该怎么处理。"
# 同一个 app 的别的起法。omlxc：oMLX 开着自己的缓存，只放内存（DESIGN §5 实测 0 的那种配置：
# settings.json 里 cache.hot_cache_only，热缓存 1 GB 同 16 GB 档），不传 --no-cache
VARIANTS = {"omlxc": {"app": "oMLX", "settings": {"cache": {"enabled": True, "hot_cache_only": True}},
                      "cache_args": ["--hot-cache-max-size", "1GB"]}}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Eng:
    """按主控的方式起（engine.py）：0600 settings.json 里放密钥、同样的 serve 参数、清掉 HF_/OMLX_、HF_HUB_OFFLINE=1。
    HOME 指到临时目录：oMLX 会往 ~/.omlx 写东西，不碰本机账户里原有的。"""

    def __init__(self, name: str, app: str, models: str, guard: float, work: str):
        self.name = name
        cfg = VARIANTS.get(name, {})
        base, mdir, home = (os.path.join(work, x) for x in ("engine", "models", "home"))
        for d in (base, mdir, home):
            os.makedirs(d)
        self.base = base
        os.symlink(os.path.join(models, BASE), os.path.join(mdir, BASE))
        self.key = secrets.token_hex(16)
        fd = os.open(os.path.join(base, "settings.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"auth": {"api_key": self.key}, **cfg.get("settings", {})}, f)
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if not (k.startswith("HF_") or k.startswith("OMLX_"))}
        env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", HF_HUB_OFFLINE="1", HOME=home)
        cmd = [os.path.join(app, "Contents/MacOS/omlx-cli"), "serve", "--base-path", base, "--model-dir", mdir, "--host", "127.0.0.1",
               "--port", str(self.port), *cfg.get("cache_args", ["--no-cache"]), "--no-hf-cache", "--memory-guard-gb", str(guard),
               "--log-level", "info"]
        self.logf = os.path.join(work, "engine.log")
        self.proc = subprocess.Popen(cmd, env=env, stdout=open(self.logf, "a"), stderr=subprocess.STDOUT, cwd="/")
        t0 = time.time()
        while time.time() - t0 < 180:
            try:
                if self.get_models():
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise SystemExit(f"{name} 3 分钟没起来，看 {self.logf}")
        self.mid = next(m for m in self.get_models() if m == BASE or m.endswith("/" + BASE))

    def h(self) -> dict:
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}

    def get_models(self) -> list:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/v1/models", headers=self.h())
        r = c.getresponse()
        return [m["id"] for m in json.loads(r.read())["data"]] if r.status == 200 else []

    def load(self) -> int:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=900)
        c.request("POST", f"/v1/models/{self.mid}/load", headers=self.h())
        r = c.getresponse()
        r.read()
        return r.status

    def chat(self, messages: list, max_tokens: int, schema: bool, cut_after: float = 0) -> dict:
        p = {"model": self.mid, "stream": True, "stream_options": {"include_usage": True}, "temperature": 0, "max_tokens": max_tokens,
             "chat_template_kwargs": {"enable_thinking": False}, "messages": messages}
        if schema:
            p["response_format"] = {"type": "json_schema", "json_schema": {"name": "triage", "schema": SCHEMA}}
        return self.send(p, cut_after)

    def send(self, p: dict, cut_after: float = 0) -> dict:
        """发一条请求体（model 换成这个引擎的 id），流式读完。"""
        p = dict(p, model=self.mid)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1800)
        t0 = time.time()
        c.request("POST", "/v1/chat/completions", body=json.dumps(p).encode(), headers=self.h())
        sock = c.sock
        out = {"ttft": None, "text": "", "usage": None, "error": None}
        def cut():
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass
        if cut_after:
            threading.Timer(cut_after, cut).start()
        try:
            r = c.getresponse()
            if r.status != 200:
                out["error"] = f"{r.status} {r.read()[:200]!r}"
            else:
                while True:
                    line = r.readline()
                    if not line:
                        break
                    line = line.decode("utf-8", "replace").strip()
                    if not line.startswith("data: ") or line[6:] == "[DONE]":
                        continue
                    o = json.loads(line[6:])
                    if o.get("error"):
                        out["error"] = str(o["error"])[:200]
                        break
                    for ch in o.get("choices") or []:
                        piece = (ch.get("delta") or {}).get("content")
                        if piece:
                            if out["ttft"] is None:
                                out["ttft"] = time.time() - t0
                            out["text"] += piece
                    if o.get("usage"):
                        out["usage"] = o["usage"]
        except OSError as e:
            out["error"] = "cut" if cut_after else f"{type(e).__name__}"
        out["total"] = time.time() - t0
        u = out["usage"] or {}
        out["pt"], out["ct"] = u.get("prompt_tokens"), u.get("completion_tokens")
        out["cached"] = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        if out["ttft"] and (out["ct"] or 0) > 1:
            out["decode"] = (out["ct"] - 1) / max(out["total"] - out["ttft"], 1e-3)
        return out

    def footprint(self) -> float:
        o = subprocess.run(["/usr/bin/footprint", "-p", str(self.proc.pid)], capture_output=True, text=True).stdout
        m = re.search(r"Footprint:\s*([\d.]+)\s*([KMG])B", o)
        return round(float(m.group(1)) / {"K": 1048576, "M": 1024, "G": 1}[m.group(2)], 3) if m else -1.0

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def msgs(user: str, system: str = SYSTEM) -> list:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


PAUSE_WHILE = None                      # "用户:进程模式"，--pause-while 给
WAITS: list = []


def wait_release() -> None:
    """那个用户在跑匹配的进程（出版机上的出版）就等，每 30 秒看一次，等了多久记下来。"""
    if not PAUSE_WHILE:
        return
    user, pattern = PAUSE_WHILE.split(":", 1)
    t0 = None
    while subprocess.run(["pgrep", "-u", user, "-f", pattern], capture_output=True).returncode == 0:
        if t0 is None:
            t0 = time.time()
            print(time.strftime("%H:%M:%S"), "release running: waiting", flush=True)
        time.sleep(30)
    if t0 is not None:
        WAITS.append([time.strftime("%H:%M:%S"), round(time.time() - t0)])
        print(time.strftime("%H:%M:%S"), f"release ended after {time.time() - t0:.0f}s of waiting", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", required=True)
    ap.add_argument("--arms", default="omlx,thin2,thin3")
    ap.add_argument("--models", required=True)
    ap.add_argument("--payloads")
    ap.add_argument("--pause-while")
    ap.add_argument("--out", required=True)
    ap.add_argument("--guard", type=float, default=40)
    ap.add_argument("--only", default="1,2,3,4,5")
    a = ap.parse_args()
    only = set(a.only.split(","))
    os.makedirs(a.out, exist_ok=True)
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(os.path.join(a.models, BASE))
    per = len(tok(EMAIL)["input_ids"])
    text = lambda n_tokens: EMAIL * max(1, n_tokens // per)  # noqa: E731
    seq = iter(range(10 ** 6))
    nonce = lambda: f"[编号 {next(seq)}-{secrets.token_hex(3)}]\n"  # noqa: E731
    arms = a.arms.split(",")
    app = lambda n: os.path.join(a.apps, ("oMLX" if n == "omlx" else VARIANTS.get(n, {}).get("app", n)) + ".app")  # noqa: E731
    global PAUSE_WHILE
    PAUSE_WHILE = a.pause_while
    P = [json.loads(l) for l in open(a.payloads, encoding="utf-8")] if a.payloads else []
    wait_release()
    E = {n: Eng(n, app(n), a.models, a.guard, tempfile.mkdtemp(prefix=f"bench3-{n}-")) for n in arms}
    R = {"rows": {}, "release_waits": WAITS}

    def save():
        json.dump(R, open(os.path.join(a.out, "bench3.json"), "w"), ensure_ascii=False, indent=1)

    def order(i: int) -> list:
        wait_release()
        k = i % len(arms)
        return arms[k:] + arms[:k]                                                 # 每遍换一个打头的

    try:
        for n, e in E.items():
            wait_release()
            assert e.load() == 200, n
            e.chat(msgs("你好"), 1, False)
            e.chat(msgs(nonce() + EMAIL), 200, True)                                # 内核、语法都先热一遍
        R["footprint_loaded_gb"] = {n: e.footprint() for n, e in E.items()}
        if "1" in only:
            rows = []
            for target in (1024, 4096, 16384, 32768):
                for i in range(3):
                    row = {"target": target, "i": i}
                    for n in order(i):
                        r = E[n].chat(msgs(nonce() + text(target)), 1, False)
                        row[n] = {"ttft": r["ttft"], "pt": r["pt"], "error": r["error"]}
                    rows.append(row)
                    print("1", json.dumps(row, ensure_ascii=False), flush=True)
            R["rows"]["prefill"] = rows
            R["footprint_after_prefill_gb"] = {n: e.footprint() for n, e in E.items()}
            save()
        if "2" in only:
            rows = []
            for i in range(4):
                row = {"i": i}
                for n in order(i):
                    r = E[n].chat(msgs(nonce() + text(500) + "\n请用三百字左右介绍家长会的意义，不要用列表。"), 300, False)
                    row[n] = {"ttft": r["ttft"], "decode": r.get("decode"), "ct": r["ct"], "error": r["error"]}
                rows.append(row)
                print("2", json.dumps(row, ensure_ascii=False), flush=True)
            R["rows"]["decode"] = rows
            save()
        if "3" in only:
            rows = []
            for conc in (2, 4, 8):
                for i in range(4):
                    row = {"n": conc, "i": i}
                    for n in order(i):
                        ref = E[n].chat(msgs(nonce() + text(700)), 200, True)       # 紧挨着的单发基准
                        res, bar = [None] * conc, threading.Barrier(conc)

                        def one(k):
                            bar.wait()
                            res[k] = E[n].chat(msgs(nonce() + text(700)), 200, True)
                        t0 = time.time()
                        ths = [threading.Thread(target=one, args=(k,)) for k in range(conc)]
                        for t in ths:
                            t.start()
                        for t in ths:
                            t.join()
                        wall = time.time() - t0
                        ok = [r for r in res if not r["error"] and r["ttft"]]
                        row[n] = {"single": ref["total"], "efficiency": conc * ref["total"] / wall if ref["total"] else None,
                                  "wall": wall, "ok": len(ok), "tokens": sum(r["ct"] or 0 for r in ok),
                                  "agg_tok_s": sum(r["ct"] or 0 for r in ok) / wall,
                                  "ttft_med": st.median([r["ttft"] for r in ok]) if ok else None, "ttft_max": max((r["ttft"] for r in ok), default=None),
                                  "total_med": st.median([r["total"] for r in ok]) if ok else None, "total_max": max((r["total"] for r in ok), default=None),
                                  "errors": [r["error"] for r in res if r["error"]][:3]}
                    rows.append(row)
                    print("3", json.dumps(row, ensure_ascii=False), flush=True)
            R["rows"]["batch"] = rows
            R["footprint_after_batch_gb"] = {n: e.footprint() for n, e in E.items()}
            save()
        if "4" in only:
            rows = []
            for i in range(3):
                row = {"i": i}
                for n in order(i):
                    big = E[n].chat(msgs(nonce() + text(16384)), 100, False, cut_after=2.0)
                    t = time.time()
                    nxt = E[n].chat(msgs(nonce() + EMAIL), 1, False)
                    row[n] = {"big_error": big["error"], "big_ttft": big["ttft"], "next_ttft": nxt["ttft"], "next_wall": time.time() - t}
                rows.append(row)
                print("4", json.dumps(row, ensure_ascii=False), flush=True)
            R["rows"]["interrupt"] = rows
            save()
        if "5" in only:
            rows = []
            longsys = SYSTEM + "\n下面是这位用户的背景资料，分拣时参考：\n" + text(3000)
            for i in range(2):
                row = {"i": i}
                for n in order(i):
                    s = nonce() + longsys                                            # 每轮换一段系统提示，轮内 4 条共用
                    rs = [E[n].chat(msgs(f"第 {k} 封：\n" + EMAIL, s), 1, False) for k in range(4)]
                    row[n] = {"ttft": [r["ttft"] for r in rs], "cached": [r["cached"] for r in rs]}
                rows.append(row)
                print("5", json.dumps(row, ensure_ascii=False), flush=True)
            R["rows"]["prefix"] = rows
            save()
            rows = []
            asks = ["这位用户最关心哪三件事？一句话。", "其中哪一件最急？一句话。", "替他写一句提醒，二十字以内。", "再换个更客气的说法。"]
            for i in range(2):
                row = {"i": i}
                for n in order(i):
                    conv = [{"role": "system", "content": nonce() + longsys}]
                    turns = []
                    for q in asks:
                        conv.append({"role": "user", "content": q})
                        r = E[n].chat(conv, 60, False)
                        conv.append({"role": "assistant", "content": r["text"]})
                        turns.append({"ttft": r["ttft"], "pt": r["pt"], "cached": r["cached"], "error": r["error"]})
                    row[n] = turns
                rows.append(row)
                print("5b", json.dumps(row, ensure_ascii=False), flush=True)
            R["rows"]["multiturn"] = rows
            save()
        if "6" in only and P:
            rows = []
            for i, x in enumerate(P):
                row = {"i": i, "tag": x["tag"], "chars": x["chars"]}
                for n in order(i):
                    r = E[n].send(x["payload"])
                    row[n] = {k: r.get(k) for k in ("ttft", "total", "pt", "ct", "cached", "error", "text")}
                rows.append(row)
                print("6", i, x["tag"], {n: (round(row[n]["ttft"] or 0, 2), round(row[n]["total"], 2), row[n]["cached"]) for n in arms}, flush=True)
            R["rows"]["triage"] = rows
            R["footprint_after_triage_gb"] = {n: e.footprint() for n, e in E.items()}
            save()
        R["done"] = True
    finally:
        for e in E.values():
            e.stop()
        R["files_left"] = {}                                                       # 引擎在 base-path 下留了什么（缓存会不会落盘）
        for n, e in E.items():
            R["files_left"][n] = [[os.path.relpath(os.path.join(d, f), e.base), os.path.getsize(os.path.join(d, f))]
                                  for d, _, fs in os.walk(e.base) for f in fs][:50]
        save()
        print("DONE", flush=True)


if __name__ == "__main__":
    main()
