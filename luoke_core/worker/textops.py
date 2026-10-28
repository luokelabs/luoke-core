"""续写答复里的纯文字处理（不依赖 MLX，便于测试）：逐 token 的增量解码、停止串判断。给 score_worker.continue_branches 用。"""
from __future__ import annotations

from typing import Callable, List


def token_texts(decode: Callable[[list], str], seq: list) -> List[str]:
    """逐个 token 的增量文字：第 i 个 token 的 text = 解码前 i+1 个 token 的文字比前 i 个多出来的那一段。
    一个汉字被切成几个字节 token 时，前面几个的 text 是空的（解码出 U+FFFD，去掉），拼成的那个 token 带整个字。"""
    out, prev = [], ""
    for i in range(1, len(seq) + 1):
        cur = decode(seq[:i]).replace("�", "")
        out.append(cur[len(prev):] if cur.startswith(prev) else cur)
        prev = cur
    return out


def hit_stop(text: str, stop: list) -> bool:
    """解码出来的文字里出现了停止串里任何一个（空串不算）。"""
    return any(s and s in text for s in stop)
