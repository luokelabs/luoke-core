#!/bin/sh
# 试用换装（顶层 docs/dogfood-2026-10.md）：把 ~/Applications 里的主控换成签名公证版，旧版留着能一条命令回滚。负责人点头后再跑。
#   tools/dogfood_swap.sh install  [签名版.app]   默认 build.noindex/dogfood/螺壳.app：旧版挪到 build.noindex/previous/<时间>/ 并注销，签名版 ditto 过去、登记、打开
#   tools/dogfood_swap.sh rollback [previous 里的那份]   默认最近一份：签名版挪到 build.noindex/previous/<时间>-signed/，旧版挪回、登记、打开
# 先在菜单栏退出主控；它还在跑就不动。只动 ~/Applications/螺壳.app 和 build.noindex/，不碰主控环境目录和数据。
set -eu
export LC_ALL=en_US.UTF-8   # 脚本里有中文：没有 UTF-8 locale 时 bash 会把 $var 后面的中文标点当变量名
here=$(cd "$(dirname "$0")/.." && pwd)
name="螺壳"; dst="$HOME/Applications/$name.app"; prev="$here/build.noindex/previous"
LS=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
cmd=${1:-}; src=${2:-}
if pgrep -f "$dst/Contents/MacOS/LuokeCore" >/dev/null 2>&1; then echo "$dst 还在跑：先在菜单栏退出螺壳再来" >&2; exit 1; fi
stamp=$(date +%Y%m%d-%H%M%S)
case "$cmd" in
  install)
    [ -n "$src" ] || src="$here/build.noindex/dogfood/$name.app"
    [ -d "$src" ] || { echo "没有 $src" >&2; exit 1; }
    spctl -a -t exec "$src" 2>/dev/null || { echo "$src 没过 Gatekeeper 评估（没签名或没公证？）" >&2; exit 1; }
    mkdir -p "$prev/$stamp"
    if [ -d "$dst" ]; then mv "$dst" "$prev/$stamp/$name.app"; "$LS" -u "$prev/$stamp/$name.app" >/dev/null 2>&1 || true; echo "旧版已挪到 $prev/$stamp/（已注销）"; fi
    ditto "$src" "$dst"; "$LS" -f "$dst" >/dev/null; codesign -dv "$dst" 2>&1 | grep -E "^Authority=Developer" | head -1
    open "$dst"; echo "已换上签名版并打开：${dst}。看一眼设置页的「登录时启动」，系统要求的话重新允许。回滚：tools/dogfood_swap.sh rollback" ;;
  rollback)
    [ -n "$src" ] || src=$(ls -d "$prev"/*/"$name.app" 2>/dev/null | grep -v -- "-signed/" | tail -1)
    [ -n "$src" ] && [ -d "$src" ] || { echo "没有可回滚的旧版（${prev}）" >&2; exit 1; }
    if [ -d "$dst" ]; then mkdir -p "$prev/$stamp-signed"; mv "$dst" "$prev/$stamp-signed/$name.app"; "$LS" -u "$prev/$stamp-signed/$name.app" >/dev/null 2>&1 || true; echo "签名版已挪到 $prev/$stamp-signed/"; fi
    mv "$src" "$dst"; "$LS" -f "$dst" >/dev/null; open "$dst"; echo "已换回旧版并打开：${dst}（来自 ${src}）" ;;
  *) echo "用法：$0 install [签名版.app] | rollback [旧版.app]" >&2; exit 64 ;;
esac
