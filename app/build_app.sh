#!/bin/sh
# 打开发版「螺壳.app」：swift build 出本体，装进自带的 Python 和 luoke_core，配方，ad-hoc 签名。
#   app/build_app.sh [输出目录] [--install]   默认输出 app/build.noindex（.noindex：Spotlight 不索引，这份副本不会按 bundle id 登记进 LaunchServices）；--install 再拷到 ~/Applications 并注册
#   环境变量：LUOKE_RECIPE_FILE（配方，默认 recipes/dev.json）、LUOKE_RELEASE_FILE（发布清单，放进 Resources/release.json）、LUOKE_BUNDLE_ID、
#   LUOKE_AGENT_SRC（luoke-agent：主窗口和 agent 核心，luoke-core#68；没设用并排的 ../luoke-agent，设成空的不带）、
#   LUOKE_SEARCH_SRC（luoke-search：放进 Contents/Helpers；没设不带，不退回并排的工作区）、
#   LUOKE_SCAN_SRC（luoke-scan：程序放进 Contents/Helpers、库放 Contents/Frameworks、数据放 Contents/Resources/scan；没设不带；
#   LUOKE_SCAN_TOOLS 是带 ClamAV、YARA 的 Homebrew 前缀，LUOKE_RELEASE_TEAM 编进扫描的团队标识，发布脚本给）、
#   LUOKE_MAIL_SRC（luoke-mail：代码进自带 Python 的 site-packages，清单进 Resources/mail；没设不带，不退回并排的工作区）
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

# 主窗口和 agent 核心并进本体（luoke-core#68）：LUOKE_AGENT_SRC 指钉住的 luoke-agent（发布脚本传，没钉就传空的）；
# 开发构建默认并排的 ../luoke-agent。本体链接它的 LuokeWindow（app/Package.swift 看这个环境变量），核心的 Python 源码放 Resources/agent。
# 没有就不带主窗口，本体照旧出（菜单栏、设置、首次运行都不变）
agent_src=${LUOKE_AGENT_SRC-$root/../luoke-agent}
if [ -n "$agent_src" ] && [ -f "$agent_src/luoke_agent/__main__.py" ] && [ -f "$agent_src/window/Package.swift" ]; then
  agent_src=$(cd "$agent_src" && pwd)
else
  [ -n "$agent_src" ] && echo "注意：没找到 luoke-agent（${agent_src}），这一份主控不带主窗口" >&2
  agent_src=""
fi
export LUOKE_AGENT_SRC="$agent_src"

cd "$here" && swift build -c release 2>&1 | tail -3
bin="$here/.build/release/LuokeCore"
[ -x "$bin" ] || { echo "没编出来：$bin" >&2; exit 1; }
# 带没带上主窗口以编出来的为准（Package.swift 的缓存没跟上环境变量时，宁可停下）
if [ -n "$agent_src" ] && [ ! -d "$here/.build/release/LuokeWindow_LuokeWindow.bundle" ]; then
  echo "LUOKE_AGENT_SRC 给了，编出来的却没有 LuokeWindow：删掉 app/.build 再打" >&2; exit 1
fi

app="$out/$name.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin" "$app/Contents/MacOS/LuokeCore"
# 发布脚本传正式配方。「_」开头的是给人看的说明（决定经过、人名），运行时不读，打包时去掉（10-30 早会：安装包传到公开仓库）；
# 仓库里的配方照旧留着它们（参与评测复用的哈希）
/usr/bin/python3 -c '
import json, sys
def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if not k.startswith("_")}
    return [strip(v) for v in o] if isinstance(o, list) else o
with open(sys.argv[1], encoding="utf-8") as f:
    r = json.load(f)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(strip(r), f, ensure_ascii=False, indent=1)
