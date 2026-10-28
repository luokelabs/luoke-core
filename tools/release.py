#!/usr/bin/env python3
"""发布脚本（V1 §7）：评测 → 差异报告 → 写配方 → 引擎包 → 发布清单 → 打包主控 → 签名公证（签名前确认一次指纹）→ 出 DMG。负责人手动跑。

    python3 tools/release.py --release 2026-10-20 [--stage eval,diff,recipe,engine,manifest,build,sign,dmg] [--notarize]
        [--github <账号>/<仓库>] [--modelscope <组织或账号>] [--huggingface <组织或账号>] [--prev <上一个发布号>] [--tiers 16,32,128] [--tasks a,b]
        [--template recipes/dev.json] [--model-source ~/.omlx/models] [--engine-app /Applications/oMLX.app]
        [--ime-corpus <语料.tsv>] [--accept] [--yes]

各阶段（默认全部按顺序跑，--stage 只跑其中几个；后面的阶段读前面阶段留下的文件）：
  eval      起实验室实例（自己的环境目录和 socket，--lab --tier 按档），按 tools/release_evals.json 跑每个任务的评测程序。
            "组合" = 任务用的模型 + 这一档的任务参数：几档共用同一个组合就只测一次（打分任务三档都是同一个专用模型、同样参数），
            参数按档不同就各测各的（分拣的 max_chars 三档不同，所以 16 和 32 GB 档虽然同是 9B 也各评一次）；
            共用的组合按其中最大的档跑（评的是质量；各代芯片的回放归人工，V1 §7）。
            汇总写 releases/<发布号>/evidence/<任务>.<组合>.json；配件评测程序的逐条结果经 --out / --result 直接写进 evidence/raw/
            （命令里的 {raw}；语料不进来，配件的工作区也不留东西）。
  diff      和上一个发布的汇总比：硬性条目零回退、总分不低于底线、不低于上次减容差、"支持"变"不支持"算回退。写 diff.md；
            有不过的就停下（硬性条目没过或低于底线：不能发；只是回退：看过报告后加 --accept 放行）。
            硬性条目按档算（V1 §7）：一个任务在某档没过、别的档过了 → 配方里只在过的档支持它，不挡发布（上次那档支持的才算回退）；
            所有档都没过才是不能发。
  recipe    recipes/<发布号>.json：模板加 recipe_version、已验证版本（评测过的配件版本和组合）、依据（每条汇总的结论和证据哈希）。
  engine    引擎包：拷一份 oMLX.app、用 release_sign.sh --preserve-entitlements 重签（--notarize 时公证）、zip、哈希；
            旁边生成它带的 Python 包的清单 THIRD-PARTY-PACKAGES.txt（名字、版本、许可）。
  manifest  发布清单 releases/<发布号>/release.json：模型目录逐文件哈希、引擎包；地址按 --modelscope / --github 填，
            没给就用本机 file:// 地址（只能本机测试，不能发）。同时把模型和引擎包的摘要写回配方。
  build     打主控 .app（配方和发布清单放进 Resources；app/build_app.sh）。
  sign      Developer ID 签名：先打印证书指纹，确认后才签（release_sign.sh；--notarize 时公证并 staple）。
  dmg       出 DMG（make_dmg.sh：签名，--notarize 时公证并 staple）；把主控 DMG 加进发布清单；打印要上传的文件。

产物：build.noindex/release/<发布号>/（不进仓库；.noindex 让 Spotlight 不索引，LaunchServices 就不会把这些副本按 bundle id 登记进去）；
      releases/<发布号>/（进仓库：evidence/、diff.md、release.json）；recipes/<发布号>.json。
上传不在这里：建仓库、上传每次先问负责人（V1 §10）。私钥一直在钥匙串里，脚本不碰它。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from luoke_core import __version__ as HUB_VERSION, apps, download, machine  # noqa: E402
from luoke_core.client import HubClient, HubError  # noqa: E402
from luoke_core.recipe import Recipe  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support  # noqa: E402  任务 × 档的支持台账
sys.path.insert(0, os.path.join(ROOT, "tools"))
import minos_check  # noqa: E402  发布前核对每一项里 Mach-O 的最低 macOS

PY = "/usr/bin/python3"
SH_ENV = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")   # shell 脚本里有中文：没有 UTF-8 locale 时 bash 会把 $var 后面的中文标点当变量名
STAGES = ["eval", "diff", "recipe", "engine", "manifest", "build", "sign", "dmg"]
EVALS = os.path.join(ROOT, "tools", "release_evals.json")
MODELSCOPE_URL = "https://modelscope.cn/api/v1/models/{org}/{repo}/repo?Revision={rev}&FilePath={{path}}"
GITHUB_URL = "https://github.com/{repo}/releases/download/{tag}"
HUGGINGFACE_URL = "https://huggingface.co/{org}/{repo}/resolve/main/{{path}}"   # 第二地址（2026-10-16 定）；主控测速后选快的、断了换
APP_NAME = "螺壳"


def say(msg: str) -> None:
    print(f"== {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr, flush=True)
    sys.exit(code)


def sha256_text(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Paths:
    def __init__(self, release: str):
        self.release = release
        self.out = os.path.join(ROOT, "build.noindex", "release", release)   # .noindex：Spotlight 不索引，副本不进 LaunchServices
        self.rec = os.path.join(ROOT, "releases", release)
        self.evidence = os.path.join(self.rec, "evidence")
        self.raw = os.path.join(self.evidence, "raw")
        self.recipe = os.path.join(ROOT, "recipes", f"{release}.json")
        self.manifest = os.path.join(self.rec, "release.json")
        self.diff = os.path.join(self.rec, "diff.md")
        self.app = os.path.join(self.out, "app", f"{APP_NAME}.app")
        # 每一版同一个文件名（luoke-brand#6）：官网链 releases/latest/download/Luoke.dmg，不用每版改官网。
        # 不用中文名：GitHub 的附件名不收中文（10-16 传 beta0 时 404）；DMG 里的应用照旧叫「螺壳」
        self.dmg = os.path.join(self.out, "Luoke.dmg")
        self.engine_dir = os.path.join(self.out, "engine")
        self.engine_info = os.path.join(self.engine_dir, "engine.json")
        self.acc_dir = os.path.join(self.out, "accessories")


# ---- 组合与汇总（纯函数，有单元测试） ----------------------------------------------------------------------

def combo_key(model: str, params: dict) -> str:
    return f"{model}:{sha256_text(json.dumps(params, sort_keys=True, ensure_ascii=False))[:8]}"


def plan_combos(recipe: Recipe, tiers: list, tasks: list) -> dict:
    """{(任务, 组合键): {task, model, base, params, tiers}}：同一个"模型 + 任务参数"的组合几档共用就只出现一次。"""
    plan: dict = {}
    for tier in tiers:
        base = recipe.base_for(tier)
        for task in tasks:
            if not recipe.task(task):
                continue
            model = recipe.model_for_task(task, tier)
            if not model:
                continue
            params = recipe.params(task, tier)
            key = combo_key(model, params)
            c = plan.setdefault((task, key), {"task": task, "key": key, "model": model, "base": base, "params": params, "tiers": []})
            c["tiers"].append(tier)
    return plan


def run_tier(tiers: list) -> str:
    """一个组合按哪一档跑：最大的那档（底座常驻，评的是质量）。"""
    return max(tiers, key=lambda t: int(t))


def summarize_mail_triage(result: dict) -> dict:
    s = result["score"]
    hard_ok = s.get("false_alarm", 0) == 0 and s.get("safety", 0) >= s.get("safety_n", 0) - 1
    return {"hard_ok": hard_ok, "score": s["basic"] + s["hard"], "score_max": s["basic_n"] + s["hard_n"], "n": s.get("n"),
            "latency": {"avg_seconds": s.get("avg_seconds")},
            "detail": {k: s.get(k) for k in ("basic", "basic_n", "hard", "hard_n", "action", "safety", "safety_n",
                                              "false_alarm", "normal_n", "json_first_try", "wall_seconds")},
            "machine": result.get("machine"), "date": result.get("date")}


def summarize_ime_rerank(stdout: str) -> dict:
    m = re.search(r"最终首选 ([\d.]+)%", stdout)
    if not m:
        raise RuntimeError("评测程序的输出里没有「最终首选」")
    n = re.search(r"n=(\d+)", stdout)
    # 延迟看"最后一个键到不标 more 的答复"那一行（壳按空格时用的就是它）；没有这一行就取第一行有 p50/p90 的
    lat = next((ln for ln in stdout.splitlines() if "不标 more 的答复" in ln), stdout)
    p50 = re.search(r"p50 (\d+) ms", lat)
    p90 = re.search(r"p90 (\d+) ms", lat)
    hit = re.search(r"前缀缓存命中 ([\d.]+)%", stdout)
    return {"hard_ok": True, "score": float(m.group(1)), "score_max": 100.0, "n": int(n.group(1)) if n else None,
            "latency": {"p50_ms": int(p50.group(1)) if p50 else None, "p90_ms": int(p90.group(1)) if p90 else None},
            "detail": {"prefix_cache_hit_pct": float(hit.group(1)) if hit else None, "stdout_tail": stdout[-800:]}}


def summarize_mail_task(result: dict) -> dict:
    """luoke-mail bench/run_tasks.py 的结果：score.passes 是硬性条目（一次都不能错），ok/n 是总分，set_sha256 是题集哈希。"""
    s = result["score"]
    return {"hard_ok": bool(s.get("passes")), "score": s["ok"], "score_max": s["n"], "n": s["n"],
            "latency": {"seconds": s.get("seconds")},
            "detail": {k: v for k, v in s.items() if k not in ("ok", "n", "passes", "seconds")},
            "evalset_sha256": result.get("set_sha256"), "machine": result.get("machine"), "date": result.get("date")}


def summarize_ime_result(result: dict) -> dict:
    """luoke-ime engines/xiaohe/bench.py --result：score.final_top1_pct 是总分，latency.settled 是最后一个键到不标 more 的答复。"""
    sc, lat = result["score"], result.get("latency") or {}
    settled = lat.get("settled") or {}
    return {"hard_ok": True, "score": float(sc["final_top1_pct"]), "score_max": 100.0, "n": (result.get("evalset") or {}).get("n"),
            "latency": {"p50_ms": settled.get("p50_ms"), "p90_ms": settled.get("p90_ms"), "first_p90_ms": (lat.get("first") or {}).get("p90_ms")},
            "detail": {"settled_top1_pct": sc.get("settled_top1_pct"), "fast_top1_pct": sc.get("fast_top1_pct"), "space": sc.get("space"),
                       "hub": result.get("hub"), "engine_options": (result.get("combo") or {}).get("engine_options")},
            "evalset_sha256": (result.get("evalset") or {}).get("sha256"), "machine": result.get("machine"), "date": result.get("date")}


def summarize_ime_predict(result: dict) -> dict:
    """luoke-ime engines/personal/eval_predict.py --result：总分 = 给出联想时第一个对的比例（现在用的门槛）。"""
    sc, lat = result["score"], result.get("latency") or {}
    return {"hard_ok": True, "score": float(sc["top1_when_shown_pct"]), "score_max": 100.0, "n": (result.get("evalset") or {}).get("n"),
            "latency": {"p50_ms": lat.get("p50_ms"), "p90_ms": lat.get("p90_ms")},
            "detail": {"shown_pct": sc.get("shown_pct"), "top3_when_shown_pct": sc.get("top3_when_shown_pct"), "gate": sc.get("gate"), "gates": sc.get("gates")},
            "evalset_sha256": (result.get("evalset") or {}).get("sha256"), "machine": result.get("machine"), "date": result.get("date")}


def summarize_mem_topics(result: dict) -> dict:
    """luoke-mem bench/run_topics.py（报告格式照 run_typing）：hard_pass 是硬性条目；总分 = interest_recall（%），
    dislike_recall、只凭标签和清理说不感兴趣的条数（dislikes_unsaid，软指标）、说到具体对象的比例（specific，luoke-mem 0.23.1 起）在 detail 里。"""
    sc = result["score"]
    def pct(k):
        v = sc.get(k)
        return round(100 * float(v), 1) if v is not None else None
    return {"hard_ok": bool(result.get("hard_pass")), "score": pct("interest_recall") or 0.0, "score_max": 100.0, "n": int(result.get("cases") or 1),
            "latency": {"seconds": sc.get("seconds")},
            "detail": {"dislike_recall": pct("dislike_recall"), "dislikes_unsaid": sc.get("dislikes_unsaid"), "specific": pct("specific"),
                       "hard_failures": sc.get("hard_failures")},
            "evalset_sha256": result.get("set_sha256"), "machine": result.get("machine"), "date": result.get("date")}


def summarize_mem_typing(result: dict) -> dict:
    """luoke-mem bench/run_typing.py：hard_pass 是硬性条目；总分 = term_recall（%），interest_recall、每例的普通词误收在 detail 里。"""
    sc = result["score"]
    cases = int(result.get("cases") or 1)
    return {"hard_ok": bool(result.get("hard_pass")), "score": round(100 * float(sc["term_recall"]), 1), "score_max": 100.0, "n": cases,
            "latency": {"seconds": sc.get("seconds")},
            "detail": {"interest_recall": round(100 * float(sc["interest_recall"]), 1), "common_terms_per_case": round(float(sc["common_terms"]) / cases, 2),
                       "common_terms": sc.get("common_terms"), "hard_failures": sc.get("hard_failures")},
            "evalset_sha256": result.get("set_sha256"), "machine": result.get("machine"), "date": result.get("date")}


# 评测程序没跑完、原因是主控 / 引擎说内存放不下：这一档不支持，不是评测出了毛病
# 评测看门狗（10-20 定，internal-1020 在 16 GB 测试机上被内存守卫拒了 95 分钟、原地重试）：生成任务连续这么久
# 实验室主控日志里没有一条这个任务 outcome=ok 的请求，就停掉评测程序，记这一档没过，接着往下评。
# 打分、联想、嵌入不进这类日志，评测程序跑完才出声（输入法 rerank 在 128 档要 12 分钟，internal-1021 被误判 stalled、
# 把开着的档关了）：这几类只在总时长超过 EVAL_MAX_SECS 时才停
EVAL_STALL_SECS = 600
EVAL_MAX_SECS = 3600


class EvalResult:
    def __init__(self, returncode: int, stdout: str, stderr: str, stalled: Optional[str] = None):
        self.returncode, self.stdout, self.stderr, self.stalled = returncode, stdout, stderr, stalled


def _stop(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _watch(p, hub_log, log_pos, task, kind, stall_secs, poll, max_secs, last, started) -> Optional[str]:
    """盯着评测程序直到它结束；没进展、超时就停掉它，答 stalled 的原因（没停答 None）。"""
    refused = False
    stalled = None
    while p.poll() is None:
        time.sleep(poll)
        progressed = False
        try:
            if os.path.getsize(hub_log) < log_pos:
                log_pos = 0           # 日志被截断或重建了（主控现在只追加、不轮转，防一手）：从头读
            with open(hub_log, encoding="utf-8", errors="replace") as f:
                f.seek(log_pos)
                chunk = f.read()
                log_pos = f.tell()
        except OSError:
            chunk = ""
        for line in chunk.splitlines():
            if f"task={task} " not in line:
                continue
            if "outcome=ok" in line:
                progressed = True
            elif "status_400" in line or "memory" in line:
                refused = True
        if progressed:
            last, refused = time.time(), False
        if (kind == "generate" and time.time() - last > stall_secs) or time.time() - started > max_secs:
            stalled = "engine_refused_memory" if refused else "stalled"
            _stop(p)
            break
    return stalled


def run_watched(cmd: list, cwd: str, env: dict, task: str, kind: str, hub_log: str, stall_secs: float = EVAL_STALL_SECS,
                poll: float = 5.0, max_secs: float = EVAL_MAX_SECS) -> EvalResult:
    """跑评测程序；stall_secs 没进展就先 SIGTERM、10 秒后 SIGKILL，返回 stalled（"engine_refused_memory" 或 "stalled"）。"""
    import tempfile
    out_f, err_f = tempfile.TemporaryFile(mode="w+"), tempfile.TemporaryFile(mode="w+")
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out_f, stderr=err_f, text=True)
    try:
        log_pos = os.path.getsize(hub_log)
    except OSError:
        log_pos = 0
    last = started = time.time()
    try:
        stalled = _watch(p, hub_log, log_pos, task, kind, stall_secs, poll, max_secs, last, started)
    finally:
        _stop(p)        # 被 SIGTERM（出版程序打断、换下一版）时也停掉评测程序，不留孤儿抢测试机的内存（10-26 16 GB 测试机）
    out_f.seek(0)
    err_f.seek(0)
    return EvalResult(p.returncode if p.returncode is not None else -1, out_f.read(), err_f.read(), stalled)


MEMORY_REFUSAL = re.compile(r"(内存不够|memory guard rejected|too_large)[^\n]*")

SUMMARIZERS = {"mail-triage": "result", "mail-task": "result", "ime-result": "result", "ime-predict-result": "result",
               "mem-typing": "result", "mem-topics": "result", "ime-rerank-stdout": "stdout"}


def apply_known_issues(summ: dict, spec: dict, tier: str, result_path: Optional[str]) -> None:
    """负责人点过头的已知问题（评测表 known_issues，按档、按题，例如 10-26 定的 0.7.2 在 32 GB 档的两格）：硬性条目只因为
    这些题没过时算过，记录里写明免了哪几题；多一题别的、或换了档，照旧没过。两种：
    - false_alarm（分拣）：这几封被标了可疑、且误报数正好等于它们的个数，可疑漏报照旧最多 1 封；
    - never（打字归纳）：硬性失败只出在这几例上、且每一条都含这一例写明的词。"""
    issues = (spec.get("known_issues") or {}).get(str(tier)) or []
    if summ.get("hard_ok") or not issues or not result_path or not os.path.exists(result_path):
        return
    with open(result_path, encoding="utf-8") as f:
        res = json.load(f)
    d = summ.get("detail") or {}
    waived = []
    if all(i.get("kind") == "false_alarm" for i in issues):
        flagged = {x.get("tag") for x in res.get("results") or [] if (x.get("triage") or {}).get("suspicious")}
        waived = [i for i in issues if i["tag"] in flagged]
        if not waived or d.get("false_alarm", 0) != len(waived) or d.get("safety", 0) < d.get("safety_n", 0) - 1:
            return
    elif all(i.get("kind") == "never" for i in issues):
        by_case = {i["tag"]: i for i in issues}
        failing = [x for x in res.get("items") or [] if x.get("hard_failures")]
        if not failing or any(x["case"] not in by_case or any(f"「{by_case[x['case']]['word']}」" not in h for h in x["hard_failures"])
                              for x in failing):
            return
        waived = [by_case[x["case"]] for x in failing]
    else:
        raise RuntimeError(f"known_issues 里有不认识的 kind：{issues}")
    summ["hard_ok"] = True
    summ["known_issues"] = [{"tag": i["tag"], "note": i.get("note", "")} for i in waived]


def summarize(kind: str, result_path: Optional[str], stdout: str) -> dict:
    if kind == "mail-triage":
        with open(result_path, encoding="utf-8") as f:
            return summarize_mail_triage(json.load(f))
    if kind == "mail-task":
        with open(result_path, encoding="utf-8") as f:
            return summarize_mail_task(json.load(f))
    if kind == "mem-typing":
        with open(result_path, encoding="utf-8") as f:
            return summarize_mem_typing(json.load(f))
    if kind == "mem-topics":
        with open(result_path, encoding="utf-8") as f:
            return summarize_mem_topics(json.load(f))
    if kind in ("ime-result", "ime-predict-result"):
        with open(result_path, encoding="utf-8") as f:
            return (summarize_ime_result if kind == "ime-result" else summarize_ime_predict)(json.load(f))
    if kind == "ime-rerank-stdout":
        return summarize_ime_rerank(stdout)
    raise RuntimeError(f"不认识的汇总方式 {kind}")


def judge(cur: dict, prev: Optional[dict], spec: dict) -> dict:
    """一条任务×组合的结论：pass / regression（看过报告可放行）/ fail（不能发）。"""
    reasons = []
    verdict = "pass"
    if not cur.get("hard_ok"):
        verdict = "fail"
        reasons.append("硬性条目没过" + ("（上次过了，回退）" if prev and prev.get("hard_ok") else ""))
    floor = spec.get("floor")
    if floor is not None and cur["score"] < floor:
        verdict = "fail"
        reasons.append(f"总分 {cur['score']} 低于底线 {floor}")
    detail = cur.get("detail") or {}
    for k, v in (spec.get("floors") or {}).items():          # 分项底线（detail 里的字段）
        if detail.get(k) is not None and detail[k] < v:
            verdict = "fail"
            reasons.append(f"{k} {detail[k]} 低于底线 {v}")
    for k, v in (spec.get("ceilings") or {}).items():        # 分项上限（例如误收的普通词）
        if detail.get(k) is not None and detail[k] > v:
            verdict = "fail"
            reasons.append(f"{k} {detail[k]} 超过上限 {v}")
    same_set = prev is not None and (prev.get("evalset") or {}).get("sha256") == (cur.get("evalset") or {}).get("sha256")
    if prev and not same_set and (prev.get("evalset") or cur.get("evalset")):
        reasons.append("题集换过了，总分不和上次比")
    elif prev:
        tol = spec.get("tolerance", 0)
        if cur["score"] < prev["score"] - tol:
            if verdict == "pass":
                verdict = "regression"
            reasons.append(f"总分 {cur['score']} 比上次 {prev['score']} 低，超过容差 {tol}")
        if prev.get("params") != cur.get("params") and verdict == "pass":
            reasons.append("任务参数和上次不同（不算回退，提醒）")
    return {"verdict": verdict, "reasons": reasons}


def passed(d: dict, evals: dict) -> bool:
    """一遍算不算过：硬性条目过，分项底线、上限都满足（judge 不给 fail）。台账只认这一个判定。"""
    return judge(d, None, evals.get(d["task"], {}))["verdict"] != "fail"


def task_tiers(template: dict) -> dict:
    """{任务: [模板里这个任务可能开的档]}：模板写了 tiers 就是那几档，没写就是配方里所有的档。"""
    assign = list((template.get("assignment") or {}).keys())
    return {task: [str(x) for x in (t.get("tiers") if t.get("tiers") is not None else assign)] for task, t in (template.get("tasks") or {}).items()}


def ledger_for(release: str, prev_release: Optional[str], cur: dict, template: dict, evals: dict) -> tuple:
    """(上一版台账, 这一版台账)。规则全在 tools/support.py。"""
    prev = support.previous(prev_release, ROOT)
    holds = {task: spec.get("held_unsupported") or {} for task, spec in evals.items()}
    runs = support.runs_by_cell(cur, lambda d: passed(d, evals))
    return prev, support.build(release, prev, runs, holds, task_tiers(template))


def diff_report(release: str, prev_release: Optional[str], cur: dict, prev: dict, evals: dict, template: dict) -> tuple:
    """cur / prev：{(任务, 组合键, 档): 汇总}。返回 (markdown, 结论, 台账)。
    哪一档开、哪一档关由台账按规则定（失败一遍就关、连过 3 遍才开），不挡出版，只列出来；
    挡出版的只有：还过着的组合总分比上次低出容差（regression，看过报告加 --accept 放行），和主要任务哪一档都没开（fail）。
    这一版没评的档（本机版只评 128）不比、不算回退。"""
    prev_led, led = ledger_for(release, prev_release, cur, template, evals)
    lines = [f"# 差异报告 {release}" + (f"（对比 {prev_release}）" if prev_release else "（第一个发布，没有可比的）"), "",
             "| 任务 | 模型 | 档 | 本次 | 上次 | 这一遍 | 结论 |", "|---|---|---|---|---|---|---|"]
    worst = "pass"
    rank = {"pass": 0, "regression": 1, "fail": 2}
    for (task, key, tier), c in sorted(cur.items()):
        model = c["model"]
        p = prev.get((task, key, tier)) or next((v for (t, _k, _tier), v in prev.items() if t == task and v["model"] == model and _tier == tier), None)
        j = judge(c, p, evals.get(task, {}))
        verdict = "regression" if j["verdict"] == "regression" else "pass"     # 没过（fail）的由台账关掉这一档，不挡出版
        worst = max(worst, verdict, key=lambda v: rank[v])
        if c.get("known_issues"):
            j["reasons"] = j.get("reasons", []) + ["已知问题例外：" + "、".join(k["tag"] for k in c["known_issues"])]
        mark = {"pass": "过" if j["verdict"] != "fail" else "没过", "regression": "回退"}[verdict]
        lines.append(f"| {task} | {model} | {', '.join(c['tiers'])} | {c['score']}/{c['score_max']} | "
                     f"{(str(p['score']) + '/' + str(p['score_max'])) if p else '—'} | {mark} | {'；'.join(j['reasons'])} |")
    lines += ["", "## 支持台账（哪一档开着）", "", "| 任务 | 档 | 上一版 | 这一版 | 理由 |", "|---|---|---|---|---|"]
    for task in sorted(led):
        for tier in sorted(led[task], key=int):
            c = led[task][tier]
            was = ((prev_led.get(task) or {}).get(tier) or {}).get("state", "—")
            lines.append(f"| {task} | {tier} | {was} | {c['state']}{' ←' if was not in ('—', c['state']) else ''} | {c['why']} |")
    for task, t in (template.get("tasks") or {}).items():
        if t.get("main") and not support.on_tiers(led, task):
            worst = "fail"
            lines.append(f"\n主要任务 {task} 哪一档都没开。")
    lines += ["", {"pass": "**结论：放行。**", "regression": "**结论：有总分回退，看过报告后用 --accept 放行。**",
                   "fail": "**结论：不能发（主要任务哪一档都没开）。**"}[worst]]
    return "\n".join(lines) + "\n", worst, led


# ---- 评测 ----------------------------------------------------------------------------------------------------

def load_evals() -> dict:
    with open(EVALS, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def read_version(repo: str, spec: dict) -> str:
    if "manifest" in spec:
        with open(os.path.join(repo, spec["manifest"]), encoding="utf-8") as f:
            return json.load(f)["version"]
    with open(os.path.join(repo, spec["file"]), encoding="utf-8") as f:
        m = re.search(spec["regex"], f.read())
    if not m:
        raise RuntimeError(f"{repo}: 读不到版本")
    return m.group(1)


def repo_commit(repo: str) -> Optional[str]:
    """配件仓库的提交号：git 检出直接问 git；实验室机器上的 rsync 副本看 .luoke-commit（同步时写的，不进对方仓库）。"""
    marker = os.path.join(repo, ".luoke-commit")
    if os.path.exists(marker):
        with open(marker, encoding="utf-8") as f:
            return f.read().strip() or None
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or None


def machine_summary() -> dict:
    chip = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    ver = subprocess.run(["/usr/bin/sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip()
    return {"chip": chip, "mem_gb": round(machine.mem_gb()), "macos": ver}


class LabHub:
    """评测用的实验室实例：自己的环境目录、自己的 socket，只停自己起的进程。"""

    def __init__(self, paths: Paths, recipe_path: str, recipe: Recipe, tier: str, model_source: str):
        self.recipe_path, self.recipe, self.tier, self.model_source = recipe_path, recipe, tier, model_source
        self.env = os.path.join(paths.out, "lab", tier)
        self.models = os.path.join(self.env, "models")
        sock = os.path.join(self.env, "run", "hub.sock")
        if len(sock.encode("utf-8")) > 100:      # AF_UNIX 限 104 字节：退到 /tmp 下的短名字（发布号取哈希）
            sock = f"/tmp/luoke-rel-{sha256_text(paths.release)[:8]}-{tier}.sock"
        self.sock = sock
        self.proc: Optional[subprocess.Popen] = None
        self.log_path = os.path.join(self.env, "hub.log")

    def _link_models(self) -> None:
        os.makedirs(self.models, exist_ok=True)
        for name, m in self.recipe.models.items():
            for cand in (m.get("source"), m.get("dir")):
                src = os.path.join(self.model_source, cand) if cand else None
                if src and os.path.isdir(src):
                    dst = os.path.join(self.models, m["dir"])
                    if os.path.islink(dst) or os.path.exists(dst):
                        os.remove(dst) if os.path.islink(dst) else shutil.rmtree(dst)
                    os.symlink(src, dst)
                    break

    def start(self) -> dict:
        os.makedirs(os.path.join(self.env, "run"), exist_ok=True)
        self._link_models()
        if os.path.exists(self.sock):
            os.remove(self.sock)      # 自己环境目录里的旧 socket
        cmd = [PY, "-B", "-m", "luoke_core", "serve", "--lab", "--tier", self.tier, "--recipe", self.recipe_path,
               "--model-dir", self.models, "--env", self.env, "--socket", self.sock]
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                     env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.path.expanduser("~")})
        say(f"实验室实例（{self.tier} GB 档）启动中：{self.sock}")
        deadline = time.time() + 1200
        while time.time() < deadline:
            if self.proc.poll() is not None:
                die(f"实验室实例起不来（退出码 {self.proc.returncode}），看 {self.log_path}")
            if os.path.exists(self.sock):
                try:
                    st = HubClient(f"luoke-core/{HUB_VERSION}", self.sock).status()
                    smoke = st.get("smoke") or {}
                    if st.get("phase") == "running" and smoke.get("ok"):
                        # 评测用全速功耗档：均衡档的"后台跑多久歇多久"是给用户的舒适度规则，评测程序连发 background 请求会撞上 busy
                        HubClient(f"luoke-core/{HUB_VERSION}", self.sock).settings({"power_mode": "full", "background_on_battery": True})
                        return st
                    if st.get("phase") == "running" and smoke.get("ok") is False and not smoke.get("deferred"):
                        self.stop()
                        die(f"冒烟检查没过：{smoke.get('error')}（看 {self.log_path}）")
                except (HubError, OSError):
                    pass
            time.sleep(2)
        self.stop()
        die(f"等了 20 分钟主控还没就绪（内存被别的引擎占着、冒烟检查推迟？看 {self.log_path}）")

    def stop(self) -> None:
        p = self.proc
        if not p or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(60)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)


def stage_eval(a, paths: Paths, recipe: Recipe) -> None:
    evals = load_evals()
    tasks = [t.strip() for t in a.tasks.split(",")] if a.tasks else list(evals)
    unknown = [t for t in tasks if t not in evals]
    if unknown:
        die(f"评测表里没有：{', '.join(unknown)}（tools/release_evals.json）")
    tiers = [t.strip() for t in a.tiers.split(",")]
    plan = plan_combos(recipe, tiers, tasks)
    os.makedirs(paths.raw, exist_ok=True)
    by_tier: dict = {}
    for c in plan.values():
        by_tier.setdefault(run_tier(c["tiers"]), []).append(c)
    say(f"要评 {len(plan)} 个组合：" + "；".join(f"{c['task']} × {c['model']}（{'/'.join(c['tiers'])} GB 档）" for c in plan.values()))
    mach = machine_summary()
    # 评测跑在哪个引擎上：配方 engine.app 的实际版本必须等于配方写的版本（审查 2026-10-17：负责人把自己的 oMLX 升了，评测跑 0.7.1、记录写 0.7.0）
    want_eng = str(recipe.engine.get("version") or "")
    try:
        with open(os.path.join(recipe.engine.get("app") or "", "Contents", "Info.plist"), "rb") as f:
            engine_actual = "omlx-" + str(plistlib.load(f).get("CFBundleShortVersionString"))
    except OSError:
        die(f"配方的引擎 {recipe.engine.get('app')} 不在")
    if engine_actual != want_eng:
        die(f"评测要跑的引擎是 {engine_actual}，配方写的是 {want_eng}：对不上就不评")
    for tier in sorted(by_tier, key=int):
        hub = LabHub(paths, a.template, recipe, tier, a.model_source)
        st = hub.start()
        try:
            for c in by_tier[tier]:
                spec = evals[c["task"]]
                repo = os.path.normpath(a.repo_override.get(spec["accessory"]) or os.path.join(ROOT, spec["repo"]))   # --repo 指定的冻结副本优先
                subs = {"hub": hub.sock, "label": f"release-{paths.release}-{tier}", "repo": repo, "ime_corpus": a.ime_corpus, "raw": paths.raw}
                for req in spec.get("requires", []):
                    if not subs.get(req) or not os.path.exists(subs[req]):
                        die(f"{c['task']} 需要 {req}（给 --{req.replace('_', '-')}）")
                cmd = [x.format(**subs) for x in spec["command"]]
                ver_before, commit_before = read_version(repo, spec["version"]), repo_commit(repo)   # 评的是哪一版：开跑前记下
                say(f"评 {c['task']}（{c['model']}，{tier} GB 档；{spec['accessory']} {ver_before} {str(commit_before)[:7]}）：{' '.join(cmd)}")
                r = run_watched(cmd, repo, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.path.expanduser("~")},
                                c["task"], (recipe.task(c["task"]) or {}).get("kind", "generate"), os.path.join(hub.env, "logs", "hub.log"))
                if (read_version(repo, spec["version"]), repo_commit(repo)) != (ver_before, commit_before):   # 跑的过程中配件仓库动了：证据说不清评的是哪一版
                    die(f"{c['task']} 评测期间 {repo} 从 {ver_before} {str(commit_before)[:7]} 变成了 "
                        f"{read_version(repo, spec['version'])} {str(repo_commit(repo))[:7]}（2026-10-15 遇到过：评到一半仓库升版）；等它稳定后重跑")
                tail = (r.stdout or "")[-2000:]
                print(tail, flush=True)
                refused = None
                if r.returncode == NOT_EVALUATED_EXIT and NOT_EVALUATED_EXIT not in (spec.get("ok_exit_codes") or [0]) and not r.stalled:
                    # 评测程序自己判"没评成"（10-22 定，luoke-ime 的 bench.py / eval_predict.py 起：主控拒了打分、一直没就绪）：
                    # 这一格这一版不写证据，台账沿用上一版的开关，整版照出；记进 not-evaluated.json，发布说明和流程报告照实写
                    why = ((r.stderr or "").strip().splitlines() or ["没评成"])[-1][:300]
                    say(f"{c['task']}：没评成，这一格这一版不写结果、台账沿用上一版（{why}）")
                    note_not_evaluated(paths.evidence, c["task"], tier, why)
                    continue
                if r.stalled:
                    say(f"{c['task']}：{EVAL_STALL_SECS // 60} 分钟没有进展（{r.stalled}），停掉评测程序，这一档记为没过")
                    refused = re.match(r"(.*)", f"{r.stalled}：{EVAL_STALL_SECS // 60} 分钟没有一条成功的请求")
                elif r.returncode not in (spec.get("ok_exit_codes") or [0]):    # 有的评测程序用退出码 2 表示"跑完了但硬性没过"
                    print(r.stderr[-2000:], file=sys.stderr)
                    refused = MEMORY_REFUSAL.search((r.stderr or "")[-4000:] + (r.stdout or "")[-2000:])
                    if not refused:
                        die(f"{c['task']} 的评测程序退出码 {r.returncode}")
                result_path = spec["result"].format(**subs) if spec.get("result") and not refused else None
                if result_path and not os.path.isabs(result_path):
                    result_path = os.path.join(repo, result_path)
                if refused:
                    # 这一档放不下（internal-1018：16 GB 档 topics 被 oMLX 的预填内存闸挡了）：记成这一档没过，不让整版停下
                    summ = {"hard_ok": False, "score": 0.0, "score_max": 100.0, "n": 0, "latency": {},
                            "detail": {"error": r.stalled or "engine_refused_memory", "message": refused.group(0)[:200]}}
                    if not r.stalled:
                        say(f"{c['task']}：这一档内存放不下（引擎拒绝），记为没过")
                else:
                    summ = summarize(spec["summary"], result_path, r.stdout or "")
                    apply_known_issues(summ, spec, tier, result_path)
                evalset = spec["evalset"].format(**subs)
                evalset_path = evalset if os.path.isabs(evalset) else os.path.join(repo, evalset)
                rec = {"release": paths.release, "task": c["task"], "accessory": spec["accessory"],
                       "accessory_version": ver_before, "accessory_commit": commit_before,
                       # 沿用证据要比的东西（审查 2026-10-17）：主控代码、实际跑的引擎、评测表这一条、语料、依赖的配件
                       "hub_commit": repo_commit(ROOT), "engine_actual": engine_actual,
                       "eval_spec_sha256": sha256_text(json.dumps(spec, sort_keys=True, ensure_ascii=False)),
                       "corpus_sha256": download.sha256_file(a.ime_corpus) if "ime_corpus" in (spec.get("requires") or []) else None,
                       "dep_commits": {dep: repo_commit(os.path.normpath(a.repo_override.get(dep) or os.path.join(ROOT, "..", dep)))
                                       for dep in spec.get("depends") or []},
                       "combo": {"key": c["key"], "model": c["model"], "base": c["base"], "tiers": c["tiers"], "params": c["params"]},
                       "tier_run": tier, "hub_version": HUB_VERSION, "recipe_template": recipe.version,
                       "engine": recipe.engine.get("version"), "base_loaded": st.get("base"),
                       "date": time.strftime("%Y-%m-%d"), "machine": mach, "hard": spec.get("hard"),
                       "evalset": {"path": os.path.relpath(evalset_path, repo) if evalset_path.startswith(repo) else os.path.basename(evalset_path),
                                   "sha256": summ.pop("evalset_sha256", None) or download.sha256_file(evalset_path)},   # 评测程序自己算的题集哈希优先
                       "command": cmd, **summ}
                if result_path and os.path.exists(result_path):
                    raw_name = f"{c['task']}.{c['key'].replace(':', '.')}.json"
                    raw_path = os.path.join(paths.raw, raw_name)
                    rec["result_sha256"] = download.sha256_file(result_path)
                    if os.path.abspath(result_path).startswith(os.path.abspath(paths.raw)):
                        os.replace(result_path, raw_path)      # 评测程序经 --out / --result 直接写在我们这边（配件工作区不留东西）
                    else:
                        shutil.copyfile(result_path, raw_path)
                        if not a.keep_accessory_results:
                            os.remove(result_path)      # 老式的评测程序写在配件仓库里：拷走后删掉
                    rec["raw"] = os.path.join("raw", raw_name)
                out = os.path.join(paths.evidence, f"{c['task']}.{c['key'].replace(':', '.')}.{tier}.json")   # 带档：几台机器各评一档时合并不撞名
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(rec, f, ensure_ascii=False, indent=1)
                say(f"{c['task']}：{rec['score']}/{rec['score_max']}，硬性条目{'过' if rec['hard_ok'] else '没过'} → {os.path.relpath(out, ROOT)}")
        finally:
            hub.stop()


# ---- 差异报告、配方 -----------------------------------------------------------------------------------------

NOT_EVALUATED_EXIT = 3     # 评测程序的约定（luoke-ime 10-22）：3 = 没评成（不是没过），stderr 最后一行写原因


def note_not_evaluated(evidence_dir: str, task: str, tier: str, why: str) -> None:
    """记一格"没评成"：evidence/not-evaluated-<档>.jsonl（.jsonl：load_evidence 不当证据读；放在 evidence 里，远端评的跟着拿回来）。"""
    with open(os.path.join(evidence_dir, f"not-evaluated-{tier}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"task": task, "tier": str(tier), "why": why, "at": time.time()}, ensure_ascii=False) + "\n")


def load_not_evaluated(rec_dir: str) -> dict:
    """{(任务, 档): 原因}：这一版没评成的格子（同一格记过几次取最后一次；后来又评成了的不算）。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(rec_dir, "evidence", "not-evaluated-*.jsonl"))):
        for line in open(p, encoding="utf-8"):
            try:
                x = json.loads(line)
            except ValueError:
                continue
            out[(x["task"], str(x["tier"]))] = x.get("why", "")
    done = {(d["task"], str(d.get("tier_run"))) for d in load_evidence(rec_dir).values()}
    return {k: v for k, v in out.items() if k not in done}


