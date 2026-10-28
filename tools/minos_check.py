"""核对一个 .app（或目录）里所有 Mach-O 的最低 macOS 版本（发布前的一道关，2026-10-15 起）。

背景：在 macOS 27 的机器上编的东西，不指定部署目标就是 minos 27.0——Info.plist 写 LSMinimumSystemVersion 14.0 也没用，
26.x 的机器上 LaunchServices 拒开（"You can't use this version of the application"），动态库则加载不了。主控的启动器、
输入法自带的引擎都中过招。所以发布脚本对清单里每一项（主控、引擎包、各配件）都查一遍：任何一个 Mach-O 的 minos 高于
该项声称的最低 macOS（配件清单的 min_macos，没有就用 Info.plist 的 LSMinimumSystemVersion）就不出清单。

只用标准库：按魔数认 Mach-O（含 fat），读 LC_BUILD_VERSION（platform 1 = macOS）或 LC_VERSION_MIN_MACOSX。
    python3 tools/minos_check.py <某.app> [--max 14.0]     列出超过的；退出码 1 表示有超过的
"""
from __future__ import annotations

import os
import plistlib
import struct
import sys
from typing import Iterator, Optional

MH_MAGIC_64 = 0xFEEDFACF
MH_CIGAM_64 = 0xCFFAEDFE
MH_MAGIC = 0xFEEDFACE
MH_CIGAM = 0xCEFAEDFE
FAT_MAGIC = 0xCAFEBABE
FAT_CIGAM = 0xBEBAFECA
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
PLATFORM_MACOS = 1


def _ver(v: int) -> str:
    return f"{v >> 16}.{(v >> 8) & 0xFF}" + (f".{v & 0xFF}" if v & 0xFF else "")


def version_tuple(s: str) -> tuple:
    return tuple(int(x) for x in str(s).split(".") if x.isdigit())


def _slice_minos(data: bytes, off: int) -> Optional[str]:
    """一个（非 fat 的）Mach-O 切片的 macOS minos；没有版本载荷（例如只给别的平台）返回 None。"""
    magic = struct.unpack_from(">I", data, off)[0]
    if magic in (MH_MAGIC_64, MH_MAGIC):
        end = ">"
    elif magic in (MH_CIGAM_64, MH_CIGAM):
        end = "<"
    else:
        return None
    is64 = magic in (MH_MAGIC_64, MH_CIGAM_64)
    ncmds = struct.unpack_from(end + "I", data, off + 16)[0]
    p = off + (32 if is64 else 28)
    for _ in range(ncmds):
        if p + 8 > len(data):
            break
        cmd, size = struct.unpack_from(end + "II", data, p)
        if size < 8:
            break
        if cmd == LC_BUILD_VERSION and size >= 24:
            platform, minos = struct.unpack_from(end + "II", data, p + 8)
            if platform == PLATFORM_MACOS:
                return _ver(minos)
        elif cmd == LC_VERSION_MIN_MACOSX and size >= 16:
            return _ver(struct.unpack_from(end + "I", data, p + 8)[0])
        p += size
    return None


def macho_minos(path: str) -> Optional[str]:
    """文件是 Mach-O 就返回它的 macOS 最低版本（fat 取各切片里最高的）；不是 Mach-O 返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if len(head) < 8:
                return None
            magic = struct.unpack(">I", head[:4])[0]
            if magic not in (MH_MAGIC_64, MH_CIGAM_64, MH_MAGIC, MH_CIGAM, FAT_MAGIC, FAT_CIGAM):
                return None
            f.seek(0)
            data = f.read()
    except OSError:
        return None
    if magic in (FAT_MAGIC, FAT_CIGAM):
        n = struct.unpack_from(">I", data, 4)[0]
        best = None
        for i in range(min(n, 16)):
            _, _, off, _, _ = struct.unpack_from(">IIIII", data, 8 + i * 20)
            if off + 32 <= len(data):
                v = _slice_minos(data, off)
                if v and (best is None or version_tuple(v) > version_tuple(best)):
                    best = v
        return best
    return _slice_minos(data, 0)


def iter_machos(root: str) -> Iterator[tuple]:
    """目录下所有 Mach-O：(相对路径, minos)。按魔数认，不看扩展名。"""
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            if os.path.islink(p):
                continue
            v = macho_minos(p)
            if v is not None:
                yield os.path.relpath(p, root), v


def declared_min(app: str) -> Optional[str]:
    """这个 .app 声称支持的最低 macOS：配件清单的 min_macos 优先，再看 Info.plist 的 LSMinimumSystemVersion。"""
    mp = os.path.join(app, "Contents", "Resources", "luoke-accessory.json")
    if os.path.exists(mp):
        import json
        try:
            with open(mp, encoding="utf-8") as f:
                v = json.load(f).get("min_macos")
            if v:
                return str(v)
        except (OSError, ValueError):
            pass
    plist = os.path.join(app, "Contents", "Info.plist")
    try:
        with open(plist, "rb") as f:
            v = plistlib.load(f).get("LSMinimumSystemVersion")
        return str(v) if v else None
    except (OSError, ValueError):
        return None


def offenders(root: str, max_version: str) -> list:
    """minos 高于 max_version 的 Mach-O：[(相对路径, minos)]。"""
    mx = version_tuple(max_version)
    return [(p, v) for p, v in iter_machos(root) if version_tuple(v) > mx]


def check_app(app: str, max_version: Optional[str] = None) -> tuple:
    """返回 (声称的最低版本, 超过的列表)。max_version 没给就用 .app 自己声称的；两样都没有就按 14.0（打包脚本的默认）。"""
    limit = max_version or declared_min(app) or "14.0"
    return limit, offenders(app, limit)


def main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path")
    ap.add_argument("--max", default=None, help="允许的最高 minos（默认取 .app 声称的最低 macOS）")
    ap.add_argument("--list", action="store_true", help="列出全部 Mach-O 和它们的 minos")
    a = ap.parse_args(argv)
    if a.list:
        for p, v in sorted(iter_machos(a.path)):
            print(f"{v:>7}  {p}")
    limit, bad = check_app(a.path, a.max)
    if bad:
        print(f"{a.path}：声称最低 macOS {limit}，但这些 Mach-O 要更高的系统：")
        for p, v in sorted(bad):
            print(f"  {v:>7}  {p}")
        return 1
    print(f"{a.path}：所有 Mach-O 的 minos 都不高于 {limit}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
