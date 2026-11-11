"""嵌入任务的 pass/fail（顶层 ADR 0018，2026-10-16）：主控 /v1/embed 算出的向量和 luoke-search 的参考向量，每条余弦都要 ≥ 0.999
——够了就和已建的索引在同一个向量空间，用户不用重建。

    python3 tools/embed_parity.py --socket <实验室主控的 socket> --vectors <luoke-search>/Tests/Reference/vectors.json [--result <结果.json>]

默认核文字的全部条目：输入读 vectors.json 同一目录的 texts.json（luoke-search 维护，逐字是送给主控的 input，已带前缀），
参考向量读 vectors.json 和 vectors-extra.json（2026-11-03 后加的 7 条：中文、3036 token 的长文、中英混排、代码）。
以前这里照 HubParityTests 手抄了原来的 5 条，后加的就没核到；现在两边读同一个文件。加 `--media` 再核图片和声音 4 条（screen-280、
photo-140、frame-70、speech；要主控这一档收图片、声音，即 oMLX 0.7.1 起的引擎包、任务参数有 image_batch / audio_batch）。
`--media auto` 照主控答的这一档任务参数定：有 image_batch 核图片（预算要在 image_budgets 里），有 audio_batch 核声音，都没有只核文字。
媒体的送法和参考向量的做法、HubParityTests 一样（2026-10-19 商定）：
screen.png 预算 280、photo.jpg 预算 140、同一张 photo.jpg 预算 70、speech.wav 整段，文件在 vectors.json 同一目录。
参考向量是 llama.cpp 做的（vectors.json 的 made_with），拷自 digup、一个字节没改（luoke-search 的 Tests/Reference/README.md）。
连的必须是显式给的 socket（实验室实例），不会退回这台机器上在用的主控。

发布评测（tools/release_evals.json 的 luoke-search.embed）每档各跑一遍、带 `--media auto`：向量和档无关，但这一档的嵌入进程
起不起得来、答不答得上、收不收图片和声音是按档的。`--result` 写给发布脚本汇总（summary embed-parity）：主控在哪一档、每条的余弦、最低的、过没过；
请求没成也写（带 error）。退出码：0 全过，1 有一条低于 0.999，2 请求没成。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AGREEMENT = 0.999      # luoke-search HubParityTests.agreement（digup ReferenceVectorTests.agreement）
TASK = "luoke-search.embed"


TEXTS = "texts.json"
EXTRA = "vectors-extra.json"      # 后加的条目的参考向量；原来的 vectors.json 一个字节不改（luoke-search 的 Tests/Reference/README.md）


def reference_texts(ref_dir: str) -> dict:
    """名字 → 送给主控的输入（已带前缀），照 texts.json 的顺序。"""
    with open(os.path.join(ref_dir, TEXTS), encoding="utf-8") as f:
        return {t["name"]: t["input"] for t in json.load(f)["texts"]}


def load_reference(vectors_path: str) -> tuple:
    """(vectors.json 的内容、vectors 并上 vectors-extra.json 的, 三个文件合起来的 sha256)。哈希是发布评测的题集哈希：
    哪个文件变了，台账都看得出题集换过。没有 vectors-extra.json 就只用 vectors.json。"""
    ref_dir = os.path.dirname(os.path.abspath(vectors_path))
    h = hashlib.sha256()
    with open(vectors_path, "rb") as f:
        raw = f.read()
    h.update(raw)
    ref = json.loads(raw)
    ref["vectors"] = dict(ref["vectors"])
    for name in (EXTRA, TEXTS):
        try:
            with open(os.path.join(ref_dir, name), "rb") as f:
                extra = f.read()
        except FileNotFoundError:
            continue
        h.update(extra)
        if name == EXTRA:
            ref["vectors"].update(json.loads(extra)["vectors"])
    return ref, h.hexdigest()


# 媒体参考：名字 → (vectors.json 同目录里的文件, 图片预算；声音 None)。送法同做参考向量时（2026-10-19 商定）
MEDIA = {
    "screen-280": ("screen.png", 280),
    "photo-140": ("photo.jpg", 140),
    "frame-70": ("photo.jpg", 70),          # 同一张照片，预算 70：视频帧当图片按 70 送
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


def media_names(mode: str, params: dict) -> list:
    """这次要核的媒体参考（MEDIA 的名字）。mode 是 off / on / auto；auto 照这一档的任务参数（GET /tasks 里的 params）。"""
    if mode == "on":
        return list(MEDIA)
    if mode != "auto":
        return []
    budgets = params.get("image_budgets")
    images = int(params.get("image_batch") or 0) > 0
    audio = int(params.get("audio_batch") or 0) > 0
    return [n for n, (_, budget) in MEDIA.items()
            if (audio if budget is None else images and (not budgets or budget in budgets))]


def cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def check(reference: dict, got: dict, threshold: float = AGREEMENT, names=None) -> tuple:
    """(全过没有, [(名字, 余弦), ...])。names 缺省是 got 里的；这次没算出来（或答了 null）的、参考向量里没有的，都算 0。"""
    names = list(names) if names is not None else list(got)
    rows = [(name, cosine(got[name], reference[name]) if got.get(name) and name in reference else 0.0) for name in names]
    return bool(rows) and all(c >= threshold for _, c in rows), rows


def result_doc(ref: dict, vectors_sha256: str, names: list, rows: list, ok: bool, hub: dict, rep: dict, media: bool,
               error=None) -> dict:
    """--result 的内容（tools/release.py 的 summarize_embed_parity 读）。rows 是 check 的结果；请求没成时 rows 为空、带 error。"""
    return {"task": TASK, "tier": hub.get("tier"), "status": hub.get("status"), "threshold": AGREEMENT, "media": media,
            "made_with": ref.get("made_with"), "vector_version": ref.get("vector_version"), "vectors_sha256": vectors_sha256,
            "expected": len(names), "model": rep.get("model"), "vector_space": rep.get("vector_space"), "dims": rep.get("dims"),
            "ms": rep.get("ms"), "rows": [{"name": n, "cosine": round(c, 6)} for n, c in rows],
            "min_cosine": round(min(c for _, c in rows), 6) if rows else None, "ok": bool(ok), "error": error,
            "date": time.strftime("%Y-%m-%d")}


def retry_not_ready(call, wait: float, sleep=None, now=None):
    """调 call()；嵌入进程正在加载时主控答 not_ready，按 retry_after（至少 1 s）重试，最多等 wait 秒；别的错误照抛。"""
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
    ap.add_argument("--vectors", required=True, help="luoke-search 的 Tests/Reference/vectors.json")
    ap.add_argument("--model", default="embeddinggemma-2", help="已不用（以前按它看 /status 的 scorers）；留着免得旧命令报错")
    ap.add_argument("--wait", type=float, default=300, help="嵌入进程加载期间（答 not_ready）最多重试几秒")
    ap.add_argument("--media", nargs="?", const="on", default="off", choices=("off", "on", "auto"),
                    help="再核图片和声音 4 条（主控这一档要收图片、声音）；auto 照这一档的任务参数定核哪几条")
    ap.add_argument("--result", help="把结果写成 JSON（发布评测用）")
    a = ap.parse_args(argv)
    from luoke_core.client import HubClient, HubError
    ref, vectors_sha256 = load_reference(a.vectors)
    texts = reference_texts(os.path.dirname(os.path.abspath(a.vectors)))
    names = list(texts)          # 拿到这一档的任务参数后再加上要核的媒体
    mnames: list = []
    c = HubClient("luoke-search/parity", socket_path=a.socket)
    hub: dict = {}
    rep: dict = {}

    def write(rows, ok, error=None):
        if a.result:
            with open(a.result, "w", encoding="utf-8") as f:
                json.dump(result_doc(ref, vectors_sha256, names, rows, ok, hub, rep, bool(mnames), error), f, ensure_ascii=False, indent=1)

    def failed(what: str, e: HubError) -> int:
        print(f"{what}失败：{e.code} {e.message}（看实验室主控的 logs/hub.log）")
        write([], False, f"{e.code}: {e.message}")
        return 2

    try:
        t = c.tasks()
        task = (t.get("tasks") or {}).get(TASK) or {}
        hub = {"tier": t.get("tier"), "status": task.get("status")}
    except HubError as e:
        return failed("GET /tasks ", e)
    mnames = media_names(a.media, task.get("params") or {})
    names += mnames
    # 先试着预热拉起嵌入进程；内存压力 warn 时预热会答 deferred（预热本来就可有可无，2026-10-19 碰到过），照样往下走：
    # 第一条嵌入请求自己会拉起嵌入进程，加载期间答 not_ready，下面按 retry_after 重试到 --wait 为止
    try:
        w = c.warmup(TASK)
        if not w.get("accepted"):
            print(f"预热没被接受（{w.get('code')}），不等它，直接发请求")
    except HubError as e:
        print(f"预热没被接受（{e.code}），不等它，直接发请求")
    tnames = list(texts)
    try:
        rep = retry_not_ready(lambda: c.embed(TASK, [texts[n] for n in tnames], priority="user-waiting"), a.wait)
    except HubError as e:
        return failed("嵌入请求", e)
    ok, rows = check(ref["vectors"], dict(zip(tnames, rep["vectors"])), names=tnames)
    print(f"参考：{ref.get('made_with')}，vector_version {ref.get('vector_version')}；主控 {hub.get('tier')} GB 档："
          f"{rep.get('model')}，{rep.get('dims')} 维，{rep.get('ms')} ms")
    if mnames:
        media = reference_media(os.path.dirname(os.path.abspath(a.vectors)))
        try:
            mrep = retry_not_ready(lambda: c.embed_inputs(TASK, [media[n] for n in mnames], priority="user-waiting"), a.wait)
        except HubError as e:
            return failed("媒体请求", e)
        mok, mrows = check(ref["vectors"], dict(zip(mnames, mrep["vectors"])), names=mnames)
        errors = dict(zip(mnames, mrep.get("errors") or [None] * len(mnames)))
        print(f"媒体 {len(mnames)} 条：{mrep.get('ms')} ms" + "".join(f"；{n} 出错 {e}" for n, e in errors.items() if e))
        ok, rows = ok and mok, rows + mrows
    for name, cos in rows:
        print(f"  {'过' if cos >= AGREEMENT else '没过'}  {name:14s} 余弦 {cos:.5f}")
    print("通过：和已建的索引同一个向量空间" if ok else f"没通过：有一条低于 {AGREEMENT}")
    write(rows, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
