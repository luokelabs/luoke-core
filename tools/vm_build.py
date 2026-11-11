"""在出版机上的一次性 lume VM 里编：只把「编」挪进 VM，冻结、评测、配方、清单、输入法、minos、泄漏词、签名、公证、
DMG、上传都留在宿主机。VM 里没有钥匙串、令牌和泄漏词表；编出来的东西拷回宿主机，宿主机核对后再签名。

开关在实验室配置（tools/lab_config.py）的 vm_build 里，默认关：
  {"vm_build": {"enabled": true, "for": ["internal-", "local-"], "image": "rel-image", "work_dir": "~/vmci",
                "clamav_db": "~/vmci/clamav/db", "cpu": 8, "memory": "16GB", "timeout_minutes": 60}}
for 是哪些发布号前缀在 VM 里编（默认只有内部版和本机版）。

每次编：从出版镜像（出版机的镜像脚本做的 rel-image）克隆一台新 VM，两个共享目录：
  job/   只读：run.sh、源码快照（luoke-core 是钉住的提交的 git archive，配件是冻结的检出去掉 .git）、配方等文件、自带 Python 的下载缓存、病毒库
  out/   可写：VM 里的 run.sh 把产物、run.log、rc 写在这里
镜像里开机就跑 job/run.sh，跑完关机；宿主机等 VM 停下、收走 out/、删掉 VM。
VM 名字带 reljob- 前缀，出版机的 VM 管家不碰它；编的时候放 release-<pid>.hold，管家就不起新的 CI VM。
两台 VM 的上限（Apple 的许可）算在一起：CI 的 VM 正在跑就等它跑完。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_ROOT = ROOT          # 打进主控的 luoke-core（出版时就是编排的这份；试跑可以指别的检出）
sys.path.insert(0, os.path.join(ROOT, "tools"))
import lab_config  # noqa: E402

LUME = os.environ.get("LUOKE_LUME", "/opt/homebrew/bin/lume")
PREFIX = "reljob-"
SHARE = "/Volumes/My Shared Files"
GJOB, GOUT = f"{SHARE}/job", f"{SHARE}/out"
GWORK = "$HOME/w"                       # VM 里的工作目录：共享目录只读，编译要往源码目录里写（app/.build 等）
MAX_VMS = 2                              # Apple 的许可：每台 Mac 最多再跑两个 macOS 实例
PY_CACHE = os.path.expanduser("~/Library/Caches/luoke-core/python-build-standalone")   # package_python_app.py 的 CACHE
ENV = dict(os.environ, LUME_TELEMETRY_ENABLED="false")    # lume 默认开遥测，每条命令都关


class Stop(Exception):
    pass


# 调用方要接的：自己抛的 Stop，加上 rsync、git archive、tar、ditto 失败（CalledProcessError）、lume 超时（TimeoutExpired）、拷文件（OSError）
ERRORS = (Stop, subprocess.SubprocessError, OSError)
KEEP_RUNS = 20          # ~/vmci/rel/ 下留最近几次（只留日志和记录，产物拷回去就删）


def config() -> dict:
    return lab_config.load().get("vm_build") or {}


def enabled(release: str) -> bool:
    c = config()
    return bool(c.get("enabled")) and release.startswith(tuple(c.get("for") or ["internal-", "local-"]))


def image() -> str:
    return config().get("image", "rel-image")


def work_dir() -> str:
    return os.path.expanduser(config().get("work_dir", "~/vmci"))


def say(msg: str) -> None:
    print(f"== VM：{msg}", flush=True)


def lume(*args: str, check: bool = True, timeout: int = 600) -> str:
    p = subprocess.run([LUME, *args], capture_output=True, text=True, env=ENV, timeout=timeout)
    if check and p.returncode != 0:
        raise Stop(f"lume {' '.join(args)}：{(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def vms() -> list:
    return json.loads(lume("ls", "--format", "json") or "[]")


def state(name: str) -> str:
    return next((v.get("status", "") for v in vms() if v["name"] == name), "")


def wait_for_slot(max_wait: float = 2 * 3600, poll: float = 15) -> None:
    """等到正在跑的 VM 少于两台（CI 的 job 不打断，等它跑完）。和 CI 的 VM 同时开着没关系：lume 的 vmnet 里 VM 之间二层就不通
    （11-09 实测，ARP 都解析不到），出版镜像也关了 SSH、没有免密 sudo、口令是随机的。"""
    t0 = time.time()
    told = False
    while True:
        live = [v["name"] for v in vms() if v.get("status") != "stopped"]
        if len(live) < MAX_VMS:
            return
        if time.time() - t0 > max_wait:
            raise Stop(f"等了 {int(max_wait / 60)} 分钟还是有 {len(live)} 台 VM 在跑（{', '.join(live)}）")
        if not told:
            say(f"已经有 {len(live)} 台 VM 在跑（{', '.join(live)}），等它们跑完")
            told = True
        time.sleep(poll)


def image_version(img: Optional[str] = None) -> str:
    """出版镜像的身份：名字加磁盘的修改时间（重做镜像就变），写进构建记录和续跑的输入摘要。镜像不在就是空串。"""
    img = img or image()
    disk = os.path.expanduser(f"~/.lume/{img}/disk.img")
    try:
        return f"{img}@{int(os.path.getmtime(disk))}"
    except OSError:
        return ""


def clamav_db() -> str:
    return os.path.expanduser(config().get("clamav_db", "~/vmci/clamav/db"))


def clamav_version(db: str) -> str:
    """病毒库 daily 的版本：更新脚本用 sigtool --info 核过以后写在库旁边的 versions.json（文件名里的版本在增量更新后可能对不上）。
    读不到就是空串。"""
    try:
        with open(os.path.join(os.path.dirname(db.rstrip("/")), "versions.json"), encoding="utf-8") as f:
            v = str(json.load(f).get("daily", ""))
    except (OSError, ValueError):
        return ""
    return v if v.isdigit() else ""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def clean_stale() -> None:
    """上一次被杀（截版窗口、机器睡眠、手动停）留下的：进程不在的 release-<pid>.hold（留着 CI 就一直停）、
    名字里的进程不在的 reljob-* VM（占名额），~/vmci/rel/ 下超过 KEEP_RUNS 次的旧目录。"""
    w = work_dir()
    for n in os.listdir(w) if os.path.isdir(w) else []:
        m = re.fullmatch(r"release-(\d+)\.hold", n)
        if m and not alive(int(m.group(1))):
            say(f"删掉上次留下的 {n}")
            os.remove(os.path.join(w, n))
    for v in vms():
        m = re.fullmatch(re.escape(PREFIX) + r".*-(\d+)-\d{6}", v["name"])
        if m and not alive(int(m.group(1))):
            say(f"删掉上次留下的 VM {v['name']}")
            lume("stop", v["name"], check=False)
            lume("delete", v["name"], "--force", check=False)
    rel = os.path.join(w, "rel")
    runs = sorted((os.path.join(rel, n) for n in os.listdir(rel)), key=os.path.getmtime) if os.path.isdir(rel) else []
    for d in runs[:-KEEP_RUNS]:
        shutil.rmtree(d, ignore_errors=True)


# ---------- 放进 VM 的东西 ----------

class Job:
    """一次 VM 构建：往 job/ 里放东西、拿到 VM 里的路径，最后 run()。"""

    def __init__(self, name: str):
        # 名字里带进程号：进程没了，下次 clean_stale 就知道这台没人管
        self.name = f"{PREFIX}{re.sub(r'[^A-Za-z0-9-]', '-', name)[:30]}-{os.getpid()}-{time.strftime('%H%M%S')}"
        self.dir = os.path.join(work_dir(), "rel", self.name)
        if os.path.exists(self.dir):
            shutil.rmtree(self.dir)
        self.job = os.path.join(self.dir, "job")
        self.out = os.path.join(self.dir, "out")
        os.makedirs(os.path.join(self.job, "src"))
        os.makedirs(os.path.join(self.job, "files"))
        os.makedirs(self.out)
        self.sources: dict = {}
        self.record: dict = {"image": image_version(), "sources": {}}

    def core(self, root: Optional[str] = None) -> str:
        """luoke-core：钉住的提交的 git archive（宿主机的 check_core 已经核过工作区就在这个提交、没有改动）。返回 VM 里的路径。"""
        root = root or CORE_ROOT
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dst = os.path.join(self.job, "src", "luoke-core")
        os.makedirs(dst)
        a = subprocess.Popen(["git", "-C", root, "archive", "--format=tar", "HEAD"], stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", dst], stdin=a.stdout, check=True)
        if a.wait() != 0:
            raise Stop("luoke-core：git archive 失败")
        self.sources[os.path.realpath(root)] = "luoke-core"
        self.record["sources"]["luoke-core"] = head
        return self.guest_src("luoke-core")

    def source(self, repo: str, path: str) -> str:
        """配件：冻结的检出（freeze_one 的持久克隆，已经 checkout 到钉住的提交、clean 过），去掉 .git 拷进去。返回 VM 里的路径。"""
        if not path:
            return ""
        real = os.path.realpath(path)
        if real in self.sources:
            return self.guest_src(self.sources[real])
        dst = os.path.join(self.job, "src", repo)
        subprocess.run(["rsync", "-a", "--exclude", "/.git", "--exclude", ".build", path.rstrip("/") + "/", dst + "/"], check=True)
        commit = ""
        try:
            with open(os.path.join(path, ".luoke-commit"), encoding="utf-8") as f:
                commit = f.read().strip()
        except OSError:
            pass
        self.sources[real] = repo
        self.record["sources"][repo] = commit
        return self.guest_src(repo)

    def file(self, path: str) -> str:
        """一个文件（配方、发布清单等）。返回 VM 里的路径。"""
        if not path:
            return ""
        name = os.path.basename(path)
        dst = os.path.join(self.job, "files", name)
        if os.path.exists(dst):
            raise Stop(f"放进 VM 的文件重名：{name}")
        shutil.copy2(path, dst)
        return f"{GJOB}/files/{name}"

    def python_cache(self) -> None:
        """自带 Python 的下载缓存（哈希钉死在 package_python_app.py，VM 里照样核）：拷 .tar.gz，不拷解开的目录。"""
        dst = os.path.join(self.job, "pycache")
        os.makedirs(dst)
        for n in sorted(os.listdir(PY_CACHE)) if os.path.isdir(PY_CACHE) else []:
            if n.endswith((".tar.gz", ".tar.zst")):
                shutil.copy2(os.path.join(PY_CACHE, n), dst)

    def virus_db(self) -> None:
        """病毒库（出版机上定时更新的那份）：扫描打包要「这台机器的病毒库」才核对得过（scan#99）。"""
        db = clamav_db()
        v = clamav_version(db)
        if not v:
            raise Stop(f"{db} 里没有病毒库（出版机上定时更新）")
        shutil.copytree(db, os.path.join(self.job, "clamav"), ignore=shutil.ignore_patterns("tmp.*", "*.tmp"))
        self.record["clamav_daily"] = v

    @staticmethod
    def guest_src(repo: str) -> str:
        return f"{GWORK}/src/{repo}"

    def run(self, script: str, timeout_minutes: Optional[int] = None) -> str:
        """script 在 VM 里跑（bash，set -euo pipefail，在 $HOME/w 下；源码已经拷到 $HOME/w/src/<仓库>，产物写到 $OUT）。
        成功返回宿主机上的 out/ 路径；失败抛 Stop（run.log 的末尾先打出来）。VM 不管成没成都删掉。"""
        timeout = 60 * (timeout_minutes or int(config().get("timeout_minutes", 60)))
        with open(os.path.join(self.job, "run.sh"), "w", encoding="utf-8") as f:
            f.write(PRELUDE + script + "\n")
        with open(os.path.join(self.out, "job.json"), "w", encoding="utf-8") as f:
            json.dump(self.record, f, ensure_ascii=False, indent=1)
        img = image()
        if not image_version(img):
            raise Stop(f"没有出版镜像 {img}（先用镜像脚本做 release 镜像）")
        clean_stale()
        hold = os.path.join(work_dir(), f"release-{os.getpid()}.hold")
        open(hold, "w").close()
        try:
            wait_for_slot()
            say(f"{self.name}：从 {img} 克隆")
            lume("clone", img, self.name)
            try:
                lume("set", self.name, "--cpu", str(config().get("cpu", 8)), "--memory", str(config().get("memory", "16GB")))
                with open(os.path.join(self.dir, "lume-run.log"), "w") as log:
                    subprocess.Popen([LUME, "run", self.name, "--no-display", "--shared-dir", f"{self.job}:ro", "--shared-dir", self.out],
                                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=ENV, start_new_session=True)
                t0 = time.time()
                time.sleep(20)
                while state(self.name) != "stopped":
                    if time.time() - t0 > timeout:
                        lume("stop", self.name, check=False)
                        raise Stop(f"{self.name}：{timeout // 60} 分钟没编完，停了")
                    time.sleep(10)
                say(f"{self.name}：跑完，{int(time.time() - t0)} 秒")
            finally:
                for args in (("stop", self.name), ("delete", self.name, "--force")):
                    try:
                        lume(*args, check=False)
                    except subprocess.TimeoutExpired:
                        say(f"lume {args[0]} {self.name} 超时")
        finally:
            os.remove(hold)
        rc = _read(os.path.join(self.out, "rc")).strip()
        if rc != "0":
            print(_read(os.path.join(self.out, "run.log"))[-4000:], flush=True)
            raise Stop(f"{self.name}：VM 里退出码 {rc or '（没有，VM 没跑到最后）'}，完整日志 {os.path.join(self.out, 'run.log')}")
        shutil.rmtree(self.job, ignore_errors=True)      # 输入删掉；out/ 的产物调用方拷回去以后 trim() 删，日志和记录留着
        return self.out

    def trim(self) -> None:
        """产物已经拷回宿主机：out/ 里只留 run.log、rc、job.json、sdk.txt。"""
        for n in os.listdir(self.out):
            if n not in ("run.log", "rc", "job.json", "sdk.txt"):
                p = os.path.join(self.out, n)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# VM 里每个 run.sh 的开头：源码从只读的 job/ 拷到可写的工作目录；有缓存、病毒库就放到工具找的地方
