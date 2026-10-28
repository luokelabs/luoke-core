# 螺壳 beta0（封闭测试版 · 演练）

这是螺壳（luoke）封闭测试前的演练版：真实下载地址、整套评测、公证过的安装包。**不是公开发布**，也没有推广；拿到链接的测试者可以装，问题请走测试群。

## 系统要求

- Apple 芯片的 Mac，**macOS 26.2 以上**（引擎里 mlx 的要求；主控首次运行会在预检页拦住更老的系统）。
- 内存 16 GB 起（推荐 32 GB 以上）；磁盘约 7 GB（16 / 32 GB 内存的档）或 28 GB（64 GB 以上）。

## 怎么装

1. 下载你要的应用的安装器：`install-mail-beta0.dmg`（邮件助手）或 `install-ime-beta0.dmg`（输入法）。两个 DMG 里是同一个签名公证过的螺壳主控，文件名叫「安装邮件助手」/「安装输入法」，只是预选的应用不同。
2. 打开 DMG，双击里面的「安装…」。macOS 会问是否打开从互联网下载的应用，点"打开"。它会把主控装进你个人的「应用程序」文件夹（`~/Applications/螺壳主控.app`）再从那里启动。
3. 首次运行六步：看这台 Mac 是哪一档 → 勾选要装的应用 → 一屏说清要下载什么、多大 → 下载安装（邮件助手装好就先打开，可以先连邮箱；模型在后台继续下）→ 本机检查（模型能加载、能出合法格式）→ 注册登录项。
4. 输入法装好后还要在「系统设置 → 键盘 → 输入法」里添加「螺壳输入法」（主控会提示）。

## 评测

三档（16 GB 16 GB 测试机（M1） M1、32 GB 32 GB 测试机（M4） M4、128 GB M5 Max）共 20 条汇总，和 2026-10-14 的基线比没有回退（`diff.md`）；16 GB 档的"从打字里归纳"硬性条目没过，配方里这一档不支持该任务（主控对它答 unsupported），其余全部通过。分数和证据哈希在 `evidence/` 与配方 `recipes/beta0.json`。

| 任务 | 配件版本 | 模型 | 档 | 分数 | 硬性条目 |
|---|---|---|---|---|---|
| luoke-ime.predict | 0.1.3 | qwen3.5-0.8b-base-4bit | 128 GB | 42.5/100.0 | 过 |
| luoke-ime.rerank | 0.1.3 | qwen3.5-0.8b-base-4bit | 128 GB | 89.8/100.0 | 过 |
| luoke-mail.cleanup | 0.12.5 | qwen3.5-9b-4bit | 16 GB | 38/40 | 过 |
| luoke-mail.cleanup | 0.12.5 | qwen3.5-9b-4bit | 32 GB | 38/40 | 过 |
| luoke-mail.cleanup | 0.12.5 | gemma-4-26b-8bit | 128 GB | 39/40 | 过 |
| luoke-mail.learn-orgs | 0.12.5 | qwen3.5-9b-4bit | 16 GB | 4/8 | 过 |
| luoke-mail.learn-orgs | 0.12.5 | qwen3.5-9b-4bit | 32 GB | 5/8 | 过 |
| luoke-mail.learn-orgs | 0.12.5 | gemma-4-26b-8bit | 128 GB | 7/8 | 过 |
| luoke-mail.learn-sent | 0.12.5 | qwen3.5-9b-4bit | 16 GB | 12/14 | 过 |
| luoke-mail.learn-sent | 0.12.5 | qwen3.5-9b-4bit | 32 GB | 13/14 | 过 |
| luoke-mail.learn-sent | 0.12.5 | gemma-4-26b-8bit | 128 GB | 12/14 | 过 |
| luoke-mail.senders | 0.12.5 | qwen3.5-9b-4bit | 16 GB | 25/25 | 过 |
| luoke-mail.senders | 0.12.5 | qwen3.5-9b-4bit | 32 GB | 25/25 | 过 |
| luoke-mail.senders | 0.12.5 | gemma-4-26b-8bit | 128 GB | 25/25 | 过 |
| luoke-mail.triage | 0.12.5 | qwen3.5-9b-4bit | 16 GB | 39/45 | 过 |
| luoke-mail.triage | 0.12.5 | qwen3.5-9b-4bit | 32 GB | 39/45 | 过 |
| luoke-mail.triage | 0.12.5 | gemma-4-26b-8bit | 128 GB | 44/45 | 过 |
| luoke-mem.typing | 0.15.0 | qwen3.5-9b-4bit | 16 GB | 96.9/100.0 | 没过（这一档不支持） |
| luoke-mem.typing | 0.15.0 | qwen3.5-9b-4bit | 32 GB | 97.9/100.0 | 过 |
| luoke-mem.typing | 0.15.0 | gemma-4-26b-8bit | 128 GB | 82.3/100.0 | 过 |

## 这一版带的

| 项目 | 版本 |
|---|---|
| 螺壳主控（安装器、模型 / 引擎管理） | 0.1.0.dev1 |
| 螺壳邮件助手 | 0.12.5 |
| 螺壳输入法 | 0.1.3 |
| 螺壳记忆（随邮件助手装） | 0.15.0 |
| 引擎包（oMLX 副本，重签名、代码未改） | omlx-0.7.0 |
| 模型（ModelScope） | Qwen3.5-9B-4bit（16 / 32 GB 档底座）、Qwen3.5-0.8B-Base-4bit（输入法打分）、gemma-4-26b-a4b-it-8bit（64 GB 以上档底座） |

下载来源：主控和引擎包在本仓库的 Releases，模型在 ModelScope（`zhixianio/…`，发布清单 `release.json` 里逐文件带哈希，主控核对后才用）。

## 已知问题

- 主控、安装器、各应用还没有图标（Finder 和 Gatekeeper 弹窗里是通用图标）。beta1 换上。
- 没有卸载界面：退出主控后删 `~/Applications/螺壳主控.app`、`~/Library/Application Support/luoke-core`，在「系统设置 → 通用 → 登录项与扩展」里移除登录项；输入法先在输入法设置里移除再删 `~/Library/Input Methods/LuokeIME.app`。记忆文件 `~/.luoke/mem.db` 归你，不会被删。
- 16 GB 内存的 Mac：记忆不从打字里归纳常用说法（这一档没通过评测）；输入法用小档、每键约 150 毫秒。

## 许可

螺壳各应用由螺壳实验室 Luoke Labs 开发。引擎包里的 oMLX 按 Apache License 2.0 再分发（`licenses/engine/`，含它自带 Python 包的许可清单；GPL / LGPL 的几个包未改动）。三个模型都是 Apache-2.0（`licenses/models/`；Gemma 另见 Google 的禁止用途政策）。应用自带的 Python 运行时许可在 `licenses/python-runtime/`。
