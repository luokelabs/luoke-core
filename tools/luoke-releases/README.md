<!-- luokelabs/luoke-releases 的 README 源稿，由 luoke-core 维护（顶层 projects/luoke-core.md），上传前先确认；英文版是同目录的 README.en.md，两份一起改、一起传。 -->

简体中文 | [English](README.en.md)

# luoke-releases：螺壳的发布

螺壳是一组在你自己的 Mac 上跑本地模型的应用：**螺壳**（菜单栏程序，管模型和推理引擎，带螺壳记忆，也负责装和升级应用）、**螺壳邮件助手**、**螺壳输入法**。这个仓库放螺壳的测试版和正式版，以及再分发要附的许可文件。

## 下载

**到 [luoke.ai](https://luoke.ai) 下载。** 打开 `Luoke.dmg`，把螺壳拖进「应用程序」，打开后照首次运行的提示走，再在「应用」里开启邮件助手或输入法。之后螺壳会自己检查更新。

- **现状**：封闭测试中。测试版（beta1 起）是这里的正式 Release，最新一版的安装盘固定叫 `Luoke.dmg`。
- **内部版**（团队每天出的版本）发在 [luokelabs/luoke-internal-builds](https://github.com/luokelabs/luoke-internal-builds)，不在这里。这里早先的内部版标成了预发布版，测试者请装测试版。
- **系统要求**：Apple 芯片的 Mac，macOS 26.2 以上，内存 16 GB 起（推荐 32 GB 以上），磁盘约 7 GB（16 / 32 GB 内存）或约 28 GB（更大内存）。

## 每个发布里有什么

| 附件 | 内容 |
|---|---|
| `Luoke.dmg` | 螺壳的安装盘 |
| 各应用的包 | 邮件助手、输入法，在螺壳的「应用」窗口里装，不用手动下 |
| 引擎包 zip | 推理引擎（[oMLX](https://github.com/jundot/omlx) 的副本，重签名、代码未改） |
| `release.json` | 发布清单：这一版的应用、引擎、模型的版本、下载地址和哈希。螺壳按它下载，逐文件核对哈希 |
| 发布说明 | 这一版改了什么、用的引擎版本、已知问题 |

模型不放在这里：放在 ModelScope 和 Hugging Face，具体仓库写在每次的 `release.json` 和发布说明里。

## 许可与署名

| 路径 | 内容 |
|---|---|
| `licenses/engine/` | 引擎包里 oMLX 的 Apache-2.0 许可（`LICENSE-oMLX.txt`），以及它自带的 Python 包清单（名字、版本、许可，`THIRD-PARTY-PACKAGES.txt`） |
| `licenses/python-runtime/` | 螺壳和应用自带的 Python 运行时（python-build-standalone）各组件的许可 |
| `licenses/models/` | 模型的来源和许可 |

- 螺壳各应用由 **螺壳实验室 Luoke Labs** 开发，代码在各自的仓库，许可见各仓库。
- 引擎包里的 oMLX 按 Apache License 2.0 再分发，版本和"代码未改"写在每次的发布说明里。它自带的 Python 包各按自己的许可，其中 GPL / LGPL 的几个（mlx_embeddings、phonemizer_fork、espeakng_loader、num2words、pycountry、soxr）未做改动，源码包见各发布的附件或 PyPI。
- 模型：Qwen3.5-9B、Qwen3.5-0.8B-Base（Apache-2.0，Alibaba Qwen），Gemma 4 26B（Apache-2.0，Google；另有 Google 的[禁止用途政策](https://ai.google.dev/gemma/prohibited_use_policy)）。实验室量化的版本在模型卡里写明上游、许可和量化方式，详见 `licenses/models/README.md`。

## 反馈和安全

封闭测试期间的反馈渠道写在给测试者的说明里；公开后在这里开 Issues。安全问题请不要在公开 issue 里写细节，用 **Security → Report a vulnerability** 私下报告（见 [luokelabs/luoke 的 SECURITY.md](https://github.com/luokelabs/luoke/blob/main/SECURITY.md)）。

© 螺壳实验室 Luoke Labs
