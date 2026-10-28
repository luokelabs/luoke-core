#!/usr/bin/env python3
"""把主控 /status 的变化记成时间线（底座加载/卸载、后台工作开始/结束、交互中/安静时刻、后台状态），给共存实测用。

    python3 tools/hub_status_log.py --socket <hub.sock> --out timeline.jsonl [--interval 1]
按 Ctrl-C 或 SIGTERM 停。每行 {"t": 秒, "loaded", "job", "interactive", "quiet", "background", "scorers"}，只在有变化时写。
"""
import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luoke_core.client import HubClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    c = HubClient("luoke-core/0.1.0.dev1", a.socket, timeout=5)
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    last = None
    t0 = time.time()
    with open(a.out, "a", encoding="utf-8") as f:
        while not stop["v"]:
            try:
                st = c.status()
                cur = {"loaded": st.get("loaded"), "job": st.get("job"), "interactive": st.get("interactive"),
                       "quiet": st.get("quiet"), "background": st.get("background", {}).get("state"),
                       "scorers": st.get("scorers"), "ceiling": st.get("ceiling_gb")}
            except Exception as e:  # noqa: BLE001
                cur = {"error": type(e).__name__}
            if cur != last:
                f.write(json.dumps({"t": round(time.time() - t0, 1), "wall": time.strftime("%H:%M:%S"), **cur}, ensure_ascii=False) + "\n")
                f.flush()
                last = cur
            time.sleep(a.interval)


if __name__ == "__main__":
    main()
