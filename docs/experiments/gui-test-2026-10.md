# 16 GB 测试机从 DMG 的 GUI 实测（2026-10-15）

- **机器**：16 GB 测试机，Mac mini M1 16 GB，macOS 26.2（25C56），干净状态（没有 `~/Applications`、`~/.luoke`、主控环境目录；只有实验室用的 `luoke-core-lab` 环境和 `/Applications/oMLX.app` 0.7.0）。
- **怎么操作**：开了屏幕共享，用 computer-use 在"Screen Sharing"窗口里点；Screen Sharing 的原始键盘输入把字符都变成 a，所以 URL 和应用用 `ssh 16 GB 测试机 open …` 交给 16 GB 测试机自己的 Safari / LaunchServices；模态对话框（Safari 的下载许可、Gatekeeper）点不动但 Return 能按默认按钮。
- **测试构建**：`build.noindex/release/test-16/`（测试用的发布号，16 / 128 两档，`--url-base http://<开发机>:8765`），主控 0.1.0.dev1，引擎包 omlx-0.7.0，配件 luoke-mail 0.12.3、luoke-mem 0.14.1、luoke-ime 0.1.0；开发机上 `python3 -m http.server 8765 --bind <内网地址>` 只在测试期间开着。
- **截图**：`docs/experiments/gui-2026-10-15/`（按步骤编号；01、16 两张带了内网地址，没放进来）。

## 每一步

| # | 步骤 | 结果 | 备注 |
|---|---|---|---|
| 1 | Safari 下载 `安装邮件助手.dmg` | 通过 | 弹"允许从 <开发机> 下载"（01）；文件带 `com.apple.quarantine` `0083;…;Safari;…` |
| 2 | 打开 DMG | 通过 | Finder 窗口：先读我.txt + 安装邮件助手（02）；安装器 .app 没有图标，显示通用图标 |
| 3 | 打开 安装邮件助手.app | 通过 | Gatekeeper："is an app downloaded from the Internet… Apple checked it for malicious software"，默认 Open（03） |
| 4 | 安装器自拷贝到 `~/Applications/螺壳主控.app` 并重启 | **第一次失败，修后通过** | 拷贝、去 quarantine、lsregister、`spctl` accepted（Notarized Developer ID）都对；但 `openApplication(at:)` 落回安装器自己，安装器退出后什么都没剩下。修法见下。修后：跑的是 `~/Applications/螺壳主控.app/Contents/MacOS/LuokeCore`，没被 App Translocation |
| 5 | 首次运行：预检 | 通过 | "内存 16 GB → 16 GB 档 / 可用磁盘 152.3 GB"，三个应用的预期文字（04） |
| 6 | 选应用 | 通过 | 邮件助手预选；输入法可选（26.2 ≥ min_macos 26）（05） |
| 7 | 下载计划 + 同意 | 通过 | 9B 5.6 GB、0.8B 424 MB、邮件助手 34 MB、记忆 33 MB，合计 6.0 GB（06）。引擎包没列：16 GB 测试机的 `/Applications/oMLX.app` 版本和配方一致，按新规则当作已装 |
| 8 | 下载安装 | 通过 | 14:20:47 → 14:25:20（4.5 分钟，局域网）；记忆、邮件助手 3 秒内装到 `~/Applications`；进度文字正常（07） |
| 9 | 装完就打开邮件助手 | **第一次失败，修后通过** | macOS："You can't use this version of the application 螺壳邮件助手 with this version of macOS"（08）。根因是启动器二进制的部署目标 27.0，见下。重打后在 26.2 正常打开，Safari 显示邮件助手 0.12.3 开始页（09） |
| 10 | 冒烟检查 | 通过 | 下载完重启引擎 → 加载 9B 4.5 s → smoke ok prefill 52.8 / decode 11.4 tok/s → 卸载；omlx-server RSS 5.2 GB（10） |
| 11 | 登录项 + 完成 | 通过 | macOS 弹 "Login Item Added"；窗口"已注册登录项"（11）。firstRunTimeline：preflight → accessories → consent → fetching → smoke → done，共 6 分 48 秒 |
| 12 | 之后 | 通过 | 主控常驻菜单栏，`/status` phase running、tier 16、9B 已加载（邮件助手连上后触发）；邮件助手进程在跑 |

## 抓到的问题

