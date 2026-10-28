# luoke-core 任务级请求：先例调研简报

> 调研参考，不是设计结论。2026-10-08 由 4 路调研 + 逐条核查原始出处汇总而成；被核查推翻的说法已删去或改正，标"未核实"的仍需确认。

（2026-10-08；方括号数字是脚注，对应文末的来源列表）

## 1. 一句话结论

**任务级请求有先例。最接近的是 Android ML Kit GenAI 和 Chrome built-in AI。** 在这两个平台上，应用只说要做什么任务，平台按设备档位选模型，再给每个任务叠加 LoRA、提示词和参数。底座升级带来的变化由平台自己跑每任务评测来兜住 [2][3][8][13]。

其他几类做到的程度不同：
- **Apple 和 Windows** 只做到"能力级 + 一层很薄的任务层"，评测和提示词维护都推给了应用 [39][44][52]。
- **本地模型管理器**（Ollama、LM Studio、llama.cpp、llama-swap、Foundry Local）都按模型名或别名寻址，没有一家按每任务的质量证据选模型 [59][60][63][64][68]。
- **生成类任务大多共用一个底座**，而翻译、嵌入、OCR、语音这类任务，平台通常各配一个专用模型 [16][28][56]。

所以你的判断是对的：做成任务级，core 就要自己承担只有平台方才做的几件事——任务契约、质量证据、驻留调度和版本门控。

## 2. 先例一览表

