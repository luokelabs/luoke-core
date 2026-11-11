"""主控起包外的程序时 disclaim（安全审查 H3；本体那一边是 app/Sources/LuokeCore/Spawn.swift）。

系统按「负责的进程」给授权：主控起的程序算在主控头上，主控在螺壳.app 里，就算螺壳，带着螺壳的完全磁盘访问。
包外的程序（引擎、打分进程、配件的命令、系统命令）经螺壳自己的 `--disclaim-exec` 起：它带着 disclaim 原地换成要起的程序，
pid 不变，subprocess 的管道、等待、结束照常用，这个程序就只有它自己的授权（没有）。

- 只在主控是螺壳.app 自带的 Python 时这样起（按 sys.executable 认，不看 LUOKE_HUB_APP）：开发时用别的 Python 跑，没有螺壳的授权可借，
  也不能拿测试用的 .app 当跳板（旧版本体不认这个开关，会整个起来）。
- 包里的程序 disclaim 了照样算螺壳（11-09 实测），所以 `--run-tool`（扫描）不经这里，装配件、改 .app 的（ditto、open、lsregister）也不经这里：
  系统只让同一个开发者的应用改它。
"""
from __future__ import annotations

import os
import plistlib
import sys
import time
from typing import Optional

from . import procs

FLAG = "--disclaim-exec"
_MARKER = os.sep + os.path.join("Contents", "Resources", "python", "bin") + os.sep


def trampoline(executable: Optional[str] = None) -> Optional[str]:
    """螺壳自己的可执行文件（`<app>/Contents/MacOS/<CFBundleExecutable>`）；主控不是螺壳自带的 Python 时是 None。"""
    exe = os.path.realpath(executable or sys.executable)
    if _MARKER not in exe:
        return None
    app = exe.split(_MARKER)[0]
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(name, str) or not name or "/" in name:
        return None
    path = os.path.join(app, "Contents", "MacOS", name)
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def disclaimed(argv: list, executable: Optional[str] = None) -> list:
    """给 subprocess 的 argv：主控是螺壳自带的 Python 时前面加上 `<螺壳> --disclaim-exec`。程序要给绝对路径。"""
    argv = list(argv)
    if not argv or not os.path.isabs(argv[0]):
        raise ValueError("disclaimed() needs an absolute program path")
    t = trampoline(executable)
    return [t, FLAG] + argv if t else argv


def wait_exec(pid: int, timeout: float = 2.0) -> bool:
    """经跳板起的进程：等它换成要起的程序（命令行里不再有跳板的开关），或者已经退出。pidfile 这类按可执行文件认进程的，要等换完再记。
    超时返回 False。"""
    deadline = time.monotonic() + timeout
    gone = 0
    while True:
        args = procs.argv(pid)
        if args and FLAG not in args[:3]:      # [螺壳, FLAG, …]
            return True
        # 读不到命令行、也读不到启动时刻：进程没了才算完。exec 到一半时两样都可能短暂读不到，
        # 只读一次就当它没了，会在换完之前记下跳板（/bin/sh），所以要连着几次都读不到
        gone = gone + 1 if not args and procs.start_time(pid) is None else 0
        if gone >= 3:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
