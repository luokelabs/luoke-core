#!/usr/bin/env python3
"""生成发布清单 release.json（V1 §4）：把要发的模型目录逐个算大小和 sha256，写上下载地址模板。

    python3 tools/make_release_manifest.py --out release.json --release 2026-10-13 \\
        --model qwen3.5-9b-4bit=<目录>=<地址前缀> [--model ...]
地址前缀末尾不带 /，文件路径接在后面；本机测试用 file:///…/<目录>；ModelScope 用
https://modelscope.cn/api/v1/models/<repo>/repo?Revision=<rev>&FilePath 这类带 {path} 的模板（写 --url-template 时整个模板由你给）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from luoke_core import download  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--release", required=True)
    ap.add_argument("--model", action="append", default=[], help="名字=目录=地址前缀（或带 {path} 的完整模板）")
    ap.add_argument("--file", action="append", default=[], help="名字=种类=版本=文件=地址前缀（主控 DMG、引擎包 zip 这类单个文件）")
    a = ap.parse_args()
    items = {}
    for spec in a.file:
        name, kind, version, path, base = spec.split("=", 4)
        items[name] = download.make_item_from_file(path, kind, version, base)
        print(f"{name}: {kind} {version}，{download.item_total(items[name]) / 2**20:.1f} MB")
    for spec in a.model:
        name, src, base = spec.split("=", 2)
        item = download.make_item_from_dir(src, name, "model", a.release, base if "{path}" not in base else base.rsplit("/{path}", 1)[0])
        if "{path}" in base:
            item["urls"] = [base]
        items[name] = item
        print(f"{name}: {len(item['files'])} 个文件，{download.item_total(item) / 2**30:.2f} GB")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"contract": 1, "release": a.release, "items": items}, f, ensure_ascii=False, indent=1)
    print("写入", a.out)


if __name__ == "__main__":
    main()