PRELUDE = f"""#!/bin/bash
set -euo pipefail
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1
eval "$(/opt/homebrew/bin/brew shellenv)"
JOB="{GJOB}"; OUT="{GOUT}"; W="{GWORK}"
rm -rf "$W"; mkdir -p "$W"
cp -R "$JOB/src" "$W/src"
if [ -d "$JOB/pycache" ]; then mkdir -p ~/Library/Caches/luoke-core/python-build-standalone; cp "$JOB"/pycache/* ~/Library/Caches/luoke-core/python-build-standalone/ 2>/dev/null || true; fi
if [ -d "$JOB/clamav" ]; then mkdir -p /opt/homebrew/var/lib/clamav; cp "$JOB"/clamav/* /opt/homebrew/var/lib/clamav/; fi
cd "$W"
echo "VM：$(sw_vers -productVersion) $(sw_vers -buildVersion)，$(xcrun --show-sdk-version 2>/dev/null || echo '?') SDK，$(swift --version 2>&1 | head -1)，$(go version)"
"""


def copy_back(src: str, dst: str) -> None:
    """VM 的产物拷回宿主机（ditto 保留签名；不带扩展属性：VM 共享目录可能带上，codesign 会报错）。"""
    if os.path.lexists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        else:
            os.remove(dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    subprocess.run(["ditto", "--noextattr", "--noqtn", src, dst], check=True)


def sdk_report(out: str) -> str:
    return _read(os.path.join(out, "sdk.txt"))


# ---------- 主控（release.py 的 build 阶段） ----------

def build_app(env: dict, app_out: str, app_name: str) -> dict:
    """app/build_app.sh 在 VM 里跑：env 里的 LUOKE_*（宿主机路径）换成 VM 里的，编好的 <app_name> 拷回 app_out/。
    返回构建记录（镜像、各源码的提交、病毒库、SDK 报告）。"""
    job = Job("core")
    g = {"LUOKE_BUNDLE_ID": env.get("LUOKE_BUNDLE_ID", ""), "LUOKE_RELEASE_TEAM": env.get("LUOKE_RELEASE_TEAM", "")}
    core = job.core()
    for var in ("LUOKE_RECIPE_FILE", "LUOKE_RELEASE_FILE"):
        g[var] = job.file(env.get(var, ""))
    for var, repo in (("LUOKE_MEM_SRC", "luoke-mem"), ("LUOKE_AGENT_SRC", "luoke-agent"), ("LUOKE_SEARCH_SRC", "luoke-search"),
                      ("LUOKE_MAIL_SRC", "luoke-mail"), ("LUOKE_SCAN_SRC", "luoke-scan")):
        g[var] = job.source(repo, env.get(var, ""))
    if env.get("LUOKE_ICON_FILE"):
        g["LUOKE_ICON_FILE"] = job.file(env["LUOKE_ICON_FILE"])
    job.python_cache()
    if env.get("LUOKE_SCAN_SRC"):
        job.virus_db()
        g["LUOKE_SCAN_TOOLS"] = "/opt/homebrew"
    exports = "\n".join(f"export {k}={shlex.quote(v) if not v.startswith(GWORK) else chr(34) + v + chr(34)}" for k, v in g.items())
    script = f"""{exports}
sh "{core}/app/build_app.sh" "$W/app"
ditto "$W/app/{app_name}" "$OUT/{app_name}"
{SDK_REPORT.format(target=f'$OUT/{app_name}')}
"""
    out = job.run(script)
    copy_back(os.path.join(out, app_name), os.path.join(app_out, app_name))
    job.trim()
    rec = dict(job.record, sdk=sdk_report(out), vm=job.name)
    with open(os.path.join(app_out, "vm-build.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return rec


# 每个 Mach-O 的 SDK 和 minos（vtool），写 $OUT/sdk.txt：清单的 SDK 不能用 xcrun 取（出版机上它会答错）
SDK_REPORT = """find "{target}" -type f -perm -u+x -o -type f -name '*.dylib' -o -type f -name '*.so' | while IFS= read -r f; do
  if file -b "$f" | grep -q Mach-O; then
    v=$(vtool -show-build "$f" 2>/dev/null | awk '/minos/{{m=$2}} /sdk/{{s=$2}} END{{print "sdk " s " minos " m}}')
    echo "${{f#{target}/}}  $v"
  fi
done | sort > "$OUT/sdk.txt"
"""


# ---------- 打进主控的配件先编一遍（internal_build 的 apps 段：check_bundled） ----------

def _stage_back(job: Job, script: str, stage: str) -> str:
    """script 把产物放在 VM 里的 $W/stage；拷回宿主机的 stage（先清掉旧的）。"""
    out = job.run(script + '\nditto "$W/stage" "$OUT/stage"\n')
    copy_back(os.path.join(out, "stage"), stage)
    job.trim()
    return stage


def build_search(src: str, stage: str) -> str:
    """螺壳搜索的 scripts/build_helper.sh（同 internal_build.build_search，不带签名身份、不装开发版）。"""
    job = Job("search")
    s = job.source("luoke-search", src)
    return _stage_back(job, f'env -u SIGN_IDENTITY -u LUOKE_SEARCH_DEV_APP LUOKE_OUT="$W/stage" sh "{s}/scripts/build_helper.sh"\n'
                            f'test -e "$W/stage/luoke-search"', stage)


def build_mail(src: str, mem_src: str, stage: str) -> str:
    """螺壳邮件助手：scripts/build_for_luoke.sh，再用自带的 Python 连同钉住的 luoke_mem 导入它的服务（同 internal_build.build_mail）。"""
    job = Job("mail")
    core, s, mem = job.core(), job.source("luoke-mail", src), job.source("luoke-mem", mem_src)
    job.python_cache()
    return _stage_back(job, f'''LUOKE_OUT="$W/stage" sh "{s}/scripts/build_for_luoke.sh"
test -d "$W/stage/luoke_mail"
/usr/bin/python3 -B "{core}/tools/package_python_app.py" --manifest "{s}/packaging/accessory.json" --package "$W/stage/luoke_mail" \\
  --package "{mem}/luoke_mem" --out "$W/check" --sign none --check
py=$(ls -d "$W"/check/*.app/Contents/Resources/python/bin/python3 | head -1)
(cd / && env -i PATH=/usr/bin:/bin HOME="$W/check" LUOKE_MAIL_HOME="$W/check/home" PYTHONNOUSERSITE=1 "$py" -B -c "import luoke_mail.server")''', stage)


def build_scan(src: str, stage: str, team: str = "") -> str:
    """螺壳扫描的 scripts/package_app.py（同 internal_build.build_scan 的编那一步；tag、改动、版本的核对留在宿主机）。
    要病毒库：打包后的核对用这台机器的病毒库真扫一遍（scan#99）。"""
    job = Job("scan")
    s = job.source("luoke-scan", src)
    job.virus_db()
    team_env = f"LUOKE_RELEASE_TEAM={shlex.quote(team)} " if team else ""
    return _stage_back(job, f'mkdir -p "$W/stage"\n{team_env}LUOKE_SCAN_TOOLS=/opt/homebrew /usr/bin/python3 -B "{s}/scripts/package_app.py" '
                            f'--out "$W/stage" --tools-from /opt/homebrew', stage)