| 平台 | 应用声明的是什么 | 平台怎么选模型 | 多个任务怎么共享内存 | 版本变化怎么保护调用方 | 质量证据从哪来 |
|---|---|---|---|---|---|
| **Android ML Kit GenAI + AICore** | 任务：Summarization、Proofreading 等，带 inputType、outputType、语言等任务参数。另有 Prompt API，可选 STABLE/PREVIEW 和 FULL/FAST [4][5] | AICore 负责分发；不同设备上的 Nano 版本可能不同 [1][7] | 一个 Nano 底座，加每个 API 一个小 LoRA，加每个 API 调好的 prompt、temperature、topK [2] | 平台用 adapter 加评测，让每个 API 不管跑在哪个 Nano 版本上都达标；`getBaseModelName()` 暴露底座版本；生产环境建议用 STABLE [1][3][5] | 任务 API 由 Google 评：按语言建评测集，LLM 评审、统计指标、人工三路打分；摘要从 77.2 提到 92.1 [2][3]。Prompt API 由应用自己评，每个 Nano 版本都要重测 [6] |
| **Chrome built-in AI** | 任务加选项：Summarizer 的 type/length/preference，Translator 的语言对；Summarizer 和 Translator 自 Chrome 138 起稳定。通用 Prompt API 在 Chrome 148 对网页开放 [15][16][17][84] | manifest 表：设备档位（gpu_high_tier / gpu_low_tier / cpu）× use case → 方案（底座或 LoRA、安全模型、配置、后处理）[8]。翻译用专用模型 [16]。Summarizer 的 speed 选项是实验特性，会路由到更小的专用模型 [9][10][86] | 底座加按需下载的 LoRA。底座和所有 LoRA 共用一个串行执行器，空闲 5 分钟后卸载 [12][13][87] | 静默热替换，JS 查不到版本；可以按 use case 做实验分流 [9][13] | 每个设备档位一个可选的 ValidationTask，是端上冒烟测试；没有公开评测 [8] |
| **Apple Foundation Models（OS 26/27）** | 模型加 session。use case 只有 `.general` 和 `.contentTagging`，另可用结构化输出和工具。OS 27 起可通过 `LanguageModel` 协议接入其他模型 [45][49] | 系统按机型和 OS 版本决定，开发者不能选 [33]。OS 27 有两档：Core（3B）和 Core Advanced（20B 稀疏模型）[34][50] | 苹果自家功能：底座加每功能 adapter，动态加载、缓存、换出 [46]。Core Advanced 权重放在 NAND，按 prompt 换专家，每个 use case 预设激活参数量 [35] | 已随 OS 推送 3 个模型版本 [38]。苹果让应用自己录输出、做对比、按版本维护 prompt [39]。第三方 adapter 在 OS 27 不再支持 [36][37] | 自家功能苹果自己评（2024 年每个 use case 750 条人工评分）[46]；第三方用 OS 27 的 Evaluations 框架自己评 [44] |
| **Windows AI APIs（Phi Silica → Aion Instruct）** | 清单里声明 `systemAIModels`，然后用 `LanguageModel` 或建在它上面的 TextSummarizer、TextRewriter；每个请求可附带 LoRA [52][55] | 由 OS 和 Windows Update 管理；Copilot+ 机型的 NPU 上预装；新模型先给谁用由 Controlled Feature Rollout 决定 [51] | 一个常驻底座加每请求 LoRA。有 `LowOnVram`、`BlockedByGameMode` 状态码，但没有文档说明 [53] | 2026 年 10 月可侧载新模型测试，11 月 Insider 可并行对比，2027 年 1 月零售版替换并删除旧模型。应用必须重训 LoRA，不兼容的 adapter 返回 `IncompatibleLowRankAdapter` [51][53] | 应用自己定评测标准 [52] |
| **Edge** | 与 Chrome 相同的 API [27] | 默认 Phi-4-mini；手动打开 flag 才换成 Aion-1.0-Instruct，低性能设备只能用 Aion [27] | 一个模型，所有网站共用 [27] | 文档只警告 "model behaviors and capabilities are subject to change" [27] | 未查到 |
| **Firefox 本地 ML** | taskName 加 modelId/revision；其余配置按 featureId 从 Remote Settings 读取 [29][30] | 功能负责人在 Remote Settings 里配置 [29] | 所有引擎共用一个推理进程，但每个功能用自己的小模型；创建引擎前先检查内存 [30] | 每个功能钉住 revision，改 revision 才触发下载 [29] | 未查到 |
| **Home Assistant AI Task** | task_name、instructions、输出结构 [58] | 只按任务类型（生成数据 / 生成图片）各存一个用户首选实体，task_name 不参与路由 [57] | 交给底层 provider | 不处理 | 没有。能力不匹配时直接报错，不回退 [57] |
| **llama-swap** | 模型名或虚拟 ID（如 `llm-code`）[59] | 管理员手写 profiles 和 selectors（warm / pin / spillover）[59] | matrix 模式：声明哪些模型可以同时驻留，并给每个模型一个驱逐代价 [59] | 无 | 无 |
| **Ollama / LM Studio / llama.cpp router** | 模型名 | 用户自己选 tag；Ollama 按显存决定默认 context 长度 [62] | 按个数设上限（Ollama 每 GPU 3 个，llama.cpp 4 个，LM Studio 的 JIT 加载只留 1 个），加空闲 TTL，再按最短 keep_alive 或 LRU 驱逐 [60][61][63][64][65] | Ollama 按 digest 或路径区分模型，更新靠用户手动 pull [61] | 无 |
| **Foundry Local** | 别名 [68] | 别名解析成按硬件排序的变体列表，用第一个 [68] | 默认在进程内运行（也有可选的本地服务）；面向单用户，没有并发排队或批处理 [67] | 应用可以钉住版本，也可以自动更新 [67] | 未查到每任务分数 |
| **Azure model router / OpenRouter Auto（云端）** | 路由器名，加模式或候选子集 [75][77] | Azure 按 prompt 预测质量，Balanced 模式在最优的 1–2% 区间内选最便宜的；OpenRouter 把请求分成约 30 种任务类型，按过去 7 天的花费占比排序 [75][77] | — | Azure 区分冻结版本和原地更新版本；新模型要显式加入子集 [75] | Azure 是内部数据；OpenRouter 的排序只是人气信号 [77] |
| **HF transformers `pipeline(task)`** | 任务字符串 [72] | 库源码里有一张每任务默认表（repo 加 commit）[72] | 进程内运行 | 默认表随库版本变化：text-generation 的默认模型在 v5.3.0 从 gpt2 换成了 SmolLM3-3B [73] | 无 |

## 3. 任务级对主控提出的要求

**① 任务契约：除了输出文本，其他全部写死**
- 先例怎么做：
  - Writing Assistance 规范明确不保证输出质量 [19]。它保证的是下载状态、错误类型、配额测量和选项枚举 [18]。
  - Summarizer 的选项带可测的输出上限，比如 headline 只能是 12、17 或 22 个词 [15]。
  - Chrome 源码里还留着一条 TODO："model configs should express the subset of supported options" [9]。
  - Mozilla 演示过 Summarizer 能被注入、被当成通用模型用 [32]（这一点我没有独立核验）。
