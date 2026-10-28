"""已装配件：读各 .app 里的配件清单（V1 §8），以及组件配件的命令行（螺壳记忆的 ui）。"""
from __future__ import annotations

import glob
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from typing import Optional

from . import apps, procs

MANIFEST_REL = "Contents/Resources/luoke-accessory.json"
PYTHON_REL = "Contents/Resources/python/bin/python3"
MEMORY_BUNDLE_ID = "ai.luoke.memory"


def default_memory_path() -> str:
    home = os.environ.get("LUOKE_HOME") or os.path.expanduser("~/.luoke")
    return os.path.join(home, "mem.db")


def load_manifest(app: str) -> Optional[dict]:
    p = os.path.join(app, MANIFEST_REL)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    if m.get("contract") != 1 or not m.get("accessory") or not m.get("bundle_id"):
        return None
    m["_app"] = app
    return m


DEFAULT_DIRS = [os.path.expanduser("~/Applications"), os.path.expanduser("~/Library/Input Methods")]


def home_is_real() -> bool:
    """HOME 是不是这个账户真正的主目录（没被改到测试目录）。实验室实例据此决定能不能删配件声明的 ~ 下数据。"""
    import pwd
    try:
        real = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return True        # 查不到就当是真的（宁可不删）
    return os.path.realpath(os.path.expanduser("~")) == os.path.realpath(real)


BUNDLED_MEMORY_REL = "Contents/Resources/memory/luoke-accessory.json"   # 螺壳记忆并进主控后，它的清单在主控 .app 里（V1 §6）


def hub_app() -> Optional[str]:
    """主控自己的 .app：自带 Python 在 <app>/Contents/Resources/python/bin/ 下（LUOKE_HUB_APP 可指定，测试用）。开发时用别的 Python 跑就是 None。"""
    env = os.environ.get("LUOKE_HUB_APP")
    if env:
        return env
    exe = os.path.realpath(sys.executable)
    marker = os.sep + os.path.join("Contents", "Resources", "python", "bin") + os.sep
    if marker in exe:
        return exe.split(marker)[0]
    return None


def bundled_memory_lib(app: Optional[str] = None) -> Optional[dict]:
    """主控 .app 里那份 luoke_mem 在哪、什么版本：{"path": site-packages 目录, "version", "python": 自带的解释器}；不是打着记忆的主控（实验室、开发版）就 None。
    配件不再各带一份、直接用这份（10-19 定，luoke-mem 牵头；工具插到 sys.path 最前面再 import，没有就退回自带的兜底那份）。"""
    mem = bundled_memory(app)
    if not mem:
        return None
    hits = sorted(glob.glob(os.path.join(mem["_app"], "Contents", "Resources", "python", "lib", "python3.*", "site-packages", "luoke_mem", "__init__.py")))
    if not hits:
        return None
    out = {"path": os.path.dirname(os.path.dirname(hits[-1])), "version": mem.get("version")}
    py = os.path.join(mem["_app"], "Contents", "Resources", "python", "bin", "python3")
    if os.path.exists(py):
        out["python"] = py      # 不带 Python 的工具（体检是 Swift）用它跑 -m luoke_mem，不用从 path 往上猜布局（luoke-core#17）
    return out


def bundled_memory(app: Optional[str] = None) -> Optional[dict]:
    """主控 .app 里带的螺壳记忆清单；_app 指主控 .app（它和配件 .app 布局一样，命令、空闲任务都用它自带的 Python 跑）。"""
    app = app or hub_app()
    if not app:
        return None
    p = os.path.join(app, BUNDLED_MEMORY_REL)
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    if m.get("contract") != 1 or not m.get("accessory") or not m.get("bundle_id"):
        return None
    m["_app"] = app
    m["_bundled"] = True
    return m


def installed(extra_dirs: Optional[list] = None, only: Optional[list] = None, hub: Optional[str] = None, bundled: bool = True) -> dict:
    """bundle id → 清单。扫 ~/Applications、~/Library/Input Methods 和指定目录下的 .app；only 给了就只扫它（测试用）。
    主控 .app 里带着螺壳记忆时，记忆以那份为准（盖过 ~/Applications 里旧的螺壳记忆.app，那份在启动时迁走）。"""
    dirs = list(only) if only is not None else DEFAULT_DIRS + list(extra_dirs or [])
    out = {}
    for d in dirs:
        for app in glob.glob(os.path.join(d, "*.app")):
            m = load_manifest(app)
            if m:
                out[m["bundle_id"]] = m
    mem = bundled_memory(hub) if bundled and (only is None or hub) else None
    if mem:
        out[mem["bundle_id"]] = mem
    return out