' "${LUOKE_RECIPE_FILE:-$root/recipes/dev.json}" "$app/Contents/Resources/recipe.json"
if [ -n "${LUOKE_RELEASE_FILE:-}" ]; then cp "$LUOKE_RELEASE_FILE" "$app/Contents/Resources/release.json"; fi   # 发布清单：首次运行按它下载
mkdir -p "$app/Contents/Resources/bin"
cp "$here/Resources/luoke" "$app/Contents/Resources/bin/luoke"     # 给高级用户的命令行：luoke launch pi 等（luoke_core/cli.py）
chmod 755 "$app/Contents/Resources/bin/luoke"
cp -R "$here/Resources/ui" "$app/Contents/Resources/ui"            # 安装、升级窗口用的品牌素材（luoke-brand ui/public/img：海螺、应用图标、吉祥物）
cp -R "$here/Resources/lucide" "$app/Contents/Resources/lucide"    # 本体窗口里的图标（Lucide，ISC，许可在同一目录的 LICENSE；Lucide.swift）
if [ -n "$agent_src" ]; then
  # 主窗口的图标：LuokeWindow 在 Contents/Resources 里找它的资源包，没有的话一画图标就退出
  cp -R "$here/.build/release/LuokeWindow_LuokeWindow.bundle" "$app/Contents/Resources/"
fi

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
# 螺壳邮件助手（ADR 0022）：用这里的 Python 和 luoke_mem，不再带自己的。.py 不能放 Contents/Helpers，外层签名过不了；本体用 MailHelper.swift 起它
mail_out=""
mail_pkg=""
if [ -n "${LUOKE_MAIL_SRC:-}" ]; then
  [ -f "$LUOKE_MAIL_SRC/scripts/build_for_luoke.sh" ] || { echo "LUOKE_MAIL_SRC（${LUOKE_MAIL_SRC}）里没有 scripts/build_for_luoke.sh：这个提交还不能进螺壳" >&2; exit 1; }
  [ -n "$mem_pkg" ] || { echo "带螺壳邮件助手要带 luoke-mem：它用这里的 luoke_mem" >&2; exit 1; }
  mail_out=$(mktemp -d)
  LUOKE_OUT="$mail_out" sh "$LUOKE_MAIL_SRC/scripts/build_for_luoke.sh" | tail -1
  mail_pkg="--package $mail_out/luoke_mail"
fi
/usr/bin/python3 "$root/tools/package_python_app.py" --manifest "$tmp/manifest.json" --package "$root/luoke_core" $mem_pkg $mail_pkg --out "$tmp/pkg" --sign none >/dev/null
mv "$tmp/pkg/$name.app/Contents/Resources/python" "$app/Contents/Resources/python"
[ -d "$tmp/pkg/$name.app/Contents/Resources/licenses" ] && mv "$tmp/pkg/$name.app/Contents/Resources/licenses" "$app/Contents/Resources/licenses"
rm -rf "$tmp"
if [ -n "$agent_src" ]; then
  # agent 核心（luoke-core#68）：只拷 Python 源码和它带的 luoke_mem，用自带的 Python 预编译，随本体签名；
  # 本体用 python3 -B -E -s -m luoke_agent serve 起它（AgentCore.swift），工作目录就是这里
  dst="$app/Contents/Resources/agent"
  mkdir -p "$dst"
  rsync -a --prune-empty-dirs --exclude='__pycache__/' --include='*/' --include='*.py' --include='*.html' --exclude='*' \
    "$agent_src/luoke_agent" "$agent_src/vendor" "$dst/"
  cp "$agent_src/vendor/luoke_mem.VERSION" "$dst/vendor/"
  "$app/Contents/Resources/python/bin/python3" -B -m compileall -q -j 0 "$dst" >/dev/null
  # 许可：agent、LuokeWindow、LuokeA2UI 是 Apache-2.0（给工具拷的几个文件另有 MIT，见 NOTICE）
  mkdir -p "$app/Contents/Resources/licenses/luoke-agent"
  for f in LICENSE LICENSE-MIT NOTICE; do [ -f "$agent_src/$f" ] && cp "$agent_src/$f" "$app/Contents/Resources/licenses/luoke-agent/"; done
  commit=$(git -C "$agent_src" rev-parse --short HEAD 2>/dev/null || cat "$agent_src/.luoke-commit" 2>/dev/null | cut -c1-7 || true)
  echo "带上主窗口和 agent 核心 ${commit:-?}（${agent_src}）"
fi
if [ -n "$mem_pkg" ]; then
  mkdir -p "$app/Contents/Resources/memory"
  cp "$mem_src/packaging/accessory.json" "$app/Contents/Resources/memory/luoke-accessory.json"   # 原样拷（V1 §6「记忆并进主控」）
  echo "带上螺壳记忆 $(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$mem_src/packaging/accessory.json")（${mem_src}）"
fi

