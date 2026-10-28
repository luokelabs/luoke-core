#!/usr/bin/env python3
"""对着一个在跑的开发版主控，把任务层每个端点走一遍（V1 §2）。

    python3 tools/hub_smoke.py --socket <hub.sock> [--mail-task luoke-mail.triage] [--ime-task luoke-ime.rerank] [--no-mem]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luoke_core.client import HubClient, HubError  # noqa: E402

SCHEMA = {"type": "object", "properties": {"category": {"type": "string"}, "summary": {"type": "string"}},
          "required": ["category", "summary"]}
MAIL = [{"role": "system", "content": "你是邮件助手。只输出 JSON。"},
        {"role": "user", "content": "邮件：\n发件人: 王老师\n主题: 下周三家长会\n正文: 各位家长好，下周三下午三点在三年级二班教室开家长会，请准时参加。\n\n请给出 category（work/family/promo/other）和一句话 summary。"}]


def step(name, fn):
    t = time.time()
    try:
        r = fn()
        print(f"ok   {name} ({time.time() - t:.1f}s): {json.dumps(r, ensure_ascii=False)[:300]}")
        return r
    except HubError as e:
        print(f"HUB  {name} ({time.time() - t:.1f}s): {e.status} {e.code} {e.message} retry_after={e.retry_after}")
        return e
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {name}: {type(e).__name__}: {e}")
        return e


def chat(c, task, priority, **kw):
    out, events = [], []
    for kind, data in c.chat(task, {"messages": MAIL, "luoke_items": 1,
                                    "response_format": {"type": "json_schema", "json_schema": {"name": "triage", "schema": SCHEMA}}},
                             priority=priority, **kw):
        if kind == "chunk":
            for ch in data.get("choices", []):
                out.append((ch.get("delta") or {}).get("content") or "")
        else:
            events.append((kind, data))
    return {"events": events, "text": "".join(out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--mail-task", default="luoke-mail.triage")
    ap.add_argument("--ime-task", default="luoke-ime.rerank")
    ap.add_argument("--no-mem", action="store_true")
    a = ap.parse_args()
    mail = HubClient("luoke-mail/0.1", a.socket)
    ime = HubClient("luoke-ime/0.1", a.socket)
    bad = HubClient("nobody/1", a.socket)

    step("status", mail.status)
    step("tasks(mail)", mail.tasks)
    step("tasks(ime)", ime.tasks)
    step("tasks(unknown) -> 403", bad.tasks)
    step("chat without priority -> 400", lambda: list(mail.chat(a.mail_task, {"messages": MAIL}, priority="nope")))
    step("chat stream:false -> 400", lambda: mail._call("POST", f"/tasks/{a.mail_task}/v1/chat/completions", {"messages": MAIL, "stream": False}, {"X-Luoke-Priority": "user-waiting"}))
    step("chat ime task as mail -> 403", lambda: list(mail.chat(a.ime_task, {"messages": MAIL})))
    step("warmup(mail)", lambda: mail.warmup(a.mail_task))
    r = step("chat user-waiting (cold load + generate)", lambda: chat(mail, a.mail_task, "user-waiting"))
    step("chat background right after -> busy(interactive)", lambda: chat(mail, a.mail_task, "background"))
    step("score (worker starts, first call may be not_ready)", lambda: ime.score(a.ime_task, "今天天气", ["很好", "狠好", "很号"]))
    for i in range(10):
        r = step(f"score retry {i}", lambda: ime.score(a.ime_task, "今天天气", ["很好", "狠好", "很号"]))
        if not isinstance(r, Exception):
            break
        time.sleep(2)
    step("score again (prefix cache hit)", lambda: ime.score(a.ime_task, "今天天气很好", ["我们", "我门"]))
    step("score too many candidates -> 400", lambda: ime.score(a.ime_task, "x", ["a"] * 100))
    step("resident", lambda: ime.resident(False))
    step("exiting(ime)", ime.exiting)
    step("status", mail.status)


if __name__ == "__main__" and "--dispatch" not in sys.argv:
    main()


def chat_body(c, task, body, priority):
    events, n = [], 0
    for kind, data in c.chat(task, body, priority=priority):
        if kind == "chunk":
            n += 1
        else:
            events.append((kind, data))
    return {"events": events, "chunks": n}


def dispatch_checks(a):
    """派发：交互窗口过后后台能进；两个后台不并行；后台跑着时"用户在等"把它打断。要等交互窗口（60 s）。"""
    import threading
    mail = HubClient("luoke-mail/0.1", a.socket)
    long_body = {"messages": [{"role": "system", "content": "你是邮件助手。把每封邮件各改写成 200 字的中文摘要，逐封输出，不要省略。"},
                              {"role": "user", "content": "".join(f"第{i}封：各位同事，关于下季度的预算安排和项目排期，请大家在周五前把各自部门的需求汇总给我，谢谢。\n" for i in range(8))}],
                 "luoke_items": 8}
    short_body = {"messages": MAIL, "luoke_items": 1}

    def wait_admittable(what):
        for _ in range(90):
            try:
                return chat_body(mail, "luoke-mail.triage", short_body, "background")
            except HubError as e:
                if e.code == "busy":
                    print(f"  ... {what}: busy ({e.message}), retry_after={e.retry_after}")
                    time.sleep(min(e.retry_after or 2, 10))
                    continue
                raise
        raise RuntimeError("后台一直进不去")
    step("background admitted once the interactive window passed", lambda: wait_admittable("等交互窗口"))
    result = {}
    started = threading.Event()

    def bg():
        for _ in range(60):
            try:
                started.set()
                result["bg"] = chat_body(mail, "luoke-mail.triage", long_body, "background")
                return
            except HubError as e:
                result["bg"] = e
                if e.code == "busy":
                    started.clear()
                    time.sleep(min(e.retry_after or 1, 5))
                    continue
                return
    th = threading.Thread(target=bg)
    th.start()
    for _ in range(100):      # 等它真的进了引擎
        if started.is_set() and mail.status()["job"]:
            break
        time.sleep(0.5)
    print("  background job in engine:", mail.status()["job"])
    step("second background while one runs -> busy", lambda: chat_body(mail, "luoke-mail.triage", short_body, "background"))
    step("user-waiting during background -> background gets interrupted", lambda: chat_body(mail, "luoke-mail.triage", short_body, "user-waiting"))
    th.join()
    bgres = result.get("bg")
    print("background outcome:", bgres if isinstance(bgres, Exception) else json.dumps(bgres, ensure_ascii=False)[:400])
    step("status", mail.status)


if __name__ == "__main__" and "--dispatch" in sys.argv:
    sys.argv.remove("--dispatch")
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    dispatch_checks(ap.parse_args())
