# 推理引擎对照：邮件评测、微基准、输入法运行环境

luoke-core#42 提议引擎不再用 oMLX，自己在官方 mlx-lm 上做一个薄引擎。这里是比引擎用的工具和 2026-10-30 的结果。
脚本在 `experiments/engine-bench/`。

## 工具

| 文件 | 做什么 |
|---|---|
| `email_eval.py` | **邮件分拣评测直接对几个引擎跑**：题、分拣、重试、打分都是 luoke-mail 自己的代码，分数和出版的 eval 同口径 |
| `engine_micro.py`、`analyze_micro.py` | 引擎微基准：长输入预填充、生成速度、连续批处理、打断、前缀复用、多轮对话 |
| `ime_parity.py` | 输入法的打分、联想 worker 换运行环境（oMLX 带的 Python vs 薄引擎的）后是否一样、快慢 |
| `thin/thin_engine.py` | 薄引擎（对照用的 v3）：mlx-lm 0.32.2 上一个文件，连续批处理 + 按消息边界的前缀缓存 |

### email_eval.py

```
python3 experiments/engine-bench/email_eval.py --core <luoke-core> --mail <luoke-mail> --models <模型目录> --tier 16 \
    --out <目录> --arm omlx=/Applications/oMLX.app --arm "thin=<python> experiments/engine-bench/thin/thin_engine.py" \
    [--arm omlxc=/Applications/oMLX.app --hot-cache omlxc=1GB] [--concurrency 1,4] \
    [--pause-while 'luoke-release:tools/(release|internal_build)\.py']
```

- **请求体和主控交给引擎的一样**：用 luoke-core 的 `build_engine_payload`，按配方这一档 `luoke-mail.<任务>` 的参数生成
  （max_tokens、temperature、top_k、思考开关、特殊 token 中和、`max_chars`、`sender_verdict`）。
- **分拣和打分用 luoke-mail 的代码**：`triage.analyze`（带重试、日期和抄送的后处理）、`bench/run_bench.py` 的 `score()`。
  每边的结果文件格式同 `run_bench.py` 的结果，多了 `timing` 和逐次请求的 `calls`。
- **不经主控**：没有派发、功耗间歇、冷加载。比的是引擎本身；要连主控一起看，照旧跑出版的 eval。
- **引擎按主控的方式起**（`luoke_core/engine.py`）：0600 的 `settings.json` 放密钥，`serve --no-cache --no-hf-cache
  --memory-guard-gb <这一档的 guard_gb>`，只听 127.0.0.1，清掉 `HF_*`、`OMLX_*`，HOME 指到临时目录。
  `--arm` 给 .app 就起它的 `Contents/MacOS/omlx-cli`，给命令就照样传这些参数（薄引擎照收 omlx-cli 的参数）。
  `--hot-cache 名字=大小` 让那一边开 oMLX 自己的缓存，只放内存（DESIGN §5 实测过的那种配置）。
- **几边同时起、模型都放着，每封（并发时每批）轮流做、每次换先后**：宿主上有别的活时各边受的影响差不多，速度看同一次里的比。
- `--concurrency 1` 是一封接一封（和出版的 eval 一样）；`4` 是 4 封同时到，看连续批处理。
- 输出：`<边>-c<N>.json`、`summary.json`、`summary.md`（分数、每封用时、首字、每分钟封数、接上缓存的请求、引擎内存、
  判断字段和第一边一样的封数）。

### 薄引擎的运行环境

```
<oMLX.app>/Contents/Resources/Python/cpython-3.11/bin/python3.11 -m venv thin-venv
thin-venv/bin/pip install mlx==0.32.4 mlx-lm==0.32.2 transformers numpy pydantic typing-extensions apache-tvm-ffi
thin-venv/bin/pip install --no-deps xgrammar==0.2.8
```

xgrammar 声明要 torch，实际不用（导入时由 `thin/torch_stub.py` 顶上，同 oMLX）；不加 `--no-deps` 会装上约 500 MB 的 torch。

`engine_micro.py` 按 `<边>.app` 找引擎：做一个只有 `Contents/MacOS/omlx-cli` 的目录，里面一行
`exec <thin-venv>/bin/python <…>/thin_engine.py "$@"` 就行。`thin_engine.py --prefix-cache-mb 0` 关掉前缀缓存（= v2），
再加 `--max-concurrent-requests 1` 就是一次一条。

### 在出版机上跑

- 给出版让路：`--pause-while 'luoke-release:tools/(release|internal_build)\.py'`。
- 机器上还有别人的 GPU 活（图像评测、Qwen 测试），量速度只看同一次里各边的比。

## 2026-10-30 的结果

机器：出版机（Mac Studio，M3 Ultra 256 GB），宿主裸机。也是出版机，量的时候给出版让路、Qwen 测试停着。模型 `Qwen3.5-9B-4bit`，16 GB 档的参数。
版本按 internal-1030-2 钉的：luoke-core `275e7fa`、luoke-mail `6a951cb`、oMLX 0.7.2。
- 「oMLX」= 主控现在的起法（`--no-cache`）。
- 「oMLX 开缓存」= 只放内存的热缓存，1 GB，同 16 GB 档。
- 「薄引擎」= `thin/thin_engine.py`。

逐轮的经过在 #42：第一轮一次一条（comment 6515436863），第二轮连续批处理（6518280348），第三轮前缀缓存（6519047617）。

### 邮件分拣评测（email_eval.py）

