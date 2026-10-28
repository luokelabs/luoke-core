# vendor/embedding_gemma2

EmbeddingGemma 2 的模型代码（只有文字这条路要的三个文件），取自 [mlx-vlm](https://github.com/Blaizzy/mlx-vlm) commit
`fec3f50379bb2cb0760ef3c8a68ebce9585e4d97`「Add EmbeddingGemma 2 multimodal embedding support (#2446)」的
`mlx_vlm/models/embedding_gemma2/`：`config.py`、`language.py`、`embedding_gemma2.py` 除文件头外逐字不改；`__init__.py`
是我们的精简版（不导入多模态处理器）。许可 MIT，见同目录 `LICENSE`（Copyright © 2025 Prince Canuma）。

为什么在这里（johnz 2026-10-16 选方案 (b)，顶层 ADR 0018）：主控的专用模型跑在引擎包（oMLX 0.7.0）自带的 Python 里，
它的 mlx-vlm 是 0.7.1，没有 `embedding_gemma2`；ADR 0001 不改引擎包。`worker/embed_worker.py` 只在引擎包的 mlx-vlm
找不到 `mlx_vlm.models.embedding_gemma2` 时，把这个目录注册成这个包名（相对导入 `..gemma4`、`..pooling`、`..base` 落到
引擎包自己的 mlx-vlm）。引擎包换成带 mlx-vlm ≥ fec3f50 的版本后自动用它自己的，这个目录就可以删。

只够文字。图片、声音、视频还要 fec3f50 的 `gemma4/processing_gemma4.py`、`gemma4/audio.py`、处理器代码和 OpenCV，
见 luoke-core 会话的评估（协调会话的共享笔记 `embed-引擎缺口-方案b.md`）。