if [ -n "$mail_out" ]; then
  mkdir -p "$app/Contents/Resources/mail" "$app/Contents/Resources/licenses/luoke-mail"
  cp "$mail_out/luoke-accessory.json" "$app/Contents/Resources/mail/"
  cp "$mail_out/LICENSE" "$mail_out/NOTICE" "$app/Contents/Resources/licenses/luoke-mail/"
  rm -rf "$mail_out"
  echo "带上螺壳邮件助手 $(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$app/Contents/Resources/mail/luoke-accessory.json")（${LUOKE_MAIL_SRC}）"
fi

# 螺壳搜索（ADR 0022）：Swift 的裸程序放 Contents/Helpers，本体当子进程起（SearchHelper.swift）。程序由它自己的 scripts/build_helper.sh 编，
# 清单和许可这里放好，程序在最后签名时才放进来（--deep 会把它重签，丢掉它靠着认自己的 -i）。
# 正式身份用发布版；开发版、实验室构建用开发版程序：发布版只认真实的 ~/.luoke-search，这里要用 ~/Library/Application Support/luoke-search
search_out=""
if [ -n "${LUOKE_SEARCH_SRC:-}" ]; then
  [ -f "$LUOKE_SEARCH_SRC/scripts/build_helper.sh" ] || { echo "LUOKE_SEARCH_SRC（${LUOKE_SEARCH_SRC}）里没有 scripts/build_helper.sh：这个提交还不能进 Helpers" >&2; exit 1; }
  search_out=$(mktemp -d)
  case "$bundle_id" in ai.luoke.core) search_dev="" ;; *) search_dev=1 ;; esac
  env -u SIGN_IDENTITY LUOKE_OUT="$search_out" LUOKE_SEARCH_DEV_APP="$search_dev" sh "$LUOKE_SEARCH_SRC/scripts/build_helper.sh" | tail -1
  [ -x "$search_out/luoke-search" ] || { echo "没编出来：luoke-search" >&2; exit 1; }
  mkdir -p "$app/Contents/Resources/search" "$app/Contents/Resources/licenses/luoke-search"
  cp "$search_out/luoke-accessory.json" "$app/Contents/Resources/search/"
  # 本体起它时和它签名里的版本比（ToolHost.swift）：没把版本编进程序的提交，打进来正式版也起不来，这里就停
  LUOKE_SEARCH_VERSION=$(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$search_out/luoke-accessory.json")
  export LUOKE_SEARCH_VERSION
  otool -P "$search_out/luoke-search" | grep -qF "<string>${LUOKE_SEARCH_VERSION}</string>" \
    || { echo "luoke-search 程序里没有版本 ${LUOKE_SEARCH_VERSION}（${LUOKE_SEARCH_SRC}）：这个提交还不能进 Helpers" >&2; exit 1; }
  cp "$search_out/LICENSE" "$search_out/NOTICE" "$app/Contents/Resources/licenses/luoke-search/"
fi

# 螺壳扫描（ADR 0022 的 11-05 修订）：打进螺壳，跟螺壳一起升级、签名，不生成 .app。它自己的 scripts/package_app.py 编出
# luoke-scan/（照 Contents 的样子：Helpers 里 luoke-scan、luoke-scan-admin 和 ClamAV、YARA 的四个工具，Frameworks 里它们的库，
# Resources/scan 里清单、bundled-tools.json、图标，Resources/licenses/luoke-scan 里许可），临时签名、定好各程序的标识、核对过。
# Resources、Frameworks 现在放；Helpers 里的程序在最后签名时才放（--deep 会把它们重签，丢掉 root 那一步和扫描靠着认它们的标识）。
# Helpers、Frameworks 里已经有同名的就停，不盖掉别人的
scan_out=""
if [ -n "${LUOKE_SCAN_SRC:-}" ]; then
  [ -f "$LUOKE_SCAN_SRC/scripts/package_app.py" ] || { echo "LUOKE_SCAN_SRC（${LUOKE_SCAN_SRC}）里没有 scripts/package_app.py：这个提交还不能打进螺壳" >&2; exit 1; }
  scan_out=$(mktemp -d)
  if ! /usr/bin/python3 -I "$LUOKE_SCAN_SRC/scripts/package_app.py" --out "$scan_out" --tools-from "${LUOKE_SCAN_TOOLS:-/opt/homebrew}" > "$scan_out/build.log" 2>&1; then
    tail -20 "$scan_out/build.log" >&2
    echo "螺壳扫描没编成，或者没核对过（${LUOKE_SCAN_SRC}）" >&2; exit 1
  fi
  scan="$scan_out/luoke-scan"
  for f in "$scan"/Helpers/* "$scan"/Frameworks/*; do
    rel=${f#"$scan"/}
    [ ! -e "$app/Contents/$rel" ] || { echo "螺壳.app 里已经有 Contents/${rel}：扫描带的同名文件会把它盖掉" >&2; exit 1; }
  done
  [ ! -e "$app/Contents/Resources/scan" ] && [ ! -e "$app/Contents/Resources/licenses/luoke-scan" ] || { echo "螺壳.app 里已经有 Resources/scan" >&2; exit 1; }
  mkdir -p "$app/Contents/Frameworks" "$app/Contents/Resources/licenses"
  ditto "$scan/Resources/scan" "$app/Contents/Resources/scan"
  ditto "$scan/Resources/licenses/luoke-scan" "$app/Contents/Resources/licenses/luoke-scan"
  ditto "$scan/Frameworks" "$app/Contents/Frameworks"
  LUOKE_SCAN_VERSION=$(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$app/Contents/Resources/scan/luoke-accessory.json")
  export LUOKE_SCAN_VERSION
fi

# 应用图标：app/AppIcon.icns（或 LUOKE_ICON_FILE 指的 .icns）有就打进去（顶层 ADR 0008 修订：图标由 luoke-community 出稿、定）
icon=${LUOKE_ICON_FILE:-$here/AppIcon.icns}
if [ -f "$icon" ]; then cp "$icon" "$app/Contents/Resources/AppIcon.icns"; has_icon=1; else has_icon=0; fi
/usr/bin/python3 - "$app/Contents/Info.plist" "$bundle_id" "$name" "$version" "$has_icon" "$shown" <<'PY'
import os, plistlib, sys
dev = sys.argv[2].endswith(".dev")
folders = "螺壳给你选的文件夹建索引，让你能按内容找到文件。索引只在这台 Mac 上。"
plist = {
    "CFBundleDevelopmentRegion": "zh_CN", "CFBundleDisplayName": sys.argv[3], "CFBundleExecutable": "LuokeCore",
    "CFBundleIdentifier": sys.argv[2], "CFBundleInfoDictionaryVersion": "6.0", "CFBundleName": sys.argv[3],
    "CFBundlePackageType": "APPL", "CFBundleShortVersionString": sys.argv[6], "CFBundleVersion": sys.argv[4],
    "LSMinimumSystemVersion": "14.0", "LSUIElement": True, "NSHighResolutionCapable": True,
    "NSPrincipalClass": "NSApplication", "LuokeAccessory": "luoke-core", "LuokeContract": 1,
    # luoke://memory：各工具用它打开记忆界面（主控没在跑时系统会拉起主控）
    "CFBundleURLTypes": [{"CFBundleURLName": "ai.luoke.core.memory", "CFBundleURLSchemes": ["luoke"]}],
}
if os.environ.get("LUOKE_SEARCH_SRC"):
    plist.update({
        # Contents/Helpers 里的程序用权限时，系统弹窗用的是这里的说明：它自己带的不算，缺了它会在弹窗那一刻被系统结束（ADR 0022）
        "NSAppleEventsUsageDescription": "螺壳请 QuickTime Player 从搜到的那一刻开始播放视频或录音。",
        "NSDesktopFolderUsageDescription": folders, "NSDocumentsFolderUsageDescription": folders, "NSDownloadsFolderUsageDescription": folders,
        "NSRemovableVolumesUsageDescription": folders, "NSNetworkVolumesUsageDescription": folders,
        "NSPhotoLibraryUsageDescription": "你打开了“搜照片图库”：螺壳读照片和视频的画面来建索引，让你能按画面找到它们。照片不会被改动，也不出这台 Mac。",
    })
if os.environ.get("LUOKE_SEARCH_VERSION"):
    # 和扫描一样：本体起 Helpers 里的 luoke-search 时和它签名里的版本比（螺壳的签名盖住这里）
    plist["LuokeSearchVersion"] = os.environ["LUOKE_SEARCH_VERSION"]
if os.environ.get("LUOKE_SCAN_VERSION"):
    # 打进来的扫描是哪一版：本体起 Helpers 里的 luoke-scan 时和它签名里的版本比，换成旧版的签名程序起不来（螺壳的签名盖住这里）
    plist["LuokeScanVersion"] = os.environ["LUOKE_SCAN_VERSION"]
if sys.argv[5] == "1":
    plist["CFBundleIconFile"] = "AppIcon"
if dev:
    del plist["CFBundleURLTypes"]      # 开发版不认领 luoke://，免得和装着的正式版抢
with open(sys.argv[1], "wb") as f:
    plistlib.dump(plist, f)
PY
printf 'APPL????' > "$app/Contents/PkgInfo"
codesign --force --deep --sign - "$app"
if [ -n "$search_out" ]; then
  mkdir -p "$app/Contents/Helpers"
  cp "$search_out/luoke-search" "$app/Contents/Helpers/luoke-search"
  codesign --force --sign - "$app"          # 不带 --deep：程序的签名留着
  signed=$(codesign -dv "$app/Contents/Helpers/luoke-search" 2>&1 | sed -n 's/^Identifier=//p')
  [ "$signed" = "ai.luoke.search" ] || { echo "Helpers 里的 luoke-search 签名 id 是 ${signed}，不是 ai.luoke.search：用户的 Mac 上起不来" >&2; exit 1; }
  rm -rf "$search_out"
  echo "带上螺壳搜索 $(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$app/Contents/Resources/search/luoke-accessory.json")（${LUOKE_SEARCH_SRC}）"
fi
if [ -n "$scan_out" ]; then
  mkdir -p "$app/Contents/Helpers"
  ditto "$scan_out/luoke-scan/Helpers" "$app/Contents/Helpers"
  codesign --force --sign - "$app"          # 不带 --deep：程序的签名和标识留着
  for p in luoke-scan:ai.luoke.scan luoke-scan-admin:ai.luoke.scan.admin clamscan:ai.luoke.scan.clamscan freshclam:ai.luoke.scan.freshclam sigtool:ai.luoke.scan.sigtool yara:ai.luoke.scan.yara; do
    signed=$(codesign -dv "$app/Contents/Helpers/${p%%:*}" 2>&1 | sed -n 's/^Identifier=//p')
    [ "$signed" = "${p#*:}" ] || { echo "Helpers 里的 ${p%%:*} 签名 id 是 ${signed}，不是 ${p#*:}：root 那一步和扫描认不出它" >&2; exit 1; }
  done
  rm -rf "$scan_out"
  echo "带上螺壳扫描 ${LUOKE_SCAN_VERSION}（${LUOKE_SCAN_SRC}）"
fi
codesign --verify --deep --strict "$app" && echo "已打包并 ad-hoc 签名：${app}（bundle id ${bundle_id}）"

# 主窗口自检（11-04：internal-1104-2 打开主窗口就崩。LuokeWindow 的资源包只在编译目录里找得到，出版机上正好有，用户的 Mac 上没有）：
# 先把编译目录里那份挪开，照用户的 Mac 那样只剩 .app 里的，画一遍主窗口；崩了或没画出来就停下
if [ -n "$agent_src" ]; then
  built="$(cd "$here/.build/release" && pwd -P)/LuokeWindow_LuokeWindow.bundle"
  shots=$(mktemp -d)
  rm -rf "$built.aside"; mv "$built" "$built.aside"
  checked=1
  "$app/Contents/MacOS/LuokeCore" --check-window "$shots" >"$shots/out.log" 2>&1 || checked=0
  mv "$built.aside" "$built"
  [ -f "$shots/check-window-apps.png" ] || checked=0
  if [ "$checked" != 1 ]; then
    tail -5 "$shots/out.log" >&2
    echo "主窗口自检没过：编译目录里的资源包挪开以后，打包好的螺壳画不出主窗口（用户的 Mac 上会一打开就崩）" >&2
    rm -rf "$shots"; exit 1
  fi
  rm -rf "$shots"
  echo "主窗口自检过了"
fi

if [ "${2:-}" = "--install" ] || [ "${INSTALL:-0}" = 1 ]; then
  dst="$HOME/Applications/$name.app"
  if pgrep -f "$dst/" >/dev/null; then echo "$dst 还在跑，先退出它（菜单栏 → 退出螺壳）再装" >&2; exit 1; fi
  mkdir -p "$HOME/Applications"; rm -rf "$dst"; cp -R "$app" "$dst"
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$dst" >/dev/null
  echo "已装到 ${dst}。打开它走一遍首次运行（最后一步会注册登录项）；之后登录时它自己启动并拉起常驻配件。"
fi
