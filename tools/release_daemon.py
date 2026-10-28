#!/usr/bin/env python3
"""无人值守出版（2026-10-22 定）：出版机器（Mac Studio）上由 launchd 每 2 分钟跑一次 `tick`。

每次 tick：
  1. 上一版出完了（tools/internal_build.py 退出）：写流程报告（tools/release_report.py），在请求 issue 里回复结果——
     成功给发布页链接和摘要，失败写停在哪一步、日志末尾。进程没了又没留退出码（机器重启过）也照实回复。
  2. 读请求 issue 里新的留言：一行以 `/release` 开头（会话的留言前面可以有「（xxx 会话）」）就排进队列，回复"排上了"。
       /release internal-MMDD[-N]   出顶层仓库这个标签钉住的版（luoke-top 每天 10:00、21:00 钉版、打标签后发；10-23 定）
       /release            临时加一版：按各仓库 origin/main 的最新提交钉版，名字从 internal-MMDD-3 起顺延（早版、晚版占着 MMDD 和 -2）
       /release … --accept  差异报告里有总分回退时放行（只认负责人本人，不认会话）
     谁能发：实验室配置里 release.allowed 的 GitHub 账号。会话和负责人用的是同一个账号，靠留言开头的「（…会话）」区分。
     beta 只认负责人本人；正式版不归流水线。
  3. 设了 LUOKE_RELEASE_HOUR 时，每天那个钟点以后、当天还没定时出过：排一版（默认不设：定时由 luoke-top 的标签加 /release 管）。
  4. 没有正在出的版、队列不空：取一个，钉版（带标签的读顶层仓库的标签，不带的取 origin/main），起 internal_build.py（后台、新会话），
     记下进程号。上一版失败、钉住的提交没变时，新的请求接着那一版续跑（它会从停下的步骤接着做）。

配置：请求 issue 和账号在实验室配置里（tools/lab_config.py，不进公开的树）的 release 一节——issue（形如 <owner>/<repo>#<n>）、
allowed（能发 /release 的账号）、owner（负责人的账号：beta 和 --accept 只认本人的留言）。环境变量 LUOKE_RELEASE_ISSUE、
LUOKE_RELEASE_ALLOWED（逗号分隔）、LUOKE_RELEASE_OWNER 设了就用环境变量。其余是 launchd 的环境变量（模板在私有的 ops 仓库 lab/）：
  LUOKE_RELEASE_DRYRUN=1  空跑：只出不传（--no-publish），回复里写明
  LUOKE_RELEASE_HOUR      自己定时出版的钟点（默认不设，不自己定时）
  LUOKE_RELEASE_TOP       顶层仓库（默认 luokelabs/luoke）：/release <标签> 从它的标签读各子模块的提交
  以及 internal_build.py 认的签名凭据变量（LUOKE_SIGN_KEYCHAIN 等），原样传下去。

    python3 tools/release_daemon.py tick         launchd 跑这个
    python3 tools/release_daemon.py status       看队列和正在出的版
只用标准库。状态在 build.noindex/release-daemon.json，日志在 build.noindex/release-daemon.log。
"""
from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import lab_config  # noqa: E402  请求 issue 和账号（不进公开的树）
BUILD = os.path.join(ROOT, "build.noindex")
STATE = os.path.join(BUILD, "release-daemon.json")
LOGF = os.path.join(BUILD, "release-daemon.log")
SRC = os.path.join(BUILD, "src")
PY = "/usr/bin/python3"
REPOS = ["luoke-core", "luoke-mail", "luoke-mem", "luoke-ime"]      # 和 internal_build.py 的 ["luoke-core"] + ACCESSORIES 一致
TAG_PREFIX = "（出版流水线）"
COMMAND = re.compile(r"^\s*(?:（[^）]*会话）\s*)?/release\b(.*)$", re.M)
SESSION = re.compile(r"^\s*（[^）]*会话）")
TAG = re.compile(r"^internal-\d{4}(-\d+)?$")
BETA = re.compile(r"^beta\d+(\.\d+)?$")


