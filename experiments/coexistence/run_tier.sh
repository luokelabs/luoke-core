#!/bin/zsh
# 实测 1：每个候选底座——装得下 / 多快（SCEN，默认 S1,B2,B3），多准（luoke-mail 分拣评测），可选 U（加载/卸载 U_CYCLES 次）
# 用法（在测试机的 ~/luoke-coexist/ 里跑，旁边要有 coexist_bench.py、score_requests.jsonl、xiaohe/、models/、luoke-mail/）：
#   SCEN=S1,S2,S3,B2,B3 U_CYCLES=3 FREE_OK=80 ./run_tier.sh <机器名> <天花板GB> <oMLX app 路径> <候选子串>...
# 每个候选跑完都会等上一轮的测试实例真的退出、内存回收再开下一轮（实测 1 第一轮就是没等，三组内存数据作废）。
set -u
M=$1; G=$2; APP=$3; shift 3
SCEN=${SCEN:-S1,B2,B3}
U_CYCLES=${U_CYCLES:-0}
FIT_SUFFIX=${FIT_SUFFIX:-}
cd ~/luoke-coexist
test_instances() {  # 测试实例的进程名是 omlx-server（omlx-cli 会 exec 成它），靠它打开/映射的文件在 ~/luoke-coexist 下认出来，不碰 brew 装的那个
  for p in $(pgrep -x omlx-server); do lsof -p $p 2>/dev/null | grep -q "luoke-coexist" && echo $p; done
}
clean_start() {  # 上一轮的测试实例必须完全退出、内存回收，否则下一轮的内存数据是脏的（2026-10-10 第一轮就是这样）
  for i in $(seq 1 30); do [ -z "$(test_instances)" ] && break; sleep 2; done
  for p in $(test_instances); do kill $p 2>/dev/null; done; sleep 5
  for p in $(test_instances); do kill -9 $p 2>/dev/null; done; sleep 2
  for i in $(seq 1 45); do f=$(memory_pressure | awk -F': ' "/free percentage/{print \$2}" | tr -d '% '); [ "${f:-0}" -ge ${FREE_OK:-80} ] && break; sleep 2; done
  echo "clean start: free ${f:-?}%, 测试实例 $(test_instances | wc -l | tr -d ' ') 个"
}
COMMON=(--omlx-app $APP --base-dir ~/luoke-coexist --model-dir ~/luoke-coexist/models --scorer-engine ~/luoke-coexist/xiaohe --scorer-model ~/luoke-coexist/models/Qwen3.5-0.8B-Base-4bit --requests score_requests.jsonl --cache none --guard-gb $G)
for C in "$@"; do
  clean_start
  echo "=== $(date) $M $C fit ($SCEN)"
  out=results/tier-$M-$C-fit$FIT_SUFFIX.jsonl
  [ -s $out ] || /usr/bin/python3 coexist_bench.py $COMMON --base $C --scenarios $SCEN --seconds 60 --out $out || echo "FAILED fit $C"
  sleep 15
  if [ "$U_CYCLES" != "0" ]; then
    clean_start
    echo "=== $(date) $M $C load/unload"
    out=results/tier-$M-$C-u.jsonl
    [ -s $out ] || /usr/bin/python3 coexist_bench.py $COMMON --base $C --scenarios U --abort-cycles $U_CYCLES --out $out || echo "FAILED U $C"
    sleep 15
  fi
  echo "=== $(date) $M $C quality"
  if [ ! -s luoke-mail/bench/results/tier-$M-$C.json ]; then
    /usr/bin/python3 coexist_bench.py $COMMON --base $C --serve-only --out results/tier-$M-$C-serve.jsonl > results/tier-$M-$C-serve.json 2>results/tier-$M-$C-serve.err &
    SPID=$!
    for i in $(seq 1 150); do sleep 2; [ -s results/tier-$M-$C-serve.json ] && break; done
    if [ -s results/tier-$M-$C-serve.json ]; then
      PORT=$(python3 -c "import json;print(json.load(open('results/tier-$M-$C-serve.json'))['port'])")
      KEY=$(python3 -c "import json;print(json.load(open('results/tier-$M-$C-serve.json'))['key'])")
      MODEL=$(python3 -c "import json;print(json.load(open('results/tier-$M-$C-serve.json'))['base'])")
      (cd luoke-mail && /usr/bin/python3 bench/run_bench.py --base-url http://127.0.0.1:$PORT/v1 --model "$MODEL" --api-key "$KEY" --label tier-$M-$C --warmup > ../results/tier-$M-$C-quality.log 2>&1) || echo "FAILED quality $C"
      tail -2 results/tier-$M-$C-quality.log
    else
      echo "FAILED serve $C"; tail -3 results/tier-$M-$C-serve.err
    fi
    kill $SPID 2>/dev/null; sleep 5; clean_start
  fi
done
echo TIERDONE
