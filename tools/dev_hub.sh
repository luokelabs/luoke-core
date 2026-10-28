#!/bin/sh
# 起一个给配件接入用的开发版主控（实验室构建）：socket 在 ~/Library/Application Support/luoke-core-lab/run/hub.sock。
#   tools/dev_hub.sh [small|full] [名字]   small（默认）：底座用 gemma-4-e4b（秒级加载，适合调接口）；full：按 recipes/dev.json 的本档底座
#   第二个参数给实例起名（默认 lab）：主控环境是 ~/Library/Application Support/luoke-core-<名字>，几个会话各起各的就不会互相顶掉。
# 模型目录只放指向 ~/.omlx/models 的符号链接（只读复用文件，不碰用户自己的 oMLX 实例和设置）。
# 实验室主控只看自己的配件目录（luoke-core#6）：要让它看到你在联调的配件，把它装进 "$env/Applications.noindex"，
# 或者设 LUOKE_ACCESSORY_DIR=<放 .app 的目录> 再跑这个脚本（看不到 ~/Applications、~/Library/Input Methods 里真实装着的）。
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
mode=${1:-small}
name=${2:-lab}
env="$HOME/Library/Application Support/luoke-core-$name"
models="$env/models"
mkdir -p "$models" "$env/run"
link() { [ -e "$1" ] && ln -sfn "$1" "$models/$(basename "$1")" || echo "没有 $1（配方里要用到的话会显示需下载）" >&2; }
link "$HOME/.omlx/models/local/Qwen3.5-0.8B-Base-4bit"
link "$HOME/.omlx/models/mlx-community/gemma-4-e4b-it-4bit"
link "$HOME/.omlx/models/gemma-4-26b-a4b-it-8bit"
recipe="$here/recipes/dev.json"
if [ "$mode" = small ]; then
  recipe="$env/recipe-small.json"
  /usr/bin/python3 - "$here/recipes/dev.json" "$recipe" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
d["recipe_version"] += "-small"
for t in d["assignment"].values():
    t["base"] = "gemma-4-e4b-4bit"
json.dump(d, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PY
fi
echo "主控环境：$env"
echo "socket：$env/run/hub.sock（配件设 LUOKE_HUB_SOCKET 指向它，或直接用这个默认的实验室路径）"
exec /usr/bin/python3 -m luoke_core serve --lab --recipe "$recipe" --model-dir "$models" --env "$env" --socket "$env/run/hub.sock" \
  ${LUOKE_ACCESSORY_DIR:+--accessory-dir "$LUOKE_ACCESSORY_DIR"}