def log(msg: str) -> None:
    os.makedirs(BUILD, exist_ok=True)
    with open(LOGF, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def load_state() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_comment": None, "scheduled": "", "queue": [], "running": None, "history": []}   # None：第一次跑，见 tick


def save_state(st: dict) -> None:
    os.makedirs(BUILD, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def sh(cmd: list, cwd: str = None, check: bool = True) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}…：{(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


# ---- issue ----------------------------------------------------------------------------------------------------

def issue() -> tuple:
    spec = os.environ.get("LUOKE_RELEASE_ISSUE") or lab_config.release().get("issue", "")
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", spec)
    if not m:
        raise RuntimeError(f"没有请求 issue：{lab_config.PATH} 的 release.issue（形如 <owner>/<repo>#<n>）")
    return m.group(1), int(m.group(2))


def comments_after(last: int) -> list:
    repo, n = issue()
    pages = json.loads(sh(["gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{n}/comments?per_page=100"]) or "[]")
    items = [c for page in pages for c in page]
    return [c for c in items if c["id"] > last]


def reply(body: str) -> None:
    repo, n = issue()
    sh(["gh", "issue", "comment", str(n), "--repo", repo, "--body", TAG_PREFIX + body], check=False)


def parse_request(c: dict, allowed: set, owner: str) -> dict:
    """一条留言 → 请求 {"accept": bool, "by": …, "comment": id}；不是请求返回 None；不允许返回 {"reject": 原因}。"""
    m = COMMAND.search(c.get("body") or "")
    if not m:
        return None
    who = (c.get("user") or {}).get("login", "")
    args = m.group(1).split()
    by_owner = who == owner and not SESSION.match(c.get("body") or "")
    if who not in allowed:
        return {"reject": f"@{who} 不在能发 /release 的名单里"}
    betas = [a for a in args if BETA.match(a)]
    if betas and not by_owner:
        return {"reject": "beta 只认负责人本人的留言（会话发的不算）"}
    if any(a.startswith("v") for a in args) or any(a.startswith("beta") and not BETA.match(a) for a in args):
        return {"reject": "正式版不归流水线；beta 写成 betaN 或 betaN.M"}
    accept = "--accept" in args
    if accept and not by_owner:
        return {"reject": "--accept 只认负责人本人的留言"}
    tags = [a for a in args if TAG.match(a) or BETA.match(a)]
    unknown = [a for a in args if a != "--accept" and a not in tags]
    if unknown or len(tags) > 1:
        return {"reject": "看不懂：" + " ".join(unknown or tags) + "（只认 /release、/release internal-MMDD[-N]、/release betaN[.M]，后面可以加 --accept）"}
    req = {"accept": accept, "by": who + ("" if by_owner or who != owner else "（会话）"), "comment": c["id"]}
    if tags:
        req["tag"] = tags[0]
    return req


# ---- 钉版、标签 --------------------------------------------------------------------------------------------------

def pins_now() -> dict:
    """各仓库 origin/main 的最新提交。luoke-core 用本仓库，配件用 build.noindex/src/<repo> 里的持久克隆。"""
    pins = {}
    for repo in REPOS:
        d = ROOT if repo == "luoke-core" else os.path.join(SRC, repo)
        sh(["git", "-C", d, "fetch", "-q", "origin", "main"])
        pins[repo] = sh(["git", "-C", d, "rev-parse", "origin/main"]).strip()
    return pins


def next_tag(day: str, history: list, first: int = 3) -> str:
    """临时加的一版：internal-MMDD-3 起顺延（看 releases/ 里的目录和出过的记录）。MMDD 和 -2 留给 luoke-top 早上、晚上钉的版。"""
    base = "internal-" + day
    used = {h.get("tag") for h in history}
    if os.path.isdir(os.path.join(ROOT, "releases")):
        used |= set(os.listdir(os.path.join(ROOT, "releases")))
    n = first
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def previous_beta(tag: str):
    """releases/ 里比 tag 早出的最近一个 beta（按 evidence 的时间）；没有就 None。
    只认有支持台账（support.json）的：beta0 是 10-16 的演练，那时还没有台账，拿它比，之后加的任务全成了"新任务"要连过 3 遍，
    差异也对不上；没有这样的 beta 就照默认比最近一版内部版。"""
    root = os.path.join(ROOT, "releases")
    names = [n for n in (os.listdir(root) if os.path.isdir(root) else []) if BETA.match(n) and n != tag
             and os.path.isdir(os.path.join(root, n, "evidence")) and os.path.exists(os.path.join(root, n, "support.json"))]
    names.sort(key=lambda n: os.path.getmtime(os.path.join(root, n, "evidence")))
    return names[-1] if names else None


def pins_from_top(tag: str) -> dict:
    """顶层仓库标签里各子模块的提交（经 GitHub，出版机器上没有顶层仓库的克隆）。只取流水线管的几个仓库。"""
    top = os.environ.get("LUOKE_RELEASE_TOP", "luokelabs/luoke")
    out = sh(["gh", "api", f"repos/{top}/git/trees/{tag}", "--jq", '.tree[] | select(.type == "commit") | .path + " " + .sha'])
    pins = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    missing = [r for r in REPOS if r not in pins]
    if missing:
        raise RuntimeError(f"顶层标签 {tag} 里没有 {', '.join(missing)}（标签打了吗？）")
    for repo in REPOS:      # 钉住的提交要在本机的克隆里：先 fetch
        sh(["git", "-C", ROOT if repo == "luoke-core" else os.path.join(SRC, repo), "fetch", "-q", "origin"])
    return {r: pins[r].strip() for r in REPOS}


def start_build(req: dict, st: dict) -> None:
    pins = pins_from_top(req["tag"]) if req.get("tag") else pins_now()
    last = (st.get("history") or [None])[-1]
    if req.get("tag"):
        tag = req["tag"]
        done = any(h.get("tag") == tag and h.get("ok") for h in st.get("history", []))
        if done:
            raise RuntimeError(f"{tag} 已经出过了；要再出一版用 /release（会顺延成 -3 起）")
        resume = os.path.isdir(os.path.join(ROOT, "releases", tag))     # 出过一半：internal_build 按 steps.json 接着做
    else:
        resume = bool(last and not last.get("ok") and last.get("pins") == pins and last.get("tag", "").startswith("internal-" + time.strftime("%m%d")))
        tag = last["tag"] if resume else next_tag(time.strftime("%m%d"), st.get("history", []))
    # 出版用的是本仓库的专用克隆：工作区放到钉住的提交上（internal_build 的 freeze 要求正好在那个提交、没有改动）
    sh(["git", "-C", ROOT, "checkout", "-q", "--detach", pins["luoke-core"]])
    cmd = [PY, "-B", "tools/internal_build.py", "--tag", tag, "--build-mail"] + [f"--pin={k}={v}" for k, v in pins.items()]
    if BETA.match(tag):
        # beta：一律全评（internal_build 对非内部版不走金丝雀）；差异对比拿上一个有台账的 beta（beta1.1 比 beta1），没有就照默认拿最近一版
        prev_beta = previous_beta(tag)
        if prev_beta:
            cmd += ["--prev", prev_beta]
    if req.get("accept"):
        cmd.append("--accept")
    if os.environ.get("LUOKE_RELEASE_DRYRUN") == "1":
        cmd.append("--no-publish")
    out = os.path.join(BUILD, f"daemon-{tag}.out")
    rc = os.path.join(BUILD, f"daemon-{tag}.rc")
    if os.path.exists(rc):
        os.remove(rc)
    script = " ".join(_q(x) for x in cmd) + f" > {_q(out)} 2>&1; echo $? > {_q(rc)}"
    p = subprocess.Popen(["/bin/sh", "-c", script], cwd=ROOT, start_new_session=True, env=dict(os.environ, LC_ALL="en_US.UTF-8"),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    st["running"] = {"tag": tag, "pid": p.pid, "rc": rc, "pins": pins, "req": req, "started": time.time(), "resume": resume}
    log(f"开始 {tag}（{'续跑' if resume else '新的一版'}，{req.get('by', '定时')}）pid {p.pid}")
    reply(f"开始出 **{tag}**" + ("（接着上次停下的地方）" if resume else "") + "：" +
          "、".join(f"{k} {v[:7]}" for k, v in pins.items()) + ("。空跑：只出不传。" if os.environ.get("LUOKE_RELEASE_DRYRUN") == "1" else "。"))


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def in_repeats(tag: str) -> bool:
    """这一版正在做发布后的复测吗（timeline.json 最后一步是 repeats、还没结束）：版已经发了，复测下一版会接着补"""
    try:
        tl = json.load(open(os.path.join(ROOT, "releases", tag, "timeline.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return False
    ph = tl.get("phases") or []
    return bool(ph) and ph[-1].get("name") == "repeats" and ph[-1].get("end") is None


def preempt_repeats(st: dict) -> None:
    """有人在排队、正在出的版只剩发布后的复测：打断它（10-23），先出新的一版"""
    run = st.get("running")
    if not run or not st.get("queue") or run.get("preempted") or not in_repeats(run["tag"]):
        return
    try:
        os.killpg(run["pid"], 15)        # start_new_session：进程组号就是 pid，连远端评测的 ssh 一起停
    except OSError:
        pass
    run["preempted"] = True
    log(f"打断 {run['tag']} 发布后的复测：有新的请求在排队")


def finish_build(st: dict) -> None:
    """正在出的版结束了吗：结束了写报告、回复结果、记进历史。"""
    run = st.get("running")
    if not run:
        return
    rc = None
    if os.path.exists(run["rc"]):
        try:
            rc = int(open(run["rc"]).read().strip() or 1)
        except ValueError:
            rc = 1
    elif alive(run["pid"]):
        return
    tag = run["tag"]
    if run.get("preempted"):
        rc = 0      # 版已经发了，只是复测被打断：算成功，复测下一版接着补
    hist = st.setdefault("history", [])
    prev = next((h["tag"] for h in reversed(hist) if h.get("ok")), None)
    subprocess.run([PY, "-B", "tools/release_report.py", tag] + (["--prev", prev] if prev else []), cwd=ROOT, capture_output=True)
    summ = subprocess.run([PY, "-B", "tools/release_report.py", tag, "--summary"] + (["--prev", prev] if prev else []),
                          cwd=ROOT, capture_output=True, text=True).stdout.strip()
    ok = rc == 0
    if ok:
        url = ""
        if os.environ.get("LUOKE_RELEASE_DRYRUN") != "1":
            repo = "luokelabs/luoke-releases" if BETA.match(tag) else "luokelabs/luoke-internal-builds"     # luoke-core#36
            url = subprocess.run(["gh", "release", "view", tag, "--repo", repo, "--json", "url", "-q", ".url"],
                                 capture_output=True, text=True).stdout.strip()
        reply(f"**{tag}** 出好了" + (f"：{url}" if url else "（空跑，没上传）") +
              ("（发布后的复测被新的请求打断，下一版接着补）" if run.get("preempted") else "") + "\n\n" + summ +
              f"\n\n完整的流程报告在 luoke-core 的 `releases/{tag}/report.md`。")
    else:
        logp = os.path.join(BUILD, f"internal-{tag}.log")
        tail = ""
        try:
            tail = "".join(open(logp, encoding="utf-8").readlines()[-30:])
        except OSError:
            pass
        why = "进程没了，也没留下退出码（机器重启或被结束过）" if rc is None else f"退出码 {rc}"
        reply(f"**{tag}** 没出成（{why}）。\n\n{summ}\n\n日志末尾：\n```\n{tail[-3500:]}\n```\n"
              "修好原因后再发一次 `/release`，钉住的提交没变就从停下的步骤接着做；差异报告里有总分回退的，负责人看过报告后发 `/release --accept`。")
    hist.append({"tag": tag, "ok": ok, "rc": rc, "pins": run["pins"], "started": run["started"], "ended": time.time()})
    st["history"] = hist[-50:]
    st["running"] = None
    log(f"结束 {tag}：{'成功' if ok else f'失败 rc={rc}'}")


# ---- tick ------------------------------------------------------------------------------------------------------

MAX_REQUEST_AGE_HOURS = 6       # 守护进程停过一阵：比这更早的请求不补跑


def request_age_hours(c: dict):
    """留言发出到现在多少小时；created_at 读不出就是 None（照常处理）。"""
    try:
        t = calendar.timegm(time.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
    except (KeyError, TypeError, ValueError):
        return None
    return (time.time() - t) / 3600


def tick() -> None:
    st = load_state()
    preempt_repeats(st)
    finish_build(st)
    save_state(st)
    lab = lab_config.release()
    allowed = {x.strip() for x in os.environ["LUOKE_RELEASE_ALLOWED"].split(",") if x.strip()} if os.environ.get("LUOKE_RELEASE_ALLOWED") \
        else set(lab.get("allowed") or [])
    owner = os.environ.get("LUOKE_RELEASE_OWNER") or lab.get("owner") or ""
    first = st.get("last_comment") is None
    try:
        new = comments_after(st.get("last_comment") or 0)
    except RuntimeError as e:
        log(f"读请求 issue 失败：{e}")
        new = []
    if first and new is not None:
        # 第一次跑（刚装上、状态文件没了）：issue 里已有的留言只记下、不补跑（10-23：装守护进程之前发的 /release 不该被当成新请求）
        st["last_comment"] = max((c["id"] for c in new), default=0)
        log(f"第一次跑：已有 {len(new)} 条留言记为看过，不处理")
        new = []
    for c in new:
        st["last_comment"] = max(st.get("last_comment") or 0, c["id"])
        if (c.get("body") or "").startswith(TAG_PREFIX):
            continue                    # 自己的回复
        req = parse_request(c, allowed, owner)
        if req is None:
            continue
        if "reject" in req:
            reply(f"没排上：{req['reject']}。")
            continue
        age = request_age_hours(c)
        if age is not None and age > MAX_REQUEST_AGE_HOURS:
            reply(f"这条是 {int(age)} 小时前的，守护进程当时没在跑，不补跑；要出版请再发一次 /release。")
            continue
        st.setdefault("queue", []).append(req)
        ahead = len(st["queue"]) - 1 + (1 if st.get("running") else 0)
        reply(f"排上了（{req['by']}）" + (f"，前面还有 {ahead} 版" if ahead else "，马上开始") + "。")
    today = time.strftime("%Y-%m-%d")
    hour = os.environ.get("LUOKE_RELEASE_HOUR", "").strip()
    if hour and int(time.strftime("%H")) >= int(hour) and st.get("scheduled") != today:
        st["scheduled"] = today
        st.setdefault("queue", []).append({"accept": False, "by": f"每天 {hour}:00 定时"})
        log("定时：排上今天的版")
    if not st.get("running") and st.get("queue"):
        req = st["queue"].pop(0)
        try:
            start_build(req, st)
        except RuntimeError as e:
            log(f"没能开始：{e}")
            reply(f"没能开始出版：{e}")
    save_state(st)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        tick()
    elif cmd == "status":
        print(json.dumps(load_state(), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
        sys.exit(64)


if __name__ == "__main__":
    main()
