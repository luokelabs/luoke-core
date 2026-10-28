"""按发布清单下载（V1 §4 发布清单、§6 首次运行第 4 步）：分块、续传、sha256 核对、临时位置原子改名；地址列表按顺序试。

发布清单 release.json：
  {"contract": 1, "release": "2026-10-13", "items": {
     "<名字>": {"kind": "model" | "engine" | "hub", "version": "...", "dir": "<装到模型目录下的目录名>",
               "files": [{"path": "config.json", "size": 123, "sha256": "..."}, ...],
               "urls": ["https://modelscope.cn/api/v1/models/<repo>/repo?Revision=<rev>&FilePath={path}",
                        "file:///…/<dir>/{path}"]}}}
  urls 里的 {path} 换成文件的相对路径；多个地址：每项先用最大的文件测几秒稳定吞吐、快的先下，几个文件同时下；下到一半断了、
  或者明显比另一个地址测出来的慢，就换过去用 Range 续传（luoke-core#24）
  （2026-10-16 定：模型放 ModelScope + Hugging Face 两处，从日本连 ModelScope 只有 1–5 MB/s）；哈希照旧逐文件核对。只用标准库。
"""
from __future__ import annotations

import concurrent.futures
import concurrent.futures.thread  # 先导入：3.9 在线程里懒加载它会撞导入锁
import glob
import hashlib
import json
import os
import re
import plistlib
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from . import apps

CHUNK = 1 << 20
RETRIES = 3
PROBE_MIN_SIZE = 8 << 20     # 文件至少这么大才值得测速
PROBE_BYTES = 32 << 20       # 每个地址最多取这么多算速度……
PROBE_SECS = 4.0             # ……或者取这么久（从第一块到手算起：302、TLS、首字节不算；luoke-core#24 只取 2 MB 时这些占了大头）
PROBE_TIMEOUT = 8.0
SWITCH_CHECK_SECS = 30.0     # 下载中每这么久看一次实际速度
SWITCH_RATIO = 2.0           # 另一个地址测出来比现在快这么多倍，就续传换过去
SWITCH_MIN_LEFT = 64 << 20   # 剩下不到这么多就不换了
PARALLEL_FILES = 4           # 一个模型的几个文件同时下（luoke-core#24：单连接从日本连 Hugging Face 约 9 MB/s，6 个连接约 60 MB/s）


class DownloadError(RuntimeError):
    pass


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def _open(url: str, start: int):
    req = urllib.request.Request(url, headers={"User-Agent": "luoke-core/0.1"})
    if start > 0:
        req.add_header("Range", f"bytes={start}-")
    return urllib.request.urlopen(req, timeout=60)


class _SlowSource(Exception):
    """下载中发现这个地址比另一个测出来的慢得多：换地址续传（不算出错）。"""


class Stopped(Exception):
    """同一项里别的文件下失败了：这个文件停下（.part 留着下次续传）。"""


def probe_speed(url: str, nbytes: int = PROBE_BYTES, secs: float = PROBE_SECS, timeout: float = PROBE_TIMEOUT) -> float:
    """算一个地址的稳定吞吐（B/s）：从第一块到手开始计时，取到 nbytes 或 secs 秒为止；连不上或超时算 0。只对 http(s) 地址有意义。"""
    if not url.startswith(("http://", "https://")):
        return float("inf")
    req = urllib.request.Request(url, headers={"User-Agent": "luoke-core/0.1", "Range": f"bytes=0-{nbytes - 1}"})
    t_open = time.monotonic()
    got = 0
    t0 = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            first = r.read(min(CHUNK, nbytes))
            if not first:
                return 0.0
            t0 = time.monotonic()
            got0 = len(first)
            while got0 + got < nbytes and time.monotonic() - t0 < secs:
                b = r.read(min(CHUNK, nbytes - got0 - got))
                if not b:
                    break
                got += len(b)
    except (urllib.error.URLError, OSError, ConnectionError, ValueError):
        if t0 is None or not got:
            return 0.0
    if not got:      # 整个文件一块就到手了：只能连开销一起算
        return got0 / max(time.monotonic() - t_open, 1e-3)
    return got / max(time.monotonic() - t0, 1e-3)