def migrate_old_memory_app(dirs: Optional[list] = None, hub: Optional[str] = None, log=print) -> Optional[str]:
    """记忆并进主控以后，旧的「螺壳记忆.app」（bundle id ai.luoke.memory，单独装在 ~/Applications）结束进程、注销登记、删掉。
    mem.db 和 ~/.luoke 下的东西一概不动（V1 §6）。主控里没带记忆就什么都不做。返回删掉的路径。"""
    mem = bundled_memory(hub)
    if not mem:
        return None
    app_path = hub or hub_app()
    hub_apps = {os.path.realpath(os.path.join(d, f"{n}.app")) for d in ("/Applications", os.path.expanduser("~/Applications")) for n in ("螺壳", "螺壳主控")}
    if dirs is None and os.path.realpath(app_path or "") not in hub_apps:
        return None     # 只有装好的那份主控（/Applications 或 ~/Applications，ADR 0012）才迁（开发、测试构建不动本机用户的螺壳记忆.app）
    for d in (dirs if dirs is not None else DEFAULT_DIRS):
        for app in glob.glob(os.path.join(d, "*.app")):
            m = load_manifest(app)
            if not m or m.get("bundle_id") != MEMORY_BUNDLE_ID:
                continue
            if _version_tuple(m.get("version")) > _version_tuple(mem.get("version")):
                log(f"migrate memory: old {app} is {m.get('version')}, newer than the bundled {mem.get('version')}; left alone")
                return None
            killed = terminate_app_processes(app)
            try:
                keep = apps.retire_app(app, _previous_dir(), "螺壳记忆.app")     # 不直接删：留在环境目录的 previous.noindex 里，出问题能放回去
            except OSError as e:
                log(f"migrate memory: could not move {app}: {e}")
                return None
            log(f"migrate memory: moved old {app} ({m.get('version')}) to {keep}, stopped {killed} process(es); mem.db untouched")
            return app
    return None


def _version_tuple(v) -> tuple:
    out = []
    for x in str(v or "0").split("."):
        num = "".join(ch for ch in x if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def _previous_dir() -> str:
    """主控环境目录下的 previous.noindex（.noindex：Spotlight 不收录，旧版不会被登记进 LaunchServices）。LUOKE_ENV_DIR 可指定。"""
    env = os.environ.get("LUOKE_ENV_DIR") or os.path.expanduser("~/Library/Application Support/luoke-core")
    return os.path.join(env, "previous.noindex")


def by_accessory(manifests: dict, accessory: str) -> Optional[dict]:
    for m in manifests.values():
        if m["accessory"] == accessory:
            return m
    return None


def run_command(manifest: dict, name: str, fill: dict, timeout: float = 30) -> subprocess.CompletedProcess:
    """用配件自带的 Python 跑清单里的命令。环境只给 PATH、HOME，并设 PYTHONDONTWRITEBYTECODE=1。"""
    args = manifest.get("commands", {}).get(name)
    if not args:
        raise KeyError(name)
    py = os.path.join(manifest["_app"], PYTHON_REL)
    argv = [py] + [a.format(**fill) if isinstance(a, str) else a for a in args]
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    if os.environ.get("LUOKE_HOME"):
        env["LUOKE_HOME"] = os.environ["LUOKE_HOME"]
    return subprocess.run(argv, env=env, cwd="/", capture_output=True, text=True, timeout=timeout)


def forget_tool(manifests: dict, accessory: str, dry_run: bool) -> dict:
    m = manifests.get(MEMORY_BUNDLE_ID)
    if not m:
        return {"code": "unsupported"}
    args = list(m["commands"]["forget-tool"]) + (["--dry-run"] if dry_run else [])
    m2 = dict(m, commands={"forget-tool": args})
    r = run_command(m2, "forget-tool", {"memory": default_memory_path(), "tool": accessory}, timeout=120)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[:300], "exit": r.returncode}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "输出不是 JSON"}


LSREGISTER = apps.LSREGISTER


def memory_root() -> str:
    return os.path.dirname(default_memory_path())


