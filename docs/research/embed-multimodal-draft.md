# `/v1/embed` 多模态字段：luoke-core 和 digup 商定的草案（2026-10-16）

> **草案，没实现，不是对外约定。** 10-16 定的范围是全多模态（顶层 ADR 0018），交付日期未定；要等主控换上带 mlx-vlm ≥ 0.7.8 的引擎包（oMLX 0.7.1，见 V1 §10b），实现、真模型核对（digup 参考向量集里的截图、照片、视频帧、声音 4 条余弦 ≥ 0.999）通过以后，才写进 V1 §2。只加不改：`texts` 照旧可用。
> 两边会话直接商定（顶层 CLAUDE.md「两个项目之间的事，由这两个项目的会话商定」），digup 的提议见它的注册页「对主控的需求」第 6 条。

## 请求

和 `texts` 二选一：

```json
{"inputs": [
  {"text": "task: search result | query: …"},
  {"image": "<base64 的 PNG 或 JPEG>", "budget": 280},
  {"audio": "<base64 的 WAV：16 kHz、单声道、16 位 PCM，≤ 30 s>"}
]}
```

- **只有图片和声音**两种媒体。视频由 digup 自己每 3 秒抽关键帧、去掉相近的，当图片按预算 70 送；扫描的 PDF 页渲染成图片按 280 送。主控不做视频解码，**不需要 OpenCV**（已核：mlx-vlm 只在视频解码函数里才导入 cv2）。
- **带字节，不带路径**：主控不读用户的文件。截图用 PNG（免得字糊），照片和视频帧用 JPEG。HEIC 之类由 digup 先转好（引擎包里的 PIL 不认）。
- `budget`：图片的 token 预算，取 mlx-vlm 支持的 70、140、280、560、1120 之一（digup 现在用 70 视频帧、140 照片、280 截图和扫描页）；本档允许哪些放任务参数 `image_budgets`。
- 图片送 digup 自己抽好的缩略图（照片和视频帧 ≤ 1024 px 的 JPEG，截图 ≤ 1536 px 的 PNG，PDF 扫描页 1400 px 的 PNG，约 100–300 KB），**不预缩到目标尺寸**；主控用模型自己的处理器按 `budget` 缩一次。见下面「图片尺寸」。
- 声音：16 kHz 单声道 16 位 PCM 的 WAV，每段 ≤ 任务参数 `max_audio_secs`（先定 30 s，和参考向量 `speech` 的做法一样；模型上下文装得下约 327 s，以后要放宽就改参数）。由 digup 按 30 s 一段、每 25 s 起一段切好，去掉静音。

## 答复

`{"vectors": [...], "errors": [...], "dims", "model", "vector_space", "ms"}`：

- `vectors` 按 `inputs` 的顺序；**逐条出错，不整批失败**：坏掉的那条 `vectors[i]` 是 `null`，`errors[i]` 是原因，其余照算（`errors` 和 `inputs` 对齐，没出错的是 `null`）。原因只写类别、不带内容（主控日志只记元数据），取这几个码之一：`image_unreadable`（图片解不开或处理器不收）、`audio_unreadable`（声音解不开）、`audio_too_long`（超过 `max_audio_secs`）、`embed_failed`（其余）。
- 请求本身不合法（不是列表、条数或字节超限、`budget` 不在允许里、`texts` 和 `inputs` 同时给）照旧整个答 `invalid_request` / `input_too_long`。
- `vector_space` 同一个：图片、声音、文字的向量在同一个空间里，能互相比。

## 主控收不收图片、声音：看任务参数（2026-10-16 和 digup 商定）

配件从 `GET /tasks` 里嵌入任务的 `params` 判断，不另加字段：
- 有 `image_batch` 且 > 0：这一档收图片。**主控有 `image_batch` 时一定同时给 `image_budgets`**（允许的预算列表）；配件那边缺了它就当五档都行，只作兜底。
- 有 `audio_batch` 且 > 0：这一档收声音。
- 两个都没有：只收文字。只收文字的档不带这两个参数。

