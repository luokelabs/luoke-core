"""`luoke`：给高级用户的命令行（螺壳 .app 自带，顶层 ADR 0014 修订"给高级用户的本机端点"）。

    luoke launch pi [--model <名字>] [-- pi 的其他参数]     配好 pi、用主控的本机端点起 pi
    luoke endpoint                                           端点的状态（端口、能用的模型；不印密钥）
    luoke install-cli [--dir ~/.local/bin]                   在 PATH 里放一个指向这个命令的链接

做法照 oMLX 的 `omlx launch pi`（integrations/pi.py）：往 pi 的 models.json 写一个名叫 luoke 的 provider（先备份；尊重
PI_CODING_AGENT_DIR；不动原有的 omlx、local 等），把 defaultProvider 设成它，再 exec `pi --model luoke/<模型>`。
端口和密钥从主控读，不用用户复制；端点没开就打开并说一句。以后要加 claude、codex、opencode，照这个加一个 launch_<工具>。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

from . import __version__

SOCKET = os.path.expanduser("~/Library/Application Support/luoke-core/run/hub.sock")
WHO = f"luoke-core/{__version__}"


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30):
        super().__init__("hub", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def hub(method: str, path: str, body: dict | None = None, sock: str = SOCKET) -> dict:
    if not os.path.exists(sock):
        sys.exit("螺壳没在运行（找不到它的 socket）。先打开「螺壳」（应用程序文件夹里的名字）。")
    c = _Unix(sock)
    try:
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"X-Luoke-Accessory": WHO, "Content-Type": "application/json"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
    except (OSError, ValueError) as e:
        sys.exit(f"连不上螺壳：{e}")
    finally:
        c.close()
    if r.status >= 400:
        sys.exit(f"螺壳答复：{data.get('message') or data.get('code') or r.status}")
    return data


def endpoint_info(sock: str = SOCKET, enable: bool = True) -> dict:
    info = hub("GET", "/endpoint", sock=sock)
    if not info.get("enabled") or not info.get("port"):
        if not enable:
            return info
        print("螺壳的本机端点没开，现在打开（设置 → 高级里可以再关掉）。", file=sys.stderr)
        hub("PUT", "/settings", {"endpoint_enabled": True}, sock=sock)
        for _ in range(20):
            info = hub("GET", "/endpoint", sock=sock)
            if info.get("port"):
                break
            time.sleep(0.25)
    return info


def list_models(port: int, key: str) -> list:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.request("GET", "/v1/models", headers={"Authorization": f"Bearer {key}"} if key else {})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
    finally:
        c.close()
    if r.status != 200:
        sys.exit(f"本机端点答复 {r.status}：{(data.get('error') or {}).get('message', '')}")
    return [m["id"] for m in data.get("data") or []]


def pick(models: list, want: str | None) -> str:
    if want:
        if want not in models:
            sys.exit(f"没有模型 {want}。能用的：" + "、".join(models))
        return want
    if not models:
        sys.exit("本机端点上没有能用的模型。")
    if len(models) == 1 or not sys.stdin.isatty():
        return models[0]
    print("用哪个模型？")
    for i, m in enumerate(models, 1):
        print(f"  {i}. {m}")
    while True:
        a = input(f"选 1–{len(models)}（回车 = 1）：").strip()
        if not a:
            return models[0]
        if a.isdigit() and 1 <= int(a) <= len(models):
            return models[int(a) - 1]


def pi_agent_dir() -> Path:
    env = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(env).expanduser() if env else Path.home() / ".pi" / "agent"


def _update_json(path: Path, update) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            existing = {}
        backup = path.with_name(path.name + ".luoke-backup")
        if not backup.exists():                      # 第一次改之前留一份原样的
            shutil.copy2(path, backup)
    update(existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def configure_pi(base_url: str, key: str, models: list, model: str, agent_dir: Path) -> None:
    def models_json(cfg: dict) -> None:
        cfg.setdefault("providers", {})
        cfg["providers"]["luoke"] = {          # 只动 luoke 这一个，原有的 omlx、local 等不碰
            "baseUrl": base_url, "api": "openai-completions",
            "apiKey": key or "luoke", "authHeader": bool(key),
            "models": [{"id": m, "name": m, "reasoning": False, "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}} for m in models],
        }

    def settings_json(cfg: dict) -> None:
        cfg["defaultProvider"] = "luoke"
        cfg["defaultModel"] = model

    _update_json(agent_dir / "models.json", models_json)
    _update_json(agent_dir / "settings.json", settings_json)


def scrubbed_env() -> dict:
    env = os.environ.copy()
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"):
        env.pop(k, None)                          # 主控自带的 Python 的变量别带给 pi 和它起的子进程
    return env


def launch_pi(a) -> None:
    if not shutil.which("pi"):
        sys.exit("没找到 pi。装法：npm install -g @mariozechner/pi-coding-agent")
    info = endpoint_info(a.socket)
    port, key = int(info["port"]), info.get("key") or ""
    models = list_models(port, key)
    model = pick(models, a.model)
    base_url = f"http://127.0.0.1:{port}/v1"
    agent_dir = pi_agent_dir()
    configure_pi(base_url, key, models, model, agent_dir)
    print(f"pi 用螺壳的本机端点：{base_url}，模型 {model}（配置写在 {agent_dir}，原来的留了 .luoke-backup）", file=sys.stderr)
    os.execvpe("pi", ["pi", "--model", f"luoke/{model}"] + list(a.rest), scrubbed_env())


def show_endpoint(a) -> None:
    info = endpoint_info(a.socket, enable=False)
    print(json.dumps({k: info.get(k) for k in ("enabled", "port", "models", "usage_today")}, ensure_ascii=False, indent=1))


def install_cli(a) -> None:
    me = os.path.realpath(os.environ.get("LUOKE_CLI_SELF") or sys.argv[0])
    d = Path(a.dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    link = d / "luoke"
    if link.is_symlink() or link.exists():
        if not link.is_symlink():
            sys.exit(f"{link} 已经是一个文件，不覆盖")
        link.unlink()
    link.symlink_to(me)
    print(f"已放好 {link} → {me}" + ("" if str(d) in os.environ.get("PATH", "").split(":") else f"（{d} 还不在 PATH 里）"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="luoke", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", default=SOCKET, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("launch", help="配好一个工具、用螺壳的本机端点起它")
    lp.add_argument("tool", choices=["pi"])
    lp.add_argument("--model")
    sub.add_parser("endpoint", help="本机端点的状态")
    ip = sub.add_parser("install-cli", help="在 PATH 里放一个 luoke 的链接")
    ip.add_argument("--dir", default="~/.local/bin")
    argv = list(sys.argv[1:] if argv is None else argv)
    rest = []
    if "--" in argv:                    # -- 后面的原样交给工具
        i = argv.index("--")
        argv, rest = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    a.rest = rest
    if a.cmd == "launch":
        launch_pi(a)
    elif a.cmd == "endpoint":
        show_endpoint(a)
    elif a.cmd == "install-cli":
        install_cli(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
