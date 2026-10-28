"""任务 × 档的支持台账：哪个任务在哪一档开着，规则只在这里算（10-19 定；顶层 ADR 0017 第 3 条：连过 3 遍才开，失败一遍就关）。

台账存在 releases/<发布号>/support.json：{任务: {档: {"state": "on"/"off", "why": 一句话, "runs": [这一版每一遍过没过],
"since": 从哪一版起是这个状态}}}。每一版由三样东西算出来：上一版的台账、这一版的评测结果（含复测）、评测表里的 hold。

一遍"过"= 硬性条目过，而且分项底线和上限都满足（judge 给的不是 fail）。规则：
- 这一版没评这一档（例如本机版只评 128）：沿用上一版的状态，不算回退；
- 上一版开着（且没有 hold）：这一版每一遍都过才继续开，失败一遍就关；
- 上一版关着、新任务、或评测表里 hold 着：这一版连过 RUNS_TO_OPEN 遍才开；第一遍过了就要复测补满。
发布脚本和配方都只读这份台账：配方里任务的 tiers 就是台账里开着的档。
"""
from __future__ import annotations

import json
import os
from typing import Optional

RUNS_TO_OPEN = 3


def decide(prev_state: Optional[str], held: bool, runs: Optional[list], prev_runs: Optional[list] = None) -> tuple:
    """一格的结论：(state, why)。prev_state 是上一版台账里的 on / off（新任务是 None）；runs 是这一版的结果，None 是这一版没评。
    prev_runs 是上一版这一格的结果（含它发布之后补跑的复测，10-20 定：复测挪到发布之后，够 3 遍在下一版打开）。"""
    was_on = prev_state == "on" and not held
    prev_full = (not held and prev_state != "on" and bool(prev_runs) and len(prev_runs) >= RUNS_TO_OPEN
                 and all(prev_runs[:RUNS_TO_OPEN]))
    if prev_full and (runs is None or (runs and all(runs))):
        return "on", (f"上一版发布后补满 {RUNS_TO_OPEN} 遍都过" + ("" if runs is None else "、这一版也过了") + "，开")
    if runs is None:          # 这一版没评这一档：沿用上一版；hold 着就关着（hold 的意思是"当作关着，连过 3 遍才开"）
        if held:
            return "off", "这一版没评这一档，hold 着"
        return (prev_state or "off"), ("这一版没评这一档，沿用上一版" if prev_state else "没评过")
    if not runs:
        return "off", "没有结果"
    if was_on:
        return ("on", "过了") if all(runs) else ("off", "失败一遍就关")
    if len(runs) >= RUNS_TO_OPEN and all(runs[:RUNS_TO_OPEN]):
        return "on", f"连过 {RUNS_TO_OPEN} 遍，开"
    if all(runs):
        return "off", f"过了 {len(runs)} 遍，要连过 {RUNS_TO_OPEN} 遍才开" + ("（hold 着）" if held else "")
    return "off", "没过" + ("（hold 着）" if held else "")


def needs_repeat(prev_state: Optional[str], held: bool, runs: list) -> int:
    """还要再跑几遍才能下结论（只有"关着、这一版遍遍都过、还没满 3 遍"要复测）。"""
    if held or prev_state == "on" or not runs or not all(runs):
        return 0              # hold 着的不复测（10-20：过了也开不了，白跑）
    return max(0, RUNS_TO_OPEN - len(runs))


def load(release_dir: str) -> Optional[dict]:
    p = os.path.join(release_dir, "support.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def from_recipe(recipe: dict) -> dict:
    """还没有台账的旧版本（internal-1018 以前）：按它的配方推出上一版的开关。"""
    assign = recipe.get("assignment") or {}
    out: dict = {}
    for task, t in (recipe.get("tasks") or {}).items():
        tiers = t.get("tiers")
        on = set(tiers) if tiers is not None else set(assign)
        out[task] = {tier: {"state": "on" if tier in on else "off", "why": "按配方推出", "runs": [], "since": recipe.get("release")}
                     for tier in assign}
    return out


def previous(prev_release: Optional[str], root: str) -> dict:
    if not prev_release:
        return {}
    led = load(os.path.join(root, "releases", prev_release))
    if led is not None:
        return led
    p = os.path.join(root, "recipes", f"{prev_release}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return from_recipe(json.load(f))
    return {}


def runs_by_cell(evidence: dict, passed) -> dict:
    """{(任务, 档): [这一版每一遍过没过]}。evidence 是 release.load_evidence 的结果；passed(record) 判一遍过没过；
    记录里的 repeat.hard_ok 是复测（含第一遍）。"""
    out: dict = {}
    for (task, _key, _tier), d in evidence.items():
        first = passed(d)
        rep = d.get("repeat") or {}
        runs = [bool(x) for x in rep.get("hard_ok") or []] or [first]
        runs[0] = first          # 第一遍以这次的判定为准（复测记录里的第一遍是同一遍）
        for tier in d["tiers"]:
            out[(task, str(tier))] = runs
    return out


def build(release: str, prev: dict, runs: dict, holds: dict, task_tiers: dict) -> dict:
    """这一版的台账。task_tiers：{任务: [配方模板里这个任务可能开的档]}；holds：{任务: {档: 理由}}。"""
    out: dict = {}
    for task, tiers in task_tiers.items():
        for tier in tiers:
            p = (prev.get(task) or {}).get(tier) or {}
            held = tier in (holds.get(task) or {})
            r = runs.get((task, tier))
            state, why = decide(p.get("state"), held, r, p.get("runs"))
            if held:
                why += f"；hold：{holds[task][tier]}"
            since = p.get("since") if p.get("state") == state else release
            out.setdefault(task, {})[tier] = {"state": state, "why": why, "runs": r or [], "since": since}
    return out


def flips(prev: dict, cur: dict) -> list:
    """开→关、关→开的格子：[(任务, 档, 上一版, 这一版, 理由)]。"""
    out = []
    for task, tiers in cur.items():
        for tier, c in tiers.items():
            was = ((prev.get(task) or {}).get(tier) or {}).get("state")
            if was and was != c["state"]:
                out.append((task, tier, was, c["state"], c["why"]))
    return sorted(out)


def on_tiers(ledger: dict, task: str) -> list:
    return sorted((t for t, c in (ledger.get(task) or {}).items() if c["state"] == "on"), key=int)


def record_repeats(release_dir: str, task: str, tier: str, runs: list) -> None:
    """发布之后补跑的复测记进这一版的台账（只改这一格的 runs，开关不动：已经发出去的版本照旧；下一版据此决定开不开）。"""
    led = load(release_dir)
    if led is None or tier not in (led.get(task) or {}):
        return
    led[task][tier]["runs"] = list(runs)
    led[task][tier]["repeated_after_publish"] = True
    save(release_dir, led)


def save(release_dir: str, ledger: dict) -> None:
    p = os.path.join(release_dir, "support.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, p)
