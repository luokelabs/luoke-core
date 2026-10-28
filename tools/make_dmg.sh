#!/bin/sh
# 出 DMG（V1 §7 发布脚本的最后一步）：.app 放进只读压缩 DMG，签名，可选公证并 staple。不上传、不装、不替换任何已装的东西。
#   tools/make_dmg.sh <某.app> <输出.dmg> [--notarize] [--identity "<证书名>"] [--profile <notarytool 钥匙串条目>] [--drag]
#   --drag：主控 DMG 的窗口——左边 app、右边「应用程序」快捷方式，拖过去就装（luoke-core#30，ADR 0012；tools/dmg/layout.py）
set -eu
set -o pipefail     # `… | tail` 之后取的是 tail 的退出码：签名、公证、staple 失败会被吞掉（审查 2026-10-17）
app=""; out=""; notarize=0; drag=0; identity="Developer ID Application"; profile="${LUOKE_NOTARY_PROFILE:-luoke.ai}"; as_name=""; readme=""
while [ $# -gt 0 ]; do
  case "$1" in
    --notarize) notarize=1 ;;
    --drag) drag=1 ;;
    --identity) identity="$2"; shift ;;
    --profile) profile="$2"; shift ;;
    --as) as_name="$2"; shift ;;        # DMG 里 .app 的文件名（例如 安装邮件助手.app：同一个主控 .app 换个文件名，签名不受影响）
    --readme) readme="$2"; shift ;;
    *) if [ -z "$app" ]; then app="$1"; else out="$1"; fi ;;
  esac
  shift
done
[ -d "$app" ] && [ -n "$out" ] || { echo "用法：$0 <某.app> <输出.dmg> [--notarize]" >&2; exit 64; }
app=$(cd "$app" && pwd)
case "$out" in /*) ;; *) out="$(pwd)/$out" ;; esac
name=$(basename "$app" .app)
[ -n "$as_name" ] && name=$(basename "$as_name" .app)
version=$(/usr/bin/plutil -extract CFBundleShortVersionString raw "$app/Contents/Info.plist")
# 出版机器（Mac Studio）：证书和公证条目放在单独的钥匙串里，LUOKE_SIGN_KEYCHAIN 指过去；
# 公证条目改用 App Store Connect API 密钥存的 luoke-notary（LUOKE_NOTARY_PROFILE）。开发机不设，照旧用登录钥匙串和 luoke.ai
kc="${LUOKE_SIGN_KEYCHAIN:-}"
kcarg=""; [ -n "$kc" ] && kcarg="--keychain $kc"
# codesign 在等钥匙串授权弹窗时会一直挂着（luoke-core#23：出版机器上没人点「始终允许」）：每次签名限 120 秒，超时就停、说清楚
cs() { perl -e 'alarm shift; exec @ARGV or die "exec: $!"' 120 codesign "$@" || { rc=$?; [ $rc = 142 ] && echo "codesign 120 秒没结束：多半在等钥匙串授权（登录账户的钥匙串弹了「codesign 想要使用密钥」）。在那台机器上点「始终允许」，或跑 security set-key-partition-list -S apple-tool:,apple:,codesign: -s ~/Library/Keychains/login.keychain-db" >&2; return $rc; }; }
hash=$(security find-identity -v -p codesigning $kc | grep "$identity" | head -1 | awk '{print $2}')
[ -n "$hash" ] || { echo "钥匙串里没有 \"$identity\" 的签名证书" >&2; exit 65; }

stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/dmg"
ditto "$app" "$stage/dmg/$name.app"
rm -f "$out"
mkdir -p "$(dirname "$out")"
if [ "$drag" = 1 ]; then
  # 拖进「应用程序」的窗口；没有先读我（背景上写了）
  /usr/bin/python3 -B "$(dirname "$0")/dmg/layout.py" "$stage/dmg/$name.app" "$out" "$name"
else
  [ -n "$readme" ] || readme="打开「${name}」：它会把螺壳装进你的「应用程序」文件夹，并引导你完成设置；之后可以推出这个磁盘映像。"
  printf '%s\n' "$readme" > "$stage/dmg/先读我.txt"
  # 不加 -quiet：失败时它的原因（例如出版账户的「App 管理」挡住了往盘里拷 .app，10-23 Studio）要进日志
  hdiutil create -volname "$name $version" -srcfolder "$stage/dmg" -ov -format UDZO -fs HFS+ "$out" >/dev/null
fi
cs --force --timestamp $kcarg --sign "$hash" "$out"
codesign --verify --verbose=1 "$out" 2>&1 | tail -1
echo "== DMG：${out}（$(du -h "$out" | cut -f1)）"

if [ "$notarize" = 1 ]; then
  log="${out%.dmg}.notarize.log"
  echo "== 提交公证（${profile}）：$out"
  xcrun notarytool submit "$out" --keychain-profile "$profile" $kcarg --wait 2>&1 | tee "$log" | tail -4
  id=$(grep -m1 "  id: " "$log" | awk '{print $2}')
  if grep -q "status: Accepted" "$log"; then
    xcrun stapler staple "$out" | tail -1
    echo "== 公证通过并已 staple；spctl："
    # 只作展示：公证票据上面 staple 时已经核过。出版机器的 launchd 会话里它退出非零（10-23 Studio 空跑，手动跑又是 0），
    # 不能让它挡住出版；结论照写进日志
    spctl -a -vv -t open --context context:primary-signature "$out" 2>&1 || echo "（spctl 退出码 $?，不影响：公证票据已 staple）"
  else
    if grep -q "No Keychain password item found" "$log"; then
      echo "== 没提交成：Mac 锁屏或钥匙串锁着时读不到公证凭据（钥匙串条目 ${profile} 还在，不要重存）。解锁后重试。" >&2
    else
      echo "== 公证没通过，详情："; xcrun notarytool log "$id" --keychain-profile "$profile" $kcarg 2>&1 | head -60
    fi
    exit 1
  fi
fi
