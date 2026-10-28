"""本机探测：内存、芯片、Metal 上限、电源、热状态、内存样本、别的 AI 引擎。只读，不改任何系统设置。"""
from __future__ import annotations

import re
import subprocess
from typing import Optional

PAGE = 16384

# 实测的 macOS 默认 GPU 工作集上限（Apple max_recommended_working_set_size）占物理内存的比例：
# 16 GB → 11.8（0.74），32 GB → 25（0.78），128 GB → 107.5（0.84）。中间档按线性插值。
_METAL_RATIO = [(16, 0.74), (32, 0.78), (128, 0.84)]


def _run(cmd: list, timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def sysctl(key: str) -> str:
    return _run(["/usr/sbin/sysctl", "-n", key]).strip()


def mem_gb() -> float:
    try:
        return int(sysctl("hw.memsize")) / 2 ** 30
    except ValueError:
        return 0.0


def chip() -> str:
    return sysctl("machdep.cpu.brand_string")


def metal_cap_gb(total: Optional[float] = None) -> float:
    total = total or mem_gb()
    pts = _METAL_RATIO
    if total <= pts[0][0]:
        r = pts[0][1]
    elif total >= pts[-1][0]:
        r = pts[-1][1]
    else:
        for (a, ra), (b, rb) in zip(pts, pts[1:]):
            if a <= total <= b:
                r = ra + (rb - ra) * (total - a) / (b - a)
                break
    return round(total * r, 1)


def wired_limit_raised() -> bool:
    """别人调高了 iogpu.wired_limit_mb 吗（主控自己永远不调）。"""
    v = sysctl("iogpu.wired_limit_mb")
    return v.isdigit() and int(v) > 0


def on_ac() -> bool:
    return "AC Power" in _run(["/usr/bin/pmset", "-g", "batt"])


def low_power_mode() -> bool:
    out = _run(["/usr/bin/pmset", "-g"])
    m = re.search(r"^\s*(lowpowermode|powermode)\s+(\d+)", out, re.M)
    if not m:
        return False
    # lowpowermode 1 = 开；powermode 1 = 低功耗（2 是高功耗 / 自动）
    return (m.group(1) == "lowpowermode" and m.group(2) == "1") or (m.group(1) == "powermode" and m.group(2) == "1")


def thermal_level() -> str:
    """nominal / fair / serious / critical。pmset -g therm 只在有警告时给数字，没有就是 nominal。"""
    out = _run(["/usr/bin/pmset", "-g", "therm"])
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    if m:
        n = int(m.group(1))
        if n < 50:
            return "critical"
        if n < 80:
            return "serious"
        if n < 100:
            return "fair"
    if re.search(r"warning level.*?(\d)", out) and not re.search(r"No thermal warning", out):
        return "fair"
    return "nominal"


def pressure_level() -> str:
    """内核的内存压力等级：normal / warn / critical（kern.memorystatus_vm_pressure_level 1 / 2 / 4）。读不到当 normal。"""
    v = sysctl("kern.memorystatus_vm_pressure_level")
    return {"1": "normal", "2": "warn", "4": "critical"}.get(v, "normal")


def memory_sample() -> dict:
    """vm_stat 的 wired、swapouts、free 页数（换算 GB），以及 memory_pressure 的空闲百分比（只作参考）。"""
    rec = {"wired_gb": None, "swapouts_gb": None, "free_gb": None, "free_pct": None}
    vm = _run(["/usr/bin/vm_stat"])
    for line in vm.splitlines():
        k, _, v = line.partition(":")
        v = v.strip().rstrip(".")
        if not v.isdigit():
            continue
        if k.strip() == "Pages wired down":
            rec["wired_gb"] = round(int(v) * PAGE / 2 ** 30, 2)
        elif k.strip() == "Swapouts":
            rec["swapouts_gb"] = round(int(v) * PAGE / 2 ** 30, 2)
        elif k.strip() == "Pages free":
            rec["free_gb"] = round(int(v) * PAGE / 2 ** 30, 2)
    mp = _run(["/usr/bin/memory_pressure"])
    m = re.search(r"free percentage:\s*(\d+)%", mp)
    if m:
        rec["free_pct"] = int(m.group(1))
    return rec


_ENGINE_NAMES = ("omlx-server", "omlx", "ollama", "llama-server", "llama-cli", "lmstudio", "LM Studio", "mlx_lm.server",
                 "vllm", "koboldcpp", "text-generation")


def other_engines(exclude_pids: set) -> list:
    """进程列表里别的 AI 引擎（只看名字和 RSS，不读它们的文件）。用于"等内存"时的占用者名单。"""
    out = _run(["/bin/ps", "-axo", "pid=,rss=,comm="])
    found = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, rss, comm = parts
        if int(pid) in exclude_pids:
            continue
        base = comm.rsplit("/", 1)[-1]
        if any(n.lower() in base.lower() for n in _ENGINE_NAMES):
            found.append({"pid": int(pid), "name": base, "rss_gb": round(int(rss) / 2 ** 20, 1)})
    return found