- 对 luoke：像 `mail.triage@1` 这样的任务，应当带上输入/输出 schema、执行类型（生成 / 打分 / 嵌入）、QoS 类、隐私类和评测集 ID。（这是设计建议。）

**② 共享底座 + 每任务增量**
- 先例怎么做：
  - ML Kit、Chrome、Apple 自家功能、Windows 都是"一个底座 + 每任务 LoRA / prompt / 参数" [2][13][46][52]。
  - LiteRT-LM 的 Engine 单例持有底座，每个 Session 可以带自己的 LoRA；切换 Session 时保存和恢复 KV cache 与 LoRA [79]。
  - ML Kit 是先评测，再决定某个任务要不要训 adapter [3]。
- 代价：
  - 底座一换，所有 LoRA 都得重训，Chrome、Apple、Windows 都是如此 [13][36][51]。
  - Chrome 的底座和所有 LoRA 共用一个串行执行器，任务之间只能排队 [12]。
  - llama.cpp 不会把 LoRA 配置不同的请求合成一批 [64]。
  - Ollama 只要 adapter 或运行参数不同就整模型重载 [61]。有用户报告每次请求都重载，单次最长 60 秒 [66]。
  - 对比之下，LoRAX 支持不同 adapter 混合批处理 [81]。

**③ 驻留与内存预算**
- 先例怎么做：
  - llama-swap 的 matrix 声明哪些模型可以同时驻留，加上每个模型的驱逐代价，尽量保留代价高的 [59]。
  - Chrome 目前每个 use case 只有一个方案；源码注释说以后可以扩展成多个方案，用来应对内存压力 [8]。
  - Apple Core Advanced 给每个 use case 预设激活参数量，听写用的是 1B 激活 [35]。这是"任务 → 资源预算"最接近的先例。
  - 本地管理器只按个数上限、TTL 和 LRU 管理 [60][63][65]。

**④ 硬件变体选择**
- 先例怎么做：
  - Chrome 分三档 [8]，Apple 按机型 [34]，Foundry Local 用别名列表里的第一个 [68]。
  - Canonical 按硬件匹配的具体程度打分 [69]。
  - NIM 用一个感知内存的选择器，把每个配置标成 Compatible、Low memory 或 Incompatible [70]。
- 同一任务在不同档位上行为可能不同：
  - Apple 员工说 Core 和 Core Advanced 的 "personalities" 差别很大，建议按变体分别维护 prompt [34]。
  - Windows 则称 NPU 和 GPU 用同一份权重、质量相同，只是 GPU 上没有投机解码 [51]。
- 对 luoke：质量证据要按（任务 × 模型 digest × 量化 × 硬件档）来存。

**⑤ 交互优先级与准入**
- 先例怎么做：
  - Chrome 分前台和后台两级，由页面是否可见推出；后台请求只在没有前台请求时才跑 [9][11]。但正在跑的任务不会被抢占 [12]。
  - Android 只允许最前台的应用做推理，并设有每应用配额（超出返回 BUSY）和长时电量配额 [1]。
  - Apple OS 27 在请求过密时返回 `rateLimited` [40]；后台使用神经引擎需要专门的 entitlement [41]。
  - llama-swap 只能按模型调整排队顺序，超过并发上限直接返回 429 [59]。
  - Ollama 的请求没有优先级字段 [61]。vLLM 有 priority 调度策略 [80]。

**⑥ 几十毫秒级的 log-prob 打分（IME 场景）**
- 先例怎么做：
  - 三大 OS 都没有在共享模型上开放 log-prob。Apple 的 `GenerationOptions` 里没有这一项 [42]。
  - Chrome 内部有 `Score()`，但不对网页开放；网页要 logprobs 的需求从 2024 年开着至今 [11][20]。
  - Apple 建议提前 1–2 秒 prewarm [43]，可见冷启动是秒级的。
  - llama.cpp server 的文档只给"生成出来的 token"的概率，没有对一段已知文本打分的接口 [64]（这一点是本次核查的）。
  - Apple 键盘每次按键用一个专用的 transformer 做预测 [85]（二手来源）。
- 平台给出的路子：同一套 API 背后挂一个应用自带、在应用进程内运行的模型 [48]。