def order_sources(urls: list, size: Optional[int], probe: Callable[[str], float] = probe_speed) -> list:
    """多个地址时按测速排：快的在前，测不到的垫后；小文件或只有一个地址不测。返回 [(url, B/s 或 None), ...]。"""
    if len(urls) < 2 or (size is not None and size < PROBE_MIN_SIZE):
        return [(u, None) for u in urls]
    speeds = [(u, probe(u)) for u in urls]
    return sorted(speeds, key=lambda x: -x[1])


def describe_speeds(ordered: list) -> str:
    """日志用："modelscope.cn 1.5 MB/s，huggingface.co 没测速"。测了是 0 写"不通"，没测写"没测速"（#24：之前都写成"不通"）。"""
    def one(u, v):
        host = urllib.parse.urlparse(u).netloc or u
        if v is None:
            return f"{host} 没测速"
        return f"{host} 不通" if not v else f"{host} {v / 2**20:.1f} MB/s"
    return "，".join(one(u, v) for u, v in ordered)

def fetch_file(urls: list, dest: str, size: Optional[int], sha256: str,
               progress: Optional[Callable[[int, Optional[int]], None]] = None,
               log: Optional[Callable[[str], None]] = None, probe: Callable[[str], float] = probe_speed,
               ordered: Optional[list] = None, stop=None) -> str:
    """把一个文件下到 dest（经 dest.part 续传），核对 sha256。返回用的地址。
    多个地址先测速排序（ordered 给了就用给的，不再测）；一个地址连续失败就换下一个；下载中每 SWITCH_CHECK_SECS 看一次实际速度，
    比下一个地址测出来的慢 SWITCH_RATIO 倍以上就续传换过去（luoke-core#24），换下来的地址垫到最后、不再拿来比。
    stop（threading.Event）置位时停下，抛 Stopped。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest) and sha256_file(dest) == sha256:
        if progress:
            progress(os.path.getsize(dest), size)
        return "cached"
    part = dest + ".part"
    last_err: Optional[Exception] = None
    if ordered is None:
        ordered = order_sources(urls, size, probe)
        if log and len(ordered) > 1 and any(v is not None for _u, v in ordered):
            log("测速 " + os.path.basename(dest) + "：" + describe_speeds(ordered))
    queue = list(ordered)
    while queue:
        url, _speed = queue.pop(0)
        for attempt in range(RETRIES):
            if stop is not None and stop.is_set():
                raise Stopped()
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if size is not None and have > size:
                os.remove(part)
                have = 0
            try:
                if size == 0:
                    open(part, "wb").close()
                    break
                if size is not None and have == size:
                    break
                if url.startswith("file://"):
                    src = urllib.request.url2pathname(urllib.parse.urlparse(url).path)
                    with open(src, "rb") as fi, open(part, "ab" if have else "wb") as fo:
                        fi.seek(have)
                        done = have
                        for b in iter(lambda: fi.read(CHUNK), b""):
                            fo.write(b)
                            done += len(b)
                            if progress:
                                progress(done, size)
                    break
                with _open(url, have) as r:
                    if have and r.status == 206:
                        # 续传：核对服务器从哪一字节开始给（审查 2026-10-17：起点不对会拼错文件）
                        cr = r.headers.get("Content-Range") or ""
                        m = re.match(r"bytes (\d+)-", cr)
                        if not m or int(m.group(1)) != have:
                            raise DownloadError(f"续传起点不对（要 {have}，给的是 {cr or '没写'}）")
                    elif have:
                        have = 0
                    # 换源只和还没用过的下一个地址比（它测出来的速度）；换下来的垫到最后，速度记成 None
                    better = queue[0][1] if queue and queue[0][1] and queue[0][1] != float("inf") else None
                    with open(part, "ab" if have else "wb") as fo:
                        done = have
                        win_t, win_done = time.monotonic(), done
                        for b in iter(lambda: r.read(CHUNK), b""):
                            if size is not None and done + len(b) > size:
                                raise DownloadError(f"{os.path.basename(dest)} 比清单写的 {size} 字节大：停下（镜像出错或被劫持）")
                            fo.write(b)
                            done += len(b)
                            if progress:
                                progress(done, size)
                            if stop is not None and stop.is_set():
                                raise Stopped()
                            if better:
                                now = time.monotonic()
                                if now - win_t >= SWITCH_CHECK_SECS:
                                    rate = (done - win_done) / (now - win_t)
                                    left = (size - done) if size is not None else SWITCH_MIN_LEFT
                                    if rate * SWITCH_RATIO < better and left >= SWITCH_MIN_LEFT:
                                        if log:
                                            log(f"{os.path.basename(dest)}：{urllib.parse.urlparse(url).netloc} 只有 {rate / 2**20:.1f} MB/s，"
                                                f"换到 {urllib.parse.urlparse(queue[0][0]).netloc}（测速 {better / 2**20:.1f} MB/s）续传")
                                        raise _SlowSource()
                                    win_t, win_done = now, done
                break
            except _SlowSource:
                queue.append((url, None))
                url = None
                break
            except DownloadError as e:      # 内容不对：这个地址不再试，删掉半成品换下一个
                last_err = e
                if os.path.exists(part):
                    os.remove(part)
                url = None
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (403, 404, 410):     # 服务器明确说没有：重试也没用，换下一个地址（10-19：被当成"网络问题"给用户看了）
                    url = None
                    break
                time.sleep(2 * (attempt + 1))
                continue
            except (urllib.error.URLError, OSError, ConnectionError) as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
                continue
        else:
            continue   # 这个地址试完了，换下一个
        if url is None:
            continue
        if os.path.exists(part):
            got = sha256_file(part)
            if got == sha256:
                os.replace(part, dest)
                return url
            os.remove(part)
            last_err = DownloadError(f"哈希不对：{os.path.basename(dest)} 期望 {sha256[:12]}… 实得 {got[:12]}…（{url}）")
    raise DownloadError(explain(os.path.basename(dest), last_err))


def explain(name: str, err) -> str:
    """给用户看的一句话：分清"服务器上没有"（版本还没发布、地址失效）、"连不上"（网络）和"内容不对"，不都说成网络问题。"""
    if isinstance(err, urllib.error.HTTPError):
        if err.code in (404, 410):
            return f"服务器上找不到 {name}（这个版本可能还没发布，或下载地址已经失效）"
        if err.code == 403:
            return f"下载服务器拒绝了 {name}（HTTP 403）"
        return f"下载服务器出错，{name} 没下下来（HTTP {err.code}），稍后再试"
    if isinstance(err, DownloadError):
        return f"{name} 下下来了但核对不过：{err}"
    if isinstance(err, (urllib.error.URLError, ConnectionError, TimeoutError)):
        return f"连不上下载服务器，{name} 没下下来（检查网络后重试）"
    return f"{name} 没下下来：{err}"




TEAM_ID = "NJQ5W3T9K5"     # 螺壳实验室的 Developer ID（主控、配件、重签过的引擎包都是它签的）


def verify_app(app: str, bundle_id: Optional[str] = None) -> None:
    """装之前核签名（审查 2026-10-17：之前只看文件名和清单）：codesign 完整校验、Team ID 是我们的、Info.plist 的 bundle id 对、
    公证票据钉在包上。LUOKE_ALLOW_UNSIGNED=1 时跳过（只给单元测试和实验室构建用）。"""
    if os.environ.get("LUOKE_ALLOW_UNSIGNED") == "1":
        return
    r = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app], capture_output=True, text=True)
    if r.returncode != 0:
        raise DownloadError(f"{os.path.basename(app)} 签名校验不过：{(r.stderr or '').strip()[:200]}")
    info = subprocess.run(["/usr/bin/codesign", "-dv", app], capture_output=True, text=True).stderr or ""
    team = next((l.split("=", 1)[1].strip() for l in info.splitlines() if l.startswith("TeamIdentifier=")), None)
    if team != TEAM_ID:
        raise DownloadError(f"{os.path.basename(app)} 不是螺壳实验室签的（Team {team}）")
    if bundle_id:
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                bid = plistlib.load(f).get("CFBundleIdentifier")
        except (OSError, ValueError):
            bid = None
        if bid != bundle_id:
            raise DownloadError(f"{os.path.basename(app)} 的 bundle id 是 {bid}，不是 {bundle_id}")
    r = subprocess.run(["/usr/bin/xcrun", "stapler", "validate", app], capture_output=True, text=True)
    if r.returncode != 0:
        raise DownloadError(f"{os.path.basename(app)} 没有公证票据")


_move = apps._move


def app_bundle_id(app: str) -> Optional[str]:
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def install_app_zip(zip_path: str, dest_dir: str, expect_bundle_id: Optional[str] = None, expect_name: Optional[str] = None,
                    rename_if_bundle: Optional[str] = None,
                    register: bool = True, backup_dir: Optional[str] = None, replace: Optional[str] = None, verify: bool = True) -> str:
    """把一个 .app 的 zip（ditto -c -k --keepParent 打的，公证过的）装到 dest_dir：解到暂存目录，核对 .app 名字、配件清单的 bundle id
    和签名（verify_app），再换进去，lsregister。返回 .app 路径。
    替换旧版（审查 2026-10-17：之前先删旧的再放新的，中间失败两头落空）：旧的先挪进 backup_dir（没给就挪到旁边临时名），新的放好了再处理旧的；
    replace 给了旧 .app 的路径（名字可能和新的不一样）也一起挪走，不留两份同 bundle id。backup_dir 里的旧版留着当回退。"""
    staging = tempfile.mkdtemp(prefix="luoke-install-", dir=os.path.dirname(os.path.abspath(zip_path)))
    moved: list = []
    try:
        subprocess.run(["/usr/bin/ditto", "-x", "-k", zip_path, staging], check=True, capture_output=True)
        found = glob.glob(os.path.join(staging, "*.app"))
        if len(found) != 1:
            raise DownloadError(f"{os.path.basename(zip_path)} 里不是一个 .app")
        app = found[0]
        if expect_name and os.path.basename(app) != expect_name and rename_if_bundle and app_bundle_id(app) == rename_if_bundle:
            # 名字不对、但确实是要的那个应用（luoke-core#31：internal-1024 起引擎包里叫 oMLX-0.7.0.app）：改回要的名字再装。
            # 签名封印不含 .app 文件夹本身的名字，改名不影响下面的签名核对
            renamed = os.path.join(staging, expect_name)
            os.rename(app, renamed)
            app = renamed
        if expect_name and os.path.basename(app) != expect_name:
            raise DownloadError(f"{os.path.basename(zip_path)} 里是 {os.path.basename(app)}，不是 {expect_name}")
        if expect_bundle_id:
            mp = os.path.join(app, "Contents", "Resources", "luoke-accessory.json")
            try:
                with open(mp, encoding="utf-8") as f:
                    bid = json.load(f).get("bundle_id")
            except (OSError, ValueError):
                bid = None
            if bid != expect_bundle_id:
                raise DownloadError(f"{os.path.basename(app)} 的清单 bundle id 是 {bid}，不是 {expect_bundle_id}")
        if verify:
            verify_app(app, expect_bundle_id)
        os.makedirs(dest_dir, exist_ok=True)
        final = os.path.join(dest_dir, os.path.basename(app))
        if expect_bundle_id and register:
            gone = apps.unregister_other_copies(expect_bundle_id, final)    # 放新包之前（luoke-ime#15）
            if gone:
                print(f"注销同 bundle id 的别的副本：{len(gone)} 份", flush=True)
        olds = [p for p in dict.fromkeys([final] + ([replace] if replace else [])) if p and os.path.lexists(p)]
        for old in olds:       # 登记和注销都由 apps 管（LaunchServices 跟着挪动走、暂存目录也可能被登记）
            keep = apps.retire_app(old, backup_dir) if backup_dir else apps.move_app(old, old + f".old-{os.getpid()}", register=False)
            moved.append((old, keep))
        try:
            apps.move_app(app, final, register=register)
        except OSError:
            for old, keep in reversed(moved):      # 放不进去：旧的放回原处
                apps.move_app(keep, old, register=register)
            moved = []
            raise
        if not backup_dir:
            for _old, keep in moved:
                apps.remove_app(keep)
        else:
            apps.unregister_dir(backup_dir)      # 备份目录里的旧版可能又被系统登记了（luoke-ime 10-22）
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return final


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    if m.get("contract") != 1 or "items" not in m:
        raise DownloadError("发布清单格式不对")
    return m


ITEM_MARKER = ".luoke-item.json"     # 模型目录核对通过、整体改名后写：记清单项的摘要，"已装"比摘要，不只看目录和大小（审查 2026-10-17）


def check_item(item: dict) -> None:
    """清单字段兜底校验（清单在签名包里，可信；这里防写错）：dir 是单个名字，文件路径是相对路径、没有 .."""
    d = item.get("dir")
    if not isinstance(d, str) or not d or d in (".", "..") or "/" in d or "\\" in d:
        raise DownloadError(f"清单项的 dir 不对：{d!r}")
    for f in item.get("files") or []:
        p = f.get("path")
        if not isinstance(p, str) or not p or os.path.isabs(p) or ".." in p.split("/") or p.startswith("~"):
            raise DownloadError(f"清单里的文件路径不对：{p!r}")


def item_digest(item: dict) -> str:
    """清单项内容的摘要：按路径排好的"路径\tsha256"逐行，再取 sha256（Swift 那边同一算法，见 Installer.swift Manifests.itemDigest）。"""
    lines = "\n".join(f"{f['path']}\t{f['sha256']}" for f in sorted(item["files"], key=lambda f: f["path"]))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def item_installed(item: dict, path: str) -> bool:
    """目录里的标记记的摘要等于清单这一项的摘要。旧算法写的标记（10-19 以前装的）也认，并当场改写成新算法，
    这样 app（Swift，只算新的）读到的也对得上。"""
    marker = os.path.join(path, ITEM_MARKER)
    try:
        with open(marker, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return False
    if m.get("digest") == item_digest(item):
        return True
    if m.get("digest") == _legacy_item_digest(item):
        m["digest"] = item_digest(item)
        tmp = marker + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(m, f)
            os.replace(tmp, marker)
        except OSError:
            pass
        return True
    return False


def item_total(item: dict) -> int:
    return sum(int(f.get("size") or 0) for f in item["files"])


def item_file_present(staging: str, f: dict) -> bool:
    """暂存区里这个文件已经齐了（大小对得上；哈希留给 fetch_file 核）：全齐了就不用测速。"""
    p = os.path.join(staging, f["path"])
    return os.path.isfile(p) and f.get("size") is not None and os.path.getsize(p) == int(f["size"])


# ---- 复用本机已有的同一个模型（团队 issue luoke-core#3，2026-10-16 定进 beta1）--------------------------------------------
# 下载前在常见位置找同名模型目录，逐个文件核对清单里的 sha256，对上的用 APFS 克隆（cp -c，不占额外空间）放进来；
# 只读别人的目录，不移动、不改原文件；克隆不了（不同的卷、不是 APFS）就照常下载。

def local_model_roots() -> list:
    """可能放着模型的目录（存在的才算）。LUOKE_LOCAL_MODEL_ROOTS（冒号分隔）可整个替换，测试用。"""
    env = os.environ.get("LUOKE_LOCAL_MODEL_ROOTS")
    if env is not None:
        return [r for r in env.split(":") if r and os.path.isdir(r)]
    home = os.path.expanduser("~")
    hf = os.path.join(os.environ.get("HF_HOME") or os.path.join(home, ".cache", "huggingface"), "hub")
    roots = [os.path.join(home, ".omlx", "models"), hf, os.path.join(home, ".lmstudio", "models"),
             os.path.join(home, ".cache", "lm-studio", "models")]
    return [r for r in roots if os.path.isdir(r)]


def local_model_candidates(dir_name: str, roots: Optional[list] = None) -> list:
    """名字等于清单里 dir 的模型目录：<根>/<dir>、<根>/<组织>/<dir>，以及 HF 缓存的 models--<组织>--<dir>/snapshots/<版本>。"""
    out = []
    for root in (roots if roots is not None else local_model_roots()):
        for pat in (os.path.join(root, dir_name), os.path.join(root, "*", dir_name),
                    os.path.join(root, f"models--*--{dir_name}", "snapshots", "*")):
            for c in sorted(glob.glob(pat)):
                if os.path.isdir(c) and c not in out:
                    out.append(c)
    return out


def clone_file(src: str, dest: str) -> bool:
    """APFS 克隆（不占额外空间、原文件不动）；克隆不了返回 False（调用方照常下载）。
    不在同一个卷上就不试：cp -c 跨卷会退回整份复制，空间翻倍、日志却写着"克隆"（luoke-core#3 复验发现）。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    try:
        if os.stat(os.path.realpath(src)).st_dev != os.stat(os.path.dirname(os.path.abspath(dest))).st_dev:
            return False
    except OSError:
        return False
    tmp = dest + ".clone"
    try:
        r = subprocess.run(["/bin/cp", "-c", os.path.realpath(src), tmp], capture_output=True)
        if r.returncode != 0:
            return False
        os.replace(tmp, dest)
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def reuse_local(item: dict, staging: str, log: Optional[Callable[[str], None]] = None, roots: Optional[list] = None,
                extra: Optional[list] = None) -> int:
    """模型项：清单里每个文件，在本机已有的同名模型里找大小和 sha256 都对得上的，克隆进 staging。返回复用的字节数。"""
    if item.get("kind", "model") != "model":
        return 0
    cands = [c for c in (extra or []) if os.path.isdir(c)] + local_model_candidates(item["dir"], roots)
    if not cands:
        return 0
    reused, sources = 0, set()
    hashed: dict = {}
    for f in item["files"]:
        dest = os.path.join(staging, f["path"])
        if os.path.exists(dest):
            continue
        for c in cands:
            src = os.path.join(c, f["path"])
            if not os.path.isfile(src) or os.path.getsize(src) != int(f.get("size", -1)):
                continue
            real = os.path.realpath(src)
            if real not in hashed:
                hashed[real] = sha256_file(real)
            if hashed[real] != f["sha256"]:
                continue
            if clone_file(real, dest):
                reused += os.path.getsize(dest)
                sources.add(c)
                break
    if log and reused:
        log(f"复用本机已有的 {item['dir']}：{reused / 2**30:.1f} GB 克隆自 " + "、".join(sorted(sources)) + "（原文件不动）")
    return reused


