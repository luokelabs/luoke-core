#!/bin/sh
# 起一份开发实例：整个螺壳.app（主窗口、agent 核心、记忆、邮件助手、搜索、扫描）的本机开发构建，给负责人看产品线的改动（docs/dev-instance.md）。
#   tools/dev_app.sh <名字>            打包并起来（名字：小写字母、数字、-，例如 agent、mail-today）
#   tools/dev_app.sh <名字> --build    只打包，不起
#   tools/dev_app.sh <名字> --open     不重打，起上次打好的那份
#   tools/dev_app.sh <名字> --stop     停掉这个实例（按它的 bundle id 找到的 PID），再打开本机装着的螺壳
#   tools/dev_app.sh <名字> --status   看它在不在跑、环境目录和 socket
# 每个名字一份：bundle id ai.luoke.core.<名字>.lab（实验室构建），包名「螺壳（<名字>）」，
# 环境目录就是装着的螺壳的 ~/Library/Application Support/luoke-core（socket 在 run/hub.sock、日志、模型、引擎），输入法这些照样找得到主控。
# 数据和正式版是同一份：记忆、agent、邮件助手、搜索、扫描都用本机用户真实的数据，应用装在真实的位置。
# 所以起之前先停掉在跑的别的螺壳（本机装着的正式版、别的开发实例），--stop 之后再打开本机装着的螺壳。
# DEV_FIRST_RUN=0：不走首次运行，直接是平常的样子（模型、引擎由主控自己补下；不登记登录项、不弄输入法和通知）。只这一次，下次不设照旧。
# 各项目的代码从哪来（默认 ~/Codes/luoke 下的检出；在 worktree 里改的，把对应的变量指向自己的 worktree；设成空的不带）：
#   LUOKE_AGENT_SRC、LUOKE_MEM_SRC、LUOKE_MAIL_SRC、LUOKE_SEARCH_SRC；扫描默认用它 release 里最新的打好的包（LUOKE_SCAN_PKG 可换，
#   LUOKE_SCAN_SRC 从源码打要扫描的开发环境）。
# DEV_BRANCH 默认 ui-refactor（界面重构的分支）：主控、agent、记忆、邮件、搜索里有这条分支的用它最新的提交；设成空的用 main。
#   主控的代码：在 main 上的检出里跑就换成 ui-refactor 最新的提交；在别的分支（会话自己的 worktree）上跑就用这份。
# 配方和发布清单默认取本机装着的 /Applications/螺壳.app 里的那一对（和本机在用的同一版模型、引擎），可用 DEV_RECIPE、DEV_RELEASE 换；
# 模型从本机在用的螺壳的模型目录克隆（APFS 克隆，只读、不占空间），可用 LUOKE_LOCAL_MODEL_ROOTS 换。
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
name=${1:-}
action=${2:-run}
case "$name" in
  ""|-*) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
echo "$name" | grep -Eq '^[a-z0-9][a-z0-9-]*$' || { echo "名字只能用小写字母、数字和 -：$name" >&2; exit 2; }
case "$name" in lab|dev|core) echo "「$name」留给已有的构建，换一个名字" >&2; exit 2 ;; esac

bundle_id="ai.luoke.core.$name.lab"
app_name="螺壳（$name）"
root=${DEV_APP_ROOT:-$here}     # 打出的包、下载缓存放在这里：主控换成分支的克隆来打时，仍放在起脚本的那份检出里
out="$root/app/build-$name.noindex"
app="$out/$app_name.app"
env_dir="$HOME/Library/Application Support/luoke-core"     # 和装着的螺壳同一个（AppInfo.envDir）
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
codes="$HOME/Codes/luoke"
installed=/Applications/螺壳.app/Contents/Resources