**⑦ 可用性状态、下载与同意**
- 状态：按"任务 + 选项"报告四态：unavailable / downloadable / downloading / available [4][14]。
- 不可用的原因：
  - Apple 有 deviceNotEligible、appleIntelligenceNotEnabled、modelNotReady [38]。
  - Windows 有 7 种，包括 DisabledByUser、CapabilityMissing、NotCompatibleWithSystemHardware、OSUpdateNeeded [54]。
- 下载和同意：
  - Windows 建议下载前先弹同意框 [51]。
  - Chrome 下载需要用户手势；空闲磁盘低于 10GB 时会删掉模型 [14]。
  - Firefox 每个功能有 Available / Enabled / Blocked 三种状态 [31]。

**⑧ 版本固定与回归评测**
- 钉版本：
  - Foundry Local 可以钉住或自动更新 [67]；Firefox 每个功能钉住 revision [29]。
  - Azure 部署有三种升级策略，新默认版本至少提前两周通知（仅限 Standard 部署）[76]；Azure router 区分冻结版本和原地更新版本 [75]。
- 试用通道：ML Kit 分 STABLE 和 PREVIEW [5]；Windows 给了一个 Insider 并行对比窗口 [51]。
- 回报实际用的是哪个模型：`getBaseModelName()` [1]、Apple 的 `variant` [50]。
- 评测门槛：
  - ML Kit 每换一个版本就重评 [3][6]。
  - Apple 让应用自己录下旧模型的输出再做对比，因为旧模型只在 beta 期间可用 [39]。
- 证据格式：HF 的 `.eval_results` 记录任务 ID、分值、数据集 revision、日期和来源，但没有模型 revision、量化和硬件字段 [74]。

**⑨ 隐私边界**
- 先例怎么做：
  - AICore 不直接联网，每个请求相互隔离，不留输入输出记录 [7]。
  - Writing Assistance 规范禁止用用户输入去训练或微调模型 [18]。
  - 浏览器和 OS 的模式都默认平台是可信的、能看到文本；WebKit 还指出，网站自己仍然可以把 prompt 外泄 [25]。
  - 本地守护进程常常默认不鉴权：Docker Model Runner 文档明说其 API 不鉴权 [71]；llama-swap 不设 apiKeys 时全部放行 [59]。

**⑩ 回退**
- 好的做法：
  - ML Kit：预览配置不可用时，回退到 STABLE/FULL [5]。
  - Azure router 内置故障转移，候选子集就是允许回退到的范围 [75]。
  - Windows 建议在 GPU 设备上设置超时和回退 [51]。
- 反例：Chrome 的 speed 选项不兼容时直接报不可用 [9]；Home Assistant 能力不匹配时直接报错 [57]。

## 4. 对 luoke-core 最有借鉴价值的模式与反面教训

**值得借鉴的模式**
1. **Chrome 的"任务 × 设备档位 → 方案"表** [8][9]。
   - luoke 的方案可以是：（引擎，模型 digest，量化，prompt/参数包，可选 adapter，后处理）。
   - 再补上 Chrome 只在注释里提过、没有实现的部分：每个任务有多个候选，按当前驻留集合和内存压力挑选 [8]。
2. **ML Kit 的"评测决定要不要增量，底座一换就重评"** [3][6]。
   - 每个 accessory 交出自己的数据集、指标和阈值，模型或引擎有变化时，hub 在本机重新评测。（设计建议）
3. **共驻集合 + 驱逐代价 + 前台/后台两级**（llama-swap matrix [59] 加 Chrome 的优先级 [11]）。
   - 再补上本地管理器都没有的两样：按请求区分的 QoS 类，以及批处理任务给交互任务让路。
4. **钉版本 + 预览通道 + 并行对比窗口 + 每次调用回报实际用的模型** [1][5][51][67]。
   - 规范隐藏模型版本是出于防指纹的考虑 [18]。本地 hub 没有这个约束，可以把版本直接告诉 app（推断）。
5. **控制面和数据面分开**（设计推断，没找到恰好这样组合的现成系统）。
   - hub 负责选型、下载、评测和展示。
   - 有隐私要求或毫秒级延迟要求的任务，在 app 进程内执行。先例：Apple 允许 app 自带 Core AI 模型、走同一套 session API [48]；Foundry Local 默认在进程内运行 [67]。