任务参数随配方和引擎包同一班发布，所以不会出现"参数说收图片、引擎包却算不了"。参数变了 `X-Luoke-Tasks-Version` 跟着变，配件重读。digup 看到某一种开始收了，会把以前在等的那些文件重读一遍（后台每次约 16 个，被答 `busy` 时停）。

## 大小上限（任务参数，配件从 `GET /tasks` 读）

`image_batch`（每次最多几张图，初值 8）、`audio_batch`（每次最多几段声音，初值 2）、`max_image_bytes`、`max_audio_bytes`（每条解码前的字节数）。主控读请求体的上限是 32 MB，base64 会胀到 4/3，上限按这个留余量：8 张 × 约 100 KB 的 JPEG（预缩好的照片约 0.65 MP）远在上限以内。

## 图片尺寸：预缩好的图会不会被再缩一次（2026-10-16 核过，**会**）

digup 想先把图缩到预算对应的目标尺寸再送（算法和 Gemma 4 图片处理器的 `get_aspect_ratio_preserving_size` 一样），省带宽、让处理器的缩放变成空操作。核了 mlx-vlm 0.7.8 的 `embedding_gemma2/image_processing_embedding_gemma2.py`：处理器确实在"目标尺寸等于当前尺寸"时跳过缩放；但它是**拿收到的图重新算目标尺寸**，而这个函数**不是不动点**——预缩好的图面积略小于预算，算出来常常在一边再**放大一格（48 像素）**。按这个模型的处理器参数（patch 16、pooling 3），对 2 万个随机尺寸实测：五档预算下都约 28% 的图会被再放大一次，例如 1920×1080 按 140 预缩成 720×384，处理器会再放到 768×384。

所以商定（digup 核对后的结论，同日）：**digup 不预缩**，送自己抽好的缩略图（见上面「请求」），由主控的处理器按 `budget` 缩一次。这和参考做法一致：digup 的 llama.cpp 那条路拿同一张缩略图算一次目标尺寸、缩一次（它的 ImageSizing 照 transformers 的 Gemma 4 图片处理器写，缩放和 Pillow 逐位一致）；它的 mlx worker 也是把同一张缩略图的路径交给处理器、缩一次；参考向量 `photo-140`、`screen-280`、`frame-70` 就是这样从 `Reference/photo.jpg`、`screen.png` 做出来的。所以**不加 `sized`**（曾提议过：带它就跳过处理器的缩放）。如果不带它，三条图片参考向量也核不过 0.999，再看差在哪一步。

## 还要做的（主控这边，等换引擎包）

**2026-10-19 已写好代码、还没打开**（10-19 说要换 oMLX 0.7.1；用哪个版本、什么时候换还在问）：主控按任务参数校验 `inputs`（`_check_embed_inputs`），任务参数有 `image_batch` / `audio_batch` 时嵌入进程带 `--media` 起（整个模型 + 处理器），工作进程的 `embed_inputs` 照下面的做法算、逐条出错（`worker/mediaops.py` 是分批的纯逻辑）；图片走 data URL、声音用 `mlx_audio` 的 read 从内存读，都不落盘。现在的配方里没有这两个参数，所以 beta1 里不打开；在 0.7.0 的引擎包上硬开，嵌入进程会在 ready 行里说"要 mlx-vlm ≥ 0.7.8"、不起来。只在假工作进程上测过，真模型和 4 条媒体参考向量要等换上 0.7.1 的引擎包。

- 工作进程加 `image`、`audio` 两种输入（用 mlx-vlm 0.7.8 的处理器，每张图按 `budget` 缩一次），逐条出错；主控校验字段和上限；`tools/embed_parity.py` 加 4 条媒体参考向量，送法和 digup 做参考向量、它的 `HubParityTests` 一样：`screen-280` ← `Reference/screen.png` 预算 280；`photo-140` ← `Reference/photo.jpg` 预算 140；`frame-70` ← **同一张** `photo.jpg` 预算 70（不是另一张帧图）；`speech` ← `Reference/speech.wav` 整段送。
- 测试、写进 V1 §2、注册页；告诉 digup。
