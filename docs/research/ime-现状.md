# 螺壳输入法（luoke-ime）现状参考

> 参考资料，不是需求。整理于 2026-10-10，取代原来的 `jiu-现状.md`：jiu（"纠"，上屏后纠正同音字）是螺壳输入法的前身，它的做法已经并进输入法的引擎里——不再等上屏后去改，而是打字时就按前文选出首选。输入法对主控的需求见 [accessory-requirements.md](accessory-requirements.md)，跨项目的分工见螺壳顶层设计 `../../../ARCHITECTURE.md` 和输入法的注册页 `../../../projects/luoke-ime.md`。实测数据写配方和评测集时参考。

## 应用概况

- **形态**：macOS 输入法（InputMethodKit），装在 `~/Library/Input Methods/LuokeIME.app`，bundle id `ai.luoke.inputmethod.Luoke`，用实验室的 Developer ID 签名。第一次装好后要用户在系统设置里手动添加输入源（macOS 26 起）；由系统的输入源机制拉起，崩溃后系统会重新拉起。
- **结构**：壳只管按键、候选窗、上屏；出候选的是"输入法引擎"——独立的子进程，经标准输入输出说 JSON 行，在 Seatbelt 沙盒里按声明放行（引擎读不到 `~/.luoke` 下别的工具的数据）。
- **自带的输入法引擎**：小鹤双拼、全拼（共用一份代码）：苹果自带的转换引擎（私有库 libmecabra）和 RIME（雾凇词库）先给出候选，本地模型再按前文逐词重排，补发更好的首选（先快后好）。
- **代码**：`~/Codes/luoke/luoke-ime/`；输入法引擎在 `engines/xiaohe`、`engines/quanpin`；打分进程是 `engines/xiaohe/scorer.py`（由 jiu 的 `lm_scorer.py` 而来）。
- **用户数据**：`~/.luoke/ime/`（设置、各输入法引擎的数据目录，包括记住的改选和本机统计）。

## 现在怎么用模型（实测）

| 项目 | 数值 | 来源 |
|---|---|---|
| 模型 | Qwen3.5-0.8B-Base，MLX 4-bit；所有 Mac 统一用它 | `engines/xiaohe/engine.json` 的选项 |
| 调用方式 | 打分：给定前文和 10–15 个候选整句，返回各自的对数概率；一次请求一批，候选共享前文；最近 16 条前缀快照 | `engines/xiaohe/scorer.py` |
| 调用时机 | 每敲一个键都可能重排一次；一次重排逐词做 1–9 次打分（平均每个音节约 0.6 次）；用户停手 0.3 秒后可能再从头选一次 | `engines/xiaohe/Refine.swift`、`main.swift` |
| 取消 | 下一个键来了，没算完的这次作废（在两次打分之间检查） | 同上 |
| 一次打分的耗时（M4，12 个候选） | 约 30–70 ms，随候选长度增加 | `engines/xiaohe/bench.py` 的拆解，在 32 GB 测试机上运行 |
| 最后一个键到重排结果（120 ms 一个键） | M4：中位数 122 ms、p90 186 ms；M5 Max：中位数 57 ms、p90 76 ms | `bench.py --typing 120` |
| 按空格时 | 壳最多再等 150 ms；停顿 100 ms 按空格时，M4 上 99.8% 赶得上 | 同上 |
| 准确率（博客 600 条） | 苹果单独 78.8% → 重排后约 90%（小鹤、全拼都是），改错极少 | `bench.py` |
| GPU 占用方式 | 打字时短促、频繁地用；停手后很快空闲 | — |
| 现在的依赖 | oMLX.app 自带的 Python 3.11 和 mlx_lm（或 Homebrew 版 oMLX）；Homebrew 的 librime；雾凇词库（随输入法引擎放在 `vendor/`）；模型在 `~/.omlx/models/local/` | `engines/xiaohe/Sources.swift` |

和 jiu 相比：

1. **调用更频繁、延迟更紧**：jiu 在上屏后判断一次（M4 上 p90 580 ms 可以接受）；输入法每个键都可能要算，最后一个键之后约 150 ms 内要拿到结果。
2. **请求来自沙盒里的子进程**：输入法引擎连不了主控的 socket，打分请求由输入法的壳（Swift）转发；对主控来说配件只有"输入法"一个。
3. **主控不可用时照常打字**：重排是可选的，苹果和 RIME 的候选随时可用。

## 证据与来源

- 准确率和速度：`luoke-ime/engines/xiaohe/README.md`、`engines/quanpin/README.md`，评测脚本 `engines/xiaohe/bench.py`（语料不进 git）。
- 早期的 jiu 资料仍在 `~/Codes/on-device-llm/ime_eval/`（README、模型对比、调研报告），作为历史参考。
