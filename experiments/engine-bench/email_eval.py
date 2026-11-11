#!/usr/bin/env python3
"""邮件分拣评测直接对几个推理引擎跑，比引擎（luoke-core#42）。题、分拣、重试、打分和出版的 eval 同一份代码。

- 请求体：luoke-core 的 build_engine_payload，按配方这一档 `luoke-mail.<任务>` 的参数生成，和主控交给引擎的一样
  （max_tokens、temperature、top_k、思考开关、特殊 token 中和、max_chars 和 sender_verdict 这些限制）。
- 分拣和打分：luoke-mail 自己的 triage.analyze（带重试、日期和抄送的后处理）和 bench/run_bench.py 的 score()。
  所以分数和出版的 eval 同口径，只是不经主控：没有派发、功耗间歇、冷加载这些主控的事。
- 另外量：每封的用时和首字、接上缓存的 token、引擎内存；几边的判断字段逐封比。

    python3 email_eval.py --core <luoke-core> --mail <luoke-mail> --models <模型目录> --tier 16 --out <目录> \\
        --arm omlx=/Applications/oMLX.app --arm "thin=<python> <thin_engine3.py>" \\
        [--arm omlxc=/Applications/oMLX.app --hot-cache omlxc=1GB] [--concurrency 1,4] \\
        [--pause-while 'luoke-release:tools/(release|internal_build)\\.py']

--arm 名字=.app（起它的 Contents/MacOS/omlx-cli）或 名字=一条命令（照收 omlx-cli 的参数，比如薄引擎）。
引擎按主控的方式起（luoke_core/engine.py）：0600 的 settings.json 里放密钥，`serve --base-path --model-dir --host 127.0.0.1
--port --no-cache --no-hf-cache --memory-guard-gb <这一档的 guard_gb>`，清掉 HF_/OMLX_ 环境变量，HOME 指到临时目录。
--hot-cache 名字=大小：那一边开 oMLX 自己的缓存，只放内存（settings.json 里 cache.hot_cache_only，不传 --no-cache）。

几边同时起、模型都放着；每封邮件（并发时每一批）各边轮流做、每次换先后，背景负载对各边差不多。
--concurrency 1 是一封接一封（和出版的 eval 一样）；N 是 N 封同时到（看连续批处理），每种都整套跑一遍。
--pause-while 用户:模式：每封（每批）开始前看那个用户有没有在跑匹配的进程，有就等（出版机上给出版让路）。

结果在 --out：每边每种并发一份 <边>-c<N>.json（格式同 run_bench.py 的结果，多了 calls），summary.json、summary.md。
"""
from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import platform
import re
import secrets
import shlex
import socket
import statistics as st
import subprocess
import sys
import tempfile
import threading
import time