def load_evidence(rec_dir: str) -> dict:
    """{(任务, 组合键, 评测时的档): 汇总}。同一任务、同一模型在不同档参数不同（分拣的 max_chars）或在不同机器上各评过一次
    （几台机器各跑一档再合并）都是不同的记录；以前按（任务, 模型）去重会互相覆盖（2026-10-15 合并三档时发现）。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(rec_dir, "evidence", "*.json"))):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["_file"] = p
        d["tiers"] = d["combo"]["tiers"]
        d["params"] = d["combo"]["params"]
        d["model"] = d["combo"]["model"]
        out[(d["task"], d["combo"]["key"], str(d.get("tier_run") or run_tier(d["tiers"])))] = d
    return out


def published(rec: str) -> bool:
    try:
        with open(os.path.join(rec, "steps.json"), encoding="utf-8") as f:
            return "publish" in json.load(f)
    except (OSError, ValueError, TypeError):
        return False


def previous_release(current: str, explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    # 只在正式出过的版本里挑（beta*、internal-MMDD[-n]），不挑 test-*、试跑这类（审查 2026-10-17：按字母序会挑到空证据的 test-*，回退闸门等于没开）
    pat = re.compile(r"^(beta\d+(\.\d+)?|internal-\d{4}(-\d+)?)$")
    root = os.path.join(ROOT, "releases")
    # 只挑真发出去了的（steps.json 记了 publish）：没出成的那版只有半截证据、没有台账，拿它比，台账从头算、证据也缺
    # （10-26：internal-1026-2 两次没出成，新开的 1026-5 按证据时间会挑到它）
    names = [n for n in os.listdir(root) if n != current and pat.match(n) and os.path.isdir(os.path.join(root, n, "evidence"))
             and published(os.path.join(root, n))] if os.path.isdir(root) else []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(root, n, "evidence")))
    return names[-1] if names else None


def stage_diff(a, paths: Paths) -> None:
    cur = load_evidence(paths.rec)
    if not cur:
        die(f"{paths.evidence} 里没有汇总，先跑 eval")
    prev_name = previous_release(paths.release, a.prev)
    prev = load_evidence(os.path.join(ROOT, "releases", prev_name)) if prev_name else {}
    with open(a.template, encoding="utf-8") as f:
        template = json.load(f)
    md, verdict, led = diff_report(paths.release, prev_name, cur, prev, load_evals(), template)
    support.save(paths.rec, led)
    with open(paths.diff, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    say(f"差异报告：{os.path.relpath(paths.diff, ROOT)}")
    if verdict == "fail":
        die("不能发：主要任务哪一档都没开。", 2)
    if verdict == "regression" and not a.accept:
        die("有总分回退：看过报告后加 --accept 再跑后面的阶段。", 2)


def stage_recipe(a, paths: Paths) -> None:
    with open(a.template, encoding="utf-8") as f:
        r = json.load(f)
    cur = load_evidence(paths.rec)
    if not cur:
        die("没有汇总，先跑 eval")
    r["recipe_version"] = paths.release
    r["release"] = paths.release
    verified = {"luoke-core": {"versions": [HUB_VERSION]}}
    evidence = []
    for (task, _key, _tier), d in sorted(cur.items()):
        model = d["model"]
        v = verified.setdefault(d["accessory"], {"versions": [], "tasks": {}})
        if d["accessory_version"] not in v["versions"]:
            v["versions"].append(d["accessory_version"])
        ev_sha = download.sha256_file(d["_file"])
        v["tasks"].setdefault(task, []).append({"model": model, "tiers": d["tiers"], "accessory_version": d["accessory_version"],
                                                "score": d["score"], "score_max": d["score_max"], "hard_ok": d["hard_ok"], "evidence": ev_sha})
        evidence.append({"accessory": d["accessory"], "version": d["accessory_version"], "task": task, "model": model, "tiers": d["tiers"],
                         "conclusion": f"{d['accessory']} {d['accessory_version']} 的 {task} 在 {model}（{'/'.join(d['tiers'])} GB 档）上 "
                                       f"{d['score']}/{d['score_max']}，硬性条目{'过' if d['hard_ok'] else '没过'}"
                                       + (f"（已知问题例外：{'、'.join(k['tag'] for k in d['known_issues'])}）" if d.get("known_issues") else "")
                                       + f"（{d['machine'].get('chip', '')}，{d['date']}）",
                         **({"known_issues": d["known_issues"]} if d.get("known_issues") else {}),
                         "evalset_sha256": d["evalset"]["sha256"], "evidence_sha256": ev_sha, "machine": d["machine"], "date": d["date"]})
    r["verified"] = verified
    r["evidence"] = evidence
    # 哪一档开：只看支持台账（tools/support.py；diff 阶段算好存在 support.json，没跑 diff 就现算）
    led = support.load(paths.rec)
    if led is None:
        _prev, led = ledger_for(paths.release, previous_release(paths.release, a.prev), cur, r, load_evals())
        support.save(paths.rec, led)
    for task, t in r["tasks"].items():
        if task in led:
            t["tiers"] = support.on_tiers(led, task)
            t["_tiers"] = "支持台账：" + "；".join(f"{tier} GB {c['state']}（{c['why']}）" for tier, c in sorted(led[task].items(), key=lambda kv: int(kv[0])))
    with open(paths.recipe, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    Recipe.load(paths.recipe)      # 能加载
    say(f"配方：{os.path.relpath(paths.recipe, ROOT)}（已验证：" +
        "；".join(f"{k} {', '.join(v['versions'])}" for k, v in verified.items()) + "）")


# ---- 引擎包、清单、打包、签名、DMG --------------------------------------------------------------------------

def package_licenses(app: str, site_rel: str, out: str) -> int:
    rows = []
    for meta in sorted(glob.glob(os.path.join(app, site_rel, "*.dist-info", "METADATA"))):
        name = version = lic = ""
        with open(meta, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("Name:"):
                    name = line[5:].strip()
                elif line.startswith("Version:"):
                    version = line[8:].strip()
                elif line.startswith("License-Expression:"):
                    lic = line.split(":", 1)[1].strip()
                elif line.startswith("License:") and not lic:
                    lic = line[8:].strip()[:80]
                elif line.startswith("Classifier: License ::") and not lic:
                    lic = line.rsplit("::", 1)[1].strip()
                elif not line.strip():
                    break
        rows.append(f"{name} {version}: {lic or '未标'}")
    with open(out, "w", encoding="utf-8") as f:
        f.write("# 引擎包（oMLX 副本）里带的 Python 包：名字 版本: 许可（来自各包的 METADATA；未改动的 PyPI 包）\n" + "\n".join(rows) + "\n")
    return len(rows)


def listing_from(m: dict) -> Optional[dict]:
    """「添加应用」里装之前看得到的介绍（顶层 ADR 0014 的 10-27 修订）：从配件清单的 capabilities 抄一句话、类别和权限说明。
    大小由流水线实测、档位由配方定，配件自己写的 size_mb / min_tier 不抄；权限只抄说明，开关的默认值和动作名不抄（状态由工具照实报）。"""
    caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
    src = caps.get("listing") if isinstance(caps.get("listing"), dict) else {}
    out = {k: src[k] for k in ("tagline", "category") if isinstance(src.get(k), str) and src[k].strip()}
    perms = []
    for p in caps.get("permissions") or []:
        if isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("title"), str):
            perms.append({k: p[k] for k in ("id", "kind", "title", "facts", "without") if k in p})
    if perms:
        out["permissions"] = perms
    return out or None


def stage_engine(a, paths: Paths, recipe: Recipe) -> None:
    src = a.engine_app
    if not os.path.isdir(src):
        die(f"没有引擎 .app：{src}")
    # 包里一律叫 oMLX.app（主控只认这个名字）：出版机器上的引擎是 ~/Applications/oMLX-<版本>.app，原样带进去就装不上（luoke-core#31）
    dst = os.path.join(paths.engine_dir, "oMLX.app")
    os.makedirs(paths.engine_dir, exist_ok=True)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    say(f"拷引擎 {src} → {dst}")
    subprocess.run(["ditto", src, dst], check=True)
    with open(os.path.join(dst, "Contents", "Info.plist"), "rb") as f:
        pl = plistlib.load(f)
    ver = pl.get("CFBundleShortVersionString", "?")
    if recipe.engine.get("version") and f"omlx-{ver}" != recipe.engine["version"]:
        die(f"引擎 {src} 是 {ver}，配方要的是 {recipe.engine['version']}：对不上不打包")
    # 引擎包真正要求的 macOS：Info.plist 说的和包里所有 Mach-O 的 minos 取最高（oMLX 0.7.0 写 15.0，但 mlx 是 26.2）；记进清单项，首次运行低于它就拦
    machos = list(minos_check.iter_machos(dst))
    top = max([str(pl.get("LSMinimumSystemVersion") or "0")] + [v for _, v in machos], key=minos_check.version_tuple)
    say(f"引擎包 {len(machos)} 个 Mach-O，最低 macOS 要求 {top}（Info.plist 写 {pl.get('LSMinimumSystemVersion')}）")
    n = package_licenses(dst, recipe.engine.get("site", ""), os.path.join(paths.engine_dir, "THIRD-PARTY-PACKAGES.txt"))
    say(f"引擎包里 {n} 个 Python 包的许可清单：{os.path.relpath(os.path.join(paths.engine_dir, 'THIRD-PARTY-PACKAGES.txt'), ROOT)}")
    cmd = ["sh", os.path.join(ROOT, "tools", "release_sign.sh"), dst, "--preserve-entitlements"] + (["--notarize"] if a.notarize else [])
    subprocess.run(cmd, check=True, env=SH_ENV)
    version = f"omlx-{ver}"
    zip_path = os.path.join(paths.engine_dir, f"engine-{version}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", dst, zip_path], check=True)
    info = {"version": version, "omlx": ver, "zip": zip_path, "size": os.path.getsize(zip_path), "sha256": download.sha256_file(zip_path),
            "notarized": bool(a.notarize), "min_macos": top}
    with open(paths.engine_info, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    say(f"引擎包：{zip_path}（{info['size'] / 2**30:.2f} GB，sha256 {info['sha256'][:12]}…）")


def modelscope_pinned_url(org: str, repo: str) -> str:
    """ModelScope 地址钉到最新的 luoke-* 版本标签（2026-10-17 同意在我们自己的模型仓库建标签、不用每次问；改模型时顺手建新标签）。
    没有标签就停：不再跟着 master 走。"""
    with urllib.request.urlopen(f"https://modelscope.cn/api/v1/models/{org}/{repo}/revisions", timeout=30) as r:
        tags = [t.get("Revision") for t in ((json.load(r).get("Data") or {}).get("RevisionMap") or {}).get("Tags") or []]
    tags = sorted(t for t in tags if t and t.startswith("luoke-"))
    if not tags:
        die(f"ModelScope {org}/{repo} 没有 luoke-* 版本标签：先建一个（build.noindex/msvenv 里 HubApi().create_model_tag）")
    return MODELSCOPE_URL.format(org=org, repo=repo, rev=tags[-1])


def hf_pinned_url(org: str, repo: str) -> str:
    """Hugging Face 地址钉到当前提交（审查 2026-10-17：跟着 main 走，上游一改所有已发版本都哈希失败）。
    ModelScope 那边钉版本标签（modelscope_pinned_url）。"""
    with urllib.request.urlopen(f"https://huggingface.co/api/models/{org}/{repo}", timeout=30) as r:
        sha = json.load(r).get("sha")
    if not sha:
        die(f"拿不到 {org}/{repo} 在 Hugging Face 上的提交号")
    return f"https://huggingface.co/{org}/{repo}/resolve/{sha}/{{path}}"


def stage_manifest(a, paths: Paths) -> None:
    if not os.path.exists(paths.recipe):
        die("没有配方，先跑 recipe")
    with open(paths.recipe, encoding="utf-8") as f:
        r = json.load(f)
    recipe = Recipe.load(paths.recipe)
    tiers = [t.strip() for t in a.tiers.split(",")]
    needed = []
    for t in tiers:
        asg = recipe.assignment.get(t) or {}
        needed += ([asg["base"]] if asg.get("base") else []) + list((asg.get("special") or {}).values())
    items = {}
    local = False
    for name in dict.fromkeys(needed):
        m = recipe.models[name]
        src = next((os.path.join(a.model_source, c) for c in (m.get("source"), m["dir"]) if c and os.path.isdir(os.path.join(a.model_source, c))), None)
        if not src:
            die(f"模型目录里没有 {name}（{m.get('source') or m['dir']}），--model-source {a.model_source}")
        if a.url_base:
            item = download.make_item_from_dir(src, name, "model", r["recipe_version"], a.url_base.rstrip("/") + "/models/" + urllib.parse.quote(m["dir"]))
        elif a.modelscope or a.huggingface:
            item = download.make_item_from_dir(src, name, "model", r["recipe_version"], "")
            item["urls"] = ([modelscope_pinned_url(a.modelscope, m["dir"])] if a.modelscope else []) + \
                           ([hf_pinned_url(a.huggingface, m["dir"])] if a.huggingface else [])
        else:
            local = True
            item = download.make_item_from_dir(src, name, "model", r["recipe_version"], "file://" + os.path.abspath(src))
        item["license"] = m.get("license")
        items[name] = item
        r["models"][name]["sha256"] = download.item_digest(item)
        say(f"{name}: {len(item['files'])} 个文件，{download.item_total(item) / 2**30:.2f} GB")
    # 配件 .app（各项目公证过的）：zip 后当清单项，首次运行按勾选下载安装（kind=accessory）
    os.makedirs(paths.acc_dir, exist_ok=True)
    if any(spec.split("=", 1)[0] == "luoke-mem" for spec in a.accessory):
        die("螺壳记忆已并进主控（V1 §6），清单里不能再有 luoke-mem 这一项")
    for spec in a.accessory:
        name, app = spec.split("=", 1)
        app = os.path.abspath(os.path.expanduser(app))
        mp = os.path.join(app, "Contents", "Resources", "luoke-accessory.json")
        if not os.path.exists(mp):
            die(f"{app} 不是配件（没有 luoke-accessory.json）")
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        if m.get("accessory") != name:
            die(f"{app} 的配件标识是 {m.get('accessory')}，不是 {name}")
        if subprocess.run(["spctl", "-a", "-t", "exec", app], capture_output=True).returncode != 0:
            die(f"{app} 没过 Gatekeeper 评估（没签名公证？），不能进清单")
        apps._ls("-u", app)      # spctl 会把这份 .app 登记进 LaunchServices：注销，别和负责人在用的那份抢 bundle id（登记只在 luoke_core/apps.py 管）
        limit, bad = minos_check.check_app(app)     # 包里任何 Mach-O 要的系统高于它声称的最低 macOS 就不出清单（2026-10-15 启动器、输入法引擎都中过招）
        if bad:
            die(f"{app} 声称最低 macOS {limit}，但这些程序 / 库要更高的系统，不能进清单：\n  " +
                "\n  ".join(f"{v}  {p}" for p, v in sorted(bad)[:20]) + ("" if len(bad) <= 20 else f"\n  …共 {len(bad)} 个"))
        zip_path = os.path.join(paths.acc_dir, f"{name}-{m['version']}.zip")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        subprocess.run(["ditto", "-c", "-k", "--keepParent", app, zip_path], check=True)
        base = a.url_base.rstrip("/") if a.url_base else (GITHUB_URL.format(repo=a.github, tag=paths.release) if a.github else "file://" + paths.acc_dir)
        local = local or not (a.github or a.url_base)
        item = download.make_item_from_file(zip_path, "accessory", m["version"], base)
        item.update({"accessory": name, "bundle_id": m["bundle_id"], "type": m.get("type", "app"), "name": m.get("name", name),
                     "memory": bool(m.get("memory")), "installer": f"安装{m.get('name', name).replace('螺壳', '')}.app"})
        if m.get("min_macos"):
            item["min_macos"] = str(m["min_macos"])     # 配件清单的可选字段：首次运行低于它就不列为可装（输入法 26 起）
        listing = listing_from(m)
        if listing:
            item["listing"] = listing
        items[name] = item
        say(f"{name}: {m.get('name')} {m['version']}（{item['type']}，{os.path.getsize(zip_path) / 2**20:.0f} MB）")
    if os.path.exists(paths.engine_info):
        with open(paths.engine_info, encoding="utf-8") as f:
            e = json.load(f)
        base = a.url_base.rstrip("/") if a.url_base else (GITHUB_URL.format(repo=a.github, tag=paths.release) if a.github else "file://" + paths.engine_dir)
        local = local or not (a.github or a.url_base)
        items["engine"] = download.make_item_from_file(e["zip"], "engine", e["version"], base)
        if e.get("min_macos"):
            items["engine"]["min_macos"] = e["min_macos"]     # 引擎包实际要求的 macOS（engine 阶段按包里的 Mach-O 算出）
        r["engine"]["version"] = e["version"]
        r["engine"]["sha256"] = e["sha256"]
    else:
        say("注意：没有引擎包（没跑 engine），清单里不带它")
    manifest = {"contract": 1, "release": paths.release, "hub_version": HUB_VERSION, "built_at": int(time.time()), "items": items}   # built_at：安装器据此判断"比已装的新"（升级，V1 §6）
    if local:
        manifest["local_only"] = True
    os.makedirs(paths.rec, exist_ok=True)
    with open(paths.manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(paths.recipe, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    say(f"发布清单：{os.path.relpath(paths.manifest, ROOT)}" + ("（本机 file:// 地址，只能本机测试，不能发；给 --modelscope 和 --github）" if local else ""))


def stage_build(a, paths: Paths) -> None:
    for p, what in ((paths.recipe, "配方"), (paths.manifest, "发布清单")):
        if not os.path.exists(p):
            die(f"没有{what}：{p}")
    out = os.path.join(paths.out, "app")
    mem_src = a.repo_override.get("luoke-mem")
    if not mem_src or not os.path.isfile(os.path.join(mem_src, "packaging", "accessory.json")):
        die("螺壳记忆要打进主控（V1 §6）：用 --repo luoke-mem=<钉住的提交的检出> 指定，不用并排的工作区（审查 2026-10-17）")
    with open(os.path.join(mem_src, "packaging", "accessory.json"), encoding="utf-8") as f:
        mem_ver = json.load(f)["version"]
    if not Recipe.load(paths.recipe).version_verified("luoke-mem", mem_ver):
        die(f"要打进主控的螺壳记忆 {mem_ver} 不在配方的已验证版本里：learn 会被拒")
    env = dict(SH_ENV, LUOKE_RECIPE_FILE=paths.recipe, LUOKE_RELEASE_FILE=paths.manifest, LUOKE_MEM_SRC=mem_src,
               LUOKE_BUNDLE_ID="ai.luoke.core")      # 正式版的身份；build_app.sh 默认是开发版（ai.luoke.core.dev，10-23）
    subprocess.run(["sh", os.path.join(ROOT, "app", "build_app.sh"), out], check=True, env=env)
    limit, bad = minos_check.check_app(paths.app)
    if bad:
        die(f"主控 .app 声称最低 macOS {limit}，但这些程序 / 库要更高的系统：\n  " + "\n  ".join(f"{v}  {p}" for p, v in sorted(bad)[:20]))
    say(f"主控：{paths.app}（所有 Mach-O 的 minos ≤ {limit}）")


def resolve_identity(identity: str) -> str:
    """证书名 → 唯一一张的 SHA-1。确认时显示的和真正签名用的是同一张（审查 2026-10-17：之前 grep | head -1 和 codesign 各挑各的）。"""
    out = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    hits = [l.split()[1] for l in out.splitlines() if identity in l and len(l.split()) > 2 and len(l.split()[1]) == 40]
    if len(set(hits)) != 1:
        die(f"钥匙串里 \"{identity}\" 的签名证书有 {len(set(hits))} 张，要正好一张")
    return hits[0]


def confirm_identity(identity: str, yes: bool) -> str:
    sha1 = resolve_identity(identity)
    pem = subprocess.run(["security", "find-certificate", "-a", "-Z", "-p", "-c", identity], capture_output=True, text=True).stdout
    blocks = re.findall(r"SHA-1 hash: ([0-9A-F]{40})\n(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)", pem, re.S)
    pem_one = next((b for h, b in blocks if h == sha1), "")
    if not pem_one:
        die(f"钥匙串里找不到 SHA-1 为 {sha1} 的证书")
    fp = subprocess.run(["openssl", "x509", "-noout", "-subject", "-fingerprint", "-sha256", "-enddate"], input=pem_one,
                        capture_output=True, text=True).stdout.strip()
    print(f"签名用的证书（SHA-1 {sha1}）：\n  " + fp.replace("\n", "\n  "), flush=True)
    if not yes:
        if not sys.stdin.isatty():
            die("要确认证书指纹：在终端里跑（或 --yes）")
        if input("指纹对吗？用这张证书签 [y/N] ").strip().lower() != "y":
            die("没签。")
    return sha1


def stage_sign(a, paths: Paths) -> None:
    if not os.path.isdir(paths.app):
        die(f"没有主控 .app：{paths.app}（先跑 build）")
    sha1 = confirm_identity(a.identity, a.yes)
    cmd = ["sh", os.path.join(ROOT, "tools", "release_sign.sh"), paths.app, "--identity", sha1] + (["--notarize"] if a.notarize else [])
    subprocess.run(cmd, check=True, env=SH_ENV)


def stage_dmg(a, paths: Paths) -> None:
    if not os.path.isdir(paths.app):
        die(f"没有主控 .app：{paths.app}")
    sha1 = resolve_identity(a.identity)
    cmd = ["sh", os.path.join(ROOT, "tools", "make_dmg.sh"), paths.app, paths.dmg, "--identity", sha1, "--drag"] + (["--notarize"] if a.notarize else [])
    subprocess.run(cmd, check=True, env=SH_ENV)
    with open(paths.manifest, encoding="utf-8") as f:
        manifest = json.load(f)
    base = a.url_base.rstrip("/") if a.url_base else (GITHUB_URL.format(repo=a.github, tag=paths.release) if a.github else "file://" + paths.out)
    manifest["items"]["hub"] = download.make_item_from_file(paths.dmg, "hub", HUB_VERSION, base)
    # 发布只出螺壳一个 DMG（10-26，luoke#7："装一个螺壳，在里面开启不同的应用"）：应用都在螺壳的「应用」窗口里装、升级，
    # 不再出按应用命名的安装器 DMG（ADR 0002 的做法，螺壳里认这种安装器的代码留着，旧的 DMG 打开照样能用）。--installer-dmgs 还能出
    manifest["installers"] = {}
    for name, item in manifest["items"].items():
        if not a.installer_dmgs or item.get("kind") != "accessory" or not item.get("installer"):
            continue
        # DMG 文件名用 ASCII（GitHub Releases 的附件名不接受中文，2026-10-16 上传 beta0 时 404）；DMG 里的 .app 仍叫「安装<应用>」
        dmg = os.path.join(paths.out, f"install-{name.replace('luoke-', '')}-{paths.release}.dmg")
        cmd = ["sh", os.path.join(ROOT, "tools", "make_dmg.sh"), paths.app, dmg, "--identity", sha1, "--as", item["installer"]] + (["--notarize"] if a.notarize else [])
        subprocess.run(cmd, check=True, env=SH_ENV)
        manifest["installers"][name] = {"file": os.path.basename(dmg), "size": os.path.getsize(dmg), "sha256": download.sha256_file(dmg),
                                        "url": base + "/" + urllib.parse.quote(os.path.basename(dmg))}
    with open(paths.manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    shutil.copyfile(paths.manifest, os.path.join(paths.out, "release.json"))
    say("发布清单已加上主控 DMG（.app 里那份没有它：v1 不自更新）")
    uploads = [f"{paths.dmg}  → GitHub Releases（tag {paths.release}）"]
    if os.path.exists(paths.engine_info):
        with open(paths.engine_info, encoding="utf-8") as f:
            uploads.append(f"{json.load(f)['zip']}  → GitHub Releases（tag {paths.release}）")
    for name, item in manifest["items"].items():
        if item["kind"] == "model":
            uploads.append(f"{a.model_source}/…/{item['dir']}  → ModelScope {a.modelscope or '<组织或账号>'}/{item['dir']}")
    for name, ins in (manifest.get("installers") or {}).items():
        uploads.append(f"{os.path.join(paths.out, ins['file'])}  → GitHub Releases（官网上「{name}」的下载按钮指它）")
    for name, item in manifest["items"].items():
        if item.get("kind") == "accessory":
            uploads.append(f"{os.path.join(paths.acc_dir, item['files'][0]['path'])}  → GitHub Releases（首次运行按清单下载安装）")
    uploads.append(f"{os.path.join(paths.out, 'release.json')}  → GitHub Releases（同一个 tag；主控现在不读远端清单，先放着）")
    print("\n要上传的（上传是对外发布，先问负责人；建仓库也是）：\n  " + "\n  ".join(uploads), flush=True)
    if manifest.get("local_only"):
        print("注意：清单里是本机 file:// 地址，这一版不能发。", flush=True)


def main() -> None:
    import signal
    # 出版程序打断（换下一版）、或远端换下一轮时会 SIGTERM 这个进程：转成 SystemExit，finally 里停掉实验室实例（连同它的引擎）
    # 和评测程序。默认的 SIGTERM 直接结束进程，那些子进程成了孤儿接着占内存（10-26 16 GB 测试机：beta1 的复测被打断后又跑了 16 分钟）
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", required=True, help="发布号，例如 2026-10-20（也是 GitHub Releases 的 tag）")
    ap.add_argument("--stage", default=",".join(STAGES), help="只跑这些阶段（逗号分隔，按固定顺序）")
    ap.add_argument("--template", default=os.path.join(ROOT, "recipes", "dev.json"), help="配方模板")
    ap.add_argument("--tiers", default="16,32,128")
    ap.add_argument("--tasks", default=None, help="只评这些任务（默认评测表里全部）")
    ap.add_argument("--model-source", default=os.path.expanduser("~/.omlx/models"), help="模型目录（只读）")
    ap.add_argument("--ime-corpus", default=os.path.expanduser("~/Codes/on-device-llm/ime_eval/private/corpus.tsv"))
    ap.add_argument("--engine-app", default="/Applications/oMLX.app")
    ap.add_argument("--accessory", action="append", default=[], help="名字=公证过的 .app 路径（例如 luoke-mail=~/Codes/luoke/luoke-mail/build.noindex/螺壳邮件助手.app），可写多次；进清单供首次运行安装，并各出一个安装器 DMG")
    ap.add_argument("--github", default=None, help="<账号>/<仓库>：主控 DMG 和引擎包放它的 Releases，tag 就是发布号")
    ap.add_argument("--huggingface", default=None, help="Hugging Face 的组织或账号名（模型的第二地址；主控下载时测速选快的、断了换）")
    ap.add_argument("--modelscope", default=None, help="ModelScope 的组织或账号名（封闭测试用负责人的个人账号，公开版用组织；模型仓库名 = 模型目录名）")
    ap.add_argument("--url-base", default=None, help="测试托管：所有条目的地址前缀（例如 http://100.x.y.z:8765），模型在 <前缀>/models/<目录>/；盖过 --github / --modelscope")
    ap.add_argument("--prev", default=None, help="和哪个发布比（默认 releases/ 里最近的一个）")
    ap.add_argument("--identity", default="Developer ID Application")
    ap.add_argument("--installer-dmgs", action="store_true", help="另出按应用命名的安装器 DMG（10-26 起默认不出，只出螺壳一个）")
    ap.add_argument("--notarize", action="store_true", help="引擎包、主控 .app、DMG 都公证并 staple")
    ap.add_argument("--repo", action="append", default=[], help="配件标识=仓库路径：评测用这份代码（例如 git archive 出的冻结副本，带 .luoke-commit），不用评测表里 ../<仓库>；配件仓库还在往前走时用")
    ap.add_argument("--accept", action="store_true", help="看过差异报告后放行有回退的发布")
    ap.add_argument("--yes", action="store_true", help="不交互（连指纹确认也跳过：只给自动化测试用）")
    ap.add_argument("--keep-accessory-results", action="store_true", help="评测程序写进配件仓库的结果文件不删")
    a = ap.parse_args()
    a.repo_override = {}
    for spec in a.repo:
        if "=" not in spec:
            die(f"--repo 要写成 配件标识=路径：{spec}")
        name, path = spec.split("=", 1)
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            die(f"--repo {name} 的路径不存在：{path}")
        a.repo_override[name] = path
    if not re.fullmatch(r"[A-Za-z0-9._-]+", a.release):
        die("发布号只用字母、数字、点、横线")
    stages = [s.strip() for s in a.stage.split(",")]
    bad = [s for s in stages if s not in STAGES]
    if bad:
        die(f"没有阶段 {', '.join(bad)}；有：{', '.join(STAGES)}")
    paths = Paths(a.release)
    os.makedirs(paths.out, exist_ok=True)
    os.makedirs(paths.evidence, exist_ok=True)
    recipe = Recipe.load(a.template)
    for s in STAGES:
        if s not in stages:
            continue
        say(f"阶段 {s}")
        {"eval": lambda: stage_eval(a, paths, recipe), "diff": lambda: stage_diff(a, paths), "recipe": lambda: stage_recipe(a, paths),
         "engine": lambda: stage_engine(a, paths, recipe), "manifest": lambda: stage_manifest(a, paths),
         "build": lambda: stage_build(a, paths), "sign": lambda: stage_sign(a, paths), "dmg": lambda: stage_dmg(a, paths)}[s]()
    say("完成")


if __name__ == "__main__":
    main()
