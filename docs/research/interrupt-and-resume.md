# 后台请求被打断后能不能断点续做（luoke-core 派发设计参考）

> 调研参考，不是设计结论。2026-10-08 由 4 路源码调查 + 逐条复核汇总；只读核对，没有向本机 oMLX 发请求。


**版本基准**：2026-10-08 只读核对，没有向 127.0.0.1:8000 发任何请求。

| 引擎 | 版本 | 说明 |
|---|---|---|
| oMLX | 0.7.0 | 本机 `/Applications/oMLX.app/Contents/Resources/omlx`，下文简写 `omlx/…` |
| mlx-lm | `main@9d8abd9` | oMLX 自带的 mlx_lm 与它的 `generate.py`、`models/cache.py` 逐字节相同 |
| llama.cpp | `@c811cb8` | |
| Ollama | `@e3cddc3` | v0.40.1 之后 2 个提交 |
| LM Studio mlx-engine | `@ab52366` | |

速度一律用你的实测：M5 Max 128 GB，oMLX 0.7.0rc1。

## 1. 一句话回答

**原理上能续做，在 Mac 上也很便宜**：在 27B 上停放一个 8k token 的请求只占约 0.66 GB，续做不用重算。

**但现在用的 oMLX 0.7.0 做不到**：
- 打断后台请求的唯一办法是断开连接。
- 断开后，这个请求新算出的 KV 和已生成的输出都会被丢掉，以后只能从头 prefill、再解码。
- 不用"换 context"，模型权重也不会重新加载，丢掉的只是这一个请求自己的 KV。

**要真正断点续做，有两条路**：
- 主控自己掌握推理循环（进程内运行 mlx-lm）。
- 退一步：主控保存已经收到的输出文字，以后带着它续写。

## 2. 三种做法

先看代价。下表按你的实测速度估算，不含排队时间。KV 大小按本机 8-bit 版 config 算，假设 KV 是 fp16。

| | Qwen3.8-27B oQ4e | Flash-Next | Bonsai 2 27B（M5 Max） |
|---|---|---|---|
| 2k 邮件 prefill | ≈3.1 s（655 tok/s） | ≈1.2 s（1,630 tok/s） | ≈5.6 s（356 tok/s） |
| 8k 新闻信 prefill | ≈12 s | ≈4.9 s | ≈22 s |
| 200 token JSON 解码 | 2.5–4.7 s（MTP）；≈6.7 s（不开 MTP） | 1.8–2.7 s | ≈5.6 s |
| oMLX 一个 2048 token prefill 块（prefill 中途打断时最多要等这么久） | ≈3.1 s | ≈1.3 s | ≈5.8 s |
| 停放这个请求的 KV（2k / 8k） | ≈0.27 / 0.66 GB | ≈0.16 / 0.30 GB | 未核实 |

KV 的算法：
- 27B：每 token 64 KiB（16 层全注意力，每层 4 个 KV 头 × 256 维），再加每个序列固定约 147 MiB 的 GatedDeltaNet 递归状态（fp32）。依据是 `mlx_lm/models/gated_delta.py#L583` 和 `~/.omlx/models/mlx-community/Qwen3.8-27B-8bit/config.json`。
- Flash-Next：每 token 24 KiB，固定 108 MiB。

分拣这类短输出任务里，解码和 prefill 的时间差不多。所以保住已生成的部分，和保住 prompt 的 KV 同样重要。

**业界做法**：
- vLLM 和 SGLang 抢占一个请求时都会保留它已生成的 token ID，之后把 prompt 和已生成部分拼起来重新 prefill（vLLM `vllm/v1/core/sched/scheduler.py#L1572-L1615 @b49c921`；论文 §4.5，arxiv 2309.06180）。
- 数据中心里"重算比换出便宜"（vLLM `docs/configuration/optimization.md#L27-L47`）的前提是换出要走 PCIe。Mac 是统一内存，不存在"把 KV 换到主机内存"这一层。对应的做法是原地不动地停放，唯一的下一层是 SSD。
- 最接近"两级等级"的研究是 ConServe（arXiv 2410.01228）：它在后台请求运行时就增量备份 KV，抢占时只需丢掉 GPU 上那份。

### (1) 丢弃，以后重跑，靠前缀缓存

**oMLX 0.7.0 上的实际行为**

- **怎么打断**：没有取消推理的 API，只能断开连接。
  - 流式请求断开后，`finally` 会调用 `abort_request`（`omlx/engine/batched.py:1223-1238`）。
  - 断开检测每 2 s 轮询一次（`omlx/server.py:2608-2700`）。也可能更快，但未核实。
  - 非流式请求还要先过 2 s 宽限期（`server.py:2753`）。
