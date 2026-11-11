"""扫描随包带的 ClamAV（GPLv2）的源码怎么随 beta 发布（11-03 定）。

某个 ClamAV 版本第一次出现在 luoke-releases 的 beta 里时，这一版附上：上游源码包 clamav-<版本>.tar.gz、那次构建用的
Homebrew 配方 clamav-<版本_修订>.rb（装好的 keg 里 .brew/clamav.rb 那一份）、配方编译前打的每个补丁
clamav-<版本_修订>-patch<序号>-<补丁名>.patch。源码包和补丁的 sha256 都对过配方。之后带同一个 ClamAV 的 beta 不再附，发布说明里链回第一次附的那一版。不另建一个放源码的发布：它会成了 Latest，官网的
releases/latest/download/Luoke.dmg 和 GitLab 的 latest 链接就坏了。所以附着源码的 beta 不能删。

各版本附在哪，写在 luoke-releases main 上的 licenses/clamav/README.md（INDEX_PATH），由流水线维护；扫描的 THIRD-PARTY.md 链到它。
内部版不管（发到 luoke-internal-builds 的不走这里）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from typing import Callable, Optional
from urllib.parse import urlparse

INDEX_PATH = "licenses/clamav/README.md"
INDEX_URL = "https://github.com/luokelabs/luoke-releases/blob/main/" + INDEX_PATH


class SourceError(Exception):
    """源码凑不齐或对不上：带扫描的 beta 不能发。"""


def clamav_version(app: str) -> Optional[str]:
    """扫描带的 ClamAV 是 Homebrew 的哪个版本（含修订号，比如 1.5.4_2）；没带 ClamAV 就 None。读扫描的打包脚本写的
    bundled-tools.json（每个文件、来自哪个配方、哪个版本）：0.3.0 起扫描打进螺壳.app，app 是扫描编出来的 luoke-scan/
    （Resources/scan/ 下）或螺壳.app（Contents/Resources/scan/ 下）；以前单独的 .app 在 Contents/Resources/ 下。"""
    for path in (os.path.join(app, "Resources", "scan", "bundled-tools.json"), os.path.join(app, "Contents", "Resources", "scan", "bundled-tools.json"),
                 os.path.join(app, "Contents", "Resources", "bundled-tools.json")):
        if os.path.isfile(path):
            break
    try:
        with open(path, encoding="utf-8") as f:
            tools = json.load(f)
    except (OSError, ValueError) as e:
        raise SourceError(f"读不了 {path}（{e}）：说不清包里的 ClamAV 是哪个版本")
    if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
        raise SourceError(f"{path} 不是一列文件：说不清包里的 ClamAV 是哪个版本")
    versions = {t.get("version") for t in tools if t.get("formula") == "clamav"}
    if len(versions) > 1 or None in versions or "" in versions:
        raise SourceError(f"{path} 里的 ClamAV 版本不止一个或是空的：{sorted(str(v) for v in versions)}")
    return versions.pop() if versions else None


def asset_names(version: str) -> tuple:
    """(源码包, 配方) 附在发布上的文件名：clamav-1.5.4.tar.gz、clamav-1.5.4_2.rb。"""
    return f"clamav-{version.split('_')[0]}.tar.gz", f"clamav-{version}.rb"


def patch_name(version: str, n: int, url: str) -> str:
    """配方里第 n 个补丁（从 1 数）附在发布上的文件名，比如 clamav-1.5.4_2-patch1-0097a6f99d9f.patch（上游提交号取前 12 位）。"""
    stem = re.sub(r"\.(patch|diff)$", "", os.path.basename(urlparse(url).path))
    return f"clamav-{version}-patch{n}-{re.sub(r'[^A-Za-z0-9._-]', '-', stem)[:12]}.patch"


def _without_head(text: str) -> str:
    """去掉 head do … end 那块（只有从 head 构建才用，里面的 url、补丁都不是稳定版的）。"""
    return re.sub(r"^([ \t]*)head do\n.*?^\1end\b", "", text, flags=re.S | re.M)


def formula_source(text: str) -> tuple:
    """配方里稳定版源码包的 (url, sha256)：去掉 head 块以后第一个 url 和它后面第一个 sha256（head 写成一行时那行不是 url）。"""
    text = _without_head(text)
    u = re.search(r'^\s*url\s+"([^"]+)"', text, re.M)
    s = re.search(r'^\s*sha256\s+"([0-9a-f]{64})"', text[u.end():], re.M) if u else None
    if not u or not s:
        raise SourceError("配方里找不到稳定版源码包的 url 和 sha256")
    return u.group(1), s.group(1)


def formula_patches(text: str) -> list:
    """配方编译前打的补丁 [(url, sha256)]，照配方里的顺序。head 那块不算（只有从 head 构建才打）；
    patch :DATA 的补丁就写在配方文件里，配方附上了就不用另附。看不懂的写法、额外的 resource 一律停，不能漏附。"""
    text = _without_head(text)
    if re.search(r"^\s*resource\b", text, re.M):
        raise SourceError("配方里有 resource（另一份源码）：不知道该怎么附，先停下看配方")
    out = []
    for m in re.finditer(r"^([ \t]*)patch\b([^\n]*)$", text, re.M):
        rest = m.group(2).strip()
        if ":DATA" in rest:
            continue
        body = re.search(r"\A(.*?)^" + re.escape(m.group(1)) + r"end\b", text[m.end():], re.S | re.M) if rest.endswith("do") else None
        u = body and re.search(r'^\s*url\s+"([^"]+)"', body.group(1), re.M)
        s = body and re.search(r'^\s*sha256\s+"([0-9a-f]{64})"', body.group(1), re.M)
        if not u or not s:
            raise SourceError(f"配方里有看不懂的补丁：patch {rest}")
        out.append((u.group(1), s.group(1)))
    return out


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetched(url: str, want: str, dst: str, fetch: Callable[[str, str], None]) -> str:
    """dst 在且 sha256 对得上就用它；否则照 url 下载（先下到临时文件，对上了才换过去）。"""
    if os.path.exists(dst) and sha256_of(dst) == want:
        return dst
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=".download-")
    os.close(fd)
    try:
        fetch(url, tmp)
        got = sha256_of(tmp)
        if got != want:
            raise SourceError(f"下载的 {os.path.basename(dst)} sha256 是 {got}，配方写的是 {want}")
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dst


def ensure_source(version: str, tools: str, cache: str, fetch: Callable[[str, str], None]) -> list:
    """出版机上留一份这个版本的源码，返回 [源码包, 配方, 补丁…] 的路径。配方从 <tools>/Cellar/clamav/<版本>/.brew/clamav.rb 拷；
    源码包和补丁照配方的 url 下载（fetch(url, 存到哪)），sha256 和配方对不上就不用。每次都重新核对。"""
    tarball_name, formula_name = asset_names(version)
    d = os.path.join(cache, version)
    os.makedirs(d, exist_ok=True)
    formula = os.path.join(d, formula_name)
    keg = os.path.join(tools, "Cellar", "clamav", version, ".brew", "clamav.rb")
    if os.path.exists(keg):
        if not os.path.exists(formula) or sha256_of(keg) != sha256_of(formula):
            shutil.copy2(keg, formula)        # 同一个版本重装过、配方变了：以这次构建用的 keg 为准
    elif not os.path.exists(formula):
        raise SourceError(f"{keg} 不在：拿不到 ClamAV {version} 构建用的 Homebrew 配方")
    with open(formula, encoding="utf-8") as f:
        url, want = formula_source(f.read())
    if os.path.basename(url) != tarball_name:
        raise SourceError(f"配方的源码包是 {os.path.basename(url)}，不是 {tarball_name}：版本对不上")
    with open(formula, encoding="utf-8") as f:
        patches = formula_patches(f.read())
    files = [_fetched(url, want, os.path.join(d, tarball_name), fetch), formula]
    for n, (purl, psha) in enumerate(patches, 1):
        files.append(_fetched(purl, psha, os.path.join(d, patch_name(version, n, purl)), fetch))
    return files


def published(releases: list, required: Optional[dict] = None) -> dict:
    """luoke-releases 已经附过源码的 ClamAV 版本 → 附全了的第一版（同一版本附过几次时取补丁最多的，一样多取最早的）：{版本: {tag, page, tarball, formula, patches}}，
    patches 是 [(文件名, 地址)]。releases 是 GitHub 发布列表（tag_name、html_url、published_at、draft、
    assets[{name, browser_download_url}]）。required 给了某个版本必须都在的文件名（源码包、配方、补丁），缺一个那一版就不算；
    没给的版本源码包和配方都在就算。"""
    required, found = required or {}, {}
    for r in sorted((r for r in releases if not r.get("draft")), key=lambda r: r.get("published_at") or ""):
        links = {a["name"]: a["browser_download_url"] for a in r.get("assets") or []}
        for name in links:
            m = re.fullmatch(r"clamav-(.+)\.rb", name)
            if not m:
                continue
            v = m.group(1)
            tarball_name = asset_names(v)[0]
            if not all(n in links for n in required.get(v) or (tarball_name, name)):
                continue
            patches = {int(p.group(1)): p.group(0) for p in (re.fullmatch(rf"clamav-{re.escape(v)}-patch(\d+)-.*\.patch", n) for n in links) if p}
            row = {"tag": r["tag_name"], "page": r["html_url"], "tarball": links[tarball_name], "formula": links[name],
                   "patches": [(patches[k], links[patches[k]]) for k in sorted(patches)]}
            # 早先那一版缺补丁、后来又附全了：索引指后来那一版（不然换了 ClamAV 版本重写索引时，旧版本那一行会指回缺补丁的）
            if v not in found or len(row["patches"]) > len(found[v]["patches"]):
                found[v] = row
    return found


def planned(repo: str, tag: str, version: str, patches: tuple = ()) -> dict:
    """这一版要附源码时，它在索引里的那一行（发布还没建，地址按 GitHub 的规矩先写出来）。patches 是补丁的文件名。"""
    base = f"https://github.com/{repo}/releases"
    tarball_name, formula_name = asset_names(version)
    return {"tag": tag, "page": f"{base}/tag/{tag}", "tarball": f"{base}/download/{tag}/{tarball_name}",
            "formula": f"{base}/download/{tag}/{formula_name}", "patches": [(n, f"{base}/download/{tag}/{n}") for n in patches]}


def notes_line(version: str, attach: bool, where: Optional[dict], patches: int = 0) -> str:
    upstream = version.split("_")[0]
    if attach:
        extra = f"、配方编译前打的 {patches} 个补丁（clamav-{version}-patch*.patch）" if patches else ""
        return (f"螺壳扫描带的 ClamAV {upstream}（GPLv2）：完整源码 clamav-{upstream}.tar.gz、这次构建用的 Homebrew 配方 "
                f"clamav-{version}.rb{extra}附在本页。各版本的源码在哪：{INDEX_URL}")
    return (f"螺壳扫描带的 ClamAV {upstream}（GPLv2）的源码、Homebrew 配方和补丁附在 {where['tag']}：{where['page']}。"
            f"各版本的源码在哪：{INDEX_URL}")


def render_index(found: dict) -> str:
    lines = ["# ClamAV 的源码", "",
             "螺壳扫描（luoke-scan）带着 [ClamAV](https://www.clamav.net/)（GPLv2）的程序和库，从 Homebrew 装好的版本原样拷进去。"
             "每个 ClamAV 版本第一次随 beta 发布时，那一版附上完整的上游源码包、那次构建用的 Homebrew 配方"
             "和配方编译前打的每个上游补丁（配方里写着它们的 sha256）。之后带同一个 ClamAV 的版本在发布说明里链回这里列的那一版。", "",
             "| ClamAV（Homebrew 版本） | 附在哪一版 | 源码包 | 配方 | 补丁 |", "|---|---|---|---|---|"]
    for v in sorted(found, key=lambda v: [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"[._]", v)]):
        w = found[v]
        tarball_name, formula_name = asset_names(v)
        patches = "<br>".join(f"[{n}]({u})" for n, u in w.get("patches") or []) or "无"
        lines.append(f"| {v} | [{w['tag']}]({w['page']}) | [{tarball_name}]({w['tarball']}) | [{formula_name}]({w['formula']}) | {patches} |")
    lines += ["", "这些发布不会删：附着源码的版本至少保留到最后一次分发这个 ClamAV 版本之后三年。", "",
              "这个文件由出版流水线在发 beta 时更新（luoke-core 的 `tools/clamav_source.py`），请不要手改。", ""]
    return "\n".join(lines)
