#!/usr/bin/env python3
"""把 coexist_bench.py 的结果（一个或多个 JSONL）汇成一张表。

  python3 summarize.py results/*.jsonl [--bench-dump FILE ...]

每个结果文件一组：各场景打分的 rtt p50 / p90 / max（毫秒）和条数、后台的 prefill 与解码速度、缓存命中、
断开后探测请求的首 token 延迟、内存。--bench-dump 是 bench.py --dump 的文件，额外给出"最后一个键到最终答复"的分位数。
"""
import argparse
import json
import statistics
from collections import defaultdict


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def fmt(x, nd=0):
    return "-" if x is None else (f"{x:.{nd}f}")


def summarize(path):
    recs = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    meta = next((r for r in recs if r["type"] == "meta" and "args" in r), {})
    base = next((r.get("base") for r in recs if r["type"] == "meta" and "base" in r), "?")
    m = meta.get("machine", {})
    print(f"\n## {path}")
    print(f"- 机器：{m.get('chip')}，{m.get('mem_gb')} GB，macOS {m.get('macos')}，{m.get('powermode')}，{'接电源' if m.get('ac') else '电池'}")
    print(f"- 底座：{base}；缓存：{meta.get('args', {}).get('cache')}；回放：{meta.get('args', {}).get('requests')}")
    load = next((r for r in recs if r["type"] == "load"), None)
    if load:
        print(f"- 底座加载：{load['secs']} s（status {load['status']}）")
    ready = next((r for r in recs if r["type"] == "scorer"), None)
    if ready:
        print(f"- 打分模型加载：{ready['secs']} s")

    scores = defaultdict(list)
    by_phase = defaultdict(list)
    for r in recs:
        if r["type"] == "score":
            scores[r["scenario"]].append(r["rtt_ms"])
            by_phase[(r["scenario"], r["phase"])].append(r["rtt_ms"])
    bg = defaultdict(list)
    for r in recs:
        if r["type"] == "bg" and not r.get("aborted") and r.get("status") == 200:
            # 旧文件的 decode_tok_s 按流式块数算，偏低（一块可含多个 token）；有 usage 就按 usage 重算
            u = r.get("usage") or {}
            if u.get("completion_tokens") and r.get("first_token") and r.get("done") and u["completion_tokens"] > 1:
                r["decode_tok_s"] = round((u["completion_tokens"] - 1) / max(r["done"] - r["first_token"], 1e-3), 1)
            bg[r["scenario"]].append(r)

    print("\n| 场景 | 打分 n | rtt p50 | p90 | max | 其中 prefill 期间 p90 | 解码期间 p90 | 后台 prefill tok/s | 解码 tok/s | 缓存命中 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for sc in ("S0", "S1", "S2", "S3", "S4", "S5", "B2", "B3"):
        xs = scores.get(sc) or []
        if not xs and sc not in bg:
            continue
        pf = by_phase.get((sc, "prefill"), [])
        dc = by_phase.get((sc, "decode"), [])
        b = bg.get(sc, [])
        pres = [x["prefill_tok_s"] for x in b if x.get("prefill_tok_s")]
        decs = [x["decode_tok_s"] for x in b if x.get("decode_tok_s")]
        pre = statistics.median(pres) if pres else None
        dec = statistics.median(decs) if decs else None
        hit = None
        if b and any(x.get("cached_tokens") is not None for x in b):
            tot = sum((x.get("usage") or {}).get("prompt_tokens", 0) for x in b)
            hit = sum(x.get("cached_tokens") or 0 for x in b) / tot if tot else None
        if not xs:
            print(f"| {sc}（只有后台） | 0 | - | - | - | - | - | {fmt(pre)} | {fmt(dec)} | {fmt(hit * 100) + '%' if hit is not None else '-'} |")
            continue
        print(f"| {sc} | {len(xs)} | {fmt(statistics.median(xs))} | {fmt(pct(xs, .9))} | {fmt(max(xs))} | "
              f"{fmt(pct(pf, .9)) if pf else '-'} ({len(pf)}) | {fmt(pct(dc, .9)) if dc else '-'} ({len(dc)}) | "
              f"{fmt(pre)} | {fmt(dec)} | {fmt(hit * 100) + '%' if hit is not None else '-'} |")

    aborts = [r for r in recs if r["type"] == "abort" and r.get("gap_close_to_first_token") is not None]
    if aborts:
        gaps = [r["gap_close_to_first_token"] for r in aborts]
        probes = [r["probe_ttft"] for r in aborts if r.get("probe_ttft")]
        idle = next((r for r in recs if r["type"] == "probe_idle" and r.get("ttft")), None)
        extra = f"；空闲时同样的探测 TTFT {idle['ttft']:.2f} s，所以打断代价约 {statistics.median(gaps) - idle['ttft']:.2f} s" if idle else ""
        print(f"\n- 打断：断开连接到探测请求首 token，中位数 {statistics.median(gaps):.2f} s，最大 {max(gaps):.2f} s（n={len(gaps)}）{extra}")
    errs = [r for r in recs if r["type"] == "bg" and r.get("error")]
    if errs:
        kinds = defaultdict(int)
        for e in errs:
            kinds[(e.get("status"), (e.get("error") or "")[:60])] += 1
        print("- 后台请求出错：" + "; ".join(f"{k[0]} {k[1]!r} ×{n}" for k, n in kinds.items()))
    lu = [r for r in recs if r["type"] == "load_unload"]
    if lu:
        loads = [r["load_secs"] for r in lu if r.get("load_status") == 200]
        unloads = [r["unload_secs"] for r in lu if r.get("unload_status") == 200]
        recl = [r["reclaim_secs"] for r in lu if r.get("reclaim_secs") is not None]
        print(f"- 底座加载/卸载（n={len(lu)}）：加载中位数 {fmt(statistics.median(loads), 1) if loads else '-'} s，卸载 {fmt(statistics.median(unloads), 1) if unloads else '-'} s，"
              f"卸载后空闲内存回到原水平 {fmt(statistics.median(recl), 1) if recl else '-'} s（{len(recl)}/{len(lu)} 次回到了）；"
              f"空闲内存 加载前/加载后/卸载后 {lu[-1].get('free_before')}% / {lu[-1].get('free_loaded')}% / {lu[-1].get('free_after')}%")
    mem = [r for r in recs if r["type"] == "mem" and r.get("free_pct") is not None]
    if mem:
        print(f"- 系统空闲内存：最低 {min(r['free_pct'] for r in mem)}%，中位数 {statistics.median(r['free_pct'] for r in mem):.0f}%")


def bench_dump(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    finals = [r["ms"][-1] for r in rows if r.get("ms")]
    firsts = [r["ms"][0] for r in rows if r.get("ms")]
    print(f"\n## bench.py --dump {path}（n={len(rows)}）")
    print(f"- 最后一个键到最终答复：p50 {fmt(statistics.median(finals))} ms，p90 {fmt(pct(finals, .9))} ms，max {fmt(max(finals))} ms")
    print(f"- 最后一个键到先给的答复：p50 {fmt(statistics.median(firsts))} ms，p90 {fmt(pct(firsts, .9))} ms")
    ok = sum(1 for r in rows if r.get("final") == r.get("gold"))
    print(f"- 最终首选正确：{ok}/{len(rows)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="*")
    ap.add_argument("--bench-dump", nargs="*", default=[])
    args = ap.parse_args()
    for p in args.results:
        summarize(p)
    for p in args.bench_dump:
        bench_dump(p)


if __name__ == "__main__":
    main()