DEV_BRANCH=${DEV_BRANCH-ui-refactor}
# 主控自己的代码：从 main 上的检出起（负责人和各会话用的同一条命令），换成 DEV_BRANCH 最新的提交来打——克隆在缓存里，检出不动
if [ "$action" = run ] || [ "$action" = --build ]; then
  if [ -n "$DEV_BRANCH" ] && [ -z "${DEV_APP_ROOT:-}" ] && [ "$(git -C "$here" symbolic-ref --short -q HEAD || true)" = main ]; then
    url=$(git -C "$here" remote get-url origin 2>/dev/null || true)
    if [ -n "$url" ] && git ls-remote --exit-code --heads "$url" "$DEV_BRANCH" >/dev/null 2>&1; then
      c="$here/app/build-cache.noindex/src/luoke-core"
      [ -e "$c/.git" ] || git clone -q --reference "$here" "$url" "$c"
      git -C "$c" fetch -q origin "$DEV_BRANCH" && git -C "$c" checkout -q -f FETCH_HEAD
      echo "luoke-core：用 $DEV_BRANCH（$(git -C "$c" rev-parse --short HEAD)）" >&2
      DEV_APP_ROOT="$here" DEV_BRANCH="$DEV_BRANCH" exec sh "$c/tools/dev_app.sh" "$@"
    fi
  fi
fi

# 按 bundle id 找（每个实例的 id 不同，不会找到本机在用的螺壳）；lsappinfo 新旧两种输出：`"pid"=123`、`pid = 123`
# lsappinfo 在进程退出后还会留一会儿旧记录：再用 kill -0 核一下它还在
pid_of() {
  p=$(lsappinfo info -only pid -app "$bundle_id" 2>/dev/null | sed -n 's/.*"\{0,1\}pid"\{0,1\} \{0,1\}= \{0,1\}\([0-9][0-9]*\).*/\1/p' | head -1)
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null && echo "$p"
  return 0
}

# 在跑的别的螺壳（本机装着的正式版、开发版、别的开发实例）：和这个实例用同一份真实数据，同时开着邮件助手、agent 会抢。
# 普通的实验室构建（ai.luoke.core.lab，评测用）有自己的数据，不算
others() {
  lsappinfo list 2>/dev/null | sed -n 's/.*bundleID="\(ai\.luoke\.core[a-z0-9.-]*\)".*/\1/p' | sort -u | grep -v -x -e "$bundle_id" -e ai.luoke.core.lab || true
}
stop_id() {
  p=$(lsappinfo info -only pid -app "$1" 2>/dev/null | sed -n 's/.*"\{0,1\}pid"\{0,1\} \{0,1\}= \{0,1\}\([0-9][0-9]*\).*/\1/p' | head -1)
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null || return 0
  kill "$p"
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 1; done
  echo "已停 $1（PID $p）"
}
# 本体退了以后，它的主控、agent 核心、邮件助手还要几秒才退完（--parent-watch）。主控还占着 socket 时起实例，实例看到有主控在应答就退了；
# agent 核心会撞上还没放开的数据目录锁
wait_children() {
  for _ in $(seq 1 30); do
    pgrep -f "luoke_core serve .*--socket $env_dir/run/hub.sock" >/dev/null 2>&1 \
      || pgrep -f "luoke_agent serve --home $HOME/.luoke/agent" >/dev/null 2>&1 \
      || pgrep -f "luoke_mail --resident" >/dev/null 2>&1 || return 0
    sleep 1
  done
  echo "上一个螺壳的主控、agent 核心、邮件助手 30 秒还没退完，没有接着起" >&2
  exit 1
}

case "$action" in
  --stop)
    pid=$(pid_of)
    if [ -n "$pid" ]; then
      kill "$pid"      # 本体退出时主控、引擎、agent 核心跟着退（--parent-watch）
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      echo "已停 $app_name（PID $pid）"
    else
      echo "$app_name 没在跑"
    fi
    "$lsregister" -u "$app" 2>/dev/null || true     # 不用了就注销：luoke:// 链接回到本机装着的螺壳
    wait_children      # 主控还在应答时打开装着的螺壳，它会当作另一个螺壳开着、自己退出
    # 换回本机装着的螺壳（别的螺壳都没在跑时）
    if [ -d /Applications/螺壳.app ] && [ -z "$(others)" ]; then open /Applications/螺壳.app && echo "打开了本机装着的螺壳"; fi
    exit 0 ;;
  --status)
    pid=$(pid_of)
    if [ -n "$pid" ]; then echo "$app_name：在跑，PID $pid"; else echo "$app_name：没在跑"; fi
    echo "包：$app"
    echo "环境目录：$env_dir"
    echo "socket：$env_dir/run/hub.sock"
    exit 0 ;;
  run|--build|--open) ;;
  *) echo "不认得：$action（--build、--open、--stop、--status）" >&2; exit 2 ;;