- **多久生效**：解码阶段在下一步生效；prefill 阶段只在每个 2048 token 块之间检查一次（`omlx/scheduler.py:4217-4240`）。源码注释也写明 Metal 不能抢占正在运行的 kernel（`scheduler.py:1517-1527`）。
  - 结果：在 27B 上，如果后台请求正在 prefill，"用户在等"的请求最坏要多等约 2 s + 3 s。
- **打断后什么都没存**：
  - `_do_abort_request` 不写前缀缓存，还会删掉快照和 `prompt_cache`（`scheduler.py:10509-10651`）。
  - 只有正常结束（stop 或 length）的请求才会写缓存（`scheduler.py:12448-12730`）。
  - 所以重跑时只能命中以前已完成请求留下的前缀。
- **本机模型的额外限制**：本机模型都是混合结构，oMLX 会把缓存块放大。
  - Qwen 放大到 2048 token，Gemma 4 放大到 1024 token（`scheduler.py:3070-3124`、`2891-2962`；本机日志里有 "Enlarging … 256 to 2048"）。
  - 短输出不进缓存（`scheduler.py:7919-7990`）。
  - 推断（未实测）：如果共享的系统提示词不足一个块，换一封邮件就命中不了。
  - `~/.omlx/stats.json` 里，Qwen3.8-27B-8bit 只有约 29% 的 prompt token 来自缓存，其他模型最高到 93%，和这个推断相符。
- **rc1 的改进帮不上这里**：0.7.0rc1 的 partial-block 缓存（下一轮 prefill 从 1,174 token 降到 37）针对的是同一对话的下一轮，前提是上一轮已正常结束。

**代价**：被打断前做的工作全部作废。
- 27B 上一封 2k 邮件解码到一半被打断：白做约 3.1 s prefill 加 1.2–3.3 s 解码。
- 8k 新闻信在 prefill 快结束时被打断：白做约 12 s。

**oMLX 上可以试的补救：两段式提交**（读代码推断，未实测）
- 先发一次 `max_tokens=1`，让 prompt 正常结束并写进缓存（混合模型会在生成提示开始处存一个尾块快照，`scheduler.py:6800-6845`），再发真正的生成请求。
- 之后生成阶段再被打断，重跑只需补几个 token，再加上恢复快照的时间。issue #3064 在 0.6.3rc2 上测到从 RAM 恢复约 0.4 s、从 SSD 约 2.0 s；0.7.0 上未核实。
- 适合 8k 这类长 prompt。

**llama.cpp 和 Ollama-MLX 天然更适合这个方案**：它们打断后会保留 KV，见第 3 节。

### (2) 暂停续做

**(2a) 原地暂停：只有进程内 mlx-lm 能做**

- **原理**：`generate_step` 是惰性生成器，cache 原地更新（`mlx_lm/generate.py#L304-L473`）。不再调用 `next()`、但留着这个对象，就是暂停。
- **实验结果**：用小型随机模型、greedy 解码测试，llama 和 qwen3_5 混合结构各测一个。中途插跑另一个请求后再继续，输出和不打断时逐 token 一致。
  - temperature 大于 0 时，续出来的是另一个合法样本，不保证和不打断时相同。
  - 在真实的 8-bit 27B 上是否逐 token 一致：未核实。
- **切换延迟**：
  - 解码阶段：一步，27B 上 12–33 ms。
  - prefill 阶段：prefill 不会让出控制权。只能在 `prompt_progress_callback` 里抛异常，在块边界停下（`#L428-L454`）；之后用 `generate_step(prompt[已处理:], prompt_cache=cache)` 可以精确续上（已实验验证）。
  - 后台请求的 `prefill_step_size` 设为 256–512 时，27B 上每块约 0.4–0.8 s。
  - 不要在 `(total,total)` 那次回调（`#L466`）里抛异常。
- **混合模型的限制**：`ArraysCache` 不能裁剪（`mlx_lm/models/cache.py#L164-L165`）。要么一直留着生成器对象，要么自己存下"已采样、还没喂回模型"的那个 token。
- **代价**：只占内存，每个请求 0.16–0.66 GB，在 128 GB 机器上可以忽略。16 GB 档要给停放总量设上限。

**(2b) 写到 SSD，再读回来**

