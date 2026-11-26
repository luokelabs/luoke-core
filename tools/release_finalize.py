#!/usr/bin/env python3
"""beta 转正、删草稿（11-25 定：beta 先发草稿，负责人看过再转正）。

守护进程（tools/release_daemon.py）收到负责人本人的 `/release betaN[.M] --publish` 或 `--discard` 时跑这个。它从工具目录（跟私有 main
的那份）跑，不在钉住的 core 里：转正只是 GitHub 上的一次操作，不打包、不评测，改它不用等进下一个钉住的版本。草稿由钉住的 core 里的
internal_build.py 建（publish 一步），建好后记在出版克隆的 releases/<tag>/draft.json。

    python3 tools/release_finalize.py publish beta3 [--root <出版克隆>]
    python3 tools/release_finalize.py discard beta3 [--root <出版克隆>]

publish：核草稿还在、叫这个名字的只有它一个、附件和 draft.json 记的一样（名字、大小，GitHub 给了 digest 再核 sha256）
  → 写 ClamAV 源码索引（建草稿时记下的原文，没有就不写）→ 转正并设为最新。GitHub 这时才在 luoke-releases 的 main 上打标签，
  GitLab 镜像（release: published）、官网的 latest/download、应用内检查更新随之生效。已经转正过的照实说、不重做。
discard：只删草稿（已转正的不碰），删掉 draft.json、去掉 steps.json 里 publish 的记录：修好后照常 /release betaN 重出，
  钉住的提交没变就从上传这一步接着做。
令牌照 internal_build：专用钥匙串里 LUOKE_RELEASES_TOKEN 那一条（不读 hub 自己的 gh 凭据）；没设就用 gh 自己登录的账号。
最后一行以「=> 」开头，是给守护进程回复用的一句话；退出码 0 做成了，1 没做成。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import internal_build as ib  # noqa: E402  令牌、ClamAV 索引、草稿记录的格式和建草稿的那份一致

BETA = re.compile(r"^beta\d+(\.\d+)?$")


class Refused(Exception):
    """没做成：说明原因，什么都没改（或者改到哪写清楚）"""


def gh_api(args: list, env, body=None):
    cmd = ["gh", "api"] + args + (["--input", "-"] if body is not None else [])
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        raise Refused(f"gh api {' '.join(args[:3])}：{(r.stderr or r.stdout).strip()[-300:]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def same_tag(repo: str, tag: str, env) -> list:
    pages = gh_api(["--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"], env) or []
    return [r for page in pages for r in page if r.get("tag_name") == tag]


def draft_path(root: str, tag: str) -> str:
    return os.path.join(root, "releases", tag, ib.DRAFT_FILE)


def load_draft(root: str, tag: str) -> dict:
    try:
        with open(draft_path(root, tag), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        raise Refused(f"releases/{tag}/{ib.DRAFT_FILE} 不在：{tag} 还没建过草稿（还在出、或者出的时候停了），或者草稿已经删了")
    except ValueError:
        raise Refused(f"releases/{tag}/{ib.DRAFT_FILE} 读不出来")


def asset_problems(rel: dict, draft: dict) -> list:
    """草稿上现在的附件和建草稿时记下的比：少了、多了、没传完、大小不对、哈希不对（GitHub 给了 digest 才核）"""
    remote = {a["name"]: a for a in rel.get("assets") or []}
    want = {a["name"]: a for a in draft.get("assets") or []}
    out = []
    if set(want) - set(remote):
        out.append("少了 " + "、".join(sorted(set(want) - set(remote))))
    if set(remote) - set(want):
        out.append("多了 " + "、".join(sorted(set(remote) - set(want))))
    for name in sorted(set(want) & set(remote)):
        a, w = remote[name], want[name]
        if a.get("state", "uploaded") != "uploaded":
            out.append(f"{name} 没传完")
        elif a.get("size") != w.get("size"):
            out.append(f"{name} 大小 {a.get('size')}，记的是 {w.get('size')}")
        elif a.get("digest") and a["digest"] != "sha256:" + w.get("sha256", ""):
            out.append(f"{name} 哈希和记的不一样")
    return out


def publish(tag: str, root: str, env) -> str:
    d = load_draft(root, tag)
    repo = d.get("repo") or ib.releases_repo(tag)
    rs = same_tag(repo, tag, env)
    done = [r for r in rs if not r.get("draft")]
    if done:
        return f"**{tag}** 已经转正过了：{done[0].get('html_url', '')}（没再动）"
    mine = [r for r in rs if r.get("id") == d.get("id")]
    if not mine:
        raise Refused(f"{repo} 里找不到 releases/{tag}/{ib.DRAFT_FILE} 记的草稿（{d.get('url')}）：被删了的话照常 /release {tag} 重出")
    if len(rs) > 1:
        raise Refused(f"{repo} 里有 {len(rs)} 个叫 {tag} 的草稿：只留记着的那个（{d.get('url')}），别的删掉再转正")
    bad = asset_problems(mine[0], d)
    if bad:
        raise Refused("草稿的附件和建草稿时记的对不上：" + "；".join(bad))
    if d.get("clamav_index"):
        ib.put_clamav_index(d["clamav_index"], env)      # 先写索引再转正：GitLab 镜像在转正时推 main，一起带过去
    ahead = (f"；ClamAV 源码索引已经写了、指着还是草稿的 {tag}，再发一次 `/release {tag} --publish` 就补上"
             if d.get("clamav_index") else "；什么都没改，可以再发一次")
    try:
        r = gh_api(["-X", "PATCH", f"repos/{repo}/releases/{d['id']}"], env, {"draft": False, "make_latest": "true"}) or {}
    except Refused as e:
        raise Refused(f"转正没成（{e}）" + ahead)
    if r.get("draft") is not False:
        raise Refused(f"转正没成：GitHub 回的还是草稿（{d.get('url')}）" + ahead)
    d["published"] = {"at": time.time(), "url": r.get("html_url", "")}
    save(draft_path(root, tag), d)
    return (f"**{tag}** 转正了：{r.get('html_url', '')}。GitLab 镜像、官网下载链接、应用内检查更新随之生效"
            + ("；ClamAV 源码索引已更新" if d.get("clamav_index") else "") + "。")


def discard(tag: str, root: str, env) -> str:
    """有 draft.json、记着的草稿也还在：只删它（别人手建的同名草稿不碰，回复里列出来）。
    没有记录、或者记着的那个已经被人手删了：所有同名草稿都删"""
    try:
        d = load_draft(root, tag)
    except Refused:
        d = {}
    repo = d.get("repo") or ib.releases_repo(tag)
    rs = same_tag(repo, tag, env)
    done = [r for r in rs if not r.get("draft")]
    if done:
        raise Refused(f"{tag} 已经转正了（{done[0].get('html_url', '')}）：--discard 只删草稿，已发布的不碰")
    gone = [r for r in rs if r.get("id") == d.get("id")]
    if not gone:
        gone = rs           # 没有记录，或者记着的那个已经不在了（有人手删过）：同名草稿都删，免得重出时卡在没记着的草稿上
    for r in gone:
        gh_api(["-X", "DELETE", f"repos/{repo}/releases/{r['id']}"], env)
    forget_local(root, tag)
    left = [r for r in rs if r not in gone]
    return ((f"**{tag}** 的草稿删了（{len(gone)} 个）" if gone else f"{repo} 里没有记着的 {tag} 草稿，本地记录清掉了") +
            (f"；另有 {len(left)} 个同名草稿不是流水线建的，没动：" + "、".join(r.get("html_url", str(r.get("id"))) for r in left) if left else "") +
            f"。修好后照常发 /release {tag} 重出，钉住的提交没变就从上传这一步接着做。")


def forget_local(root: str, tag: str) -> None:
    """删掉 draft.json，steps.json 里去掉 publish：internal_build 续跑时重新上传"""
    try:
        os.remove(draft_path(root, tag))
    except FileNotFoundError:
        pass
    steps = os.path.join(root, "releases", tag, "steps.json")
    try:
        with open(steps, encoding="utf-8") as f:
            done = json.load(f)
    except (OSError, ValueError):
        return
    if done.pop("publish", None) is not None:
        save(steps, done)


def save(path: str, data: dict) -> None:
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["publish", "discard"])
    ap.add_argument("tag")
    ap.add_argument("--root", default=os.environ.get("LUOKE_BUILD_ROOT") or ROOT, help="出版克隆（releases/<tag>/ 在这里）")
    a = ap.parse_args()
    if not BETA.match(a.tag):
        print(f"=> {a.tag} 不是 beta（betaN[.M]）：只有 beta 先发草稿再转正")
        sys.exit(1)
    root = os.path.abspath(os.path.expanduser(a.root))
    ib.unlock_sign_keychain()
    env = ib.releases_token_env()
    try:
        msg = (publish if a.action == "publish" else discard)(a.tag, root, env)
    except Refused as e:
        print(f"=> 没做成：{e}")
        sys.exit(1)
    print("=> " + msg)


if __name__ == "__main__":
    main()
