#!/bin/sh
# 本机正式版：整个螺壳.app 用正式的 bundle id（ai.luoke.core）、Developer ID 签名并公证，装到 /Applications，
# 给负责人在本机验只有正式版才看得出的东西（系统给正式版开的权限、通知、登录项）。不跑评测、不上传（docs/dev-instance.md「本机正式版」）。
#   tools/local_app.sh             打包、签名公证，放在 app/build-<版本号>.noindex/app/螺壳.app
#   tools/local_app.sh --install   打好以后换掉 /Applications/螺壳.app 并打开：先退出在跑的螺壳，旧的挪到 app/backup-<旧版本号>.noindex/
# 版本号是 local-MMDD-HHMM：设置里标「本机版，没上传」，不从网上查更新。
# 配方和发布清单取本机装着的 /Applications/螺壳.app 里的那一对（同一版模型、引擎），只把版本号和打包时间换掉；可用 LOCAL_RECIPE、LOCAL_RELEASE 换。
# 各项目的代码（和 dev_app.sh 一样从 ~/Codes/luoke 下的检出取，克隆在 app/build-cache.noindex/src/<项目>，不动各项目的检出）：
#   LOCAL_BRANCH 默认 ui-refactor：主控、agent、记忆、邮件、搜索里有这条分支的用它最新的提交，没有的用 main；设成空的都用 main。
#   LUOKE_AGENT_REF、LUOKE_MEM_REF、LUOKE_MAIL_REF、LUOKE_SEARCH_REF：单独钉某个项目的分支或提交（例如只换 agent，其余照上一版）。
#   主控：在 main 上的检出里跑就换成 LOCAL_BRANCH 最新的提交来打；在别的分支（会话自己的 worktree）上跑就用这份，要求没有没提交的改动。
#   扫描：默认取它 release 里最新的打好的包；LUOKE_SCAN_PKG 指定一个 luoke-scan-<版本>-macos-arm64.tar.gz。
# 签名公证要读钥匙串：Mac 锁着时读不到，保持解锁。用了哪些提交写在 app/build-<版本号>.noindex/local.json。
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
action=${1:-build}
case "$action" in
  build|--install) ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

codes="$HOME/Codes/luoke"
root=${LOCAL_APP_ROOT:-$here}      # 打出的包、克隆、备份放在这里：主控换成分支的克隆来打时，仍放在起脚本的那份检出里
cache="$root/app/build-cache.noindex"
installed=/Applications/螺壳.app
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
LOCAL_BRANCH=${LOCAL_BRANCH-ui-refactor}

# 主控自己的代码：从 main 上的检出起，换成 LOCAL_BRANCH 最新的提交来打（克隆在缓存里，检出不动）
if [ -n "$LOCAL_BRANCH" ] && [ -z "${LOCAL_APP_ROOT:-}" ] && [ "$(git -C "$here" symbolic-ref --short -q HEAD || true)" = main ]; then
  url=$(git -C "$here" remote get-url origin)
  if git ls-remote --exit-code --heads "$url" "$LOCAL_BRANCH" >/dev/null 2>&1; then
    c="$cache/src/luoke-core"
    [ -e "$c/.git" ] || git clone -q --reference "$here" "$url" "$c"
    git -C "$c" fetch -q origin "$LOCAL_BRANCH" && git -C "$c" checkout -q -f FETCH_HEAD
    echo "luoke-core：用 $LOCAL_BRANCH（$(git -C "$c" rev-parse --short HEAD)）" >&2
    LOCAL_APP_ROOT="$here" exec sh "$c/tools/local_app.sh" "$@"
  fi
fi
if [ -n "$(git -C "$here" status --porcelain --untracked-files=no)" ]; then
  echo "$here 有没提交的改动：正式签名的包只从提交过的代码打" >&2
  exit 1
fi

tag=local-$(date +%m%d-%H%M)
out="$root/app/build-$tag.noindex"
[ -e "$out" ] && { echo "$out 已经有了：等一分钟再打" >&2; exit 1; }
mkdir -p "$cache/src" "$out"

# $1 项目 $2 分支或提交（空的：LOCAL_BRANCH，没有这条分支就 main）→ 打包用的克隆目录（标准输出）
src_of() {
  c="$cache/src/$1"
  url=$(git -C "$codes/$1" remote get-url origin)
  [ -e "$c/.git" ] || git clone -q --reference "$codes/$1" "$url" "$c" >&2
  git -C "$c" fetch -q origin >&2
  want=$2
  if [ -z "$want" ]; then
    want=main
    [ -n "$LOCAL_BRANCH" ] && git -C "$c" rev-parse -q --verify "origin/$LOCAL_BRANCH" >/dev/null && want=$LOCAL_BRANCH
  fi
  ref=$(git -C "$c" rev-parse -q --verify "origin/$want^{commit}" || git -C "$c" rev-parse -q --verify "$want^{commit}") \
    || { echo "$1：找不到 $want" >&2; exit 1; }
  git -C "$c" checkout -q -f "$ref" >&2 && git -C "$c" clean -qfdx >&2
  echo "$1：用 $want（$(git -C "$c" rev-parse --short HEAD)）" >&2
  echo "$c"
}
LUOKE_AGENT_SRC=$(src_of luoke-agent "${LUOKE_AGENT_REF:-}")
LUOKE_MEM_SRC=$(src_of luoke-mem "${LUOKE_MEM_REF:-}")
LUOKE_MAIL_SRC=$(src_of luoke-mail "${LUOKE_MAIL_REF:-}")
LUOKE_SEARCH_SRC=$(src_of luoke-search "${LUOKE_SEARCH_REF:-}")
[ -f "$LUOKE_AGENT_SRC/window/Package.swift" ] || { echo "$LUOKE_AGENT_SRC 里没有 window/Package.swift：agent 的提交太旧" >&2; exit 1; }
[ -f "$LUOKE_SEARCH_SRC/scripts/build_helper.sh" ] || { echo "$LUOKE_SEARCH_SRC 里没有 scripts/build_helper.sh：搜索的提交太旧" >&2; exit 1; }

