# 开发实例：本机起一份整个螺壳给负责人看

本机开发不走内部版：负责人看产品线会话做的东西时，会话直接在本机起开发构建。开发实例是一份完整的螺壳.app，带主窗口、agent 核心、记忆、邮件助手、搜索和扫描，用的是负责人真实的数据，和直接用正式版一样。一切以开发顺畅为准。

## 一条命令

在自己的 luoke-core worktree 里运行：

```sh
tools/dev_app.sh <名字>            # 打包并起来
tools/dev_app.sh <名字> --stop     # 停，再打开本机装着的螺壳
tools/dev_app.sh <名字> --status   # 在不在跑、环境目录、socket
tools/dev_app.sh <名字> --open     # 不重打，起上次打好的
tools/dev_app.sh <名字> --build    # 只打包
```

- **名字**：小写字母、数字和 `-`，用会话或功能名，例如 `agent`、`mail-today`。`lab`、`dev`、`core` 留给已有的构建，不能用。
- **代码从哪来**：默认用 `~/Codes/luoke` 下各项目的检出，打包前先拉到最新的 main（检出不在 main 上、或有没提交的改动时照原样用）。在自己 worktree 里改的，把对应的变量指过去；设成空的就不带：

  | 变量 | 默认 |
  |---|---|
  | `LUOKE_AGENT_SRC` | `~/Codes/luoke/luoke-agent`（检出太旧、没有 `window/` 时脚本会停下） |
  | `LUOKE_MEM_SRC` | `~/Codes/luoke/luoke-mem` |
  | `LUOKE_MAIL_SRC` | `~/Codes/luoke/luoke-mail` |
  | `LUOKE_SEARCH_SRC` | `~/Codes/luoke/luoke-search` |
  | `LUOKE_SCAN_PKG` | 扫描 release 里最新的打好的包（`gh release download`），和装进正式版的同一种。给 `LUOKE_SCAN_SRC` 就从源码打，那要扫描的开发环境（Go、Homebrew 的 ClamAV 和 YARA），负责人的 Mac 上不装 |
  | `LUOKE_ENGINE_PKG` | 不带。给 luoke-engine release 里的 tar.gz 就把螺壳引擎打进去（配方要的正是这一版时才用它） |

  扫描现在在开发构建里起不来：release 的包编进了发布团队，只认 Developer ID 签名的螺壳起它、只用 Developer ID 签名的工具，任务都退 77，`luoke-scan.log` 写「不是螺壳起的」。扫描每次 release 另出一个不编团队的包之后，脚本默认改用那个。

  例：`LUOKE_AGENT_SRC=~/Codes/luoke/luoke-agent/.claude/worktrees/<你的> tools/dev_app.sh agent`
- **默认用界面重构的分支**：不带参数就是 `DEV_BRANCH=ui-refactor`，负责人和各会话用同一条命令、看到同一个螺壳。主控、agent、记忆、邮件、搜索里有 `ui-refactor` 分支的，用它最新的提交（克隆在 `app/build-cache.noindex/src/<项目>`，不动各项目的检出）；没有的用 main。主控的代码：在 main 上的检出里跑，换成 `ui-refactor` 最新的提交；在别的分支（会话自己的 worktree）上跑，就用这份。`DEV_BRANCH=` 设成空的，都用 main。上面几个 `*_SRC` 变量给了的，照给的用。
- **跳过首次运行**：`DEV_FIRST_RUN=0 tools/dev_app.sh <名字>`，起来直接是平常的样子。只管这一次，没走完首次运行的实例下次照旧弹出来。不要自己写 `firstRunDone`。

## 数据是真的

开发实例和正式版用同一份数据：

| 东西 | 在哪 |
|---|---|
| 记忆 | `~/.luoke/mem.db` |
| agent | `~/.luoke/agent` |
| 邮件助手 | `~/.luoke-mail`，钥匙串里的授权码照用，连的是真实的邮箱 |
| 搜索、扫描 | `~/.luoke-search`、`~/.luoke-scan`；扫描查、清的是这台 Mac 的真文件 |
| 装的应用 | `~/Applications`、`~/Library/Input Methods` |
| 主控自己的 | `~/Library/Application Support/luoke-core`：状态、设置、日志、模型、引擎、socket（`run/hub.sock`） |