**反面教训**
1. **每任务 LoRA 和底座版本绑死，最后成了"重训跑步机"。**
   - Apple 的第三方 adapter 每个只兼容一个系统模型版本，训练工具包止于 26.0.0，OS 27 直接不再支持 [36][37]。
   - Windows 换成 Aion 时，要求所有 LoRA 重训 [51]。
2. **静默换模型、隐藏版本。**
   - Chrome 静默热替换模型，JS 查不到版本 [13]。
   - HF 的默认模型在 v5.3.0 被换掉，运行时只有一条日志警告 [72][73]。
   - OpenRouter preset 发起的 API 请求总是用最新版本 [78]。
   - Mozilla 认为开发者照样会针对 Gemini 去调，prompt 会固化在单个模型的怪癖上 [23][24]。
   - TAG 也追问过：模型在两次页面加载之间更新了，页面怎么得到一致的结果 [26]。
3. **做成任务级，平台就要为结果负责，而且必须让用户知情。**
   - Apple 因 BBC 投诉，暂停了新闻类通知摘要 [47]。
   - Chrome 自己承认 Writer API 有质量问题、采用率低 [21]。
   - Chrome 静默下载 4GB 权重受到批评 [22]。
   - WebKit 批评在没有用户同意的情况下跑推理，等于把电量和算力成本转嫁给用户 [25]。

## 5. 和 luoke-core 的不同之处

先例的共同前提是：平台自有一个尺寸固定的小模型，平台有团队训 adapter、跑评测，而且平台控制着 OS，能强制执行前台限制、配额和 entitlement [1][3][41]。luoke 面对的则是用户自己装的多种引擎、尺寸差几十倍的开放权重，而且 hub 既不拥有引擎，也不拥有 OS。

**能迁移的**
- 任务契约、四态可用性加原因、下载前同意：可以直接照搬 [14][38][51]。
- 任务 × 硬件档 → 方案的表，以及 ValidationTask 式的端上冒烟测试 [8]：可以用，但方案里要多出"引擎"这一维。
- 钉版本加回归评测：可以用，而且比浏览器更容易做，因为没有防指纹的约束 [18]。
- 在 hub 入口做准入和返回显式拒绝码：可以做。但 hub 管不到引擎内部：Ollama 的请求没有优先级字段 [61]，llama-swap 只能按模型调整排队顺序 [59]。

**不能直接迁移的**
- **"一个底座 + 每任务 LoRA"。** 用户的模型五花八门，LoRA 要针对每个底座、每种量化分别重训，连 Apple 都放弃了第三方 adapter [37]。更现实的是 prompt/参数包。DSPy 的经验是换了 LM 就要重新编译 prompt [82]，所以 prompt 包要按（任务，模型）生成并重新评测。
- **平台自产质量证据。** luoke 没有 Google 那样的评测流水线，证据只能由 accessory 提供评测集，在用户机器上跑。MTEB 显示没有哪个嵌入方法在所有任务上都最好 [83]，这支持"按任务选"，但本地跑评测的时间和电量成本需要实测。
- **平台的强制力。** luoke 不能禁止 app 后台运行，也不能发 entitlement。accessory 甚至可以绕过 hub 直连引擎，因为本地守护进程常常默认不鉴权 [59][71]。
- **"平台可信、能看文本"的信任模型** [25]。对"永不把文本发给 hub"的 app，只能靠控制面和数据面分离来解决（推断）。
- **常驻底座的假设。** 平台模型足够小，可以一直常驻。在统一内存上，luoke 的驻留集合可能只容得下 1 个大模型加几个小模型，所以任务表必须能降级到小模型，或者排队等待（推断）。

## 6. 仍不确定、需要实测或继续查的点

