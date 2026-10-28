#!/usr/bin/env python3
"""打断探针（16 GB 的关键路径）：等到安静时刻，发一条很长的后台请求（订阅阅读那种 prefill），它跑起来几秒后发一次打分，
看后台有没有收到 luoke.error interrupted、底座多久卸掉、打分本身花了多久。

    python3 tools/interrupt_probe.py --socket <hub.sock> [--chars 6000] [--after 8]
"""
import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luoke_core.client import HubClient, HubError  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--chars", type=int, default=6000)
    ap.add_argument("--after", type=float, default=8.0, help="后台跑起来几秒后发打分")
    a = ap.parse_args()
    mail = HubClient("luoke-mail/0.1", a.socket)
    ime = HubClient("luoke-ime/0.1", a.socket)
    core = HubClient("luoke-core/0.1.0.dev1", a.socket)
    t0 = time.time()

    def log(msg, **kw):
        print(f"[{time.time() - t0:6.1f}s] {msg} {kw if kw else ''}", flush=True)

    system = "把这封订阅邮件总结成不超过 8 条要点。"
    limit = int(mail.tasks()["tasks"]["luoke-mail.digest-read"]["params"].get("max_chars", a.chars))
    chars = min(a.chars, limit - len(system) - 50)      # 别超过任务的字符上限（含系统提示）
    body = {"messages": [{"role": "system", "content": system},
                         {"role": "user", "content": ("今天的通讯里，编辑部整理了本周的几件事：预算、排期、新同事入职，以及下季度的计划。" * 200)[:chars]}],
            "luoke_items": 1}
    log("background body", chars=chars, limit=limit)
    log("waiting for the hub to admit a background request (quiet time)")
    res = {}

    def bg():
        while True:
            try:
                ev = []
                tt = time.time()
                for kind, data in mail.chat("luoke-mail.digest-read", body, priority="background", allow_battery=True):
                    if kind != "chunk":
                        ev.append((kind, data))
                res["events"] = ev
                res["secs"] = round(time.time() - tt, 1)
                return
            except HubError as e:
                if e.code in ("busy", "paused"):
                    time.sleep(min(e.retry_after or 5, 20))
                    continue
                res["events"] = [("rejected", {"code": e.code, "message": e.message})]
                log("background rejected", code=e.code, message=e.message[:80])
                return
    th = threading.Thread(target=bg, daemon=True)
    th.start()
    for _ in range(400):
        st = core.status()
        if st["job"]:
            break
        time.sleep(2)
    log("background running", job=st["job"], loaded=st["loaded"])
    time.sleep(a.after)
    ts = time.time()
    for i in range(10):
        try:
            r = ime.score("luoke-ime.rerank", "今天天气", ["很好", "狠好"])
            log("score answered", ms=r["ms"], rtt_ms=round((time.time() - ts) * 1000, 1), cache_hit=r["cache_hit"])
            break
        except HubError as e:
            log("score refused", code=e.code)
            time.sleep(e.retry_after or 2)
    unloaded = None
    for i in range(60):
        st = core.status()
        if not st["loaded"]:
            unloaded = round(time.time() - ts, 1)
            break
        time.sleep(0.5)
    log("base unloaded after score" if unloaded is not None else "base still loaded 30 s after score", secs=unloaded, job=st["job"])
    th.join(timeout=300)
    log("background outcome", events=[(k, d.get("code") if isinstance(d, dict) else d) for k, d in res.get("events", [])], secs=res.get("secs"))


if __name__ == "__main__":
    main()
