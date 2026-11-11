#!/usr/bin/python3
"""把一个组件同步到它的公开仓库（顶层 ADR 0017 10-29 修订）：私有仓库 `<名>-private` 留一个 `public` 分支，和公开仓库的
`main` 是同一串提交。四个组件（core、mail、mem、ime）共用这一个脚本，各自在自己的仓库里跑：

  python3 <luoke-core>/tools/public_sync.py init                    第一次：本地 public 从公开 main 建（推到 origin 由人做）
  python3 <luoke-core>/tools/public_sync.py scan [--rev main]       只查泄漏：导出 rev 的公开树，列出命中，有命中退出码 1
  python3 <luoke-core>/tools/public_sync.py sync [--rev main] [--dry-run] [--push] [--base <公开提交> --base-from <私有提交>]

导出提交：公开 main 上由本脚本加的提交，作者和提交者是 Luoke Labs，说明最后一行是 `Exported-From: <导出自的私有提交>`。
只认提交者不够：有人用 Luoke Labs 身份直接在公开 main 上提交的改动，会被当成导出，下次同步悄悄删掉。

sync 依次做（任何一步不过就停，什么都不推）：
  1. 取公开仓库的 main（--public-url，默认按 origin 的名字去掉 -private）和 origin 的 public。
     origin/public 必须就是公开 main，或者是它的祖先（公开仓库里合了外部 PR，私有 public 还没快进）；
     反过来（私有 public 比公开 main 新）或者分叉了，停。
     本地 public 上可以是上次 sync 写下、还没推的导出提交；别的东西停。public 正检出着也停（--dry-run 除外）。
  2. 导出 rev 的公开树：rev 里有 tools/public_tree.py 的（luoke-core）用它导出；其余是 rev 的树去掉会话文件。
     导出工具从 rev 里读，不从工作区读。
  3. 泄漏检查 0 命中（导出的文件和提交说明都查）：通用的词（个人邮箱、本机路径、私有仓库名、内部仓库、称呼、令牌和私钥）
     + 本机词表（人名、机器名这类不能写进公开代码的；--patterns，默认 ~/.config/luoke/leak-patterns，一行一个正则，# 开头是注释）
     + rev 里 public_tree.check 的词（有的话）。
  4. 上一个导出提交：公开 main 第一父链上最近的那个。rev 必须是它导出自的私有提交的后代，
     不然停（导出旧提交或另一条线上的提交，公开仓库会悄悄退回去）。从 main 切的新公开线要接替发过补丁版的旧线，
     先在新线上 git merge -s ours <上次导出的私有提交>（不改内容，只记下接替）。
     第一次用本脚本同步时公开 main 上还没有导出提交：--base 给上一次导出的公开提交，--base-from 给它导出自的私有提交。
  5. 外部改动没被删掉：三方合并，一边是公开 main，一边是新导出的树；
     合并结果和新导出的树不一样，就是公开仓库里有改动还没 cherry-pick 回私有 main，停，列出文件。
     基准是上一个导出提交，再往后挪过已经 cherry-pick -x 搬回 rev 的外部提交（按 -x 留下的那一行认），
     所以搬回以后私有这边又改了同一处，不算没搬回。
  6. 在公开 main 上加一个导出提交，父提交就是公开 main（作者、提交者的名字和邮箱都查）。
     不加 --dry-run 时写到本地的 public 分支。
  7. 加了 --push（问过负责人以后）：先推公开 main，再推私有 public，都只快进。
     本地 public 上已经有这次要推的提交（上次不加 --push 写下的，树和导出自的提交都一样），就推它，不重建。
     第二步失败的话，下次 sync 的第 1 步会把私有 public 快进上去；在那之前出版机的检查也会挡住发版。

本脚本不写人名和机器名（它们在本机词表里），可以随公开代码导出。只用标准库。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

EXPORT_NAME = "Luoke Labs"
EXPORT_EMAIL = "zhixian@luoke.ai"
EXPORTED_FROM = re.compile(r"(?:\A|\n)Exported-From: ([0-9a-f]{40})\s*\Z")       # 只认说明的最后一行
PICKED = re.compile(r"\(cherry picked from commit ([0-9a-f]{40})\)")              # cherry-pick -x 留下的那一行
DEFAULT_PATTERNS = "~/.config/luoke/leak-patterns"
SESSION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "STATUS.md")
_OWNER_WORD = "\u4e3b\u4eba"        # 内部称呼；写成转义，免得这个文件自己命中
# luokelabs 下公开的仓库；链接指向别的（私有）仓库就算泄漏。新公开一个仓库要加进来，不然链到它会停
PUBLIC_REPOS = ("luoke", "luoke-agent", "luoke-atlas", "luoke-contribute", "luoke-core", "luoke-ime", "luoke-internal-builds",
                "luoke-mail", "luoke-mem", "luoke-releases", "luoke-scan", "luoke-search")   # agent、scan、search：用原名建公开仓库，私有的是 -private
_NOT_PUBLIC = r"(?!(?:" + "|".join(PUBLIC_REPOS) + r")(?:\.git)?(?![\w-]|\.\w))"     # 公开仓库名后面跟句号（句末）也算公开
GENERIC = [
    # 文档和测试里的示例地址（me@gmail.com、12345678@qq.com 这类）不算；QQ 号只放过 12345 到 1234567890 这几个
    ("个人邮箱", re.compile(r"(?<![\w.%+-])(?!(?:me|you|user|test|demo|new|work|home|[a-z]|12345(?:6(?:7(?:8(?:9(?:0)?)?)?)?)?)(?:(?:\+|%2B)\w+)?@)"
                         r"[A-Za-z0-9._%+-]+@(?:vip\.)?(?:gmail|qq|163|126|outlook|hotmail|icloud|me|foxmail|yahoo)\.com", re.I)),
    # 对外只用 10-30 定的公开地址；公司域名一律不出现
    ("内部邮箱", re.compile(r"(?<![\w.%+-])(?!" + re.escape(EXPORT_EMAIL) + r"\b)[A-Za-z0-9._%+-]+@(?:[\w-]+\.)*luoke\.ai\b|\blay2\.dev\b", re.I)),
    ("本机路径", re.compile(r"/Users/(?!(?:me|you|x|user|<[^>]*>)/)[A-Za-z0-9._-]+/")),
    # GitHub、GitLab 上的任何不公开的仓库；别的写法（裸的 luokelabs/x、API、raw 地址）只认 luoke 开头的，
    # 不然 Hugging Face 上公开的模型（huggingface.co/luokelabs/Qwen…）也会算
    ("私有仓库", re.compile(r"luoke-[a-z]+-priv" r"ate\b|(?:github|gitlab)\.com[/:]luokelabs/" + _NOT_PUBLIC + r"[\w.-]+"
                         r"|(?<![\w.-])luokelabs/" + _NOT_PUBLIC + r"luoke[\w.-]*", re.I)),
    ("内部仓库", re.compile(r"luoke-(?:ops|meetings)\b")),
    ("内部称呼", re.compile(_OWNER_WORD)),
    # GitHub、GitLab、Hugging Face、Anthropic、OpenAI、AWS（AWS 文档里的示例和测试里常用的假号不算）
    ("令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bglpat-[A-Za-z0-9_-]{20,}|\bhf_[A-Za-z0-9]{30,}"
                      r"|\bsk-ant-[A-Za-z0-9_-]{20,}|\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{40,}"
                      r"|\bAKIA(?!IOSFODNN7EXAMPLE|ABCDEFGHIJKLMNOP)[0-9A-Z]{16}\b")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
# 第三方的代码和许可文本不查（作者名里难免有和团队成员同名的）
SKIP = re.compile(r"(^|/)(vendor|third_party|licenses|runtime-licenses)/")


class Stop(Exception):
    pass


def git(repo: str, *args: str, input: bytes = None, env: dict = None, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], input=input, capture_output=True, env=env)
    if check and r.returncode != 0:
        raise Stop(f"git {' '.join(args[:3])}：{r.stderr.decode(errors='replace').strip()[-400:]}")
    return r.stdout.decode(errors="replace").strip()


def ok(repo: str, *args: str) -> bool:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True).returncode == 0


# ---- 导出 -----------------------------------------------------------------------------------------------------

def tree_files(repo: str, treeish: str) -> dict:
    """treeish 里的文件 → (内容, 模式)。符号链接照样带上（内容是目标路径）。"""
    out = {}
    listing = git(repo, "ls-tree", "-r", "-z", treeish).split("\0")
    entries = []
    for line in filter(None, listing):
        meta, path = line.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob":
            entries.append((path, int(mode, 8), sha))
    if not entries:
        return out
    r = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input="".join(s + "\n" for _, _, s in entries).encode(),
                       capture_output=True, check=True).stdout
    pos = 0
    for path, mode, _ in entries:
        nl = r.index(b"\n", pos)
        size = int(r[pos:nl].split()[2])
        out[path] = (r[nl + 1:nl + 1 + size], mode)
        pos = nl + 1 + size + 1
    return out


def repo_public_tree(repo: str, rev: str):
    """rev 里的导出工具（luoke-core 的 tools/public_tree.py）；没有就是 None。
    从 rev 读，不从工作区读：工作区检出的是别的分支，或者这个文件改了没提交，导出的就不是按 rev 的规则了。"""
    path = "tools/public_tree.py"
    if not ok(repo, "cat-file", "-e", f"{rev}:{path}"):
        return None
    import types
    m = types.ModuleType("public_tree_of_rev")
    m.__file__ = os.path.join(repo, path)
    exec(compile(git(repo, "show", f"{rev}:{path}"), f"{rev}:{path}", "exec"), m.__dict__)
    return m


def export(repo: str, rev: str) -> dict:
    pt = repo_public_tree(repo, rev)
    files = tree_files(repo, rev)
    if pt is not None:
        return pt.public(files)
    return {p: v for p, v in files.items() if os.path.basename(p) not in SESSION_FILES}


def write_tree(repo: str, files: dict) -> str:
    """把导出的文件写成 git 的树对象（不碰工作区和索引），返回树的 id。"""
    tmp = tempfile.mkdtemp(prefix="public-sync-")
    try:
        index = os.path.join(tmp, "index")
        env = dict(os.environ, GIT_INDEX_FILE=index)
        lines = []
        shas = git(repo, "hash-object", "-w", "--stdin-paths", "--no-filters",
                   input=_spill(tmp, files)).splitlines()
        for (path, (_, mode)), sha in zip(sorted(files.items()), shas):
            m = "120000" if (mode & 0o170000) == 0o120000 else ("100755" if mode & 0o111 else "100644")
            lines.append(f"{m} {sha}\t{path}")
        git(repo, "update-index", "--add", "--index-info", input=("\n".join(lines) + "\n").encode(), env=env)
        return git(repo, "write-tree", env=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _spill(tmp: str, files: dict) -> bytes:
    paths = []
    for i, (path, (data, _)) in enumerate(sorted(files.items())):
        p = os.path.join(tmp, f"f{i}")
        with open(p, "wb") as f:
            f.write(data)
        paths.append(p)
    return ("\n".join(paths) + "\n").encode()


# ---- 泄漏检查 -------------------------------------------------------------------------------------------------

def load_patterns(path: str, required: bool = True) -> list:
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        if required:
            raise Stop(f"没有本机词表 {path}：人名、机器名这类一行一个正则写在那里（文件 600，不进任何仓库）")
        return []
    pats = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if line.strip() and not line.lstrip().startswith("#"):
                try:
                    pats.append((f"本机词表第 {n} 行", re.compile(line, re.I)))
                except re.error as e:
                    raise Stop(f"{path} 第 {n} 行不是正则：{e}")
    return pats


def scan(files: dict, patterns: list, pt=None, allow: tuple = ()) -> list:
    """[(路径, 行号, 哪条规则, 那一行)]；二进制文件只查路径。pt 是仓库自己的导出工具（repo_public_tree），它的词表也查。
    allow 里的路径不查（比如仓库自己旧的扫描脚本，里面写着要查的词）。"""
    rules = GENERIC + patterns
    hits = []
    for path in sorted(files):
        if SKIP.search(path) or path in allow:
            continue
        for why, pat in rules:
            if pat.search(path):
                hits.append((path, 0, why, "（路径）"))
        data = files[path][0]
        if b"\0" in data[:8000]:
            continue
        try:
            texts = [data.decode()]
        except UnicodeDecodeError:      # 不是 UTF-8 的文本（比如 GBK 写的中文）两种都查：UTF-8 读法保住 ASCII，GB18030 读法保住中文
            texts = [data.decode(errors="replace"), data.decode("gb18030", errors="replace")]
        found = set()
        for text in texts:
            for n, line in enumerate(text.splitlines(), 1):
                if n in found:
                    continue
                for why, pat in rules:
                    if pat.search(line):
                        hits.append((path, n, why, line.strip()[:160]))
                        found.add(n)
                        break
    if pt is not None:        # luoke-core 自己的词表照样查（它写在不导出的 public_tree.py 里）
        seen = {(p, n) for p, n, _, _ in hits}
        hits += [(p, n, "public_tree", l) for p, n, l in pt.check(files)[0] if (p, n) not in seen and p not in allow]
    return hits


BINARY_HOME = re.compile(rb"/Users/([A-Za-z0-9._-]+)/")
# 安装包里不是我们写的：自带的 Python 运行时（连同它带的 Tcl/Tk）、第三方包、许可文本
PACKAGE_SKIP = re.compile(r"/Resources/(licenses|runtime-licenses)/|/Resources/python/(bin|include|share)/|/Resources/python/lib/(?!python3\.\d+/site-packages/)"
                          r"|/site-packages/(?!luoke_)[^/]+/|/vendor/")


def scan_dir(root: str, patterns: list, skip=PACKAGE_SKIP) -> tuple:
    """目录（比如打好的 .app）里的 (命中, 提示)；给出版流水线查安装包用。
    文本文件照 scan 查。二进制文件（编好的库和程序）查编译机的本机路径 /Users/<名>/（10-30 输入法的 librime 带着开发机的路径进了安装包）：
    名字对得上本机词表的算命中，其余（出版账户、第三方运行时的编译机）只提示。"""
    files, hits, notes = {}, [], []
    for d, _, names in os.walk(root):
        for n in names:
            full = os.path.join(d, n)
            rel = os.path.relpath(full, root)
            if not os.path.isfile(full) or os.path.islink(full) or (skip and skip.search("/" + rel)):
                continue
            with open(full, "rb") as f:
                data = f.read()
            if b"\0" not in data[:8000]:
                files[rel] = (data, 0o644)
                continue
            for user in sorted({m.decode() for m in BINARY_HOME.findall(data)} - {"Shared"}):
                bad = any(p.search(user) for _, p in patterns)
                (hits if bad else notes).append((rel, 0, "二进制里的本机路径", f"/Users/{user}/"))
    return hits + scan(files, patterns), notes


# ---- 同步 -----------------------------------------------------------------------------------------------------

def component_name(repo: str) -> str:
    url = git(repo, "remote", "get-url", "origin")
    name = re.sub(r"\.git$", "", url.rstrip("/")).rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return re.sub(r"-priv" r"ate$", "", name)


def identity_ok(repo: str, commit: str) -> list:
    """提交的作者和提交者（名字、邮箱）不是 Luoke Labs 的，列出来。"""
    a = git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", commit).split("\0")
    bad = []
    if (a[0], a[1]) != (EXPORT_NAME, EXPORT_EMAIL):
        bad.append(f"作者是 {a[0]} <{a[1]}>")
    if (a[2], a[3]) != (EXPORT_NAME, EXPORT_EMAIL):
        bad.append(f"提交者是 {a[2]} <{a[3]}>")
    return bad


def last_export(repo: str, head: str) -> tuple:
    """head 的第一父链上最近一个导出提交和它导出自的私有提交：(公开提交, 私有提交)；没有就是 (None, None)。
    导出提交要同时有 Exported-From 尾注、提交者是 Luoke Labs（GitHub 上压缩合并的外部 PR 说明里就算有这一行，提交者也是 GitHub）。"""
    for rec in git(repo, "log", "--first-parent", "--format=%H%x00%cn%x00%ce%x00%B%x1e", head).split("\x1e"):
        if not rec.strip():
            continue
        sha, cn, ce, body = rec.strip("\n").split("\0", 3)
        m = EXPORTED_FROM.search(body)
        if m and (cn, ce) == (EXPORT_NAME, EXPORT_EMAIL):
            return sha, m.group(1)
    return None, None


def carried_back(repo: str, last: str, last_src: str, public_main: str, src: str) -> str:
    """上一个导出提交之后公开 main 第一父链上的提交，从前往后看已经 cherry-pick -x 搬回 src 的（src 在 last_src 之后的历史里
    有它的「cherry picked from」那一行；GitHub 的合并提交整个搬回，或者 PR 里的提交一个个搬回都算），到第一个没搬回的为止，
    返回最后一个搬回的；一个都没有就是 last。三方合并拿它当基准：搬回以后私有这边又改了同一处，不再算成没搬回。"""
    picked = set(PICKED.findall(git(repo, "log", "--format=%B", f"{last_src}..{src}")))
    base = last
    for c in git(repo, "rev-list", "--first-parent", "--reverse", f"{last}..{public_main}").split():
        parents = git(repo, "show", "-s", "--format=%P", c).split()
        pr = set(git(repo, "rev-list", "--no-merges", f"{parents[0]}..{c}").split()) if len(parents) > 1 else set()
        if c not in picked and not (pr and pr <= picked):
            break
        base = c
    return base


def external_changes(repo: str, base: str, public_main: str, candidate: str, new_tree: str) -> list:
    """三方合并（基准是上一个导出提交）：结果和新导出的树不一样的文件，就是公开仓库里还没搬回私有 main 的改动。"""
    r = subprocess.run(["git", "-C", repo, "merge-tree", "--write-tree", "--name-only", f"--merge-base={base}", public_main, candidate],
                       capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise Stop(f"git merge-tree：{r.stderr.strip()}")
    lines = r.stdout.splitlines()
    if r.returncode == 1:
        names = lines[1:lines.index("")] if "" in lines else lines[1:]
        files = sorted(set(names))
        return [f"{f}（合并冲突）" for f in files] or ["（合并冲突）"]
    merged = lines[0]
    if merged == new_tree:
        return []
    return git(repo, "diff", "--name-only", new_tree, merged).splitlines()


def sync(repo: str, rev: str, public_url: str, push_url: str, patterns: list, dry_run: bool, push: bool, message: str = None,
         allow: tuple = (), base: str = None, base_from: str = None) -> str:
    name = component_name(repo)
    say = lambda s: print(s, flush=True)
    if bool(base) != bool(base_from):
        raise Stop("--base 和 --base-from 要一起给：上一次导出的公开提交，和它导出自的私有提交")
    if message and re.search(r"(?im)^\s*exported-from:", message):
        raise Stop("--message 里不能有 Exported-From：这一行由脚本加")
    if not dry_run and git(repo, "symbolic-ref", "-q", "HEAD", check=False) == "refs/heads/public":
        raise Stop("public 分支正检出着，sync 要挪动它：先切到别的分支（看导出的提交用 git show public）")
    # 1. 两边的现状
    git(repo, "fetch", "-q", public_url, f"+refs/heads/main:refs/public-sync/{name}/main")
    pub_main = git(repo, "rev-parse", f"refs/public-sync/{name}/main")
    if not git(repo, "ls-remote", "origin", "refs/heads/public"):
        raise Stop("origin 上还没有 public 分支：先跑 init")
    git(repo, "fetch", "-q", "origin", "+refs/heads/public:refs/remotes/origin/public")
    priv_public = git(repo, "rev-parse", "refs/remotes/origin/public")
    if priv_public != pub_main:
        if ok(repo, "merge-base", "--is-ancestor", priv_public, pub_main):
            say(f"公开 main {pub_main[:7]} 比私有 public {priv_public[:7]} 新（外部 PR）：新提交接在公开 main 上，推的时候私有 public 一起快进")
        elif ok(repo, "merge-base", "--is-ancestor", pub_main, priv_public):
            raise Stop(f"私有 public {priv_public[:7]} 比公开 main {pub_main[:7]} 新：上次只推了私有一边？先查清楚，公开 main 不能落后")
        else:
            raise Stop(f"私有 public {priv_public[:7]} 和公开 main {pub_main[:7]} 分叉了")
    local = git(repo, "rev-parse", "-q", "--verify", "refs/heads/public", check=False)
    unpushed = None       # 上次不加 --push 的 sync 写下的导出提交（父提交是公开 main，或者公开 main 的祖先）
    if local and local not in (pub_main, priv_public) and not ok(repo, "merge-base", "--is-ancestor", local, pub_main):
        parents = git(repo, "show", "-s", "--format=%P", local).split()
        if len(parents) == 1 and last_export(repo, local)[0] == local and ok(repo, "merge-base", "--is-ancestor", parents[0], pub_main):
            unpushed = local
        else:
            raise Stop(f"本地 public {local[:7]} 不在公开 main 上，也不是上次 sync 写下的导出提交：确认不要了就 git branch -f public {pub_main[:7]}")
    last, last_src = last_export(repo, pub_main)
    if last and base:
        raise Stop(f"公开 main 上已有导出提交 {last[:7]}（导出自 {last_src[:7]}），不用 --base")
    # 2–3. 导出、查泄漏（导出规则和词表都按 rev 里的）
    src = git(repo, "rev-parse", f"{rev}^{{commit}}")
    pt = repo_public_tree(repo, src)
    files = export(repo, src)
    hits = scan(files, patterns, pt, allow)
    if hits:
        for p, n, why, line in hits[:200]:
            say(f"  {p}:{n}: [{why}] {line}")
        raise Stop(f"泄漏检查：{len(hits)} 行命中（{len({h[0] for h in hits})} 个文件），先在 {rev} 上清掉")
    say(f"导出 {name} {src[:7]}：{len(files)} 个文件，泄漏检查 0 命中")
    new_tree = write_tree(repo, files)
    if new_tree == git(repo, "rev-parse", f"{pub_main}^{{tree}}"):
        say("公开 main 已经是这棵树，不用同步")
        if priv_public != pub_main:
            if push:
                git(repo, "push", "-q", "origin", f"{pub_main}:refs/heads/public")
                say(f"私有 public 快进到公开 main {pub_main[:7]}")
            else:
                say(f"私有 public 还在 {priv_public[:7]}，落后公开 main：加 --push 快进")
        return pub_main
    # 4. 上一个导出提交；rev 要接在它导出自的私有提交后面
    if not last:
        if not base:
            raise Stop(f"公开 main {pub_main[:7]} 上还没有导出提交（第一次用本脚本同步）："
                       "用 --base <上一次导出的公开提交> --base-from <它导出自的私有提交> 指定")
        last = git(repo, "rev-parse", "-q", "--verify", f"{base}^{{commit}}", check=False)
        if not last or not ok(repo, "merge-base", "--is-ancestor", last, pub_main):
            raise Stop(f"--base {base} 不在公开 main {pub_main[:7]} 上")
        last_src = git(repo, "rev-parse", "-q", "--verify", f"{base_from}^{{commit}}", check=False)
        if not last_src:
            raise Stop(f"私有仓库里没有 --base-from {base_from}")
        differ = git(repo, "diff", "--name-only", write_tree(repo, export(repo, last_src)), last).splitlines()
        if differ:
            say(f"提示：{base_from} 现在导出的树和公开 {last[:7]} 有 {len(differ)} 个文件不同（{'、'.join(differ[:8])}）："
                "以前的导出脚本规则不一样的话是正常的，不然查一下 --base-from 给对没有")
    elif not ok(repo, "cat-file", "-e", f"{last_src}^{{commit}}"):
        raise Stop(f"私有仓库里没有上次导出自的提交 {last_src[:7]}：先 git fetch origin；还没有就是私有历史被改写过")
    if ok(repo, "merge-base", "--is-ancestor", src, last_src) and src != last_src:
        raise Stop(f"{rev} {src[:7]} 比上次导出的私有提交 {last_src[:7]} 旧：导出它，公开仓库会退回去")
    if not ok(repo, "merge-base", "--is-ancestor", last_src, src):
        # 那条线上、rev 里没有的提交（按改动认：从 main 摘过去的修复，main 上有同样的改动就算有）
        lost = git(repo, "rev-list", "--cherry-pick", "--right-only", "--no-merges", f"{src}...{last_src}").split()
        if lost:
            raise Stop(f"{rev} {src[:7]} 不是上次导出的私有提交 {last_src[:7]} 的后代，那条线上有 {len(lost)} 个提交 {rev} 里没有"
                       f"（{'、'.join(c[:7] for c in lost[:10])}{'…' if len(lost) > 10 else ''}），导出会把它们从公开仓库拿掉：先把它们合进 {rev}")
        raise Stop(f"{rev} {src[:7]} 不是上次导出的私有提交 {last_src[:7]} 的后代。那条线上的改动 {rev} 里都有，"
                   f"{rev} 是要接替那条线的新公开线的话，先在 {rev} 上 git merge -s ours {last_src[:7]}（不改内容，只记下接替）再同步")
    # 5–6. 新提交，查外部改动和身份
    env = dict(os.environ, GIT_AUTHOR_NAME=EXPORT_NAME, GIT_AUTHOR_EMAIL=EXPORT_EMAIL,
               GIT_COMMITTER_NAME=EXPORT_NAME, GIT_COMMITTER_EMAIL=EXPORT_EMAIL)
    msg = (message or f"{name}：同步私有 {rev} {src[:7]}").strip() + f"\n\nExported-From: {src}"
    msg_hits = scan({"（提交说明）": (msg.encode(), 0o644)}, patterns, pt)
    if msg_hits:
        raise Stop("提交说明里有泄漏词：" + "；".join(f"[{why}] {line}" for _, _, why, line in msg_hits))
    if unpushed and git(repo, "show", "-s", "--format=%P%n%T", unpushed).split() == [pub_main, new_tree] \
            and git(repo, "show", "-s", "--format=%B", unpushed) == msg:
        candidate = unpushed      # 推上次写下、给人看过的那个（重建的话时间不同，提交号会变）
        say(f"用本地 public 上次写下的 {unpushed[:7]}")
    else:
        if unpushed:
            say(f"本地 public 上次写下的 {unpushed[:7]} 过时了（公开 main、导出的树或说明变了），换成新的")
        candidate = git(repo, "commit-tree", new_tree, "-p", pub_main, "-m", msg, env=env)
    merge_base = carried_back(repo, last, last_src, pub_main, src)
    lost = external_changes(repo, merge_base, pub_main, candidate, new_tree)
    if lost:
        raise Stop("公开仓库里有改动还没搬回私有 main（cherry-pick -x 进 main 再同步；搬没搬回按 -x 留下的那一行认）：\n  "
                   + "\n  ".join(lost[:50]))
    bad = identity_ok(repo, candidate)
    parents = git(repo, "show", "-s", "--format=%P", candidate).split()
    if bad or parents != [pub_main]:
        raise Stop("新提交不对：" + "；".join(bad + ([] if parents == [pub_main] else [f"父提交是 {parents}"])))
    stat = git(repo, "diff", "--shortstat", pub_main, candidate)
    moved = "" if merge_base == last else f"，搬回到 {merge_base[:7]}"
    say(f"新提交 {candidate[:7]}（父 {pub_main[:7]}，上次导出 {last[:7]} 导出自 {last_src[:7]}{moved}，作者和提交者 {EXPORT_NAME}）：{stat}")
    if dry_run:
        say("--dry-run：没写 public 分支")
        return candidate
    git(repo, "update-ref", "refs/heads/public", candidate)
    if not push:
        say("已写到本地 public。问过负责人以后加 --push 再跑一次（会先推公开 main，再推私有 public）")
        return candidate
    # 6. 推：先公开，再私有；推之前再确认公开 main 没动过
    git(repo, "fetch", "-q", public_url, f"+refs/heads/main:refs/public-sync/{name}/main")
    if git(repo, "rev-parse", f"refs/public-sync/{name}/main") != pub_main:
        raise Stop("公开 main 刚才又变了：重跑 sync")
    git(repo, "push", "-q", push_url, f"{candidate}:refs/heads/main")
    say(f"公开 main → {candidate[:7]}")
    try:
        git(repo, "push", "-q", "origin", f"{candidate}:refs/heads/public")
        say(f"私有 public → {candidate[:7]}")
    except Stop as e:
        say(f"私有 public 没推上（{e}）：公开已经推了，下次 sync 第 1 步会把私有 public 快进上去；要补就 git push origin {candidate[:7]}:public")
    return candidate


def init(repo: str, public_url: str) -> str:
    """第一次：本地 public 分支从公开 main 建（不加任何代码，提交号和公开仓库一样）。推到 origin 由人来做。"""
    name = component_name(repo)
    git(repo, "fetch", "-q", public_url, f"+refs/heads/main:refs/public-sync/{name}/main")
    pub_main = git(repo, "rev-parse", f"refs/public-sync/{name}/main")
    remote = git(repo, "ls-remote", "origin", "refs/heads/public").split()
    if remote:
        if remote[0] != pub_main:
            raise Stop(f"origin 已有 public {remote[0][:7]}，不是公开 main {pub_main[:7]}")
        print(f"origin 的 public 已经是公开 main {pub_main[:7]}")
        return pub_main
    git(repo, "update-ref", "refs/heads/public", pub_main, "")
    print(f"本地 public → {pub_main[:7]}（公开 main）。问过负责人以后：git push origin public")
    return pub_main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="组件同步到公开仓库（ADR 0017 10-29 修订）")
    ap.add_argument("cmd", choices=["init", "scan", "sync"])
    ap.add_argument("--repo", default=".", help="私有仓库的工作目录（默认当前目录）")
    ap.add_argument("--rev", default="main")
    ap.add_argument("--patterns", default=os.environ.get("LUOKE_LEAK_PATTERNS", DEFAULT_PATTERNS))
    ap.add_argument("--public-url", help="取公开 main 的地址（默认 https://github.com/luokelabs/<名>.git）")
    ap.add_argument("--public-push-url", help="推公开 main 的地址（默认 git@github.com:luokelabs/<名>.git）")
    ap.add_argument("--message", help="提交说明（默认「<名>：同步私有 <rev> <提交号>」；后面总会加 Exported-From 尾注）")
    ap.add_argument("--base", help="第一次用本脚本同步（公开 main 上还没有导出提交）：上一次导出的公开提交")
    ap.add_argument("--base-from", help="和 --base 一起：它导出自的私有提交")
    ap.add_argument("--allow-path", action="append", default=[], help="这个路径不查泄漏（可以写几次）；只给写着词表的扫描脚本用")
    ap.add_argument("--dry-run", action="store_true", help="只检查、建提交对象，不写 public 分支")
    ap.add_argument("--push", action="store_true", help="问过负责人以后：推公开 main，再推私有 public")
    a = ap.parse_args(argv)
    try:
        repo = git(os.path.abspath(a.repo), "rev-parse", "--show-toplevel")
        patterns = load_patterns(a.patterns) if a.cmd != "init" else []
        if a.cmd == "scan":
            files = export(repo, a.rev)
            hits = scan(files, patterns, repo_public_tree(repo, a.rev), tuple(a.allow_path))
            for p, n, why, line in hits:
                print(f"{p}:{n}: [{why}] {line}")
            print(f"{a.rev}：导出 {len(files)} 个文件，命中 {len(hits)} 行（{len({h[0] for h in hits})} 个文件）", file=sys.stderr)
            return 1 if hits else 0
        if a.push and a.dry_run:
            raise Stop("--push 和 --dry-run 不能一起用")
        name = component_name(repo)
        if a.cmd == "init":
            init(repo, a.public_url or f"https://github.com/luokelabs/{name}.git")
            return 0
        sync(repo, a.rev, a.public_url or f"https://github.com/luokelabs/{name}.git",
             a.public_push_url or f"git@github.com:luokelabs/{name}.git", patterns, a.dry_run, a.push, a.message, tuple(a.allow_path),
             a.base, a.base_from)
        return 0
    except Stop as e:
        print(f"停：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
