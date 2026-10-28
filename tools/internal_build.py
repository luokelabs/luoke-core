#!/usr/bin/env python3
"""每天的内部版（顶层 docs/internal-builds.md，2026-10-16 定）：按 luoke-top 钉住的六个提交，夜里自动出一版。

    python3 tools/internal_build.py --tag internal-1016 --pin luoke-core=<sha> --pin luoke-mail=<sha> --pin luoke-mem=<sha> --pin luoke-ime=<sha>
        [--from-top]            从顶层仓库的标签读子模块指针（先 git fetch --tags），不用一个个 --pin
        [--prev <发布号>]        沿用谁的证据、和谁比差异（默认 releases/ 里最近的一个）
        [--tiers 16,32,128] [--steps freeze,apps,eval,merge,diff,recipe,engine,manifest,build,sign,dmg,publish]
        [--skip-remote]         只评本机那一档（排练用）
        [--no-publish]          不传 GitHub
    本机版（--tag local-MMDD-HHMM --prev <开发机上那版>）：试用期里修好的随时换到开发机上（2026-10-17 定）。
        只评本机那一档里改过的任务，不上传；装好后主控认本机放行（tools/local_allow.py），设置里标"本机版"

步骤：
  freeze    luoke-core 的工作区必须正好在钉住的提交上、没有未提交的改动；三个配件在 build.noindex/src/<repo>（持久克隆）里 checkout 到钉住的提交
  apps      配件 .app：邮件助手、记忆用各自放在 internal/ 的公证包，核对清单 version 等于钉住提交里的版本、且 .luoke-commit 到钉住提交之间
            app 目录（luoke_mail/packaging/scripts、luoke_mem/packaging）没有差异；输入法按钉住的提交用它的 scripts/install.sh 自己打（签名公证）。
            和上一版相比钉住的提交没变的，直接用上一版的 .app
  eval      沿用上一版的证据：同一（任务, 组合, 档）且配件从上一版评测时的提交到钉住的提交之间没有非文档改动、引擎版本相同 → 拷过来标 reused_from；
            其余按档分机器跑：128 本机，16、32 在各自的测试机上（rsync 冻结副本过去，远端跑 release.py --stage eval，评完 rsync 回来）
  merge     三档证据合并成 releases/<tag>/evidence/（文件名带档，raw/<档>/）
  diff…dmg  照 release.py：差异（有回退就停，不出这一版）→ 配方 → 引擎包（版本没变就拷上一版的）→ 清单 → 打包 → 签名公证 → DMG 公证
  publish   gh release create：预发布版，标题「螺壳 <tag>（内部版 · 团队用）」，说明第一行"团队内部用，测试者请装 beta"
只用标准库。失败就停在那一步、非零退出；日志在 build.noindex/internal-<tag>.log。
要钥匙串的步骤（输入法现打、引擎包重打、签名、DMG、上传）前先探测公证凭据读不读得到：负责人锁屏时读不到，就每 30 秒试一次，
解锁后自动续上（2026-10-16 定：照常锁屏，不改设置）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = os.path.dirname(ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import lab_config  # noqa: E402  实验室配置：别的档的机器（不进公开的树）
import release  # noqa: E402
import support  # noqa: E402  任务 × 档的支持台账
from luoke_core import apps as lsapps  # noqa: E402  LaunchServices 的登记只在这里管
from luoke_core.recipe import Recipe  # noqa: E402

PY = "/usr/bin/python3"
SRC = os.path.join(ROOT, "build.noindex", "src")
ACCESSORIES = ["luoke-mail", "luoke-mem", "luoke-ime"]      # luoke-mem 要冻结源码（评测、打进主控），但不再出单独的 .app（V1 §6）
APP_ACCESSORIES = ["luoke-mail", "luoke-ime"]
APP_DIRS = {"luoke-mail": ["luoke_mail", "packaging", "scripts"], "luoke-mem": ["luoke_mem", "packaging"]}   # 这些目录没变就算同一份 app
INTERNAL_APP = {"luoke-mail": ("build.noindex/internal", "螺壳邮件助手.app"), "luoke-mem": ("build/release.noindex/internal", "螺壳记忆.app")}
APP_NAME = {"luoke-mail": "螺壳邮件助手.app", "luoke-mem": "螺壳记忆.app", "luoke-ime": "LuokeIME.app"}
RELEASES_REPO = "luokelabs/luoke-releases"            # beta 和正式版
INTERNAL_REPO = "luokelabs/luoke-internal-builds"     # 内部版（10-28，luoke-core#36：外部的人从 luoke-releases 装错内部版、从此跟内部版渠道）
# 过渡（luoke-core#36）：internal-1028 及更早的主控只在 luoke-releases 里找内部版。改成按渠道查仓库以后的第一版内部版
# 在 luoke-releases 也建一条同名的 Pre-release，只放 release.json（清单里的地址都指向新仓库），旧版据此升级过去；
# 这一版发出去以后改成 False，之后的内部版只发新仓库。
INTERNAL_BRIDGE = True


def releases_repo(tag: str) -> str:
    return RELEASES_REPO if re.fullmatch(r"beta\d+(\.\d+)?", tag) else INTERNAL_REPO
# 出版机器（Mac Studio）的凭据都在一个单独的钥匙串里，由负责人亲手放；开发机不设这几个变量，照旧用登录钥匙串。
#   LUOKE_SIGN_KEYCHAIN     专用钥匙串的路径（Developer ID 证书、公证条目、上传令牌都在里面）
#   LUOKE_NOTARY_PROFILE    notarytool 条目名（Mac Studio 上是 App Store Connect API 密钥存的 luoke-notary）
#   LUOKE_KEYCHAIN_PW_FILE  专用钥匙串的密码文件（只有出版账户能读，600）：出版前用它解锁专用钥匙串（10-23：
#                           登录钥匙串会跟着屏幕一起锁，无人值守不能靠它）
#   LUOKE_KEYCHAIN_PW_ITEM  （旧）登录钥匙串里存专用钥匙串密码的条目
#   LUOKE_RELEASES_TOKEN    专用钥匙串里只能写 luoke-releases 和 luoke-internal-builds 的令牌条目（服务名），上传时交给 gh
SIGN_KEYCHAIN = os.environ.get("LUOKE_SIGN_KEYCHAIN", "")
NOTARY_PROFILE = os.environ.get("LUOKE_NOTARY_PROFILE", "luoke.ai")
# 本机那一档评测用、打引擎包用的 oMLX.app。出版机器上的出版账户不是管理员，装在 ~/Applications/oMLX-0.7.0.app；
# 不设时照配方里写的（/Applications/oMLX.app）
ENGINE_APP = os.environ.get("LUOKE_ENGINE_APP", "")     # 可以写 {version}（例如 ~/Applications/oMLX-{version}.app），按配方的引擎版本填


def engine_app_for(template: str, path: str) -> str:
    """路径里的 {version} 换成配方的引擎版本（"omlx-0.7.2" → "0.7.2"）：换引擎只改配方一处（10-26 换 0.7.2）"""
    if "{version}" not in path:
        return path
    with open(template, encoding="utf-8") as f:
        ver = str(json.load(f)["engine"]["version"]).replace("omlx-", "")
    return path.replace("{version}", ver)
MODEL_SOURCE = os.path.join(ROOT, "build.noindex", "upload")
# 输入法评测的私人语料（仓库外）。出版机器上 luoke-ime 拷到 ~/luoke-private/ime/corpus.tsv（目录 700、文件 600），由 LUOKE_IME_CORPUS 指过去
IME_CORPUS = os.path.expanduser(os.environ.get("LUOKE_IME_CORPUS") or "~/Codes/on-device-llm/ime_eval/private/corpus.tsv")
# 别的档在哪台机器上评（rsync 冻结副本过去，远端跑 release.py --stage eval）：主机表不进公开的树，在实验室配置里（tools/lab_config.py）
MACHINES = lab_config.machines()
LOG = None


# ---- 流程报告的原始数据：每一步的起止、结果，加上每 30 秒一次的本机资源采样 ---------------------------
class Timeline:
    """写进 releases/<tag>/timeline.json：{"tag", "host", "phases": [{name, start, end, ok}], "samples": [{t, load, wired_gb, compressed_gb,
    swapouts, pressure, free_disk_gb}]}。续跑时接着原来的文件写（之前的阶段留着），由 tools/release_report.py 读成报告。"""

    def __init__(self):
        self.path = None
        self.data = {"phases": [], "samples": []}
        self.lock = threading.Lock()

    def open(self, tag: str, rec_dir: str) -> None:
        self.path = os.path.join(rec_dir, "timeline.json")
        if os.path.exists(self.path):
            try:
                self.data = json.load(open(self.path, encoding="utf-8"))
            except (OSError, ValueError):
                pass
        self.data.update(tag=tag, host=os.uname().nodename.split(".")[0])
        self.data.pop("failed", None)       # 续跑：上一次停在哪儿不再算数，这一次停了会重新记
        self.data.setdefault("runs", []).append(time.time())
        self.save()
        threading.Thread(target=self._sampler, daemon=True).start()

    def save(self) -> None:
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def phase(self, name: str):
        tl = self

        class _P:
            def __enter__(self):
                self.rec = {"name": name, "start": time.time(), "end": None, "ok": None}
                with tl.lock:
                    tl.data["phases"].append(self.rec)
                tl.save()
                return self

            def __exit__(self, et, ev, tb):
                self.rec.update(end=time.time(), ok=et is None)
                tl.save()
                return False
        return _P()

    @staticmethod
    def sample() -> dict:
        out = {"t": time.time(), "load": round(os.getloadavg()[0], 2)}
        try:
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            def pages(label):
                m = re.search(label + r":\s+(\d+)", vm)
                return int(m.group(1)) if m else 0
            out["wired_gb"] = round(pages("Pages wired down") * page / 2**30, 1)
            out["compressed_gb"] = round(pages("Pages occupied by compressor") * page / 2**30, 1)
            out["swapouts"] = pages("Swapouts")
            out["pressure"] = int(subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True, timeout=10).stdout.strip() or 0)
            st = os.statvfs(ROOT)
            out["free_disk_gb"] = round(st.f_bavail * st.f_frsize / 2**30, 1)
        except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
            pass
        return out

    def _sampler(self) -> None:
        while True:
            s = self.sample()
            with self.lock:
                self.data["samples"].append(s)
            self.save()
            time.sleep(30)


TIMELINE = Timeline()


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOG:
        LOG.write(line + "\n")
        LOG.flush()


def die(msg: str, code: int = 1) -> None:
    log("失败：" + msg)
    TIMELINE.data["failed"] = msg      # 流程报告和 issue 回复里写停在哪、为什么
    TIMELINE.save()
    sys.exit(code)


def run(cmd: list, cwd: str = None, env: dict = None, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd) + (f"   (cwd {cwd})" if cwd else ""))
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not quiet and r.stdout.strip():
        log(r.stdout.strip()[-3000:])
    if r.returncode != 0:
        log(r.stderr.strip()[-3000:])
        if check:
            die(f"命令退出码 {r.returncode}：{' '.join(cmd[:4])}…")
    return r


def git(repo: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} 在 {repo}：{r.stderr.strip()}")
    return r.stdout.strip()


# ---- 钉住的提交 -------------------------------------------------------------------------------------------

def pins_from_top(tag: str) -> dict:
    git(TOP, "fetch", "-q", "--tags", "origin")
    out = git(TOP, "ls-tree", "-r", tag)
    pins = {}
    for line in out.splitlines():
        mode, kind, sha, path = line.split(None, 3)
        if kind == "commit":
            pins[path] = sha
    return pins


def check_core(pin: str) -> None:
    """luoke-core 工作区正好在钉住的提交、没有改动；打进主控的几个目录里也没有未跟踪的文件（审查 2026-10-17：会话这几个小时里新建的文件会被打进去）。
    开头查一次，打包、签名、上传前各再查一次。"""
    head = git(ROOT, "rev-parse", "HEAD")
    if not head.startswith(pin) and not pin.startswith(head):
        die(f"luoke-core 工作区在 {head[:7]}，钉住的是 {pin[:7]}：先 checkout / 推到位再跑")
    dirty = git(ROOT, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        die("luoke-core 有未提交的改动：\n" + dirty)
    stray = git(ROOT, "status", "--porcelain", "--untracked-files=all", "--", "luoke_core", "app/Sources", "app/build_app.sh", "tools")
    if stray:
        die("luoke-core 打包用到的目录里有未跟踪的文件：\n" + stray)


def freeze(pins: dict) -> dict:
    """三个配件的持久克隆 checkout 到钉住的提交；返回 {repo: 本地路径}。"""
    os.makedirs(SRC, exist_ok=True)
    paths = {}
    for repo in ACCESSORIES:
        pin = pins[repo]
        dst = os.path.join(SRC, repo)
        if not os.path.isdir(os.path.join(dst, ".git")):
            if os.path.isdir(dst):
                shutil.rmtree(dst)     # 10-15 用 git archive 解出来的旧冻结副本，没有 .git，换成持久克隆
            run(["git", "clone", "-q", os.path.join(TOP, repo), dst], quiet=True)
        git(dst, "fetch", "-q", "origin")
        git(dst, "checkout", "-q", "-f", "--detach", pin)    # 持久克隆只给出版用：上次留下的改动（试跑时盖过的文件）一律丢掉
        # 输入法 vendor 里编好的 librime、雾凇留着：luoke-ime b5a480b 起按版本写标记、对不上就重取重编（源码和包里的对得上，GPL；审查 2026-10-17）
        git(dst, "clean", "-qfdx", "-e", "/vendor", "-e", "/engines/*/vendor")
        full = git(dst, "rev-parse", "HEAD")
        with open(os.path.join(dst, ".luoke-commit"), "w") as f:
            f.write(full + "\n")
        paths[repo] = dst
        log(f"冻结 {repo} @ {full[:7]} → {os.path.relpath(dst, ROOT)}")
    return paths


# ---- 配件 .app --------------------------------------------------------------------------------------------

def notarized(app: str) -> bool:
    ok = subprocess.run(["xcrun", "stapler", "validate", app], capture_output=True).returncode == 0
    lsapps._ls("-u", app)   # 校验会把它登记进 LaunchServices（luoke-mem 2026-10-16 发现），立刻注销
    return ok


def app_version(app: str) -> str:
    with open(os.path.join(app, "Contents", "Resources", "luoke-accessory.json"), encoding="utf-8") as f:
        return json.load(f)["version"]


def bundled_mem_ok(app: str, mem_src: str, mem_pin: str) -> Optional[str]:
    """工具自带的 luoke_mem 现在只是兜底（10-19 定：配件用主控里那份，主控在 GET /tasks 里报位置）：版本可以比钉住的旧，不能比它新
    ——新了说明钉错了（主控里那份反而落后）。之前要求三个文件逐字节一致，记忆每改一次三方都得出新版。返回问题说明，没问题返回 None。"""
    hits = glob.glob(os.path.join(app, "Contents", "Resources", "python", "lib", "python3.*", "site-packages", "luoke_mem", "__init__.py"))
    if not hits:
        return None       # 不带兜底的工具：只用主控里那份
    def ver(text):
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
        return tuple(int(x) for x in re.findall(r"\d+", m.group(1))) if m else ()
    with open(hits[0], encoding="utf-8") as f:
        have = ver(f.read())
    want = ver(git(mem_src, "show", f"{mem_pin}:luoke_mem/__init__.py"))
    if have and want and have > want:
        return f"自带的兜底 luoke_mem {'.'.join(map(str, have))} 比钉住的 luoke-mem {mem_pin[:7]}（{'.'.join(map(str, want))}）还新：钉错了？"
    return None


def package_matches(app: str, pkg: str, src: str, pin: str) -> Optional[str]:
    """包里 site-packages/<pkg> 的每个 .py 和 git show <pin>:<pkg>/… 逐字节一样，两边文件一一对应（审查 2026-10-17：之前只比版本号和 diff）。"""
    hits = glob.glob(os.path.join(app, "Contents", "Resources", "python", "lib", "python3.*", "site-packages", pkg))
    if not hits:
        return "包里没有这个包"
    have = {}
    for root, _d, names in os.walk(hits[0]):
        for n in names:
            if n.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, n), hits[0])
                with open(os.path.join(root, n), "rb") as f:
                    have[rel] = f.read()
    listed = git(src, "ls-tree", "-r", "--name-only", pin, pkg).splitlines()
    want = {os.path.relpath(p, pkg): p for p in listed if p.endswith(".py")}
    if set(have) != set(want):
        return "文件不一一对应：" + "、".join(sorted(set(have) ^ set(want))[:5])
    for rel, path in want.items():
        r = subprocess.run(["git", "-C", src, "show", f"{pin}:{path}"], capture_output=True)
        if r.stdout != have[rel]:
            return f"{rel} 内容不同"
    return None


def take_internal_app(repo: str, src: str, pin: str, out: str, pins: dict = None, src_all: dict = None) -> str:
    """邮件助手 / 记忆：用它们放在 internal/ 的公证包（和 luoke-mail、luoke-mem 2026-10-16 商定的核对法）。"""
    sub, name = INTERNAL_APP[repo]
    app = os.path.join(TOP, repo, sub, name)
    marker = os.path.join(TOP, repo, sub, ".luoke-commit")
    if not os.path.isdir(app) or not os.path.exists(marker):
        die(f"{repo}：{app} 或旁边的 .luoke-commit 不在")
    with open(marker, encoding="utf-8") as f:
        built = f.read().strip()
    want = json.loads(git(src, "show", f"{pin}:packaging/accessory.json"))["version"]
    have = app_version(app)
    if have != want:
        die(f"{repo}：internal/ 里的 .app 是 {have}，钉住的提交里 packaging/accessory.json 是 {want}")
    r = subprocess.run(["git", "-C", src, "diff", "--quiet", built, pin, "--", *APP_DIRS[repo]], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 1:
        die(f"{repo}：从打包的提交 {built[:7]} 到钉住的 {pin[:7]}，app 目录有改动，internal/ 里的包是旧的")
    if r.returncode != 0:
        die(f"{repo}：git diff {built[:7]}..{pin[:7]} 出错：{r.stderr.strip()}")
    if not notarized(app):
        die(f"{repo}：{app} 没公证（stapler validate 不过）")
    pkg = {"luoke-mail": "luoke_mail", "luoke-mem": "luoke_mem"}[repo]
    bad = package_matches(app, pkg, src, pin)
    if bad:
        die(f"{repo}：包里的 {pkg} 和钉住的 {pin[:7]} 不一样：{bad}")
    if repo == "luoke-mail" and pins and src_all:
        bad = bundled_mem_ok(app, src_all["luoke-mem"], pins["luoke-mem"])
        if bad:
            die(f"{repo}：{bad}")
    dst = os.path.join(out, name)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    run(["ditto", app, dst], quiet=True)
    lsapps._ls("-u", dst)
    log(f"{repo}：用 internal/ 的 {have}（打包提交 {built[:7]}，钉住 {pin[:7]}，公证过）")
    return dst


def build_ime(src: str, out: str) -> str:
    """输入法按钉住的提交用它的 scripts/install.sh 打（luoke-ime 2026-10-16 给的做法）：签名、公证、staple，不装不注册。"""
    wait_for_keychain()        # 它也要公证
    env = dict(os.environ, LUOKE_NOTARIZE="1", LUOKE_OUT=out)
    env.pop("LUOKE_SKIP_BUILD", None)
    log("输入法：scripts/install.sh（编壳和引擎、公证，第一次要编 librime，几分钟）")
    r = subprocess.run(["zsh", os.path.join(src, "scripts", "install.sh")], cwd=src, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (r.stdout or "")[-2500:] + "\n" + (r.stderr or "")[-2500:]
    log(tail.strip())
    if r.returncode != 0:
        die(f"输入法构建退出码 {r.returncode}")
    app = os.path.join(out, "LuokeIME.app")
    if not os.path.isdir(app) or not notarized(app):
        die("输入法：LuokeIME.app 不在或没公证")
    return app


def build_mail(src: str, mem_src: str, out: str) -> str:
    """邮件助手按钉住的提交由流水线自己打（出版机器上没有邮件助手会话交来的公证包）：
    它自己的 scripts/package-app.sh（用本仓库的 package_python_app.py，打进钉住的 luoke-mem），再 release_sign.sh 签名公证。"""
    env = dict(os.environ, LUOKE_CORE=ROOT, LUOKE_MEM=mem_src)
    log("邮件助手：scripts/package-app.sh（按钉住的提交打包）")
    r = subprocess.run(["sh", os.path.join(src, "scripts", "package-app.sh")], cwd=src, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(((r.stdout or "")[-1500:] + "\n" + (r.stderr or "")[-1500:]).strip())
    built = os.path.join(src, "build.noindex", APP_NAME["luoke-mail"])
    if r.returncode != 0 or not os.path.isdir(built):
        die(f"邮件助手打包退出码 {r.returncode}")
    app = os.path.join(out, APP_NAME["luoke-mail"])
    if os.path.lexists(app):
        lsapps.remove_app(app)
    run(["ditto", built, app], quiet=True)
    lsapps._ls("-u", built)
    wait_for_keychain()
    run([os.path.join(ROOT, "tools", "release_sign.sh"), app, "--notarize"])
    if not notarized(app):
        die("邮件助手：没公证上")
    return app


def build_apps(tag: str, pins: dict, src: dict, prev_pins: dict, out: str, own: Optional[dict] = None, build_mail_too: bool = False) -> dict:
    """配件 .app：钉住的提交和 luoke-mem 都没变就沿用。先看这一版自己上一次已经打好的（续跑，own 里每项记着当时的 mem），再看上一版。"""
    os.makedirs(out, exist_ok=True)
    apps = {}
    for repo in APP_ACCESSORIES:
        mine = ((own or {}).get("apps") or {}).get(repo) or {}
        if mine.get("mem") and mine.get("pin") == pins[repo] and mine["mem"] == pins.get("luoke-mem") and os.path.isdir(mine.get("app", "")):
            apps[repo] = mine["app"]
            log(f"{repo}：这一版上一次已经打好（{mine['app']}），接着用")
            continue
        prev = (prev_pins or {}).get("apps", {}).get(repo)
        same_mem = (prev or {}).get("mem", ((prev_pins or {}).get("pins") or {}).get("luoke-mem")) == pins.get("luoke-mem")
        if prev and prev.get("pin") == pins[repo] and same_mem and os.path.isdir(prev.get("app", "")):
            if repo == "luoke-mail":
                bad = bundled_mem_ok(prev["app"], src["luoke-mem"], pins["luoke-mem"])
                if bad:
                    die(f"{repo}：沿用的包 {bad}")
            apps[repo] = prev["app"]
            log(f"{repo}：钉住的提交和 luoke-mem 都和上一版一样，沿用 {prev['app']}")
            continue
        if repo == "luoke-ime":
            apps[repo] = build_ime(src[repo], out)
        elif repo == "luoke-mail" and build_mail_too:
            apps[repo] = build_mail(src[repo], src["luoke-mem"], out)
        else:
            apps[repo] = take_internal_app(repo, src[repo], pins[repo], out, pins, src)
        if prev and prev.get("pin") and prev["pin"] != pins[repo] and not unchanged_since(src[repo], prev["pin"], pins[repo]):
            # 代码变了、版本号没变：主控按版本号决定要不要升级，装着旧版的机器会悄悄留在旧版（10-19 输入法 5ddf047 就这样没到开发机上）
            new_v, old_v = app_version(apps[repo]), app_version(prev.get("app", ""))
            if new_v and new_v == old_v:
                die(f"{repo}：从 {prev['pin'][:7]} 到 {pins[repo][:7]} 代码改了，版本号还是 {new_v}——请配件升版本号再出")
    return apps


def app_version(app: str) -> Optional[str]:
    try:
        with open(os.path.join(app, "Contents", "Resources", "luoke-accessory.json"), encoding="utf-8") as f:
            return json.load(f).get("version")
    except (OSError, ValueError):
        return None


# ---- 评测：沿用 + 分机器跑 -------------------------------------------------------------------------------------

def unchanged_since(src: str, since: str, pin: str) -> bool:
    """配件从上次评测时的提交到钉住的提交，除文档外没改动。since 可能是短号。"""
    if not since:
        return False
    r = subprocess.run(["git", "-C", src, "diff", "--quiet", since, pin, "--", ".", ":(exclude)docs", ":(exclude)*.md"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def reusable(old: dict, spec: dict, src: dict, pins: dict, recipe, ignore_hub: bool = False) -> bool:
    """上一版的这条证据能不能沿用（审查 2026-10-17：之前只看配件自己的 diff 和配方里的引擎版本字符串）。全部满足才沿用：
    配件自己没改、主控代码没改（luoke_core/）、实际跑的引擎是配方的版本、评测表这一条没改、题集和语料的哈希没变、依赖的配件没改。
    缺任何一个字段（旧版证据）就不沿用。"""
    acc = spec["accessory"]
    if not unchanged_since(src[acc], old.get("accessory_commit"), pins[acc]):
        return False
    hub = old.get("hub_commit")
    if ignore_hub:
        pass        # 金丝雀（ADR 0008 修订 2026-10-23）：只看主控以外的条件，主控变没变由金丝雀的原始答复来证明
    elif not hub or subprocess.run(["git", "-C", ROOT, "diff", "--quiet", hub, pins["luoke-core"], "--", "luoke_core"],
                                 capture_output=True).returncode != 0:
        return False
    if old.get("engine_actual") != recipe.engine.get("version"):
        return False
    if old.get("eval_spec_sha256") != release.sha256_text(json.dumps(spec, sort_keys=True, ensure_ascii=False)):
        return False
    es = (old.get("evalset") or {})
    path = os.path.join(src[acc], es.get("path") or "")
    in_repo = "ime_corpus" not in (spec.get("requires") or [])      # 输入法的题集是仓库外的私人语料：只记了文件名，由下面的 corpus_sha256 核
    if not es.get("path") or (in_repo and not os.path.isfile(path)):
        return False
    # 记录里的 sha256 有两种：文件哈希，或评测程序自己算的题集哈希（run_tasks、run_typing、run_topics 都是后者，之前这几项永远沿用不了，
    # internal-1018 栽过）。题集文件在配件仓库里，上面已经核过配件从那次评测到钉住的提交没改，题集也就没变；这里只要求记录里有哈希
    if not es.get("sha256"):
        return False
    # 出版机器上还没放语料时（过渡期）：10-22 定，输入法没改就沿用（上面已经核过配件和依赖都没改），
    # 不核语料哈希；输入法改了，plan_eval 停下来，回 MBP 评。语料放过去以后照常核
    if "ime_corpus" in (spec.get("requires") or []) and os.path.isfile(IME_CORPUS) and old.get("corpus_sha256") != release.download.sha256_file(IME_CORPUS):
        return False
    for dep in spec.get("depends") or []:
        if not unchanged_since(src[dep], (old.get("dep_commits") or {}).get(dep), pins[dep]):
            return False
    return True


# 金丝雀挑哪条（ADR 0008 修订 2026-10-23，定"挑长提示的"）：按这个顺序取这一档开着的第一条。topics 一次喂整批材料，
# 走得到截断、内存重试这些短任务走不到的分支；thread 也是长的；都没有再取别的
CANARY_ORDER = ["luoke-mem.topics", "luoke-mail.thread", "luoke-mail.triage", "luoke-mail.cleanup"]
FULL_EVAL_DAYS = 7          # 每周至少全评一次（金丝雀的保底之二）


def plan_eval(tag: str, pins: dict, src: dict, prev: str, template: str, tiers: list, evals: dict, only: list = None,
              prev_led: Optional[dict] = None, canary_ok: bool = False) -> tuple:
    """返回 (要跑的 {档: [任务]}, 沿用的 [(档, 记录)], 挪到发布后的 [(档, 任务)], 金丝雀 {档: {"task", "old", "rest": [(任务, 记录)]}})。
    金丝雀（canary_ok，ADR 0008 修订）：一格只差"主控代码变了"、别的条件都满足时不直接重评，每档挑一条先评，
    原始答复和上一版逐字节一样，这一档其余这样的格子沿用（评测那一步做比对）；不一样就照旧全评。
    挪到发布后（10-23，规则不变）：上一版关着、这一版也开不了的格子——上一版发布后没补满 3 遍——这一版评了也照"关"发，
    不占发布前的时间，发布后和复测一起评、补满 3 遍，下一版据此打开。上一版补满了的留在发布前评（这一版评过就开）；hold 着的照旧。用专用模型（不是本档底座）的任务只在最高档评一次（质量和档无关，beta0 也只评 128）。
    续跑（10-19：出版步骤要能续跑）：这一版自己上一次没跑完时已经有的证据，满足沿用条件就留着不重跑，不算"沿用"。"""
    recipe = Recipe.load(template)
    prev_ev = release.load_evidence(os.path.join(ROOT, "releases", prev)) if prev else {}
    own_ev = release.load_evidence(os.path.join(ROOT, "releases", tag))
    tasks = [t for t in evals if not t.startswith("_") and (not only or t in only)]
    top = max(tiers, key=int)
    to_run: dict = {}
    reused = []
    deferred = []
    hub_only: dict = {}
    for tier in tiers:
        plan = release.plan_combos(recipe, [tier], tasks)
        for (task, key), c in plan.items():
            if c["model"] != recipe.base_for(tier) and tier != top:
                continue
            spec = evals[task]
            mine = own_ev.get((task, key, tier))
            if mine and not mine.get("reused_from") and reusable(mine, spec, src, pins, recipe):
                continue          # 上一次已经评好了
            if mine and mine.get("canary") and mine.get("reused_from") == prev and canary_still_holds(own_ev, mine["canary"], tier, evals, src, pins, recipe) \
                    and reusable(mine, spec, src, pins, recipe, ignore_hub=True):
                continue          # 上一次金丝雀核过、已经沿用了：续跑不再挑一个金丝雀重评（internal-1026-2 续跑时换挑了 thread，没对上，整档重评）
            old = prev_ev.get((task, key, tier))
            if old and reusable(old, spec, src, pins, recipe):
                reused.append((tier, old))
            elif closed_cant_open(task, tier, prev_led, evals):
                deferred.append((tier, task))
            elif canary_ok and old and reusable(old, spec, src, pins, recipe, ignore_hub=True):
                hub_only.setdefault(tier, []).append((task, old))
            else:
                to_run.setdefault(tier, []).append(task)
    canary: dict = {}
    for tier, cells in hub_only.items():
        def is_on(task):
            return ((prev_led or {}).get(task) or {}).get(str(tier), {}).get("state", "on") == "on"
        names = [t for t, _ in cells]
        # 只从 CANARY_ORDER 里挑（只看模型答复的生成任务）。输入法的 rerank、predict 不行：结果里有"在时限内来不来得及"
        # 这类跟速度有关的数（10-25：internal-1025-2 的 rerank changed 81 对 82），逐字节比永远对不上。没有可挑的这一档就全评
        pick = next((t for t in CANARY_ORDER if t in names and is_on(t)), None) or next((t for t in CANARY_ORDER if t in names), None)
        if pick is None:
            to_run.setdefault(tier, []).extend(names)
            continue
        old = dict(cells)[pick]
        canary[tier] = {"task": pick, "old": old, "rest": [(t, o) for t, o in cells if t != pick]}
        to_run.setdefault(tier, []).append(pick)
    need = sorted({f"{t} {task}" for t, ts in to_run.items() for task in ts if "ime_corpus" in (evals[task].get("requires") or [])})
    if need and not os.path.isfile(IME_CORPUS):
        die("要重评输入法（" + "、".join(need) + "），它读的私人语料只在开发机上（10-22 定：输入法改了回 MBP 评）。"
            "这台机器上还没有语料（LUOKE_IME_CORPUS）：请在 MBP 上出这一版，或请 luoke-ime 把语料放过来")
    # 金丝雀会挑的那几个任务排在每一档最前面（按 CANARY_ORDER），总在刚起的引擎上跑：16 GB 测试机（M1、9B）上，同一个任务前面先跑过
    # 别的，温度 0 的答复也会差几个字（10-25 查清：1024、1024-2 的 16 档 topics 排在邮件后面，1025-3 排第一，三份不一样；
    # 同样排第一，新旧两版主控的答复逐字节一样）。这样每一版的金丝雀原始答复才比得上
    for tier in to_run:
        to_run[tier] = canary_first(to_run[tier], (canary.get(tier) or {}).get("task"))
    return to_run, reused, deferred, canary


def canary_still_holds(own_ev: dict, task: str, tier: str, evals: dict, src: dict, pins: dict, recipe) -> bool:
    """这一版自己评的那条金丝雀还在、还满足沿用条件（钉住的提交没变）：靠它沿用的格子续跑时照旧沿用"""
    return any(t == task and str(g) == str(tier) and not d.get("reused_from") and reusable(d, evals[task], src, pins, recipe)
               for (t, _k, g), d in own_ev.items())


def canary_first(tasks: list, pick: Optional[str] = None) -> list:
    """这一档的金丝雀（pick，可能不在 CANARY_ORDER 里，例如只剩输入法的档）排第一，其余会被挑的按 CANARY_ORDER，再往后保持原来的顺序"""
    rank = {t: i + 1 for i, t in enumerate(CANARY_ORDER)}
    if pick:
        rank[pick] = 0
    return sorted(tasks, key=lambda t: rank.get(t, len(rank) + 1))


def canary_matches(tag: str, tier: str, task: str, old: dict) -> tuple:
    """金丝雀的原始答复和上一版那一条逐字节一样吗（去掉用时、日期、标签这些每次都变的字段）。返回 (一样不一样, 说明)。"""
    ev_dir = os.path.join(ROOT, "releases", tag, "evidence")
    mine = [d for (t, _k, g), d in release.load_evidence(os.path.join(ROOT, "releases", tag)).items() if t == task and str(g) == str(tier)]
    if not mine:
        return False, "金丝雀没出证据"
    new = mine[0]
    if (new.get("score"), new.get("hard_ok")) != (old.get("score"), old.get("hard_ok")):
        return False, f"分数 {new.get('score')} 对上一版 {old.get('score')}"
    def raw_of(d, base):
        r = d.get("raw")
        if not r:
            return None
        for cand in (os.path.join(base, r), os.path.join(base, "raw", str(tier), os.path.basename(r)), os.path.join(base, "raw", os.path.basename(r))):
            if os.path.exists(cand):
                with open(cand, encoding="utf-8") as f:
                    return json.load(f)
        return None
    a = raw_of(new, ev_dir)
    b = raw_of(old, os.path.dirname(old["_file"]))
    if a is None or b is None:
        return False, "找不到原始答复"
    if strip_volatile(a) != strip_volatile(b):
        return False, "原始答复不一样"
    return True, "原始答复和上一版逐字节一样"


VOLATILE = re.compile(r"(sec|seconds|_ms$|^ms$|time|latency|wall|elapsed|date|duration|label|release|hub|sock|path|file|machine|host)", re.I)


def strip_volatile(x):
    """比对前去掉每次都会变、和模型答复无关的字段（用时、日期、标签、路径）"""
    if isinstance(x, dict):
        return {k: strip_volatile(v) for k, v in x.items() if not VOLATILE.search(str(k))}
    if isinstance(x, list):
        return [strip_volatile(v) for v in x]
    return x


def canary_allowed(tag: str) -> tuple:
    """这一版能不能用金丝雀（保底之二、之三）：beta、公开版一律全评；离上一次全评超过 7 天也全评。返回 (能不能, 说明)。"""
    if not re.match(r"(internal|local|studio)-", tag):
        return False, "beta 和公开版一律全评"
    newest = 0.0
    for p in glob.glob(os.path.join(ROOT, "releases", "*", "pins.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("tag") != tag and d.get("eval_mode") == "full":
            newest = max(newest, os.path.getmtime(p))
    if time.time() - newest > FULL_EVAL_DAYS * 86400:
        return False, f"{FULL_EVAL_DAYS} 天内没全评过，这一版全评"
    return True, ""


def closed_cant_open(task: str, tier: str, prev_led: Optional[dict], evals: dict) -> bool:
    """上一版这一格明确关着、没 hold、发布后也没补满 3 遍：这一版不管评不评都关着（support.decide）"""
    if not prev_led:
        return False
    cell = (prev_led.get(task) or {}).get(str(tier)) or {}
    if cell.get("state") != "off" or str(tier) in (evals.get(task, {}).get("held_unsupported") or {}):
        return False
    runs = cell.get("runs") or []
    return not (len(runs) >= support.RUNS_TO_OPEN and all(runs[:support.RUNS_TO_OPEN]))


def copy_reused(tag: str, prev: str, reused: list, canary: Optional[str] = None) -> None:
    ev = os.path.join(ROOT, "releases", tag, "evidence")
    os.makedirs(ev, exist_ok=True)
    for tier, old in reused:
        d = {k: v for k, v in old.items() if not k.startswith("_") and k not in ("tiers", "params", "model")}
        d["reused_from"] = prev
        d["release"] = tag
        if canary:
            d["canary"] = canary        # 金丝雀核过：主控代码变了，但这一档的金丝雀答复和上一版逐字节一样（ADR 0008 修订）
        raw = d.get("raw")
        if raw:
            dst_raw = os.path.join(ev, raw)
            os.makedirs(os.path.dirname(dst_raw), exist_ok=True)
            # 上一版自己也是沿用的、原始答复没在它那里：顺着 reused_from 往前找
            for src_tag in [prev] + ([old["reused_from"]] if old.get("reused_from") else []):
                src_raw = os.path.join(ROOT, "releases", src_tag, "evidence", raw)
                if os.path.exists(src_raw):
                    shutil.copyfile(src_raw, dst_raw)
                    break
        name = f"{d['task']}.{d['combo']['key'].replace(':', '.')}.{tier}.json"
        with open(os.path.join(ev, name), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        log(f"沿用 {prev} 的证据：{d['task']} {tier} GB 档（{d['accessory']} {d.get('accessory_version')} {str(d.get('accessory_commit'))[:7]}）")


def eval_args(tag: str, tier: str, tasks: list, template: str, model_source: str, corpus: str, repos: dict) -> list:
    args = ["tools/release.py", "--release", tag, "--stage", "eval", "--tiers", tier, "--tasks", ",".join(tasks), "--template", template,
            "--model-source", model_source, "--ime-corpus", corpus]
    for repo, path in repos.items():
        args += ["--repo", f"{repo}={path}"]
    return args


def eval_local(tag: str, tier: str, tasks: list, template: str, src: dict) -> None:
    if tier != "128":   # 别的档的机器在实验室配置里；没配上时不能拿本机冒充那一档
        raise RuntimeError(f"{tier} GB 档没有评测机器（{lab_config.PATH}）")
    if ENGINE_APP:      # 引擎不在配方写的位置：照远端的做法，另写一份只改了引擎位置的配方
        with open(template, encoding="utf-8") as f:
            r = json.load(f)
        r["engine"]["app"] = engine_app_for(template, ENGINE_APP)
        template = os.path.join(ROOT, "recipes", "internal-local.json")
        with open(template, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
    r = subprocess.run([PY, "-B"] + eval_args(tag, tier, tasks, template, MODEL_SOURCE, IME_CORPUS, src), cwd=ROOT,
                       env=dict(os.environ, LC_ALL="en_US.UTF-8"), capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"[本机 {tier}] " + (r.stdout or "")[-4000:])
    if r.returncode != 0:
        log((r.stderr or "")[-3000:])
        raise RuntimeError(f"本机 {tier} GB 档评测退出码 {r.returncode}")


def rsync(src: str, dst: str, excludes: list = ()) -> None:
    cmd = ["rsync", "-a", "--delete"] + [f"--exclude={e}" for e in excludes] + [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
    run(cmd, quiet=True)


REMOTE_PIDFILE = "luoke-coexist/release-eval.pid"


def stop_remote_eval(host: str) -> None:
    """这台测试机上还有上一轮的评测（被打断的复测、断了线的出版）：先 SIGTERM 它（release.py 收到后停掉实验室实例和评测程序），
    最多等 90 秒，还在就 SIGKILL。只认 pid 文件里那个、而且命令行确实是 tools/release.py 的进程（防 pid 被别的进程重用）。"""
    script = (f"p=$(cat ~/{REMOTE_PIDFILE} 2>/dev/null); [ -n \"$p\" ] || exit 0; "
              "ps -o command= -p $p | grep -q tools/release.py || { rm -f ~/" + REMOTE_PIDFILE + "; exit 0; }; "
              "echo stopping $p; kill -TERM $p; n=0; while kill -0 $p 2>/dev/null && [ $n -lt 90 ]; do sleep 1; n=$((n+1)); done; "
              "kill -0 $p 2>/dev/null && kill -KILL $p; rm -f ~/" + REMOTE_PIDFILE)
    r = subprocess.run(["ssh", host, script], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if "stopping" in (r.stdout or ""):
        log(f"[{host}] 停掉了上一轮还在跑的评测（{r.stdout.strip()}）")


def eval_remote(tag: str, tier: str, tasks: list, src: dict, template_path: str, core_pin: str = "") -> None:
    m = MACHINES[tier]
    host = m["host"]
    # 同步：主控（工作区，不带 .git 和产物）、冻结的配件副本（带 .luoke-commit）、实验室用的配方（引擎位置按这台机器改）
    rsync(ROOT, f"{host}:{m['core']}", ["/.git", "/build.noindex", f"/releases/{tag}", "__pycache__", "/.luoke-commit"])
    if core_pin:   # 远端没有 .git：写 .luoke-commit，证据里的 hub_commit 才有值（不然沿用规则对远端证据永远不沿用）
        run(["ssh", host, f"printf '%s\\n' {core_pin} > {m['core']}/.luoke-commit"], quiet=True)   # 本次的证据只从远端拿回来，不推过去（10-16 推过去又拿回来，多出一堆双档名的副本）
    for repo, local in src.items():
        for remote in [m["repos"][repo]] + m["extra_sync"].get(repo, []):     # 专用目录 luoke-coexist/internal/：不写进有人在用的开发检出（审查 2026-10-17）
            rsync(local, f"{host}:{remote}", ["/.git", "__pycache__", "/build.noindex", "/build", "/vendor", "/.build"])
    with open(template_path, encoding="utf-8") as f:
        r = json.load(f)
    r["engine"]["app"] = engine_app_for(template_path, m["engine_app"])
    local_recipe = os.path.join(ROOT, "recipes", f"internal-{host}.json")
    with open(local_recipe, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    run(["scp", "-q", local_recipe, f"{host}:{m['core']}/recipes/internal-{host}.json"], quiet=True)
    evals = release.load_evals()
    needs_corpus = any("ime_corpus" in (evals[t].get("requires") or []) for t in tasks)
    run(["ssh", host, f"mkdir -p {os.path.dirname(m['corpus'])} luoke-coexist/internal"], quiet=True)
    if needs_corpus:
        run(["scp", "-q", IME_CORPUS, f"{host}:{m['corpus']}"], quiet=True)   # 语料是私密的：只在这台要评输入法时才拷，跑完就删（审查 2026-10-17）
    home = run(["ssh", host, "echo $HOME"], quiet=True).stdout.strip()
    repos = {repo: f"{home}/{m['repos'][repo]}" for repo in src}
    args = eval_args(tag, tier, tasks, f"recipes/internal-{host}.json", f"{home}/{m['models']}", f"{home}/{m['corpus']}", repos)
    remote_log = f"{home}/{os.path.dirname(m['core'])}/internal-{tag}-{tier}.log"      # 绝对路径：下面先 cd 进主控目录，相对路径就不对了（10-16 试跑栽过）
    stop_remote_eval(host)
    # 评测进程的 pid 记在 REMOTE_PIDFILE：本机这边被打断时 ssh 断了，远端的评测照跑（sshd 不替没有终端的命令发 SIGHUP），
    # 下一轮开跑前靠它先停掉上一轮（10-26：beta1 的复测被打断后在 16 GB 测试机上又跑了 16 分钟，internal-1026-2 的 16 档起不来）
    remote_cmd = f"cd {m['core']} && mkdir -p releases/{tag}/evidence && {{ LC_ALL=en_US.UTF-8 /usr/bin/python3 -B " + " ".join(f"'{a}'" for a in args) + \
        f" > {remote_log} 2>&1 & p=$!; echo $p > ~/{REMOTE_PIDFILE}; wait $p; rc=$?; }}; rm -f ~/{REMOTE_PIDFILE} {home}/{m['corpus']}; exit $rc"
    log(f"[{host} {tier}] 开跑：{len(tasks)} 个任务，日志 {host}:{remote_log}")
    r = subprocess.run(["ssh", host, remote_cmd], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.stderr.strip():
        log(f"[{host} {tier}] ssh stderr: " + r.stderr.strip()[-1500:])
    tail = subprocess.run(["ssh", host, f"tail -40 {remote_log}"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    log(f"[{host} {tier}] 退出码 {r.returncode}\n" + tail[-4000:])
    if r.returncode != 0:
        raise RuntimeError(f"{host} {tier} GB 档评测退出码 {r.returncode}")


def build_inputs_digest(tag: str, pins: dict) -> str:
    """打包以后几步的输入：钉住的提交、这一版的配方、清单里每一项的文件哈希（不含 built_at 这类每次都变的）。"""
    import hashlib
    items = {}
    rel = os.path.join(ROOT, "build.noindex", "release", tag, "release.json")
    if os.path.exists(rel):
        with open(rel, encoding="utf-8") as f:
            items = {k: [x.get("sha256") for x in v.get("files", [])] for k, v in json.load(f).get("items", {}).items()}
    recipe = os.path.join(ROOT, "recipes", f"{tag}.json")
    rsha = release.download.sha256_file(recipe) if os.path.exists(recipe) else None
    return hashlib.sha256(json.dumps({"pins": pins, "recipe": rsha, "items": items}, sort_keys=True).encode()).hexdigest()


def run_one(rtag: str, tier: str, task: str, src: dict, pins: dict, template: str, evals: dict) -> bool:
    """单独评一格一遍（复测、挪到发布后的格子用）：过了答 True。"""
    shutil.rmtree(os.path.join(ROOT, "releases", rtag), ignore_errors=True)
    try:
        if tier in MACHINES:
            eval_remote(rtag, tier, [task], src, template, pins["luoke-core"])
            merge_remote(rtag, tier)
        else:
            eval_local(rtag, tier, [task], template, src)
        got = release.load_evidence(os.path.join(ROOT, "releases", rtag))
        return bool(got) and all(release.passed(g, evals) for (t, _k, _t), g in got.items() if t == task)
    except Exception as e:  # noqa: BLE001
        log(f"{task} {tier} GB 档单评没跑成：{e}")
        return False
    finally:
        shutil.rmtree(os.path.join(ROOT, "releases", rtag), ignore_errors=True)


def repeat_runs(tag: str, prev: str, src: dict, pins: dict, template: str, deferred: list = ()) -> None:
    """关着的格子（上一版不支持、新任务；hold 着的不复测）这一版过了，就复测补满 3 遍（support.RUNS_TO_OPEN）；要不要复测、要几遍只问 support。
    10-20 定：放在发布之后跑（当天的版只跑一遍、关着的照"关"发），结果写回这一版的证据（repeat）和台账（runs），
    下一版据此打开。中途停了下次接着补，不重来。"""
    evals = release.load_evals()
    prev_led = support.previous(prev, ROOT)
    cur_led = support.load(os.path.join(ROOT, "releases", tag)) or {}
    ev_dir = os.path.join(ROOT, "releases", tag, "evidence")
    for p in sorted(glob.glob(os.path.join(ev_dir, "*.json"))):
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        tier = str(d.get("tier_run"))
        d.setdefault("tiers", d["combo"]["tiers"])
        runs = [release.passed(d, evals)] + [bool(x) for x in ((d.get("repeat") or {}).get("hard_ok") or [])[1:]]
        held = tier in (evals.get(d["task"], {}).get("held_unsupported") or {})
        if ((cur_led.get(d["task"]) or {}).get(tier) or {}).get("state") == "on":
            continue          # 这一版已经开了（例如上一版发布后补满 3 遍、今天也过了）：不用再补（luoke-top 10-21 核查）
        need = support.needs_repeat(((prev_led.get(d["task"]) or {}).get(tier) or {}).get("state"), held, runs)
        for n in range(need):
            rtag = f"{tag}-rep{len(runs)}"
            shutil.rmtree(os.path.join(ROOT, "releases", rtag), ignore_errors=True)
            try:
                if tier in MACHINES:
                    eval_remote(rtag, tier, [d["task"]], src, template, pins["luoke-core"])
                    merge_remote(rtag, tier)
                else:
                    eval_local(rtag, tier, [d["task"]], template, src)
                got = release.load_evidence(os.path.join(ROOT, "releases", rtag))
                ok = bool(got) and all(release.passed(g, evals) for (t, _k, _t), g in got.items() if t == d["task"])
            except Exception as e:  # noqa: BLE001
                log(f"复测 {d['task']} {tier} GB 档第 {len(runs) + 1} 遍没跑成：{e}")
                ok = False
            shutil.rmtree(os.path.join(ROOT, "releases", rtag), ignore_errors=True)
            runs.append(ok)
            d["repeat"] = {"runs": len(runs), "hard_ok": runs, "all_ok": all(runs)}
            d.pop("tiers", None)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            d["tiers"] = d["combo"]["tiers"]
            support.record_repeats(os.path.join(ROOT, "releases", tag), d["task"], tier, runs)
            if not ok:
                break             # 失败一遍就够了：这一版这一格关着
        if need:
            log(f"{d['task']} {tier} GB 档：关着、这一版过了，复测后 {runs}")
    # 挪到发布后的格子（plan_eval 的 deferred）：这一版发布前没评，现在评、补满 3 遍，失败一遍就停；结果记进这一版台账，下一版据此打开
    for tier, task in deferred:
        cell = (cur_led.get(task) or {}).get(str(tier)) or {}
        runs = list(cell.get("runs") or []) if cell.get("repeated_after_publish") else []     # 续跑：接着上次补到哪儿
        while len(runs) < support.RUNS_TO_OPEN and all(runs):
            runs.append(run_one(f"{tag}-post{len(runs) + 1}", str(tier), task, src, pins, template, evals))
            support.record_repeats(os.path.join(ROOT, "releases", tag), task, str(tier), runs)
        log(f"{task} {tier} GB 档：关着、挪到发布后评，结果 {runs}")


def merge_remote(tag: str, tier: str) -> None:
    m = MACHINES[tier]
    ev = os.path.join(ROOT, "releases", tag, "evidence")
    os.makedirs(ev, exist_ok=True)      # 复测（<tag>-repN）时本机还没有这个目录：之前拷不进来，远端过了也记成没过（internal-1018 栽过）
    tmp = os.path.join(ROOT, "build.noindex", f"evidence-{tag}-{tier}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    rsync(f"{m['host']}:{m['core']}/releases/{tag}/evidence", tmp)
    for p in glob.glob(os.path.join(tmp, "*.json")):
        b = os.path.basename(p)[:-5]
        with open(p, encoding="utf-8") as f:
            if str(json.load(f).get("tier_run")) != tier:
                continue          # 不是这台机器这一档评的（例如别处的副本）：不收
        if not b.endswith("." + tier):
            b += "." + tier
        shutil.copyfile(p, os.path.join(ev, b + ".json"))
    ne = os.path.join(tmp, f"not-evaluated-{tier}.jsonl")      # 这一档没评成的格子（release.py 记的）
    if os.path.exists(ne):
        shutil.copyfile(ne, os.path.join(ev, f"not-evaluated-{tier}.jsonl"))
    raw_src = os.path.join(tmp, "raw")
    if os.path.isdir(raw_src):
        raw_dst = os.path.join(ev, "raw", tier)
        # 合并进去，不先删：这一档沿用的证据，原始答复已经从上一版拷进来了（copy_reused），删了下一版的金丝雀就
        # "找不到原始答复"（10-25：internal-1025 的 32 档 topics 没有 raw，1025-3 的金丝雀因此全评）
        shutil.copytree(raw_src, raw_dst, dirs_exist_ok=True)


def normalize_raw(tag: str) -> None:
    """本机 128 的 raw 搬进 raw/128/；所有记录的 raw 字段改成 raw/<档>/<名字>。"""
    ev = os.path.join(ROOT, "releases", tag, "evidence")
    raw = os.path.join(ev, "raw")
    if os.path.isdir(raw):
        for x in os.listdir(raw):
            if x not in ("16", "32", "128"):
                os.makedirs(os.path.join(raw, "128"), exist_ok=True)
                dst = os.path.join(raw, "128", x)
                if os.path.exists(dst):
                    os.remove(dst) if os.path.isfile(dst) else shutil.rmtree(dst)
                shutil.move(os.path.join(raw, x), dst)
    for p in glob.glob(os.path.join(ev, "*.json")):
        tier = os.path.basename(p).rsplit(".", 2)[1]
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        r = d.get("raw")
        if r and not r.startswith(f"raw/{tier}/"):
            d["raw"] = f"raw/{tier}/" + r.split("/", 1)[1]
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)


# ---- 钥匙串：负责人锁屏时公证凭据读不到 -----------------------------------------------------------------------------

def unlock_sign_keychain() -> None:
    """出版机器：用登录钥匙串里存的密码解锁专用钥匙串（无人值守时没人输密码）。密码只在这个子进程之间传，不进日志。"""
    if not SIGN_KEYCHAIN:
        return
    pw_file, item = os.environ.get("LUOKE_KEYCHAIN_PW_FILE"), os.environ.get("LUOKE_KEYCHAIN_PW_ITEM")
    if pw_file:
        try:
            with open(os.path.expanduser(pw_file), encoding="utf-8") as f:
                pw = f.read().rstrip("\n")
        except OSError as e:
            log(f"读不到专用钥匙串的密码文件（{e.strerror}），没法解锁；等人来")
            return
    elif item:
        r = subprocess.run(["security", "find-generic-password", "-s", item, "-w"], capture_output=True, text=True)
        if r.returncode != 0:
            log(f"登录钥匙串里没有 {item}（专用钥匙串的密码），没法解锁；等人来")
            return
        pw = r.stdout.rstrip("\n")
    else:
        return
    u = subprocess.run(["security", "unlock-keychain", "-p", pw, SIGN_KEYCHAIN], capture_output=True, text=True)
    if u.returncode != 0:
        log(f"解锁专用钥匙串没成功：{u.stderr.strip()[-300:]}")


def releases_token_env() -> dict:
    """上传用的环境：出版机器上从专用钥匙串取只能写两个发布仓库的令牌交给 gh（GH_TOKEN）；MBP 上照旧用 gh 自己登录的账号。"""
    item = os.environ.get("LUOKE_RELEASES_TOKEN")
    if not item:
        return None
    cmd = ["security", "find-generic-password", "-s", item, "-w"] + ([SIGN_KEYCHAIN] if SIGN_KEYCHAIN else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        die(f"钥匙串里没有上传令牌 {item}")
    return dict(os.environ, GH_TOKEN=r.stdout.strip())


def keychain_ready() -> bool:
    """公证凭据（钥匙串里 notarytool 的条目，MBP 上是 luoke.ai）现在读得到吗。负责人锁屏时读不到（报 No Keychain password item found，
    2026-10-14 曾误以为丢了）；notarytool 不弹框，适合探测。只看退出码，不碰凭据内容。出版机器上先解锁专用钥匙串。"""
    unlock_sign_keychain()
    try:
        r = subprocess.run(["xcrun", "notarytool", "history", "--keychain-profile", NOTARY_PROFILE, "--output-format", "json"]
                           + (["--keychain", SIGN_KEYCHAIN] if SIGN_KEYCHAIN else []),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    return codesign_ready()


def codesign_ready(timeout: float = 60) -> bool:
    """签名证书的私钥现在能不弹窗地用吗（luoke-core#23：公证凭据读得到，不代表 codesign 能用私钥；出版机器上 codesign
    等「始终允许」的弹窗会一直挂着）。拿证书给一个临时文件签一下，限时；超时当钥匙串没准备好，日志写明。"""
    import tempfile
    ids = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"] + ([SIGN_KEYCHAIN] if SIGN_KEYCHAIN else []),
                         capture_output=True, text=True).stdout
    m = re.search(r"\b([0-9A-F]{40}) \"Developer ID Application", ids)
    if not m:
        log("钥匙串里找不到 Developer ID Application 证书")
        return False
    with tempfile.TemporaryDirectory() as d:
        probe = os.path.join(d, "probe")
        with open(probe, "w") as f:
            f.write("luoke codesign probe\n")
        try:
            r = subprocess.run(["codesign", "--force", "--sign", m.group(1)] + (["--keychain", SIGN_KEYCHAIN] if SIGN_KEYCHAIN else []) + [probe],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"codesign {timeout:.0f} 秒没结束：多半在等钥匙串授权弹窗（「codesign 想要使用密钥」）。在出版机器上点「始终允许」，"
                "或在出版账户里跑 security set-key-partition-list -S apple-tool:,apple:,codesign: -s ~/Library/Keychains/login.keychain-db")
            return False
    if r.returncode != 0:
        log("试签名没成：" + (r.stderr or "").strip()[-300:])
        return False
    return True


def wait_for_keychain(max_hours: float = 16) -> None:
    """定（2026-10-16，顶层 docs/internal-builds.md）：照常锁屏；夜里先跑完评测和打包，签名、公证、上传等负责人早上解锁后自动接着跑——
    每 30 秒试一次，解锁了就续上，不用谁手动触发。"""
    started = time.time()
    n = 0
    while not keychain_ready():
        if time.time() - started > max_hours * 3600:
            die(f"等了 {max_hours} 小时钥匙串还是读不到（负责人没解锁？），签名以后的阶段没做")
        if n % 120 == 0:
            log("钥匙串读不到公证凭据或签名证书（锁着？）：每 30 秒试一次，解锁后自动接着签名、公证、上传")
        n += 1
        time.sleep(30)          # 10-23：之前 5 分钟一次，解锁后平均白等两分半
    if n:
        log(f"钥匙串可用了（等了 {(time.time() - started) / 60:.0f} 分钟），接着跑")


# ---- 后面的阶段照 release.py ----------------------------------------------------------------------------------

def release_py(tag: str, stages: str, extra: list, tiers: str, prev: str, template: str) -> None:
    cmd = [PY, "-B", "tools/release.py", "--release", tag, "--tiers", tiers, "--template", template, "--stage", stages] + (["--prev", prev] if prev else []) + extra
    r = subprocess.run(cmd, cwd=ROOT, env=dict(os.environ, LC_ALL="en_US.UTF-8"), capture_output=True, text=True, encoding="utf-8", errors="replace")
    log(f"[release.py {stages}] " + (r.stdout or "")[-5000:])
    if r.returncode != 0:
        log((r.stderr or "")[-3000:])
        die(f"release.py --stage {stages} 退出码 {r.returncode}", 2 if stages == "diff" else 1)


def engine_zip_ok(zip_path: str) -> bool:
    """引擎包的 zip 顶层是 oMLX.app（主控只认这个名字，luoke-core#31）"""
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as z:
            tops = {n.split("/", 1)[0] for n in z.namelist() if n and not n.startswith("__MACOSX")}
    except (OSError, zipfile.BadZipFile):
        return False
    return tops == {"oMLX.app"}