1. **安装器交接落回自己**（`app/Sources/LuokeCore/Installer.swift`）：安装器和已装的主控是同一个 bundle id，`NSWorkspace.openApplication(at:)` 被 LaunchServices 当成"打开已在跑的那个"，reopen 发给了安装器自己（它取走 preselect、显示首次运行、按流程 terminate）。证据：`~/Library/Preferences/ai.luoke.core.plist` 的 firstRunTimeline 只有一条 preflight，`run/` 为空，没有进程。修法：已装的在跑 → 只 `activate` 它，主控的状态定时器每 3 秒取 `run/preselect.json`；没在跑 → `OpenConfiguration.createsNewApplicationInstance = true`。在开发机 上没暴露，因为那里主控一直在跑（走的是交接路径）。
2. **Python 配件在 macOS 26 上打不开**（`tools/package_python_app.py`）：启动器 Launcher.swift 用 swiftc 编译时没给 `-target`，`LC_BUILD_VERSION minos` 成了打包机的 27.0；Info.plist 的 `LSMinimumSystemVersion` 14.0 不起作用。修法：`-target arm64-apple-macosx14.0`（缓存键带上目标）。影响：用旧脚本打的邮件助手、记忆（公证版）都要重打重新公证；主控（swift build，platforms .v14）和输入法（minos 14.0）没事。在开发机（27.0.1）上同样不暴露。
3. **主控 / 安装器没有图标**：Info.plist 没有 `CFBundleIconFile`，Finder、Gatekeeper 弹窗、"You can't use…"都是通用图标。待做（要一份 .icns）。
4. 小：`<env>/downloads/` 下多出空目录 `downloads/` 和 `apps/`（`fetch_item` 对 zip 项的暂存目录算法）。

## 顺带定的

- 配方 `engine.app` 指的本机 oMLX.app（开发版用法）只有 Info.plist 版本和配方引擎版本一致（"0.7.0" ↔ "omlx-0.7.0"）才当作已装引擎，否则照样下引擎包；主控（`hub.py` `recipe_engine_app`）和首次运行（`Installer.swift` `Manifests.engineInstalled`）同一规则。

## 修后从头重走（同日 14:35–）

16 GB 测试机清干净（停掉我们的进程、删 `~/Applications` 里三个 .app、环境目录、`ai.luoke.core` 偏好、`~/.luoke`、下载的 DMG），用修好的构建（交接 + 启动器目标 + 引擎版本规则，DMG 重新公证）再走一遍：Safari 下载（这次不再问是否允许下载）→ 打开 DMG → Gatekeeper（12）→ 安装器自拷贝、起新进程、取走预选 → 预检（13）→ 选应用 → 下载计划 → 下载：记忆、邮件助手 8 秒内装好，**邮件助手装完立刻自动打开**（14，Safari 开到 0.12.3 开始页，模型还在下）→ 模型下载 → 冒烟 → 登录项 → 完成。全程没有再出问题；结果见下表末尾。

主控在跑时再打开另一个安装器（加装路径，用签名未公证的修复版经 `ssh open` 测）：安装器把预选交给在跑的主控后退出，主控 3 秒内弹"安装应用"，输入法已勾选、邮件助手标"已装"（15）。这轮输入法的 zip 哈希不对（测试环境：装的主控内嵌的是旧清单，服务器上的 zip 后来重打过；发布一旦发出不会变），但暴露了完成页在失败时仍写"应用装好了，已经启动"和输入法指引（16）——已改：检查页给"重试"（只重排没成的项），完成页照实说没装上什么，输入法指引只在真装上时出现。

修好重试后再测加装（主控换成修复版，先把测试 HTTP 服务停掉再打开"安装输入法.app"）：下载失败（Connection refused）后检查页显示红字"有没下下来的：luoke-ime。网络好了可以重试；也可以先继续，以后再装"和"重试"按钮（17）；服务起来后点重试，输入法 26 秒内下载安装到 `~/Library/Input Methods/LuokeIME.app`，完成页"应用装好了，已经启动。luoke-ime：完成"加输入法指引（18）。没有在 16 GB 测试机上添加输入源（输入法自带引擎是 minos 27，luoke-ime 修好后要再测）。

## 引擎包的 macOS 要求

用 `tools/minos_check.py` 查 oMLX.app 0.7.0（即引擎包的来源）：Info.plist 写 LSMinimumSystemVersion 15.0，但包里 mlx 的 `core.cpython-311-darwin.so`、`libmlx.dylib`、`libjaccl.dylib` 是 minos **26.2**，`custom_kernels/` 下 10 个程序和库是 26.0——引擎实际要求 macOS 26.2。发布脚本的 engine 阶段现在把这个算出来记进清单项 `engine.min_macos`，首次运行低于它就在预检页拦住；官网素材的"系统要求"要改成 macOS 26.2（待定）。

## 测试后 16 GB 测试机的状态

