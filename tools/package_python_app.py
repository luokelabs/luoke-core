#!/usr/bin/env python3
"""把一个 Python 配件打成 .app：自带一份 python-build-standalone，加配件清单，签名。

    python3 tools/package_python_app.py --manifest <luoke-accessory.json> --package <含 __init__.py 的包目录> \
        --out <输出目录> [--package <另一个包>] [--sign adhoc|none] [--check] [--install]

- 清单（V1 §8）给出 bundle id、名字、版本、入口（交给自带 Python 的参数）、LSUIElement；脚本不另要这些参数。
  可选 "icon": .icns 文件的路径，相对清单文件所在目录（2026-10-15 应 luoke-mail 的需求加）：拷成 Contents/Resources/AppIcon.icns，
  Info.plist 写 CFBundleIconFile=AppIcon；声明了就必须在、必须是 icns。
- 产出布局（主控和配件都按这个找东西）：
    <名字>.app/Contents/Info.plist
    <名字>.app/Contents/MacOS/<配件标识>            启动器：默认是编译版（tools/launcher/Launcher.swift）——当 .app 的进程，
                                                   入口当子进程；再次打开时再跑一次入口加 --reopen；--notify 发通知；
                                                   主控登录时拉起会传 --resident。--launcher shell 则退回只 exec 的脚本
    <名字>.app/Contents/Resources/luoke-accessory.json
    <名字>.app/Contents/Resources/licenses/            自带运行时的第三方许可（THIRD-PARTY-LICENSES.txt + LICENSE.*.txt）
    <名字>.app/Contents/Resources/python/bin/python3  python-build-standalone（install_only）
    <名字>.app/Contents/Resources/python/lib/python3.X/site-packages/<包>   配件的包就装在这里，`-m <包>` 直接可用
- Python 运行时按版本钉死（文件名 + sha256 写在下面的表里），第一次从 GitHub 下载到 ~/Library/Caches/luoke-core/，校验后解开复用。
- --sign adhoc 是开发版（ad-hoc 签名，只在本机能跑）；正式版的 Developer ID 签名和公证在发布脚本里做（V1 §7、§11），这里不做。
- --check：签名前用自带的 python（-B）导入每个包，签名后 codesign --verify，失败就非零退出。
- 打包时把标准库和配件的包都预编译成 .pyc，运行时就没有东西要写进 .app。谁拿自带的 python 跑清单里的命令（主控跑 ui、forget-tool），仍要在环境里设 PYTHONDONTWRITEBYTECODE=1（子进程也继承；只加 -B 管不到它再起的进程）：往 .app 里写 __pycache__ 会弄坏签名的封印。
- --install：拷到 ~/Applications/<名字>.app（已有的先确认没有进程在跑，再整个替换），然后 lsregister。
只用标准库，/usr/bin/python3（3.9）就能跑。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
from typing import Optional
import sys
import tarfile
import tempfile
import urllib.request

RUNTIMES = {
    # 版本标签: (文件名, sha256)。来源：https://github.com/astral-sh/python-build-standalone/releases
    "3.12.15+20261009": (
        "cpython-3.12.15+20261009-aarch64-apple-darwin-install_only.tar.gz",
        "83dcb04777c798bd95f3687acf9ae84b09860272ee6bdfb05be8bcf9447f75b8",
    ),
}
DEFAULT_RUNTIME = "3.12.15+20261009"
RELEASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/{name}"
CACHE = os.path.expanduser("~/Library/Caches/luoke-core/python-build-standalone")
MIN_MACOS = "14.0"


def die(msg: str) -> None:
    sys.exit("package_python_app: " + msg)


def resolve_icon(manifest_path: str, manifest: dict) -> Optional[str]:
    """清单可选字段 icon：.icns 文件，路径相对清单文件所在目录（绝对路径也行）。声明了就必须在、必须是 icns（文件头 "icns"）。"""
    rel = manifest.get("icon")
    if not rel:
        return None
    path = rel if os.path.isabs(rel) else os.path.join(os.path.dirname(os.path.abspath(manifest_path)), rel)
    if not os.path.isfile(path):
        raise SystemExit(f"清单的 icon 不存在：{path}")
    with open(path, "rb") as f:
        if f.read(4) != b"icns":
            raise SystemExit(f"清单的 icon 不是 .icns 文件（文件头不是 icns）：{path}")
    return path


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    for k in ("contract", "accessory", "bundle_id", "name", "version", "type", "entry"):
        if k not in m:
            die(f"清单缺字段 {k!r}：{path}")
    if m["contract"] != 1:
        die(f"清单的 contract 是 {m['contract']!r}，这个脚本只认 1")
    if not isinstance(m["entry"], list) or not all(isinstance(x, str) for x in m["entry"]):
        die("清单的 entry 要是字符串列表（交给自带 python 的参数）")
    if m["type"] not in ("app", "component", "input-method"):
        die(f"清单的 type 不认识：{m['type']!r}")
    acc = m["accessory"]
    if not acc or not all(c.isalnum() or c in "-_." for c in acc) or not acc.isascii():
        die(f"配件标识只能是 ASCII 字母数字和 -_.：{acc!r}")
    return m


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_runtime(version: str, cache: str) -> str:
    """下载（如需要）、校验、解开 python-build-standalone，返回解开后的 python/ 目录。"""
    if version not in RUNTIMES:
        die(f"没有钉住的运行时版本 {version!r}，可选：{', '.join(RUNTIMES)}")
    name, want = RUNTIMES[version]
    tag = version.split("+", 1)[1]
    os.makedirs(cache, exist_ok=True)
    tarball = os.path.join(cache, name)
    if not os.path.exists(tarball) or sha256_of(tarball) != want:
        url = RELEASE_URL.format(tag=tag, name=name)
        print(f"下载 {url}", flush=True)
        tmp = tarball + ".part"
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        got = sha256_of(tmp)
        if got != want:
            os.remove(tmp)
            die(f"运行时校验失败：{name}\n  期望 {want}\n  实得 {got}")
        os.replace(tmp, tarball)
    extracted = os.path.join(cache, version)
    marker = os.path.join(extracted, ".ok")
    if not os.path.exists(marker):
        shutil.rmtree(extracted, ignore_errors=True)
        os.makedirs(extracted)
        with tarfile.open(tarball) as t:
            for member in t.getmembers():
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    die(f"运行时压缩包里有可疑路径：{member.name}")
            t.extractall(extracted)
        if not os.path.exists(os.path.join(extracted, "python", "bin", "python3")):
            die("解开的运行时里没有 python/bin/python3")
        open(marker, "w").close()
    return os.path.join(extracted, "python")


def site_packages(python_dir: str) -> str:
    lib = os.path.join(python_dir, "lib")
    vers = [d for d in os.listdir(lib) if d.startswith("python3.") and os.path.isdir(os.path.join(lib, d))]
    if len(vers) != 1:
        die(f"运行时的 lib 下找不到唯一的 python3.X：{vers}")
    return os.path.join(lib, vers[0], "site-packages")


def copy_package(src: str, dst_site: str) -> str:
    src = os.path.abspath(src.rstrip("/"))
    if not os.path.exists(os.path.join(src, "__init__.py")):
        die(f"不是包（没有 __init__.py）：{src}")
    name = os.path.basename(src)
    dst = os.path.join(dst_site, name)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".git*"))
    return name


LAUNCHER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher", "Launcher.swift")


def build_native_launcher(path: str, cache: str) -> bool:
    """编译版启动器（tools/launcher/Launcher.swift）：再次打开、通知、--resident。编译产物按源码哈希缓存。"""
    swiftc = subprocess.run(["/usr/bin/xcrun", "-f", "swiftc"], capture_output=True, text=True).stdout.strip()
    if not swiftc or not os.path.exists(LAUNCHER_SRC):
        return False
    target = f"arm64-apple-macosx{MIN_MACOS}"   # 不指定的话部署目标是打包机的 macOS 版本（2026-10-15 在 27.0 上打的包，26.2 的机器上 LaunchServices 拒开："You can't use this version of the application"）
    key = sha256_of(LAUNCHER_SRC)[:12] + "-" + target.replace("arm64-apple-", "")
    built = os.path.join(cache, f"launcher-{key}")
    if not os.path.exists(built):
        r = subprocess.run([swiftc, "-O", "-target", target, "-o", built, LAUNCHER_SRC, "-framework", "AppKit", "-framework", "UserNotifications"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("编译启动器失败，退回 shell 启动器：\n" + r.stderr[-1500:])
            return False
    shutil.copyfile(built, path)
    os.chmod(path, 0o755)
    return True


def write_launcher(path: str, entry: list[str]) -> None:
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in entry)
    script = (
        "#!/bin/sh\n"
        "# 由 luoke-core 的打包脚本生成：只做一件事，用自带的 Python 跑清单里的入口。\n"
        'here="$(cd "$(dirname "$0")/.." && pwd)"\n'
        "export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1\n"
        "unset PYTHONPATH PYTHONHOME\n"
        "cd /\n"
        f'exec "$here/Resources/python/bin/python3" {quoted} "$@"\n'
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def build(manifest_path: str, manifest: dict, packages: list[str], runtime_dir: str, out_dir: str,
          launcher: str = "native", cache: str = CACHE, runtime_version: str = DEFAULT_RUNTIME) -> str:
    app_name = manifest["name"]
    exe = manifest["accessory"]
    app = os.path.join(out_dir, app_name + ".app")
    if os.path.exists(app):
        shutil.rmtree(app)
    contents = os.path.join(app, "Contents")
    os.makedirs(os.path.join(contents, "MacOS"))
    os.makedirs(os.path.join(contents, "Resources"))
    # 运行时
    py_dst = os.path.join(contents, "Resources", "python")
    shutil.copytree(runtime_dir, py_dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "test", "tests"))
    # 包
    site = site_packages(py_dst)
    os.makedirs(site, exist_ok=True)
    names = [copy_package(p, site) for p in packages]
    # 预编译：标准库和配件的包都先生成 .pyc，封印之后运行时就没有东西要写进 .app
    py = os.path.join(py_dst, "bin", "python3")
    lib = os.path.dirname(site)
    r = subprocess.run([py, "-B", "-m", "compileall", "-q", "-j", "0", lib], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")})
    if r.returncode != 0:
        die("预编译失败：\n" + r.stderr[-2000:])
    # 第三方许可：自带运行时的组件许可（tools/runtime-licenses/<版本>/），公开发布要带着
    lic_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime-licenses", runtime_version)
    if os.path.isdir(lic_src):
        shutil.copytree(lic_src, os.path.join(contents, "Resources", "licenses"))
    else:
        print(f"注意：没有 {lic_src}，.app 里不带运行时的第三方许可")
    # 清单、启动器、plist
    shutil.copyfile(manifest_path, os.path.join(contents, "Resources", "luoke-accessory.json"))
    launcher_path = os.path.join(contents, "MacOS", exe)
    kind = "native" if launcher == "native" and build_native_launcher(launcher_path, cache) else "shell"
    if kind == "shell":
        write_launcher(launcher_path, manifest["entry"])
    print(f"启动器：{kind}")
    icon = resolve_icon(manifest_path, manifest)   # 应用图标（顶层 ADR 0017：封闭测试前各应用换上）
    if icon:
        shutil.copyfile(icon, os.path.join(contents, "Resources", "AppIcon.icns"))
    plist = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": app_name,
        "CFBundleExecutable": exe,
        "CFBundleIdentifier": manifest["bundle_id"],
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": str(manifest["version"]),
        "CFBundleVersion": str(manifest["version"]),
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSUIElement": bool(manifest.get("ui_element", False)),
        "NSHighResolutionCapable": True,
        "LuokeAccessory": manifest["accessory"],
        "LuokeContract": 1,
    }
    if icon:
        plist["CFBundleIconFile"] = "AppIcon"
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(plist, f)
    with open(os.path.join(contents, "PkgInfo"), "w") as f:
        f.write("APPL????")
    print(f"已打包 {app}（包：{', '.join(names)}）")
    return app


def sign(app: str, how: str) -> None:
    if how == "none":
        return
    if how == "adhoc":
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], check=True)
        print("已 ad-hoc 签名（开发版，只在本机能跑）")
    else:
        die(f"--sign 只支持 adhoc 或 none（正式签名在发布脚本里做）：{how!r}")


def check_imports(app: str, packages: list[str]) -> None:
    """签名之前做：用自带的 python 导入每个包。一律 -B，免得往 .app 里写 __pycache__ 弄坏封印。"""
    py = os.path.join(app, "Contents", "Resources", "python", "bin", "python3")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "PYTHONNOUSERSITE": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    for p in packages:
        name = os.path.basename(os.path.abspath(p.rstrip("/")))
        r = subprocess.run([py, "-B", "-c", f"import {name}; print(getattr({name}, '__version__', ''))"],
                           env=env, cwd="/", capture_output=True, text=True)
        if r.returncode != 0:
            die(f"自带的 python 导入 {name} 失败：\n{r.stderr}")
        print(f"检查：import {name} 正常" + (f"（{r.stdout.strip()}）" if r.stdout.strip() else ""))
    launcher = os.path.join(app, "Contents", "MacOS")
    exe = os.listdir(launcher)[0]
    if not os.access(os.path.join(launcher, exe), os.X_OK):
        die("启动器不可执行")


def verify_signature(app: str) -> None:
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", app], capture_output=True, text=True)
    if r.returncode != 0:
        die("codesign --verify 失败：\n" + r.stderr)
    print("检查：签名有效")


def install(app: str) -> str:
    dst = os.path.join(os.path.expanduser("~/Applications"), os.path.basename(app))
    if os.path.exists(dst):
        r = subprocess.run(["pgrep", "-f", dst + "/"], capture_output=True, text=True)
        if r.stdout.strip():
            die(f"{dst} 里还有进程在跑（pid {r.stdout.split()}），不替换；等它退出再装")
        shutil.rmtree(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copytree(app, dst, symlinks=True)
    lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
                  "/Support/lsregister")
    subprocess.run([lsregister, "-f", dst], check=False)
    print(f"已装到 {dst}")
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help="配件清单 luoke-accessory.json（V1 §8）")
    ap.add_argument("--package", action="append", required=True, help="要装进去的包目录，可多个")
    ap.add_argument("--out", required=True, help="输出目录，产出 <名字>.app")
    ap.add_argument("--python", default=DEFAULT_RUNTIME, help=f"python-build-standalone 版本（默认 {DEFAULT_RUNTIME}）")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--sign", default="adhoc", help="adhoc（默认，开发版）或 none")
    ap.add_argument("--launcher", default="native", choices=["native", "shell"],
                    help="native（默认）：编译版启动器，支持再次打开、通知、--resident；shell：只 exec 的脚本")
    ap.add_argument("--check", action="store_true", help="打完包用自带 python 导入每个包、验证签名")
    ap.add_argument("--install", action="store_true", help="拷到 ~/Applications 并注册")
    a = ap.parse_args()
    a.manifest, a.out = os.path.abspath(a.manifest), os.path.abspath(a.out)
    a.package = [os.path.abspath(p) for p in a.package]

    manifest = load_manifest(a.manifest)
    runtime = ensure_runtime(a.python, a.cache)
    os.makedirs(a.out, exist_ok=True)
    app = build(a.manifest, manifest, a.package, runtime, a.out, launcher=a.launcher, cache=a.cache, runtime_version=a.python)
    if a.check:
        check_imports(app, a.package)
    sign(app, a.sign)
    if a.check and a.sign != "none":
        verify_signature(app)
    if a.install:
        install(app)


if __name__ == "__main__":
    main()
