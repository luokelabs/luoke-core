#!/usr/bin/env python3
"""Stand-in for the IME engine's scorer.py that records every scoring request.

Put a copy of this file as `scorer.py` in a scratch engine folder (with the real scorer
symlinked as `real_scorer.py`), point bench.py at that folder with --engine, and every
request the engine makes ends up in $COEXIST_RECORD as JSON lines:
    {"t": seconds since start, "ctx": ..., "cands": [...], "ms": scorer time}
The replies are forwarded unchanged, so the engine behaves exactly as before.
"""
import json
import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
real = os.path.join(here, "real_scorer.py")
out = open(os.environ.get("COEXIST_RECORD", "/tmp/score_requests.jsonl"), "a", encoding="utf-8")
p = subprocess.Popen([sys.executable, real] + sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
sys.stdout.write(p.stdout.readline())  # {"ready": ...}
sys.stdout.flush()
t0 = time.time()
for line in sys.stdin:
    t = time.time()
    p.stdin.write(line)
    p.stdin.flush()
    reply = p.stdout.readline()
    try:
        req, rep = json.loads(line), json.loads(reply)
        out.write(json.dumps({"t": round(t - t0, 4), "ctx": req.get("ctx", ""), "cands": req.get("cands", []),
                              "ms": rep.get("ms")}, ensure_ascii=False) + "\n")
        out.flush()
    except ValueError:
        pass
    sys.stdout.write(reply)
    sys.stdout.flush()
