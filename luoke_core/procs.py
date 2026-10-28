"""按内核给的事实认进程：可执行文件的真实路径（libproc proc_pidpath）、原样的 argv（sysctl KERN_PROCARGS2）、启动时间。

不解析 ps 的文字（10-19：从 launchd 起的主控没有 UTF-8 locale，ps 把中文 .app 路径印成转义，按路径一个都找不到）。
只用标准库（ctypes）。读不到的进程（别的用户的、刚退出的）当作不存在。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from typing import Optional

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib", use_errno=True)
_PATH_MAX = 4096
_PROC_PIDTBSDINFO = 3
_CTL_KERN, _KERN_PROCARGS2, _KERN_ARGMAX = 1, 49, 8


def pids() -> list:
    n = _libc.proc_listallpids(None, 0)
    if n <= 0:
        return []
    buf = (ctypes.c_int * (n + 64))()
    n = _libc.proc_listallpids(buf, ctypes.sizeof(buf))
    return [p for p in buf[:max(n, 0)] if p > 0]


def exe_path(pid: int) -> Optional[str]:
    buf = ctypes.create_string_buffer(_PATH_MAX)
    n = _libc.proc_pidpath(int(pid), buf, _PATH_MAX)
    return buf.value.decode("utf-8", "replace") if n > 0 else None


def argv(pid: int) -> list:
    """进程启动时的 argv（不含环境变量）；读不到就是 []。"""
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, int(pid))
    size = ctypes.c_size_t(0)
    argmax = ctypes.c_int(0)
    sz = ctypes.c_size_t(ctypes.sizeof(argmax))
    if _libc.sysctl((ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX), 2, ctypes.byref(argmax), ctypes.byref(sz), None, 0) != 0:
        return []
    size.value = argmax.value
    buf = ctypes.create_string_buffer(size.value)
    if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0 or size.value < 4:
        return []
    raw = buf.raw[:size.value]
    argc = struct.unpack("i", raw[:4])[0]
    rest = raw[4:]
    end = rest.find(b"\0")                      # 先是可执行文件路径，再是若干 \0 填充，然后是 argv
    if end < 0:
        return []
    rest = rest[end:].lstrip(b"\0")
    parts = rest.split(b"\0")
    return [p.decode("utf-8", "replace") for p in parts[:argc]]


def start_time(pid: int) -> Optional[float]:
    """进程启动的时刻（秒，带微秒）：和 PID 一起才认得准一个进程（PID 会被复用）。"""
    # struct proc_bsdinfo：pbi_start_tvsec、pbi_start_tvusec 是最后两个 uint64
    buf = ctypes.create_string_buffer(136)
    n = _libc.proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, ctypes.c_uint64(0), buf, 136)
    if n < 136:
        return None
    sec, usec = struct.unpack("QQ", buf.raw[120:136])
    return sec + usec / 1e6


def inside(path: Optional[str], app: str) -> bool:
    """path 在这个 .app 里（按真实路径比，大小写照文件系统）。"""
    if not path:
        return False
    roots = {os.path.abspath(app).rstrip("/") + "/", os.path.realpath(app).rstrip("/") + "/"}
    return any(path.startswith(r) for r in roots)


def in_app(app: str) -> list:
    """可执行文件在这个 .app 里的进程，不含自己。"""
    me = os.getpid()
    return [p for p in pids() if p != me and inside(exe_path(p), app)]


def running_module(app: str, module: str) -> list:
    """用这个 .app 里的解释器跑着 `-m <module>` 的进程（例如主控 .app 里的 python 跑 luoke_mem）。
    只看可执行文件的位置不够：输入法的个人化组件也借主控的 python。"""
    out = []
    for p in in_app(app):
        a = argv(p)
        if any(a[i] == "-m" and a[i + 1] == module for i in range(len(a) - 1)):
            out.append(p)
    return out
