#!/usr/bin/env python3
"""实验用：把主控任务层的最小一角（GET /tasks、POST /tasks/<任务>/v1/chat/completions）接到任意 OpenAI 兼容服务上，
让配件自带的评测程序（luoke-mem 的 run_typing / run_topics 等，只认 --hub SOCKET）能对着主控还不支持的引擎跑（例如 Strata）。

    python3 tools/exp_hub_shim.py --socket /tmp/exp-shim.sock --base-url http://127.0.0.1:8092/v1 --model NAME \
        --recipe recipes/dev.json --tier 32

只给实验用：不排队、不让路、不认配件身份；任务参数按配方那一档算 max_tokens（item_tokens × 条数 + fixed_tokens），
temperature 0、不思考。结果不能当发布证据。
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def task_params(recipe: dict, task: str, tier: str) -> dict:
    p = (recipe["tasks"].get(task) or {}).get("params") or {}
    return dict(p.get("*") or {}, **(p.get(tier) or {}))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--recipe", default=os.path.join(ROOT, "recipes", "dev.json"))
    ap.add_argument("--tier", default="32")
    a = ap.parse_args()
    with open(a.recipe, encoding="utf-8") as f:
        recipe = json.load(f)

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def address_string(self):
            return "unix"

        def log_message(self, fmt, *args):
            sys.stderr.write("shim: " + fmt % args + "\n")

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/tasks":
                return self._json(200, {"tasks": {t: {"status": "available", "params": task_params(recipe, t, a.tier)}
                                                  for t in recipe["tasks"]}})
            return self._json(404, {"code": "not_found"})

        def do_POST(self):
            parts = self.path.split("/")
            if len(parts) < 3 or parts[1] != "tasks" or not self.path.endswith("/v1/chat/completions"):
                return self._json(404, {"code": "not_found"})
            task = parts[2]
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            prm = task_params(recipe, task, a.tier)
            items = int(body.pop("luoke_items", 1) or 1)
            out = {"model": a.model, "messages": body["messages"], "stream": True, "temperature": 0,
                   "max_tokens": int(prm.get("item_tokens", 256)) * items + int(prm.get("fixed_tokens", 100)),
                   "chat_template_kwargs": {"enable_thinking": bool(prm.get("think", False))}}
            if body.get("response_format"):
                out["response_format"] = body["response_format"]
            req = urllib.request.Request(a.base_url.rstrip("/") + "/chat/completions", data=json.dumps(out).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            try:
                r = urllib.request.urlopen(req, timeout=1800)
            except Exception as e:  # noqa: BLE001
                return self._json(502, {"code": "engine_failed", "message": str(e)[:200]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for line in r:
                self.wfile.write(line)
                self.wfile.flush()
            self.close_connection = True

    class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    if os.path.exists(a.socket):
        os.remove(a.socket)
    srv = S(a.socket, H)
    print(f"shim on {a.socket} -> {a.base_url} ({a.model}), tier {a.tier}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