esac

if [ "$action" != --open ]; then
  recipe=${DEV_RECIPE:-$installed/recipe.json}
  release=${DEV_RELEASE:-$installed/release.json}
  [ -f "$recipe" ] || { echo "没有配方：$recipe（设 DEV_RECIPE）" >&2; exit 1; }
  [ -f "$release" ] || { echo "没有发布清单：$release（设 DEV_RELEASE）" >&2; exit 1; }
  # 打包脚本默认找并排的 ../luoke-agent 这些：在 worktree 里找不到，这里给 ~/Codes/luoke 下的检出。
  # 默认的检出先拉到最新的 main（各项目的检出只用来拉取，改动都在各自的 worktree 里）：在 main 上、没有没提交的改动才拉，不然照原样用
  fresh() {
    [ -e "$1/.git" ] || return 0
    if [ "$(git -C "$1" symbolic-ref --short -q HEAD)" = main ] && [ -z "$(git -C "$1" status --porcelain --untracked-files=no)" ]; then
      git -C "$1" pull -q --ff-only 2>/dev/null || echo "$1：拉不到最新的 main，照原样用" >&2
    else
      echo "$1 不在 main 上或有没提交的改动：照原样用" >&2
    fi
  }
  # DEV_BRANCH=<分支>（例如 ui-refactor）：哪个项目有这条分支，就用它最新的提交，克隆在 app/build-cache.noindex/src/<项目> 里打
  # （不动各项目的检出）；没有这条分支的照上面用 main 的检出
  src_of() {   # $1 项目名 → 打包用的源码目录（标准输出）
    co="$codes/$1"
    if [ -n "${DEV_BRANCH:-}" ] && [ -e "$co/.git" ]; then
      url=$(git -C "$co" remote get-url origin 2>/dev/null || true)
      if [ -n "$url" ] && git ls-remote --exit-code --heads "$url" "$DEV_BRANCH" >/dev/null 2>&1; then
        c="$root/app/build-cache.noindex/src/$1"
        [ -e "$c/.git" ] || git clone -q --reference "$co" "$url" "$c" >&2
        git -C "$c" fetch -q origin "$DEV_BRANCH" >&2 && git -C "$c" checkout -q -f FETCH_HEAD >&2
        echo "$1：用 $DEV_BRANCH（$(git -C "$c" rev-parse --short HEAD)）" >&2
        echo "$c"
        return 0
      fi
      echo "$1 没有 $DEV_BRANCH 分支：用 main" >&2
    fi
    fresh "$co"
    echo "$co"
  }
  [ -n "${LUOKE_AGENT_SRC+x}" ] || LUOKE_AGENT_SRC=$(src_of luoke-agent)
  [ -n "${LUOKE_MEM_SRC+x}" ] || LUOKE_MEM_SRC=$(src_of luoke-mem)
  [ -n "${LUOKE_MAIL_SRC+x}" ] || LUOKE_MAIL_SRC=$(src_of luoke-mail)
  if [ -z "${LUOKE_SEARCH_SRC+x}" ]; then
    LUOKE_SEARCH_SRC=$(src_of luoke-search)
    # 默认的检出太旧（还不能打进 Helpers）：这次不带搜索，不整个停下
    [ -f "$LUOKE_SEARCH_SRC/scripts/build_helper.sh" ] || { echo "$LUOKE_SEARCH_SRC 太旧（没有 scripts/build_helper.sh），这次不带搜索：拉一下它的 main，或者用 LUOKE_SEARCH_SRC 指新的检出" >&2; LUOKE_SEARCH_SRC=""; }
  fi
  export LUOKE_AGENT_SRC LUOKE_MEM_SRC LUOKE_MAIL_SRC LUOKE_SEARCH_SRC
  # 扫描：默认取它 release 里最新的打好的包（和装进正式版的同一种），这台 Mac 不用装扫描的开发环境；给了 LUOKE_SCAN_SRC 就从源码打
  if [ -z "${LUOKE_SCAN_PKG+x}" ] && [ -z "${LUOKE_SCAN_SRC:-}" ]; then
    cache="$root/app/build-cache.noindex/scan"
    rm -rf "$cache"; mkdir -p "$cache"
    scan_repo="luokelabs/luoke-scan${LUOKE_REPO_SUFFIX:--private}"     # 私有仓库带 -private 后缀（和 internal_build.py 的 REPO_SUFFIX 一致）
    if gh release download --repo "$scan_repo" --pattern 'luoke-scan-*-macos-arm64.tar.gz*' --dir "$cache" >/dev/null 2>&1; then
      LUOKE_SCAN_PKG=$(ls "$cache"/luoke-scan-*-macos-arm64.tar.gz | head -1)
    else
      echo "扫描的包没下下来（gh release download $scan_repo）：这次不带扫描" >&2
      LUOKE_SCAN_PKG=""
    fi
  fi
  export LUOKE_SCAN_PKG
  # 要的是整个螺壳：给了 agent 却不全（检出太旧，还没有 window/）就停下，不像 build_app.sh 那样不带主窗口照打
  if [ -n "$LUOKE_AGENT_SRC" ] && [ ! -f "$LUOKE_AGENT_SRC/window/Package.swift" ]; then
    echo "LUOKE_AGENT_SRC（$LUOKE_AGENT_SRC）里没有 window/Package.swift：检出太旧，换成 luoke-agent 新的 main（或自己的 worktree）；不带主窗口设成空的" >&2
    exit 1
  fi
  if [ -n "$(pid_of)" ]; then echo "$app_name 正在跑：先 tools/dev_app.sh $name --stop" >&2; exit 1; fi
  LUOKE_BUNDLE_ID="$bundle_id" LUOKE_RECIPE_FILE="$recipe" LUOKE_RELEASE_FILE="$release" sh "$here/app/build_app.sh" "$out"
  "$lsregister" -u "$app" 2>/dev/null || true      # .noindex 只挡 Spotlight；打包时万一被登记了，也注销掉
