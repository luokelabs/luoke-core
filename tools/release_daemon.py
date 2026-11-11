#!/usr/bin/env python3
"""无人值守出版（2026-10-22 定）：出版机器（Mac Studio）上由 launchd 每 2 分钟跑一次 `tick`。

每次 tick：
  1. 上一版出完了（tools/internal_build.py 退出）：写流程报告（tools/release_report.py），在请求 issue 里回复结果——
     成功给发布页链接和摘要，失败写停在哪一步、日志末尾。进程没了又没留退出码（机器重启过）也照实回复。
  2. 读请求 issue 里新的留言：一行以 `/release` 开头（会话的留言前面可以有「（xxx 会话）」）就排进队列，回复"排上了"。
       /release internal-MMDD[-N]   出顶层仓库这个标签钉住的版（luoke-top 每天 10:00、21:00 钉版、打标签后发；10-23 定）
       /release            临时加一版：按各仓库 origin/main 的最新提交钉版（扫描钉 main 上最近的、发了 release 的 tag），名字从 internal-MMDD-3 起顺延（早版、晚版占着 MMDD 和 -2）
       /release … --accept  差异报告里有总分回退时放行（只认负责人本人，不认会话）
       /release … --no-eval 不评测：整版沿用上一版真评过的证据和开关，不比差异、不做发布后复测，发布说明写明（只给内部版）
       /release betaN[.M]-rcM       出候选版：钉四个公开仓库 main 现在的提交（导出、推送做完以后），全评、打包、签名，不上传，
                                    拷到局域网托管的目录，回复里给测试机装的地址
     谁能发：实验室配置里 release.allowed 的 GitHub 账号。会话、定时截版和负责人用的是同一个账号，靠留言开头的「（…会话）」「（…定时截版）」区分。
     beta 只认负责人本人；候选版认负责人本人和 release.rc_senders 里的账号（机器账号；不在 allowed 里的只能发候选版）；正式版不归流水线。
  3. 设了 LUOKE_RELEASE_HOUR 时，每天那个钟点以后、当天还没定时出过：排一版（默认不设：定时由 luoke-top 的标签加 /release 管）。
  4. 没有正在出的版、队列不空：取一个，钉版（带标签的读顶层仓库的标签，不带的取 origin/main），起 internal_build.py（后台、新会话），
     记下进程号。上一版失败、钉住的提交没变时，新的请求接着那一版续跑（它会从停下的步骤接着做）。

配置：请求 issue 和账号在实验室配置里（tools/lab_config.py，不进公开的树）的 release 一节——issue（形如 <owner>/<repo>#<n>）、
allowed（能发 /release 的账号）、owner（负责人的账号：beta 和 --accept 只认本人的留言）。环境变量 LUOKE_RELEASE_ISSUE、
LUOKE_RELEASE_ALLOWED（逗号分隔）、LUOKE_RELEASE_OWNER 设了就用环境变量。候选版另有三项，只在实验室配置里：rc_senders（能发
候选版的机器账号，不在 allowed 里的只能发候选版）、rc_url_base（局域网托管的地址前缀，写局域网 IP：Tailscale 的 100.x 地址写进
清单会被扫包拦下）、rc_serve_dir（那个地址对应的本机目录，由常驻的静态文件服务挂出去，流水线只往里拷）。其余是 launchd 的环境变量
（模板在私有的 ops 仓库 lab/）：
  LUOKE_RELEASE_DRYRUN=1  空跑：只出不传（--no-publish），回复里写明
  LUOKE_RELEASE_HOUR      自己定时出版的钟点（默认不设，不自己定时）
  LUOKE_RELEASE_TOP       顶层仓库（默认 luokelabs/luoke）：/release <标签> 从它的标签读各子模块的提交
  LUOKE_BUILD_ROOT        出版克隆（luoke-core 的一份克隆，每版切到钉住的提交）。设了的话，这个脚本所在的仓库就是只跟 main 的
                          工具目录：没在出版时每轮取一次 origin/main 并切过去，下一轮生效。不设就是同一份（以前的装法，不跟 main）
  以及 internal_build.py 认的签名凭据变量（LUOKE_SIGN_KEYCHAIN 等），原样传下去。

    python3 tools/release_daemon.py tick         launchd 跑这个
    python3 tools/release_daemon.py status       看队列和正在出的版
只用标准库。状态在出版克隆的 build.noindex/release-daemon.json，日志在 build.noindex/release-daemon.log。
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # 流水线自己的代码
sys.path.insert(0, os.path.join(ROOT, "tools"))
import lab_config  # noqa: E402  请求 issue 和账号（不进公开的树）
# 出版克隆：每出一版切到钉住的提交，internal_build、流程报告在这里跑，状态、日志、releases/ 也在这里。
# 流水线自己从另一份只跟 main 的工具目录跑，LUOKE_BUILD_ROOT 指向出版克隆（11-02 定）：
# 不然出一次钉旧提交的版（beta 候选版），之后每一轮跑的都是旧代码。没设就是同一份（以前的装法）
CORE = os.path.abspath(os.path.expanduser(os.environ.get("LUOKE_BUILD_ROOT") or ROOT))
BUILD = os.path.join(CORE, "build.noindex")
STATE = os.path.join(BUILD, "release-daemon.json")
LOGF = os.path.join(BUILD, "release-daemon.log")
SRC = os.path.join(BUILD, "src")
TOP = os.path.dirname(CORE)     # 配件的私有仓库克隆在出版克隆旁边（TOP/<repo>，连 GitHub）；SRC 里的持久克隆是从它们 clone 的
PY = "/usr/bin/python3"
REPOS = ["luoke-core", "luoke-mail", "luoke-mem", "luoke-ime"]      # 必须钉的；和 internal_build.py 的 ACCESSORIES 里不在 OPTIONAL 的一致
# 新进流水线的配件（beta 2，11-03）：钉得到就钉，钉不到这一版不带（internal_build 也这样办）。持久克隆由 internal_build 的 freeze 建，
# 这里不取。和 internal_build.py 的 OPTIONAL、TOP_ALIAS 一致
OPTIONAL = ["luoke-search", "luoke-scan", "luoke-agent"]
# 私有仓库带 -private 后缀；地址按名字拼（代码要公开，不写私有仓库的地址）。和 internal_build.py 的 GITHUB_ORG、REPO_SUFFIX 一致
GITHUB_ORG = "luokelabs"
REPO_SUFFIX = "-private"
TOP_ALIAS = {"luoke-audit": "luoke-scan"}      # 顶层子模块还叫 luoke-audit（仓库 11-03 改名 luoke-scan）
# 只收 release tag 的配件（internal_build.build_scan）：钉 main 上最近的、发了 release 的 v<版本> tag，不钉 main 的最新提交。
# main 常常比最新的 tag 新（合完 PR 过一阵才打 tag）：钉 main 就编不过，退回旧版（11-09 internal-1109-6）
TAG_PINNED = ["luoke-scan"]
TAG_PREFIX = "（出版流水线）"
# 会话和 luoke-top 的定时截版（10-29 起在出版机上定时跑）用的都是负责人的 gh 账号，靠留言开头的「（…会话）」
# 「（…定时截版）」和负责人本人区分：这两种都不能出 beta、不能 --accept。哪一行以它开头都算（定时截版是开头一行标签、
# /release 在第二行；开头先写了别的、会话标签写在后面也一样）
COMMAND = re.compile(r"^\s*(?:（[^）]*(?:会话|定时截版)）\s*)?/release\b(.*)$", re.M)
SESSION = re.compile(r"^\s*（[^）]*(?:会话|定时截版)）", re.M)
TAG = re.compile(r"^internal-\d{4}(-\d+)?$")
BETA = re.compile(r"^beta\d+(\.\d+)?$")
RC = re.compile(r"^(beta\d+(?:\.\d+)?)-rc\d+$")      # 候选版：第 1 组是它要出成的 beta


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


def parse_request(c: dict, allowed: set, owner: str, rc_senders: set = frozenset()) -> dict:
    """一条留言 → 请求 {"accept": bool, "by": …, "comment": id}；不是请求返回 None；不允许返回 {"reject": 原因}。
    候选版 /release betaN-rcM 认负责人本人和 rc_senders 里的账号（机器账号）；只在 rc_senders、不在 allowed 里的只能发候选版。"""
    m = COMMAND.search(c.get("body") or "")
    if not m:
        return None
    who = (c.get("user") or {}).get("login", "")
    args = m.group(1).split()
    by_owner = who == owner and not SESSION.search(c.get("body") or "")
    if who not in allowed and who not in rc_senders:
        return {"reject": f"@{who} 不在能发 /release 的名单里"}
    rcs = [a for a in args if RC.match(a)]
    if rcs and not (by_owner or who in rc_senders):
        return {"reject": "候选版只认负责人本人和出候选版的机器账号"}
    if rcs and NOT_FROM_PUBLIC.match(RC.match(rcs[0]).group(1)):
        return {"reject": "beta1.x 不做公开同步，它的候选版钉不了公开提交，不走流水线"}
    if not rcs and who not in allowed:
        return {"reject": f"@{who} 只能发候选版 /release betaN-rcM"}
    betas = [a for a in args if BETA.match(a)]
    if betas and not by_owner:
        return {"reject": "beta 只认负责人本人的留言（会话发的不算）"}
    if any(a.startswith("v") for a in args) or any(a.startswith("beta") and not BETA.match(a) and not RC.match(a) for a in args):
        return {"reject": "正式版不归流水线；beta 写成 betaN 或 betaN.M，候选版写成 betaN-rcM"}
    accept = "--accept" in args
    if accept and not by_owner:
        return {"reject": "--accept 只认负责人本人的留言"}
    no_eval = "--no-eval" in args
    if no_eval and (rcs or betas):
        return {"reject": "--no-eval 只给内部版：beta 和候选版一律全评"}
    tags = [a for a in args if TAG.match(a) or BETA.match(a) or RC.match(a)]
    unknown = [a for a in args if a not in ("--accept", "--no-eval") and a not in tags]
    if unknown or len(tags) > 1:
        return {"reject": "看不懂：" + " ".join(unknown or tags) + "（只认 /release、/release internal-MMDD[-N]、/release betaN[.M]、"
                          "/release betaN[.M]-rcM，后面可以加 --accept；内部版还可以加 --no-eval）"}
    req = {"accept": accept, "by": who + ("" if by_owner or who != owner else "（会话）"), "comment": c["id"]}
    if no_eval:
        req["no_eval"] = True
    if tags:
        req["tag"] = tags[0]
    return req


# ---- 钉版、标签 --------------------------------------------------------------------------------------------------

def pins_now() -> dict:
    """各仓库 origin/main 的最新提交。luoke-core 用出版克隆，配件用 build.noindex/src/<repo> 里的持久克隆。"""
    pins = {}
    for repo in REPOS:
        d = CORE if repo == "luoke-core" else os.path.join(SRC, repo)
        sh(["git", "-C", d, "fetch", "-q", "origin", "main"])
        pins[repo] = sh(["git", "-C", d, "rev-parse", "origin/main"]).strip()
    for repo in OPTIONAL:
        try:
            if repo in TAG_PINNED:
                pins[repo] = latest_tag_on_main(repo)
            else:
                pins[repo] = sh(["gh", "api", f"repos/{GITHUB_ORG}/{repo}{REPO_SUFFIX}/commits/main", "--jq", ".sha"]).strip()
        except Exception as e:  # noqa: BLE001  取不到就这一版不带
            # 扫描已经收进主控：不带就等于用户升上来没有扫描。取不到 tag（接口出错、gh 不在）时钉最近发布那版内部版的
            # （和 internal_build.check_bundled 退回同一个口径），那也没有才不带（#151 审查第 3 条）
            old = published_pin(repo) if repo in TAG_PINNED else ""
            if old:
                pins[repo] = old
                log(f"{repo}：取不到发了 release 的 tag（{e}），钉最近发布那版内部版的 {old[:7]}")
            elif repo in TAG_PINNED:
                log(f"{repo}：取不到发了 release 的 tag（{e}），最近发布的内部版也没有，这一版不带")
            else:
                log(f"{repo}：取不到 main（{e}），这一版不带")
    return pins


def published_pin(repo: str) -> str:
    """出版克隆的 releases/ 里，最近发布那版内部版钉的 repo 的提交（internal_build.last_published_pin）；出错就空。"""
    try:
        import internal_build
        return internal_build.last_published_pin(repo, "", root=CORE)
    except Exception as e:  # noqa: BLE001
        log(f"{repo}：找最近发布那版内部版钉的提交出错（{e}）")
        return ""


def latest_tag_on_main(repo: str) -> str:
    """main 上（main 已经包含的）、发了 GitHub release 的、版本号最大的 v<版本> tag 指向的提交。tag 打在没合进 main 的提交上的跳过。
    扫描的 release 要测试、打包都过了才发（比 tag 晚 3–8 分钟），测试没过的 tag 一直没有 release：11-09 的 v0.3.15 就是，
    到 v0.3.16 接上之前的 8 分钟里钉到它就编出没过测试的扫描。草稿、预发布（prerelease）不算。"""
    full = f"repos/{GITHUB_ORG}/{repo}{REPO_SUFFIX}"
    released = set(sh(["gh", "api", "--paginate", f"{full}/releases", "--jq", '.[] | select((.draft or .prerelease) | not) | .tag_name']).split())
    tags = []
    for line in sh(["gh", "api", "--paginate", f"{full}/tags", "--jq", '.[] | [.name, .commit.sha] | @tsv']).splitlines():
        name, _, sha = line.partition("\t")
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", name.strip())
        if m and sha.strip() and name.strip() in released:
            tags.append((tuple(int(x) for x in m.groups()), sha.strip()))
    for _v, sha in sorted(tags, reverse=True)[:5]:
        if sh(["gh", "api", f"{full}/compare/{sha}...main", "--jq", ".status"]).strip() in ("ahead", "identical"):
            return sha
    raise RuntimeError("main 上没有发了 release 的 v<版本> tag")


def next_tag(day: str, history: list, first: int = 3) -> str:
    """临时加的一版：internal-MMDD-3 起顺延（看 releases/ 里的目录和出过的记录）。MMDD 和 -2 留给 luoke-top 早上、晚上钉的版。"""
    base = "internal-" + day
    used = {h.get("tag") for h in history}
    if os.path.isdir(os.path.join(CORE, "releases")):
        used |= set(os.listdir(os.path.join(CORE, "releases")))
    n = first
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def previous_beta(tag: str):
    """releases/ 里比 tag 早出的最近一个 beta（按 evidence 的时间）；没有就 None。
    只认有支持台账（support.json）的：beta0 是 10-16 的演练，那时还没有台账，拿它比，之后加的任务全成了"新任务"要连过 3 遍，
    差异也对不上；没有这样的 beta 就照默认比最近一版内部版。"""
    root = os.path.join(CORE, "releases")
    names = [n for n in (os.listdir(root) if os.path.isdir(root) else []) if BETA.match(n) and n != tag
             and os.path.isdir(os.path.join(root, n, "evidence")) and os.path.exists(os.path.join(root, n, "support.json"))]
    names.sort(key=lambda n: os.path.getmtime(os.path.join(root, n, "evidence")))
    return names[-1] if names else None


def pins_from_top(tag: str) -> dict:
    """顶层仓库标签里各子模块的提交（经 GitHub，出版机器上没有顶层仓库的克隆）。只取流水线管的几个仓库。"""
    top = os.environ.get("LUOKE_RELEASE_TOP", "luokelabs/luoke")
    out = sh(["gh", "api", f"repos/{top}/git/trees/{tag}", "--jq", '.tree[] | select(.type == "commit") | .path + " " + .sha'])
    pins = dict(line.split(" ", 1) for line in out.splitlines() if " " in line)
    pins = {TOP_ALIAS.get(k, k): v for k, v in pins.items()}
    missing = [r for r in REPOS if r not in pins]
    if missing:
        raise RuntimeError(f"顶层标签 {tag} 里没有 {', '.join(missing)}（标签打了吗？）")
    for repo in REPOS:      # 钉住的提交要在本机的克隆里：先 fetch
        sh(["git", "-C", CORE if repo == "luoke-core" else os.path.join(SRC, repo), "fetch", "-q", "origin"])
    return {r: pins[r].strip() for r in list(REPOS) + list(OPTIONAL) if r in pins}


# 公开版只用公开仓库里有的提交（顶层 ADR 0017 10-29 修订）：beta 和正式版钉的四个提交都要在公开仓库的 main 上，
# 发布说明里的提交号外面的人才查得到。beta1、beta1.1 是这条之前定的，照旧从私有 beta 出（10-29 定的例外）；
# beta1.x 的补丁版也一样（那条线接不上公开仓库，11-02 定），公开从 beta2 开始
PUBLIC_OWNER = os.environ.get("LUOKE_PUBLIC_OWNER", "luokelabs")
NOT_FROM_PUBLIC = re.compile(r"^beta1(\.\d+)?$")


def not_on_public(pins: dict) -> list:
    """钉住的提交里不在公开仓库 main 上的：[说明]。经 GitHub 的 compare（base=钉住的提交，head=main）：
    identical 或 ahead 说明 main 包含它；behind、diverged 或公开仓库里根本没有这个提交都不行。"""
    bad = []
    for repo, sha in pins.items():
        r = subprocess.run(["gh", "api", f"repos/{PUBLIC_OWNER}/{repo}/compare/{sha}...main", "--jq", ".status"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        status = r.stdout.strip()
        if r.returncode != 0 and "(HTTP 404)" in r.stderr:      # 别的错误里也可能有 404（提交号、地址），只认 gh 的这一句
            bad.append(f"{repo} {sha[:7]}：公开仓库里没有这个提交")
        elif r.returncode != 0:         # 网络、登录这类：查不了，照样不放行，但别说成没有这个提交
            bad.append(f"{repo} {sha[:7]}：查不了（{r.stderr.strip()[-200:]}）")
        elif status not in ("identical", "ahead"):
            bad.append(f"{repo} {sha[:7]}：不在公开 main 上（{status}）")
    return bad


# ---- 候选版 ----------------------------------------------------------------------------------------------------

def pins_public() -> dict:
    """候选版钉的：四个公开仓库 main 现在的提交（导出、推送做完以后就是这一版要公开的）。同样的提交也在私有仓库的 public
    分支上，从 GitHub 取进本机连 GitHub 的克隆：core 是出版克隆，配件是 TOP/<repo>（SRC 里的持久克隆是从它 clone 的、
    只看得到它的本地分支，取不到 public；internal_build 的 freeze 再按提交号从 TOP/<repo> 取过去）。
    取下来还没有，说明私有 public 没推到这个提交（推送做了一半），停。"""
    pins = {}
    for repo in REPOS:
        sha = sh(["gh", "api", f"repos/{PUBLIC_OWNER}/{repo}/commits/main", "--jq", ".sha"]).strip()
        d = CORE if repo == "luoke-core" else os.path.join(TOP, repo)
        sh(["git", "-C", d, "fetch", "-q", "origin", "public"])
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or \
                subprocess.run(["git", "-C", d, "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode != 0:
            raise RuntimeError(f"{repo} 公开 main 是 {sha[:7] or '？'}，私有 public 上还没有（推送没做完？）")
        pins[repo] = sha
    # 新配件（beta 2）照另外四个钉公开 main（不走私有提交的例外）。还没有公开仓库、或者 main 还是空的，
    # 这一版不带它；钉住的提交在不在私有 public 上由 internal_build 的 freeze 按提交号取时核（取不到就不带）
    # 原名在公开仓库建好以前会跳到改了名的私有仓库（GitHub 的改名跳转）：只认真的公开、名字没跳过的
    for repo in OPTIONAL:
        meta = sh(["gh", "api", f"repos/{PUBLIC_OWNER}/{repo}", "--jq", '.full_name + " " + (.private | tostring)'], check=False).strip()
        if meta != f"{PUBLIC_OWNER}/{repo} false":
            log(f"{repo}：还没有公开仓库（{meta or '查不到'}），这一版不带")
            continue
        sha = sh(["gh", "api", f"repos/{PUBLIC_OWNER}/{repo}/commits/main", "--jq", ".sha"], check=False).strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            pins[repo] = sha
        else:
            log(f"{repo}：公开仓库的 main 还是空的，这一版不带")
    return pins


def candidate_ready(core_pin: str) -> None:
    """候选版跑的是钉住的公开 core 里的 internal_build.py（出版克隆切到那个提交再跑）：那一版得认得候选版（全评、全部现打、
    带 --url-base 时不做发布后的复测，和这里的候选版一起改的），不然它会拿 beta 的证据、复测几个小时，或者根本不认 --url-base。
    还不认就停：先把带候选版的 core 导出、推到公开仓库。"""
    src = sh(["git", "-C", CORE, "show", f"{core_pin}:tools/internal_build.py"], check=False)
    if "(-rc\\d+)?" not in src:
        raise RuntimeError(f"公开 core {core_pin[:7]} 的 internal_build.py 还不认候选版：先把带候选版的 luoke-core 导出、推到公开仓库")


def rc_hosting() -> tuple:
    """候选版挂到哪：(局域网地址前缀, 那个地址对应的本机目录)，实验室配置 release 里的 rc_url_base、rc_serve_dir。"""
    lab = lab_config.release()
    base, d = (lab.get("rc_url_base") or "").rstrip("/"), lab.get("rc_serve_dir") or ""
    if not base or not d:
        raise RuntimeError(f"{lab_config.PATH} 的 release 里没有 rc_url_base、rc_serve_dir：候选版不知道挂到哪")
    return base, os.path.expanduser(d)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def host_candidate(tag: str) -> str:
    """候选版出好了：清单列的文件（配件包、引擎包、主控 DMG、安装器）和 release.json 拷到 <rc_serve_dir>/<tag>/，逐个核对哈希
    （和 internal_build 上传时挑的文件一样）。清单里的地址本来就是 <rc_url_base>/<tag>/<文件名>（--url-base）。返回主控 DMG 的地址。"""
    base, serve = rc_hosting()
    out = os.path.join(BUILD, "release", tag)
    with open(os.path.join(out, "release.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    files, dmg = [], None
    for item in manifest["items"].values():
        sub = {"accessory": "accessories", "engine": "engine", "hub": ""}.get(item.get("kind"))
        if sub is None:
            continue                    # 模型照旧从 ModelScope / Hugging Face 下
        for f in item["files"]:
            files.append((os.path.join(out, sub, f["path"]), f["sha256"]))
            if item["kind"] == "hub":
                dmg = os.path.basename(f["path"])
    files += [(os.path.join(out, ins["file"]), ins["sha256"]) for ins in (manifest.get("installers") or {}).values()]
    dest = os.path.join(serve, tag)
    tmp, old = dest + ".partial", dest + ".old"
    for d in (tmp, old):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(tmp)
    try:
        for p, want in files:
            if not os.path.exists(p) or _sha256(p) != want:
                raise RuntimeError(f"{os.path.relpath(p, out)} 不在或哈希和清单不一致")
            shutil.copy2(p, os.path.join(tmp, os.path.basename(p)))
        shutil.copy2(os.path.join(out, "release.json"), tmp)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)      # 没拷完的几个 GB 别留在挂出去的目录里；挂着的不动
        raise
    if os.path.exists(dest):
        os.replace(dest, old)                       # 同一个候选版重挂：整个换掉，别留上一次的文件（先挪开再换，中间不缺目录）
    os.replace(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)
    return f"{base}/{tag}/" + (urllib.parse.quote(dmg) if dmg else "")


def start_build(req: dict, st: dict) -> None:
    cand = RC.match(req.get("tag") or "")
    if cand:
        base, serve = rc_hosting()      # 先看挂得到哪，没配就别白出一版
        if os.path.isdir(os.path.join(serve, req["tag"])):
            # 出过的记录只留最近 50 条：挂着的目录也算出过，不然同一个号会钉到新的提交、换掉测试机正在装的文件
            raise RuntimeError(f"{req['tag']} 已经挂在局域网上了；要再出一个候选版就把 rc 后面的号加一")
        url_base = base + "/" + req["tag"]
        pins = pins_public()
        candidate_ready(pins["luoke-core"])
    else:
        pins = pins_from_top(req["tag"]) if req.get("tag") else pins_now()
    public_line = cand.group(1) if cand else req.get("tag") if BETA.match(req.get("tag") or "") else None
    if public_line and not NOT_FROM_PUBLIC.match(public_line):
        bad = not_on_public(pins)
        if bad:
            raise RuntimeError(f"{req['tag']} 钉的提交要先同步到公开仓库：" + "；".join(bad))
    last = (st.get("history") or [None])[-1]
    if req.get("tag"):
        tag = req["tag"]
        done = any(h.get("tag") == tag and h.get("ok") for h in st.get("history", []))
        if done:
            raise RuntimeError(f"{tag} 已经出过了；" + ("要再出一个候选版就把 rc 后面的号加一" if cand else "要再出一版用 /release（会顺延成 -3 起）"))
        resume = os.path.isdir(os.path.join(CORE, "releases", tag))     # 出过一半：internal_build 按 steps.json 接着做
    else:
        resume = bool(last and not last.get("ok") and last.get("pins") == pins and last.get("tag", "").startswith("internal-" + time.strftime("%m%d")))
        tag = last["tag"] if resume else next_tag(time.strftime("%m%d"), st.get("history", []))
    # 出版克隆的工作区放到钉住的提交上（internal_build 的 freeze 要求正好在那个提交、没有改动）；流水线自己不跟着切
    sh(["git", "-C", CORE, "checkout", "-q", "--detach", pins["luoke-core"]])
    cmd = [PY, "-B", "tools/internal_build.py", "--tag", tag, "--build-mail"] + [f"--pin={k}={v}" for k, v in pins.items()]
    if BETA.match(tag) or cand:
        # beta 和它的候选版：一律全评（internal_build 对非内部版不走金丝雀）；差异对比拿上一个有台账的 beta（beta1.1 比 beta1），
        # 没有就照默认拿最近一版。候选版不算 beta（previous_beta 只认 betaN[.M]），rc2 也和上一个 beta 比，不和 rc1 比
        prev_beta = previous_beta(tag)
        if prev_beta:
            cmd += ["--prev", prev_beta]
    if cand:
        cmd += ["--url-base", url_base]     # 清单里配件包、引擎包、主控的地址都写成局域网的；隐含不上传
    if req.get("accept"):
        cmd.append("--accept")
    if req.get("no_eval"):
        cmd.append("--no-eval")
    if os.environ.get("LUOKE_RELEASE_DRYRUN") == "1":
        cmd.append("--no-publish")
    out = os.path.join(BUILD, f"daemon-{tag}.out")
    rc = os.path.join(BUILD, f"daemon-{tag}.rc")
    if os.path.exists(rc):
        os.remove(rc)
    script = " ".join(_q(x) for x in cmd) + f" > {_q(out)} 2>&1; echo $? > {_q(rc)}"
    p = subprocess.Popen(["/bin/sh", "-c", script], cwd=CORE, start_new_session=True, env=dict(os.environ, LC_ALL="en_US.UTF-8"),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    st["running"] = {"tag": tag, "pid": p.pid, "rc": rc, "pins": pins, "req": req, "started": time.time(), "resume": resume}
    log(f"开始 {tag}（{'续跑' if resume else '新的一版'}，{req.get('by', '定时')}）pid {p.pid}")
    reply(f"开始出 **{tag}**" + ("（接着上次停下的地方）" if resume else "") + "：" +
          "、".join(f"{k} {v[:7]}" for k, v in pins.items()) + ("。空跑：只出不传。" if os.environ.get("LUOKE_RELEASE_DRYRUN") == "1" else "。")
          + ("不评测：沿用上一版真评过的证据和开关。" if req.get("no_eval") else ""))


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
        tl = json.load(open(os.path.join(CORE, "releases", tag, "timeline.json"), encoding="utf-8"))
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
    # 流程报告和出版时比的是同一版：beta 和它的候选版比上一个 beta（start_build 的 --prev），内部版比上一个出好了的
    prev = (previous_beta(tag) if BETA.match(tag) or RC.match(tag) else None) or next((h["tag"] for h in reversed(hist) if h.get("ok")), None)
    subprocess.run([PY, "-B", "tools/release_report.py", tag] + (["--prev", prev] if prev else []), cwd=CORE, capture_output=True)
    summ = subprocess.run([PY, "-B", "tools/release_report.py", tag, "--summary"] + (["--prev", prev] if prev else []),
                          cwd=CORE, capture_output=True, text=True).stdout.strip()
    ok = rc == 0
    cand = RC.match(tag)
    why = ""
    url = ""
    if ok and cand:
        try:
            url = host_candidate(tag)
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            ok, why = False, f"打好了，但挂到局域网时出错：{e}；再发一次同样的请求会跳过做完的步骤、重新挂"
    if ok:
        if not cand and os.environ.get("LUOKE_RELEASE_DRYRUN") != "1":
            repo = "luokelabs/luoke-releases" if BETA.match(tag) else "luokelabs/luoke-internal-builds"     # luoke-core#36
            url = subprocess.run(["gh", "release", "view", tag, "--repo", repo, "--json", "url", "-q", ".url"],
                                 capture_output=True, text=True).stdout.strip()
        reply(f"**{tag}** 出好了" + ("（候选版：没上传，挂在局域网上给测试机装）" if cand else "") + (f"：{url}" if url else "（空跑，没上传）") +
              ("（发布后的复测被新的请求打断，下一版接着补）" if run.get("preempted") else "") + "\n\n" + summ +
              f"\n\n完整的流程报告在 luoke-core 的 `releases/{tag}/report.md`。")
    else:
        logp = os.path.join(BUILD, f"internal-{tag}.log")
        tail = ""
        try:
            tail = "".join(open(logp, encoding="utf-8").readlines()[-30:])
        except OSError:
            pass
        why = why or ("进程没了，也没留下退出码（机器重启或被结束过）" if rc is None else f"退出码 {rc}")
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


def follow_main() -> None:
    """工具目录切到最新的 origin/main（这一轮已经载入了，下一轮生效）。和出版克隆是同一份时不动：那份要留在钉住的提交上。
    取不到、切不过去（有人在工具目录里改了文件）只记日志，照旧用现在的代码。"""
    if os.path.realpath(ROOT) == os.path.realpath(CORE):
        return
    r = subprocess.run(["git", "-C", ROOT, "fetch", "-q", "origin", "main"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        log(f"工具目录取 origin/main 失败：{r.stderr.strip()[-300:]}")
        return
    head, new = (sh(["git", "-C", ROOT, "rev-parse", x]).strip() for x in ("HEAD", "FETCH_HEAD"))
    if head == new:
        return
    r = subprocess.run(["git", "-C", ROOT, "checkout", "-q", "--detach", new], capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"工具目录 {head[:7]} → {new[:7]}" if r.returncode == 0 else f"工具目录切不到 {new[:7]}，还在 {head[:7]}：{r.stderr.strip()[-300:]}")


def tick() -> None:
    st = load_state()
    preempt_repeats(st)
    finish_build(st)
    save_state(st)
    if not st.get("running"):
        follow_main()
    lab = lab_config.release()
    allowed = {x.strip() for x in os.environ["LUOKE_RELEASE_ALLOWED"].split(",") if x.strip()} if os.environ.get("LUOKE_RELEASE_ALLOWED") \
        else set(lab.get("allowed") or [])
    owner = os.environ.get("LUOKE_RELEASE_OWNER") or lab.get("owner") or ""
    rc_senders = set(lab.get("rc_senders") or [])
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
        req = parse_request(c, allowed, owner, rc_senders)
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