DECISION = ("action", "category", "priority", "due_date", "suspicious")
SCORE_KEYS = ("basic", "hard", "action", "safety", "false_alarm", "dates", "false_due", "priority", "json_first_try", "failed")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Engine:
    """一个引擎实例，照主控的方式起（luoke_core/engine.py）。"""

    def __init__(self, name: str, spec: str, models: str, model: str, guard: float, hot_cache: str | None, work: str):
        self.name, self.spec, self.model = name, spec, model
        self.base, mdir, home = (os.path.join(work, x) for x in ("engine", "models", "home"))
        for d in (self.base, mdir, home):
            os.makedirs(d)
        os.symlink(os.path.join(models, model), os.path.join(mdir, model))
        self.key = secrets.token_hex(16)
        settings = {"auth": {"api_key": self.key}}
        cache_args = ["--no-cache"]
        if hot_cache:
            settings["cache"] = {"enabled": True, "hot_cache_only": True}
            cache_args = ["--hot-cache-max-size", hot_cache]
        fd = os.open(os.path.join(self.base, "settings.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f)
        self.port = free_port()
        env = {k: v for k, v in os.environ.items() if not (k.startswith("HF_") or k.startswith("OMLX_"))}
        env.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", HF_HUB_OFFLINE="1", HOME=home)
        exe = [os.path.join(spec, "Contents/MacOS/omlx-cli")] if spec.rstrip("/").endswith(".app") else shlex.split(os.path.expanduser(spec))
        cmd = exe + ["serve", "--base-path", self.base, "--model-dir", mdir, "--host", "127.0.0.1", "--port", str(self.port),
                     *cache_args, "--no-hf-cache", "--memory-guard-gb", str(guard), "--log-level", "info"]
        self.log = os.path.join(work, "engine.log")
        self.proc = subprocess.Popen(cmd, env=env, stdout=open(self.log, "a"), stderr=subprocess.STDOUT, cwd="/")
        t0 = time.time()
        while time.time() - t0 < 180:
            if self.proc.poll() is not None:
                raise SystemExit(f"{name} 退出了（{self.proc.returncode}），看 {self.log}")
            try:
                ids = self.models()
                if any(i == model or i.endswith("/" + model) for i in ids):
                    self.mid = next(i for i in ids if i == model or i.endswith("/" + model))
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise SystemExit(f"{name} 3 分钟没起来，看 {self.log}")
        self.start_secs = round(time.time() - t0, 2)

    def h(self) -> dict:
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}

    def models(self) -> list:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/v1/models", headers=self.h())
        r = c.getresponse()
        return [m["id"] for m in json.loads(r.read())["data"]] if r.status == 200 else []

    def load(self) -> float:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=900)
        t0 = time.time()
        c.request("POST", f"/v1/models/{self.mid}/load", headers=self.h())
        r = c.getresponse()
        body = r.read()
        if r.status != 200:
            raise SystemExit(f"{self.name} 加载 {self.mid} 失败：{r.status} {body[:200]!r}")
        return round(time.time() - t0, 2)

    def stream(self, payload: dict, timeout: float) -> dict:
        """发一条请求体（流式，同主控），读完。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        t0 = time.time()
        c.request("POST", "/v1/chat/completions", body=json.dumps(payload).encode(), headers=self.h())
        r = c.getresponse()
        out = {"ttft": None, "text": "", "usage": None, "error": None}
        if r.status != 200:
            out["error"] = f"HTTP {r.status} {r.read()[:300]!r}"
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
                    out["error"] = str(o["error"])[:300]
                    break
                for ch in o.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        if out["ttft"] is None:
                            out["ttft"] = time.time() - t0
                        out["text"] += piece
                if o.get("usage"):
                    out["usage"] = o["usage"]
        out["total"] = time.time() - t0
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


def make_link_class(engines, build_engine_payload, recipe: dict, tier: str, special: list):
    """luoke-mail 的 Link：limits 按配方这一档答，chat 把请求体做成主控交给引擎的样子、直接发给引擎。"""
    def params(task: str) -> tuple:
        t = recipe["tasks"]["luoke-mail." + task]
        p = dict(t["params"].get("*", {}))
        p.update(t["params"].get(tier, {}))
        return p, dict(t.get("base_params", {}))

    class CoreLink(engines.Link):
        def __init__(self, eng: Engine):
            self.eng, self.name = eng, eng.name
            self.calls: list = []
            self.lock = threading.Lock()

        def limits(self, task: str) -> dict:
            p, _ = params(task)
            return {"max_chars": p.get("max_chars"), "batch": p.get("batch"), "sender_verdict": p.get("sender_verdict")}

        def chat(self, task, messages, schema, *, items=1, max_tokens=700, timeout=300):
            p, bp = params(task)
            body = {"messages": messages, "stream": True, "luoke_items": max(1, int(items)),        # 同 luoke_mail/hub.py
                    "response_format": {"type": "json_schema", "json_schema": {"name": task.replace("-", "_"), "schema": schema}}}
            r = self.eng.stream(build_engine_payload(body, p, bp, self.eng.mid, special), timeout)
            u = r["usage"] or {}
            with self.lock:
                self.calls.append({"task": task, "ttft": r["ttft"], "total": round(r["total"], 3), "pt": u.get("prompt_tokens"),
                                   "ct": u.get("completion_tokens"), "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                                   "error": r["error"]})
            if r["error"]:
                raise engines.EngineError(r["error"])
            return r["text"], u, r["total"]

    return CoreLink


def pause(spec: str | None, waits: list) -> None:
    if not spec:
        return
    user, pattern = spec.split(":", 1)
    t0 = None
    while subprocess.run(["pgrep", "-u", user, "-f", pattern], capture_output=True).returncode == 0:
        if t0 is None:
            t0 = time.time()
            print(time.strftime("%H:%M:%S"), f"{user} 在跑 {pattern}：等", flush=True)
        time.sleep(30)
    if t0 is not None:
        waits.append([time.strftime("%Y-%m-%d %H:%M:%S"), round(time.time() - t0)])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--core", required=True, help="luoke-core 的源码（读配方、build_engine_payload）")
    ap.add_argument("--mail", required=True, help="luoke-mail 的源码（题、分拣、打分）")
    ap.add_argument("--models", required=True, help="模型目录（里面是各模型的目录，名字同配方的 dir）")
    ap.add_argument("--tier", default="16")
    ap.add_argument("--arm", action="append", required=True, metavar="名字=APP或命令")
    ap.add_argument("--hot-cache", action="append", default=[], metavar="名字=大小")
    ap.add_argument("--concurrency", default="1")
    ap.add_argument("--pause-while", default=None, metavar="用户:进程模式")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    a.core, a.mail, a.models = (os.path.abspath(os.path.expanduser(x)) for x in (a.core, a.mail, a.models))

    sys.path.insert(0, a.core)
    from luoke_core.proxy import build_engine_payload
    spec = importlib.util.spec_from_file_location("run_bench", os.path.join(a.mail, "bench", "run_bench.py"))
    rb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rb)                       # 它自己把 luoke-mail 放进 sys.path，带进 engines、samples、triage
    recipe = json.load(open(os.path.join(a.core, "recipes", "dev.json"), encoding="utf-8"))
    base = recipe["assignment"][a.tier]["base"]
    meta = recipe["models"][base]
    Link = make_link_class(rb.engines, build_engine_payload, recipe, a.tier, meta.get("special_tokens", []))
    items = rb.samples.build()
    arms = [x.split("=", 1) for x in a.arm]
    hot = dict(x.split("=", 1) for x in a.hot_cache)
    concs = [int(x) for x in a.concurrency.split(",")]
    waits: list = []

    pause(a.pause_while, waits)
    E = {}
    try:
        for n, s in arms:
            E[n] = Engine(n, s, a.models, meta["dir"], recipe["tiers"][a.tier]["guard_gb"], hot.get(n), tempfile.mkdtemp(prefix=f"email-eval-{n}-"))
        info = {n: {"spec": e.spec, "hot_cache": hot.get(n), "start_secs": e.start_secs, "load_secs": e.load()} for n, e in E.items()}
        for n, e in E.items():                        # 同出版的 --warmup：先空跑一封（内核、语法都热了），不计
            try:
                rb.triage.analyze(items[0]["mail"], Link(e), today=rb.samples.TODAY, user_address=rb.samples.ME)
            except Exception as ex:  # noqa: BLE001
                print(n, "warmup failed:", ex, flush=True)
            info[n]["footprint_loaded_gb"] = e.footprint()
        summary = {"tier": a.tier, "model": meta["dir"], "core": a.core, "mail": a.mail, "machine": rb.engines.machine_summary(),
                   "date": time.strftime("%Y-%m-%d %H:%M"), "arms": info, "runs": {}, "pauses": waits}
        names = [n for n, _ in arms]
        first: dict = {}                              # 第一种并发下各边的结果：别的并发和它比，看批处理改没改答案
        for conc in concs:
            links = {n: Link(e) for n, e in E.items()}
            res = {n: [None] * len(items) for n in names}
            wall = {n: 0.0 for n in names}
            chunks = [list(range(i, min(i + conc, len(items)))) for i in range(0, len(items), conc)]
            for ci, chunk in enumerate(chunks):
                pause(a.pause_while, waits)
                k = ci % len(names)
                for n in names[k:] + names[:k]:
                    def one(i, n=n):
                        r = {"tag": items[i]["tag"]}
                        try:
                            r["triage"] = rb.triage.analyze(items[i]["mail"], links[n], today=rb.samples.TODAY, user_address=rb.samples.ME)
                        except Exception as ex:  # noqa: BLE001
                            r["error"] = str(ex)[:300]
                        res[n][i] = r
                    t0 = time.time()
                    ths = [threading.Thread(target=one, args=(i,)) for i in chunk]
                    for t in ths:
                        t.start()
                    for t in ths:
                        t.join()
                    wall[n] += time.time() - t0
                print(time.strftime("%H:%M:%S"), f"c{conc} {chunk[-1] + 1}/{len(items)}",
                      {n: [(res[n][i].get("triage") or {}).get("action", "ERR") for i in chunk] for n in names}, flush=True)
            run = {}
            for n in names:
                s = rb.score(items, res[n])
                s["wall_seconds"] = round(wall[n], 1)
                calls = links[n].calls
                tt = [c["ttft"] for c in calls if c["ttft"]]
                cached = [c["cached"] for c in calls if c["cached"]]
                timing = {"emails_per_min": round(len(items) / wall[n] * 60, 2), "ttft_median": round(st.median(tt), 3) if tt else None,
                          "request_median": round(st.median([c["total"] for c in calls]), 3), "requests": len(calls),
                          "cached_requests": len(cached), "cached_tokens_median": st.median(cached) if cached else 0,
                          "footprint_after_gb": E[n].footprint()}
                json.dump({"label": f"{n}-c{conc}", "model": meta["dir"], "engine": E[n].spec, "tier": a.tier, "concurrency": conc,
                           "machine": summary["machine"], "python": platform.python_version(), "date": summary["date"],
                           "score": s, "timing": timing, "results": res[n], "calls": calls},
                          open(os.path.join(a.out, f"{n}-c{conc}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                run[n] = {"score": {k: s[k] for k in SCORE_KEYS}, "n": s["n"], "wall_seconds": s["wall_seconds"],
                          "avg_seconds": s["avg_seconds"], "timing": timing,
                          f"same_decisions_as_{names[0]}": same_decisions(res[n], res[names[0]])}
                if conc != concs[0]:
                    run[n][f"same_decisions_as_c{concs[0]}"] = same_decisions(res[n], first[n])
                    run[n][f"changed_vs_c{concs[0]}"] = changed(res[n], first[n])
            if conc == concs[0]:
                first = dict(res)
            summary["runs"][f"c{conc}"] = run
            json.dump(summary, open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        write_md(summary, items, os.path.join(a.out, "summary.md"))
        print(open(os.path.join(a.out, "summary.md"), encoding="utf-8").read(), flush=True)
    finally:
        for e in E.values():
            e.stop()


def same_decisions(a: list, b: list) -> int:
    """判断字段（DECISION）全一样的封数。"""
    return sum(1 for x, y in zip(a, b) if x.get("triage") and y.get("triage") and all(x["triage"].get(f) == y["triage"].get(f) for f in DECISION))


def changed(a: list, b: list) -> list:
    """判断字段不一样的那几封：[标签, {字段: [b 的, a 的]}]。"""
    return [[x["tag"], {f: [y["triage"].get(f), x["triage"].get(f)] for f in DECISION if x["triage"].get(f) != y["triage"].get(f)}]
            for x, y in zip(a, b) if x.get("triage") and y.get("triage") and any(x["triage"].get(f) != y["triage"].get(f) for f in DECISION)]


def write_md(summary: dict, items: list, path: str) -> None:
    names = list(summary["arms"])
    lines = [f"# 邮件分拣评测：{summary['tier']} 档，{summary['model']}，{len(items)} 封（{summary['date']}）", "",
             f"机器：{summary['machine']}", ""]
    for key, run in summary["runs"].items():
        lines += [f"## 并发 {key[1:]}", "", "| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        n0 = run[names[0]]["n"]
        for k in SCORE_KEYS:
            lines.append(f"| {k} | " + " | ".join(str(run[n]["score"][k]) for n in names) + " |")
        rows = [("每封用时（中位，含重试）", lambda r: f"{r['timing']['request_median']} s"),
                ("首字（中位）", lambda r: f"{r['timing']['ttft_median']} s"),
                ("每分钟封数", lambda r: str(r["timing"]["emails_per_min"])),
                ("接上缓存的请求 / 中位 token", lambda r: f"{r['timing']['cached_requests']} / {r['timing']['cached_tokens_median']}"),
                ("引擎内存（跑完）", lambda r: f"{r['timing']['footprint_after_gb']} GB"),
                (f"判断字段和 {names[0]} 一样", lambda r: f"{r[f'same_decisions_as_{names[0]}']}/{n0}")]
        base = next((k[len("same_decisions_as_"):] for k in run[names[0]] if re.fullmatch(r"same_decisions_as_c\d+", k)), None)
        if base:
            rows.append((f"判断字段和自己 {base} 时一样", lambda r: f"{r[f'same_decisions_as_{base}']}/{n0}"))
        for label, f in rows:
            lines.append(f"| {label} | " + " | ".join(f(run[n]) for n in names) + " |")
        lines.append("")
        for n in names:
            for tag, d in (run[n].get(f"changed_vs_{base}") or []) if base else []:
                lines.append(f"- {n}：{tag} " + "，".join(f"{k} {v[0]} → {v[1]}" for k, v in d.items()))
        lines.append("")
    open(path, "w", encoding="utf-8").write("\n".join(lines))


if __name__ == "__main__":
    main()