fi
[ "$action" = --build ] && { echo "打好了：$app"; exit 0; }

[ -d "$app" ] || { echo "还没打包：tools/dev_app.sh $name" >&2; exit 1; }
if [ -n "$(pid_of)" ]; then echo "$app_name 已经在跑（PID $(pid_of)）"; exit 0; fi
# 同一份真实数据只让一个螺壳开着：先停掉在跑的别的（本机装着的正式版、别的开发实例），--stop 时再打开本机装着的
for id in $(others); do stop_id "$id"; done
wait_children
roots=${LUOKE_LOCAL_MODEL_ROOTS:-"$HOME/Library/Application Support/luoke-core/models:$HOME/.omlx/models"}
# 跑着的时候留在 LaunchServices 里：通知中心靠它认这个 app（注销了「打开通知」就一直是没决定、系统设置里也没有它这一行），
# luoke:// 链接也落到正在用的这份。--stop 时注销，链接回到本机装着的螺壳
skip=0
[ "${DEV_FIRST_RUN:-1}" = 0 ] && skip=1
open -n --env "LUOKE_LOCAL_MODEL_ROOTS=$roots" --env "LUOKE_SKIP_FIRST_RUN=$skip" "$app"
sleep 2
"$lsregister" -f "$app" 2>/dev/null || true
echo "起来了：$app_name（PID $(pid_of)）"
echo "环境目录：$env_dir"
echo "socket：$env_dir/run/hub.sock（配件、curl --unix-socket 用这个）"
echo "停：tools/dev_app.sh $name --stop"
