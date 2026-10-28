# 再分发许可核查（上传前必做，V1 §10 第 1 项）

- **状态**：2026-10-13 初查。公开发布等于再分发：主控 DMG、引擎包（oMLX 副本重签）、模型（ModelScope）每一样都要能合法再分发。有问题的地方停下问负责人（见 §4）。
- **查法**：模型看模型卡的 `license` 字段和 LICENSE 文件；oMLX 看它仓库的 LICENSE；引擎包里带的 162 个 Python 包看各自 `METADATA` 的 License 字段。

## 1. 模型

| 模型 | 许可 | 来源 | 再分发 / 量化版 |
|---|---|---|---|
| Qwen3.5-9B（底座，16 / 32 GB 档） | Apache-2.0 | 模型卡 `license: apache-2.0`，无附加限制；社区有数百个量化版 | 可以；保留许可和 NOTICE |
| Qwen3.5-0.8B-Base（打分模型） | Apache-2.0 | 模型卡 `license: apache-2.0`，`license_link` 指向 Qwen 的 LICENSE | 可以 |
| gemma-4-26b-a4b-it（底座，128 GB 档） | Apache-2.0 | 模型卡 `license: apache-2.0`，`license_link` 指向 ai.google.dev 的 Gemma 4 许可页，页面正文就是 Apache 2.0 全文；Gemma 4 起不再用自定义的 Gemma Terms | 可以；按 Apache §4 给许可副本、标明修改（量化）、保留 NOTICE。页面另链接"禁止用途政策"和"使用条款"，正文没给，发布说明里引用一下链接即可 |
| gemma-4-e4b-it-4bit（只在实测里用过） | 模型卡写 `license: gemma`（社区量化版沿用旧标签） | — | 不在 v1 配方里，不发 |

实验室自己量化的版本（ADR 0003）同样按上游许可发：Apache-2.0 允许修改和再分发，量化就是"修改"，要在模型卡里写明来源和量化方式、附 LICENSE 和 NOTICE。

## 2. oMLX 本体

Apache License 2.0，"Copyright 2025 oMLX contributors"，仓库 `github.com/jundot/omlx`，没有附加条款。重签后再分发：保留它的 LICENSE 和 NOTICE，发布说明写明版本和未改代码（ADR 0001）。

## 3. 引擎包里带的 Python 包（oMLX.app 内 162 个）

绝大多数是 MIT / Apache / BSD / PSF。**不是宽松许可的**：

| 包 | 许可 | 干什么用 | 主控用不用 |
|---|---|---|---|
| `mlx_embeddings` | **GPLv3** | oMLX 的嵌入引擎 | 不用（2026-10-16 加的嵌入任务走主控自己的工作进程 `worker/embed_worker.py`，用引擎包里的 mlx-vlm（MIT）加载，不经 oMLX 的嵌入引擎、不 import `mlx_embeddings`） |
| `phonemizer_fork` | **GPLv3+** | 语音合成的音素化（mlx_audio 链） | 不用 |
| `espeakng_loader` | 未标（espeak-ng 是 GPLv3） | 同上 | 不用 |
| `num2words` | LGPL | 同上 | 不用 |
| `pycountry` | LGPL-2.1 | 同上 | 不用 |
| `soxr` | LGPL-2.1+ | 音频重采样 | 不用 |
| `openai_harmony` | 未标（上游是 Apache-2.0） | gpt-oss 的格式 | 不用 |

GPL / LGPL 允许再分发，条件是附许可文本并让接收者能拿到对应源码（这些都是未改动的 PyPI 包，给出名字、版本和 PyPI 地址即可，也可以在发布仓库放一份源码包以稳妥）。它们和主控的代码没有链接关系（只在 oMLX 进程里），不影响主控自己的许可。

## 3b. 打包脚本给 Python 配件带的运行时（python-build-standalone 3.12.15+20261009，install_only）

查了该发布的 `PYTHON.json`（扩展模块的 `licenses` / `license_path`）和它的 `licenses/` 目录（在 `-full` 包里，install_only 包不带）。**全部是宽松许可，没有 GPL**：CPython（Python-2.0 / CNRI）；OpenSSL 3.5（Apache-2.0）；SQLite（公有领域）；xz / liblzma（0BSD）；libffi、expat（MIT）；mpdecimal（BSD-2）；libedit（readline 模块用的是它，不是 GNU readline；BSD-3）；libuuid（BSD-3）；bzip2；zlib；ncurses、libX11 / libXau / libxcb（X11 / MIT）；Tcl / Tk、tix（TCL）。这些在 install_only 包里大多静态链进 `libpython3.12.dylib`，只有 `_tkinter` 带着 `libtcl9` / `libtcl9tk9` 动态库。

处理：许可全文和一份汇总（`THIRD-PARTY-LICENSES.txt`）放在仓库 `tools/runtime-licenses/<运行时版本>/`，打包脚本自动拷进每个 .app 的 `Contents/Resources/licenses/`；换运行时版本时重新生成这个目录。luoke-mail、luoke-mem、主控都由此带上。

## 4. 已定的和待定的

1. **引擎包里的 GPL 包：原样带着**（2026-10-15 定，按推荐）：不动引擎包；发布仓库里放引擎包全部 Python 包的许可清单（发布脚本 engine 阶段生成的 THIRD-PARTY-PACKAGES.txt）和这 7 个 GPL / LGPL 包的源码包（或 PyPI 地址），发布说明注明。负责人同时说明：螺壳系列本身都是开源项目（代码仓库现在私有，公开发布时开源）。原来的两条路：
   - A（推荐）：原样带着，发布仓库里放一份 `THIRD-PARTY-LICENSES.txt`（162 个包的名字、版本、许可、来源）和 GPL/LGPL 包的源码包，发布说明里注明。不动 oMLX 的包，和 ADR 0001"一行不改"一致，也不会因为少了包让 oMLX 起不来（它启动时把 mlx_embeddings 当一个引擎注册）。
   - B：从引擎包里删掉 v1 用不到的 GPL 包（嵌入、语音合成那一串）。更干净，但要实测 oMLX 少了它们还能不能起，以后 oMLX 升级每次都要重做，而且已经偏离"不改引擎包"。
2. **Gemma 4 的"禁止用途政策"：同意屏不提，只在发布说明和模型卡里放链接**（2026-10-15 定，按推荐；见顶层 `docs/release-v1.md`「要负责人定的」）。理由：Apache 本身不要求，只有 128 GB 档用 Gemma，模型本身 Apache-2.0；Google 的页面把政策和许可并列，给链接即可。

## 5. 发布前的清单

- [x] Python 配件和主控的 .app 里带自带运行时的第三方许可（打包脚本自动放）。
- [ ] 发布仓库里放：oMLX 的 LICENSE / NOTICE；引擎包里 Python 包的清单（发布脚本的 engine 阶段生成 `build.noindex/release/<发布号>/engine/THIRD-PARTY-PACKAGES.txt`：名字、版本、许可）；7 个 GPL/LGPL 包的源码包（发布脚本 engine 阶段顺带从 PyPI 下的 sdist，待做）；各模型的 LICENSE 和来源说明。
- [ ] 实验室量化的模型卡写清上游、许可、量化方式。
- [x] §4 两条定了（10-15）；发布说明模板要带：oMLX 版本和未改代码、GPL/LGPL 包清单和源码包、各模型许可和 Gemma 禁止用途政策链接。