- **工具**：`save_prompt_cache` / `load_prompt_cache`（`cache.py#L43-L112`），能保存混合模型的状态。
- **本机实测**：27B 形状、3k / 8k token 的 cache 分别是 339 / 659 MiB；写入 0.08–0.20 s，读回 0.02–0.23 s。注意这是在热页缓存里测的，没有 fsync，冷 SSD 读取未测。
- **保守估计**：llama.cpp README 的例子有效速率只有约 0.33 GB/s（`tools/server/README.md#L1150-L1186`）。照这个速率，0.66 GB 要约 2 s。
- **和重算比**：27B 上重新 prefill 8k 约 12 s，读回大概率更快。2k 邮件重算只要约 3 s，差距小，但读回能连已生成的部分一起保住。
- **什么时候需要**：统一内存下没有"主机内存"这一层，写 SSD 只在内存紧张时有意义，主要是 16 GB 档。

**(2c) 带着已生成的部分重跑：不需要引擎支持暂停**

主控用 stream=true 接收后台输出，这样打断时手里就有已生成的文字。
- **llama.cpp**：用 token 数组重新提交 prompt 加已生成部分。命中最长公共前缀后只需重算至少 1 个 token（`server-context.cpp#L3589-L3591, #L3789-L3795`），几乎等于续做。
- **oMLX**：可以把已生成部分作为最后一条 assistant 消息，加上 `partial: true`（`omlx/api/openai_models.py:104-105`）。
  - 缓存里没有这次的 KV，所以 prompt 和已生成部分要重新 prefill，2k + 100 token 在 27B 上约 3.2 s。
  - 好处是省掉重新解码那 100 token（约 1.2–3.3 s）。
- **风险**：
  - 重新分词后可能和原来的 token 对不上。
  - 有 JSON 约束时，llama.cpp 的 grammar 不会跟着 prompt 里的 token 前进，续写结果不可靠（`server-context.cpp#L418-L438`；`common/sampling.cpp#L476-L502`）。oMLX 是否也这样：未核实。
  - 对约 200 token 的分拣 JSON，整段重跑更稳。

### (3) 不打断：并发批处理

**oMLX**
- **支持，但本机现在是关的**：oMLX 通过 mlx-lm 的 BatchGenerator 支持并发，`max_concurrent_requests` 默认是 8（`omlx/settings.py:328`）。
  - 本机设成了 1（`~/.omlx/settings.json:47`），`chunked_prefill` 也是关的（:49）。
  - 所以现在"用户在等"的请求只能排在后台请求后面。
- **主控不能改**：借用用户的 oMLX 时，主控不能改它的设置（`CONTEXT.md:64-72`）。
- **开了也没有优先级，而且照顾的方向正好相反**：
  - `decode_fairness`（默认开）会在新请求 prefill 期间，把约 1/3 的 GPU 时间让给正在解码的后台请求（`scheduler.py:1517-1540`）。
  - 不开 `chunked_prefill` 时，一个长 prefill 会在单个 step 里做完。`add_request` 和 step 跑在同一个线程上（`engine_core.py:349-352`），新请求连排队都进不去。
- **MTP 会掉速**：有新请求加入时，正在跑的投机解码会退回普通解码（`patches/mlx_lm_mtp/batch_generator.py:3608-3640`）。issue #4226（M5 Ultra、Flash-Next）开了 chunked_prefill 之后：
  - 长 prefill 期间，短请求的最差首 token 延迟从 9.9 s 降到 2.4 s；
  - 但长请求的解码速度从 73.7 tok/s 降到 46.9 tok/s。
- **内存守卫**：
  - 软压力下，只要有请求在跑就不接纳新请求（`scheduler.py:11335-11348`），小内存档上并发实际会退化成串行。
  - 硬压力下会 `abort_all_requests`，不分等级（`process_memory_enforcer.py:1745-1800`）。

**Mac 上的两个特点**
- 解码速度受内存带宽限制，batch 从 1 变成 2，每步只会略慢。这是推断，未实测；MoE 模型更不确定。
- mlx-lm 会把 batch 里每一行左侧填充到最长序列的长度（`cache.py#L880-L1098`）。1k 的新请求加入 8k 的后台批，会按 8k 填充，27B 上约多占 0.45 GB。

**数据中心系统的对照**
- vLLM V1：等待中的高优先级请求不会抢占正在运行的请求（`scheduler.py#L1265-L1272`）。
- SGLang：只在 batch 满时才按优先级抢占（`schedule_policy.py#L1529-L1599 @45abea0`）。

## 3. 引擎支持矩阵

