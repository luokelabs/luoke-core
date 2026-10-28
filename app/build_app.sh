#!/bin/sh
# 打开发版「螺壳.app」：swift build 出本体，装进自带的 Python 和 luoke_core，配方，ad-hoc 签名。
#   app/build_app.sh [输出目录] [--install]   默认输出 app/build.noindex（.noindex：Spotlight 不索引，这份副本不会按 bundle id 登记进 LaunchServices）；--install 再拷到 ~/Applications 并注册
#   环境变量：LUOKE_RECIPE_FILE（配方，默认 recipes/dev.json）、LUOKE_RELEASE_FILE（发布清单，放进 Resources/release.json）、LUOKE_BUNDLE_ID
# 自带 Python 和 luoke_core 的放法与 tools/package_python_app.py 一致（Resources/python，包装进 site-packages，预编译）。
set -eu
set -o pipefail     # 下面 `swift build | tail`：编译失败也要停下（10-23 发现：之前取的是 tail 的退出码，编不过照样打包，用的是 .build 里旧的二进制）
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
out=${1:-"$here/build.noindex"}
case "$out" in /*) ;; *) out="$(pwd)/$out" ;; esac     # 相对路径按调用时的目录算，别跟着下面的 cd 跑偏
# 默认是开发版的身份（10-23 定）：bundle id ai.luoke.core.dev、名字「螺壳（开发）」、数据在 luoke-core-dev。
# 开发版是 ad-hoc 签名、没有开发者团队，系统对它的授权记录（例如 App Management 的拒绝）会按 bundle id 存下来；
# 和正式版共用 ai.luoke.core 时，记录落到了正式版头上（10-22 开发机上升级被挡、报"已损坏"）。
# 正式版由 tools/release.py 传 LUOKE_BUNDLE_ID=ai.luoke.core（2026-10-13 定，发布后不改）；实验室构建传 ai.luoke.core.lab
bundle_id=${LUOKE_BUNDLE_ID:-ai.luoke.core.dev}
case "$bundle_id" in *.dev) name="螺壳（开发）" ;; *) name="螺壳" ;; esac
version=$(/usr/bin/python3 -c "import sys; sys.path.insert(0, '$root'); import luoke_core; print(luoke_core.__version__)")
# Finder「显示简介」看到的版本（CFBundleShortVersionString）写发布号（beta1、internal-1027；10-27），没有发布清单的开发版写主控版本号；
# 主控版本号放 CFBundleVersion，程序里读它（HubProcess.version）
shown=$version
if [ -n "${LUOKE_RELEASE_FILE:-}" ]; then shown=$(/usr/bin/python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['release'])" "$LUOKE_RELEASE_FILE"); fi

cd "$here" && swift build -c release 2>&1 | tail -3
bin="$here/.build/release/LuokeCore"
[ -x "$bin" ] || { echo "没编出来：$bin" >&2; exit 1; }

app="$out/$name.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin" "$app/Contents/MacOS/LuokeCore"
cp "${LUOKE_RECIPE_FILE:-$root/recipes/dev.json}" "$app/Contents/Resources/recipe.json"      # 发布脚本传正式配方
if [ -n "${LUOKE_RELEASE_FILE:-}" ]; then cp "$LUOKE_RELEASE_FILE" "$app/Contents/Resources/release.json"; fi   # 发布清单：首次运行按它下载
mkdir -p "$app/Contents/Resources/bin"
cp "$here/Resources/luoke" "$app/Contents/Resources/bin/luoke"     # 给高级用户的命令行：luoke launch pi 等（luoke_core/cli.py）
chmod 755 "$app/Contents/Resources/bin/luoke"
cp -R "$here/Resources/ui" "$app/Contents/Resources/ui"            # 安装、升级窗口用的品牌素材（luoke-brand ui/public/img：海螺、应用图标、吉祥物）

# 自带 Python + luoke_core：借 package_python_app 的运行时下载和预编译，用一份临时清单
tmp=$(mktemp -d)
cat > "$tmp/manifest.json" <<EOF
{"contract": 1, "accessory": "luoke-core", "bundle_id": "$bundle_id", "name": "$name", "version": "$version",
 "type": "app", "entry": ["-m", "luoke_core", "version"], "resident": true, "memory": false, "ui_element": true, "tasks": [], "data": []}
EOF
# 螺壳记忆并进主控（顶层 ADR 0003 修订，2026-10-16 定进 beta1）：luoke-mem 的包和清单一起打进来。代码归 luoke-mem，主控只打包、启动。
#   LUOKE_MEM_SRC：luoke-mem 仓库（发布脚本传钉住的那份）；默认并排的 ../luoke-mem；没有就不带记忆（开发构建会说一声）
mem_src=${LUOKE_MEM_SRC:-$root/../luoke-mem}
mem_pkg=""
if [ -f "$mem_src/luoke_mem/__init__.py" ] && [ -f "$mem_src/packaging/accessory.json" ]; then mem_pkg="--package $mem_src/luoke_mem"
else echo "注意：没找到 luoke-mem（${mem_src}），这一份主控不带螺壳记忆" >&2; fi
/usr/bin/python3 "$root/tools/package_python_app.py" --manifest "$tmp/manifest.json" --package "$root/luoke_core" $mem_pkg --out "$tmp/pkg" --sign none >/dev/null
mv "$tmp/pkg/$name.app/Contents/Resources/python" "$app/Contents/Resources/python"
[ -d "$tmp/pkg/$name.app/Contents/Resources/licenses" ] && mv "$tmp/pkg/$name.app/Contents/Resources/licenses" "$app/Contents/Resources/licenses"
rm -rf "$tmp"
if [ -n "$mem_pkg" ]; then
  mkdir -p "$app/Contents/Resources/memory"
  cp "$mem_src/packaging/accessory.json" "$app/Contents/Resources/memory/luoke-accessory.json"   # 原样拷（V1 §6「记忆并进主控」）
  echo "带上螺壳记忆 $(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$mem_src/packaging/accessory.json")（${mem_src}）"
fi

# 应用图标：app/AppIcon.icns（或 LUOKE_ICON_FILE 指的 .icns）有就打进去（顶层 ADR 0008 修订：图标由 luoke-community 出稿、定）
icon=${LUOKE_ICON_FILE:-$here/AppIcon.icns}
if [ -f "$icon" ]; then cp "$icon" "$app/Contents/Resources/AppIcon.icns"; has_icon=1; else has_icon=0; fi
/usr/bin/python3 - "$app/Contents/Info.plist" "$bundle_id" "$name" "$version" "$has_icon" "$shown" <<'PY'
import plistlib, sys
dev = sys.argv[2].endswith(".dev")
plist = {
    "CFBundleDevelopmentRegion": "zh_CN", "CFBundleDisplayName": sys.argv[3], "CFBundleExecutable": "LuokeCore",
    "CFBundleIdentifier": sys.argv[2], "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": sys.argv[3],
    "CFBundlePackageType": "APPL", "CFBundleShortVersionString": sys.argv[6], "CFBundleVersion": sys.argv[4],
    "LSMinimumSystemVersion": "14.0", "LSUIElement": True, "NSHighResolutionCapable": True,
    "NSPrincipalClass": "NSApplication", "LuokeAccessory": "luoke-core", "LuokeContract": 1,
    # luoke://memory：各工具用它打开记忆界面（主控没在跑时系统会拉起主控）
    "CFBundleURLTypes": [{"CFBundleURLName": "ai.luoke.core.memory", "CFBundleURLSchemes": ["luoke"]}],
}
if sys.argv[5] == "1":
    plist["CFBundleIconFile"] = "AppIcon"
if dev:
    del plist["CFBundleURLTypes"]      # 开发版不认领 luoke://，免得和装着的正式版抢
with open(sys.argv[1], "wb") as f:
    plistlib.dump(plist, f)
PY
printf 'APPL????' > "$app/Contents/PkgInfo"
codesign --force --deep --sign - "$app"
codesign --verify --deep --strict "$app" && echo "已打包并 ad-hoc 签名：${app}（bundle id ${bundle_id}）"

if [ "${2:-}" = "--install" ] || [ "${INSTALL:-0}" = 1 ]; then
  dst="$HOME/Applications/$name.app"
  if pgrep -f "$dst/" >/dev/null; then echo "$dst 还在跑，先退出它（菜单栏 → 退出螺壳）再装" >&2; exit 1; fi
  mkdir -p "$HOME/Applications"; rm -rf "$dst"; cp -R "$app" "$dst"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$dst" >/dev/null
  echo "已装到 ${dst}。打开它走一遍首次运行（最后一步会注册登录项）；之后登录时它自己启动并拉起常驻配件。"
fi
