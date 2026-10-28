"""嵌入工作进程（V1 §2 /v1/embed，2026-10-16 提议、10-19 认可：全生态推理和权重统一归主控，digup 搜索改调主控；顶层 ADR 0018）。

在引擎包的 Python 里跑，用 mlx-vlm 以库方式加载嵌入模型（EmbeddingGemma 2 的文字部分），按行协议算向量。和打分工作进程同构
（父子之间只有继承的管道；不监听端口，父进程消失即退出），但无状态：没有前缀快照，一进一出。

    in:  {"op": "embed", "texts": ["task: search result | query: …", ...], "max_tokens": 2048}
    out: {"vectors": [[…768 个 float16 精度的数…], ...], "dims": 768, "ms": 耗时}
    in:  {"op": "ping"}     out: {"ok": true, "dims": 768, ...内存}

文字这条路逐行照 digup 的参考 worker（luoke-digup worker/digup_worker.py，MIT，2026-10-16 读的 main 60132ea）的 --text-only：
- 加载：load_config → vision_config、audio_config 置空（只加载 271M 的文字部分）→ mlx_vlm.embedding_loader.load_embedding_model；
  分词器 transformers.AutoTokenizer；
- 检索提示前缀由调用方加好（"task: search result | query: …"、"title: <t> | text: …"），这里不加；
- 分词：tokenizer(批, padding=True, truncation=True, max_length=2048)——特殊 token（<bos>…）由分词器按它的配置加，
  截断长度含特殊 token；每批 16 条；
- 模型自己做双向注意力、均值池化、512→768 投影和 L2 归一化，取 text_embeds；满维输出，转 float16（digup 自己存 float32）。

**引擎包里的 mlx-vlm 是 0.7.1**（oMLX 0.7.0 带的，ADR 0001 不改引擎包）：没有 EmbeddingGemma 2 的模型类型 embedding_gemma2
（mlx-vlm fec3f50 "Add EmbeddingGemma 2 multimodal embedding support" 才加，digup 锁的就是它）。2026-10-16 选方案 (b)：
主控自带那次提交里文字要的三个文件（vendor/embedding_gemma2/，MIT），引擎包的 mlx-vlm 没有这个模型类型时注册上去
（ensure_model_module）；引擎包以后带了就用它自己的。ready 行的 "module" 说用的是哪份。

**图片和声音**（`--media`；和 digup 商定的草案 docs/research/embed-multimodal-draft.md，还不是契约；主控只在任务参数里有
image_batch / audio_batch 时才用这个模式起）：
    in:  {"op": "embed_inputs", "inputs": [{"text": …} | {"image": "<base64>", "budget": 280} | {"audio": "<base64 WAV>"}],
          "max_tokens": 2048, "image_batch": 8, "audio_batch": 2, "max_audio_secs": 30}
    out: {"vectors": [[…] | null, ...], "errors": [null | "原因类别", ...], "dims": 768, "ms": 耗时}
照 digup 参考 worker 的多模态路（不带 --text-only）：mlx_vlm.load 整个模型；图片按预算设 image_processor.max_soft_tokens，
放进对话的 {"type": "image", "url": …}；声音放 {"type": "audio", …}；processor.apply_chat_template 后取 text_embeds；
一批出错就逐条重算，坏的那条给 null 和原因（mediaops）。和参考的区别只在来源：参考给文件路径，这里给字节——
图片用 data URL（处理器同一条解码路），声音用 mlx_audio 的 read 从内存读（和 load_audio 读文件同一个函数），
先混成单声道再重采样（同 load_audio），**都不落盘**（V1 §9：请求内容及其派生物只在内存里）。
要引擎包自带 mlx-vlm ≥ 0.7.8 的 embedding_gemma2（自带的 vendor 只有文字部分），否则 ready 行说明原因、不起来。
文字条目照旧走 AutoTokenizer + 模型（同 --text-only 那条），所以文字向量两种模式一样。
"""
import argparse
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import mlx.core as mx

try:
    from luoke_core.worker import mediaops
except ImportError:      # 工作进程跑在引擎包的 Python 里，luoke_core 不在它的路径上：按文件名读旁边的 mediaops.py（同 score_worker 读 textops）
    _spec = importlib.util.spec_from_file_location("luoke_mediaops", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediaops.py"))
    mediaops = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mediaops)

DEFAULT_MAX_TOKENS = 2048
BATCH = 16          # 同 digup 的 BATCH["text"]
MODEL_MODULE = "mlx_vlm.models.embedding_gemma2"
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "embedding_gemma2")