def fetch_item(item: dict, dest_dir: str, progress: Optional[Callable[[str, int, int], None]] = None,
               log: Optional[Callable[[str], None]] = None, probe: Callable[[str], float] = probe_speed,
               workers: int = PARALLEL_FILES) -> str:
    """把清单里的一项（例如一个模型目录）下到 dest_dir/<dir>。先下到 <dir>.part/，全部核对通过后整体改名。
    几个文件同时下（workers 个）；progress(正在下的文件, 这一项已下字节, 这一项总字节)。"""
    check_item(item)
    final = os.path.join(dest_dir, item["dir"])
    if item_installed(item, final):
        return final
    parent = os.path.dirname(os.path.abspath(dest_dir))
    if os.path.basename(parent) == "downloads":      # dest_dir 本身就是暂存区（<env>/downloads/apps）：就地加 .part，别再套一层 downloads/downloads
        staging = os.path.join(os.path.abspath(dest_dir), item["dir"] + ".part")
    else:
        staging = os.path.join(parent, "downloads", item["dir"])   # 不在模型目录里：引擎扫不到半成品
    os.makedirs(staging, exist_ok=True)
    total = item_total(item)
    try:
        # 已有的同名目录（旧版、没写摘要的、坏了的）也当复用来源：对得上哈希的克隆过来，对不上的重下
        reuse_local(dict(item, kind=item.get("kind", "model")) if item.get("kind", "model") == "model" else item, staging, log,
                    extra=[final] if os.path.isdir(final) and not os.path.islink(final) else None)
    except Exception as e:  # noqa: BLE001  复用只是省流量：出什么错都照常下载
        if log:
            log(f"复用本机模型没成（{type(e).__name__}），照常下载")
    def urls_of(f):
        return [u.replace("{path}", urllib.parse.quote(f["path"], safe="/")) for u in item["urls"]]
    # 每项只测一次速（用最大的文件），各文件按地址的序号沿用这个顺序
    big = max(item["files"], key=lambda f: int(f.get("size") or 0), default=None)
    rank = None
    if big is not None and len(item["urls"]) > 1 and int(big.get("size") or 0) >= PROBE_MIN_SIZE \
            and not all(item_file_present(staging, f) for f in item["files"]):
        probed = order_sources(urls_of(big), int(big["size"]), probe)
        if log:
            log(f"测速 {item['dir']}（用 {big['path']}）：" + describe_speeds(probed))
        idx = {u: n for n, u in enumerate(urls_of(big))}
        rank = [(idx[u], v) for u, v in probed]
    lock = threading.Lock()
    done_by: dict = {}
    stop = threading.Event()

    def one(f):
        urls = urls_of(f)
        dest = os.path.join(staging, f["path"])

        def prog(done, _size):
            with lock:
                done_by[f["path"]] = done
                if progress:
                    progress(f["path"], sum(done_by.values()), total)
        ordered = [(urls[n], v) for n, v in rank] if rank else [(u, None) for u in urls]
        used = fetch_file(urls, dest, f.get("size"), f["sha256"], prog, log=log, ordered=ordered, stop=stop)
        with lock:
            done_by[f["path"]] = int(f.get("size") or os.path.getsize(dest))
        if log and used != "cached" and int(f.get("size") or 0) >= PROBE_MIN_SIZE:
            log(f"下载 {f['path']} 用的是 {urllib.parse.urlparse(used).netloc or used}")
    # 大的先下（几个大分片并行）；一个文件失败就让其余的停下（.part 留着下次续传），报第一个错
    files = sorted(item["files"], key=lambda f: -int(f.get("size") or 0))
    first_err: Optional[BaseException] = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(files)))) as pool:
        futs = [pool.submit(one, f) for f in files]
        for fu in concurrent.futures.as_completed(futs):
            e = fu.exception()
            if e is not None and not isinstance(e, Stopped) and first_err is None:
                first_err = e
                stop.set()
    if first_err is not None:
        raise first_err
    # 暂存区里清单上没有的东西（上一版留下的文件、.part、.clone）不带进去（审查 2026-10-17）
    want = {os.path.normpath(f["path"]) for f in item["files"]}
    for root, _dirs, names in os.walk(staging):
        for n in names:
            rel = os.path.normpath(os.path.relpath(os.path.join(root, n), staging))
            if rel not in want:
                os.remove(os.path.join(root, n))
    with open(os.path.join(staging, ITEM_MARKER), "w", encoding="utf-8") as f:
        json.dump({"digest": item_digest(item), "version": item.get("version")}, f)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    old = None
    if os.path.lexists(final):
        old = final + f".old-{os.getpid()}"
        _move(final, old)
    try:
        _move(staging, final)
    except OSError:
        if old:
            _move(old, final)
        raise
    if old:
        if os.path.islink(old):
            os.remove(old)
        else:
            shutil.rmtree(old, ignore_errors=True)
    return final