if [ -z "${LUOKE_SCAN_PKG:-}" ]; then
  scan="$out/scan"
  mkdir -p "$scan"
  gh release download --repo "luokelabs/luoke-scan${LUOKE_REPO_SUFFIX:--private}" --pattern 'luoke-scan-*-macos-arm64.tar.gz*' --dir "$scan" >/dev/null \
    || { echo "扫描的包没下下来：用 LUOKE_SCAN_PKG 指一个" >&2; exit 1; }
  LUOKE_SCAN_PKG=$(ls "$scan"/luoke-scan-*-macos-arm64.tar.gz | head -1)
fi
echo "luoke-scan：$(basename "$LUOKE_SCAN_PKG")" >&2

recipe=${LOCAL_RECIPE:-$installed/Contents/Resources/recipe.json}
release=${LOCAL_RELEASE:-$installed/Contents/Resources/release.json}
[ -f "$recipe" ] || { echo "没有配方：$recipe（设 LOCAL_RECIPE）" >&2; exit 1; }
[ -f "$release" ] || { echo "没有发布清单：$release（设 LOCAL_RELEASE）" >&2; exit 1; }
cp "$recipe" "$out/recipe.json"
python3 - "$release" "$out/release.json" "$tag" <<'EOF'
import json, sys, time
d = json.load(open(sys.argv[1], encoding="utf-8"))
d["release"], d["built_at"] = sys.argv[3], int(time.time())
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
EOF

python3 - "$out/local.json" "$tag" "$here" "$LUOKE_AGENT_SRC" "$LUOKE_MEM_SRC" "$LUOKE_MAIL_SRC" "$LUOKE_SEARCH_SRC" "$LUOKE_SCAN_PKG" "$recipe" <<'EOF'
import json, os, subprocess, sys
dst, tag, core, agent, mem, mail, search, scan, recipe = sys.argv[1:]
head = lambda d: subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
rec = {"release": tag, "luoke-core": head(core), "luoke-agent": head(agent), "luoke-mem": head(mem), "luoke-mail": head(mail),
       "luoke-search": head(search), "luoke-scan": os.path.basename(scan),
       "recipe_version": json.load(open(recipe, encoding="utf-8")).get("recipe_version")}
with open(dst, "w", encoding="utf-8") as f:
    json.dump(rec, f, ensure_ascii=False, indent=1)
EOF

export LUOKE_AGENT_SRC LUOKE_MEM_SRC LUOKE_MAIL_SRC LUOKE_SEARCH_SRC LUOKE_SCAN_PKG
LUOKE_BUNDLE_ID=ai.luoke.core LUOKE_RECIPE_FILE="$out/recipe.json" LUOKE_RELEASE_FILE="$out/release.json" \
  sh "$here/app/build_app.sh" "$out/app"
app="$out/app/螺壳.app"
"$lsregister" -u "$app" 2>/dev/null || true      # .noindex 只挡 Spotlight；打包时万一被登记了，也注销掉
sh "$here/tools/release_sign.sh" "$app" --notarize
"$lsregister" -u "$app" 2>/dev/null || true      # spctl 核公证时会登记它
echo "打好了：$app（$tag）"
[ "$action" = --install ] || exit 0

# 换装：退出在跑的螺壳，等主控、agent 核心、邮件助手、搜索跟着退完（--parent-watch），旧包挪走留着，拷新的进去
if lsappinfo info -only pid -app ai.luoke.core 2>/dev/null | grep -q '[0-9]'; then
  osascript -e 'tell application id "ai.luoke.core" to quit' >/dev/null
fi
for _ in $(seq 1 30); do
  pgrep -f "$installed/Contents/(MacOS|Helpers)/" >/dev/null 2>&1 \
    || pgrep -f "luoke_core serve|luoke_agent serve|luoke_mail --resident" >/dev/null 2>&1 || break
  sleep 1
done
if pgrep -f "$installed/Contents/(MacOS|Helpers)/" >/dev/null 2>&1; then
  echo "装着的螺壳 30 秒还没退完，没有换装；新包在 $app" >&2
  exit 1
fi
if [ -d "$installed" ]; then
  old=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('release','unknown'))" "$installed/Contents/Resources/release.json" 2>/dev/null || echo unknown)
  backup="$root/app/backup-$old.noindex"
  [ -e "$backup/螺壳.app" ] && backup="$root/app/backup-$old-$(date +%H%M%S).noindex"
  mkdir -p "$backup"
  mv "$installed" "$backup/螺壳.app"
  "$lsregister" -u "$backup/螺壳.app" 2>/dev/null || true
  echo "旧的（$old）挪到了 $backup/螺壳.app" >&2
fi
ditto "$app" "$installed"
codesign --verify --deep --strict "$installed"
spctl -a -t exec "$installed"
"$lsregister" -f "$installed"
open "$installed"
echo "装好了：$installed（$tag）"
