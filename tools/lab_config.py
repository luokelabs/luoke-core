"""实验室的配置（不进公开的树）：别的档在哪台机器上评、出版的请求 issue、谁能发 /release。

放在 ~/.config/luoke/lab.json（600），LUOKE_LAB_CONFIG 可以指别处；正本在私有的 ops 仓库 lab/lab.json，改了两边同步。
只放主机、路径、账号名这类，不放令牌、密码。没有这个文件时：内部版只能评本机这一档，出版守护进程不知道看哪个 issue。

  {"machines": {"16": {"host": ..., "core": ..., "models": ..., "engine_app": ..., "repos": {...}, "corpus": ...}, "32": {...}},
   "release": {"issue": "<owner>/<repo>#<n>", "allowed": ["<GitHub 账号>"], "owner": "<GitHub 账号>"}}
"""
import json
import os

PATH = os.path.expanduser(os.environ.get("LUOKE_LAB_CONFIG") or "~/.config/luoke/lab.json")


def load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def machines() -> dict:
    """档 → 那台机器（评测时 rsync 过去、远端跑）。extra_sync 不写就是空。"""
    return {str(t): {"extra_sync": {}, **m} for t, m in (load().get("machines") or {}).items()}


def release() -> dict:
    return load().get("release") or {}
