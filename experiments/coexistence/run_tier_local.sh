#!/bin/zsh
# 128 GB 档（本机 M5 Max）：和 run_tier.sh 一样的两步，但直接用本机的仓库和 ~/.omlx/models 里的模型（模型目录放符号链接，不碰用户自己的 oMLX 实例）。
# 用法：./run_tier128.sh <候选子串>...   （候选：Qwen3.8-Flash-Next、gemma-4-26b）
set -u
SP=$(cd "$(dirname "$0")" && pwd)
CORE=~/Codes/luoke/luoke-core/experiments/coexistence
cd $SP
COMMON=(--omlx-app /Applications/oMLX.app --base-dir $SP --model-dir $SP/models --scorer-engine ~/Codes/luoke/luoke-ime/engines/personal --scorer-model ~/.omlx/models/local/Qwen3.5-0.8B-Base-4bit --requests $CORE/data/score_requests.jsonl --cache none --guard-gb 100)
label_of() { case "$1" in
  gemma-4-26b*) echo "gemma-4-26B-A4B 8bit (oMLX, M5 Max 128GB, 与打分模型同时常驻)";;
  Qwen3.8-Flash-Next*) echo "Qwen3.8-Flash-Next-oQ4e (oMLX, M5 Max 128GB, 与打分模型同时常驻)";;
  *) echo "tier-m5max-$1";; esac }
for C in "$@"; do
  LABEL=$(label_of $C)
  echo "=== $(date) m5max $C fit"
  out=$CORE/data/results/tier-m5max-$C-fit.jsonl
  [ -s $out ] || /usr/bin/python3 $CORE/coexist_bench.py $COMMON --base $C --scenarios S1,S2,S3,B2,B3 --seconds 60 --out $out || echo "FAILED fit $C"
  sleep 10
  echo "=== $(date) m5max $C quality"
  if [ ! -s ~/Codes/luoke-mail/bench/results/"$LABEL.json" ]; then
    /usr/bin/python3 $CORE/coexist_bench.py $COMMON --base $C --serve-only --out $SP/tier-m5max-$C-serve.jsonl > $SP/tier-m5max-$C-serve.json 2>$SP/tier-m5max-$C-serve.err &
    SPID=$!
    for i in $(seq 1 300); do sleep 2; [ -s $SP/tier-m5max-$C-serve.json ] && break; done
    if [ -s $SP/tier-m5max-$C-serve.json ]; then
      PORT=$(/usr/bin/python3 -c "import json;print(json.load(open('$SP/tier-m5max-$C-serve.json'))['port'])")
      KEY=$(/usr/bin/python3 -c "import json;print(json.load(open('$SP/tier-m5max-$C-serve.json'))['key'])")
      MODEL=$(/usr/bin/python3 -c "import json;print(json.load(open('$SP/tier-m5max-$C-serve.json'))['base'])")
      (cd ~/Codes/luoke-mail && /usr/bin/python3 bench/run_bench.py --base-url http://127.0.0.1:$PORT/v1 --model "$MODEL" --api-key "$KEY" --label "$LABEL" --warmup > $SP/tier-m5max-$C-quality.log 2>&1) || echo "FAILED quality $C"
      tail -2 $SP/tier-m5max-$C-quality.log
    else
      echo "FAILED serve $C"; tail -3 $SP/tier-m5max-$C-serve.err
    fi
    kill $SPID 2>/dev/null; sleep 5
  fi
done
echo TIER128DONE