MEMORY_FILE_PREFIX = "mem.db"   # 记忆文件和它的旁文件（mem.db-ui.json、mem.db-learn.lock、-wal、-shm）都归用户


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(a).casefold() == os.path.normcase(b).casefold()


def _inside(path: str, root: str) -> bool:
    """path 在 root 里面（或就是 root）：不分大小写比路径段（APFS 默认不分大小写，realpath 不规范化大小写；2026-10-16 审查发现）。"""
    rel = os.path.relpath(path, root)
    return not (rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel))


def own_data_forms(manifest: dict) -> list:
    """一个配件能声明成"我的数据"的路径（相对主目录）：只认和它自己的标识绑定的几种位置，别的一律不删（10-19：卸载只认清单声明的目录，不再猜）。
    标识 id = 清单 accessory（例如 luoke-mail），short = 去掉 luoke- 的部分（mail），bundle = bundle_id。"""
    acc = str(manifest.get("accessory") or "")
    bundle = str(manifest.get("bundle_id") or "")
    if not acc.startswith("luoke-") or len(acc) <= len("luoke-"):
        return []
    short = acc[len("luoke-"):]
    forms = [f".{acc}", f".luoke/{short}", f"Library/Application Support/{acc}", f"Library/Caches/{acc}", f"Library/Logs/{acc}"]
    if bundle:
        forms += [f"Library/Application Support/{bundle}", f"Library/Caches/{bundle}", f"Library/Logs/{bundle}",
                  f"Library/Preferences/{bundle}.plist", f"Library/Containers/{bundle}", f"Library/HTTPStorages/{bundle}",
                  f"Library/Saved Application State/{bundle}.savedState"]
    return forms


def data_dirs(manifest: dict) -> list:
    """配件清单 data 字段里能删的路径（V1 §6、DESIGN §9）：声明的每一项必须正好是 own_data_forms 里的一种（不分大小写），
    而且路径上除了最后一段都不是软链接（最后一段是软链接就只删链接）；记忆永远不删（里面有 mem.db 一类的文件就不删，顶层 ADR 0014）。
    不合规则的静默丢掉，只列存在的。"""
    home = os.path.realpath(os.path.expanduser("~"))
    forms = {f.casefold() for f in own_data_forms(manifest)}
    out = []
    for d in manifest.get("data") or []:
        if not isinstance(d, str) or not d.startswith("~/"):
            continue
        rel = os.path.normpath(d[2:])
        if rel.startswith("..") or rel.casefold() not in forms:
            continue
        p = os.path.join(home, rel)
        if not os.path.lexists(p) or os.path.realpath(os.path.dirname(p)).casefold() != os.path.dirname(p).casefold():
            continue        # 上层目录经软链接指到别处：不删
        if os.path.basename(p).casefold().startswith(MEMORY_FILE_PREFIX):
            continue
        if os.path.isdir(p) and not os.path.islink(p) and any(
                f.casefold().startswith(MEMORY_FILE_PREFIX) for _r, _d, files in os.walk(p) for f in files):
            continue        # 里面有 mem.db 一类的文件：记忆归用户，不删
        if p not in out:
            out.append(p)
    return out


def app_processes(app: str) -> list:
    """可执行文件在这个 .app 里的进程的 pid：按内核给的可执行文件真实路径认（procs.in_app），不按名字匹配、不解析 ps 的文字
    （顶层 CLAUDE.md：不用 pkill -f 这类宽泛匹配；10-19：ps 在没有 UTF-8 locale 时把中文路径印成转义）。"""
    return procs.in_app(app)


def terminate_app_processes(app: str, grace: float = 5.0) -> int:
    """结束 .app 里残留的进程（本体按 bundle id 结束不到的——例如 Python 配件的启动器 exec 出来的 python3，
    16 GB 测试机 2026-10-16 实测邮件助手的 python3 在 .app 删掉后还活着）：先 SIGTERM，等 grace 秒，再 SIGKILL。返回结束的个数。"""
    pids = app_processes(app)
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + grace
    alive = list(pids)
    while alive and time.time() < deadline:
        time.sleep(0.2)
        alive = [p for p in alive if _alive(p)]
    for p in alive:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    return len(pids)


