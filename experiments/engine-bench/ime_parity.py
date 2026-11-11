#!/usr/bin/env python3
"""输入法的两个模型任务（luoke-ime.rerank、luoke-ime.predict）换运行环境后是否一样（luoke-core#42 第 7 条）。

这两个任务不走引擎服务：主控用引擎包里的 Python 和 site-packages 起 worker/score_worker.py，按行协议打分、联想。
换掉 oMLX，worker 就得跑在薄引擎的运行环境上（mlx-lm 0.32.2，oMLX 0.7.2 带的是 0.31.4.dev132）。这里把同一个
score_worker.py 用两套环境各起一份（参数和环境变量照 luoke_core/scorer.py），同样的请求交替发给两边，比：
- 打分：每个候选的 log 概率差多少、首选是否一样、整个排序是否一样
- 联想：给出的续写和首个是否一样
- 用时：worker 自己报的 ms（中位、p90），前缀快照命中率

输入法真正的评测（engines/xiaohe/bench.py、engines/personal/eval_predict.py）读负责人的私人语料，在出版账户里，这里不碰；
这里的文字取自 luoke-mail 的合成样例邮件（samples.py），候选是「真的下两个字 + 别处的两字词」，只为比两套环境，不代表准确率。

    python3 ime_parity.py --core <luoke-core 源码> --mail <luoke-mail 源码> --model <Qwen3.5-0.8B-Base-4bit 目录>
        --runtime omlx=<oMLX.app 里的 python3.11>:<framework-mlx-base 的 site-packages>
        --runtime thin=<thin-venv/bin/python>:<thin-venv 的 site-packages> --out <json>
"""
import argparse
import json
import os
import random
import re
import statistics as st
import subprocess
import sys
import time


class Worker:
    def __init__(self, name: str, python: str, site: str, core: str, model: str, snapshots: int):
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/"), "PYTHONPATH": site,
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1"}       # 同 scorer.py
        argv = [python, "-B", os.path.join(core, "luoke_core/worker/score_worker.py"), model, "--snapshots", str(snapshots)]
        self.name = name
        self.proc = subprocess.Popen(argv, env=env, cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.ready = json.loads(self.proc.stdout.readline())
        probe = subprocess.run([python, "-c", "import importlib.metadata as m, mlx_lm;"
                                "print(m.version('mlx'), m.version('mlx-lm'), mlx_lm.__file__)"],
                               env=env, capture_output=True, text=True)
        self.versions = probe.stdout.strip()

    def ask(self, req: dict) -> dict:
        t0 = time.time()
        self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        out = json.loads(self.proc.stdout.readline())
        out["wall_ms"] = (time.time() - t0) * 1000
        return out

    def stop(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(30)


def texts(mail: str) -> list:
    """luoke-mail 合成样例邮件的正文，切成句子（够长的）。"""
    sys.path.insert(0, mail)
    from luoke_mail import samples
    out = []
    for it in samples.build():
        body = it["mail"].get("body") or ""
        for s in re.split(r"(?<=[。！？\n])", body):
            s = s.strip()
            if 12 <= len(s) <= 60 and re.search(r"[一-鿿]{6}", s):
                out.append(s)
    return list(dict.fromkeys(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True)
    ap.add_argument("--mail", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--runtime", action="append", required=True, help="名字=python:site-packages，两次")
    ap.add_argument("--sentences", type=int, default=40)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    recipe = json.load(open(os.path.join(a.core, "recipes/dev.json"), encoding="utf-8"))
    rp = recipe["tasks"]["luoke-ime.rerank"]["params"]["*"]
    pp = recipe["tasks"]["luoke-ime.predict"]["params"]["*"]
    rng = random.Random(42)
    sents = texts(a.mail)
    rng.shuffle(sents)
    sents = sents[:a.sentences]
    pool = sorted({s[i:i + 2] for s in sents for i in range(0, len(s) - 1, 3) if re.fullmatch(r"[一-鿿]{2}", s[i:i + 2])})
    reqs = [{"kind": "score", "req": {"ctx": rp["probe"]["context"], "cands": rp["probe"]["candidates"]}},     # 配方自带的探针
            {"kind": "predict", "req": {"op": "predict", "ctx": pp["probe"]["context"], "k": pp["k"], "n": pp["n"]}}]
    for s in sents:                                   # 像打字：前文逐步变长，每两个字重排一次；每句末尾附近联想一次
        for p in range(4, len(s) - 2, 2):
            true = s[p:p + 2]
            if not re.fullmatch(r"[一-鿿]{2}", true):
                continue
            cands = [true] + rng.sample([w for w in pool if w != true], 9)
            rng.shuffle(cands)
            reqs.append({"kind": "score", "req": {"ctx": s[:p], "cands": cands}, "true": true})
        reqs.append({"kind": "predict", "req": {"op": "predict", "ctx": s[:max(6, len(s) * 2 // 3)], "k": pp["k"], "n": pp["n"]}})
    rts = [r.split("=", 1) for r in a.runtime]
    W = [Worker(n, *spec.split(":", 1), a.core, a.model, rp["prefix_snapshots"]) for n, spec in rts]
    names = [w.name for w in W]
    print("ready", {w.name: w.ready for w in W}, {w.name: w.versions for w in W}, flush=True)
    rows = []
    try:
        for i, x in enumerate(reqs):
            order = W if i % 2 == 0 else W[::-1]          # 交替先后
            row = {"kind": x["kind"], "true": x.get("true")}
            for w in order:
                row[w.name] = w.ask(x["req"])
            rows.append(row)
    finally:
        for w in W:
            w.stop()
    A, B = names
    sc = [r for r in rows if r["kind"] == "score"]
    pr = [r for r in rows if r["kind"] == "predict"]
    diff = [max(abs(x - y) for x, y in zip(r[A]["lp"], r[B]["lp"])) for r in sc]
    top_same = sum(max(range(len(r[A]["lp"])), key=r[A]["lp"].__getitem__) == max(range(len(r[B]["lp"])), key=r[B]["lp"].__getitem__) for r in sc)
    rank_same = sum(sorted(range(len(r[A]["lp"])), key=lambda k: -r[A]["lp"][k]) == sorted(range(len(r[B]["lp"])), key=lambda k: -r[B]["lp"][k]) for r in sc)
    cont = lambda r, n: [c[0] for c in r[n].get("cont") or []]  # noqa: E731
    p_same = sum(cont(r, A) == cont(r, B) for r in pr)
    p_top = sum(cont(r, A)[:1] == cont(r, B)[:1] for r in pr)
    q = lambda xs, f: round(sorted(xs)[min(len(xs) - 1, int(f * len(xs)))], 1)  # noqa: E731
    summary = {"requests": {"score": len(sc), "predict": len(pr)}, "versions": {w.name: w.versions for w in W},
               "score": {"max_abs_lp_diff_median": round(st.median(diff), 4), "max_abs_lp_diff_max": round(max(diff), 4),
                         "top1_same": top_same, "rank_same": rank_same},
               "predict": {"same_list": p_same, "same_first": p_top},
               "ms": {n: {k: {"p50": q([r[n]["ms"] for r in rs], 0.5), "p90": q([r[n]["ms"] for r in rs], 0.9),
                              "hit": round(sum(bool(r[n].get("hit")) for r in rs) / len(rs), 3)}
                          for k, rs in (("score", sc), ("predict", pr))} for n in names}}
    json.dump({"summary": summary, "rows": rows}, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)


if __name__ == "__main__":
    main()