| | oMLX | oMLX 开缓存 | 薄引擎 |
|---|---|---|---|
| 一封接一封：分数（basic · hard · action · safety · 误报 · dates · false due · priority · JSON 一次成） | 28 · 15 · 45 · 13 · 0 · 14 · 1 · 29 · 48 | 一样 | 一样 |
| 一封接一封：每封 / 首字（中位） | 2.15 s / 0.65 s | 2.16 s / 0.66 s | **1.63 s / 0.23 s** |
| 一封接一封：每分钟封数 | 26.3 | 26.2 | **34.3** |
| 4 封同时到：每分钟封数 | 37.1 | 36.5 | **63.5** |
| 4 封同时到：判断字段和自己一封接一封时一样 | 47/48 | 46/48 | **48/48** |
| 接上缓存的请求 | 0 | 0 | 48，各 593 token（系统提示） |
| 引擎内存（跑完） | 6.6 GB | 7.9 GB | 5.5 GB |

- 一封接一封时三边的分数和第一轮经主控跑的 eval 16 完全一样，判断字段 48/48 一样。
- 4 封同时到时，判断字段和各自一封接一封时比：
  - 薄引擎一封都没变。
  - oMLX 变了 1 封：injection-forwarded 的 priority 从 low 变成 high。这封是可疑邮件，priority 不计分。
  - oMLX 开缓存变了 2 封：同上一封，加 multi-ask 的 due_date 从 10-15 变成 10-14，正好让 hard、dates 各多对一个。
  - 批里的数值和单发不完全一样，可能改到个别判断。这次 oMLX 改了、薄引擎没改，但不保证每次都不改。
  - 主控的 eval 一条一条发，测不到这个；要看就跑 `--concurrency 1,4`。

### 引擎微基准（engine_micro.py）

| | oMLX | 薄引擎 |
|---|---|---|
| 预填充首字 1k / 4k / 16k / 32k | 0.89 / 3.38 / 14.1 / 30.5 s | 慢 0.5 / 3.3 / 2.6 / 1.1% |
| 生成速度（约 500 token 输入） | 116 tok/s | 0.97 倍 |
| 批处理效率 N = 2 / 4 / 8 | 1.10 / 1.23 / 1.35 | 1.31 / 1.48 / 1.54 |
| 3000 token 系统提示 + 不同问题，第 2–4 条首字 | 2.53 s（开缓存也一样，接不上） | 0.20 s |
| 4 轮对话，第 2–4 轮首字 | 2.5 s；开缓存 0.21–0.29 s | 0.09–0.18 s |
| 打断：16k 输入在第 1–5 秒断开，下一条多久出首字 | 0.5–3.1 s | 0.6–2.1 s |

- 批处理效率 = N × 单发用时 ÷ 这一批的总用时；1 就是一条接一条。
- 打断的快慢看断在预填充块的哪儿：两边都要等手上那一块算完。块越大停得越慢（块 8192 要 5.3 s）。

### oMLX 自己的缓存为什么帮不上分拣

- 开着只放内存的热缓存时，oMLX 只在新请求**原样接着**上一条往下说时才接得上，比如多轮对话。
- 系统提示一样、用户消息不一样，就一点接不上：分拣的 593 token 不行，3000 token 也不行，换成回答 1 / 20 / 200 token 都一样。
- 这和 DESIGN §5 10-10 在 oMLX 0.7.0 上量的「分拣命中 0%」对得上。0.7.2 的源码里，混合模型的缓存块至少 2048 token（`_ARRAYS_CACHE_BLOCK_SIZE`）。
- 开着还多占约 1.3 GB。base-path 下没有落盘的缓存文件。
- 薄引擎按消息边界（系统提示末尾、其余部分末尾、每条做完）存线性注意力的状态，所以接得上。存的是自己复制的一份，按字节封顶，默认 512 MB。

### 输入法（ime_parity.py）

- 输入法的两个模型任务（`luoke-ime.rerank` 打分、`luoke-ime.predict` 联想）不走引擎服务：主控用引擎包里的 Python 起 `worker/score_worker.py`。
- 换掉 oMLX，worker 就跑在薄引擎的运行环境上：mlx-lm 0.32.2，oMLX 带的是 0.31.4.dev132；mlx 都是 0.32.4。
- 同一个 worker，两套环境，同样的请求交替发：
  - 文字取自 luoke-mail 的合成样例邮件，加上配方自带的探针；
  - 338 次逐键重排打分：每个候选的 log 概率**逐位一样**，首选和排序全一样；
  - 41 次联想，给出的续写全一样；
  - 打分 p50 / p90：15.8 / 20.0 ms 对 15.7 / 19.6 ms；联想 23.4 ms 两边一样；前缀快照命中率一样。
- 输入法自己的评测（`engines/xiaohe/bench.py`、`engines/personal/eval_predict.py`）读负责人的私人语料，在出版账户里，这里没跑。上面的结果说明换运行环境不会改它的分数。

### 没做的

- **推测解码**：
  - MTP：配方里的 9B 4-bit 没带 MTP 头。
  - PLD：在这 48 封的真实答复上模拟，每步最多出 1.35–1.41 个 token。
- **ANE 预填充**：要用私有框架，macOS 26 上不收 4 位权重。
- **真的 16 GB 机器（M1）还没跑。** 这台的预填充约 1100 tok/s。16 GB 档按配方是 95 tok/s，前缀缓存每封省的时间会大得多，要在 16 GB 测试机上跑 `email_eval.py` 才算数。