def make_item_from_dir(src_dir: str, name: str, kind: str, version: str, url_base: str) -> dict:
    """给发布脚本用：把一个目录（模型）描述成清单项，算每个文件的大小和 sha256。url_base 末尾不带 /。"""
    files = []
    for root, _, names in os.walk(src_dir):
        for n in sorted(names):
            if n.startswith(".") or n.endswith((".lock", ".part", ".tmp")):
                continue
            p = os.path.join(root, n)
            rel = os.path.relpath(p, src_dir)
            files.append({"path": rel, "size": os.path.getsize(p), "sha256": sha256_file(p)})
    return {"kind": kind, "version": version, "dir": os.path.basename(src_dir.rstrip("/")), "files": files,
            "urls": [url_base + "/{path}"]}


def make_item_from_file(path: str, kind: str, version: str, url_base: str) -> dict:
    """给发布脚本用：一个文件（主控 DMG、引擎包 zip）描述成清单项。"""
    name = os.path.basename(path)
    return {"kind": kind, "version": version, "dir": os.path.splitext(name)[0],
            "files": [{"path": name, "size": os.path.getsize(path), "sha256": sha256_file(path)}],
            "urls": [url_base + "/{path}"]}


def _legacy_item_digest(item: dict) -> str:
    """10-19 以前实际在用的摘要（"路径 sha256"、按整行排序）：它和上面的 item_digest 同名，写在后面，把新的盖掉了，
    Swift 那边算的是新的，所以 app 侧一直认为主控装好的模型没装（luoke-core#3 复验发现）。只用来认旧标记，见 item_installed。"""
    lines = sorted(f"{f['path']} {f['sha256']}" for f in item["files"])
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
