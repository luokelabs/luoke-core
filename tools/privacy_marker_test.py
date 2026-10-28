#!/usr/bin/env python3
"""标记字符串测试（V1 §9）：请求文字不能出现在主控日志、引擎的 base_path、工作进程输出里。

    python3 tools/privacy_marker_test.py --socket <hub.sock> --env <主控环境目录> [--engine-base <oMLX base_path>]

对着在跑的开发版主控（要 --lab，故障路用它的 /lab/ 端点）发几条带标记的请求（正常完成、格式不合法、超长输入、打断、打分、内存保护拒绝、请求中引擎崩溃），结束后在主控环境目录
（含引擎的 base_path、日志、缓存、usage.sqlite3、stats.json、settings.json）里逐字节找标记。找到就非零退出。
"""
import argparse
import os
import stat
import secrets
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luoke_core.client import HubClient, HubError  # noqa: E402

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def send_all(sock: str, marker: str) -> list:
    mail = HubClient("luoke-mail/0.1", sock)
    ime = HubClient("luoke-ime/0.1", sock)
    done = []

    def chat(body, priority="user-waiting"):
        try:
            return [k for k, _ in mail.chat("luoke-mail.triage", body, priority=priority)]
        except HubError as e:
            return [e.code]
    msgs = [{"role": "system", "content": f"你是助手。回答里引用这个记号：{marker}"},
            {"role": "user", "content": f"请把记号 {marker} 原样写出来，再说一句话。"}]
    done.append(("normal", chat({"messages": msgs, "luoke_items": 1})))
    done.append(("json_schema", chat({"messages": msgs, "luoke_items": 1,
                                      "response_format": {"type": "json_schema", "json_schema": {"name": "m", "schema": SCHEMA}}})))
    done.append(("invalid", chat({"messages": [{"role": "tool", "content": marker}]})))
    done.append(("too_long", chat({"messages": [{"role": "user", "content": marker + "x" * 100000}]})))
    # 打断：起一条长的后台，再发"用户在等"
    res = {}

    def bg():
        for _ in range(120):
            try:
                out = [k for k, _ in mail.chat("luoke-mail.triage", {"messages": [{"role": "user", "content": f"围绕记号 {marker} 写 500 字。"}], "luoke_items": 1}, priority="background")]
                res["bg"] = out
                return
            except HubError as e:
                res["bg"] = [e.code]
                if e.code in ("busy", "paused"):
                    time.sleep(min(e.retry_after or 2, 10))
                    continue
                return
    for _ in range(80):   # 等交互窗口过去，后台才接
        st = mail.status()
        if not st["interactive"]:
            break
        time.sleep(2)
    th = threading.Thread(target=bg)
    th.start()
    for _ in range(300):   # 等后台真的进了引擎再发"用户在等"，这样打断的路径一定走到
        if mail.status()["job"]:
            break
        time.sleep(0.5)
    time.sleep(1)
    done.append(("user_waiting_during_bg", chat({"messages": msgs, "luoke_items": 1})))
    th.join()
    done.append(("background", res.get("bg")))
    for _ in range(15):
        try:
            r = ime.score("luoke-ime.rerank", f"记号{marker}之后", ["很好", "狠好"])
            done.append(("score", ["ok", r["cache_hit"]]))
            break
        except HubError as e:
            if e.code == "not_ready":
                time.sleep(2)
                continue
            done.append(("score", [e.code]))
            break
    # 联想和通用续写：前文也是用户正在打的字（V1 §9），同样不能落地
    for name, call in (("predict", lambda: ime.predict("luoke-ime.predict", f"记号{marker}之后要")),
                       ("continue", lambda: ime.continue_("luoke-ime.predict", f"记号{marker}之后要", k=4, max_tokens=4, stop=["。"])),
                       ("continue_invalid", lambda: ime.continue_("luoke-ime.predict", marker, k=0))):
        try:
            r = call()
            done.append((name, ["ok", len(r.get("branches", r.get("continuations", [])))]))
        except HubError as e:
            done.append((name, [e.code]))
    # 空闲任务：主控只记任务名、退出码、原因、秒数和配件自己保证不含内容的 stderr 尾巴（state.json），命令本身不经请求；扫描 env 时一并覆盖 state.json
    # 下面两条要实验室构建（--lab）的故障端点；正式构建没有
    try:
        mail._call("POST", "/lab/ceiling", {"gb": 1})            # 底座放不下：oMLX 把它卸掉，再请求就是内存保护拒绝
        time.sleep(2)
        done.append(("memory_guard_reject", chat({"messages": msgs, "luoke_items": 1})))
        mail._call("POST", "/lab/ceiling", {"gb": 100})
        time.sleep(2)
        done.append(("after_ceiling_restored", chat({"messages": msgs, "luoke_items": 1})))
        # 请求进行中杀引擎：已经发出输出 → engine_failed；主控几秒内重启引擎
        res2 = {}

        def long_chat():
            try:
                res2["out"] = chat({"messages": [{"role": "user", "content": f"围绕记号 {marker} 写 800 字。"}], "luoke_items": 1})
            except Exception as e:  # noqa: BLE001
                res2["out"] = [str(e)]
        th2 = threading.Thread(target=long_chat)
        th2.start()
        time.sleep(1.5)
        mail._call("POST", "/lab/engine/kill", {})
        th2.join()
        done.append(("engine_crash_mid_request", res2.get("out")))
        for _ in range(60):
            if mail.status().get("engine_ready"):
                break
            time.sleep(1)
        done.append(("after_engine_restart", chat({"messages": msgs, "luoke_items": 1})))
    except HubError as e:
        done.append(("lab_endpoints", [e.code, "（不是实验室构建，跳过两条故障路）"]))
    return done


def scan(root: str, marker: str) -> list:
    hits = []
    needle = marker.encode("utf-8")
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            if not stat.S_ISREG(os.lstat(p).st_mode):   # socket（run/hub.sock）、链接不是文件：读不了也不算泄露
                continue
            try:
                with open(p, "rb") as fh:          # 逐字节找，sqlite 的页、WAL、日志、缓存都一样找
                    if needle in fh.read():
                        hits.append(p)
                        continue
            except Exception as e:  # noqa: BLE001
                hits.append(f"{p} (读不了：{e})")
                continue
            if f.endswith((".sqlite3", ".db")):   # 再按表查一遍，防着压缩或编码过的列；打不开就算了（字节扫过了）
                try:
                    con = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)
                    for (name,) in con.execute("select name from sqlite_master where type='table'"):
                        for row in con.execute(f'select * from "{name}"'):
                            if any(isinstance(v, (str, bytes)) and (needle.decode() in v if isinstance(v, str) else needle in v) for v in row):
                                hits.append(p + f" (表 {name})")
                                break
                    con.close()
                except Exception:
                    pass
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--env", required=True, help="主控环境目录（含 logs/ 和引擎的 base_path）")
    ap.add_argument("--extra", action="append", default=[], help="再扫这些目录")
    a = ap.parse_args()
    marker = "LUOKEMARK" + secrets.token_hex(6).upper()
    print("marker:", marker)
    for name, outcome in send_all(a.socket, marker):
        print(f"  {name}: {outcome}")
    time.sleep(3)
    hits = []
    for root in [a.env] + a.extra:
        hits += scan(root, marker)
    if hits:
        print("泄露：标记出现在")
        for h in hits:
            print("  ", h)
        return 1
    print(f"通过：{a.env} 及 {len(a.extra)} 个额外目录里没有标记")
    return 0


if __name__ == "__main__":
    sys.exit(main())
