"""python3 -m luoke_core serve：起开发版主控。

    python3 -m luoke_core serve --recipe recipes/dev.json --model-dir <模型目录> [--env <主控环境>] [--socket <路径>] [--lab]
        [--accessory-dir <目录>] [--mem-gb N]

- 主控环境默认 ~/Library/Application Support/luoke-core（--lab 时 luoke-core-lab），socket 在它的 run/hub.sock。
- --mem-gb 可以假装成别的硬件档（只影响档位判定，引擎的天花板也跟着档走，所以 128 GB 机器可以当 16 GB 档试规则）。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading

from . import __version__
from .hub import Hub, serve
from .recipe import Recipe


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="luoke_core")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--recipe", required=True)
    s.add_argument("--model-dir", required=True)
    s.add_argument("--env", default=None)
    s.add_argument("--socket", default=None)
    s.add_argument("--lab", action="store_true")
    s.add_argument("--accessory-dir", action="append", default=[])
    s.add_argument("--mem-gb", type=float, default=None)
    s.add_argument("--tier", default=None, help="按这一档的策略和任务参数跑（实验室评测用；内存仍按真机算）")
    s.add_argument("--no-smoke", action="store_true", help="启动时不做冒烟检查（默认做，同一组合通过过就不重做）")
    s.add_argument("--parent-watch", action="store_true", help="父进程（主控 .app）没了就退出，免得引擎成孤儿；终端里起的不要带")
    s.add_argument("--release", default=None, help="发布清单 release.json（没有就不能下载模型）")
    sub.add_parser("version")
    a = ap.parse_args(argv)
    if a.cmd == "version":
        print(__version__)
        return 0
    env = a.env or os.path.expanduser("~/Library/Application Support/" + ("luoke-core-lab" if a.lab else "luoke-core"))
    sock = a.socket or os.path.join(env, "run", "hub.sock")
    if len(sock.encode("utf-8")) > 100:
        sys.exit(f"socket 路径太长（macOS 的 AF_UNIX 限 104 字节）：{sock}；用 --socket 指定短路径")
    recipe = Recipe.load(a.recipe)
    hub = Hub(recipe, env, os.path.abspath(a.model_dir), lab=a.lab, accessory_dirs=a.accessory_dir, mem_gb=a.mem_gb,
              smoke=not a.no_smoke, release=os.path.abspath(a.release) if a.release else None, tier=a.tier)
    srv = serve(hub, sock)          # 先占住 socket 并开始应答（启动期间只答 /status），再起引擎：起不来时不会留下孤儿引擎
    hub.socket_path = sock
    sock_ino = os.stat(sock).st_ino
    server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    server_thread.start()
    stopping = threading.Event()

    def stop(*_):
        stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        try:
            hub.start()
        except Exception as e:  # noqa: BLE001
            print(f"luoke-core 启动失败：{e}", flush=True)
            raise
        print(f"luoke-core {__version__} tier={hub.tier_name} base={hub.base_id} socket={sock}", flush=True)
        while not stopping.wait(0.5):
            if a.parent_watch and os.getppid() == 1:
                print("父进程没了，退出", flush=True)
                break
    finally:
        srv.shutdown()
        srv.server_close()
        hub.stop()
        try:
            if os.stat(sock).st_ino == sock_ino:   # 只删自己绑的那个，别删后来者的
                os.remove(sock)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