| 引擎 | 取消正在生成的请求 | 取消后前缀缓存 | 已生成部分能否续做 | KV 写 SSD / 恢复 | 并发批处理 | 优先级 |
|---|---|---|---|---|---|---|
| oMLX 0.7.0 | 只能断开 HTTP，没有推理取消 API。检测轮询 2 s；解码阶段一步内生效，prefill 阶段在块之间生效（见上） | 本请求新算的 KV 全部丢弃（`scheduler.py:10509-10651`），只剩以前已完成请求留下的前缀 | 没有缓存层面的续做；可以用 `partial: true` 重发，但要重新 prefill | 有 RAM 热层加 SSD 分页缓存（`cache/paged_ssd_cache.py:1-14`），只存已完成的请求；没有按请求保存或暂停的功能（`PREEMPTED` 状态从没被用到，`request.py:26-27`） | 支持，默认 8；本机设成 1 | 没有。先来先服务（`scheduler.py:2042`）；#3047 已转为讨论，`main@b75060e` 仍未实现 |
| mlx-lm（进程内） | 停止迭代即可；解码一步内生效；prefill 只能在回调里于块边界停；`BatchGenerator.remove`（`generate.py#L1715-L1740`） | 库本身没有自动前缀缓存；cache 原地更新，取消后仍保有 prompt 和已生成部分 | 能真正暂停（已实验验证）；混合模型要保留生成器对象，或存下那个还没喂回模型的 token | `save_prompt_cache` / `load_prompt_cache`，本机 0.66 GB 读写都在 1 s 以内（热缓存） | BatchGenerator 随时可以加入或移除请求；会左填充 | 没有，先进先出（`#L1758`） |
| llama.cpp server | 断开后把 CANCEL 插到队首（`server-queue.cpp#L602-L621`），1 s 轮询；当前批算完后调用 `slot.release()`（`server-context.cpp#L2797-L2806`） | 保留 prompt 和已生成部分的 KV，通常差最后一个 token（`#L582-L602`）。默认配置下，下一个任务启动时会把它转进 8 GiB 的 RAM prompt cache（`#L2779-L2795`），超过上限的会丢 | 基本能：按 token 重新提交，只重算至少 1 个 token；有 JSON 或 grammar 约束时不可靠 | `/slots/{id}?action=save\|restore`，要配 `--slot-save-path`；slot 必须空闲，而且要在下一个任务启动前发出 | `-np` 加连续批处理（默认 4 个 slot） | 没有（`--prio` 是线程优先级；#22560 已以 not planned 关闭） |
| Ollama | 客户端 context 会传下去（`llm/llama_server.go#L1735, #L1775-L1779`） | GGUF 模型实际跑在 llama-server 子进程里，行为同上一行；MLX runner 取消时会把已生成的 token 存进缓存（`mlxrunner/prefix_cache.go#L584-L627`） | GGUF 同 llama.cpp（通过 Ollama API 能否按 token 精确重提：未核实）；MLX runner 重试时可以从停下的地方续（`pipeline.go#L184-L190`） | 没有；不转发 `/slots`；MLX runner 的快照只放内存，上限 8 GiB | `OLLAMA_NUM_PARALLEL` 默认 1，qwen35、qwen3next 等结构强制为 1（`server/sched.go#L507-L520`）；MLX runner 严格串行 | 没有 |
| LM Studio | SDK 有 `prediction.cancel()`；mlx-engine 会把请求移出 batch（`request_lifecycle.py#L114-L142`） | mlx-engine 在 prefill 和解码过程中按 256 token 写 `/tmp` 磁盘缓存，取消后已写的部分保留（`batch_generator.py#L642-L680, #L970-L998`）；llama.cpp 引擎闭源，未核实 | mlx-engine：推断可以恢复到最近的 256 token 边界，未实测 | mlx-engine 自动写磁盘缓存（空闲磁盘不足 10 GiB 时停用，卸载模型时清空），没有显式 API | 有，默认最多 4 个并发预测 | 文档里没有 |

**补充 mlx_lm.server**：
- 没有取消端点，只能靠流式断开；非流式请求断开时检测不到（读代码，未测）。
- batch 路径会丢掉已生成部分的 KV，只保留按系统提示和用户消息分段的缓存（`server.py#L887-L896, #L830-L846`）。
- chat 接口无法续写（`#L562`）。
- 已发布的 LM Studio 是否和 mlx-engine 的 `main` 一致：未核实。

## 4. 对派发的建议（建议，不是结论）

**1. 等级逻辑放在主控的派发里。**
- Mac 上没有哪个引擎支持按请求的优先级或抢占。
- 每个共享模型同一时间最多只放一个后台请求进引擎。oMLX 是先来先服务，多放进去的都会挡在"用户在等"的请求前面。

