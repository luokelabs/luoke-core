#!/usr/bin/env python3
"""端到端共存验证（V1 开头"要验证的三件事"第二件）：经主控的任务层，16 GB 上交互打分和后台批量能不能共存。

    python3 tools/coexist_e2e.py --socket <hub.sock> --requests <录好的打分请求 JSONL> [--emails <JSONL，每行 {"subject","body"}>]
        [--quiet-wait 330] [--score-secs 60] [--out result.json]

走的是主控的规则，不是脚本自己的假设：
  1. 打分独跑（底座没加载）：回放录好的打分请求，记 rtt；
  2. 等安静时刻（默认 5 分钟不交互）后，用 luoke-mail 的标识发后台分拣请求（遇 busy/paused 按 retry_after 重试），看主控什么时候接、底座什么时候加载；
  3. 后台跑着时开始打字（回放打分，user-waiting）：看后台有没有被打断（luoke.error interrupted）、底座多久被卸掉、打分 rtt 在"底座在内存里""卸掉以后"各是多少；
  4. 打字停下再等安静时刻，看后台会不会恢复。
输出每一段的 p50/p90/max 和事件时间线。
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luoke_core.client import HubClient, HubError  # noqa: E402

SCHEMA = {"type": "object", "properties": {"category": {"type": "string"}, "summary": {"type": "string"}, "action": {"type": "string"}},
          "required": ["category", "summary", "action"]}
SYSTEM = "你是邮件助手。按 JSON 给出 category（work/family/promo/other）、一句话 summary、action（reply/archive/ignore）。"


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def stats(xs):
    return {"n": len(xs), "p50": round(statistics.median(xs), 1) if xs else None, "p90": round(pct(xs, .9), 1) if xs else None,
            "max": round(max(xs), 1) if xs else None}


class Timeline:
    def __init__(self):
        self.t0 = time.time()
        self.events = []
        self.lock = threading.Lock()

    def add(self, what, **kw):
        with self.lock:
            self.events.append({"t": round(time.time() - self.t0, 1), "what": what, **kw})
            print(f"[{self.events[-1]['t']:7.1f}s] {what} {kw if kw else ''}", flush=True)


def replay_scores(ime: HubClient, task: str, requests: list, seconds: float, tl: Timeline, label: str, status_fn):
    """按录好的间隔回放打分；返回 rtt 列表（毫秒），并按主控的 loaded 状态分成两组。"""
    rtts, by_loaded = [], {"loaded": [], "unloaded": []}
    t_start = time.time()
    i = 0
    prev_t = requests[0]["t"]
    not_ready = 0
    while time.time() - t_start < seconds:
        r = requests[i % len(requests)]
        gap = max(0.0, min(r["t"] - prev_t, 2.0)) if i % len(requests) else 0.0
        prev_t = r["t"]
        if gap > 0:
            time.sleep(gap)
        t = time.time()
        try:
            ime.score(task, r["ctx"], r["cands"][:16])
            rtt = (time.time() - t) * 1000
            rtts.append(rtt)
            loaded = status_fn()
            by_loaded["loaded" if loaded else "unloaded"].append(rtt)
        except HubError as e:
            if e.code == "not_ready":
                not_ready += 1
                time.sleep(0.5)
            else:
                tl.add("score error", code=e.code)
        i += 1
    tl.add(f"scores done ({label})", **stats(rtts), not_ready=not_ready)
    return rtts, by_loaded


def background_loop(mail: HubClient, task: str, emails: list, stop: threading.Event, tl: Timeline, results: dict):
    """luoke-mail 式的后台分拣：一封一个请求，busy/paused 按 retry_after 等。"""
    done, interrupted, errors, admitted_at = 0, 0, 0, None
    i = 0
    while not stop.is_set():
        em = emails[i % len(emails)]
        body = {"messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": f"主题：{em['subject']}\n\n{em['body']}"}],
                "luoke_items": 1, "response_format": {"type": "json_schema", "json_schema": {"name": "triage", "schema": SCHEMA}}}
        try:
            events = []
            t = time.time()
            for kind, data in mail.chat(task, body, priority="background", allow_battery=True):
                if kind != "chunk":
                    events.append((kind, data))
            if admitted_at is None:
                admitted_at = time.time()
                tl.add("background admitted")
            err = [d for k, d in events if k == "error"]
            if err:
                if err[0].get("code") == "interrupted":
                    interrupted += 1
                    tl.add("background interrupted")
                else:
                    errors += 1
                    tl.add("background error", code=err[0].get("code"))
                time.sleep(min(err[0].get("retry_after") or 5, 30))
            else:
                done += 1
                i += 1
                if done == 1:
                    tl.add("first background email done", secs=round(time.time() - t, 1))
        except HubError as e:
            if e.code in ("busy", "paused"):
                stop.wait(min(e.retry_after or 5, 30))
                continue
            errors += 1
            tl.add("background rejected", code=e.code, message=e.message[:60])
            stop.wait(10)
    results.update({"done": done, "interrupted": interrupted, "errors": errors})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--requests", required=True)
    ap.add_argument("--emails", default=None)
    ap.add_argument("--score-task", default="luoke-ime.rerank")
    ap.add_argument("--mail-task", default="luoke-mail.triage")
    ap.add_argument("--quiet-wait", type=float, default=330, help="等安静时刻的秒数（主控的 M 分钟 + 余量）")
    ap.add_argument("--score-secs", type=float, default=60)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    ime = HubClient("luoke-ime/0.1", a.socket)
    mail = HubClient("luoke-mail/0.1", a.socket)
    core = HubClient("luoke-core/0.1.0.dev1", a.socket)
    requests = [json.loads(l) for l in open(a.requests, encoding="utf-8") if l.strip()]
    requests = [r for r in requests if r.get("cands")]
    if a.emails:
        emails = [json.loads(l) for l in open(a.emails, encoding="utf-8") if l.strip()]
    else:
        import random
        random.seed(7)
        emails = [{"subject": f"第 {i} 封：{random.choice(['会议安排', '账单提醒', '周报', '家长会', '促销', '安全提醒'])}",
                   "body": "".join(random.choice(["请大家周五前把需求汇总给我。", "本月账单已出，请在 25 日前支付。", "下周三下午三点开会。",
                                                   "本季度的项目排期见附件。", "限时优惠只到本周末。", "你的账户在新设备登录。"]) for _ in range(random.randint(8, 30)))}
                  for i in range(60)]
    tl = Timeline()
    st = core.status()
    tl.add("start", tier=st["tier"], base=st["base"], loaded=st["loaded"])
    loaded = lambda: bool(core.status()["loaded"])

    # 1. 打分独跑
    s1, _ = replay_scores(ime, a.score_task, requests, a.score_secs, tl, "alone", loaded)

    # 2. 等安静时刻，后台开跑
    tl.add("waiting for quiet time", secs=a.quiet_wait)
    stop = threading.Event()
    bg_res = {}
    th = threading.Thread(target=background_loop, args=(mail, a.mail_task, emails, stop, tl, bg_res), daemon=True)
    th.start()
    t_wait = time.time()
    while time.time() - t_wait < a.quiet_wait + 120:
        st = core.status()
        if st["job"] or st["loaded"]:
            break
        time.sleep(5)
    tl.add("background state", job=st["job"], loaded=st["loaded"], quiet=st["quiet"])
    time.sleep(20)   # 让后台跑一会儿

    # 3. 用户回来打字
    tl.add("typing starts while background runs")
    t_type = time.time()
    unloaded_at = None

    def loaded_and_note():
        nonlocal unloaded_at
        l = bool(core.status()["loaded"])
        if not l and unloaded_at is None:
            unloaded_at = round(time.time() - t_type, 1)
            tl.add("base unloaded after typing started", secs=unloaded_at)
        return l
    s3, by = replay_scores(ime, a.score_task, requests, a.score_secs, tl, "while/after background", loaded_and_note)
    st = core.status()
    tl.add("after typing", job=st["job"], loaded=st["loaded"], bg=dict(bg_res))

    # 4. 停下来，看后台会不会在安静时刻恢复
    tl.add("typing stopped; waiting for background to resume", secs=a.quiet_wait)
    t_wait = time.time()
    resumed = None
    while time.time() - t_wait < a.quiet_wait + 120:
        st = core.status()
        if st["job"]:
            resumed = round(time.time() - t_wait, 1)
            break
        time.sleep(5)
    tl.add("background resumed" if resumed else "background did not resume", secs=resumed)
    stop.set()
    th.join(timeout=60)
    result = {"scores_alone": stats(s1), "scores_typing_phase": stats(s3),
              "scores_while_base_loaded": stats(by["loaded"]), "scores_after_unload": stats(by["unloaded"]),
              "base_unloaded_after_typing_secs": unloaded_at, "background": bg_res, "resumed_after_secs": resumed,
              "timeline": tl.events}
    print(json.dumps({k: v for k, v in result.items() if k != "timeline"}, ensure_ascii=False, indent=1))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
