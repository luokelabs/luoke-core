#!/usr/bin/env python3
"""每版的流程报告：读 releases/<tag>/timeline.json（tools/internal_build.py 写的），写 releases/<tag>/report.md。

    python3 tools/release_report.py <tag> [--prev <上一版>] [--summary]
        --summary   只打印给 issue 回复用的几行摘要（总用时、最慢的 3 步、内存峰值、明显变慢的步骤）

报告里有机器名，只放在 luoke-core 的 releases/ 里，不进公开的 luoke-releases。只用标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOWER_RATIO = 1.5          # 比上一版慢一半以上……
SLOWER_MIN_SECS = 300       # ……而且多出 5 分钟以上，才算明显变慢


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
    lines = [f"总用时 {fmt(span(tl))}（{tl.get('host', '?')}）。"]
    top = sorted(((n, t) for n, t in d.items() if n != "keychain wait"), key=lambda x: x[1], reverse=True)[:3]
    if top:
        lines.append("最慢的 3 步：" + "、".join(f"{n} {fmt(t)}" for n, t in top) + "。")
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
    ne = not_evaluated(tag)
    if ne:
        lines.append("没评成（沿用上一版的开关）：" + "、".join(f"{t} {g} GB" for t, g in ne) + "。")
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
    out += ["## 本机资源（每 30 秒采样）", "",
            f"- 采样 {len(tl.get('samples', []))} 次；负载峰值 {pk['load']}；wired 峰值 {pk['wired_gb']} GB；压缩峰值 {pk['compressed_gb']} GB",
            f"- 内存压力最高：{PRESSURE.get(pk['pressure'], pk['pressure'])}；换出 {pk['swapouts']} 页；磁盘最少剩 {pk['free_disk_gb']} GB",
            "- 16、32 档测试机的资源没有采：那两档的用时看上面的 eval 16 / eval 32。", ""]
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