- **顶替装着的螺壳**：输入法、邮件助手这些按 `luoke-core/run/hub.sock` 找主控，开发实例开着时找到的就是它。
- **同时只开一个螺壳**：数据是同一份，两个螺壳同时开着，主控抢 socket，邮件助手、agent 抢数据。所以起实例之前，脚本先停掉在跑的别的螺壳：本机装着的正式版、开发版、别的开发实例。`--stop` 之后再打开本机装着的螺壳。螺壳起来时看到 socket 上已经有主控在应答，就把开着的那个叫到前面、自己退出；主控进程也一样，不抢 socket（退 4）。
- **仍然要先问负责人的**：永久删除负责人的数据（邮件、文件、记忆库）、对外发信、改系统设置。钥匙串里的凭据只用，不删、不覆盖。
- 普通的实验室构建（`ai.luoke.core.lab`，评测和自动化测试用）照旧用自己环境目录里的数据，不碰真的。

## 每个实例各自的

每个实例的身份由名字决定，都是实验室构建：

- **bundle id**：`ai.luoke.core.<名字>.lab`
- **包名**：「螺壳（<名字>）」
- **包**：worktree 里的 `app/build-<名字>.noindex/螺壳（<名字>）.app`。跑着的时候登记在 LaunchServices 里：通知中心靠它认这个 app，系统设置的通知里有它自己的一行；`luoke://` 链接也落到它。`--stop` 时注销，链接回到本机装着的螺壳。
- **首次运行进度、系统权限**：按 bundle id 各一份。主控的设置在环境目录里，和装着的螺壳同一份。完全磁盘访问等权限按签名记，开发构建是临时签名，重打一份可能要重新授权。
- **不自己升级**：要新的就重打一份。

**配方和发布清单**：默认取本机装着的 `/Applications/螺壳.app` 里那一对，用的模型和引擎和本机在用的是同一版。要换就设 `DEV_RECIPE`、`DEV_RELEASE`。

**模型和引擎**：用装着的螺壳的那一份。配方要的这里没有，主控自己补下。

## 要注意的

- **内存**：首次运行走完后，引擎会加载本档的底座。128 GB 的机器上是 gemma-4-26b 8-bit，约 28 GB。用完就 `--stop`。
- **实验室构建放宽的**：配方里的任务全开，配件哪一版都认。装应用、装引擎照样核签名，只给本体用的接口照样核是不是本体。只在本机开发用，不发给别人。
- 不要在 Finder 里打开 `build-*.noindex`：Finder 显示到的 .app 会被登记进 LaunchServices。打开过的话，跑一次 `lsregister -u <那个 .app>` 注销。
- **清理**：删掉 `app/build-<名字>.noindex`。环境目录是装着的螺壳的，不删。

## 和别的起法的区别

- `tools/dev_hub.sh`：只起主控进程，不起 .app，给应用接入、调接口用。环境目录是 `luoke-core-<名字>`，数据不是真的。
- `app/build_app.sh --install`：普通开发版 `ai.luoke.core.dev`，只有一份，环境目录、数据都和装着的螺壳同一份。给负责人看东西用开发实例。
- 内部版：照旧每天两版，给团队和测试者。

## 本机正式版

开发实例是临时签名、bundle id 带 `.lab`，系统给正式版开的权限（通知、完全磁盘访问、照片图库）它拿不到，页面上的权限状态在开发实例里验不了。要在本机验这类只有正式版才看得出的东西，打一份本机正式版：

```sh
tools/local_app.sh --install
```

- **是什么**：整个螺壳.app，bundle id `ai.luoke.core`，Developer ID 签名并公证；版本号 `local-MMDD-HHMM`，设置里标「本机版，没上传」，不从网上查更新。不跑评测、不上传。
- **代码**：和开发实例一样，各项目有 `ui-refactor` 的用它最新的提交，没有的用 `main`；`LUOKE_AGENT_REF` 这类变量可以单独钉某个项目（例如只换 agent，其余照上一版，提交号见上一版的 `local.json`）。主控在 `main` 的检出里跑换成 `ui-refactor`，在会话自己的 worktree 里跑用那份，要求没有没提交的改动。扫描用它 release 里最新的包。
- **配方和发布清单**：取装着的螺壳里那一对，只换版本号和打包时间：模型、引擎和本机在用的同一版。改了主控的配方或要评测的，走 `internal_build.py --tag local-MMDD-HHMM`。
- **换装**：`--install` 先退出在跑的螺壳，旧包挪到 `app/backup-<旧版本号>.noindex/`（要退回就拿它换回去），新包拷进 `/Applications` 后打开。只打不装去掉 `--install`，包在 `app/build-<版本号>.noindex/app/`。
- **要注意**：签名公证要读钥匙串，Mac 保持解锁。它换掉的是负责人在用的螺壳，装之前先问负责人。用完的 `build-local-*`、`backup-*` 目录自己删。
