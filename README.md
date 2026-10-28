简体中文 | [English](README.en.md)

# 螺壳

**螺壳（luoke-core）** 让你的 Mac 用上本地 AI：模型下载、加载、内存和功耗都由它来管，螺壳的各个应用（螺壳邮件助手、螺壳输入法等）共用这一份模型和一份记忆。你不用挑模型，每台 Mac 用什么模型，都是实验室实测后写进"配方"的。

## 它是什么

螺壳是一个菜单栏程序，在螺壳家族里负责推理：本机所有的模型推理都经过它，包括加载、排队、内存紧张时让路、什么时候休息。配方是实验室发布的，规定每个任务用哪个模型、哪个推理引擎（钉住版本、不改代码的 [oMLX](https://github.com/jundot/omlx)），以及按内存分的每一档开哪些任务。配方里的每个选择都有评测证据。

螺壳也负责装应用：在「应用」窗口里勾选应用，它会装好、升级，需要时退回上一版，卸载时只删应用自己声明的数据。螺壳记忆打包在螺壳里（菜单「记忆」）。

整个家族的分工见 [luokelabs/luoke](https://github.com/luokelabs/luoke)，系统文档里螺壳这一页：[luoke-atlas · 螺壳](https://luokelabs.github.io/luoke-atlas/#core)。

## 现状

**封闭测试中（beta1）**。对外接口（v1 约定）已冻结，见 [docs/V1.md](docs/V1.md) §2、§8。

- Apple 芯片的 Mac，macOS 26.2 以上（推理引擎要求 26.2；螺壳本身最低 14，但没有引擎什么都做不了）。
- 内存 16 GB 起，推荐 32 GB 以上。按内存分三档（16 GB、32 GB、128 GB），每一档的模型和开着的任务由配方决定。
- 磁盘：模型和引擎约 7 GB（16 / 32 GB 档），或约 28 GB（更大内存的档）。

数字的出处：[docs/community/luoke-core.md](docs/community/luoke-core.md)、[docs/V1.md](docs/V1.md)（首次运行的预检、硬件档）。

## 怎么用

到 [luoke.ai](https://luoke.ai) 下载螺壳，打开后照首次运行的提示走，然后在「应用」里开启你要用的应用。测试版和正式版发在 [luokelabs/luoke-releases](https://github.com/luokelabs/luoke-releases)。

## 从源码构建

要用 Xcode 或命令行开发者工具（Swift 5.9 以上）和系统自带的 `/usr/bin/python3`。主控进程只用 Python 标准库。

```bash
python3 -m unittest discover -s tests          # 单元测试
sh app/build_app.sh                            # 打出开发版 app/build.noindex/螺壳（开发）.app
sh app/build_app.sh app/build.noindex --install   # 再装到 ~/Applications
```

- 开发版的 bundle id 是 `ai.luoke.core.dev`，数据在 `~/Library/Application Support/luoke-core-dev/`，和正式版互不影响。
- 第一次构建会从 GitHub 下载钉住版本的 Python 运行时（[python-build-standalone](https://github.com/astral-sh/python-build-standalone)），校验后缓存在 `~/Library/Caches/luoke-core/`。
- 构建时会找并排放着的 [luoke-mem](https://github.com/luokelabs/luoke-mem) 仓库（`../luoke-mem`，或用 `LUOKE_MEM_SRC` 指定），找不到就打出不带螺壳记忆的一份。
- 开发版没有发布清单，首次运行不会下载任何东西：引擎要自己装官方 oMLX，版本等于配方 `engine.version` 里的版本；模型放在 `~/.omlx/models` 里，目录名用配方 `models` 里写的。缺什么，首次运行的检查页会写明。

不打包、直接跑主控进程：

```bash
python3 -m luoke_core serve --recipe recipes/dev.json --model-dir <模型目录> --env <环境目录> --socket /tmp/luoke-hub.sock
python3 tools/hub_smoke.py --socket /tmp/luoke-hub.sock      # 把每个端点走一遍，--dispatch 再测派发
```

给应用开发者：应用怎么接入螺壳（打包、调模型、交评测集、数据和升级）见 [docs/DESIGN.md](docs/DESIGN.md) §11 和 [docs/V1.md](docs/V1.md) §8，Python 客户端在 [`luoke_core/client.py`](luoke_core/client.py)，自带 Python 的打包脚本在 [`tools/package_python_app.py`](tools/package_python_app.py)。

## 数据和隐私

推理只在本机。螺壳起的推理引擎只听 127.0.0.1 上的随机端口，每次启动换一把随机密钥；不开 MCP，也不调引擎的联网搜索接口。应用发来的请求由螺壳按固定字段重写，不带工具调用。没有遥测，也没有崩溃上报。「导出诊断信息」只存成文件。

螺壳会连外网的只有两件事：

- **检查更新**：连 `api.github.com` 和 `github.com`，默认自动查（测试版每天一次），只提示、不自动装；「检查更新」窗口底下的「每天自动检查」能关。
- **下载**：首次运行、在「应用」里装或升级、升级后补齐时，从 `modelscope.cn`、`huggingface.co` 下模型，从 `github.com` 下推理引擎和应用；首次运行以后缺了东西会自动补下，按流量计费的网络上不自动下。

给高级用户的本机端点（OpenAI 兼容，设置 → 高级）默认关，打开后也只听 127.0.0.1。

数据放在 `~/Library/Application Support/luoke-core/`（模型、引擎、日志、设置）。螺壳本身不读记忆；记忆在 `~/.luoke/mem.db`，归用户，卸载时永远不删。

联网清单逐条对着代码列在 luoke-audit 的审查里；规则见 [docs/DESIGN.md](docs/DESIGN.md) §8。

## 文档

| 文件 | 内容 |
|---|---|
| [CONTEXT.md](CONTEXT.md) | 术语表 |
| [docs/V1.md](docs/V1.md) | v1 规格：对外约定、首次运行、安装和卸载、评测和发布 |
| [docs/DESIGN.md](docs/DESIGN.md) | 长期设计：全部规则和理由 |
| [docs/adr/](docs/adr/) | 难以反悔的决定及理由 |
| [docs/experiments/](docs/experiments/) | 实测记录：各档底座、共存、引擎上限 |
| [docs/research/](docs/research/) | 调研材料（参考，不是结论） |
| [tools/release_evals.json](tools/release_evals.json)、[tools/support.py](tools/support.py) | 发布前评测：每个任务怎么评、底线，哪一档开着 |

## 许可、安全、贡献

- 许可：[Apache-2.0](LICENSE)；名称、图标和品牌素材不随许可授予，第三方代码的许可见 [NOTICE](NOTICE)。
- 安全问题请私下报告，见 [SECURITY.md](SECURITY.md)。
- 想参与，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

© 螺壳实验室 Luoke Labs