**2. 默认做法：打断后丢弃，稍后重跑；主控保存已收到的输出文字。**
- 后台请求一律用 stream=true。
- "用户在等"的请求到来时，先估算后台请求还要多久：剩余 prefill token ÷ prefill 速度 + 剩余输出 token ÷ 解码速度。
  - 如果剩余时间比打断本身的延迟还短，就让它跑完。在 oMLX 上，打断延迟约是 2 s 检测，加上解码一步，或者 prefill 中途最多一个 2048 token 块。
  - 否则先断开后台流，再提交"用户在等"的请求。顺序很重要，因为 oMLX 的 `add_request` 要等当前 step 结束。
- 被打断的后台请求放回后台队列的最前面。约 200 token 的分拣请求直接整段重跑。

**3. 第二步：先测量，再决定做不做。**
- 长 prompt（8k 新闻信）：在 oMLX 上试两段式提交。
- 长输出：试 `partial: true` 续写。
- 如果模型目录允许某一档用 llama.cpp server，它的"取消 + 按 token 重提"已经接近免费续做。要注意 JSON 约束的问题和 `--cache-ram` 的上限。

**4. 并发批处理不作为默认。**
- 借用用户的 oMLX 时改不了它的设置。
- 主控环境里自己的引擎可以试 `max_concurrent_requests=2` 加 `chunked_prefill`。
- 但"用户在等"的请求 prefill 会被 `decode_fairness` 拖慢，MTP 也会掉速，要实测后再定。

**5. 什么时候值得做真正的暂停续做。** 大部分条件同时满足时：
- **引擎**：主控环境里用进程内 mlx-lm 做这一档的引擎（`CONTEXT.md:56` 允许）。实现上由一个推理线程独占模型，停放生成器，限制停放的总字节数，后台请求用小的 prefill 块。
- **浪费大**：打断很频繁，或者单次浪费的工作量大，比如低档机（M1 16 GB 上 Bonsai 处理一封邮件要 22–29 s）、8k 以上的长 prompt、长输出。
- **能接受的代价**：会失去 oMLX 的 MTP 和 DFlash 加速、SSD 前缀缓存和内存守卫；oQ 量化格式在原版 mlx-lm 上能否加载：未核实。
  - mlx-lm 原生 MTP 的 PR #990 没有合并（未单独核实）。
  - 27B 上不开 MTP 解码约 30 tok/s，开 MTP 是 43–81 tok/s。这个差距可能比省下的重算还大。
- **已知 bug**：mlx-lm #1845（open）：BatchGenerator 里 `ArraysCache.advance` 会泄漏，27B 单次输出超过约 10.6k token 会崩。
- **另一条路**：向 oMLX 提两个需求：
  - "abort 时也把已经算好的 prompt KV 存进缓存"，llama.cpp 和 Ollama-MLX 已经这样做；
  - 按等级接纳请求（#3047）。

## 5. 仍需实测

1. **oMLX 的打断延迟**：从流式断开到 abort 生效实际要多久；后台 prefill 中途打断时，"用户在等"的请求首 token 延迟是多少。
2. **重跑命中率**：用 `usage.prompt_tokens_details.cached_tokens` 测（`omlx/api/openai_models.py:418-434`；流式请求需要设 `stream_options.include_usage`）。要确认三件事：
   - abort 后命中是否确实为 0；
   - 换一封邮件后，系统提示词能不能命中；
   - 为什么 27B 只有约 29% 的 prompt 来自缓存。
3. **两段式提交**：`max_tokens=1` 能否命中；混合模型快照恢复在 0.7.0 上要多久。
4. **`partial: true` 续写**：续出来的 token 能否和原输出对上；能否和 luoke-mail 的 JSON 输出一起用（`chat_json` 有没有用约束解码：未核实）。
5. **并发 2 加 chunked_prefill**：首 token 延迟、两边的解码速度、MTP / DFlash 的切换损失、内存守卫的表现。
6. **冷 SSD 读回**：读回 0.3–0.7 GB KV 要多久（现有数字都是热页缓存下测的）。
7. **16 GB 档**：Bonsai 2 实际的 prefill 和解码速度、KV 结构和每个请求的大小、能停放几个请求。
8. **真实模型上的暂停续做**：在真实的 8-bit 或 oQ4e 模型上，暂停续做是否逐 token 一致（现有实验只用了小型随机模型和 greedy）；带 MTP 时草稿状态怎么保存。