主控、邮件助手、记忆、输入法都装在 16 GB 测试机上（`~/Applications`、`~/Library/Input Methods`），环境目录 `~/Library/Application Support/luoke-core` 里有 9B 和 0.8B，登录项已注册，`~/.luoke/mem.db` 是测试时建的。留着给 luoke-ime 修好后再测；测试用的 HTTP 服务已关（要再测时在开发机 上 `cd build.noindex/serve && python3 -m http.server 8765 --bind <开发机>`）。

## 第三轮（2026-10-16 上午）：beta0 真实下载 → 首次运行 → 输入源 → 卸载

beta0 从 GitHub Releases + ModelScope 真下（9B 5.6 GB 用了 72 分钟，1–1.5 MB/s）。开屏幕共享，经 computer-use 点。

| 步骤 | 结果 |
|---|---|
| 下载、安装三个配件和两个模型 | 通过（哈希核对后安装） |
| 冒烟检查（beta0 构建） | **失败**：`RuntimeError: timeout`。原因：下载 72 分钟后底座的闲置时钟（从引擎启动起算）早已过期，冒烟检查刚把 9B 加载好，`_idle_unload` 就把它卸了，请求挂到 300 s。修：检查期间 `smoke_running` 挡住闲置卸载、时钟从加载时重起；加 `POST /smoke` 和首次运行页的「重新检查」。换上修过的构建（test-uninstall = beta0 配方 + 当天代码，签名未公证，rsync 换装）后重跑：**通过**，load 4.8 s、prefill 58.5 tok/s、decode 11.3 tok/s（M1 16 GB） |
| 首次运行（已装好的状态下再走一遍） | 通过：预检 → "都已经在了" → 同意 → 本机检查 → 好了，登录项已注册。发现：旧的 FAILED 记录还挂在 `/status` 里时本机检查页已给「继续」，一次点击就跳到了完成页；已改成检查开始时把记录标成 running（界面显示"正在检查"），未重测 |
| 添加输入源 | 系统设置 → 键盘 → 文字输入 → 输入源 → 编辑… → + → Chinese, Simplified → 「Luoke 螺壳」→ Add。`AppleEnabledThirdPartyInputSources` 出现两条（.Hans 输入模式 + 输入法本身）。期间 System Settings 掉了一个 `ExcUserFault_KeyboardSettings` 诊断（已告知 luoke-ime） |
| 打字 | TextEdit 里切到「螺」，nihao / women 候选正常、上屏"你好我们"；输入法起了 quanpin、rime_helper 和跑在主控 Python 上的 personal.py；主控 3.3 s 起打分进程，16 GB 档底座让路（日志同一秒记了 3 条 "base unloaded: user typing"，待看） |
| 卸载 邮件助手（勾删数据） | 通过：删 .app 和 `~/.luoke-mail`（3150 条），`~/.luoke` 没动；记忆里它 0 条记录所以没显示"忘掉"。**发现**：邮件助手的 python3（启动器 exec 出来的）按 bundle id 结束不到，.app 删了它还活着；修：主控侧按 .app 路径前缀结束残留进程（SIGTERM 5 s 后 SIGKILL） |
| 卸载 输入法（勾删数据） | macOS 26 上 `TISDisableInputSource` 只切走了选中、`AppleEnabledThirdPartyInputSources` 里两条不动 → 主控按设计提示去系统设置移除。**发现**：用户移除后本进程 TIS 的 IsEnabled 仍报 true（缓存过期），卡死在提示上；修：26 起直接读 `com.apple.inputsources/AppleEnabledThirdPartyInputSources`。换构建后重跑：通过——进程结束，删 `LuokeIME.app` 和 `~/.luoke/ime`（192 条），`~/.luoke` 只剩 mem.db 和 mem.db-learn.lock |
| 卸载全部 | 通过：删螺壳记忆.app → 停主控 → 删环境目录（6 GB）→ 注销登录项 → 清 `ai.luoke.core` 设置 → 主控.app 进废纸篓 → 14 s 后自己退出。快照比对：除 luoke 路径（6171 条）外没少任何文件；`~/.luoke/mem.db` 保留 |
| 卸载窗口 | **发现**：菜单关掉后系统把前台还给原来的应用，窗口开在后面看不见（`NSApp.activate(ignoringOtherApps:)` 在 14+ 上不顶用）；修：`orderFrontRegardless` + `NSRunningApplication.current.activate` 两次。换构建后窗口自己到前面 |

屏幕共享的经验补充：画面更新比点击慢一两秒，点完立刻截图会看到旧画面（之前以为"点击丢了"多半是这个）；菜单栏菜单里的项目要用鼠标点，回车不触发；Mission Control 用工具栏按钮能开，但在里面点窗口不灵。
