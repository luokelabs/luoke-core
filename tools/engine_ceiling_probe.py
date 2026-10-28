#!/usr/bin/env python3
"""V1 §10 第 3 项的两个实测：oMLX 运行中改内存天花板是否生效；另一个 MLX 进程（打分工作进程）在旁边时它怎么算。

    python3 tools/engine_ceiling_probe.py --model-dir <目录> --base <底座目录名> --scorer <打分模型目录名> [--env <临时目录>]

只用主控自己起的 oMLX（独立 base_path、随机端口），不碰负责人的 oMLX 和 ~/.omlx，不动系统设置。
步骤：起引擎（天花板 100 GB）→ 加载底座 → 发一条 2k 请求（应成功）→ 经管理接口把天花板改成 1 GB → 再发（看是否被拒）
→ 改回 100 GB → 再发（应成功）→ 起打分工作进程，比较 oMLX 报告的 current_bytes 有没有把它算进去。
"""
import argparse
import http.client
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luoke_core.engine import Engine  # noqa: E402
from luoke_core.scorer import ScoreWorker  # noqa: E402

OMLX = "/Applications/oMLX.app"
PY = OMLX + "/Contents/Resources/Python/cpython-3.11/bin/python3.11"
SITE = OMLX + "/Contents/Resources/Python/framework-mlx-base/lib/python3.11/site-packages"


class Admin:
    def __init__(self, eng: Engine):
        self.eng = eng
        c = eng.conn(10)
        c.request("POST", "/admin/api/login", body=json.dumps({"api_key": eng.key}).encode(), headers=eng.headers())
        r = c.getresponse()
        r.read()
        self.cookie = r.getheader("Set-Cookie", "").split(";")[0]

    def call(self, method, path, body=None):
        c = self.eng.conn(30)
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"Cookie": self.cookie, "Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")[:300]

    def ceiling(self):
        st, d = self.call("GET", "/admin/api/stats")
        am = (d.get("active_models") or {}) if isinstance(d, dict) else {}
        mp = am.get("memory_pressure") or {}
        return {"current_gb": round(mp.get("current_bytes", 0) / 2**30, 2), "soft_gb": round(mp.get("soft_bytes", 0) / 2**30, 2),
                "hard_gb": round(mp.get("hard_bytes", 0) / 2**30, 2), "level": mp.get("pressure_level"),
                "model_max_gb": round(am.get("model_memory_max", 0) / 2**30, 2)}


def ask(eng: Engine, model: str, tokens: int) -> dict:
    text = ("请把下面这段话改写得更正式一些。" + "今天的会议讨论了下季度的预算安排和项目排期，各部门需要在周五前提交需求。" * (tokens // 30))
    payload = {"model": model, "messages": [{"role": "user", "content": text}], "stream": True,
               "stream_options": {"include_usage": True}, "max_tokens": 20, "temperature": 0}
    lines = []
    info = eng.stream_chat(payload, lambda l: (lines.append(l) or True), threading.Event(), 120, 300)
    return {"status": info["status"], "error": (info["error"] or "")[:160], "ttft": info["first_token_secs"],
            "prompt_tokens": (info.get("usage") or {}).get("prompt_tokens")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--scorer", required=True)
    ap.add_argument("--env", default=None)
    a = ap.parse_args()
    env = a.env or tempfile.mkdtemp(prefix="luoke-ceiling-")
    eng = Engine(OMLX, os.path.join(env, "engine"), a.model_dir, guard_gb=100, log_level="info", log=print)
    eng.start(wait_models=[a.base])
    try:
        model = eng.resolve(a.base)
        print("load:", eng.load(model), "s")
        adm = Admin(eng)
        print("ceiling at start:", adm.ceiling())
        print("2k request @100GB:", ask(eng, model, 2000))
        st, d = adm.call("POST", "/admin/api/global-settings", {"memory_guard_tier": "custom", "memory_guard_custom_ceiling_gb": 1.0})
        print("set ceiling 1GB ->", st, (d.get("message") if isinstance(d, dict) else d))
        time.sleep(3)
        print("ceiling now:", adm.ceiling())
        print("2k request @1GB:", ask(eng, model, 2000))
        print("loaded still?", model in str(adm.call("GET", "/admin/api/stats")[1].get("active_models", {})))
        st, d = adm.call("POST", "/admin/api/global-settings", {"memory_guard_tier": "custom", "memory_guard_custom_ceiling_gb": 100.0})
        print("set ceiling 100GB ->", st, (d.get("message") if isinstance(d, dict) else d))
        time.sleep(3)
        print("ceiling now:", adm.ceiling())
        if eng.loaded != model:
            eng.loaded = None
            print("reload:", eng.load(model), "s")
        print("2k request @100GB again:", ask(eng, model, 2000))
        # 第二个 MLX 进程
        before = adm.ceiling()
        w = ScoreWorker(PY, SITE, os.path.join(a.model_dir, a.scorer), 4, print)
        w.start()
        for _ in range(60):
            if w.state in ("ready", "failed"):
                break
            time.sleep(0.5)
        print("scorer state:", w.state)
        for _ in range(3):
            w.score("今天天气", ["很好", "狠好"], 10)
        after = adm.ceiling()
        print("oMLX current_bytes before/after scorer:", before["current_gb"], "/", after["current_gb"], "GB (custom 档只看自己的进程时应不变)")
        w.stop()
    finally:
        eng.stop()
        print("engine stopped; env:", env)


if __name__ == "__main__":
    main()