def after_swap(app: str, swapped_out: str, input_method: bool) -> int:
    """换包（升级、回到上一版）之后：结束还在跑换下来那份代码的进程，输入法再立刻拉起新包（10-24 开发机：0.1.13 → 0.1.16 后，
    旧进程一直在跑旧代码。换包前结束的话，系统下一次按键会马上从还没换的旧包里再拉起一个，跟着旧包挪进 previous.noindex，
    换包前那一下就白结束了；所以输入法改成换好以后再结束）。旧壳按包路径找资源、起引擎，换完不能留着它（luoke-ime 10-24）。
    返回结束的个数。拉起用 open -g（不抢前台），系统不一定马上按键时拉起（32 GB 测试机上见过）。"""
    killed = terminate_app_processes(swapped_out, grace=3.0) if swapped_out and os.path.exists(swapped_out) else 0
    if input_method:
        # 换好以后又有用旧代码起来的？按路径认不出（路径是新包的），按启动时间也不可靠：交给输入法自己核版本（luoke-ime 0.1.18 起）
        try:
            subprocess.run(["/usr/bin/open", "-g", app], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return killed


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _previous_copy(previous_dir: str, app: str, bundle_id: Optional[str]) -> Optional[str]:
    """换装时挪进 previous.noindex 的旧版（同名、同 bundle id）；没有就 None。"""
    keep = os.path.join(previous_dir, os.path.basename(app))
    if not bundle_id or not os.path.isdir(keep) or os.path.islink(keep):
        return None
    try:
        with open(os.path.join(keep, "Contents", "Info.plist"), "rb") as f:
            return keep if plistlib.load(f).get("CFBundleIdentifier") == bundle_id else None
    except (OSError, ValueError):
        return None


def app_version(app: str) -> Optional[str]:
    """一份配件 .app 清单里的版本号（previous.noindex 里的旧版也这么读，不运行它：0.1.6 及更早的输入法不带参数运行会起第二个
    IMKServer，luoke-ime 10-20）。"""
    try:
        with open(os.path.join(app, MANIFEST_REL), encoding="utf-8") as f:
            return str(json.load(f).get("version") or "") or None
    except (OSError, ValueError):
        return None


def rollback_candidates(manifests: dict, previous_dir: Optional[str] = None) -> list:
    """装着的、previous.noindex 里留有旧版的配件：[{accessory, name, type, bundle_id, app, current, previous, path}]。"""
    out = []
    for m in manifests.values():
        if m.get("_bundled"):
            continue
        keep = _previous_copy(previous_dir or _previous_dir(), m["_app"], m.get("bundle_id"))
        old = app_version(keep) if keep else None
        if keep and old and old != m.get("version"):
            out.append({"accessory": m["accessory"], "name": m.get("name", m["accessory"]), "type": m.get("type", "app"),
                        "bundle_id": m.get("bundle_id"), "app": m["_app"], "current": m.get("version"), "previous": old, "path": keep})
    return sorted(out, key=lambda c: c["accessory"])


def rollback(manifests: dict, accessory: str, previous_dir: Optional[str] = None, verify: bool = True) -> dict:
    """把配件换回 previous.noindex 里的旧版（issue #12，10-20 定进 beta1）：先核旧版的签名和 bundle id（不运行它），结束现在
    这份的进程（SIGTERM，等一会儿再 SIGKILL；输入法 0.1.7 起收到 SIGTERM 会先报 exiting），两处对调：旧版回原处并登记，现在这份
    进 previous.noindex 并注销（再点一次就换回来）。不动输入源（bundle id 和输入模式没变，不再请求启用，luoke-ime 10-20）。
    调用方先确认主控认这个旧版本（验证记录），并停掉它的任务。"""
    c = next((x for x in rollback_candidates(manifests, previous_dir) if x["accessory"] == accessory), None)
    if not c:
        return {"code": "unsupported", "error": f"{accessory} 没有可以换回的上一版"}
    if verify:
        from . import download
        try:
            download.verify_app(c["path"], c.get("bundle_id"))
        except download.DownloadError as e:
            return {"code": "invalid_request", "error": f"上一版没通过签名检查：{e}"}
    im = c.get("type") == "input-method"
    killed = 0 if im else terminate_app_processes(c["app"], grace=3.0)     # 输入法换好再结束（after_swap）
    try:
        apps.swap_app(c["app"], c["path"])
    except OSError as e:
        return {"code": "engine_failed", "error": f"没能换回上一版（{e}），现在这一版还在原处"}
    apps.unregister_dir(os.path.dirname(c["path"]))      # 核签名、对调之后再扫一遍备份目录（luoke-ime 10-22）
    killed += after_swap(c["app"], c["path"], im)
    return dict(c, killed=killed, **{"from": c["current"], "to": c["previous"]})


def reregister(manifests: dict, accessory: str) -> dict:
    """重新登记一个装着的配件（luoke-ime#15：系统把输入源绑到了别的副本上、切不过去）：结束它在跑的进程（按路径），注销同 bundle id 的
    别的副本，用 ditto 复制一份新的换上原位（新 inode，LaunchServices 记一条新记录，系统才重新绑定；同盘挪开再挪回 inode 不变，
    可能还认成原来那条——恢复时 rm 后 ditto 重放，记录号才变），登记新的、删掉换下的。不改内容、不重下。"""
    m = by_accessory(manifests, accessory)
    if not m or m.get("_bundled"):
        return {"code": "unsupported", "error": f"没装 {accessory}"}
    app, bid = m["_app"], m.get("bundle_id")
    killed = terminate_app_processes(app, grace=3.0)
    others = apps.unregister_other_copies(bid, app) if bid else []
    stage = app.rstrip("/") + f".reregister-new-{os.getpid()}"
    old = app.rstrip("/") + f".reregister-old-{os.getpid()}"
    try:
        subprocess.run(["/usr/bin/ditto", app, stage], check=True, capture_output=True)
        apps.move_app(app, old, register=False)
        try:
            apps.move_app(stage, app, register=True)
        except OSError:
            apps.move_app(old, app, register=True)      # 放不回新的：旧的放回原处
            raise
        apps.remove_app(old)
    except (OSError, subprocess.CalledProcessError) as e:
        if os.path.exists(stage):
            apps.remove_app(stage)
        return {"code": "engine_failed", "error": f"没能重新登记：{e}"}
    return {"accessory": accessory, "app": app, "unregistered_others": len(others), "killed": killed}


def restart_input_method(manifests: dict, accessory: str) -> dict:
    """重新启动输入法（luoke-ime#18：螺壳升级后输入法菜单里那一项变灰、切不过去，重启输入法进程就好；系统菜单栏那边缓存了过时的
    "不可用"，输入法自己察觉不到，它自己的菜单变灰时也打不开，所以放在「应用」窗口里）：和换版以后一样，按包路径结束在跑的进程，
    再 open -g 拉起。不动登记、不动输入源。只认输入法。"""
    m = by_accessory(manifests, accessory)
    if not m or m.get("_bundled"):
        return {"code": "unsupported", "error": f"没装 {accessory}"}
    if m.get("type") != "input-method":
        return {"code": "unsupported", "error": f"{accessory} 不是输入法"}
    killed = terminate_app_processes(m["_app"], grace=3.0)
    try:
        r = subprocess.run(["/usr/bin/open", "-g", m["_app"]], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"code": "engine_failed", "error": f"没能拉起：{e}"}
    if r.returncode != 0:
        return {"code": "engine_failed", "error": "没能拉起：" + (r.stderr.decode(errors="replace").strip() or f"open 退出码 {r.returncode}")}
    return {"accessory": accessory, "app": m["_app"], "killed": killed}


def uninstall_plan(manifests: dict, accessory: str, preview_forget: bool = True, previous_dir: Optional[str] = None) -> dict:
    """卸载前先算清楚要动什么：.app、换装时留下的旧版（previous.noindex 里的，issue #14：之前只有「卸载全部」才删）、能删的数据路径、
    记忆里能忘掉多少（配件声明读写记忆、螺壳记忆装着时跑 forget-tool --dry-run）。dry_run 的答复就是它；界面按它画勾选框。"""
    m = by_accessory(manifests, accessory)
    if not m:
        return {"code": "unsupported", "error": f"没装 {accessory}"}
    if m.get("_bundled"):
        return {"code": "invalid_request", "error": "螺壳记忆是主控的一部分，不能单独卸载；「卸载全部」会连主控一起删（记忆文件保留）"}
    is_memory = m.get("bundle_id") == MEMORY_BUNDLE_ID
    others = [x["accessory"] for x in manifests.values() if x["accessory"] != accessory and x.get("memory")]
    can_forget = (not is_memory and bool(m.get("memory")) and MEMORY_BUNDLE_ID in manifests
                  and "forget-tool" in (manifests[MEMORY_BUNDLE_ID].get("commands") or {}))
    plan = {"accessory": accessory, "name": m.get("name", accessory), "type": m.get("type", "app"), "bundle_id": m.get("bundle_id"),
            "app": m["_app"], "previous": _previous_copy(previous_dir or _previous_dir(), m["_app"], m.get("bundle_id")),
            "data": data_dirs(m), "forget": None, "can_forget": can_forget, "memory_needed_by": others if is_memory else []}
    if preview_forget and can_forget:
        r = forget_tool(manifests, accessory, dry_run=True)
        if not r.get("error") and not r.get("code"):
            plan["forget"] = {"records": int(r.get("records") or 0), "notes": int(r.get("notes") or 0)}
    return plan


def uninstall(manifests: dict, accessory: str, data: bool = False, forget: bool = False, dry_run: bool = False,
              previous_dir: Optional[str] = None) -> dict:
    """卸载一个配件（V1 §6，2026-10-16 定进 beta1）。顺序：忘记忆（可选；只动这个工具的记录，经螺壳记忆.app 的 forget-tool；退出码非 0 只提示）
    → 删清单声明的数据路径（可选，默认保留）→ 注销 LaunchServices、删 .app。记忆文件永远不碰；卸螺壳记忆本身只删 app。调用方保证配件进程已结束。"""
    plan = uninstall_plan(manifests, accessory, preview_forget=dry_run, previous_dir=previous_dir)
    if plan.get("code"):
        return plan
    m = by_accessory(manifests, accessory)
    result = dict(plan, dry_run=dry_run, removed=[], forgot=None, cleanup=None, killed=0, errors=[])
    if dry_run:
        return result
    result["killed"] = terminate_app_processes(plan["app"])   # 本体已按 bundle id 结束过；这里兜住它找不到的（启动器 exec 出的 python3）
    if forget and plan["can_forget"]:     # 真卸载时不预跑 dry-run，plan["forget"] 是 None；按"能不能忘"判断（2026-10-16 审查：之前勾了也从不执行）
        r = forget_tool(manifests, accessory, dry_run=False)
        result["forgot"] = r
        if r.get("error") or r.get("code"):
            result["errors"].append("从记忆里忘掉：" + str(r.get("error") or r.get("code")))
    if data and "uninstall" in (m.get("commands") or {}):
        # 配件自己的数据收尾（例如邮件助手删它存在钥匙串里的授权码，2026-10-16 与 luoke-mail 商定）：跟"同时删除我的数据"走——
        # 用户留着数据重装还能接着用，授权码也得留着；在它停掉之后、删文件之前跑；失败只提示
        try:
            r = run_command(m, "uninstall", {}, timeout=60)
            result["cleanup"] = {"exit": r.returncode, "output": (r.stdout or "").strip()[-300:]}
            if r.returncode != 0:
                result["errors"].append("它自己的收尾命令失败（" + str(r.returncode) + "）：" + (r.stderr or r.stdout).strip()[:200])
        except (OSError, subprocess.TimeoutExpired) as e:
            result["errors"].append(f"它自己的收尾命令没跑成：{e}")
    if data:
        for p in plan["data"]:
            try:
                if os.path.islink(p) or not os.path.isdir(p):
                    os.remove(p)
                else:
                    shutil.rmtree(p)
                result["removed"].append(p)
            except OSError as e:
                result["errors"].append(f"{p}：{e}")
    for app in (plan["app"], plan["previous"]):     # 旧版不是用户数据，跟 .app 一起删
        if not app:
            continue
        try:
            apps.remove_app(app)
            result["removed"].append(app)
        except OSError as e:
            result["errors"].append(f"{app}：{e}")
    if plan.get("bundle_id"):     # 别处还登记着的副本（废纸篓里的旧包等）也注销，免得重装时系统绑到它们上（luoke-ime#15）
        apps.unregister_other_copies(plan["bundle_id"], plan["app"])
    return result


PUBLIC_FIELDS = ("accessory", "bundle_id", "name", "version", "type", "resident", "memory", "tasks", "capabilities", "jobs")


def public_list(manifests: dict) -> list:
    """GET /accessories：装了哪些配件，只给清单里的公开字段（给 luoke-agent 看装了哪些工具、声明了什么能力；主控只告诉、不传话）。"""
    out = []
    for m in manifests.values():
        d = {k: m[k] for k in PUBLIC_FIELDS if k in m}
        if "jobs" in d:
            d["jobs"] = [j.get("name") for j in d["jobs"] if isinstance(j, dict)]
        out.append(d)
    return sorted(out, key=lambda d: d.get("accessory", ""))
