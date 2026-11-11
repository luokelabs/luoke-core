"""转写工作进程（V1 §2 /v1/transcribe，2026-11-05：输入法的语音输入）。

在引擎包的 Python 里跑，用引擎包自带的 mlx-audio 以库方式加载语音识别模型（Qwen3-ASR），按行协议转写。和嵌入工作进程同构
（父子之间只有继承的管道；不监听端口，父进程消失即退出），无状态：一次听写的音频由主控攒着（会话），每次整段送来。

    in:  {"op": "transcribe", "audio": "<base64，PCM16LE 16 kHz 单声道>", "context": "…", "language": "Chinese" | "", "max_tokens": 512}
    out: {"text": "…", "ms": 耗时, "audio_secs": 秒数}
    in:  {"op": "ping"}     out: {"ok": true, ...内存}

context 原样当 Qwen3-ASR 的系统提示（它靠这段文字偏向专名）：怎么拼光标前的字和常用说法由调用的工具定，主控不懂内容
（ARCHITECTURE §2）。language 由任务参数或调用方给（例如 "Chinese"）：给了就不让模型自己判断，英文一多时它会把整句译成
英文；空串才让模型判断，它报的「language …」前缀 mlx-audio 已经去掉。
**音频不落盘、不进日志**（V1 §9）：字节只在内存里转成数组；出错只回异常类型。
"""
import argparse
import base64
import json
import sys
import threading
import time

import mlx.core as mx
import numpy as np

SAMPLE_RATE = 16000
DEFAULT_MAX_TOKENS = 512


def mem_mb() -> dict:
    return {"active_mb": round(mx.get_active_memory() / 2**20), "peak_mb": round(mx.get_peak_memory() / 2**20)}


def drop_cache_when_idle(lock, last, idle: float = 30.0) -> None:
    """闲了就把 MLX 的缓冲还给系统（同嵌入进程）：常驻的只剩权重"""
    while True:
        time.sleep(idle / 2)
        with lock:
            if time.monotonic() - last[0] > idle:
                mx.clear_cache()


def pcm16(audio_b64: str) -> mx.array:
    raw = base64.b64decode(audio_b64, validate=True)
    if len(raw) % 2:
        raise ValueError("odd byte count")
    return mx.array(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)


def transcribe(model, audio: mx.array, context: str, max_tokens: int, language: str = "") -> str:
    out = model.generate(audio, max_tokens=max_tokens, system_prompt=context or None, language=language or None, verbose=False)
    text = out.text if hasattr(out, "text") else str(out)
    return text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir")
    ap.add_argument("--cache-mb", type=int, default=512, help="MLX 缓冲上限（MB），同嵌入进程")
    a = ap.parse_args()
    mx.set_cache_limit(max(64, a.cache_mb) * 2**20)
    try:
        from mlx_audio.stt.utils import load
        model = load(a.model_dir)
        transcribe(model, mx.zeros((SAMPLE_RATE,), dtype=mx.float32), "", 8)      # 头一次要编译内核：起来时先付掉
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"{type(e).__name__}: {e}"[:300]}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps({"ready": True, "cache_limit_mb": a.cache_mb, **mem_mb()}), flush=True)
    lock, last = threading.Lock(), [time.monotonic()]
    threading.Thread(target=drop_cache_when_idle, args=(lock, last), daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            print(json.dumps({"error": "bad json"}), flush=True)
            continue
        if req.get("op") == "ping":
            print(json.dumps({"ok": True, **mem_mb()}), flush=True)
            continue
        with lock:
            last[0] = time.monotonic()
            handle(req, model)
    return 0


def handle(req: dict, model) -> None:
    t = time.perf_counter()
    try:
        audio = pcm16(req.get("audio") or "")
        secs = round(audio.shape[0] / SAMPLE_RATE, 2)
        text = transcribe(model, audio, req.get("context") or "", int(req.get("max_tokens") or DEFAULT_MAX_TOKENS),
                          req.get("language") or "") if secs else ""
        print(json.dumps({"text": text, "audio_secs": secs, "ms": round((time.perf_counter() - t) * 1000, 2)}, ensure_ascii=False), flush=True)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}"[:200]}), flush=True)      # 只记类型：信息里可能带了请求内容


if __name__ == "__main__":
    sys.exit(main())
