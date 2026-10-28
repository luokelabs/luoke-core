# Text-only package init for the vendored mlx-vlm embedding_gemma2 (commit fec3f50, MIT, see LICENSE).
# Upstream's __init__ also imports EmbeddingGemma2Processor (images, video, audio); that needs
# gemma4.processing_gemma4.get_aspect_ratio_preserving_size, which mlx-vlm 0.7.1 lacks, and text doesn't use it.
from .config import AudioConfig, ModelConfig, TextConfig, VisionConfig
from .embedding_gemma2 import Model

__all__ = ["Model", "ModelConfig", "TextConfig", "VisionConfig", "AudioConfig"]