1. Apple 和 Android 怎样在多个应用之间仲裁同一个模型（排队、驱逐、空闲卸载）：没有公开资料，未核实。
2. Windows `LowOnVram` 和 `BlockedByGameMode` 的触发条件：没有文档 [53]。
3. 本地能不能做到几十毫秒的候选打分：llama.cpp server 的文档没有"给已知文本打分"的接口 [64]。MLX、llama.cpp 库内直接调用、以及 hub 的 IPC 往返延迟，都需要实测。
4. 目标 Mac 上冷加载、换模型和 KV cache 的真实代价：Apple 建议提前 1–2 秒 prewarm [43]，有 Ollama 用户报告单次重载长达 60 秒 [66]。需要在 luoke 的目标机器上实测。
5. 量化等级和引擎不同，对任务质量的影响有多大：没找到公开数据；HF 的证据格式也没有这些字段 [74]。
6. 在用户本机跑每任务评测，最少需要多少样本、要多久、耗多少电：需要实测。
7. 引擎里正在跑的生成，能不能从外部取消或让路（Ollama、LM Studio、llama.cpp 各自的行为），以及 vLLM 的 priority 策略会不会抢占正在运行的请求：均未核实。
8. OS 27 的 `LanguageModel` 协议能不能让 luoke 作为第三方后端，供 accessory 用 Apple 的 API 来调用：文档说可以接入其他模型 [45]，但能否跨进程未核实。
9. 几个次要问题：Chrome Writer/Rewriter 是否已正式弃用（只在 polyfill README 里看到，未核实）；Edge 的 Aion 是否已开源（未核实）；Firefox 141 耗电投诉的细节（二手来源，未核实）。

