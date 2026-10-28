"""嵌入任务的 pass/fail（顶层 ADR 0018，2026-10-16）：主控 /v1/embed 算出的向量和 digup 的参考向量，每条余弦都要 ≥ 0.999
——够了就和 digup 已有的索引兼容，用户不用重建。

    python3 tools/embed_parity.py --socket <实验室主控的 socket> --vectors <luoke-digup>/Tests/LlamaRuntimeTests/Reference/vectors.json

默认核文字的 5 条（query、query-bn、query-ar、document、document-long）。加 `--media` 再核图片和声音 4 条（screen-280、
photo-140、frame-70、speech；要主控这一档收图片、声音，即 oMLX 0.7.1 起的引擎包、任务参数有 image_batch / audio_batch）。
文字输入和 digup 的 Tests/LlamaRuntimeTests/ReferenceVectorTests.swift、Sources/DigUpKit/Model.swift 的 Prompts 逐字一致
（2026-10-16 读的 luoke-digup main 60132ea）；媒体的送法和 digup 做参考向量、它的 HubParityTests 一样（2026-10-19 商定）：
screen.png 预算 280、photo.jpg 预算 140、同一张 photo.jpg 预算 70、speech.wav 整段，文件在 vectors.json 同一目录。
参考向量是 llama.cpp 做的（vectors.json 的 made_with），digup 实测 mlx-vlm 和它 ≥ 0.999。
连的必须是显式给的 socket（实验室实例），不会退回这台机器上在用的主控。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AGREEMENT = 0.999      # digup ReferenceVectorTests.agreement
TASK = "digup.embed"


def query(text: str) -> str:
    return f"task: search result | query: {text}"


def document(title, text: str) -> str:
    return f"title: {title if title else 'none'} | text: {text}"


def reference_texts() -> dict:
    """名字 → 输入（已带前缀）。照 ReferenceVectorTests.texts 逐字抄。"""
    long_text = " ".join(f"Note {n}: the review moved to room {n % 7 + 2} because the projector in the main hall failed again, "
                         "and the team agreed to send the slides the night before." for n in range(1, 61))
    return {
        "query": query("payment declined error"),
        "query-bn": query("ইলিশ মাছ রান্না"),
        "query-ar": query("متى تغلق المكتبة"),
        "document": document("Tokyo itinerary", "Flight to Tokyo Haneda on November 3. Then the "
                             "train to Kamakura to see the Great Buddha, and back for dinner in Shibuya."),
        "document-long": document(None, long_text),     # 约 1800 个 token：过了滑动窗口，全局层也算进来
    }


# 媒体参考：名字 → (vectors.json 同目录里的文件, 图片预算；声音 None)。送法同 digup 做参考向量时（2026-10-19 商定）
MEDIA = {
    "screen-280": ("screen.png", 280),
    "photo-140": ("photo.jpg", 140),
    "frame-70": ("photo.jpg", 70),          # 同一张照片，预算 70：digup 把视频帧当图片按 70 送
    "speech": ("speech.wav", None),         # 整段送
}


def reference_media(ref_dir: str) -> dict:
    """名字 → inputs 里的一条（base64 字节；只在内存里，不复制文件）。"""
    import base64
    out = {}
    for name, (fname, budget) in MEDIA.items():
        with open(os.path.join(ref_dir, fname), "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        out[name] = {"audio": b64} if budget is None else {"image": b64, "budget": budget}
    return out


def cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def check(reference: dict, got: dict, threshold: float = AGREEMENT, names=None) -> tuple:
    """(全过没有, [(名字, 余弦), ...])。names 缺省是文字 5 条；参考里有、这次没算出来（或答了 null）的算 0。"""
    names = list(names) if names is not None else list(reference_texts())
    rows = [(name, cosine(got[name], reference[name]) if got.get(name) else 0.0) for name in names if name in reference]
    return all(c >= threshold for _, c in rows) and len(rows) == len(names), rows


def retry_not_ready(call, wait: float, sleep=None, now=None):
    """调 call()；嵌入进程正在加载时主控答 not_ready，按 retry_after（至少 1 s）重试，最多等 wait 秒；别的错误照抛。"""
    import time
    from luoke_core.client import HubError
    sleep, now = sleep or time.sleep, now or time.time
    deadline = now() + wait
    while True:
        try:
            return call()
        except HubError as e:
            if e.code != "not_ready" or now() >= deadline:
                raise
            sleep(max(1.0, float(e.retry_after or 2)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--socket", required=True, help="实验室主控的 socket（必须显式给）")
    ap.add_argument("--vectors", required=True, help="digup 的 Tests/LlamaRuntimeTests/Reference/vectors.json")
    ap.add_argument("--model", default="embeddinggemma-2", help="已不用（以前按它看 /status 的 scorers）；留着免得旧命令报错")
    ap.add_argument("--wait", type=float, default=300, help="嵌入进程加载期间（答 not_ready）最多重试几秒")
    ap.add_argument("--media", action="store_true", help="再核图片和声音 4 条（主控这一档要收图片、声音）")
    a = ap.parse_args(argv)
    from luoke_core.client import HubClient
    with open(a.vectors, encoding="utf-8") as f:
        ref = json.load(f)
    texts = reference_texts()
    names = list(texts)
    from luoke_core.client import HubError
    c = HubClient("digup/parity", socket_path=a.socket)
    # 先试着预热拉起嵌入进程；内存压力 warn 时预热会答 deferred（预热本来就可有可无，2026-10-19 digup 碰到），照样往下走：
    # 第一条嵌入请求自己会拉起嵌入进程，加载期间答 not_ready，下面按 retry_after 重试到 --wait 为止
    w = c.warmup(TASK)
    if not w.get("accepted"):
        print(f"预热没被接受（{w.get('code')}），不等它，直接发请求")
    try:
        rep = retry_not_ready(lambda: c.embed(TASK, [texts[n] for n in names], priority="user-waiting"), a.wait)
    except HubError as e:
        print(f"嵌入请求失败：{e.code} {e.message}（看实验室主控的 logs/hub.log）")
        return 2
    ok, rows = check(ref["vectors"], dict(zip(names, rep["vectors"])))
    print(f"参考：{ref.get('made_with')}，vector_version {ref.get('vector_version')}；主控：{rep.get('model')}，{rep.get('dims')} 维，{rep.get('ms')} ms")
    if a.media:
        media = reference_media(os.path.dirname(os.path.abspath(a.vectors)))
        mnames = list(media)
        try:
            mrep = retry_not_ready(lambda: c.embed_inputs(TASK, [media[n] for n in mnames], priority="user-waiting"), a.wait)
        except HubError as e:
            print(f"媒体请求失败：{e.code} {e.message}（这一档收图片、声音吗？看 logs/hub.log）")
            return 2
        mok, mrows = check(ref["vectors"], dict(zip(mnames, mrep["vectors"])), names=mnames)
        errors = dict(zip(mnames, mrep.get("errors") or [None] * len(mnames)))
        print(f"媒体 {len(mnames)} 条：{mrep.get('ms')} ms" + "".join(f"；{n} 出错 {e}" for n, e in errors.items() if e))
        ok, rows = ok and mok, rows + mrows
    for name, cos in rows:
        print(f"  {'过' if cos >= AGREEMENT else '没过'}  {name:14s} 余弦 {cos:.5f}")
    print("通过：和 digup 的索引兼容" if ok else f"没通过：有一条低于 {AGREEMENT}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
