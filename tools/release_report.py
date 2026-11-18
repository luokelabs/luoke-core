#!/usr/bin/env python3
"""每版的流程报告：读 releases/<tag>/timeline.json（tools/internal_build.py 写的）和 evidence/timing-<档>.jsonl
（tools/release.py 写的，每格评测的起止和其中加载模型的秒数），写 releases/<tag>/report.md。

    python3 tools/release_report.py <tag> [--prev <上一版>] [--summary]
        --summary   只打印给 issue 回复用的几行摘要（总用时、最慢的 3 步、最慢的 3 格评测、内存峰值、明显变慢的步骤和格子）

报告里有机器名，只放在 luoke-core 的 releases/ 里，不进公开的 luoke-releases。只用标准库。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOWER_RATIO = 1.5          # 比上一版慢一半以上……
SLOWER_MIN_SECS = 300       # ……而且多出 5 分钟以上，才算明显变慢（步骤和每格评测都按这个）
SLOWEST_CELLS = 15          # 报告里列最慢的几格评测
EARLIER_HOPS = 10           # 上一版沿用的格子顺着 reused_from 往前找几版


def load(tag: str) -> dict:
    p = os.path.join(ROOT, "releases", tag, "timeline.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def durations(tl: dict) -> dict:
    """每一步的用时（秒）。同名的步骤（续跑时重做的、等钥匙串的几次）加起来；没结束的算到现在。"""
    out: dict = {}
    for p in tl.get("phases", []):
        end = p.get("end") or time.time()
        out[p["name"]] = out.get(p["name"], 0) + max(0.0, end - p["start"])
    return out


def fmt(secs: float) -> str:
    secs = int(round(secs))
    if secs < 60:
        return f"{secs} 秒"
    h, m = divmod(secs // 60, 60)
    return f"{h} 小时 {m} 分" if h else f"{m} 分"


def span(tl: dict) -> float:
    ph = tl.get("phases", [])
    if not ph:
        return 0.0
    return max((p.get("end") or time.time()) for p in ph) - min(p["start"] for p in ph)


def idle(tl: dict) -> float:
    """总用时里不在任何一步里的秒数：步骤之间的空档，停下以后到续跑之间（例如差异对比停下、等人放行）也在这里。"""
    ph = sorted(((p["start"], p.get("end") or time.time()) for p in tl.get("phases", [])))
    gap, reach = 0.0, None
    for a, b in ph:
        if reach is not None and a > reach:
            gap += a - reach
        reach = b if reach is None else max(reach, b)
    return gap


def timings(tag: str) -> list:
    """evidence/timing-*.jsonl 里的每一段：kind 是 hub（实验室主控起来）或 cell（一格评测）。"""
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "releases", tag, "evidence", "timing-*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def cells(tag: str) -> dict:
    """{(任务, 档): 用时记录}：这一版真评了的格子；同一格评了几次（续跑、金丝雀没对上后重评）取最后一次。"""
    return {(x["task"], str(x["tier"])): x for x in timings(tag) if x.get("kind") == "cell"}


def reused_sources(tag: str) -> dict:
    """{(任务, 档): 沿用自哪一版}：这一版证据里沿用来的格子。"""
    out = {}
    for p in glob.glob(os.path.join(ROOT, "releases", tag, "evidence", "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("reused_from"):
            out[(d.get("task"), str(d.get("tier_run")))] = d["reused_from"]
    return out


def earlier(prev: str, keys) -> dict:
    """{(任务, 档): (那一版, 用时记录)}：每一格在 prev 或更早最近一次真评的用时。prev 沿用的格子顺着证据的 reused_from 往前找。"""
    seen: dict = {}

    def at(tag):
        if tag not in seen:
            seen[tag] = (cells(tag), reused_sources(tag))
        return seen[tag]
    out = {}
    for k in keys:
        tag = prev
        for _ in range(EARLIER_HOPS):
            if not tag:
                break
            here, src = at(tag)
            if k in here:
                out[k] = (tag, here[k])
                break
            tag = src.get(k)
    return out


def cell_name(k: tuple) -> str:
    return f"{k[0]} {k[1]} GB"


def peaks(tl: dict) -> dict:
    s = tl.get("samples", [])
    def mx(k):
        vals = [x[k] for x in s if isinstance(x.get(k), (int, float))]
        return max(vals) if vals else None
    sw = [x["swapouts"] for x in s if isinstance(x.get("swapouts"), int)]
    return {"wired_gb": mx("wired_gb"), "compressed_gb": mx("compressed_gb"), "load": mx("load"), "pressure": mx("pressure"),
            "swapouts": (sw[-1] - sw[0]) if len(sw) > 1 else None,
            "free_disk_gb": min((x["free_disk_gb"] for x in s if isinstance(x.get("free_disk_gb"), (int, float))), default=None)}


def slower(cur: dict, prev: dict) -> list:
    """[(步骤, 这一版秒数, 上一版秒数)]：明显变慢的。"""
    out = []
    for name, t in cur.items():
        p = prev.get(name)
        if p and t > p * SLOWER_RATIO and t - p > SLOWER_MIN_SECS:
            out.append((name, t, p))
    return sorted(out, key=lambda x: x[1] - x[2], reverse=True)


PRESSURE = {1: "正常", 2: "警告", 4: "紧张"}


def summary(tag: str, prev: str = None) -> list:
    tl = load(tag)
    if not tl:
        return [f"{tag} 没有流程记录（timeline.json）。"]
    d = durations(tl)
    gap = idle(tl)
    lines = [f"总用时 {fmt(span(tl))}（{tl.get('host', '?')}）" + (f"，其中 {fmt(gap)}不在任何一步里（步骤之间的空档，停下以后等续跑也算在这里）" if gap > 60 else "") + "。"]
    top = sorted(((n, t) for n, t in d.items() if n != "keychain wait"), key=lambda x: x[1], reverse=True)[:3]
    if top:
        lines.append("最慢的 3 步：" + "、".join(f"{n} {fmt(t)}" for n, t in top) + "。")
    cur = cells(tag)
    if cur:
        top_cells = sorted(cur.items(), key=lambda x: x[1]["secs"], reverse=True)[:3]
        lines.append("最慢的 3 格评测：" + "、".join(f"{cell_name(k)} {fmt(x['secs'])}" for k, x in top_cells) + "。")
    if d.get("keychain wait", 0) > 60:
        lines.append(f"等钥匙串 {fmt(d['keychain wait'])}。")
    pk = peaks(tl)
    if pk["wired_gb"] is not None:
        lines.append(f"内存：wired 峰值 {pk['wired_gb']} GB，压缩峰值 {pk['compressed_gb']} GB，压力最高 {PRESSURE.get(pk['pressure'], pk['pressure'])}"
                     + (f"，换出 {pk['swapouts']} 页" if pk["swapouts"] else "") + "。")
    if prev:
        sl = slower(d, durations(load(prev)))
        if sl:
            lines.append(f"比 {prev} 明显变慢：" + "、".join(f"{n} {fmt(t)}（上一版 {fmt(p)}）" for n, t, p in sl) + "。")
        if cur:
            was = earlier(prev, cur)
            sl = slower({cell_name(k): x["secs"] for k, x in cur.items()}, {cell_name(k): x["secs"] for k, (_t, x) in was.items()})
            if sl:
                lines.append("比上次评明显变慢的格：" + "、".join(f"{n} {fmt(t)}（上次 {fmt(p)}）" for n, t, p in sl) + "。")
    ne = not_evaluated(tag)
    if ne:
        lines.append("没评成（沿用上一版的开关）：" + "、".join(f"{t} {g} GB" for t, g in ne) + "。")
    if tl.get("unevaluated_engine"):
        lines.append(tl["unevaluated_engine"] + "。")         # --no-eval 沿用的那一版用的是别的引擎（internal_build.unevaluated_engine）
    for repo, note in sorted((tl.get("kept") or {}).items()):
        lines.append(note + "。")
    for repo, why in sorted((tl.get("dropped") or {}).items()):
        lines.append(f"这一版没带 {repo}（{why}）。")
    if tl.get("failed"):
        lines.append("停在：" + tl["failed"])
    return lines


def not_evaluated(tag: str) -> list:
    """[(任务, 档)]：evidence/not-evaluated-*.jsonl 里记的、后来也没评成的格子（tools/release.py 写的）。"""
    ev = os.path.join(ROOT, "releases", tag, "evidence")
    got = set()
    try:
        names = os.listdir(ev)
    except OSError:
        return []
    for n in names:
        if n.startswith("not-evaluated-") and n.endswith(".jsonl"):
            for line in open(os.path.join(ev, n), encoding="utf-8"):
                try:
                    x = json.loads(line)
                except ValueError:
                    continue
                got.add((x["task"], str(x["tier"])))
    done = set()
    for n in names:
        if n.endswith(".json"):
            try:
                d = json.load(open(os.path.join(ev, n), encoding="utf-8"))
                done.add((d.get("task"), str(d.get("tier_run"))))
            except (OSError, ValueError):
                pass
    return sorted(got - done)


def cell_section(tag: str, prev: str = None) -> list:
    """每格评测：每一档的合计（评测程序、实验室主控起来、其余），最慢的几格和上次评这一格的用时。"""
    ts = timings(tag)
    if not ts:
        return []
    d = durations(load(tag))
    out = ["## 每格评测（任务 × 档）", ""]
    for tier in sorted({str(x["tier"]) for x in ts}, key=int):
        hubs = [x for x in ts if str(x["tier"]) == tier and x.get("kind") == "hub"]
        mine = [x for x in ts if str(x["tier"]) == tier and x.get("kind") == "cell"]
        hs, cs = sum(x["secs"] for x in hubs), sum(x["secs"] for x in mine)
        line = (f"- {tier} GB 档：评了 {len(mine)} 格，共 {fmt(cs)}（其中加载模型 {fmt(sum(x.get('load_secs') or 0 for x in mine))}）；"
                f"实验室主控起了 {len(hubs)} 次，共 {fmt(hs)}（其中加载模型 {fmt(sum(x.get('load_secs') or 0 for x in hubs))}）")
        if d.get(f"eval {tier}"):
            line += f"；eval {tier} 一共 {fmt(d[f'eval {tier}'])}，其余 {fmt(max(0.0, d[f'eval {tier}'] - hs - cs))}不在评测里（同步、起停、拷回）"
        out.append(line + "。")
    cur = cells(tag)
    rows = sorted(cur.items(), key=lambda x: x[1]["secs"], reverse=True)[:SLOWEST_CELLS]
    was = earlier(prev, [k for k, _ in rows]) if prev else {}
    out += ["", f"最慢的 {len(rows)} 格（同一格评了几次取最后一次）"
            + (f"。「上次评」是 {prev} 这一格的用时；{prev} 沿用的格子往前找到真评的那一版，括号里写是哪一版" if prev else "") + "：", "",
            "| 任务 | 档 | 开始 | 用时 | 其中加载模型 | 上次评 | 备注 |", "|---|---|---|---|---|---|---|"]
    for k, x in rows:
        start = time.strftime("%m-%d %H:%M", time.localtime(x["start"]))
        per: dict = {}
        for m in x.get("loads") or []:
            n, secs = per.get(m["model"], (0, 0.0))
            per[m["model"]] = (n + 1, secs + m["secs"])
        loads = "、".join(f"{name}{f' {n} 次' if n > 1 else ''} {fmt(secs)}" for name, (n, secs) in per.items())
        t, p = was.get(k, (None, None))
        before = (fmt(p["secs"]) + ("" if t == prev else f"（{t}）")) if p else "—"
        note = "没进展、停掉" if x.get("stalled") else (f"退出码 {x['exit']}" if x.get("exit") else "")
        out.append(f"| {k[0]} | {k[1]} | {start} | {fmt(x['secs'])} | {fmt(x.get('load_secs') or 0)}{'（' + loads + '）' if loads else ''} | {before} | {note} |")
    return out + [""]


def report(tag: str, prev: str = None) -> str:
    tl = load(tag)
    d = durations(tl)
    rows = []
    for p in tl.get("phases", []):
        start = time.strftime("%m-%d %H:%M", time.localtime(p["start"]))
        took = fmt((p.get("end") or time.time()) - p["start"])
        state = "进行中" if p.get("end") is None else ("完成" if p.get("ok") else "失败")
        rows.append(f"| {p['name']} | {start} | {took} | {state} |")
    pd = durations(load(prev)) if prev else {}
    cmp_rows = [f"| {n} | {fmt(t)} | {fmt(pd[n]) if n in pd else '—'} |" for n, t in d.items()]
    pk = peaks(tl)
    out = [f"# 流程报告：{tag}", "", *summary(tag, prev), "",
           "## 每一步", "", "| 步骤 | 开始 | 用时 | 结果 |", "|---|---|---|---|", *rows, ""]
    if prev:
        out += [f"## 和 {prev} 比", "", "| 步骤 | 这一版 | 上一版 |", "|---|---|---|", *cmp_rows, ""]
    out += cell_section(tag, prev)
    out += ["## 本机资源（每 30 秒采样）", "",
            f"- 采样 {len(tl.get('samples', []))} 次；负载峰值 {pk['load']}；wired 峰值 {pk['wired_gb']} GB；压缩峰值 {pk['compressed_gb']} GB",
            f"- 内存压力最高：{PRESSURE.get(pk['pressure'], pk['pressure'])}；换出 {pk['swapouts']} 页；磁盘最少剩 {pk['free_disk_gb']} GB",
            "- 16、32 档测试机的资源没有采：那两档的用时看上面的 eval 16 / eval 32 和每格评测。", ""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tag")
    ap.add_argument("--prev", default=None)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.summary:
        print("\n".join(summary(a.tag, a.prev)))
        return
    text = report(a.tag, a.prev)
    path = os.path.join(ROOT, "releases", a.tag, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(path)


if __name__ == "__main__":
    sys.exit(main())
