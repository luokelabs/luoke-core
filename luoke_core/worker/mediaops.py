"""嵌入工作进程处理多模态 inputs 的纯逻辑（不依赖 MLX、numpy，便于测试）：认条目种类、按种类和预算分批、图片的 data URL。
多模态字段的约定见 docs/research/embed-multimodal-draft.md（和 digup 商定的草案，还不是契约）。"""
from __future__ import annotations

from typing import List, Optional, Tuple

KINDS = ("text", "image", "audio")

# 逐条出错时 errors[i] 里的原因：只写类别，不带内容（主控日志、答复里都不能有用户的东西）
IMAGE_UNREADABLE = "image_unreadable"
AUDIO_UNREADABLE = "audio_unreadable"
AUDIO_TOO_LONG = "audio_too_long"
EMBED_FAILED = "embed_failed"


def kind_of(item) -> Optional[str]:
    """条目的种类：text / image / audio 恰好一个；不是 dict 或种类不唯一返回 None。"""
    if not isinstance(item, dict):
        return None
    keys = [k for k in KINDS if k in item]
    return keys[0] if len(keys) == 1 else None


def plan_batches(inputs: list, text_batch: int, image_batch: int, audio_batch: int) -> List[Tuple[str, Optional[int], List[int]]]:
    """[(种类, 图片预算或 None, [inputs 里的下标, ...]), ...]：同种类放一批（图片再按预算分，同一批只能一个预算），
    每批不超过各自的批大小。顺序：文字、图片（预算从小到大）、声音；结果按下标回填，所以顺序不影响答复。"""
    groups: dict = {}
    for i, it in enumerate(inputs):
        k = kind_of(it)
        if k is None:
            continue
        groups.setdefault((k, it.get("budget") if k == "image" else None), []).append(i)
    size = {"text": text_batch, "image": image_batch, "audio": audio_batch}
    out = []
    for k, b in sorted(groups, key=lambda kb: (KINDS.index(kb[0]), kb[1] or 0)):
        idx = groups[(k, b)]
        n = max(1, int(size[k] or 1))
        for s in range(0, len(idx), n):
            out.append((k, b, idx[s:s + n]))
    return out


def image_url(b64: str) -> str:
    """图片字节（base64）交给处理器的样子：data URL，放进 digup 的 mlx worker 放文件路径的同一个位置（content 的 "url"），
    处理器就走和文件同一条解码（PIL 打开、按 EXIF 转正、转 RGB），而且不落盘。前缀里的类型只是记号，PIL 按内容认格式。"""
    return "data:image/png;base64," + b64
