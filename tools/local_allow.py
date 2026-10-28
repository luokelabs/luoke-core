#!/usr/bin/env python3
"""本机放行（试用期，2026-10-17 定）：在开发机上让主控认一个还没进发布评测的新版配件，不用等下一版主控。

    python3 -B tools/local_allow.py add luoke-mail 0.13.18 --app ~/Applications/螺壳邮件助手.app --by luoke-mail --reason "…"
    python3 -B tools/local_allow.py remove luoke-mail 0.13.18 --by luoke-mail
    python3 -B tools/local_allow.py list

规矩：
- 只在内部版（internal-*）和本机版（local-*）主控上生效；beta 和公开发布不认这份文件。
- 只放行配方里已经有的配件的新版本，任务沿用配方里那个配件的任务；新配件、新任务要等新一版主控。
- 放行前核 .app：螺壳实验室签名、公证票据、清单里的配件名和版本对得上。
- 每次增删都追加进 logs/local-allow.log；主控用到放行条目时也记进 hub.log，设置里能看到放行了什么。
- 主控换成配方里已经有这个版本的新版后，条目自动不再算数（不用手动清）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from luoke_core import download  # noqa: E402
from luoke_core.accessories import MANIFEST_REL  # noqa: E402

ENV = os.environ.get("LUOKE_ENV_DIR") or os.path.expanduser("~/Library/Application Support/luoke-core")
HUB_APP = os.environ.get("LUOKE_HUB_APP") or next(
    (p for p in ("/Applications/螺壳.app", os.path.expanduser("~/Applications/螺壳.app"), os.path.expanduser("~/Applications/螺壳主控.app"))
     if os.path.isdir(p)), "/Applications/螺壳.app")      # 装在哪就用哪份（ADR 0012）
FILE = "local-verified.json"


def die(msg: str) -> None:
    print("local_allow: " + msg, file=sys.stderr)
    sys.exit(1)


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"allowed": []}
    except ValueError as e:
        die(f"{path} 读不了：{e}")


def save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def journal(line: str) -> None:
    os.makedirs(os.path.join(ENV, "logs"), exist_ok=True)
    with open(os.path.join(ENV, "logs", "local-allow.log"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")


def hub_info() -> tuple:
    res = os.path.join(HUB_APP, "Contents", "Resources")
    try:
        with open(os.path.join(res, "release.json"), encoding="utf-8") as f:
            tag = json.load(f).get("release") or ""
        with open(os.path.join(res, "recipe.json"), encoding="utf-8") as f:
            verified = json.load(f).get("verified") or {}
    except (OSError, ValueError) as e:
        die(f"读不了主控 {HUB_APP} 的发布清单或配方：{e}")
    return tag, verified


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["add", "remove", "list"])
    ap.add_argument("accessory", nargs="?")
    ap.add_argument("version", nargs="?")
    ap.add_argument("--app", help="add：已经签名公证好的配件 .app")
    ap.add_argument("--by", help="哪个会话放行的")
    ap.add_argument("--reason", default="", help="为什么现在要换（写进记录）")
    a = ap.parse_args()
    path = os.path.join(ENV, FILE)
    data = load(path)
    tag, verified = hub_info()
    if a.action == "list":
        print(f"主控 {tag}" + ("" if tag.startswith(("internal-", "local-")) else "（这一版不认本机放行）"))
        for e in data.get("allowed", []):
            live = e.get("accessory") in verified
            in_recipe = e.get("version") in (verified.get(e.get("accessory")) or {}).get("versions", [])
            print(f"  {e.get('accessory')} {e.get('version')}  by {e.get('by')} {e.get('at')}  {e.get('reason', '')}"
                  + ("  （配方里已有，不再算放行）" if in_recipe else "" if live else "  （配方里没有这个配件，不生效）"))
        return
    if not a.accessory or not a.version or not a.by:
        die("要写配件名、版本和 --by")
    if a.action == "remove":
        before = len(data.get("allowed", []))
        data["allowed"] = [e for e in data.get("allowed", []) if not (e.get("accessory") == a.accessory and e.get("version") == a.version)]
        if len(data["allowed"]) == before:
            die(f"没有 {a.accessory} {a.version} 这一条")
        save(path, data)
        journal(f"remove {a.accessory} {a.version} by {a.by}")
        print(f"已撤销 {a.accessory} {a.version}")
        return
    if not tag.startswith(("internal-", "local-")):
        die(f"主控是 {tag or '源码构建'}：只有内部版和本机版认本机放行")
    if a.accessory == "luoke-core" or a.accessory not in verified:
        die(f"{a.accessory} 不在主控配方里：新配件要等新一版主控")
    if a.version in verified[a.accessory].get("versions", []):
        die(f"{a.accessory} {a.version} 已经在配方里了，不用放行")
    if not a.app or not os.path.isdir(a.app):
        die("--app 要指向签名公证好的 .app")
    try:
        with open(os.path.join(a.app, MANIFEST_REL), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        die(f"{a.app} 里没有可读的 {MANIFEST_REL}：{e}")
    if m.get("accessory") != a.accessory or m.get("version") != a.version:
        die(f"{a.app} 的清单写的是 {m.get('accessory')} {m.get('version')}，不是 {a.accessory} {a.version}")
    try:
        download.verify_app(a.app, m.get("bundle_id"))
    except download.DownloadError as e:
        die(str(e))
    data["allowed"] = [e for e in data.get("allowed", []) if not (e.get("accessory") == a.accessory and e.get("version") == a.version)]
    data["allowed"].append({"accessory": a.accessory, "version": a.version, "by": a.by, "reason": a.reason,
                            "at": time.strftime("%Y-%m-%d %H:%M"), "app": os.path.abspath(a.app), "hub_release": tag})
    save(path, data)
    journal(f"add {a.accessory} {a.version} by {a.by} (hub {tag}, app {os.path.abspath(a.app)}): {a.reason}")
    print(f"已放行 {a.accessory} {a.version}（主控 {tag}）。主控下一次收到请求时就认；撤销用 remove。")


if __name__ == "__main__":
    main()