def copy_engine(tag: str, prev: str, template: str) -> bool:
    with open(template, encoding="utf-8") as f:
        want = json.load(f)["engine"]["version"]
    src = os.path.join(ROOT, "build.noindex", "release", prev, "engine") if prev else ""
    info = os.path.join(src, "engine.json")
    if not os.path.exists(info):
        return False
    with open(info, encoding="utf-8") as f:
        e = json.load(f)
    if e.get("version") != want or not e.get("notarized") or not os.path.exists(e.get("zip", "")):
        return False
    if not engine_zip_ok(e["zip"]):
        log(f"{prev} 的引擎包里不是 oMLX.app（luoke-core#31 之前打的）：不沿用，重打")
        return False
    dst = os.path.join(ROOT, "build.noindex", "release", tag, "engine")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    e["zip"] = os.path.join(dst, os.path.basename(e["zip"]))
    with open(os.path.join(dst, "engine.json"), "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=1)
    log(f"引擎包 {want} 和 {prev} 一样，拷过来（{os.path.basename(e['zip'])}，公证过）")
    return True


KNOWN_DATA = ("engines/xiaohe/vendor/rime-ice/lua/lunar.db",)    # 雾凇自带的农历数据（公开的词库数据，不是用户数据）
FORBIDDEN = re.compile(r"(^|/)(\.git|\.env|[^/]*\.(db|sqlite|sqlite3|tsv))(/|$)|/Users/")


def scan_assets(paths: list) -> None:
    """上传到公开仓库前看一眼包里有没有不该有的东西（审查 2026-10-17）：.git、.env、数据库、语料（.tsv）、带负责人路径的文件名。"""
    import zipfile
    for p in paths:
        names = []
        if p.endswith(".zip"):
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                names += [os.path.relpath(os.path.join(root, n), p) for n in dirs + files]
        def plain(n):      # ditto 把扩展属性存成 ._<名字> 的 AppleDouble 条目：按原名判断
            d, b = os.path.split(n.rstrip("/"))
            return os.path.join(d, b[2:]) if b.startswith("._") else n
        bad = [n for n in names if FORBIDDEN.search("/" + n) and not plain(n).endswith(KNOWN_DATA)]
        if bad:
            die(f"{os.path.basename(p)} 里有不该发出去的东西：" + "、".join(bad[:5]))


def publish(tag: str, pins: dict, apps: dict, reused: list, to_run: dict, note: str = "") -> None:
    out = os.path.join(ROOT, "build.noindex", "release", tag)
    with open(os.path.join(out, "release.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    beta = bool(re.fullmatch(r"beta\d+(\.\d+)?", tag))
    if not beta and not re.fullmatch(r"internal-\d{4}(-\d+)?", tag):
        die(f"{tag} 不是内部版（internal-MMDD[-n]）也不是 beta（betaN[.M]）的标签：公开发布不走这个脚本的上传（要先问负责人）")
    # 只传清单列出的文件（审查 2026-10-17：之前按通配符收，目录里的旧文件会一起传）
    assets = []
    for name, item in manifest["items"].items():
        if item.get("kind") in ("accessory", "engine", "hub"):
            for f in item["files"]:
                p = os.path.join(out, "accessories" if item["kind"] == "accessory" else ("engine" if item["kind"] == "engine" else ""), f["path"])
                if not os.path.exists(p) or release.download.sha256_file(p) != f["sha256"]:
                    die(f"要传的 {f['path']} 不在或哈希和清单不一致")
                assets.append(p)
    for ins in (manifest.get("installers") or {}).values():
        p = os.path.join(out, ins["file"])
        if not os.path.exists(p) or release.download.sha256_file(p) != ins["sha256"]:
            die(f"安装器 {ins['file']} 不在或哈希不对")
        assets.append(p)
    for p in assets:
        if p.endswith(".dmg") and subprocess.run(["xcrun", "stapler", "validate", p], capture_output=True).returncode != 0:
            die(f"{os.path.basename(p)} 没有钉上公证票据")
    scan_assets([p for p in assets if p.endswith(".zip")] + [os.path.join(out, "app", "螺壳.app")])
    assets.append(os.path.join(out, "release.json"))
    # 对外的名字（luoke-top 10-26）：螺壳写发布号（0.1.0.dev1 这种主控版本号对测试者没意义），应用写清单里的名字
    versions = "、".join(f"{item.get('name') or name} {item.get('version')}" for name, item in manifest["items"].items() if item.get("kind") == "accessory")
    engine = str(manifest["items"].get("engine", {}).get("version", "?")).replace("omlx-", "oMLX ")
    notes = ([f"封闭测试版 {tag}。", ""] if beta else [f"团队内部用，测试者请装 beta。", ""]) + [
             f"{'测试版' if beta else '内部版'} {tag}（{time.strftime('%Y-%m-%d %H:%M')}）：螺壳 {tag}，{versions}，引擎 {engine}。",
             "钉住的提交：" + "、".join(f"{k} {v[:7]}" for k, v in sorted(pins.items())), "",
             f"评测：重跑 {sum(len(v) for v in to_run.values())} 条，沿用上一版 {len(reused)} 条。" +
             (f"注意：{note}" if note else ""),
             "安装：下载 Luoke.dmg，把「螺壳」拖进「应用程序」再打开；邮件助手、输入法在螺壳的「应用」窗口里装。已装着更早版本的，在菜单「检查更新」里升级，数据和下载过的模型保留。更新说明由 luoke-top 补。"]
    notes_path = os.path.join(out, "notes.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write("\n".join(notes) + "\n")
    gh_env = releases_token_env()
    # 内部版标 Pre-release，beta 和正式版不标：GitHub 的 latest 只落在 beta / 正式版上，官网的 latest/download 链接才对（luoke-brand#6）
    repo = releases_repo(tag)
    title = f"螺壳 {tag}（封闭测试版）" if beta else f"螺壳 {tag}（内部版 · 团队用）"
    run(["gh", "release", "create", tag, "--repo", repo] + ([] if beta else ["--prerelease"]) + ["--title", title,
         "--notes-file", notes_path] + assets, env=gh_env)
    r = run(["gh", "release", "view", tag, "--repo", repo, "--json", "assets,url", "-q", ".url, (.assets | length)"], quiet=True, env=gh_env)
    log("已发布：" + r.stdout.strip().replace("\n", "，附件 "))
    if not beta and INTERNAL_BRIDGE:
        page = r.stdout.strip().splitlines()[0]
        bridge = os.path.join(out, "bridge-notes.md")
        with open(bridge, "w", encoding="utf-8") as f:
            f.write(f"内部版从这一版起发在 {INTERNAL_REPO}：{page}\n\n这一条只给装着 internal-1028 及更早版本的螺壳找到升级用。\n")
        run(["gh", "release", "create", tag, "--repo", RELEASES_REPO, "--prerelease", "--title", title, "--notes-file", bridge,
             os.path.join(out, "release.json")], env=gh_env)
        log(f"过渡：{RELEASES_REPO} 里也建了 {tag}，只放 release.json")


def main() -> None:
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--pin", action="append", default=[], help="仓库=提交号")
    ap.add_argument("--from-top", action="store_true", help="从顶层仓库的标签读子模块指针")
    ap.add_argument("--prev", default=None, help="和谁比差异（默认 releases/ 里最近的一个）")
    ap.add_argument("--reuse-from", default=None, help="沿用谁的评测证据（默认同 --prev；例如排练过的那版）")
    ap.add_argument("--tiers", default="16,32,128")
    ap.add_argument("--steps", default="freeze,apps,eval,merge,diff,recipe,engine,manifest,build,sign,dmg,publish")
    ap.add_argument("--template", default=os.path.join(ROOT, "recipes", "dev.json"))
    ap.add_argument("--skip-remote", action="store_true")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--full-eval", action="store_true", help="不用金丝雀，全评（beta、公开版本来就全评）")
    ap.add_argument("--build-mail", action="store_true", help="邮件助手按钉住的提交现打现签（出版机器上；不用 internal/ 里交来的公证包）")
    ap.add_argument("--accept", action="store_true", help="差异报告有回退也放行（内部版默认不放行）")
    ap.add_argument("--allow-deviation", default=None, metavar="理由", help="--pin 和 --from-top 的标签不一致时放行，理由写进发布说明")
    ap.add_argument("--only-tasks", default=None, help="只评这些任务（逗号分隔；试跑远端用）")
    a = ap.parse_args()
    os.makedirs(os.path.join(ROOT, "build.noindex"), exist_ok=True)
    LOG = open(os.path.join(ROOT, "build.noindex", f"internal-{a.tag}.log"), "a", encoding="utf-8")
    steps = [s.strip() for s in a.steps.split(",")]
    tiers = [t.strip() for t in a.tiers.split(",")]
    local = a.tag.startswith("local-")
    if local:
        # 本机版（2026-10-17：试用期里修好的随时换到开发机上）：只在本机这一档评改过的任务，不上传；
        # 主控认本机放行（local-verified.json），设置里标"本机版"
        if not re.fullmatch(r"local-\d{4}-\d{4}", a.tag):
            die(f"{a.tag}：本机版的标签是 local-MMDD-HHMM")
        if not a.prev:
            die("本机版要写 --prev（沿用哪一版的证据，通常是开发机上现在那版）")
        a.skip_remote = True
        steps = [s for s in steps if s != "publish"]
    if a.skip_remote:
        tiers = [t for t in tiers if t not in MACHINES]
    elif [t for t in tiers if t != "128" and t not in MACHINES]:
        die(f"{lab_config.PATH} 里没有 {'、'.join(t for t in tiers if t != '128' and t not in MACHINES)} 档的机器：只有 128 档在本机评")
    log(f"=== 内部版 {a.tag}：步骤 {','.join(steps)}，档 {','.join(tiers)}")

    pins = pins_from_top(a.tag) if a.from_top else {}
    deviation_note = ""
    for spec in a.pin:
        k, v = spec.split("=", 1)
        if a.from_top and k in pins and not (pins[k].startswith(v) or v.startswith(pins[k])):
            # 和标签不一致（审查 2026-10-17：internal-1016-4 这样悄悄换过）：要明确放行，并写进发布说明
            if not a.allow_deviation:
                die(f"--pin {k}={v[:7]} 和标签 {a.tag} 钉的 {pins[k][:7]} 不一样：确实要这样就加 --allow-deviation")
            deviation_note += f"注意：{k} 用的是 {v[:7]}，不是标签里的 {pins[k][:7]}（{a.allow_deviation}）。"
        pins[k] = v
    missing = [r for r in ["luoke-core"] + ACCESSORIES if r not in pins]
    if missing:
        die("缺钉住的提交：" + ", ".join(missing))
    prev = release.previous_release(a.tag, a.prev)
    reuse_from = a.reuse_from or prev
    if re.fullmatch(r"beta\d+(\.\d+)?", a.tag):
        # beta 一律全评、全部现打：不沿用任何一版的证据和打好的应用（金丝雀本来就不给 beta 用，沿用证据也一样，luoke-top 10-26）
        reuse_from = None
    log("钉住：" + "、".join(f"{k} {v[:7]}" for k, v in sorted(pins.items())) + f"；差异对比：{prev}；证据沿用自：{reuse_from}")
    rec = os.path.join(ROOT, "releases", a.tag)
    os.makedirs(os.path.join(rec, "evidence"), exist_ok=True)
    TIMELINE.open(a.tag, rec)
    ph = TIMELINE.phase
    pins_path = os.path.join(rec, "pins.json")
    state = json.load(open(pins_path, encoding="utf-8")) if os.path.exists(pins_path) else {"tag": a.tag, "pins": pins, "apps": {}}
    own_state = json.loads(json.dumps(state))      # 这一版上一次留下的（续跑时沿用已打好的 .app）
    state["pins"] = pins          # 每次都更新（审查 2026-10-17：重跑时 pins.json 还是第一次的）
    if deviation_note:
        state["deviation"] = deviation_note
    prev_state_path = os.path.join(ROOT, "releases", reuse_from, "pins.json") if reuse_from else ""   # 配件 .app 也按它沿用
    prev_state = json.load(open(prev_state_path, encoding="utf-8")) if prev_state_path and os.path.exists(prev_state_path) else {}

    def save_state():
        with open(pins_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)

    check_core(pins["luoke-core"])
    src = {repo: os.path.join(SRC, repo) for repo in ACCESSORIES}
    if "freeze" in steps:
        with ph("freeze"):
            src = freeze(pins)
    apps = {k: v["app"] for k, v in state.get("apps", {}).items()} if state.get("apps") else {}
    if "apps" in steps:
        out = os.path.join(ROOT, "build.noindex", "release", a.tag, "apps.noindex")
        with ph("apps"):
            apps = build_apps(a.tag, pins, src, prev_state, out, own=own_state, build_mail_too=a.build_mail)
        state["apps"] = {repo: {"pin": pins[repo], "mem": pins.get("luoke-mem"), "app": apps[repo]} for repo in apps}
        save_state()
    evals = release.load_evals()
    canary_ok, canary_why = canary_allowed(a.tag)
    if a.full_eval:
        canary_ok, canary_why = False, "--full-eval"
    to_run, reused, deferred, canary = plan_eval(a.tag, pins, src, reuse_from, a.template, tiers, evals,
                                                 a.only_tasks.split(",") if a.only_tasks else None,
                                                 prev_led=support.previous(prev, ROOT), canary_ok=canary_ok)
    state["eval_mode"] = "canary" if canary else "full"
    if not canary_ok:
        log(f"这一版全评（{canary_why}）")
    for t, c in sorted(canary.items(), key=lambda x: int(x[0])):
        log(f"金丝雀：{t} GB 档先评 {c['task']}，和上一版逐字节一样就沿用其余 {len(c['rest'])} 格（{'、'.join(x for x, _ in c['rest']) or '无'}）")
    state["eval"] = {"to_run": to_run, "reused": [f"{t} {d['task']}" for t, d in reused], "deferred": [[t, task] for t, task in deferred],
                     "canary": {t: {"task": c["task"], "rest": [x for x, _ in c["rest"]]} for t, c in canary.items()}}
    save_state()
    log("评测计划：重跑 " + ("；".join(f"{t} GB 档 {','.join(v)}" for t, v in sorted(to_run.items(), key=lambda x: int(x[0]))) or "无") + f"；沿用 {len(reused)} 条"
        + ("；挪到发布后（关着、这一版开不了）：" + "、".join(f"{t} GB 档 {task}" for t, task in deferred) if deferred else ""))
    if "eval" in steps:
        copy_reused(a.tag, reuse_from, reused)
        # 金丝雀对不上时这一档其余照旧全评，那几格也算进来（internal-1026-2：128 档金丝雀没对上、全评到 rerank，引擎没编）
        if "luoke-ime" in src and (any("luoke-ime.rerank" in v for v in to_run.values())
                                   or any(x == "luoke-ime.rerank" for c in canary.values() for x, _ in c["rest"])):
            # 本机评输入法要编好的引擎（xiaohe、rime_helper）：输入法 .app 沿用上一版时没跑 install.sh，冻结时的 git clean 又把它们清掉了（internal-1018 栽过）
            here = os.path.join(src["luoke-ime"], "engines", "xiaohe")
            if not (os.path.exists(os.path.join(here, "xiaohe")) and os.path.exists(os.path.join(here, "rime_helper"))):
                log("输入法引擎没编：engines/xiaohe/build.sh")
                r = subprocess.run(["zsh", os.path.join(here, "build.sh")], cwd=here, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    die("输入法引擎编不过：" + (r.stderr or r.stdout or "")[-500:])
        errors: list = []

        def run_tier(tier, tasks):
            if tier in MACHINES:
                eval_remote(a.tag, tier, tasks, src, a.template, pins["luoke-core"])
                merge_remote(a.tag, tier)      # 一台评完就拿回来：别的档失败了，这一档的结果也留着，续跑时不重评
            else:
                eval_local(a.tag, tier, tasks, a.template, src)

        def worker(tier, tasks):
            try:
                with ph(f"eval {tier}"):
                    run_tier(tier, tasks)
                    c = canary.get(tier)
                    if c and c["rest"]:
                        same, why = canary_matches(a.tag, tier, c["task"], c["old"])
                        log(f"金丝雀 {tier} GB 档 {c['task']}：{why}" + ("，其余沿用" if same else "，这一档其余照旧全评"))
                        if same:
                            copy_reused(a.tag, reuse_from, [(tier, o) for _t, o in c["rest"]], canary=c["task"])
                        else:
                            run_tier(tier, [t for t, _ in c["rest"]])
            except BaseException as e:  # noqa: BLE001  （die() 抛 SystemExit：原因已经写进日志）
                errors.append(f"{tier} GB 档" + ("" if isinstance(e, SystemExit) else f"：{e}"))
        threads = [threading.Thread(target=worker, args=(t, ts)) for t, ts in to_run.items()]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if errors:
            die("评测没跑完：" + "；".join(errors))
    if "merge" in steps:
        normalize_raw(a.tag)
        ev = release.load_evidence(rec)
        log(f"证据 {len(ev)} 条：" + "；".join(f"{k[0]} {k[2]}GB {d['score']}/{d['score_max']} {'过' if d['hard_ok'] else '没过'}{'（金丝雀核过、沿用）' if d.get('canary') else ('（沿用）' if d.get('reused_from') else '')}"
                                        for k, d in sorted(ev.items())))
    tiers_arg = ",".join(tiers)
    if "diff" in steps:
        # 哪一档开关由支持台账按规则定（tools/support.py），不挡出版；挡的只有总分回退（看过报告加 --accept）和主要任务哪一档都没开
        with ph("diff"):
            release_py(a.tag, "diff", ["--accept"] if a.accept else [], tiers_arg, prev, a.template)
        led = support.load(rec) or {}
        state["support_flips"] = [f"{t} {g} GB {w}→{n}（{why}）" for t, g, w, n, why in support.flips(support.previous(prev, ROOT), led)]
        save_state()
    if "recipe" in steps:
        with ph("recipe"):
            release_py(a.tag, "recipe", [], tiers_arg, prev, a.template)
    if "engine" in steps and not copy_engine(a.tag, reuse_from, a.template) and not copy_engine(a.tag, prev, a.template):
        with ph("keychain wait"):
            wait_for_keychain()        # 引擎包换版本才重打，要公证
        with ph("engine"):
            release_py(a.tag, "engine", ["--notarize", "--yes"] + (["--engine-app", engine_app_for(a.template, ENGINE_APP)] if ENGINE_APP else []), tiers_arg, prev, a.template)
    common = ["--model-source", MODEL_SOURCE, "--modelscope", "zhixianio", "--huggingface", "luokelabs",
              "--repo", f"luoke-mem={src['luoke-mem']}"]          # 螺壳记忆按钉住的源码打进主控
    if not local:
        common += ["--github", releases_repo(a.tag)]
    # 本机版不上传：配件包、引擎包、主控的地址是本机的 file://（模型照旧用 ModelScope / Hugging Face 的钉住地址）。
    # 之前给了 --github，清单指向一个不存在的 GitHub 发布，开发机上升级后邮件助手 0.15.0 下不来（404，10-19）
    for repo, app in apps.items():
        if repo in APP_ACCESSORIES:
            common += ["--accessory", f"{repo}={app}"]
    if "manifest" in steps:
        with ph("manifest"):
            release_py(a.tag, "manifest", common, tiers_arg, prev, a.template)
    # 续跑（10-19）：打包、签名、DMG、上传做完就记进 steps.json，连同"输入"的摘要（钉住的提交 + 配方 + 清单里的文件哈希）；
    # 再跑时输入没变就跳过这一步，变了从这一步重做（之前失败后要手工拼目录、从头再来）
    steps_path = os.path.join(rec, "steps.json")
    done = json.load(open(steps_path, encoding="utf-8")) if os.path.exists(steps_path) else {}
    inputs = build_inputs_digest(a.tag, pins)
    redo = False

    def step(name, fn):
        nonlocal redo
        if not redo and done.get(name) == inputs:
            log(f"{name}：上次已经做完、输入没变，跳过")
            return
        with ph(name):
            fn()
        redo = True       # 这一步重做了，后面的都得重做
        done[name] = inputs
        with open(steps_path, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=1)

    if "build" in steps:
        step("build", lambda: (check_core(pins["luoke-core"]), release_py(a.tag, "build", common, tiers_arg, prev, a.template)))
    if "sign" in steps or "dmg" in steps or ("publish" in steps and not a.no_publish):
        if any(done.get(n) != inputs for n in ("sign", "dmg", "publish")) or redo:
            with ph("keychain wait"):
                wait_for_keychain()        # 签名、公证、上传（gh 的令牌也在钥匙串里）都要它
    if "sign" in steps:
        step("sign", lambda: (check_core(pins["luoke-core"]), release_py(a.tag, "sign", common + ["--notarize", "--yes"], tiers_arg, prev, a.template)))
    if "dmg" in steps:
        step("dmg", lambda: release_py(a.tag, "dmg", common + ["--notarize", "--yes"], tiers_arg, prev, a.template))
    if "publish" in steps and not a.no_publish:
        flips = state.get("support_flips") or []
        ne = release.load_not_evaluated(rec)       # 没评成的格子（10-22：不写结果、沿用上一版的开关，发布说明照实写）
        ne_note = ("这一版没评成（沿用上一版的开关）：" + "；".join(f"{t} {g} GB（{why}）" for (t, g), why in sorted(ne.items())) + "。") if ne else ""
        step("publish", lambda: (check_core(pins["luoke-core"]),
                                 publish(a.tag, pins, apps, reused, to_run, ("这一版档位变了：" + "；".join(flips) + "。" if flips else "") + ne_note + deviation_note)))
    if "merge" in steps:      # 复测在发布之后（10-20）：当天的版不等它；补满 3 遍的格子下一版打开
        log("发布后的复测：关着、这一版过了的格子补到 3 遍，结果进台账，下一版生效")
        with ph("repeats"):
            repeat_runs(a.tag, prev, src, pins, a.template, deferred=[tuple(x) for x in state["eval"].get("deferred", [])])
    log(f"=== 内部版 {a.tag} 完成")


if __name__ == "__main__":
    main()
