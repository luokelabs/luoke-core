"""放置、移走、删除 .app：LaunchServices 的登记只在这里管（Swift 那边是 AppPlacement.swift，同一套规矩）。

规矩（10-17 到 10-19 在开发机上先后栽过三次，见 git log）：
- LaunchServices 会跟着 .app 的挪动更新它记的路径，所以挪之前在旧路径注销、挪之后在新路径再按要求登记或注销；
- 核签名（codesign、stapler）时，暂存目录里的 .app 也可能被登记，放到最终位置时一并注销旧路径；
- 不该被"按 bundle id 打开"的副本（备份、回滚用的旧版、引擎包、实验室构建）放好后一律注销，放在 *.noindex 里。
"""
from __future__ import annotations

import os
import shutil
import subprocess

LSREGISTER = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"


def _ls(flag: str, path: str) -> None:
    if os.path.exists(LSREGISTER):
        subprocess.run([LSREGISTER, flag, path], check=False, capture_output=True)


def _move(src: str, dst: str) -> None:
    try:
        os.replace(src, dst)
    except OSError:          # 跨卷（EXDEV）
        shutil.move(src, dst)


def move_app(src: str, dst: str, register: bool) -> str:
    """把 src 挪到 dst（dst 不能已经存在）。register=True 登记新位置（用户要按 bundle id 打开的那份），否则注销。"""
    _ls("-u", src)
    _move(src, dst)
    _ls("-f" if register else "-u", dst)
    return dst


def retire_app(app: str, keep_dir: str, name: str = "") -> str:
    """把一份 .app 挪进 keep_dir（回滚用、不该被打开的位置，通常是 *.noindex）并注销；那里原来同名的旧备份先删。返回新路径。
    name 给了就用这个名字放（例如旧的记忆 app 不管原来叫什么都放成 螺壳记忆.app）。"""
    os.makedirs(keep_dir, exist_ok=True)
    keep = os.path.join(keep_dir, name or os.path.basename(app.rstrip("/")))
    if os.path.lexists(keep):
        remove_app(keep)
    return move_app(app, keep, register=False)


def unregister_dir(keep_dir: str) -> list:
    """keep_dir（previous.noindex 这类回滚备份目录）里的每份 .app 都从 LaunchServices 注销。挪进去时注销过，但之后还会被系统重新
    登记（luoke-ime 10-22 在开发机上发现 previous.noindex 里的 LuokeIME.app 是登记着的，和 luoke-ime#15 同一类：同一个 bundle id
    两份登记），所以主控启动、每次换装和回滚之后都再扫一遍。返回注销了的路径。"""
    try:
        names = sorted(os.listdir(keep_dir))
    except OSError:
        return []
    done = []
    for n in names:
        p = os.path.join(keep_dir, n)
        if n.endswith(".app") and os.path.isdir(p) and not os.path.islink(p):
            _ls("-u", p)
            done.append(p)
    return done


def remove_app(app: str) -> None:
    """注销再删。删不掉抛 OSError。"""
    _ls("-u", app)
    if os.path.islink(app) or not os.path.isdir(app):
        os.remove(app)
    else:
        shutil.rmtree(app)


def swap_app(a: str, b: str) -> None:
    """两个位置对调（配件回滚：a 是现在用的，b 是 *.noindex 里的旧版；对调后 a 处登记、b 处注销）。中途失败尽量把 a 放回原处再抛。"""
    tmp = os.path.join(os.path.dirname(b), ".swap-" + os.path.basename(a.rstrip("/")))
    if os.path.lexists(tmp):
        remove_app(tmp)
    _ls("-u", a)
    _ls("-u", b)
    try:
        _move(a, tmp)
        _move(b, a)
        _move(tmp, b)
    except OSError:
        if not os.path.exists(a) and os.path.exists(tmp):
            _move(tmp, a)
        _ls("-f", a)
        raise
    _ls("-u", b)
    _ls("-f", a)


def registered_copies(bundle_id: str) -> list:
    """LaunchServices 登记着的、这个 bundle id 的所有 .app 路径（lsregister -dump 里 identifier 往上最近的 path:），加上废纸篓里同 bundle id 的。"""
    import plistlib
    import re
    out: list = []
    if os.path.exists(LSREGISTER):
        r = subprocess.run([LSREGISTER, "-dump"], capture_output=True, text=True, errors="replace")
        path = None
        for line in r.stdout.splitlines():
            if line.startswith("path:"):
                path = re.sub(r" \(0x[0-9a-f]+\)$", "", line[5:].strip())
            elif re.match(r"^identifier:\s*" + re.escape(bundle_id) + r"\s*$", line) and path:
                out.append(path)
    trash = os.path.expanduser("~/.Trash")
    try:
        names = os.listdir(trash)
    except OSError:
        names = []
    for n in names:
        p = os.path.join(trash, n)
        if not n.endswith(".app") or os.path.islink(p):
            continue
        try:
            with open(os.path.join(p, "Contents", "Info.plist"), "rb") as f:
                if plistlib.load(f).get("CFBundleIdentifier") == bundle_id:
                    out.append(p)
        except (OSError, ValueError):
            continue
    return list(dict.fromkeys(out))


def unregister_other_copies(bundle_id: str, keep: str) -> list:
    """同一个 bundle id 的别的副本（卸载时移进废纸篓的旧包、DMG 里那份……）从 LaunchServices 注销，keep 那份不动。
    要在放新包**之前**做：先放后注销不管用，系统（输入源这类）的绑定不跟着变（luoke-ime#15，macOS 27 上：装好了切不过去）。"""
    keep_real = os.path.realpath(keep)
    done = []
    for p in registered_copies(bundle_id):
        if os.path.realpath(p) == keep_real:
            continue
        _ls("-u", p)
        done.append(p)
    return done
