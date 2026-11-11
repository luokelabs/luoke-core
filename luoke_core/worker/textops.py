"""纯文字处理（不依赖 MLX，便于测试）：逐 token 的增量解码、停止串判断（给 score_worker.continue_branches 用）；
底座记号的中和（对话 proxy、打分、嵌入三路共用）。"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, List

PIPED = re.compile(r"<\|[^<>\s]*>|<[^<>\s]*\|>")       # <|fim_prefix|>、Gemma 4 的 <|turn>、<turn|>：正文里不会这么写


def _chat_template(model_path: str) -> str:
    text = ""
    try:
        with open(os.path.join(model_path, "chat_template.jinja"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        pass
    try:
        with open(os.path.join(model_path, "tokenizer_config.json"), encoding="utf-8") as f:
            tpl = json.load(f).get("chat_template")
        text += tpl if isinstance(tpl, str) else json.dumps(tpl or "")
    except (OSError, ValueError, AttributeError):
        pass
    return text


def tokenizer_markers(model_path: str) -> list:
    """模型分词器里要中和的记号（DESIGN §5「按底座的分词器中和」；配方里手写的清单跟不上换代：Gemma 4 的 <|turn>
    写成了 Gemma 3 的 <start_of_turn>）。取 tokenizer.json 的 added_tokens 里三类：标了 special 的；没标、但对话模板里
    用到的（Qwen 的 <think>、<tool_call>）；带竖线的 <|…> / <…|>。别的不算：嵌入模型把连续换行、连续空格、<table> 这类
    HTML 标签也登记成整串 token，那是普通文字，中和了会往正文里插满零宽空格。<unusedN> 占位不算。读不到就是空的。"""
    try:
        with open(os.path.join(model_path, "tokenizer.json"), encoding="utf-8") as f:
            added = json.load(f).get("added_tokens") or []
    except (OSError, ValueError, AttributeError):
        return []
    template = _chat_template(model_path)
    out = []
    for t in added:
        c = t.get("content") if isinstance(t, dict) else None
        if not isinstance(c, str) or len(c) < 2 or not c.strip() or c.startswith("<unused"):
            continue
        if t.get("special") or PIPED.fullmatch(c) or (c[0] in "<[" and c in template):
            out.append(c)
    return out


def neutralize(text: str, markers: list) -> str:
    """把文字里出现的记号中和掉（第一个字后插一个零宽空格），免得配件的文字冒充模板或控制 token。长的先换，免得短的先拆了长的一截。"""
    for tok in sorted(set(markers), key=len, reverse=True):
        if tok and tok in text:
            text = text.replace(tok, tok[0] + "​" + tok[1:])
    return text


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
