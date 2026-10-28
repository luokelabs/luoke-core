"""生成请求的白名单与覆盖（V1 §2）：配件只决定消息、输出格式、条目数；其余由任务参数和底座参数定。"""
from __future__ import annotations

import json
from typing import Optional

ROLES = ("system", "user", "assistant")


class BadRequest(ValueError):
    def __init__(self, code: str, message: str, retry_after: Optional[float] = None, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.extra = extra           # 错误体里多带的字段，例如 busy / paused 的 reason


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):   # OpenAI 的分段内容，只认文字段
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def neutralize(text: str, special_tokens: list) -> str:
    """把消息里出现的底座特殊 token 串中和掉（插一个零宽空格），免得配件文字冒充模板。"""
    for tok in special_tokens:
        if tok and tok in text:
            text = text.replace(tok, tok[0] + "​" + tok[1:])
    return text


def build_engine_payload(body: dict, task_params: dict, base_params: dict, model_id: str,
                         special_tokens: list) -> dict:
    """校验配件的请求体，产出交给 oMLX 的请求体。不合法就抛 BadRequest（在主控处拒绝，不转发）。"""
    if not isinstance(body, dict):
        raise BadRequest("invalid_request", "请求体要是 JSON 对象")
    if body.get("stream") is not True:
        raise BadRequest("invalid_request", "生成请求一律流式：stream 必须是 true")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BadRequest("invalid_request", "messages 要是非空列表")
    clean = []
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ROLES:
            raise BadRequest("invalid_request", "每条消息要有 role（system/user/assistant）")
        text = _text_of(m.get("content"))
        if not isinstance(m.get("content"), (str, list)):
            raise BadRequest("invalid_request", "消息的 content 要是字符串或分段列表")
        total_chars += len(text)
        clean.append({"role": m["role"], "content": neutralize(text, special_tokens)})
    max_chars = int(task_params.get("max_chars", 0) or 0)
    if max_chars and total_chars > max_chars:
        raise BadRequest("input_too_long", f"输入 {total_chars} 字符，超过上限 {max_chars}；请按上限截短或分段")
    items = body.get("luoke_items", 1)
    if not isinstance(items, int) or items < 1:
        raise BadRequest("invalid_request", "luoke_items 要是正整数")
    batch = int(task_params.get("batch", 1) or 1)
    if items > batch:
        raise BadRequest("invalid_request", f"条目数 {items} 超过批大小 {batch}，请分批")
    max_tokens = int(task_params.get("item_tokens", 200)) * items + int(task_params.get("fixed_tokens", 0))
    payload = {
        "model": model_id,
        "messages": clean,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "temperature": base_params.get("temperature", 0),
        "top_p": base_params.get("top_p", 1),
        # top_k 一定写明（0 = 不用 top-k，和 oMLX 0.7.0 的全局默认一样）：oMLX 0.7.1 起会给没存过采样设置的模型存一份
        # generation_config 的推荐值（Qwen top_k 20、Gemma 64），请求里没写就用它，起草回复这类不是 0 温度的任务输出会悄悄变
        # （2026-10-19 rc1 实验室实测：model_settings.json 里存了 top_k 20；请求里的值优先，0 也算）
        "top_k": int(base_params.get("top_k", 0)),
        "chat_template_kwargs": {"enable_thinking": bool(task_params.get("think", False))},
    }
    rf = body.get("response_format")
    if rf is not None:
        if not isinstance(rf, dict) or rf.get("type") not in ("json_schema", "json_object"):
            raise BadRequest("invalid_request", "response_format 只支持 json_schema 或 json_object")
        if rf["type"] == "json_schema":
            js = rf.get("json_schema")
            if not isinstance(js, dict) or not isinstance(js.get("schema"), dict):
                raise BadRequest("invalid_request", "json_schema 要带 schema 对象")
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": str(js.get("name", "out")), "schema": js["schema"]}}
        else:
            payload["response_format"] = {"type": "json_object"}
    return payload


def estimate_secs(payload: dict, prefill_tok_s: float, decode_tok_s: float, chars_per_token: float = 1.6) -> float:
    """粗估一条生成请求要多久：输入字符数 ÷ 每 token 字符数 ÷ prefill 速度，加输出上限 ÷ 解码速度。"""
    chars = sum(len(m["content"]) for m in payload["messages"])
    return chars / chars_per_token / max(prefill_tok_s, 1) + payload["max_tokens"] / max(decode_tok_s, 1)


def error_body(code: str, message: str, retry_after: Optional[float] = None, **extra) -> bytes:
    d = {"code": code, "message": message, "retry_after": retry_after}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


STATUS = {"invalid_request": 400, "forbidden": 403, "unknown_accessory": 403, "task_unavailable": 404, "unsupported": 404,
          "input_too_long": 413, "busy": 503, "paused": 503, "not_ready": 503, "needs_user": 503, "engine_failed": 503}