---
**来源**
[1] https://developers.google.com/ml-kit/genai
[2] https://android-developers.googleblog.com/2025/05/on-device-gen-ai-apis-ml-kit-gemini-nano.html
[3] https://android-developers.googleblog.com/2025/08/the-latest-gemini-nano-with-on-device-ml-kit-genai-apis.html
[4] https://developers.google.com/ml-kit/genai/summarization/android
[5] https://developers.google.com/ml-kit/genai/prompt/android/select-model
[6] https://developers.google.com/ml-kit/genai/prompt/android/evaluate-prompt
[7] https://developer.android.com/ai/gemini-nano
[8] https://chromium.googlesource.com/chromium/src/+/main/components/optimization_guide/proto/manifest.proto
[9] https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/ai/ai_manager.cc
[10] https://chromium.googlesource.com/chromium/src/+/main/third_party/blink/public/mojom/ai/ai_common.mojom
[11] https://chromium.googlesource.com/chromium/src/+/main/services/on_device_model/public/mojom/on_device_model.mojom
[12] https://chromium.googlesource.com/chromium/src/+/main/services/on_device_model/on_device_model_mojom_impl.cc
[13] https://developer.chrome.com/docs/ai/understand-built-in-model-management
[14] https://developer.chrome.com/docs/ai/get-started
[15] https://developer.chrome.com/docs/ai/summarizer-api
[16] https://developer.chrome.com/docs/ai/translator-api
[17] https://developer.chrome.com/docs/ai/built-in-apis
[18] https://webmachinelearning.github.io/writing-assistance-apis/ （Draft CG Report；见 #privacy-model-version、#privacy-user-input）
[19] https://github.com/webmachinelearning/writing-assistance-apis/blob/main/README.md
[20] https://github.com/webmachinelearning/prompt-api/issues/20
[21] https://groups.google.com/a/chromium.org/g/blink-dev/c/HCSzUSI8kI8/m/A3I4o5YlBAAJ
[22] https://engadget.com/2166113/chrome-downloads-a-4gb-ai-file-without-user-consent-researcher-alleges/
[23] https://github.com/mozilla/standards-positions/issues/1067#issuecomment-2920301847
[24] https://github.com/mozilla/standards-positions/issues/1213#issuecomment-4347988313
[25] https://github.com/WebKit/standards-positions/issues/495#issuecomment-4356846488
[26] https://github.com/w3ctag/design-reviews/issues/1093#issuecomment-3515070512
[27] https://learn.microsoft.com/en-us/microsoft-edge/web-platform/prompt-api
[28] https://blogs.windows.com/msedgedev/2026/06/02/expanding-on-device-ai-in-microsoft-edge-new-models-and-apis-for-the-web/
[29] https://firefox-source-docs.mozilla.org/toolkit/components/ml/models.html
[30] https://firefox-source-docs.mozilla.org/toolkit/components/ml/api.html
[31] https://blog.mozilla.org/en/firefox/how-to-use-ai-controls/
[32] https://github.com/mozilla/standards-positions/issues/1067
[33] https://developer.apple.com/forums/thread/832555
[34] https://developer.apple.com/forums/thread/843063
[35] https://machinelearning.apple.com/research/introducing-third-generation-of-apple-foundation-models
[36] https://developer.apple.com/apple-intelligence/foundation-models-adapter/
[37] https://developer.apple.com/forums/thread/831314
[38] https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel
[39] https://developer.apple.com/documentation/foundationmodels/updating-prompts-for-new-model-versions
[40] https://developer.apple.com/documentation/foundationmodels/languagemodelerror/ratelimited(_:)
[41] https://developer.apple.com/documentation/ios-ipados-release-notes/ios-ipados-27-release-notes
[42] https://developer.apple.com/documentation/foundationmodels/generationoptions
[43] https://developer.apple.com/documentation/foundationmodels/optimizing-key-value-caching-in-language-model-sessions
[44] https://developer.apple.com/documentation/evaluations
[45] https://developer.apple.com/documentation/foundationmodels/languagemodel
[46] https://machinelearning.apple.com/research/introducing-apple-foundation-models
[47] https://www.nbcnewyork.com/news/business/money-report/apple-disables-ai-notifications-for-news-in-its-beta-iphone-software/6110884/
[48] https://developer.apple.com/documentation/foundationmodels/running-a-core-ai-model-in-a-foundation-models-session
[49] https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel/usecase
[50] https://developer.apple.com/documentation/foundationmodels/systemlanguagemodel/variant-swift.struct
[51] https://learn.microsoft.com/en-us/windows/ai/apis/phi-silica
[52] https://learn.microsoft.com/en-us/windows/ai/apis/phi-silica-lora
[53] https://learn.microsoft.com/en-us/windows/windows-app-sdk/api/winrt/microsoft.windows.ai.text.languagemodelresponsestatus
[54] https://learn.microsoft.com/en-us/windows/windows-app-sdk/api/winrt/microsoft.windows.ai.aifeaturereadystate
[55] https://learn.microsoft.com/en-us/windows/ai/apis/get-started
[56] https://learn.microsoft.com/en-us/windows/ai/apis/
[57] https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/ai_task/task.py
[58] https://raw.githubusercontent.com/home-assistant/home-assistant.io/current/source/_integrations/ai_task.markdown
[59] https://github.com/mostlygeek/llama-swap/blob/main/docs/config.example.yaml
[60] https://docs.ollama.com/faq
[61] https://github.com/ollama/ollama/blob/main/server/sched.go
[62] https://docs.ollama.com/context-length
[63] https://lmstudio.ai/docs/app/api/ttl-and-auto-evict
[64] https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
[65] https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-models.cpp
[66] https://github.com/anomalyco/opencode/issues/2979
[67] https://learn.microsoft.com/en-us/azure/foundry-local/what-is-foundry-local
[68] https://learn.microsoft.com/en-us/azure/foundry-local/reference/reference-cli
[69] https://github.com/canonical/inference-snaps-cli/blob/main/pkg/selector/select_stack.go
[70] https://docs.nvidia.com/nim/large-language-models/2.0.11/deployment/model-profiles-and-selection.html
[71] https://docs.docker.com/ai/model-runner/
[72] https://github.com/huggingface/transformers/blob/main/src/transformers/pipelines/__init__.py
[73] https://github.com/huggingface/transformers/pull/43325
[74] https://huggingface.co/docs/hub/eval-results
[75] https://learn.microsoft.com/en-us/azure/ai-foundry/openai/concepts/model-router
[76] https://learn.microsoft.com/en-us/azure/foundry-classic/foundry-models/concepts/model-versions
[77] https://openrouter.ai/docs/guides/routing/routers/auto-router
[78] https://openrouter.ai/docs/features/presets
[79] https://developers.googleblog.com/en/on-device-genai-in-chrome-chromebook-plus-and-pixel-watch-with-litert-lm/
[80] https://docs.vllm.ai/en/latest/configuration/engine_args.html
[81] https://github.com/predibase/lorax
[82] https://sky.cs.berkeley.edu/project/dspy/
[83] https://arxiv.org/abs/2210.07316
[84] https://developer.chrome.com/blog/new-in-chrome-148
[85] https://9to5mac.com/2023/06/05/ios-17-iphone-autocorrect/
[86] https://chromium.googlesource.com/chromium/src/+/main/third_party/blink/renderer/platform/runtime_enabled_features.json5
[87] https://chromium.googlesource.com/chromium/src/+/main/services/on_device_model/on_device_model_mojom_impl.h