def ensure_model_module() -> str:
    """引擎包的 mlx-vlm 自带 embedding_gemma2 就用它的（返回 "engine"）；没有（0.7.1）就把 vendor/embedding_gemma2
    （mlx-vlm fec3f50 的文字部分，MIT，2026-10-16 选方案 (b)）注册成这个包名（返回 "vendor"）。
    相对导入 ..gemma4、..pooling、..base 落到引擎包自己的 mlx-vlm；不改引擎包的任何文件。"""
    if MODEL_MODULE in sys.modules:
        return "vendor" if getattr(sys.modules[MODEL_MODULE], "__file__", "").startswith(VENDOR_DIR) else "engine"
    if importlib.util.find_spec(MODEL_MODULE) is not None:
        return "engine"
    spec = importlib.util.spec_from_file_location(MODEL_MODULE, os.path.join(VENDOR_DIR, "__init__.py"),
                                                  submodule_search_locations=[VENDOR_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[MODEL_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[MODEL_MODULE]
        raise
    return "vendor"


def load_model(model_dir):
    """返回 (model, tokenizer, 用的哪份模型代码)。只加载文字部分（同 digup --text-only）。"""
    from mlx_vlm.embedding_loader import load_embedding_model
    from mlx_vlm.utils import load_config
    from transformers import AutoTokenizer

    source = ensure_model_module()
    config = load_config(model_dir)
    config["audio_config"] = None
    config["vision_config"] = None
    try:
        model = load_embedding_model(Path(model_dir), config=config)
    except ValueError as e:
        if "not supported" in str(e):      # mlx-vlm 的 get_model_and_args：没有这个模型类型
            raise RuntimeError(f"引擎包里的 mlx-vlm 加载不了 {config.get('model_type')}（EmbeddingGemma 2 要 mlx-vlm fec3f50 以后）") from e
        raise
    mx.eval(model.parameters())
    return model, AutoTokenizer.from_pretrained(model_dir), source


def load_media_model(model_dir):
    """--media：整个模型（文字、视觉、声音）+ 处理器；返回 (model, 文字用的 tokenizer, processor, 用的哪份模型代码)。"""
    from mlx_vlm import load
    from transformers import AutoTokenizer

    source = ensure_model_module()
    if source != "engine":
        raise RuntimeError("图片和声音要引擎包的 mlx-vlm 自带 embedding_gemma2（≥ 0.7.8，oMLX 0.7.1 起）；主控自带的只有文字部分")
    model, processor = load(model_dir)
    mx.eval(model.parameters())
    return model, AutoTokenizer.from_pretrained(model_dir), processor, source


def decode_audio(b64: str, sampling_rate: int, max_secs: float):
    """WAV 字节（base64）→ 单声道 float32，重采样到模型的采样率。照 mlx_vlm.utils.load_audio 读文件的那几步，只是从内存读。"""
    import base64
    import io

    import numpy as np
    from mlx_audio.audio_io import read as read_audio
    from mlx_audio.utils import resample_audio

    audio, rate = read_audio(io.BytesIO(base64.b64decode(b64, validate=True)), dtype="float32")
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:            # 先混成单声道再重采样（load_audio 的注释：反过来会重采样到声道那一维）
        audio = audio.mean(axis=1)
    if rate != sampling_rate:
        audio = resample_audio(audio, rate, sampling_rate)
    if len(audio) > (max_secs + 0.01) * sampling_rate:
        raise OverflowError(mediaops.AUDIO_TOO_LONG)
    return np.asarray(audio, dtype=np.float32)


def _embed_media(model, processor, kind, payloads, budget):
    if kind == "image":
        processor.image_processor.max_soft_tokens = budget
        conv = [[{"role": "user", "content": [{"type": "image", "url": mediaops.image_url(p)}]}] for p in payloads]
    else:
        conv = [[{"role": "user", "content": [{"type": "audio", "audio": p}]}] for p in payloads]
    inputs = processor.apply_chat_template(conv, tokenize=True, return_dict=True, return_tensors="mlx")
    emb = model(**inputs).text_embeds.astype(mx.float16)
    mx.eval(emb)
    return emb.tolist()


def embed_inputs(model, tok, processor, inputs, max_tokens, image_batch, audio_batch, max_audio_secs):
    """→ (vectors, errors)，都和 inputs 对齐；出错的那条 vector 是 None、error 是原因类别（不带内容）。"""
    n = len(inputs)
    vectors, errors = [None] * n, [None] * n
    rate = int(getattr(getattr(processor, "feature_extractor", None), "sampling_rate", 16000) or 16000)
    for kind, budget, idx in mediaops.plan_batches(inputs, BATCH, image_batch, audio_batch):
        payloads, ok_idx = [], []
        for i in idx:
            item = inputs[i]
            if kind == "audio":
                try:
                    payloads.append(decode_audio(item["audio"], rate, max_audio_secs))
                    ok_idx.append(i)
                except OverflowError:
                    errors[i] = mediaops.AUDIO_TOO_LONG
                except Exception:  # noqa: BLE001
                    errors[i] = mediaops.AUDIO_UNREADABLE
            else:
                payloads.append(item[kind])
                ok_idx.append(i)
        if not ok_idx:
            continue
        run = (lambda ps: embed_texts(model, tok, ps, max_tokens)) if kind == "text" else \
              (lambda ps: _embed_media(model, processor, kind, ps, budget))
        try:
            for i, v in zip(ok_idx, run(payloads)):
                vectors[i] = v
        except Exception:  # noqa: BLE001     一条坏的别连累同批的：逐条重算（同 digup 的做法）
            for i, p in zip(ok_idx, payloads):
                try:
                    vectors[i] = run([p])[0]
                except Exception:  # noqa: BLE001
                    errors[i] = mediaops.IMAGE_UNREADABLE if kind == "image" else mediaops.EMBED_FAILED
    return vectors, errors


def embed_texts(model, tok, texts, max_tokens=DEFAULT_MAX_TOKENS):
    rows = []
    for i in range(0, len(texts), BATCH):
        enc = tok(texts[i:i + BATCH], padding=True, truncation=True, max_length=max_tokens, return_tensors="np")
        out = model(**{k: mx.array(v) for k, v in enc.items()})
        emb = getattr(out, "text_embeds", None)
        if emb is None:
            raise ValueError("这个模型不出嵌入（没有 text_embeds）")
        emb = emb.astype(mx.float16)
        mx.eval(emb)
        rows.extend(emb.tolist())
    return rows


IDLE_CLEAR_SECS = 30


def mem_mb() -> dict:
    return {"active_mb": round(mx.get_active_memory() / 2**20), "cache_mb": round(mx.get_cache_memory() / 2**20),
            "peak_mb": round(mx.get_peak_memory() / 2**20)}


def drop_cache_when_idle(lock: threading.Lock, last: list) -> None:
    while True:
        time.sleep(IDLE_CLEAR_SECS / 3)
        with lock:
            if last[0] is not None and time.monotonic() - last[0] > IDLE_CLEAR_SECS:
                mx.clear_cache()
                last[0] = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir")
    ap.add_argument("--cache-mb", type=int, default=512,
                    help="MLX 缓冲上限（MB）；和打分进程同理（见 score_worker.py）；digup 也是 512")
    ap.add_argument("--media", action="store_true", help="连视觉、声音一起加载，收图片和声音（任务参数里有 image_batch / audio_batch 时）")
    a = ap.parse_args()
    mx.set_cache_limit(max(64, a.cache_mb) * 2**20)
    processor = None
    try:
        if a.media:
            model, tok, processor, source = load_media_model(a.model_dir)
        else:
            model, tok, source = load_model(a.model_dir)
        dims = 0
        for _ in range(2):      # 头两次要编译内核（digup 实测约 18 ms 对 6 ms），起来时先付掉；顺便量出维度
            dims = len(embed_texts(model, tok, ["task: search result | query: warm up"])[0])
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": f"{type(e).__name__}: {e}"[:300]}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps({"ready": True, "dims": dims, "module": source, "media": processor is not None,
                      "cache_limit_mb": a.cache_mb, **mem_mb()}), flush=True)
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
            print(json.dumps({"ok": True, "dims": dims, **mem_mb()}), flush=True)
            continue
        with lock:
            last[0] = time.monotonic()
            handle(req, model, tok, dims, processor)
    return 0


def handle(req: dict, model, tok, dims: int, processor=None) -> None:
    t = time.perf_counter()
    try:
        max_tokens = int(req.get("max_tokens") or DEFAULT_MAX_TOKENS)
        if req.get("op") == "embed_inputs":
            if processor is None:
                raise RuntimeError("这个嵌入进程没带 --media，不收图片和声音")
            vecs, errors = embed_inputs(model, tok, processor, req.get("inputs") or [], max_tokens,
                                        int(req.get("image_batch") or 8), int(req.get("audio_batch") or 2),
                                        float(req.get("max_audio_secs") or 30))
            print(json.dumps({"vectors": vecs, "errors": errors, "dims": dims,
                              "ms": round((time.perf_counter() - t) * 1000, 2)}), flush=True)
            return
        vecs = embed_texts(model, tok, req.get("texts") or [], max_tokens)
        print(json.dumps({"vectors": vecs, "dims": dims, "ms": round((time.perf_counter() - t) * 1000, 2)}), flush=True)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}"[:200]}), flush=True)      # 只记类型：信息里可能带了请求内容


if __name__ == "__main__":
    sys.exit(main())
