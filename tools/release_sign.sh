#!/bin/sh
# 正式签名与公证（V1 §7 发布脚本的签名部分）：Developer ID Application + hardened runtime + 时间戳，由内向外签；公证、staple、校验。
#   tools/release_sign.sh <某.app> [--notarize] [--identity "<证书名>"] [--profile <notarytool 钥匙串条目>] [--entitlements <plist>]
# - 默认证书 "Developer ID Application"（钥匙串里按前缀找到唯一一张），默认公证条目 luoke.ai。
# - 自带 Python 的 .app（打包脚本的产物、主控）：先签 Resources 里所有 Mach-O（python 可执行文件、.so、.dylib），再签启动器/主程序，最后签 .app。
#   Python 要的 entitlements 默认用 tools/entitlements-python.plist（允许未签名的可执行内存；不关库校验——所有库都是同一个 Team 签的）。
# - 只签、只公证，不上传到别处、不替换任何已装的东西。输出放在原 .app 旁边：<名字>.zip（公证提交用）和 <名字>.notarize.log。
set -eu
set -o pipefail     # `… | tail` 之后取的是 tail 的退出码：签名、公证、staple 失败会被吞掉（审查 2026-10-17）
app=""; notarize=0; identity="Developer ID Application"; profile="${LUOKE_NOTARY_PROFILE:-luoke.ai}"; ent=""; preserve=0
while [ $# -gt 0 ]; do
  case "$1" in
    --notarize) notarize=1 ;;
    --identity) identity="$2"; shift ;;
    --profile) profile="$2"; shift ;;
    --entitlements) ent="$2"; shift ;;
    --preserve-entitlements) preserve=1 ;;   # 重签第三方 .app（引擎包）：每个二进制沿用它自己原有的 entitlements，没有的才用默认
    *) app="$1" ;;
  esac
  shift
done
[ -d "$app" ] || { echo "用法：$0 <某.app> [--notarize]" >&2; exit 64; }
app=$(cd "$app" && pwd)
here=$(cd "$(dirname "$0")" && pwd)
[ -n "$ent" ] || ent="$here/entitlements-python.plist"
name=$(basename "$app" .app)

# 证书：按前缀找，必须唯一
# 出版机器（Mac Studio）：证书和公证条目放在单独的钥匙串里，LUOKE_SIGN_KEYCHAIN 指过去；
# 公证条目改用 App Store Connect API 密钥存的 luoke-notary（LUOKE_NOTARY_PROFILE）。开发机不设，照旧用登录钥匙串和 luoke.ai
kc="${LUOKE_SIGN_KEYCHAIN:-}"
kcarg=""; [ -n "$kc" ] && kcarg="--keychain $kc"
# codesign 在等钥匙串授权弹窗时会一直挂着（luoke-core#23：出版机器上没人点「始终允许」）：每次签名限 120 秒，超时就停、说清楚
cs() { perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 120 codesign "$@" || { rc=$?; [ $rc = 142 ] && echo "codesign 120 秒没结束：多半在等钥匙串授权（登录账户的钥匙串弹了「codesign 想要使用密钥」）。在那台机器上点「始终允许」，或跑 security set-key-partition-list -S apple-tool:,apple:,codesign: -s ~/Library/Keychains/login.keychain-db" >&2; return $rc; }; }
hash=$(security find-identity -v -p codesigning $kc | grep "$identity" | head -1 | awk '{print $2}')
[ -n "$hash" ] || { echo "钥匙串里没有 \"$identity\" 的签名证书" >&2; exit 65; }
sign() { cs --force --timestamp --options runtime $kcarg --sign "$hash" "$@"; }
tmpent=$(mktemp -d); trap 'rm -rf "$tmpent"' EXIT
# 给某个二进制该用的 entitlements 文件：--preserve-entitlements 时先取它原来的
ent_for() {
  if [ "$preserve" = 1 ]; then
    f="$tmpent/$(echo "$1" | md5).plist"
    if codesign -d --entitlements :- "$1" > "$f" 2>/dev/null && grep -q "<key>" "$f"; then echo "$f"; return; fi
  fi
  echo "$ent"
}

echo "== 由内向外签：$app"
# 1. Resources 里所有 Mach-O（python 运行时、扩展、动态库）。找不到 Mach-O 的文件跳过。
find "$app/Contents/Resources" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) | while read -r f; do
  if file -b "$f" | grep -q "Mach-O"; then sign --entitlements "$(ent_for "$f")" "$f" 2>/dev/null || sign "$f"; fi
done
# 2. Frameworks（如有）
if [ -d "$app/Contents/Frameworks" ]; then
  find "$app/Contents/Frameworks" -depth -name "*.framework" -o -depth -name "*.dylib" | while read -r f; do sign "$f"; done
fi
# 3. 启动器 / 主程序，带 entitlements（它们 exec python）
for exe in "$app"/Contents/MacOS/*; do sign --entitlements "$(ent_for "$exe")" "$exe"; done
# 4. 整个 .app
sign --entitlements "$(ent_for "$app")" "$app"
codesign --verify --deep --strict --verbose=2 "$app" 2>&1 | tail -2
echo "== 签名完成（${identity}，hardened runtime，时间戳）"

if [ "$notarize" = 1 ]; then
  zip="$(dirname "$app")/$name.zip"
  rm -f "$zip"
  ditto -c -k --keepParent "$app" "$zip"
  echo "== 提交公证（${profile}）：$zip"
  log="$(dirname "$app")/$name.notarize.log"
  xcrun notarytool submit "$zip" --keychain-profile "$profile" $kcarg --wait 2>&1 | tee "$log" | tail -4
  id=$(grep -m1 "  id: " "$log" | awk '{print $2}')
  if grep -q "status: Accepted" "$log"; then
    xcrun stapler staple "$app" | tail -1
    echo "== 公证通过并已 staple；spctl："
    spctl -a -vv -t exec "$app" 2>&1 || echo "（spctl 退出码 $?，不影响：公证票据已 staple）"     # 只作展示（同 make_dmg.sh）
    # spctl / stapler 的检查会把这份 .app 按 bundle id 登记进 LaunchServices（2026-10-15 发现）：注销掉，别让它和负责人在用的那份抢
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -u "$app" >/dev/null 2>&1 || true
  else
    if grep -q "No Keychain password item found" "$log"; then
      echo "== 没提交成：Mac 锁屏或钥匙串锁着时读不到公证凭据（钥匙串条目 ${profile} 还在，不要重存）。解锁后重试。" >&2
    else
      echo "== 公证没通过，详情："; xcrun notarytool log "$id" --keychain-profile "$profile" $kcarg 2>&1 | head -60
    fi
    exit 1
  fi
fi
