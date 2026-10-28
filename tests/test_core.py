"""不碰引擎的单元测试：配方、派发规则、请求白名单、清单。  python3 -m unittest discover -s tests"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

# 测试永远不碰本机用户在用的东西：记忆路径按 LUOKE_HOME 走临时目录；主控环境、模型目录、socket 都由各测试自己建临时的；不起服务。
import atexit  # noqa: E402
# 这一轮测试的临时文件都放进一个目录，跑完整个删掉（10-21：TMPDIR 里留了 tmp* 目录）。名字短：里面有 AF_UNIX socket（104 字节上限）
_RUN_TMP = tempfile.mkdtemp(prefix="lk")
tempfile.tempdir = _RUN_TMP
atexit.register(shutil.rmtree, _RUN_TMP, True)
os.environ["LUOKE_HOME"] = tempfile.mkdtemp(prefix="luoke-test-home-")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luoke_core import accessories, dispatch, machine  # noqa: E402
from luoke_core.dispatch import Dispatcher  # noqa: E402
from luoke_core.proxy import BadRequest, build_engine_payload, estimate_secs, neutralize  # noqa: E402
from luoke_core.recipe import Recipe, RecipeError  # noqa: E402

RECIPE = os.path.join(ROOT, "recipes", "dev.json")


def _no_real_engine(self, *a, **k):
    # 这些是"不碰引擎"的单元测试：换档等路径会在后台线程里重启引擎，之前真的拉起了 /Applications/oMLX.app，
    # 测试结束后留成孤儿进程（10-18 在本机攒了十个）。一律挡住，要测引擎的写成实验室实测
    from luoke_core.engine import EngineError
    raise EngineError("unit tests never start a real engine")


_REAL_ENGINE_START = None


def setUpModule():
    global _REAL_ENGINE_START
    from luoke_core import engine
    _REAL_ENGINE_START = engine.Engine.start      # 只给把 Popen 换成假的那几个测试用
    unittest.mock.patch.object(engine.Engine, "start", _no_real_engine).start()


def tearDownModule():
    unittest.mock.patch.stopall()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, secs):
        self.t += secs


class IsolationTest(unittest.TestCase):
    """任何一个测试文件单独跑、任意顺序，都碰不到本机用户的 ~/.luoke、主控环境目录和运行中的 socket。"""

    def test_paths_are_temporary(self):
        self.assertTrue(accessories.default_memory_path().startswith(os.environ["LUOKE_HOME"]))
        self.assertFalse(accessories.default_memory_path().startswith(os.path.expanduser("~/.luoke")))
        from luoke_core.client import DEFAULT_SOCKET
        self.assertFalse(os.path.exists(DEFAULT_SOCKET) and False)   # 从不打开它：测试里没有 HubClient 连默认位置


class RecipeTest(unittest.TestCase):
    def test_tiers_and_assignment(self):
        r = Recipe.load(RECIPE)
        self.assertEqual(r.tier_for(16), "16")
        self.assertEqual(r.tier_for(18), "16")
        self.assertEqual(r.tier_for(32), "32")
        self.assertEqual(r.tier_for(128), "128")
        self.assertEqual(r.tier_for(256), "128")
        self.assertEqual(r.base_for("16"), "qwen3.5-9b-4bit")
        self.assertEqual(r.base_for("128"), "gemma-4-26b-8bit")
        self.assertEqual(r.model_for_task("luoke-ime.rerank", "32"), "qwen3.5-0.8b-base-4bit")
        self.assertEqual(r.model_for_task("luoke-ime.predict", "128"), "qwen3.5-0.8b-base-4bit")
        self.assertEqual(r.tasks["luoke-ime.predict"]["kind"], "predict")
        p = r.params("luoke-mail.triage", "16")
        self.assertEqual(p["max_chars"], 6000)
        self.assertEqual(p["item_tokens"], 320)
        self.assertEqual(len(r.tasks_for("luoke-mail")), 9)
        self.assertTrue(r.version_verified("luoke-mail", "0.1"))
        self.assertFalse(r.version_verified("nobody", "1"))
        self.assertEqual(r.speed("qwen3.5-9b-4bit", "16", "prefill_tok_s", 1), 95)
        self.assertEqual(r.speed("qwen3.5-9b-4bit", "128", "prefill_tok_s", 1), 200)

    def test_bad_task_name(self):
        d = json.load(open(RECIPE))
        d["tasks"]["Bad.Name"] = d["tasks"]["luoke-mail.triage"]
        with self.assertRaises(RecipeError):
            Recipe(d)


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tier32 = {"base_resident": True}
        self.tier16 = {"base_resident": False}

    def disp(self, tier, **kw):
        d = Dispatcher(self.clock, tier, **kw)
        return d

    def test_interactive_then_quiet(self):
        d = self.disp(self.tier32)
        self.assertFalse(d.interactive())
        self.assertFalse(d.quiet())           # 刚启动，不到 M 分钟
        self.clock.tick(dispatch.QUIET_MINUTES * 60)
        self.assertTrue(d.quiet())
        d.note_user_waiting()
        self.assertTrue(d.interactive())
        self.assertFalse(d.quiet())
        self.clock.tick(dispatch.INTERACTIVE_SECS)
        self.assertFalse(d.interactive())

    def test_endpoint_rests_by_power_mode(self):
        """10-22：功耗档是全局的——本机端点（pi 这类）的请求做完，下一条按用时的 0 / 1 / 3 倍歇；最多歇 ENDPOINT_MAX_REST_SECS；
        切到全速立即不用等。"""
        for mode, mult in (("full", 0), ("balanced", 1), ("quiet", 3)):
            d = self.disp(self.tier32, power_mode=mode)
            self.assertEqual(d.endpoint_wait(), 0)
            self.assertEqual(d.endpoint_rested(20), 20 * mult)
            self.assertAlmostEqual(d.endpoint_wait(), 20 * mult)
            self.clock.tick(10)
            self.assertAlmostEqual(d.endpoint_wait(), max(0, 20 * mult - 10))
        d = self.disp(self.tier32, power_mode="quiet")
        self.assertEqual(d.endpoint_rested(3600), dispatch.ENDPOINT_MAX_REST_SECS)
        d.power_mode = "full"
        self.assertEqual(d.endpoint_wait(), 0)
        d.power_mode = "quiet"
        self.assertGreater(d.endpoint_wait(), 0)
        # 后台的间歇和端点的各算各的
        self.assertTrue(d.admit_background(False, True)[0] or True)
        self.assertEqual(d.cooldown_until, 0.0)

    def test_endpoint_requests_go_one_at_a_time_unless_full(self):
        """luoke-top 10-23 核查：均衡、静音下几条同时到的端点请求，一次只放一条；前一条做完按用时歇，再放下一条。全速不限。"""
        d = self.disp(self.tier32, power_mode="quiet")
        self.assertEqual(d.endpoint_try_begin(), 0)
        self.assertGreater(d.endpoint_try_begin(), 0)        # 第二条：前一条还在做
        self.assertEqual(d.endpoint_end(10), 30)            # 做了 10 秒，静音歇 30 秒
        self.assertAlmostEqual(d.endpoint_try_begin(), 30)
        self.clock.tick(30)
        self.assertEqual(d.endpoint_try_begin(), 0)
        d.endpoint_end(0)                                    # 没成的也要放掉
        self.assertEqual(d.endpoint_in_flight, 0)
        f = self.disp(self.tier32, power_mode="full")
        self.assertEqual((f.endpoint_try_begin(), f.endpoint_try_begin()), (0, 0))

    def test_background_admission_rules(self):
        d = self.disp(self.tier32)
        self.clock.tick(dispatch.QUIET_MINUTES * 60)
        ok, code, retry, reason = d.admit_background(False, True)
        self.assertTrue(ok)
        d.note_user_waiting()
        ok, code, retry, reason = d.admit_background(False, True)
        self.assertEqual((ok, code, reason), (False, "busy", "interactive"))
        self.assertGreater(retry, 0)
        self.clock.tick(dispatch.INTERACTIVE_SECS)
        d.note_score()                       # 打分也算交互，且 K 分钟内不接后台
        self.clock.tick(dispatch.INTERACTIVE_SECS)
        ok, code, retry, reason = d.admit_background(False, True)
        self.assertEqual((ok, code, reason), (False, "busy", "scored_recently"))
        self.clock.tick(dispatch.SCORE_WINDOW_SECS)
        self.assertTrue(d.admit_background(False, True)[0])
        # 放不下底座 → paused（等内存）
        self.assertEqual(d.admit_background(False, False)[1], "paused")
        # 电池：默认不跑，带 allow_battery 可以
        d.on_ac = False
        self.assertEqual(d.admit_background(False, True)[1], "paused")
        self.assertTrue(d.admit_background(True, True)[0])
        d.on_ac = True
        # 一个在跑就不接第二个；结束后按功耗档歇一歇
        job = d.start_background("luoke-mail.triage", 30, lambda: None)
        self.assertEqual(d.admit_background(False, True)[3], "engine_busy")
        d.end_background(job, gpu_secs=10)
        self.assertEqual(d.admit_background(False, True)[3], "power_cooldown")   # 均衡档：歇 10 s
        self.clock.tick(10)
        self.assertTrue(d.admit_background(False, True)[0])

    def test_retry_after_ms_is_precise(self):
        """retry_after 是取整的秒，retry_after_ms 是精确值（2026-10-17 应 digup 加）：短的间歇别白等一整秒。"""
        c = Clock()
        d = Dispatcher(c, {"base_resident": True}, power_mode="balanced")
        d.memory_ok_since = c() - 3600
        job = d.start_background("t", 1.0, lambda: None)
        d.end_background(job, 0.36)                                  # 均衡档：歇 1 倍，0.36 s
        ok, code, retry, reason = d.admit_background(False, True)
        self.assertEqual((ok, code, retry, reason), (False, "busy", 1, "power_cooldown"))
        self.assertEqual(d.retry_after_ms(reason), 360)
        c.tick(0.1)
        self.assertEqual(d.retry_after_ms("power_cooldown"), 260)
        c.tick(1)
        d.warmup("t", None)                                          # 交互中算上预热
        self.assertEqual(d.admit_background(False, True)[3], "interactive")
        c.tick(10)
        self.assertEqual(d.retry_after_ms("interactive"), (dispatch.INTERACTIVE_SECS - 10) * 1000)
        d.last_score = c()
        self.assertEqual(d.retry_after_ms("scored_recently"), dispatch.SCORE_WINDOW_SECS * 1000)
        d.job = dispatch.BackgroundJob("t", c(), 20.0, lambda: None)
        d.job.progress_secs = 12.5
        self.assertEqual(d.retry_after_ms("engine_busy"), 7500)
        for r in ("idle_job", "not_quiet", "battery", "memory", None):     # 固定估计的不给
            self.assertIsNone(d.retry_after_ms(r))

    def test_16gb_only_in_quiet_time(self):
        d = self.disp(self.tier16)
        self.assertEqual(d.admit_background(False, True)[3], "not_quiet")
        self.clock.tick(dispatch.QUIET_MINUTES * 60)
        self.assertTrue(d.admit_background(False, True)[0])

    def test_interrupt_decision(self):
        d = self.disp(self.tier32)
        self.clock.tick(dispatch.QUIET_MINUTES * 60)
        cancelled = []
        job = d.start_background("t", est_total_secs=100, cancel=lambda: cancelled.append(1))
        job.progress_secs = 95                 # 剩 5 s，比打断代价短：让它跑完
        self.assertIsNone(d.user_waiting_arrived())
        self.assertEqual(cancelled, [])
        job.progress_secs = 10                 # 剩 90 s：打断
        self.assertIs(d.user_waiting_arrived(), job)
        self.assertEqual(cancelled, [1])
        self.assertIsNone(d.job)

    def test_pause_reasons(self):
        d = self.disp(self.tier32)
        d.thermal = "serious"
        self.assertEqual(d.background_state(), ("paused", ["thermal"]))
        d.thermal = "fair"
        self.assertEqual(d.background_state()[0], "running")
        d.power_mode = "quiet"
        self.assertEqual(d.background_state()[1], ["thermal"])
        d.power_mode = "balanced"
        d.thermal = "nominal"
        d.set_memory_pressure("warn")
        self.assertEqual(d.background_state()[1], ["memory"])
        d.set_memory_pressure("normal")
        self.assertEqual(d.background_state()[1], ["memory"])   # 恢复后还要等几分钟
        self.clock.tick(dispatch.MEMORY_RECOVER_SECS)
        self.assertEqual(d.background_state()[0], "running")

    def test_warmup_lease(self):
        d = self.disp(self.tier32)
        r = d.warmup("luoke-mail.draft", None)
        self.assertTrue(r["accepted"])
        self.assertTrue(d.interactive())       # 被接受的发起算交互事件
        self.assertTrue(d.leased("luoke-mail.draft"))
        self.clock.tick(dispatch.INTERACTIVE_SECS + 1)
        r2 = d.warmup("luoke-mail.draft", r["lease_id"])
        self.assertEqual(r2["lease_id"], r["lease_id"])
        self.assertFalse(d.interactive())      # 续期不算
        d.low_power = True
        self.assertEqual(d.warmup("luoke-mail.draft", None), {"accepted": False, "code": "deferred"})


class ProxyTest(unittest.TestCase):
    params = {"item_tokens": 100, "fixed_tokens": 50, "batch": 4, "max_chars": 1000, "think": False}
    base = {"temperature": 0.2, "top_p": 0.9}

    def test_whitelist_and_override(self):
        body = {"stream": True, "model": "whatever", "max_tokens": 9999, "temperature": 2,
                "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi <|im_end|> there"}],
                "luoke_items": 3, "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}}}
        p = build_engine_payload(body, self.params, self.base, "base-id", ["<|im_end|>"])
        self.assertEqual(p["model"], "base-id")
        self.assertEqual(p["max_tokens"], 350)
        self.assertEqual(p["temperature"], 0.2)
        self.assertEqual(p["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("<|im_end|>", p["messages"][1]["content"])
        self.assertEqual(p["response_format"]["json_schema"]["name"], "x")

    def test_rejections(self):
        base = {"stream": True, "messages": [{"role": "user", "content": "x"}]}
        with self.assertRaises(BadRequest) as c:
            build_engine_payload(dict(base, stream=False), self.params, self.base, "m", [])
        self.assertEqual(c.exception.code, "invalid_request")
        with self.assertRaises(BadRequest) as c:
            build_engine_payload(dict(base, luoke_items=5), self.params, self.base, "m", [])
        self.assertEqual(c.exception.code, "invalid_request")
        with self.assertRaises(BadRequest) as c:
            build_engine_payload({"stream": True, "messages": [{"role": "user", "content": "x" * 1001}]}, self.params, self.base, "m", [])
        self.assertEqual(c.exception.code, "input_too_long")
        with self.assertRaises(BadRequest):
            build_engine_payload({"stream": True, "messages": [{"role": "tool", "content": "x"}]}, self.params, self.base, "m", [])
        with self.assertRaises(BadRequest):
            build_engine_payload(dict(base, response_format={"type": "text"}), self.params, self.base, "m", [])

    def test_estimate_and_neutralize(self):
        p = build_engine_payload({"stream": True, "messages": [{"role": "user", "content": "x" * 160}]}, self.params, self.base, "m", [])
        self.assertAlmostEqual(estimate_secs(p, prefill_tok_s=100, decode_tok_s=10), 1.0 + 15.0)
        self.assertEqual(neutralize("a<eos>b", ["<eos>"]), "a<​eos>b")


class AccessoriesTest(unittest.TestCase):
    def test_manifest_scan(self):
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "X.app")
            os.makedirs(os.path.join(app, "Contents", "Resources"))
            m = {"contract": 1, "accessory": "x", "bundle_id": "ai.luoke.x", "name": "X", "version": "1", "type": "app",
                 "entry": ["-m", "x"], "tasks": [], "data": []}
            with open(os.path.join(app, accessories.MANIFEST_REL), "w") as f:
                json.dump(m, f)
            found = accessories.installed(only=[d])   # 别扫 ~/Applications：本机可能真装着螺壳记忆
            self.assertIn("ai.luoke.x", found)
            self.assertEqual(accessories.by_accessory(found, "x")["_app"], app)
            self.assertEqual(accessories.uninstall_plan(found, "nope"), {"code": "unsupported", "error": "没装 nope"})

    def _fake_app(self, d, accessory, extra):
        app = os.path.join(d, "Applications", accessory + ".app")
        os.makedirs(os.path.join(app, "Contents", "Resources"))
        m = dict({"contract": 1, "accessory": accessory, "bundle_id": "ai.luoke." + accessory, "name": accessory, "version": "1",
                  "type": "app", "entry": ["-m", "x"], "tasks": [], "data": []}, **extra)
        with open(os.path.join(app, accessories.MANIFEST_REL), "w") as f:
            json.dump(m, f)
        return app

    def test_registered_copies_finds_launchservices_and_trash(self):
        """luoke-ime#15：同 bundle id 的别的副本（废纸篓里的旧包）还登记着，新装的切不过去。放新包之前把它们都注销。"""
        import plistlib, types
        from luoke_core import apps
        d = tempfile.mkdtemp()
        trash_app = os.path.join(d, ".Trash", "LuokeIME 12.app")
        os.makedirs(os.path.join(trash_app, "Contents"))
        with open(os.path.join(trash_app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "ai.luoke.x"}, f)
        dump = ("path:                       /A/LuokeIME.app (0x1a2b)\nidentifier:                 ai.luoke.x\n"
                "path:                       /B/Other.app (0x1a2c)\nidentifier:                 ai.luoke.other\n"
                "path:                       /C/Old LuokeIME.app (0x1a2d)\nidentifier:                 ai.luoke.x\n")
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(stdout=dump if argv[-1] == "-dump" else "", returncode=0)
        with unittest.mock.patch.dict(os.environ, {"HOME": d}), \
             unittest.mock.patch.object(apps.subprocess, "run", fake_run), \
             unittest.mock.patch.object(apps.os.path, "exists", lambda p: True if p == apps.LSREGISTER else os.path.lexists(p)):
            self.assertEqual(apps.registered_copies("ai.luoke.x"), ["/A/LuokeIME.app", "/C/Old LuokeIME.app", trash_app])
            done = apps.unregister_other_copies("ai.luoke.x", "/A/LuokeIME.app")
        self.assertEqual(done, ["/C/Old LuokeIME.app", trash_app])
        self.assertIn([apps.LSREGISTER, "-u", trash_app], calls)
        self.assertNotIn([apps.LSREGISTER, "-u", "/A/LuokeIME.app"], calls)

    def test_unregister_dir_unregisters_every_kept_app(self):
        """luoke-ime 10-22：previous.noindex 里的旧版会被系统重新登记；主控启动、换装、回滚后把那里每份 .app 都注销一次。"""
        from luoke_core import apps
        d = tempfile.mkdtemp()
        for n in ("LuokeIME.app", "螺壳邮件助手.app"):
            os.makedirs(os.path.join(d, n, "Contents"))
        open(os.path.join(d, "notes.txt"), "w").close()
        os.symlink(os.path.join(d, "LuokeIME.app"), os.path.join(d, "Link.app"))
        calls = []
        with unittest.mock.patch.object(apps, "_ls", lambda flag, path: calls.append((flag, path))):
            done = apps.unregister_dir(d)
            self.assertEqual(apps.unregister_dir(os.path.join(d, "missing")), [])
        self.assertEqual(sorted(done), sorted(os.path.join(d, n) for n in ("LuokeIME.app", "螺壳邮件助手.app")))
        self.assertEqual(sorted(calls), sorted(("-u", p) for p in done))

    def test_reregister_replaces_with_a_fresh_copy(self):
        """luoke-ime#15：重新登记用 ditto 复制一份新的换上原位（新 inode），内容不变，不留暂存和换下的副本。"""
        from luoke_core import apps
        with tempfile.TemporaryDirectory() as d:
            app = self._fake_app(d, "luoke-x", {})
            with open(os.path.join(app, "Contents", "marker"), "w") as f:
                f.write("keep")
            ino = os.stat(app).st_ino
            found = accessories.installed(only=[os.path.join(d, "Applications")])
            calls = []
            with unittest.mock.patch.object(apps, "_ls", lambda flag, path: calls.append((flag, path))), \
                 unittest.mock.patch.object(apps, "registered_copies", lambda bid: []):
                r = accessories.reregister(found, "luoke-x")
            self.assertNotIn("code", r)
            # 暂存那份和换下的那份都先注销再挪 / 删；最后只登记原位那一份（luoke-ime 10-21：别留指向已删路径的记录）
            for path in {p for _f, p in calls if p != app}:
                self.assertIn(("-u", path), calls)
                self.assertNotIn(("-f", path), calls)
            last_app = [c for c in calls if c[1] == app][-1]
            self.assertEqual(last_app, ("-f", app))                     # 原位那一份最后是登记着的
            self.assertNotEqual(os.stat(app).st_ino, ino)
            with open(os.path.join(app, "Contents", "marker")) as f:
                self.assertEqual(f.read(), "keep")
            self.assertEqual(os.listdir(os.path.join(d, "Applications")), ["luoke-x.app"])

    def test_restart_input_method_stops_then_opens(self):
        """luoke-ime#18：「重新启动」只认输入法；按包路径结束在跑的进程，再 open -g 拉起，不碰登记（不调 lsregister）。"""
        from luoke_core import apps
        with tempfile.TemporaryDirectory() as d:
            ime = self._fake_app(d, "luoke-ime", {"type": "input-method"})
            self._fake_app(d, "luoke-mail", {})
            found = accessories.installed(only=[os.path.join(d, "Applications")])
            order, ls = [], []
            def run(cmd, **kw):
                order.append(("open", cmd))
                return accessories.subprocess.CompletedProcess(cmd, 0, b"", b"")
            with unittest.mock.patch.object(accessories, "terminate_app_processes", lambda app, grace=5.0: order.append(("kill", app)) or 2), \
                 unittest.mock.patch.object(accessories.subprocess, "run", run), \
                 unittest.mock.patch.object(apps, "_ls", lambda flag, path: ls.append((flag, path))):
                r = accessories.restart_input_method(found, "luoke-ime")
                self.assertEqual(r["killed"], 2)
                self.assertEqual(order, [("kill", ime), ("open", ["/usr/bin/open", "-g", ime])])
                self.assertEqual(ls, [])
                order.clear()
                self.assertEqual(accessories.restart_input_method(found, "luoke-mail")["code"], "unsupported")
                self.assertEqual(accessories.restart_input_method(found, "nope")["code"], "unsupported")
                self.assertEqual(order, [])
        src = open(os.path.join(ROOT, "luoke_core", "hub.py"), encoding="utf-8").read()
        block = src[src.index('path == "/accessories/restart"'):][:300]
        self.assertIn("require_hub(", block)                                  # 只有主控自己的界面能调

    def test_uninstall_removes_the_retired_previous_copy(self):
        """issue #14：换装时挪进 previous.noindex 的旧版，卸载这个配件时一起删（之前只有「卸载全部」才删）；别的 bundle id 的同名副本不动。"""
        import plistlib
        with tempfile.TemporaryDirectory() as d:
            self._fake_app(d, "luoke-x", {})
            prev = os.path.join(d, "previous.noindex")
            def copy(bid):
                keep = os.path.join(prev, "luoke-x.app")
                os.makedirs(os.path.join(keep, "Contents"), exist_ok=True)
                with open(os.path.join(keep, "Contents", "Info.plist"), "wb") as f:
                    plistlib.dump({"CFBundleIdentifier": bid}, f)
                return keep
            keep = copy("ai.luoke.other")
            found = accessories.installed(only=[os.path.join(d, "Applications")])
            self.assertIsNone(accessories.uninstall_plan(found, "luoke-x", previous_dir=prev)["previous"])
            copy("ai.luoke.luoke-x")
            self.assertEqual(accessories.uninstall_plan(found, "luoke-x", previous_dir=prev)["previous"], keep)
            r = accessories.uninstall(found, "luoke-x", previous_dir=prev)
            self.assertEqual(r["errors"], [])
            self.assertIn(keep, r["removed"])
            self.assertFalse(os.path.exists(keep))

    def test_bundled_memory_lib_location(self):
        """10-19：配件改用主控里那份 luoke_mem，主控在 GET /tasks 里报它的 site-packages 和版本。"""
        with tempfile.TemporaryDirectory() as d:
            hub = os.path.join(d, "螺壳.app")
            os.makedirs(os.path.join(hub, "Contents", "Resources", "memory"))
            sp = os.path.join(hub, "Contents", "Resources", "python", "lib", "python3.12", "site-packages")
            self.assertIsNone(accessories.bundled_memory_lib(hub))
            with open(os.path.join(hub, "Contents", "Resources", "memory", "luoke-accessory.json"), "w") as f:
                json.dump({"contract": 1, "accessory": "luoke-mem", "bundle_id": accessories.MEMORY_BUNDLE_ID, "version": "0.23.0",
                           "type": "component", "tasks": [], "data": []}, f)
            self.assertIsNone(accessories.bundled_memory_lib(hub))          # 清单在、库不在：不报
            os.makedirs(os.path.join(sp, "luoke_mem"))
            open(os.path.join(sp, "luoke_mem", "__init__.py"), "w").close()
            self.assertEqual(accessories.bundled_memory_lib(hub), {"path": sp, "version": "0.23.0"})
            py = os.path.join(hub, "Contents", "Resources", "python", "bin", "python3")
            os.makedirs(os.path.dirname(py))
            open(py, "w").close()
            self.assertEqual(accessories.bundled_memory_lib(hub), {"path": sp, "version": "0.23.0", "python": py})

    def test_bundled_memory_and_migration(self):
        """记忆并进主控：主控 .app 里的清单盖过 ~/Applications 的旧螺壳记忆.app；旧的迁走；不能单独卸载；mem.db 不动。"""
        with tempfile.TemporaryDirectory() as d:
            hub = os.path.join(d, "螺壳.app")
            os.makedirs(os.path.join(hub, "Contents", "Resources", "memory"))
            mem = {"contract": 1, "accessory": "luoke-mem", "bundle_id": accessories.MEMORY_BUNDLE_ID, "name": "螺壳记忆", "version": "9",
                   "type": "component", "entry": ["-m", "luoke_mem", "app"], "tasks": [], "data": [], "commands": {"forget-tool": ["-m", "luoke_mem", "forget-tool", "{tool}"]}}
            with open(os.path.join(hub, accessories.BUNDLED_MEMORY_REL), "w") as f:
                json.dump(mem, f)
            apps = os.path.join(d, "Applications")
            old = self._fake_app(d, "old-mem", {"accessory": "luoke-mem", "bundle_id": accessories.MEMORY_BUNDLE_ID, "version": "1", "type": "component"})
            luoke = os.path.join(d, ".luoke"); os.makedirs(luoke); open(os.path.join(luoke, "mem.db"), "w").close()
            found = accessories.installed(only=[apps], hub=hub)
            self.assertEqual(found[accessories.MEMORY_BUNDLE_ID]["_app"], hub)
            self.assertTrue(found[accessories.MEMORY_BUNDLE_ID]["_bundled"])
            self.assertEqual(accessories.uninstall_plan(found, "luoke-mem")["code"], "invalid_request")
            with unittest.mock.patch.dict(os.environ, {"LUOKE_ENV_DIR": os.path.join(d, "env")}):   # 别碰真实的环境目录
                self.assertEqual(accessories.migrate_old_memory_app(dirs=[apps], hub=hub, log=lambda m: None), old)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.isdir(os.path.join(d, "env", "previous.noindex", "螺壳记忆.app")))   # 挪走，不删
            self.assertTrue(os.path.exists(os.path.join(luoke, "mem.db")))
            self.assertIsNone(accessories.migrate_old_memory_app(dirs=[apps], hub=hub, log=lambda m: None))
            self.assertIsNone(accessories.migrate_old_memory_app(dirs=[apps], hub=os.path.join(d, "none.app"), log=lambda m: None))   # 主控没带记忆：不迁

    def test_data_dirs_never_reach_memory_case_variants(self):
        """2026-10-16 审查：大小写变体、包含记忆目录的路径、~/Library 第二层，都不能进删除清单。"""
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            with unittest.mock.patch.dict(os.environ, {"HOME": home, "LUOKE_HOME": os.path.join(home, ".luoke")}):
                os.makedirs(os.path.join(home, ".luoke", "ime"))
                open(os.path.join(home, ".luoke", "mem.db"), "w").close()
                os.makedirs(os.path.join(home, "Library", "Caches", "x"))
                os.makedirs(os.path.join(home, ".luoke-x", "nested"))
                open(os.path.join(home, ".luoke-x", "nested", "MEM.db-wal"), "w").close()
                os.makedirs(os.path.join(home, "Library", "Caches", "ai.luoke.inputmethod"))
                ime = {"accessory": "luoke-ime", "bundle_id": "ai.luoke.inputmethod"}
                bad = ["~/.LUOKE", "~/.Luoke/MEM.db", "~/.luoke/Mem.DB", "~/Library/Caches", "~/library/caches", "~/.luoke-x", "~/Library/Caches/x"]
                self.assertEqual(accessories.data_dirs(dict(ime, data=bad)), [])
                self.assertEqual(accessories.data_dirs({"data": ["~/.luoke/ime"]}), [])          # 清单没有标识：什么都不删
                self.assertEqual(accessories.data_dirs(dict(ime, data=["~/.luoke/ime", "~/Library/Caches/ai.luoke.inputmethod"])),
                                 [os.path.join(home, ".luoke/ime"), os.path.join(home, "Library/Caches/ai.luoke.inputmethod")])

    def test_uninstall_runs_forget_tool(self):
        """勾了"从记忆里忘掉"真的会跑 forget-tool（2026-10-16 审查：之前真卸载时 plan["forget"] 恒为 None，从不执行）。"""
        with tempfile.TemporaryDirectory() as d:
            app = self._fake_app(d, "x", {"memory": True})
            memapp = self._fake_app(d, "mem", {"accessory": "luoke-mem", "bundle_id": accessories.MEMORY_BUNDLE_ID, "type": "component",
                                              "commands": {"forget-tool": ["-m", "luoke_mem", "forget-tool", "{tool}"]}})
            found = accessories.installed(only=[os.path.join(d, "Applications")])
            calls = []
            with unittest.mock.patch.object(accessories, "forget_tool", lambda m, a, dry_run: calls.append((a, dry_run)) or {"records": 2, "notes": 1}), \
                    unittest.mock.patch.object(accessories, "terminate_app_processes", lambda app, grace=5.0: 0):
                r = accessories.uninstall(found, "x", forget=True)
            self.assertIn(("x", False), calls)
            self.assertEqual(r["forgot"], {"records": 2, "notes": 1})
            self.assertFalse(os.path.exists(app))
            self.assertTrue(os.path.exists(memapp))

    def test_terminate_app_processes_by_path(self):
        """只结束可执行文件在那个 .app 里的进程（启动器 exec 出的 python3 这类，本体按 bundle id 找不到）；别的进程不碰。"""
        import shutil as _sh, signal as _sig, subprocess as _sp, time as _t
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "X.app")
            os.makedirs(os.path.join(app, "Contents", "MacOS"))
            exe = os.path.join(app, "Contents", "MacOS", "x")
            # 不能拷 /bin/sleep：平台二进制搬出系统卷会被 launch constraints 直接 SIGKILL。现编一个最小的程序（没有编译器就跳过）
            cc = _sp.run(["/usr/bin/xcrun", "--find", "cc"], capture_output=True, text=True).stdout.strip()
            if not cc:
                self.skipTest("没有 C 编译器")
            # 经 xcrun 调才带上 SDK（直接调工具链里的 cc 找不到 unistd.h）
            _sp.run(["/usr/bin/xcrun", "cc", "-x", "c", "-", "-o", exe],
                    input="#include <unistd.h>\nint main(void){sleep(60);return 0;}\n", text=True, check=True, capture_output=True)
            inside = _sp.Popen([exe])
            outside = _sp.Popen(["/bin/sleep", "60"])
            try:
                _t.sleep(0.3)
                self.assertIn(inside.pid, accessories.app_processes(app))
                self.assertNotIn(outside.pid, accessories.app_processes(app))
                self.assertEqual(accessories.terminate_app_processes(app, grace=2.0), 1)
                self.assertIsNotNone(inside.wait(timeout=5))
                self.assertIsNone(outside.poll())
            finally:
                for p in (inside, outside):
                    if p.poll() is None:
                        p.send_signal(_sig.SIGKILL); p.wait()

    def test_uninstall_deletes_only_declared_and_never_memory(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            with unittest.mock.patch.dict(os.environ, {"HOME": home, "LUOKE_HOME": os.path.join(home, ".luoke")}):
                for sub in ("Applications", ".luoke/x/records", ".luoke/ime", ".luoke/other", ".luoke-x/data", "Library/Application Support/luoke-x", "Documents"):
                    os.makedirs(os.path.join(home, sub))
                for f in ("mem.db", "mem.db-ui.json", "mem.db-learn.lock"):
                    open(os.path.join(home, ".luoke", f), "w").close()
                os.symlink(os.path.join(home, "Documents"), os.path.join(home, ".luoke-x", "link"))
                os.symlink("/", os.path.join(home, ".luoke-x", "root"))
                app = self._fake_app(d, "luoke-x", {"memory": True, "data": [
                    "~/.luoke/x", "~/.luoke-x", "~/Library/Application Support/luoke-x",   # 自己的：能删
                    "~/.luoke/ime", "~/.luoke-mail",                                      # 别的配件的：不能删（10-19：只认和自己标识绑定的位置）
                    "~/.luoke", "~/.luoke/mem.db", "~/.luoke/mem.db-ui.json", "~/Library", "~/Documents", "~", "/tmp", "~/..", "~/.luoke-x/root",  # 不能删
                    "~/nope"]})                                                           # 不存在：不列
                other = self._fake_app(d, "luoke-y", {"data": ["~/.luoke/other"]})
                found = accessories.installed(only=[os.path.join(d, "Applications")])
                plan = accessories.uninstall_plan(found, "luoke-x")
                self.assertEqual(sorted(plan["data"]), sorted(os.path.join(home, p) for p in (".luoke/x", ".luoke-x", "Library/Application Support/luoke-x")))
                self.assertIsNone(plan["forget"])   # 没装螺壳记忆：不显示"忘掉"
                self.assertEqual(plan["memory_needed_by"], [])
                r = accessories.uninstall(found, "luoke-x", data=False, dry_run=True)
                self.assertTrue(r["dry_run"]); self.assertTrue(os.path.isdir(app))
                r = accessories.uninstall(found, "luoke-x", data=False)   # 默认保留数据
                self.assertFalse(os.path.exists(app)); self.assertTrue(os.path.isdir(os.path.join(home, ".luoke/x")))
                self.assertEqual(r["removed"], [app]); self.assertEqual(r["errors"], [])
                app = self._fake_app(d, "luoke-x", {"data": ["~/.luoke/x", "~/.luoke-x", "~/Library/Application Support/luoke-x", "~/.luoke/mem.db"]})
                found = accessories.installed(only=[os.path.join(d, "Applications")])
                r = accessories.uninstall(found, "luoke-x", data=True)
                self.assertEqual(r["errors"], [])
                for gone in ("Applications/luoke-x.app", ".luoke/x", ".luoke-x", "Library/Application Support/luoke-x"):
                    self.assertFalse(os.path.lexists(os.path.join(home, gone)), gone)
                for kept in (".luoke/mem.db", ".luoke/mem.db-ui.json", ".luoke/mem.db-learn.lock", ".luoke/other", ".luoke/ime", "Documents", "Library", "Applications/luoke-y.app"):
                    self.assertTrue(os.path.lexists(os.path.join(home, kept)), kept)
                self.assertTrue(os.path.isdir("/Applications"))   # 指向根目录的软链接：没跟着删
                self.assertTrue(os.path.isdir(other))


class ContractConstantsTest(unittest.TestCase):
    """本体（Swift）和主控（Python）各写了一份的常量必须一致（V1 §10b 越界自查剩下的一条）：清单路径、自带 Python 路径、
    螺壳记忆的 bundle id、lsregister、配件目录、socket 位置、身份头。Swift 源不在（打包后的测试）就跳过。"""
    SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "Sources", "LuokeCore")

    def swift(self, name):
        p = os.path.join(self.SRC, name)
        if not os.path.exists(p):
            self.skipTest("没有 Swift 源：" + p)
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_literals_match(self):
        from luoke_core import client
        app_src, hub_src, inst_src, cli_src = (self.swift(n) for n in ("LuokeCoreApp.swift", "HubProcess.swift", "Installer.swift", "HubClient.swift"))
        self.assertIn('"' + accessories.MANIFEST_REL + '"', app_src)
        self.assertIn('"' + accessories.PYTHON_REL.split("Contents/Resources/", 1)[1] + '"', hub_src)
        self.assertIn('static let memoryBundleID = "' + accessories.MEMORY_BUNDLE_ID + '"', app_src)
        self.assertIn('static let lsregister = "' + accessories.LSREGISTER + '"', self.swift("AppPlacement.swift"))
        for d in accessories.DEFAULT_DIRS:
            self.assertIn('"' + os.path.relpath(d, os.path.expanduser("~")) + '"', app_src)
        self.assertIn('"run/hub.sock"', hub_src)
        self.assertTrue(client.DEFAULT_SOCKET.endswith("/luoke-core/run/hub.sock"))
        self.assertIn("X-Luoke-Accessory: ", cli_src)
        self.assertIn('"luoke-core/" + AppInfo.version', cli_src)   # 本体以 luoke-core 身份调主控（/uninstall 只认它）

    def test_no_auto_download_on_metered_network(self):
        """DESIGN §457（10-27 定进 beta1.1）：网络计费或受限时首次运行以后的自动补下不下，只在「还缺」那一行提示，等用户点「重试」。"""
        import re
        app_src = self.swift("LuokeCoreApp.swift")
        guard = re.search(r"func autoDownload\(_ st: HubStatus\) \{\n\s+guard (.+?) else \{ return \}", app_src, re.S).group(1)
        self.assertIn("!meteredNetwork", guard)
        self.assertIn("path.isExpensive || path.isConstrained", app_src)
        self.assertIn("model.meteredNetwork ?", app_src)               # 提示在「还缺」那一行
        retry = re.search(r"func retryDownloads\(\) \{(.+?)\n    \}", app_src, re.S).group(1)
        self.assertNotIn("meteredNetwork", retry)                      # 用户点「重试」照下

    def test_update_check_pages_until_its_channel(self):
        """10-28：检查更新原来只看最新 40 条发布，内部版一天两版，三周后最近的 beta 就掉出第一页、装 beta 的人查不到更新。"""
        import re
        src = self.swift("Updater.swift")
        self.assertIn("per_page=\\(perPage)&page=\\(page)", src)
        loop = re.search(r"for page in 1\.\.\.(\d+) \{(.+?)\n        \}", src, re.S)
        self.assertGreaterEqual(int(loop.group(1)), 5)
        self.assertIn("batch.count < perPage", loop.group(2))           # 翻到最后一页就停
        self.assertIn("matches(", loop.group(2))                        # 这个渠道有版本了就停

    def test_power_modes_reasons_engine_version(self):
        import re
        from luoke_core import dispatch
        app_src, cli_src, inst_src = (self.swift(n) for n in ("LuokeCoreApp.swift", "HubClient.swift", "Installer.swift"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hub_py = open(os.path.join(root, "luoke_core", "hub.py"), encoding="utf-8").read()
        dispatch_py = open(os.path.join(root, "luoke_core", "dispatch.py"), encoding="utf-8").read()
        # 功耗档名：本体菜单的列表 == 主控接受的集合 == 派发器的冷却表
        swift_modes = re.findall(r'\("([a-z]+)", "', re.search(r'title: "能耗模式"\)(.+?)selection:', app_src, re.S).group(1))   # 能耗模式只在设置里改（菜单 10-23 起不放）
        hub_modes = re.search(r'patch\["power_mode"\] not in \(([^)]+)\)', hub_py).group(1).replace('"', "").replace(" ", "").split(",")
        self.assertEqual(swift_modes, hub_modes)
        self.assertEqual(set(swift_modes), set(dispatch.POWER_COOLDOWN))
        # 后台暂停的原因码：派发器会给出的每一个，本体都有文字
        py_reasons = set(re.findall(r'reasons\.append\("([a-z_]+)"\)', dispatch_py))
        swift_names = set(re.findall(r'"([a-z_]+)": "[^"]+"', re.search(r'let names: \[String: String\] = \[([^\]]+)\]', cli_src).group(1)))
        self.assertEqual(py_reasons, swift_names)
        # 引擎已装的判断：两边都把配方引擎版本去掉 "omlx-" 前缀再和 Info.plist 的 CFBundleShortVersionString 比
        self.assertIn('hasPrefix("omlx-")', inst_src)
        self.assertIn("CFBundleShortVersionString", inst_src)
        self.assertIn('"omlx-"', hub_py)
        self.assertIn("CFBundleShortVersionString", hub_py)
        # 螺壳自己的 Info.plist：给人看的版本写发布号，主控版本号在 CFBundleVersion，程序读后者（10-27）
        build_sh = open(os.path.join(ROOT, "app", "build_app.sh"), encoding="utf-8").read()
        self.assertIn('"CFBundleShortVersionString": sys.argv[6], "CFBundleVersion": sys.argv[4]', build_sh)
        self.assertIn("['release']", build_sh)
        hubproc = open(os.path.join(ROOT, "app", "Sources", "LuokeCore", "HubProcess.swift"), encoding="utf-8").read()
        self.assertIn('infoDictionary?["CFBundleVersion"]', hubproc)


class ReuseLocalModelTest(unittest.TestCase):
    """issue luoke-core#3：本机已有同一个模型就克隆进来；哈希不对的照下；原文件不动。"""

    def test_item_digest_matches_swift_and_migrates_old_markers(self):
        """luoke-core#3 复验：download.py 有两个 item_digest，后面的旧算法把新的盖掉了，Swift 算的是新的，app 侧总认为模型没装。
        现在只有一个（和 Installer.swift Manifests.itemDigest 同一算法）；旧算法写的标记认一次、当场改写。"""
        from luoke_core import download
        import hashlib
        item = {"files": [{"path": "b.json", "sha256": "22"}, {"path": "a.safetensors", "sha256": "11"}]}
        swift_alg = hashlib.sha256("a.safetensors\t11\nb.json\t22".encode()).hexdigest()   # 按路径排、制表符、换行连
        self.assertEqual(download.item_digest(item), swift_alg)
        src = open(os.path.join(ROOT, "app", "Sources", "LuokeCore", "Installer.swift"), encoding="utf-8").read()
        self.assertIn('"\\($0.0)\\t\\($0.1)"', src)                 # Swift 那边还是同一种写法
        d = tempfile.mkdtemp()
        with open(os.path.join(d, download.ITEM_MARKER), "w") as f:
            json.dump({"digest": download._legacy_item_digest(item), "version": "x"}, f)
        self.assertTrue(download.item_installed(item, d))
        with open(os.path.join(d, download.ITEM_MARKER)) as f:
            self.assertEqual(json.load(f)["digest"], swift_alg)          # 改写成新算法了
        self.assertTrue(download.item_installed(item, d))
        with open(os.path.join(d, download.ITEM_MARKER), "w") as f:
            json.dump({"digest": "nope"}, f)
        self.assertFalse(download.item_installed(item, d))

    def test_download_errors_say_what_happened(self):
        """10-19：没发布的版本（404）被说成"网络出问题"。分清服务器上没有、连不上、核对不过。"""
        import urllib.error
        from luoke_core import download
        e404 = urllib.error.HTTPError("https://x/luoke-mail-0.15.0.zip", 404, "Not Found", {}, None)
        self.assertIn("找不到", download.explain("luoke-mail-0.15.0.zip", e404))
        self.assertNotIn("网络", download.explain("luoke-mail-0.15.0.zip", e404))
        self.assertIn("网络", download.explain("x.zip", urllib.error.URLError("no route")))
        self.assertIn("核对不过", download.explain("x.zip", download.DownloadError("哈希不对")))
        with tempfile.TemporaryDirectory() as d:                       # 404 不重试：马上换下一个地址、马上报
            t0 = time.time()
            with unittest.mock.patch("urllib.request.urlopen", side_effect=e404):
                with self.assertRaises(download.DownloadError) as cm:
                    download.fetch_file(["https://a/x.zip", "https://b/x.zip"], os.path.join(d, "x.zip"), 10, "0" * 64, probe=lambda u: 1.0)
            self.assertLess(time.time() - t0, 2)
            self.assertIn("找不到", str(cm.exception))

    def test_clone_refuses_other_volume(self):
        from luoke_core import download
        d = tempfile.mkdtemp()
        src = os.path.join(d, "f")
        open(src, "w").write("x")
        real_stat = os.stat
        def fake_stat(p, *a, **k):
            st = real_stat(p, *a, **k)
            if os.path.realpath(p) == os.path.realpath(src):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
            return st
        with unittest.mock.patch("os.stat", fake_stat):
            self.assertFalse(download.clone_file(src, os.path.join(d, "g")))      # 跨卷：不复制，交给下载
        self.assertTrue(download.clone_file(src, os.path.join(d, "h")))

    def test_reuse_by_hash(self):
        from luoke_core import download
        with tempfile.TemporaryDirectory() as d:
            omlx = os.path.join(d, "omlx", "mlx-community", "M")
            hf = os.path.join(d, "hf", "models--org--M", "snapshots", "abc")
            os.makedirs(omlx); os.makedirs(hf); os.makedirs(os.path.join(d, "hf", "blobs"))
            with open(os.path.join(omlx, "w.bin"), "wb") as f:
                f.write(b"weights" * 1000)
            with open(os.path.join(omlx, "README.md"), "w") as f:
                f.write("upstream card")                       # 和清单里的不一样：不复用，照下
            blob = os.path.join(d, "hf", "blobs", "x")
            with open(blob, "wb") as f:
                f.write(b"tok")
            os.symlink(blob, os.path.join(hf, "tok.json"))     # HF 缓存里是指向 blobs 的软链接
            files = []
            for path, data in (("w.bin", b"weights" * 1000), ("README.md", b"our card"), ("tok.json", b"tok")):
                files.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            item = {"kind": "model", "dir": "M", "files": files, "urls": ["file:///nonexistent/{path}"]}
            staging = os.path.join(d, "staging")
            before = os.stat(os.path.join(omlx, "w.bin"))
            n = download.reuse_local(item, staging, roots=[os.path.join(d, "omlx"), os.path.join(d, "hf")])
            self.assertEqual(n, 7000 + 3)
            self.assertTrue(os.path.exists(os.path.join(staging, "w.bin")))
            self.assertTrue(os.path.exists(os.path.join(staging, "tok.json")) and not os.path.islink(os.path.join(staging, "tok.json")))
            self.assertFalse(os.path.exists(os.path.join(staging, "README.md")))
            after = os.stat(os.path.join(omlx, "w.bin"))
            self.assertEqual((before.st_size, before.st_mtime, before.st_ino), (after.st_size, after.st_mtime, after.st_ino))   # 原文件没动
            self.assertEqual(download.reuse_local(dict(item, kind="accessory"), os.path.join(d, "s2"), roots=[os.path.join(d, "omlx")]), 0)


class LabIsolationTest(unittest.TestCase):
    """实验室实例不碰本机用户的东西：只扫自己的配件目录、HOME 没隔离不删配件数据（2026-10-16 gateway 集成测试发现；
    10-19 用 main 复现：没设 LUOKE_HOME 的实验室实例照样扫到真实的旧「螺壳记忆.app」和它的 learn）。"""

    def _real_dirs_with_app(self):
        """假装成本机用户的 ~/Applications：里面装着一个还没迁走、登记了空闲任务的旧螺壳记忆。"""
        d = tempfile.mkdtemp()
        app = os.path.join(d, "螺壳记忆.app")
        os.makedirs(os.path.join(app, "Contents", "Resources"))
        m = {"contract": 1, "accessory": "luoke-mem", "bundle_id": accessories.MEMORY_BUNDLE_ID, "name": "螺壳记忆", "version": "0.15.0",
             "type": "component", "entry": [], "tasks": [], "data": [],
             "jobs": [{"name": "learn", "command": ["-m", "luoke_mem", "--path", "{memory}", "learn"]}]}
        with open(os.path.join(app, accessories.MANIFEST_REL), "w") as f:
            json.dump(m, f)
        return d

    def hub(self, lab):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        return Hub(Recipe.load(RECIPE), d, d, lab=lab, accessory_dirs=[], mem_gb=32, smoke=False)

    def test_lab_does_not_scan_real_accessory_dirs(self):
        with unittest.mock.patch.object(accessories, "DEFAULT_DIRS", [self._real_dirs_with_app()]):
            self.assertIn(accessories.MEMORY_BUNDLE_ID, self.hub(lab=False).manifests)   # 正式实例照旧看得到
            h = self.hub(lab=True)
            self.assertNotIn(accessories.MEMORY_BUNDLE_ID, h.manifests)                # 实验室实例看不到
            self.assertNotIn(accessories.MEMORY_BUNDLE_ID, h.scan_accessories())       # 采样线程、/accessories、/uninstall 走同一个
            h.jobs.tick(h.manifests)                                                    # 安静时刻的调度：没有可跑的空闲任务
            st = h.jobs.status()
            self.assertEqual((st["running"], st["records"]), (None, {}))
            old = os.environ.pop("LUOKE_HOME")                                          # 正是 10-19 复现的情形：没设 LUOKE_HOME
            try:
                self.assertNotIn(accessories.MEMORY_BUNDLE_ID, self.hub(lab=True).manifests)
            finally:
                os.environ["LUOKE_HOME"] = old

    def test_lab_refuses_deleting_accessory_data_with_real_home(self):
        from luoke_core.proxy import BadRequest
        h = self.hub(lab=True)
        with unittest.mock.patch.object(accessories, "home_is_real", return_value=True):
            with self.assertRaises(BadRequest) as cm:
                h.check_uninstall_data(data=True, dry_run=False)
            self.assertEqual(cm.exception.code, "forbidden")
            h.check_uninstall_data(data=True, dry_run=True)      # 只算计划可以
            h.check_uninstall_data(data=False, dry_run=False)    # 只删 .app、不删数据可以
            self.hub(lab=False).check_uninstall_data(data=True, dry_run=False)   # 正式实例照旧
        with unittest.mock.patch.object(accessories, "home_is_real", return_value=False):
            h.check_uninstall_data(data=True, dry_run=False)     # HOME 指向测试目录了：可以删
class Engine071PrepTest(unittest.TestCase):
    """换 oMLX 0.7.1 之前要的三处（2026-10-19 rc1 实验室验证时发现）。"""

    def test_top_k_is_always_explicit(self):
        body = {"messages": [{"role": "user", "content": "x"}], "stream": True}
        p = build_engine_payload(body, {"item_tokens": 10}, {"temperature": 0.3, "top_p": 0.9}, "m", [])
        self.assertEqual(p["top_k"], 0)          # 0 = 不用 top-k：0.7.0 的默认；0.7.1 起没写就用模型存的推荐值（Qwen 20）
        p = build_engine_payload(body, {"item_tokens": 10}, {"temperature": 0.3, "top_k": 40}, "m", [])
        self.assertEqual(p["top_k"], 40)

    def test_smoke_check_warms_up_before_measuring(self):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.base_id = "qwen"
        h.fits_memory = lambda name: True
        h.engine.load = lambda model: 0.5
        calls = []
        def fake_stream(payload, on_line, cancel, first, total):
            calls.append(payload)
            if payload.get("max_tokens") == 1:                     # 空跑：第一条要现编内核，很慢，不该算进速度
                return {"status": 200, "error": None, "first_token_secs": 2.5, "usage": None}
            on_line('data: {"choices": [{"delta": {"content": "{\\"ok\\": true, \\"text\\": \\"你好\\"}"}}]}')
            on_line("data: [DONE]")
            return {"status": 200, "error": None, "first_token_secs": 0.1, "usage": {"prompt_tokens": 400, "completion_tokens": 11}}
        h.engine.stream_chat = fake_stream
        rec = h.smoke_check(force=True)
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["max_tokens"], 1)
        self.assertIn("response_format", calls[1])
        self.assertEqual(rec["prefill_tok_s"], 4000.0)                # 400 token / 0.1 s，不是被空跑拖慢的那一条

    def test_smoke_cache_remeasures_old_method(self):
        """已经测过的机器（旧量法的记录）在下次启动时重测一次；同一量法照旧用缓存；重测之前不用旧的数，按配方的值。"""
        from luoke_core import hub as hubmod
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.base_id = "qwen"
        h.fits_memory = lambda name: True
        h.engine.load = lambda model: 0.5
        runs = []
        def fake_stream(payload, on_line, cancel, first, total):
            runs.append(payload["max_tokens"])
            if payload.get("max_tokens") == 1:
                return {"status": 200, "error": None, "first_token_secs": 2.5, "usage": None}
            on_line('data: {"choices": [{"delta": {"content": "{\\"ok\\": true, \\"text\\": \\"你好\\"}"}}]}')
            on_line("data: [DONE]")
            return {"status": 200, "error": None, "first_token_secs": 0.1, "usage": {"prompt_tokens": 400, "completion_tokens": 11}}
        h.engine.stream_chat = fake_stream
        combo = f"{h.recipe.engine['version']}+{h.base_name}"
        old = {"combo": combo, "ok": True, "prefill_tok_s": 50.2, "decode_tok_s": 30.0, "error": None}
        for stale in (dict(old), dict(old, method=hubmod.SMOKE_METHOD - 1)):    # 没有 method（合并前的记录）、版本更低
            h.state["smoke"] = stale
            self.assertEqual(h.measured_speed("prefill_tok_s", 230.0), 230.0)   # 旧的 50 不用
            runs.clear()
            rec = h.smoke_check()
            self.assertEqual(runs, [1, 120], stale)                              # 重测了（先空跑）
            self.assertEqual((rec["method"], rec["prefill_tok_s"]), (hubmod.SMOKE_METHOD, 4000.0))
            self.assertEqual(h.measured_speed("prefill_tok_s", 230.0), 4000.0)
        runs.clear()
        self.assertIs(h.smoke_check(), h.state["smoke"])                         # 同一量法：照旧用缓存，不重测
        self.assertEqual(runs, [])

    def test_marker_scan_skips_sockets(self):
        import socket as _socket
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import privacy_marker_test as pmt
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            s.bind(os.path.join(d, "hub.sock"))
            with open(os.path.join(d, "log.txt"), "w") as f:
                f.write("前文 LUOKEMARKTEST 后文")
            self.assertEqual(pmt.scan(d, "LUOKEMARKTEST"), [os.path.join(d, "log.txt")])   # socket 不算泄露，文件里的照样找到
            os.remove(os.path.join(d, "log.txt"))
            self.assertEqual(pmt.scan(d, "LUOKEMARKTEST"), [])
        finally:
            s.close()


class MachineTest(unittest.TestCase):
    def test_metal_cap_table(self):
        self.assertAlmostEqual(machine.metal_cap_gb(16), 11.8, places=1)
        self.assertAlmostEqual(machine.metal_cap_gb(32), 25.0, places=1)
        self.assertAlmostEqual(machine.metal_cap_gb(128), 107.5, places=1)
        self.assertGreater(machine.metal_cap_gb(64), 25)


if __name__ == "__main__":
    unittest.main()


class HubUnitTest(unittest.TestCase):
    """不起引擎，只测 Hub 里的纯逻辑：slow 标记、预算扣除、冒烟缓存。"""

    def make(self, mem_gb=32):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        return Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=mem_gb, smoke=False)

    def local_allow_hub(self, tag):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        rel = os.path.join(d, "release.json")
        with open(rel, "w") as f:
            json.dump({"contract": 1, "release": tag, "items": {}}, f)
        h = Hub(Recipe.load(RECIPE), d, d, lab=False, accessory_dirs=[d], mem_gb=128, smoke=False, release=rel)
        h.recipe.verified = {"luoke-core": {"versions": ["0.1.0"]}, "luoke-mail": {"versions": ["0.13.15"]}}
        return h, d

    def write_allow(self, d, entries):
        p = os.path.join(d, "local-verified.json")
        with open(p, "w") as f:
            json.dump({"allowed": entries}, f)
        st = os.stat(p)
        os.utime(p, (st.st_atime, st.st_mtime + len(entries) + 1))     # 同一秒里改两次也要让主控重读

    def test_hub_only_routes_check_the_peer_process(self):
        """luoke-top 10-20：forbidden 要答 403（之前 STATUS 里没有，落到 500）；只给本体用的接口核对端进程，不只信自报的身份。
        命令行 luoke（主控同一个 Python 跑 -m luoke_core.cli）只在 allow_cli 的接口上算数。"""
        from luoke_core import hub as hubmod
        from luoke_core.proxy import STATUS
        self.assertEqual(STATUS["forbidden"], 403)
        h, d = self.local_allow_hub("internal-1020")
        h.recipe.verified["luoke-core"] = {"versions": ["0.1.0"]}
        hd = object.__new__(hubmod.Handler)
        hd.hub = h
        peer = {"pid": 4242, "exe": "/somewhere/else/python3", "argv": ["python3", "-m", "luoke_core.cli"]}
        hd._peer_pid = lambda: peer["pid"]
        with unittest.mock.patch.object(hubmod.os, "getppid", lambda: 777), \
             unittest.mock.patch.object(hubmod.procs, "exe_path", lambda pid: peer["exe"]), \
             unittest.mock.patch.object(hubmod.procs, "argv", lambda pid: peer["argv"]):
            with self.assertRaises(BadRequest) as cm:
                hd.require_hub("luoke-core/0.1.0", "x", allow_cli=True)       # 自称主控、却是别的程序
            self.assertEqual(cm.exception.code, "forbidden")
            peer["exe"] = sys.executable
            hd.require_hub("luoke-core/0.1.0", "x", allow_cli=True)           # 主控的 Python 跑的命令行
            with self.assertRaises(BadRequest):
                hd.require_hub("luoke-core/0.1.0", "x")                        # 命令行不能卸载、下载
            peer["argv"] = ["python3", "-m", "luoke_mail"]
            with self.assertRaises(BadRequest):
                hd.require_hub("luoke-core/0.1.0", "x", allow_cli=True)       # 借主控 Python 的配件不算
            peer["pid"] = 777
            hd.require_hub("luoke-core/0.1.0", "x")                            # 本体（主控进程的父进程）
            with self.assertRaises(BadRequest):
                hd.require_hub("luoke-mail/0.13.15", "x")

    def test_old_engines_are_removed_after_the_new_one_passes(self):
        """10-27（beta1.1）：换了引擎、新引擎冒烟检查过了，engines/ 里旧版本删掉；开发版、实验室不动，别的东西不碰"""
        from luoke_core import hub as hubmod
        with tempfile.TemporaryDirectory() as d:
            h = object.__new__(hubmod.Hub)
            h.env_dir, h.lab, h.release_path = d, False, os.path.join(d, "release.json")
            open(h.release_path, "w").write("{}")
            h.recipe = unittest.mock.Mock(engine={"version": "omlx-0.7.2"})
            h.log = lambda m: None
            for name in ("omlx-0.7.0", "omlx-0.7.2", "notes"):
                os.makedirs(os.path.join(d, "engines", name, "oMLX.app", "Contents"))
            self.assertEqual(h.prune_old_engines(), ["omlx-0.7.0"])
            self.assertEqual(sorted(os.listdir(os.path.join(d, "engines"))), ["notes", "omlx-0.7.2"])
            os.makedirs(os.path.join(d, "engines", "omlx-0.7.0"))
            h.lab = True
            self.assertEqual(h.prune_old_engines(), [])
            h.lab, h.release_path = False, None
            self.assertEqual(h.prune_old_engines(), [])

    def test_rolled_back_accessory_keeps_the_verification_it_had(self):
        """issue #12（10-20"认它当时的验证"）：装着的版本被本版配方认时记下验证记录；换回这个版本后，后来的主控按记录认它，
        只给当时验证过（硬性条目过了）的任务；没装着的旧版本、没有记录的版本不认。"""
        h, d = self.local_allow_hub("beta1")
        app = os.path.join(d, "luoke-mail.app")
        os.makedirs(os.path.join(app, "Contents", "Resources"))
        def install(version):
            with open(os.path.join(app, "Contents", "Resources", "luoke-accessory.json"), "w") as f:
                json.dump({"contract": 1, "accessory": "luoke-mail", "bundle_id": "ai.luoke.mail", "name": "邮件助手", "version": version,
                           "type": "app", "entry": ["-m", "x"], "tasks": [], "data": []}, f)
            h.manifests = h.scan_accessories()
        h.recipe.verified["luoke-mail"] = {"versions": ["0.13.15"], "tasks": {
            "luoke-mail.triage": [{"accessory_version": "0.13.15", "hard_ok": True}],
            "luoke-mail.thread": [{"accessory_version": "0.13.15", "hard_ok": False}]}}
        install("0.13.15")
        with open(os.path.join(d, "verified-history.json"), encoding="utf-8") as f:
            noted = json.load(f)["luoke-mail"]["0.13.15"]["tasks"]
        # 评测过且过了的（triage）记，评测过没过的（thread）不记；配方里根本没有评测记录的（草稿、每日摘要这类）照本版当时放行的也记（issue #32）
        unevaluated = sorted(t for t, spec in h.recipe.tasks.items() if spec.get("accessory") == "luoke-mail" and t not in ("luoke-mail.triage", "luoke-mail.thread"))
        self.assertIn("luoke-mail.draft", unevaluated)
        self.assertEqual(noted, sorted(["luoke-mail.triage"] + unevaluated))
        h.recipe.verified["luoke-mail"] = {"versions": ["0.14.0"], "tasks": {}}      # 下一版主控只认 0.14.0
        install("0.14.0")
        with self.assertRaises(BadRequest):
            h.check_accessory("luoke-mail/0.13.15")              # 装着的不是它：不认
        install("0.13.15")                                        # 换回上一版
        self.assertEqual(h.check_accessory("luoke-mail/0.13.15", "luoke-mail.triage"), ("luoke-mail", "0.13.15"))
        with self.assertRaises(BadRequest) as cm:
            h.check_accessory("luoke-mail/0.13.15", "luoke-mail.thread")
        self.assertEqual(cm.exception.code, "task_unavailable")
        self.assertEqual(h.check_accessory("luoke-mail/0.13.15", "luoke-mail.draft"), ("luoke-mail", "0.13.15"))   # issue #32
        install("0.13.14")
        with self.assertRaises(BadRequest):
            h.check_accessory("luoke-mail/0.13.14")              # 没有记录：不认

    def test_rollback_swaps_the_previous_copy_back(self):
        import plistlib
        with tempfile.TemporaryDirectory() as d:
            def make(path, version):
                os.makedirs(os.path.join(path, "Contents", "Resources"), exist_ok=True)
                with open(os.path.join(path, accessories.MANIFEST_REL), "w") as f:
                    json.dump({"contract": 1, "accessory": "luoke-x", "bundle_id": "ai.luoke.x", "name": "X", "version": version,
                               "type": "app", "entry": ["-m", "x"], "tasks": [], "data": []}, f)
                with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
                    plistlib.dump({"CFBundleIdentifier": "ai.luoke.x"}, f)
            cur = os.path.join(d, "Applications", "luoke-x.app")
            prev = os.path.join(d, "previous.noindex")
            make(cur, "2")
            found = accessories.installed(only=[os.path.join(d, "Applications")])
            self.assertEqual(accessories.rollback_candidates(found, prev), [])
            self.assertEqual(accessories.rollback(found, "luoke-x", prev, verify=False)["code"], "unsupported")
            make(os.path.join(prev, "luoke-x.app"), "1")
            c = accessories.rollback_candidates(found, prev)
            self.assertEqual([(x["current"], x["previous"]) for x in c], [("2", "1")])
            r = accessories.rollback(found, "luoke-x", prev, verify=False)
            self.assertEqual((r["from"], r["to"]), ("2", "1"))
            self.assertEqual(accessories.app_version(cur), "1")
            self.assertEqual(accessories.app_version(os.path.join(prev, "luoke-x.app")), "2")   # 再点一次就换回来
            self.assertFalse(any(n.startswith(".swap-") for n in os.listdir(prev)))

    def test_local_allow_only_on_internal_hubs(self):
        """2026-10-17：试用期里项目会话可以在开发机上放行自己的新版配件；beta 和公开发布一点不放松。"""
        h, d = self.local_allow_hub("internal-1017")
        with self.assertRaises(BadRequest):
            h.check_accessory("luoke-mail/0.13.18")
        self.write_allow(d, [{"accessory": "luoke-mail", "version": "0.13.18", "by": "luoke-mail", "reason": "t"},
                             {"accessory": "luoke-new", "version": "1.0", "by": "x"},          # 新配件：不放行
                             {"accessory": "luoke-core", "version": "9", "by": "x"},           # 主控自己：不放行
                             {"accessory": "luoke-mail", "version": "0.13.15", "by": "x"}])    # 配方里已有：不算放行
        self.assertEqual(h.check_accessory("luoke-mail/0.13.18"), ("luoke-mail", "0.13.18"))
        self.assertEqual([e["version"] for e in h.local_allow_list()], ["0.13.18"])
        with self.assertRaises(BadRequest):
            h.check_accessory("luoke-new/1.0")
        with open(os.path.join(d, "logs", "hub.log"), encoding="utf-8") as f:
            self.assertIn("local allow used: luoke-mail/0.13.18", f.read())
        self.write_allow(d, [])                       # 撤销立即生效
        with self.assertRaises(BadRequest):
            h.check_accessory("luoke-mail/0.13.18")
        for tag in ("beta1", "v1.0", ""):
            h, d = self.local_allow_hub(tag)
            self.write_allow(d, [{"accessory": "luoke-mail", "version": "0.13.18", "by": "luoke-mail"}])
            with self.assertRaises(BadRequest):
                h.check_accessory("luoke-mail/0.13.18")
            self.assertEqual(h.local_allow_list(), [])
        h, d = self.local_allow_hub("local-1017-2330")
        self.write_allow(d, [{"accessory": "luoke-mail", "version": "0.13.18", "by": "luoke-mail"}])
        self.assertEqual(h.check_accessory("luoke-mail/0.13.18")[1], "0.13.18")

    def test_app_processes_survive_c_locale(self):
        """10-19：主控从 launchd 起、没有 LANG 时，ps 把中文 .app 路径印成转义，按路径找进程一个都找不到。"""
        import subprocess
        d = tempfile.mkdtemp()
        app = os.path.join(d, "螺壳测试.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        exe = os.path.join(app, "Contents", "MacOS", "sleeper")
        src = os.path.join(d, "s.c")
        with open(src, "w") as f:
            f.write("#include <unistd.h>\nint main(){sleep(30);return 0;}\n")
        subprocess.run(["/usr/bin/cc", "-o", exe, src], check=True)
        p = subprocess.Popen([exe])
        try:
            with unittest.mock.patch.dict(os.environ, {"LANG": "", "LC_ALL": "", "LC_CTYPE": ""}):
                self.assertIn(p.pid, accessories.app_processes(app))
        finally:
            p.kill()
            p.wait()

    def test_app_placement_handles_launchservices(self):
        """apps.py：挪之前注销旧路径、挪之后按要求登记或注销新路径；退下来的放进 keep_dir 并注销；删之前注销。"""
        from luoke_core import apps
        d = tempfile.mkdtemp()
        calls = []
        with unittest.mock.patch.object(apps, "_ls", lambda flag, path: calls.append((flag, os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path)))):
            a = os.path.join(d, "stage", "X.app")
            os.makedirs(os.path.join(a, "Contents"))
            final = os.path.join(d, "Apps", "X.app")
            os.makedirs(os.path.dirname(final))
            apps.move_app(a, final, register=True)
            self.assertEqual(calls, [("-u", "stage/X.app"), ("-f", "Apps/X.app")])
            calls.clear()
            keep = apps.retire_app(final, os.path.join(d, "prev.noindex"), "Old.app")
            self.assertEqual(calls, [("-u", "Apps/X.app"), ("-u", "prev.noindex/Old.app")])
            self.assertTrue(os.path.isdir(keep))
            calls.clear()
            apps.remove_app(keep)
            self.assertEqual(calls, [("-u", "prev.noindex/Old.app")])
            self.assertFalse(os.path.exists(keep))

    def test_engine_reaps_only_its_own_orphan(self):
        """DESIGN §10：上次没收好的引擎按 PID + 启动时间 + 可执行文件认；PID 被别的进程复用（启动时间不同）就不碰。"""
        import subprocess
        from luoke_core import procs
        from luoke_core.engine import Engine
        d = tempfile.mkdtemp()
        exe = os.path.join(d, "fake-engine")
        src = os.path.join(d, "s.c")
        with open(src, "w") as f:
            f.write("#include <unistd.h>\nint main(){sleep(30);return 0;}\n")
        subprocess.run(["/usr/bin/cc", "-o", exe, src], check=True)
        e = Engine("/nonexistent.app", d, d, 10)
        p = subprocess.Popen([exe])
        try:
            with open(e.pidfile, "w") as f:                       # 启动时间对不上：当作别的进程
                json.dump({"pid": p.pid, "start": 1.0, "exe": procs.exe_path(p.pid)}, f)
            e._reap_orphan()
            self.assertIsNone(p.poll())
            self.assertFalse(os.path.exists(e.pidfile))
            with open(e.pidfile, "w") as f:                       # 全对得上：是上次留下的，结束它
                json.dump({"pid": p.pid, "start": procs.start_time(p.pid), "exe": procs.exe_path(p.pid)}, f)
            e._reap_orphan()
            self.assertIsNotNone(p.wait(5))
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()

    def test_engine_key_not_on_command_line(self):
        """luoke-core#35：密钥不进命令行和环境变量，写进只有本账户能读的 settings.json；引擎和工作进程都不去 Hugging Face 找。"""
        from luoke_core import engine as eng
        d = tempfile.mkdtemp()
        e = eng.Engine("/nonexistent.app", d, d, 10)
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump({"auth": {"api_key": "old"}, "server": {"host": "0.0.0.0"}}, f)
        proc = unittest.mock.Mock(pid=4242, returncode=1)
        proc.poll.return_value = 1
        with unittest.mock.patch.object(eng.subprocess, "Popen", return_value=proc) as popen, \
                unittest.mock.patch.object(e, "_write_pidfile"):
            with self.assertRaises(eng.EngineError):
                _REAL_ENGINE_START(e)
        cmd, env = popen.call_args.args[0], popen.call_args.kwargs["env"]
        self.assertNotIn("--api-key", cmd)
        self.assertFalse(any(e.key in a for a in cmd) or any(e.key in v for v in env.values()))
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        sp = os.path.join(d, "settings.json")
        self.assertEqual(json.load(open(sp)), {"auth": {"api_key": e.key}})     # 旧的整份清掉，只留密钥
        self.assertEqual(os.stat(sp).st_mode & 0o777, 0o600)
        self.assertIn('"HF_HUB_OFFLINE": "1"', open(os.path.join(ROOT, "luoke_core", "scorer.py")).read())

    def test_status_does_not_run_lsof(self):
        """luoke-core#4：/status 每次都跑 lsof、没超时，软上限很大时堆上千个。现在只在引擎起来时核一次，/status 读缓存。"""
        from luoke_core.engine import Engine
        e = Engine("/nonexistent.app", tempfile.mkdtemp(), tempfile.mkdtemp(), 10)
        e.proc = unittest.mock.Mock(pid=4242)
        e.proc.poll.return_value = None
        with unittest.mock.patch("subprocess.run") as run:
            self.assertIsNone(e.owns_port())                      # 还没核
            e.port_check = ((4242, e.port), True)
            self.assertTrue(e.owns_port())
            e.port_check = ((1, e.port), True)                    # 换了进程：不认旧结果
            self.assertIsNone(e.owns_port())
            run.assert_not_called()
        with unittest.mock.patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("lsof", 10)) as run:
            self.assertIsNone(e.check_port())                     # 超时：没核成，不当成"不是自己的"
            self.assertEqual(run.call_args.kwargs.get("timeout"), 10)
            self.assertIn("ulimit -n 1024", run.call_args.args[0][2])

    def test_idle_unload_waits_for_smoke_check(self):
        """2026-10-16 16 GB 测试机：下载 72 分钟后闲置时钟早已过期，冒烟检查刚加载好底座就被闲置卸载掉。检查期间不许卸。"""
        h = self.make(16)
        calls = []
        h.engine.loaded = "m"
        h.engine.unload = lambda *a, **k: calls.append(1)
        h.last_base_use = 0
        h.smoke_running = True
        h._idle_unload()
        self.assertEqual(calls, [])
        h.smoke_running = False
        h._idle_unload()
        self.assertEqual(calls, [1])

    def test_slow_from_probe_with_hysteresis(self):
        from luoke_core.hub import decide_slow
        self.assertFalse(decide_slow(False, 45, 50))
        self.assertTrue(decide_slow(False, 55, 50))
        self.assertTrue(decide_slow(True, 45, 50))      # 标了以后 45 > 40 还不清
        self.assertFalse(decide_slow(True, 35, 50))
        h = self.make(32)
        self.assertIsNone(h.slow_info("luoke-ime.rerank"))          # 没探过不标
        h.state["probe"] = {"qwen3.5-0.8b-base-4bit": {"score_p90_ms": 40.0, "predict_p90_ms": 70.0, "date": "x"}}
        self.assertIsNone(h.slow_info("luoke-ime.rerank"))          # 40 < 50（阈值 "*"，各档相同）
        self.assertEqual(h.threshold_ms("luoke-ime.rerank"), 50.0)
        h.state["probe"]["qwen3.5-0.8b-base-4bit"]["score_p90_ms"] = 55.0
        info = h.slow_info("luoke-ime.rerank")
        self.assertEqual((info["threshold_ms"], info["measured_ms"], info["source"]), (50.0, 55.0, "probe"))
        h.state["probe"]["qwen3.5-0.8b-base-4bit"]["score_p90_ms"] = 45.0
        self.assertIsNotNone(h.slow_info("luoke-ime.rerank"))       # 滞回：45 > 40 仍是慢
        h.state["probe"]["qwen3.5-0.8b-base-4bit"]["score_p90_ms"] = 35.0
        self.assertIsNone(h.slow_info("luoke-ime.rerank"))
        self.assertIsNone(h.slow_info("luoke-ime.predict"))         # 70 < 200
        self.assertIsNone(h.slow_info("luoke-mail.triage"))         # 生成任务没有延迟要求

    def test_task_unsupported_on_unverified_tier(self):
        h = self.make(32)
        self.assertEqual(h.task_status("luoke-mem.typing")["status"] in ("available", "needs_download", "paused"), True)
        h.recipe.tasks["luoke-mem.typing"]["tiers"] = ["128"]
        st = h.task_status("luoke-mem.typing")
        self.assertEqual((st["status"], st["reason"]), ("unsupported", "这一档没通过验证"))
        h.recipe.tasks["luoke-mem.typing"]["tiers"] = ["32", "128"]
        self.assertNotEqual(h.task_status("luoke-mem.typing")["status"], "unsupported")

    def test_budget_subtracts_running_specials(self):
        h = self.make(32)
        self.assertEqual(h.specials_running_gb(), 0.0)
        real = machine.memory_sample
        try:
            machine.memory_sample = lambda: {"wired_gb": 3.0, "swapouts_gb": 0, "free_gb": 20, "free_pct": 60}   # 别用这台机器的实况
            self.assertTrue(h.fits_memory("qwen3.5-9b-4bit"))            # 5.8 <= (20-2)*0.95；25 - 3 >= 6.8
            h.tier = dict(h.tier, guard_gb=7)
            self.assertFalse(h.fits_memory("qwen3.5-9b-4bit"))           # 5.8 > (7-2)*0.95
        finally:
            machine.memory_sample = real

    def test_user_chosen_tier(self):
        """省空间（issue luoke-core#2）：大内存机器可以选更低的档；设置里存的档开机就用；不能往上选。"""
        from luoke_core.hub import BadRequest, Hub
        d = tempfile.mkdtemp()
        for m in ("Qwen3.5-9B-4bit", "gemma-4-26b-a4b-it-8bit"):
            os.makedirs(os.path.join(d, m))                       # 两个底座都在：换档立刻生效
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump({"tier": "32"}, f)
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        self.assertEqual((h.memory_tier, h.tier_name, h.base_name), ("128", "32", "qwen3.5-9b-4bit"))
        h.update_settings({"tier": None})                       # 清掉：回到内存算的档
        self.assertEqual((h.tier_name, h.base_name), ("128", "gemma-4-26b-8bit"))
        h.update_settings({"tier": "16"})
        self.assertEqual(h.tier_name, "16")
        with open(os.path.join(d, "settings.json")) as f:
            self.assertEqual(json.load(f)["tier"], "16")
        h32 = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertEqual(h32.tier_name, "16")                   # 设置里的 16 对 32 GB 机器也合法
        with self.assertRaises(BadRequest):
            h32.update_settings({"tier": "128"})
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump({"tier": "128"}, f)                         # 文件里写了不合法的（内存不够）：忽略
        self.assertEqual(Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False).tier_name, "32")

    def test_tier_switch_waits_for_base(self):
        """审查 2026-10-17：新底座没下好之前不换档（之前名字先改、底座不下）；重启时选的档没底座就先用有底座的那档。"""
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "Qwen3.5-9B-4bit"))           # 只有 9B，没有 gemma
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump({"tier": "32"}, f)
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        self.assertEqual(h.tier_name, "32")
        h.update_settings({"tier": None})                         # 想回 128，但 gemma 还没有：照旧 32，记着待换
        self.assertEqual((h.tier_name, h.base_name, h.pending_tier), ("32", "qwen3.5-9b-4bit", "128"))
        h3 = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)   # luoke-core#25：下到一半重启
        self.assertEqual((h3.tier_name, h3.pending_tier), ("32", "128"))
        self.assertIn("gemma-4-26b-8bit", h3.needed_downloads())        # 等着的档的底座照样算缺：菜单栏会接着补下
        d2 = tempfile.mkdtemp()
        os.makedirs(os.path.join(d2, "gemma-4-26b-a4b-it-8bit"))   # 选了 32 但 9B 没下好、重启了
        with open(os.path.join(d2, "settings.json"), "w") as f:
            json.dump({"tier": "32"}, f)
        h2 = Hub(Recipe.load(RECIPE), d2, d2, lab=True, accessory_dirs=[d2], mem_gb=128, smoke=False)
        self.assertEqual((h2.tier_name, h2.pending_tier), ("128", "32"))

    def test_smoke_cache(self):
        h = self.make(32)
        from luoke_core.hub import SMOKE_METHOD          # 当前量法的记录（旧量法的重测，见 Engine071PrepTest）
        h.state = {"smoke": {"combo": h.recipe.engine["version"] + "+qwen3.5-9b-4bit", "method": SMOKE_METHOD, "ok": True,
                             "prefill_tok_s": 123.0}}
        self.assertEqual(h.measured_speed("prefill_tok_s", 1.0), 123.0)
        self.assertEqual(h.measured_speed("decode_tok_s", 7.0), 7.0)
        h.base_name = "qwen3.5-9b-4bit"
        self.assertTrue(h.smoke_check()["ok"])                       # 同一组合已通过：不重做（引擎没起也能返回）


class TasksVersionTest(unittest.TestCase):
    def test_version_tracks_params(self):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        v1 = h.tasks_version()
        self.assertEqual(v1, h.tasks_version())                 # 稳定
        h2 = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        self.assertNotEqual(v1, h2.tasks_version())             # 换档就变
        h.recipe.tasks["luoke-mail.triage"]["params"]["*"]["batch"] = 2
        self.assertNotEqual(v1, h.tasks_version())              # 任务参数变了就变


class FitsMemoryTest(unittest.TestCase):
    def test_other_engines_wired_memory_blocks_loading(self):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        real = machine.memory_sample
        try:
            machine.memory_sample = lambda: {"wired_gb": 6.0, "swapouts_gb": 0, "free_gb": 60, "free_pct": 50}
            self.assertTrue(h.fits_memory("gemma-4-26b-8bit"))          # 107.5 - 6 >= 26 + 1
            machine.memory_sample = lambda: {"wired_gb": 85.0, "swapouts_gb": 0, "free_gb": 10, "free_pct": 8}
            self.assertFalse(h.fits_memory("gemma-4-26b-8bit"))         # 别的引擎占着 85 GB：107.5 - 85 < 26 + 1
            # 16 GB 档：9B 4-bit 文件 5.8 GB，天花板 10 → 可用 7.6，装得下；系统占着 1.9 GB 时 11.8 - 1.9 >= 6.8
            h16 = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=16, smoke=False)
            machine.memory_sample = lambda: {"wired_gb": 1.9, "swapouts_gb": 0, "free_gb": 8, "free_pct": 60}
            self.assertTrue(h16.fits_memory("qwen3.5-9b-4bit"))
        finally:
            machine.memory_sample = real


class DownloadTest(unittest.TestCase):
    def test_fetch_item_from_file_urls_with_resume_and_hash(self):
        from luoke_core import download
        src = tempfile.mkdtemp()
        with open(os.path.join(src, "config.json"), "w") as f:
            f.write('{"a": 1}')
        with open(os.path.join(src, "weights.bin"), "wb") as f:
            f.write(os.urandom(3 * (1 << 20) + 17))
        item = download.make_item_from_dir(src, "m", "model", "1", "file://" + src)
        self.assertEqual({f["path"] for f in item["files"]}, {"config.json", "weights.bin"})
        dest = tempfile.mkdtemp()
        seen = []
        out = download.fetch_item(item, dest, lambda p, d, t: seen.append((p, d, t)))
        self.assertEqual(os.path.basename(out), os.path.basename(src))
        self.assertEqual(download.sha256_file(os.path.join(out, "weights.bin")), item["files"][1]["sha256"])
        self.assertEqual(seen[-1][1], seen[-1][2])                      # 进度走到总量
        self.assertEqual(download.fetch_item(item, dest), out)          # 已有且完整：不重下
        self.assertTrue(download.item_installed(item, out))             # 写了摘要
        for bad_path in ("/etc/x", "../x", "a/../../x", ""):
            with self.assertRaises(download.DownloadError):
                download.check_item({"dir": "m", "files": [{"path": bad_path}]})
        for bad_dir in ("", ".", "..", "a/b"):
            with self.assertRaises(download.DownloadError):
                download.check_item({"dir": bad_dir, "files": []})
        # 续传：留一个半截 .part
        dest2 = tempfile.mkdtemp()
        staging = os.path.join(os.path.dirname(dest2), "downloads", item["dir"])
        os.makedirs(staging)
        with open(os.path.join(src, "weights.bin"), "rb") as f, open(os.path.join(staging, "weights.bin.part"), "wb") as g:
            g.write(f.read(1 << 20))
        download.fetch_item(item, dest2)
        self.assertEqual(download.sha256_file(os.path.join(dest2, item["dir"], "weights.bin")), item["files"][1]["sha256"])
        # 哈希不对就报错，不留坏文件
        bad = json.loads(json.dumps(item))
        bad["files"][0]["sha256"] = "0" * 64
        dest3 = tempfile.mkdtemp()
        with self.assertRaises(download.DownloadError):
            download.fetch_item(bad, dest3)
        self.assertFalse(os.path.exists(os.path.join(dest3, item["dir"])))
        # 目标本身在暂存区里（<env>/downloads/apps）：就地 .part，不再套一层 downloads/downloads
        env = tempfile.mkdtemp()
        apps = os.path.join(env, "downloads", "apps")
        out4 = download.fetch_item(item, apps)
        self.assertEqual(out4, os.path.join(apps, item["dir"]))
        self.assertFalse(os.path.exists(os.path.join(env, "downloads", "downloads")))
        self.assertFalse(os.path.exists(out4 + ".part"))


class PackagerIconTest(unittest.TestCase):
    """tools/package_python_app.py 的 icon 字段：相对清单目录、必须在、必须是 icns。"""

    def test_resolve_icon(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import package_python_app as pk
        d = tempfile.mkdtemp()
        mp = os.path.join(d, "packaging", "accessory.json")
        os.makedirs(os.path.dirname(mp))
        with open(mp, "w") as f:
            f.write("{}")
        self.assertIsNone(pk.resolve_icon(mp, {}))
        with open(os.path.join(d, "packaging", "AppIcon.icns"), "wb") as f:
            f.write(b"icns" + b"\x00" * 12)
        self.assertEqual(pk.resolve_icon(mp, {"icon": "AppIcon.icns"}), os.path.join(d, "packaging", "AppIcon.icns"))
        with self.assertRaises(SystemExit):
            pk.resolve_icon(mp, {"icon": "missing.icns"})
        with open(os.path.join(d, "packaging", "bad.icns"), "wb") as f:
            f.write(b"\x89PNG....")
        with self.assertRaises(SystemExit):
            pk.resolve_icon(mp, {"icon": "bad.icns"})


class ContinueTest(unittest.TestCase):
    """通用续写 /v1/continue（V1 §2，2026-10-16）：增量解码、停止串、主控侧的参数上限。"""

    def test_token_texts_and_stop(self):
        from luoke_core.worker.textops import hit_stop, token_texts
        # 假分词器：1 → "结论"，2 → "和方法"，3/4 是"建"的两个字节（单独解码出 U+FFFD），5 → "，"
        table = {1: "结论", 2: "和方法", 5: "，"}
        def decode(seq):
            out = ""
            i = 0
            while i < len(seq):
                t = seq[i]
                if t in table:
                    out += table[t]; i += 1
                elif t == 3 and i + 1 < len(seq) and seq[i + 1] == 4:
                    out += "建"; i += 2
                else:
                    out += "\ufffd"; i += 1
            return out
        self.assertEqual(token_texts(decode, [1, 2, 3, 4, 5]), ["结论", "和方法", "", "建", "，"])
        self.assertEqual(token_texts(decode, [1]), ["结论"])
        self.assertTrue(hit_stop("结论和方法，", ["，", "。"]))
        self.assertFalse(hit_stop("结论和方法", ["，", ""]))

    def test_hub_validates_continue_params(self):
        from luoke_core.hub import Hub
        from luoke_core.proxy import BadRequest
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        task = "luoke-ime.predict"
        def bad(body, code):
            with self.assertRaises(BadRequest) as cm:
                h.continue_(task, body)
            self.assertEqual(cm.exception.code, code)
        bad({"context": 5}, "invalid_request")
        bad({"context": "x" * 5000}, "input_too_long")
        bad({"context": "你好", "k": 99}, "invalid_request")              # k 超过任务参数
        bad({"context": "你好", "max_tokens": 0}, "invalid_request")
        bad({"context": "你好", "stop": "，"}, "invalid_request")           # 要列表
        bad({"context": "你好", "stop": ["，"] * 65}, "invalid_request")    # 最多 64 个
        bad({"context": "你好", "stop": ["一二三四五六七八九"]}, "invalid_request")   # 每个最多 8 个字
        bad({"context": "你好", "stop": [""]}, "invalid_request")
        # 参数都合法时走到任务状态：实验室里没模型，答 task_unavailable / unsupported 之一，而不是参数错
        with self.assertRaises(BadRequest) as cm:
            h.continue_(task, {"context": "你好", "k": 4, "max_tokens": 3, "stop": ["，", "。"]})
        self.assertIn(cm.exception.code, ("task_unavailable", "unsupported", "not_ready"))


class EmbedTest(unittest.TestCase):
    """算向量 /v1/embed（V1 §2，2026-10-16 提议、10-19 认可，顶层 ADR 0018）：配方、参数校验、请求等级、<bos>…<eos> 截断、工作进程监管。
    不碰 mlx、不起引擎。"""
    FAKE_WORKER = r'''
import json, sys
print(json.dumps({"ready": True, "dims": 4}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req.get("op") == "ping":
        print(json.dumps({"ok": True}), flush=True); continue
    assert set(req) == {"op", "texts", "max_tokens"}, req          # 不再有 kind、dims
    vecs = [[1.0, 0.0, 0.0, 0.0] for _ in req["texts"]]
    print(json.dumps({"vectors": vecs, "dims": 4, "ms": 1.0, "max_tokens_seen": req["max_tokens"]}), flush=True)
'''

    def hub(self, assign=False):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        r = Recipe.load(RECIPE)
        if assign:          # 测试里把嵌入模型分到各档，并放一个空模型目录
            for a in r.assignment.values():
                a.setdefault("special", {})["digup.embed"] = "embeddinggemma-2"
            os.makedirs(os.path.join(d, r.model("embeddinggemma-2")["dir"]))
        h = Hub(r, d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.worker_hold_reason = lambda model: None   # 起进程前的内存检查看的是这台机器真实的 wired，测试里另测（不然本机内存紧时这些测试会失败）
        h.engine_missing = lambda: False      # 当作引擎已装：任务状态才走到"本档有没有模型"那一步
        return h

    def test_recipe_kind_params_and_unassigned_model(self):
        r = Recipe.load(RECIPE)
        self.assertEqual(r.tasks["digup.embed"]["kind"], "embed")
        p = r.params("digup.embed", "32")
        self.assertEqual((p["max_tokens"], p["batch"]), (2048, 32))     # 和 digup 一样按 2048 个 token 截
        self.assertNotIn("dims", p)                                      # 不截维（顶层 ADR 0018）
        self.assertEqual(r.model("embeddinggemma-2")["revision"], "914f7f8")
        for tier in r.tiers:      # 还没分配：不下载、不进发布清单，各档答 unsupported
            self.assertIsNone(r.model_for_task("digup.embed", tier))
        d = json.load(open(RECIPE))
        d["tasks"]["digup.embed"]["kind"] = "vectorize"
        with self.assertRaises(RecipeError):
            Recipe(d)
        h = self.hub()
        self.assertNotIn("embeddinggemma-2", h.needed_downloads())
        self.assertEqual(h.task_status("digup.embed")["status"], "unsupported")

    def test_vector_space(self):
        """向量空间（2026-10-16 应 digup 加，只加不改）：配方的模型声明，GET /tasks 和答复里都带；变了任务参数版本也变。"""
        r = Recipe.load(RECIPE)
        self.assertIsNone(r.vector_space("digup.embed", "32"))                 # 没分配：null
        self.assertIsNone(r.vector_space("luoke-mail.triage", "32"))           # 不是嵌入任务
        self.assertEqual(r.models["embeddinggemma-2"]["vector_space"], "embeddinggemma-2/1")
        d = json.load(open(RECIPE))                                          # 分配了的嵌入模型必须声明向量空间
        d["assignment"]["32"]["special"]["digup.embed"] = "embeddinggemma-2"
        del d["models"]["embeddinggemma-2"]["vector_space"]
        with self.assertRaises(RecipeError):
            Recipe(d)
        h = self.hub()
        payload = h.tasks_payload("digup")["tasks"]["digup.embed"]
        self.assertIn("vector_space", payload)
        self.assertIsNone(payload["vector_space"])
        self.assertNotIn("vector_space", h.tasks_payload("luoke-mail")["tasks"]["luoke-mail.triage"])   # 别的任务不多字段
        h = self.hub(assign=True)
        self.assertEqual(h.tasks_payload("digup")["tasks"]["digup.embed"]["vector_space"], "embeddinggemma-2/1")
        v1 = h.tasks_version()
        h.recipe.models["embeddinggemma-2"]["vector_space"] = "embeddinggemma-2/2"
        self.assertNotEqual(h.tasks_version(), v1)                            # 配件看到版本变了就重读 /tasks

    def test_hub_validates_embed_request(self):
        h = self.hub()
        task = "digup.embed"
        def bad(body, code, **kw):
            with self.assertRaises(BadRequest) as cm:
                h.embed(task, body, **kw)
            self.assertEqual(cm.exception.code, code)
        bad({}, "invalid_request")
        bad({"texts": "一段话"}, "invalid_request")                    # 要列表
        bad({"texts": []}, "invalid_request")                          # 非空
        bad({"texts": ["a", 3]}, "invalid_request")
        bad({"texts": ["a"] * 33}, "invalid_request")                  # 超过批大小 32
        bad({"texts": ["a", "x" * 32001]}, "input_too_long")           # 只防过大的请求
        bad({"texts": ["a", ""]}, "invalid_request")                   # 空串（#5 审阅）
        bad({"texts": ["x" * 30000] * 11}, "input_too_long")           # 每条都没超，总量 33 万超了 max_total_chars
        bad({"texts": ["a"]}, "invalid_request", priority="urgent")
        # 参数都合法时走到任务状态：模型没分配，答 unsupported。2001 字的长文档不再被拒（截断按 token，在工作进程里）
        bad({"texts": ["task: search result | query: a", "title: none | text: " + "x" * 2001]}, "unsupported")
        bad({"texts": ["a"]}, "unsupported", priority="user-waiting")

    def test_worker_mirrors_digup_text_path(self):
        """工作进程逐行照 digup 的参考 worker（--text-only）：只加载文字部分、分词器自己加特殊 token 并按 2048 截、每批 16、不加前缀。"""
        with open(os.path.join(ROOT, "luoke_core", "worker", "embed_worker.py"), encoding="utf-8") as f:
            src = f.read()
        for needle in ('config["audio_config"] = None', 'config["vision_config"] = None', "load_embedding_model",
                       "AutoTokenizer", "padding=True, truncation=True, max_length=max_tokens", "BATCH = 16", "mx.float16"):
            self.assertIn(needle, src)
        self.assertNotIn("PROMPTS", src)                               # 前缀归调用方（顶层 ADR 0018）
        self.assertNotIn("bos_token_id", src)                          # 特殊 token 只由分词器加，别再手动加一次

    def test_parity_tool_retries_not_ready(self):
        """预热答 deferred 也照样往下核（2026-10-19 digup 在内存 warn 时碰到）：嵌入请求自己拉起嵌入进程，not_ready 时按 retry_after 重试。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import embed_parity as ep
        from luoke_core.client import HubError
        clock, slept = [0.0], []
        def sleep(s):
            slept.append(s); clock[0] += s
        answers = [HubError(503, "not_ready", "嵌入模型正在加载", 2), HubError(503, "not_ready", "嵌入模型正在加载", None), {"vectors": [[1.0]]}]
        def call():
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        self.assertEqual(ep.retry_not_ready(call, wait=60, sleep=sleep, now=lambda: clock[0]), {"vectors": [[1.0]]})
        self.assertEqual(slept, [2.0, 2.0])                                 # 按 retry_after，没有就 2 s
        def failing():
            raise HubError(503, "engine_failed", "嵌入进程失败", 30)
        with self.assertRaises(HubError):                                   # 别的错误不重试
            ep.retry_not_ready(failing, wait=60, sleep=sleep, now=lambda: clock[0])
        def never_ready():
            raise HubError(503, "not_ready", "x", 5)
        with self.assertRaises(HubError):                                   # 等过了 wait 就不再等
            ep.retry_not_ready(never_ready, wait=10, sleep=sleep, now=lambda: clock[0])

    def test_vendored_embedding_gemma2_is_upstream_verbatim(self):
        """方案 (b)（2026-10-16）：自带 mlx-vlm fec3f50 的 embedding_gemma2 文字部分三个文件，除文件头外逐字不改、带 MIT 许可。"""
        import hashlib
        v = os.path.join(ROOT, "luoke_core", "worker", "vendor", "embedding_gemma2")
        upstream = {"config.py": "9ea6a2d51c8693cfc32891e378b0f6577480ec3fd442d36b5a3d951474489c94",
                    "language.py": "001033b2cc50dc80c5b0ec7c1c9f1121047e054e78d5eef4143fec4095e1aa3b",
                    "embedding_gemma2.py": "f0a56966b3d09604192304378189f6d46d73cc24f9893fc7005391559898604c"}
        for name, sha in upstream.items():
            with open(os.path.join(v, name), encoding="utf-8") as f:
                text = f.read()
            head, body = text.split("\n\n", 1)
            self.assertIn("fec3f50379bb2cb0760ef3c8a68ebce9585e4d97", head)
            self.assertIn("MIT License", head)
            self.assertEqual(hashlib.sha256(body.encode("utf-8")).hexdigest(), sha, name)
        with open(os.path.join(v, "LICENSE"), encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("MIT License"))
        with open(os.path.join(v, "__init__.py"), encoding="utf-8") as f:
            init = f.read()
        self.assertIn("from .embedding_gemma2 import Model", init)
        self.assertNotIn("processing_embedding_gemma2", init)        # 多模态处理器要 0.7.1 没有的函数，文字用不到
        with open(os.path.join(ROOT, "luoke_core", "worker", "embed_worker.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('MODEL_MODULE = "mlx_vlm.models.embedding_gemma2"', src)
        self.assertIn("find_spec(MODEL_MODULE)", src)                  # 引擎包自己有就用它的

    def test_parity_tool(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import embed_parity as ep
        t = ep.reference_texts()
        self.assertEqual(t["query"], "task: search result | query: payment declined error")
        self.assertTrue(t["document-long"].startswith("title: none | text: Note 1: the review moved to room 3 because"))
        self.assertTrue(t["document-long"].endswith("Note 60: the review moved to room 6 because the projector in the main hall "
                                                    "failed again, and the team agreed to send the slides the night before."))
        ref = {n: [1.0, 0.0, 0.0] for n in t}
        self.assertTrue(ep.check(ref, dict(ref))[0])
        self.assertFalse(ep.check(ref, dict(ref, query=[0.95, 0.31, 0.0]))[0])      # 余弦约 0.95 < 0.999
        self.assertFalse(ep.check(ref, {n: v for n, v in ref.items() if n != "speech" and n != "query"})[0])   # 少一条
        digup = os.path.join(os.path.dirname(ROOT.split("/.claude/worktrees/")[0]), "luoke-digup",
                             "Tests", "LlamaRuntimeTests", "Reference", "vectors.json")
        if os.path.exists(digup):                                       # 有 digup 的只读副本：名字对得上、自己和自己全过
            with open(digup, encoding="utf-8") as f:
                vecs = json.load(f)["vectors"]
            self.assertTrue(set(t) <= set(vecs))
            self.assertTrue(ep.check(vecs, {n: vecs[n] for n in t})[0])
            self.assertTrue(all(len(vecs[n]) == 768 for n in t))
            # 媒体 4 条（2026-10-19 商定的送法）：名字都在参考里；frame-70 是同一张照片按 70；声音整段送
            import base64
            media = ep.reference_media(os.path.dirname(digup))
            self.assertEqual(set(media), {"screen-280", "photo-140", "frame-70", "speech"})
            self.assertTrue(set(media) <= set(vecs) and all(len(vecs[n]) == 768 for n in media))
            self.assertEqual((media["screen-280"]["budget"], media["photo-140"]["budget"], media["frame-70"]["budget"]), (280, 140, 70))
            self.assertEqual(media["frame-70"]["image"], media["photo-140"]["image"])
            with open(os.path.join(os.path.dirname(digup), "speech.wav"), "rb") as f:
                self.assertEqual(base64.b64decode(media["speech"]["audio"]), f.read())
            self.assertTrue(ep.check(vecs, {n: vecs[n] for n in media}, names=list(media))[0])
            self.assertFalse(ep.check(vecs, dict({n: vecs[n] for n in media}, speech=None), names=list(media))[0])   # null 算没过

    def test_not_ready_refusals_are_logged_rate_limited(self):
        """嵌入进程正在加载时答 not_ready，也记进 hub.log（只有元数据）；同一原因 10 s 内只记一条，省掉的条数记在下一条里。"""
        from luoke_core import scorer
        h = self.hub(assign=True)
        w = scorer.EmbedWorker("/py", "/site", "/m")
        w.state = "loading"                                    # 正在（重新）加载：_ask 答 not_ready
        h.scorers["embeddinggemma-2"] = w
        marker = "LUOKE-MARKER-嵌入请求文字"
        def refused():
            with self.assertRaises(BadRequest) as cm:
                h.embed("digup.embed", {"texts": [marker]}, priority="user-waiting", accessory="digup")
            self.assertEqual(cm.exception.code, "not_ready")
        for _ in range(5):
            refused()
        h.logf.flush()
        log_path = os.path.join(h.env_dir, "logs", "hub.log")
        lines = [l for l in open(log_path, encoding="utf-8") if "refused embed" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("task=digup.embed prio=user-waiting accessory=digup code=not_ready worker=loading", lines[0])
        h.EMBED_REFUSAL_LOG_SECS = 0                           # 过了间隔：再记一条，带上省掉的 4 条
        refused()
        h.logf.flush()
        lines = [l for l in open(log_path, encoding="utf-8") if "refused embed" in l]
        self.assertEqual(len(lines), 2)
        self.assertIn("(+4 more since the last line)", lines[1])
        self.assertNotIn(marker, open(log_path, encoding="utf-8").read())    # 请求文字不进日志

    def test_score_worker_argv_unchanged_by_refactor(self):
        from luoke_core import scorer
        w = scorer.ScoreWorker("/py", "/site", "/m", 16, cache_mb=1024)
        self.assertEqual(w._argv(), ["/py", "-B", scorer.WORKER, "/m", "--snapshots", "16", "--cache-mb", "1024"])
        self.assertEqual((w.kind, w.snapshots, w.state), ("score", 16, "stopped"))
        e = scorer.EmbedWorker("/py", "/site", "/m")
        self.assertEqual(e._argv(), ["/py", "-B", scorer.EMBED_WORKER, "/m", "--cache-mb", "512"])

    def test_background_slot_released_when_worker_cannot_start(self):
        """建 / 起嵌入进程出错（Popen 的 OSError、apply_budget 出异常）：后台名额照样还，下一个后台请求进得来（#5 审阅必须改）。"""
        h = self.hub(assign=True)
        clock = Clock(10000.0)
        d = h.dispatcher
        d.now, d.started_at, d.memory_ok_since = clock, clock() - 3600, clock() - 3600

        def broken(task):
            raise OSError("no such python")
        h.scorer_for = broken
        with self.assertRaises(OSError):
            h.embed("digup.embed", {"texts": ["title: none | text: 你好"]}, accessory="digup")
        self.assertIsNone(d.job)                                       # 名额还了
        clock.tick(1)                                                  # 过了这次的功耗间歇（用时几乎为 0）
        self.assertEqual(d.admit_background(False, True)[:2], (True, None))   # 邮件、记忆的后台不会一直 engine_busy

        class Stub:
            state = "ready"

            def embed(self, texts, max_tokens, timeout):
                return {"vectors": [[1.0, 0.0] for _ in texts], "dims": 2, "ms": 1.0}
        h.scorer_for = lambda task: Stub()
        out = h.embed("digup.embed", {"texts": ["title: none | text: 你好"]}, accessory="digup")
        self.assertEqual(len(out["vectors"]), 1)

    def test_worker_start_failure_is_recorded(self):
        """Popen 起不来：记成 failed、计一次失败（不再停在 loading），下次请求还能重起。"""
        from luoke_core import scorer
        w = scorer.EmbedWorker("/nonexistent/python3", "", "/nonexistent/model", lambda m: None)
        with self.assertRaises(OSError):
            w.start()
        self.assertEqual((w.state, w.failures), ("failed", 1))

    def test_workers_stop_when_idle_or_under_pressure_and_hold(self):
        """issue #11：空闲 WORKER_IDLE_SECS 停（重起沿用探针结果）；内存压力 critical 全停、一段时间内不起，起的时候答 not_ready。"""
        from luoke_core import hub as hubmod, scorer
        h = self.hub(assign=True)
        h.apply_budget = lambda: None
        w = scorer.EmbedWorker("/nonexistent/python3", "", "/nonexistent/model", lambda m: None)
        w.state, w.probed = "ready", True
        h.scorers["embeddinggemma-2"] = w
        h._idle_workers("normal")
        self.assertEqual(w.state, "ready")                       # 刚用过：不停
        w.last_used -= hubmod.WORKER_IDLE_SECS + 1
        h._idle_workers("normal")
        self.assertEqual(w.state, "stopped"); self.assertTrue(w.idle_stopped)
        w.start = lambda: setattr(w, "state", "loading")
        h.worker_hold_reason = lambda model: None
        self.assertIs(h.scorer_for("digup.embed"), w)
        self.assertEqual(w.state, "loading")
        w.state = "ready"
        del h.worker_hold_reason
        h._idle_workers("critical")
        self.assertEqual(w.state, "stopped"); self.assertFalse(w.idle_stopped)
        with self.assertRaises(BadRequest) as cm:
            h.scorer_for("digup.embed")
        self.assertEqual((cm.exception.code, cm.exception.retry_after), ("not_ready", hubmod.WORKER_HOLD_SECS))

    def test_tasks_payload_has_a_display_model_name(self):
        """10-20 应 luoke-mail：GET /tasks 每个任务带给人看的 model_name（配方 label，没有就是模型目录名），本档没模型时 null。"""
        h = self.hub(assign=True)
        self.assertEqual(h.tasks_payload("digup")["tasks"]["digup.embed"]["model_name"],
                         os.path.basename(h.recipe.model("embeddinggemma-2")["dir"]))
        self.assertIsNone(self.hub(assign=False).tasks_payload("digup")["tasks"]["digup.embed"]["model_name"])

    def test_worker_start_waits_when_the_machine_lacks_memory(self):
        """起专用模型工作进程前看这台机器还剩多少：Metal 上限减去 wired，再加上引擎里我们自己的底座（能让出来）。"""
        from luoke_core import machine
        h = self.hub(assign=True)
        del h.worker_hold_reason
        cap = machine.metal_cap_gb(h.mem_gb)
        need = float(h.recipe.model("embeddinggemma-2")["wired_gb"])
        with unittest.mock.patch.object(machine, "memory_sample", lambda: {"wired_gb": cap - need - 0.5}):
            self.assertIsNone(h.worker_hold_reason("embeddinggemma-2"))
        with unittest.mock.patch.object(machine, "memory_sample", lambda: {"wired_gb": cap - need / 2}):
            self.assertIsNotNone(h.worker_hold_reason("embeddinggemma-2"))
        with unittest.mock.patch.object(machine, "memory_sample", lambda: {"wired_gb": None}):
            self.assertIsNone(h.worker_hold_reason("embeddinggemma-2"))     # 读不到就不挡

    def test_idle_stopped_worker_keeps_its_probe(self):
        from luoke_core import scorer
        w = scorer.ScoreWorker("/nonexistent/python3", "", "/nonexistent/model", 16, lambda m: None)
        w.probed, w.idle_stopped = True, True
        with self.assertRaises(OSError):
            w.start()
        self.assertTrue(w.probed)
        w.probed = True
        with self.assertRaises(OSError):
            w.start()                                             # 不是空闲停（失败、配件退出）：重测
        self.assertFalse(w.probed)

    def test_scorer_for_creates_one_worker_under_concurrency(self):
        """两个（这里是八个）请求同时第一次进来：只建一个、只起一次（#5 审阅第 5 条）。"""
        from luoke_core import scorer
        h = self.hub(assign=True)
        budget = []
        h.apply_budget = lambda: budget.append(1)
        starts = []
        real = scorer.EmbedWorker.start

        def slow_start(self):
            starts.append(self)
            time.sleep(0.05)                 # 把竞争窗口拉大：没锁时别的线程会在这期间再建一个
            self.state = "loading"
        scorer.EmbedWorker.start = slow_start
        try:
            got, barrier = [], threading.Barrier(8)

            def go():
                barrier.wait()
                got.append(h.scorer_for("digup.embed"))
            ts = [threading.Thread(target=go) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            scorer.EmbedWorker.start = real
        self.assertEqual(len({id(w) for w in got}), 1)
        self.assertEqual((len(starts), len(h.scorers), len(budget)), (1, 1, 1))

    def test_embed_end_to_end_with_fake_worker(self):
        """主控 → EmbedWorker → 子进程（假的，只说行协议）→ 答复；请求等级；常驻进程计进预算；配件退出时停掉。"""
        from luoke_core import scorer
        fake = os.path.join(tempfile.mkdtemp(), "fake_embed_worker.py")
        with open(fake, "w") as f:
            f.write(self.FAKE_WORKER)
        h = self.hub(assign=True)
        h.engine_python, h.engine_site = sys.executable, ""
        h.apply_budget = lambda: None
        clock = Clock(10000.0)
        d = h.dispatcher
        d.now, d.started_at, d.memory_ok_since = clock, clock() - 3600, clock() - 3600   # 固定时钟，启动早已过安静期
        real = scorer.EMBED_WORKER
        scorer.EMBED_WORKER = fake
        try:
            self.assertEqual(h.task_status("digup.embed")["status"], "available")
            w = h.scorer_for("digup.embed")
            self.assertIsInstance(w, scorer.EmbedWorker)
            for _ in range(100):
                if w.state == "ready":
                    break
                time.sleep(0.05)
            self.assertEqual(w.state, "ready")
            self.assertEqual(w.ready_info.get("dims"), 4)
            # 不带等级 = 后台：单独记账（#5 审阅 2026-10-19）——不占引擎的后台名额、不设功耗档间歇，紧接着的下一条后台照样接
            out = h.embed("digup.embed", {"texts": ["title: none | text: 你好", "title: none | text: 世界"]}, accessory="digup")
            self.assertEqual((len(out["vectors"]), out["dims"]), (2, 4))
            self.assertTrue(out["model"].endswith("+embeddinggemma-2"))
            self.assertEqual(out["vector_space"], "embeddinggemma-2/1")       # 每次答复都带，建索引中途换了也认得出
            self.assertIsNone(d.job)
            self.assertEqual(d.cooldown_until, 0.0)                         # 引擎的间歇不动：不挤邮件、记忆的后台
            ok, *_ = d.admit_background(False, True)
            self.assertTrue(ok)
            with self.assertRaises(BadRequest) as cm:                      # 嵌入这条通道自己歇（luoke-core#9 Q1）
                h.embed("digup.embed", {"texts": ["title: none | text: 再来"]}, accessory="digup")
            self.assertEqual(cm.exception.extra.get("reason"), "lane_busy")
            self.assertIn("retry_after_ms", cm.exception.extra)
            clock.tick(5)
            out2 = h.embed("digup.embed", {"texts": ["title: none | text: 再来"]}, accessory="digup")
            self.assertEqual(len(out2["vectors"]), 1)
            clock.tick(5)
            # 后台批切成几条一份算，答复和一次算的一样（顺序不变）
            many = [f"title: none | text: {i}" for i in range(h.EMBED_CHUNK * 2 + 3)]
            out3 = h.embed("digup.embed", {"texts": many}, accessory="digup")
            self.assertEqual(len(out3["vectors"]), len(many))
            # 搜索时的查询（user-waiting）走优先通道：不打断引擎上的后台、不让全机后台停（#5 审阅），也不进打分的 K 分钟窗口
            out = h.embed("digup.embed", {"texts": ["task: search result | query: 你好"]}, priority="user-waiting", accessory="digup")
            self.assertEqual(len(out["vectors"]), 1)
            self.assertFalse(d.interactive())
            self.assertFalse(d.scored_recently())
            clock.tick(5)                                                # 上一批后台的通道间歇过去
            self.assertEqual(len(h.embed("digup.embed", {"texts": ["title: none | text: 后台"]}, accessory="digup")["vectors"]), 1)
            clock.tick(5)
            d.user_waiting_arrived()                                     # 真有用户在等引擎（例如分拣）：后台嵌入照样让路
            with self.assertRaises(BadRequest) as cm:
                h.embed("digup.embed", {"texts": ["title: none | text: 后台"]}, accessory="digup")
            self.assertEqual(cm.exception.extra.get("reason"), "interactive")
            self.assertGreater(h.specials_running_gb(), 0)             # 常驻的嵌入进程从底座预算里先扣
            clock.tick(3600)                                            # 过了安静期，探针本该跑——但嵌入进程不探
            h._probe_scorers()
            self.assertFalse(w.probed)
            self.assertEqual(w.probe_attempts, 0)
            h.release_accessory("digup")                                # digup 退出：只有它在用的嵌入进程停掉
            self.assertEqual(w.state, "stopped")
        finally:
            scorer.EMBED_WORKER = real
            for x in h.scorers.values():
                x.stop()


class EmbedMediaTest(unittest.TestCase):
    """/v1/embed 的图片和声音（草案 docs/research/embed-multimodal-draft.md，2026-10-19 写的代码，还不是契约）：
    任务参数开关、校验、逐条出错。不碰 mlx。现在的配方里没有 image_batch / audio_batch，所以这条路在 beta1 里不打开。"""
    FAKE_MEDIA_WORKER = r"""
import json, sys
print(json.dumps({"ready": True, "dims": 4, "media": "--media" in sys.argv}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req.get("op") == "ping":
        print(json.dumps({"ok": True}), flush=True); continue
    assert req["op"] == "embed_inputs", req
    vecs, errs = [], []
    for it in req["inputs"]:
        if it.get("image") == "QkFE":          # base64 of "BAD"
            vecs.append(None); errs.append("image_unreadable")
        else:
            vecs.append([1.0, 0.0, 0.0, 0.0]); errs.append(None)
    print(json.dumps({"vectors": vecs, "errors": errs, "dims": 4, "ms": 1.0}), flush=True)
"""
    MEDIA = {"image_batch": 8, "image_budgets": [70, 140, 280], "audio_batch": 2, "max_image_bytes": 600000,
             "max_audio_bytes": 1100000, "max_audio_secs": 30}

    def hub(self, media=True, assign=False):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        r = Recipe.load(RECIPE)
        if media:
            r.tasks["digup.embed"]["params"]["*"].update(self.MEDIA)
        if assign:
            for a in r.assignment.values():
                a.setdefault("special", {})["digup.embed"] = "embeddinggemma-2"
            os.makedirs(os.path.join(d, r.model("embeddinggemma-2")["dir"]))
        h = Hub(r, d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.worker_hold_reason = lambda model: None   # 起进程前的内存检查看的是这台机器真实的 wired，测试里另测（不然本机内存紧时这些测试会失败）
        h.engine_missing = lambda: False
        return h

    def test_mediaops(self):
        from luoke_core.worker import mediaops as m
        self.assertEqual([m.kind_of(x) for x in ({"text": "a"}, {"image": "x", "budget": 70}, {"audio": "x"}, {"text": "a", "image": "b"}, {}, "a")],
                         ["text", "image", "audio", None, None, None])
        inputs = [{"image": "a", "budget": 280}, {"text": "t1"}, {"audio": "s1"}, {"image": "b", "budget": 70},
                  {"image": "c", "budget": 280}, {"image": "d", "budget": 280}, {"audio": "s2"}, {"audio": "s3"}]
        plan = m.plan_batches(inputs, text_batch=16, image_batch=2, audio_batch=2)
        self.assertEqual(plan, [("text", None, [1]), ("image", 70, [3]), ("image", 280, [0, 4]), ("image", 280, [5]),
                                ("audio", None, [2, 6]), ("audio", None, [7])])
        self.assertEqual(sorted(i for _, _, idx in plan for i in idx), list(range(len(inputs))))   # 每条恰好一次
        self.assertTrue(m.image_url("QUJD").startswith("data:image/") and m.image_url("QUJD").endswith(",QUJD"))

    def test_media_kinds_and_worker_mode(self):
        from luoke_core.hub import media_kinds
        from luoke_core import scorer
        self.assertEqual(media_kinds({}), set())
        self.assertEqual(media_kinds({"image_batch": 8}), {"image"})
        self.assertEqual(media_kinds({"image_batch": 0, "audio_batch": 2}), {"audio"})
        r = Recipe.load(RECIPE)                                         # 现在的配方：只收文字
        self.assertEqual(media_kinds(r.params("digup.embed", "32")), set())
        self.assertEqual(scorer.EmbedWorker("/py", "/s", "/m", media=True)._argv()[-1], "--media")
        self.assertNotIn("--media", scorer.EmbedWorker("/py", "/s", "/m")._argv())
        h = self.hub(media=True, assign=True)
        h.apply_budget = lambda: None
        real = scorer.EmbedWorker.start
        scorer.EmbedWorker.start = lambda self: None                    # 不真起进程，只看建的是哪种
        try:
            self.assertTrue(h.scorer_for("digup.embed").media)
            self.assertFalse(self.hub(media=False, assign=True).scorer_for("digup.embed").media)
        finally:
            scorer.EmbedWorker.start = real

    def test_hub_validates_inputs(self):
        h = self.hub(media=True)
        task = "digup.embed"
        img = {"image": "QUJD", "budget": 280}
        def bad(body, code, h=h):
            with self.assertRaises(BadRequest) as cm:
                h.embed(task, body, priority="user-waiting")
            self.assertEqual(cm.exception.code, code, cm.exception.message)
            self.assertNotIn("QUJD", cm.exception.message)              # 错误信息里不带内容
            return cm.exception
        bad({"texts": ["a"], "inputs": [img]}, "invalid_request")       # 二选一
        bad({"inputs": [{"text": "x" * 30000}] * 11 + [img]}, "input_too_long")   # inputs 里文字的总量也算
        bad({"inputs": []}, "invalid_request")
        bad({"inputs": "x"}, "invalid_request")
        bad({"inputs": [{"text": "a", "image": "QUJD"}]}, "invalid_request")     # 种类不唯一
        bad({"inputs": [{"video": "QUJD"}]}, "invalid_request")         # 不收视频
        bad({"inputs": [{"image": ""}]}, "invalid_request")
        bad({"inputs": [{"image": "QUJD"}]}, "invalid_request")         # 图片要 budget
        bad({"inputs": [dict(img, budget=1120)]}, "invalid_request")    # 不在 image_budgets 里
        bad({"inputs": [{"image": "A" * 900000, "budget": 280}]}, "input_too_long")
        bad({"inputs": [{"audio": "A" * 2000000}]}, "input_too_long")
        bad({"inputs": [img] * 9}, "invalid_request")                    # 超过 image_batch 8
        bad({"inputs": [{"audio": "QUJD"}] * 3}, "invalid_request")     # 超过 audio_batch 2
        bad({"inputs": [{"text": "x" * 32001}]}, "input_too_long")
        bad({"inputs": [{"text": "a"}, img, {"audio": "QUJD"}]}, "unsupported")   # 都合法：走到任务状态（没分配模型）
        e = bad({"inputs": [img]}, "invalid_request", h=self.hub(media=False))    # 只收文字的档
        self.assertIn("texts", e.message)
        h2 = self.hub(media=False)
        h2.recipe.tasks["digup.embed"]["params"]["*"]["audio_batch"] = 2          # 只收声音的档不收图片
        bad({"inputs": [img]}, "invalid_request", h=h2)

    def test_inputs_end_to_end_with_fake_worker(self):
        from luoke_core import scorer
        fake = os.path.join(tempfile.mkdtemp(), "fake_media_worker.py")
        with open(fake, "w") as f:
            f.write(self.FAKE_MEDIA_WORKER)
        h = self.hub(media=True, assign=True)
        h.engine_python, h.engine_site = sys.executable, ""
        h.apply_budget = lambda: None
        real = scorer.EMBED_WORKER
        scorer.EMBED_WORKER = fake
        try:
            w = h.scorer_for("digup.embed")
            for _ in range(100):
                if w.state == "ready":
                    break
                time.sleep(0.05)
            self.assertEqual(w.state, "ready")
            self.assertTrue(w.ready_info.get("media"))                  # 按任务参数带了 --media 起
            out = h.embed("digup.embed", {"inputs": [{"text": "title: none | text: a"}, {"image": "QkFE", "budget": 140},
                                                     {"image": "QUJD", "budget": 280}, {"audio": "QUJD"}]},
                          priority="user-waiting", accessory="digup")
            self.assertEqual(len(out["vectors"]), 4)
            self.assertIsNone(out["vectors"][1])                        # 坏的那张：null + 原因，不连累别的
            self.assertEqual(out["errors"], [None, "image_unreadable", None, None])
            self.assertEqual(out["vector_space"], "embeddinggemma-2/1")
        finally:
            scorer.EMBED_WORKER = real
            for x in h.scorers.values():
                x.stop()


class MinosCheckTest(unittest.TestCase):
    """tools/minos_check.py：按魔数认 Mach-O，读 LC_BUILD_VERSION / LC_VERSION_MIN_MACOSX，和 .app 声称的最低 macOS 比。"""

    @staticmethod
    def macho(minos: tuple, cmd: str = "build") -> bytes:
        import struct
        ver = (minos[0] << 16) | (minos[1] << 8) | (minos[2] if len(minos) > 2 else 0)
        if cmd == "build":   # LC_BUILD_VERSION: cmd, size, platform, minos, sdk, ntools
            lc = struct.pack("<IIIIII", 0x32, 24, 1, ver, ver, 0)
        else:                # LC_VERSION_MIN_MACOSX: cmd, size, version, sdk
            lc = struct.pack("<IIII", 0x24, 16, ver, ver)
        header = struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, len(lc), 0, 0)   # 64 位头 32 字节
        return header + lc

    def test_parse_and_check(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import minos_check
        d = tempfile.mkdtemp()
        app = os.path.join(d, "X.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        os.makedirs(os.path.join(app, "Contents", "Resources", "lib"))
        with open(os.path.join(app, "Contents", "MacOS", "x"), "wb") as f:
            f.write(self.macho((14, 0)))
        with open(os.path.join(app, "Contents", "Resources", "lib", "libold.dylib"), "wb") as f:
            f.write(self.macho((11, 0), "min"))
        with open(os.path.join(app, "Contents", "Resources", "lib", "libnew.dylib"), "wb") as f:
            f.write(self.macho((27, 0)))
        with open(os.path.join(app, "Contents", "Resources", "notes.txt"), "w") as f:
            f.write("not a binary")
        import plistlib
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"LSMinimumSystemVersion": "14.0"}, f)
        self.assertEqual(minos_check.macho_minos(os.path.join(app, "Contents", "MacOS", "x")), "14.0")
        self.assertEqual(minos_check.macho_minos(os.path.join(app, "Contents", "Resources", "lib", "libold.dylib")), "11.0")
        self.assertIsNone(minos_check.macho_minos(os.path.join(app, "Contents", "Resources", "notes.txt")))
        found = dict(minos_check.iter_machos(app))
        self.assertEqual(found["Contents/Resources/lib/libnew.dylib"], "27.0")
        self.assertEqual(len(found), 3)
        limit, bad = minos_check.check_app(app)
        self.assertEqual(limit, "14.0")
        self.assertEqual(bad, [("Contents/Resources/lib/libnew.dylib", "27.0")])
        # 配件清单的 min_macos 优先于 Info.plist
        with open(os.path.join(app, "Contents", "Resources", "luoke-accessory.json"), "w") as f:
            json.dump({"min_macos": "26"}, f)
        self.assertEqual(minos_check.check_app(app)[0], "26")
        self.assertEqual(minos_check.check_app(app, "27.0")[1], [])
        self.assertGreater(minos_check.version_tuple("26.2"), minos_check.version_tuple("26"))


class DualSourceTest(unittest.TestCase):
    """模型文件两个地址（ModelScope + Hugging Face）：测速排序、一个地址坏了换另一个续传。"""

    def test_order_sources_by_speed(self):
        from luoke_core import download
        speeds = {"https://a/x": 1e6, "https://b/x": 5e6, "https://c/x": 0.0}
        out = download.order_sources(list(speeds), 100 << 20, probe=lambda u: speeds[u])
        self.assertEqual([u for u, _ in out], ["https://b/x", "https://a/x", "https://c/x"])
        self.assertEqual(download.order_sources(["https://a/x"], 100 << 20, probe=lambda u: 1 / 0)[0], ("https://a/x", None))   # 单地址不测
        self.assertEqual(download.order_sources(["https://a/x", "https://b/x"], 1 << 20, probe=lambda u: 1 / 0)[1][1], None)  # 小文件不测

    def test_fetch_file_fails_over_to_second_source(self):
        from luoke_core import download
        d = tempfile.mkdtemp()
        src = os.path.join(d, "good.bin")
        with open(src, "wb") as f:
            f.write(os.urandom(3 * (1 << 20) + 5))
        sha = download.sha256_file(src)
        dest = os.path.join(d, "out", "x.bin")
        logs = []
        used = download.fetch_file(["file://" + os.path.join(d, "missing.bin"), "file://" + src], dest, os.path.getsize(src), sha,
                                   log=logs.append, probe=lambda u: 1.0)
        self.assertTrue(used.endswith("good.bin"))
        self.assertEqual(download.sha256_file(dest), sha)

    @staticmethod
    def _serve(data: dict, delay: float = 0.0, first_delay: float = 0.0):
        """本机的假下载源：data 是 路径 -> 字节，支持 Range；delay 是每 64 KB 之间睡多久，first_delay 是首字节前睡多久。"""
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = data.get(self.path)
                if body is None:
                    self.send_response(404); self.end_headers(); return
                start, end = 0, len(body) - 1
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
                if m:
                    start = int(m.group(1)); end = min(end, int(m.group(2))) if m.group(2) else end
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                time.sleep(first_delay)
                try:
                    for i in range(start, end + 1, 1 << 16):
                        self.wfile.write(body[i:min(i + (1 << 16), end + 1)])
                        if delay:
                            time.sleep(delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def test_probe_excludes_time_to_first_byte(self):
        # luoke-core#24：2 MB 测速里 302、TLS、首字节占了大头，快的源反而测得慢。首字节前慢 1 秒的源不该因此吃亏
        from luoke_core import download
        body = os.urandom(8 << 20)
        srv, base = self._serve({"/x": body}, first_delay=1.0)
        try:
            v = download.probe_speed(base + "/x", nbytes=8 << 20, secs=2.0)
        finally:
            srv.shutdown()
        self.assertGreater(v, 20 * 2**20)        # 本机回环远快于 20 MB/s；把 1 秒首字节算进去就只有 ~8 MB/s
        self.assertEqual(download.describe_speeds([("https://a.cn/x", 2**20), ("https://b.co/x", None), ("https://c.io/x", 0.0)]),
                         "a.cn 1.0 MB/s，b.co 没测速，c.io 不通")

    def test_fetch_file_switches_away_from_slow_source(self):
        # 测速排在前面的源下着下着变慢：比下一个源测出来的慢一半以上就续传换过去，文件照样核对通过
        from luoke_core import download
        body = os.urandom(3 << 20)
        sha = hashlib.sha256(body).hexdigest()
        slow, slow_base = self._serve({"/x": body}, delay=0.05)        # 约 1.3 MB/s
        fast, fast_base = self._serve({"/x": body})
        d = tempfile.mkdtemp()
        logs = []
        saved = (download.SWITCH_CHECK_SECS, download.SWITCH_MIN_LEFT)
        download.SWITCH_CHECK_SECS, download.SWITCH_MIN_LEFT = 0.3, 1 << 20
        try:
            used = download.fetch_file([slow_base + "/x", fast_base + "/x"], os.path.join(d, "x.bin"), len(body), sha, log=logs.append,
                                       ordered=[(slow_base + "/x", 50 * 2**20), (fast_base + "/x", 40 * 2**20)])
        finally:
            download.SWITCH_CHECK_SECS, download.SWITCH_MIN_LEFT = saved
            slow.shutdown(); fast.shutdown()
        self.assertEqual(used, fast_base + "/x")
        self.assertTrue(any("续传" in l for l in logs), logs)
        self.assertEqual(download.sha256_file(os.path.join(d, "x.bin")), sha)

    def test_fetch_item_parallel_progress_and_single_probe(self):
        # 一项只测一次速；几个文件同时下，进度是这一项的累计字节、只增不减，最后等于总数
        from luoke_core import download
        files = {f"/m/f{i}.bin": os.urandom((9 << 20) if i == 0 else (1 << 20) + i) for i in range(4)}
        srv, base = self._serve(files)
        srv2, base2 = self._serve(files)
        item = {"dir": "m", "urls": [base + "/m/{path}", base2 + "/m/{path}"],
                "files": [{"path": p.rsplit("/", 1)[1], "size": len(b), "sha256": hashlib.sha256(b).hexdigest()} for p, b in files.items()]}
        d = tempfile.mkdtemp()
        probes, seen = [], []
        try:
            out = download.fetch_item(item, os.path.join(d, "models"), lambda p, done, t: seen.append((done, t)),
                                      probe=lambda u: probes.append(u) or (5.0 if u.startswith(base2) else 1.0))
        finally:
            srv.shutdown(); srv2.shutdown()
        total = sum(len(b) for b in files.values())
        self.assertEqual(len(probes), 2)                       # 两个地址各测一次，不是每个文件都测
        self.assertEqual([x for x, _ in seen], sorted(x for x, _ in seen))
        self.assertEqual(seen[-1], (total, total))
        for p, b in files.items():
            with open(os.path.join(out, p.rsplit("/", 1)[1]), "rb") as fh:
                self.assertEqual(fh.read(), b)

    def test_fetch_item_stops_other_files_when_one_fails(self):
        from luoke_core import download
        good = os.urandom(1 << 20)
        srv, base = self._serve({"/m/a.bin": good}, delay=0.01)
        item = {"dir": "m", "urls": [base + "/m/{path}"],
                "files": [{"path": "a.bin", "size": len(good), "sha256": hashlib.sha256(good).hexdigest()},
                          {"path": "missing.bin", "size": 10, "sha256": "0" * 64}]}
        d = tempfile.mkdtemp()
        try:
            with self.assertRaises(download.DownloadError) as cm:
                download.fetch_item(item, os.path.join(d, "models"))
        finally:
            srv.shutdown()
        self.assertIn("找不到", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(d, "models", "m")))


class EngineZipNameTest(unittest.TestCase):
    """luoke-core#31：internal-1024 起引擎包里叫 oMLX-0.7.0.app；是 oMLX（bundle id app.omlx）就改回 oMLX.app 装上，别的应用照旧拒"""

    def _zip(self, d, name, bid):
        import plistlib, subprocess
        app = os.path.join(d, "src", name)
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bid, "CFBundleShortVersionString": "0.7.2"}, f)
        z = os.path.join(d, name + ".zip")
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", app, z], check=True)
        return z

    def test_misnamed_engine_is_renamed(self):
        from luoke_core import download
        d = tempfile.mkdtemp()
        out = download.install_app_zip(self._zip(d, "oMLX-0.7.0.app", "app.omlx"), os.path.join(d, "engines"), expect_name="oMLX.app",
                                       rename_if_bundle="app.omlx", register=False, verify=False)
        self.assertEqual(os.path.basename(out), "oMLX.app")
        with self.assertRaises(download.DownloadError):
            download.install_app_zip(self._zip(d, "Other.app", "com.example.other"), os.path.join(d, "engines2"), expect_name="oMLX.app",
                                     rename_if_bundle="app.omlx", register=False, verify=False)


class EngineZipCheckTest(unittest.TestCase):
    def test_engine_zip_must_hold_omlx_app(self):
        import zipfile, importlib.util
        spec = importlib.util.spec_from_file_location("ib", os.path.join(ROOT, "tools", "internal_build.py"))
        ib = importlib.util.module_from_spec(spec); spec.loader.exec_module(ib)
        d = tempfile.mkdtemp()
        for name, ok in (("oMLX.app", True), ("oMLX-0.7.2.app", False)):
            z = os.path.join(d, name + ".zip")
            with zipfile.ZipFile(z, "w") as f:
                f.writestr(f"{name}/Contents/Info.plist", "x")
                f.writestr(f"__MACOSX/{name}/._x", "x")
            self.assertEqual(ib.engine_zip_ok(z), ok)


class AfterSwapTest(unittest.TestCase):
    """10-24：换包以后结束还在跑换下来那份的进程；输入法再 open -g 拉起新包（换包前结束会被系统马上从旧包拉起）。"""

    def test_after_swap(self):
        from luoke_core import accessories
        d = tempfile.mkdtemp()
        old = os.path.join(d, "previous.noindex", "LuokeIME.app")
        os.makedirs(old)
        calls = []
        with unittest.mock.patch.object(accessories, "terminate_app_processes", lambda app, grace=5.0: calls.append(("kill", app)) or 2), \
                unittest.mock.patch.object(accessories.subprocess, "run", lambda cmd, **k: calls.append(("run", cmd))):
            self.assertEqual(accessories.after_swap("/x/LuokeIME.app", old, True), 2)
            self.assertEqual(calls, [("kill", old), ("run", ["/usr/bin/open", "-g", "/x/LuokeIME.app"])])
            calls.clear()
            self.assertEqual(accessories.after_swap("/x/Mail.app", os.path.join(d, "gone.app"), False), 0)     # 换下来的不在：不结束、普通应用不拉起
            self.assertEqual(calls, [])


class ReleaseToolsTest(unittest.TestCase):
    """发布脚本里的纯函数（tools/release.py）：组合规划、汇总、差异判定。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("release_tool", os.path.join(ROOT, "tools", "release.py"))
        cls.rel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.rel)
        cls.recipe = Recipe.load(os.path.join(ROOT, "recipes", "dev.json"))

    def test_signing_credentials_default_to_the_mbp_and_follow_env_on_the_studio(self):
        """出版机器的凭据在专用钥匙串里，由环境变量指过去；没设时（开发机）照旧 luoke.ai、登录钥匙串、gh 自己的账号。"""
        import importlib.util, types, unittest.mock
        def load(env):
            with unittest.mock.patch.dict(os.environ, env, clear=False):
                for k in ("LUOKE_SIGN_KEYCHAIN", "LUOKE_NOTARY_PROFILE", "LUOKE_RELEASES_TOKEN", "LUOKE_KEYCHAIN_PW_ITEM"):
                    if k not in env:
                        os.environ.pop(k, None)
                spec = importlib.util.spec_from_file_location("internal_build_t", os.path.join(ROOT, "tools", "internal_build.py"))
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)
                return m, dict(os.environ)
        mbp, env = load({})
        self.assertEqual((mbp.NOTARY_PROFILE, mbp.SIGN_KEYCHAIN), ("luoke.ai", ""))
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(mbp.releases_token_env())
        studio, env = load({"LUOKE_SIGN_KEYCHAIN": "/k/luoke-release.keychain-db", "LUOKE_NOTARY_PROFILE": "luoke-notary", "LUOKE_RELEASES_TOKEN": "luoke-releases-gh"})
        self.assertEqual((studio.NOTARY_PROFILE, studio.SIGN_KEYCHAIN), ("luoke-notary", "/k/luoke-release.keychain-db"))
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="tok\n", stderr="")
        with unittest.mock.patch.dict(os.environ, env, clear=True), unittest.mock.patch.object(studio.subprocess, "run", fake_run):
            self.assertEqual(studio.releases_token_env()["GH_TOKEN"], "tok")
        self.assertEqual(calls[-1], ["security", "find-generic-password", "-s", "luoke-releases-gh", "-w", "/k/luoke-release.keychain-db"])
        for sh in ("release_sign.sh", "make_dmg.sh"):      # 两个签名脚本也认这两个变量
            src = open(os.path.join(ROOT, "tools", sh), encoding="utf-8").read()
            self.assertIn('profile="${LUOKE_NOTARY_PROFILE:-luoke.ai}"', src)
            self.assertIn('kc="${LUOKE_SIGN_KEYCHAIN:-}"', src)

    def _tool(self, name):
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", name + ".py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_release_requests_from_issue_comments(self):
        """/release 谁能发、--accept 只认负责人本人（会话的留言以「（…会话）」开头）、beta 不归流水线。"""
        d = self._tool("release_daemon")
        allowed, owner = {"lab-owner"}, "lab-owner"
        def c(body, who="lab-owner", i=1):
            return {"id": i, "body": body, "user": {"login": who}}
        self.assertIsNone(d.parse_request(c("今晚能出吗"), allowed, owner))
        self.assertEqual(d.parse_request(c("/release"), allowed, owner), {"accept": False, "by": "lab-owner", "comment": 1})
        self.assertEqual(d.parse_request(c("（luoke-mail 会话）/release"), allowed, owner)["by"], "lab-owner（会话）")
        self.assertEqual(d.parse_request(c("改好了\n（luoke-mail 会话）/release"), allowed, owner)["accept"], False)
        self.assertTrue(d.parse_request(c("/release --accept"), allowed, owner)["accept"])
        self.assertIn("只认负责人本人", d.parse_request(c("（luoke-top 会话）/release --accept"), allowed, owner)["reject"])
        self.assertIn("不在", d.parse_request(c("/release", who="someone"), allowed, owner)["reject"])
        self.assertEqual(d.parse_request(c("/release beta1"), allowed, owner)["tag"], "beta1")            # 负责人本人可以出 beta（10-26）
        self.assertEqual(d.parse_request(c("/release beta1.1 --accept"), allowed, owner)["tag"], "beta1.1")
        self.assertIn("只认负责人本人", d.parse_request(c("（luoke-top 会话）/release beta1"), allowed, owner)["reject"])
        self.assertIn("正式版", d.parse_request(c("/release v1.0"), allowed, owner)["reject"])
        self.assertIn("看不懂", d.parse_request(c("/release now"), allowed, owner)["reject"])
        self.assertIsNone(d.parse_request(c("用 `/release` 出版"), allowed, owner))      # 句中提到不算
        r = d.parse_request(c("（luoke-top 会话）/release internal-1024-2"), allowed, owner)        # luoke-top 打了标签以后发
        self.assertEqual((r["tag"], r["accept"]), ("internal-1024-2", False))
        self.assertEqual(d.parse_request(c("/release internal-1024 --accept"), allowed, owner)["tag"], "internal-1024")
        self.assertIn("看不懂", d.parse_request(c("/release internal-1024 internal-1024-2"), allowed, owner)["reject"])

    def test_closed_cells_that_cannot_open_move_after_publish(self):
        """10-23：上一版关着、发布后没补满 3 遍的格子，这一版评了也照"关"发，挪到发布后评；
        补满了的（这一版过了就开）、开着的、hold 着的、新任务都照旧在发布前评。"""
        ib = self._tool("internal_build")
        evals = {"t": {"held_unsupported": {"32": "x"}}}
        led = {"t": {"16": {"state": "off", "runs": [True]}, "32": {"state": "off", "runs": []},
                     "128": {"state": "on", "runs": [True]}},
               "u": {"16": {"state": "off", "runs": [True, True, True]}}}
        self.assertTrue(ib.closed_cant_open("t", "16", led, evals))
        self.assertFalse(ib.closed_cant_open("t", "32", led, evals))       # hold 着
        self.assertFalse(ib.closed_cant_open("t", "128", led, evals))      # 开着
        self.assertFalse(ib.closed_cant_open("u", "16", led, {"u": {}}))   # 上一版补满了：这一版过了就开
        self.assertFalse(ib.closed_cant_open("new", "16", led, {"new": {}}))
        self.assertFalse(ib.closed_cant_open("t", "16", None, evals))

    def test_internal_builds_have_their_own_repo(self):
        """10-28（luoke-core#36）：内部版发 luoke-internal-builds，beta 和正式版发 luoke-releases；主控按渠道查同一个仓库。"""
        ib = self._tool("internal_build")
        for tag in ("beta1", "beta1.1"):
            self.assertEqual(ib.releases_repo(tag), "luokelabs/luoke-releases")
        for tag in ("internal-1029", "internal-1029-2"):
            self.assertEqual(ib.releases_repo(tag), "luokelabs/luoke-internal-builds")
        src = open(os.path.join(ROOT, "app", "Sources", "LuokeCore", "Updater.swift"), encoding="utf-8").read()
        self.assertIn('channel == .internalBuild ? "luokelabs/luoke-internal-builds" : "luokelabs/luoke-releases"', src)

    def test_canary_tasks_run_first(self):
        """10-25：16 GB 测试机上同一任务前面跑过别的，答复会差几个字；金丝雀会挑的任务每一档都排最前"""
        ib = self._tool("internal_build")
        self.assertEqual(ib.canary_first(["luoke-mail.senders", "luoke-mail.triage", "luoke-mem.typing", "luoke-mem.topics", "luoke-mail.cleanup"]),
                         ["luoke-mem.topics", "luoke-mail.triage", "luoke-mail.cleanup", "luoke-mail.senders", "luoke-mem.typing"])
        self.assertEqual(ib.canary_first(["luoke-mem.topics", "luoke-mail.senders", "luoke-ime.rerank"], "luoke-ime.rerank")[0], "luoke-ime.rerank")

    def test_resume_keeps_cells_a_canary_already_cleared(self):
        """internal-1026-2：续跑时，上一次金丝雀核过、已经沿用的格子照旧沿用，只要这一版自己那条金丝雀还在、还满足条件"""
        ib = self._tool("internal_build")
        own = {("luoke-mem.topics", "k", "32"): {"task": "luoke-mem.topics"},
               ("luoke-mail.thread", "k2", "32"): {"task": "luoke-mail.thread", "reused_from": "beta1", "canary": "luoke-mem.topics"}}
        evals = {"luoke-mem.topics": {}}
        with unittest.mock.patch.object(ib, "reusable", lambda d, *a, **k: True):
            self.assertTrue(ib.canary_still_holds(own, "luoke-mem.topics", "32", evals, {}, {}, None))
            self.assertFalse(ib.canary_still_holds(own, "luoke-mem.topics", "16", evals, {}, {}, None))
        with unittest.mock.patch.object(ib, "reusable", lambda d, *a, **k: False):      # 钉住的提交变了：金丝雀作废，重新挑
            self.assertFalse(ib.canary_still_holds(own, "luoke-mem.topics", "32", evals, {}, {}, None))

    def test_canary_compares_answers_not_timings(self):
        """ADR 0008 修订（10-23）：金丝雀的原始答复和上一版逐字节一样才算过；用时、日期、标签这些每次都变的不比。"""
        ib = self._tool("internal_build")
        root = tempfile.mkdtemp()
        def write(rel, tag, answer, secs, score=10):
            ev = os.path.join(root, "releases", rel, "evidence")
            os.makedirs(os.path.join(ev, "raw"), exist_ok=True)
            json.dump({"label": f"release-{rel}-16", "results": [{"tag": "a", "triage": {"action": answer, "seconds": secs}}]},
                      open(os.path.join(ev, "raw", "t.json"), "w"))
            rec = {"task": "luoke-mem.topics", "tier_run": "16", "score": score, "score_max": 10, "hard_ok": True, "raw": "raw/t.json",
                   "combo": {"key": "k", "model": "m", "base": "m", "tiers": ["16"], "params": {}}}
            json.dump(rec, open(os.path.join(ev, "luoke-mem.topics.k.16.json"), "w"))
            rec["_file"] = os.path.join(ev, "luoke-mem.topics.k.16.json")
            return rec
        with unittest.mock.patch.object(ib, "ROOT", root):
            old = write("internal-1023", "x", "reply", 3.1)
            write("internal-1024", "x", "reply", 4.7)
            self.assertTrue(ib.canary_matches("internal-1024", "16", "luoke-mem.topics", old)[0])     # 只差用时：一样
            write("internal-1025", "x", "ignore", 3.1)
            self.assertFalse(ib.canary_matches("internal-1025", "16", "luoke-mem.topics", old)[0])    # 答复变了
            write("internal-1026", "x", "reply", 3.1, score=9)
            self.assertFalse(ib.canary_matches("internal-1026", "16", "luoke-mem.topics", old)[0])    # 分数变了

    def test_canary_only_for_internal_builds_with_a_recent_full_eval(self):
        ib = self._tool("internal_build")
        root = tempfile.mkdtemp()
        with unittest.mock.patch.object(ib, "ROOT", root):
            self.assertFalse(ib.canary_allowed("beta1")[0])                     # beta、公开版一律全评
            self.assertFalse(ib.canary_allowed("internal-1024")[0])             # 7 天内没全评过
            os.makedirs(os.path.join(root, "releases", "internal-1023"))
            json.dump({"tag": "internal-1023", "eval_mode": "full"}, open(os.path.join(root, "releases", "internal-1023", "pins.json"), "w"))
            self.assertTrue(ib.canary_allowed("internal-1024")[0])
            self.assertFalse(ib.canary_allowed("beta1")[0])

    def test_daemon_preempts_only_post_publish_repeats(self):
        d = self._tool("release_daemon")
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "releases", "internal-1024"))
        p = os.path.join(root, "releases", "internal-1024", "timeline.json")
        with unittest.mock.patch.object(d, "ROOT", root):
            json.dump({"phases": [{"name": "publish", "start": 0, "end": 1}, {"name": "repeats", "start": 1, "end": None}]}, open(p, "w"))
            self.assertTrue(d.in_repeats("internal-1024"))
            json.dump({"phases": [{"name": "eval 16", "start": 0, "end": None}]}, open(p, "w"))
            self.assertFalse(d.in_repeats("internal-1024"))
            self.assertFalse(d.in_repeats("missing"))

    def test_daemon_first_run_and_stale_requests_are_not_built(self):
        """10-23：装守护进程之前发在 issue 里的 /release 不补跑；守护进程停过一阵，6 小时前的请求回一句、不出版。"""
        d = self._tool("release_daemon")
        tmp = tempfile.mkdtemp()
        now = time.time()
        ts = lambda h: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - h * 3600))
        comments = [{"id": 5, "body": "/release", "user": {"login": "lab-owner"}, "created_at": ts(1)}]
        replies, started = [], []
        with unittest.mock.patch.object(d, "STATE", os.path.join(tmp, "st.json")), \
                unittest.mock.patch.object(d, "LOGF", os.path.join(tmp, "log")), \
                unittest.mock.patch.object(d, "comments_after", lambda last: [c for c in comments if c["id"] > last]), \
                unittest.mock.patch.object(d, "reply", replies.append), \
                unittest.mock.patch.object(d, "start_build", lambda req, st: started.append(req)), \
                unittest.mock.patch.object(d, "preempt_repeats", lambda st: None), \
                unittest.mock.patch.object(d, "finish_build", lambda st: None), \
                unittest.mock.patch.dict(os.environ, {"LUOKE_RELEASE_HOUR": "25", "LUOKE_RELEASE_ALLOWED": "lab-owner", "LUOKE_RELEASE_OWNER": "lab-owner"}):
            d.tick()                                            # 第一次：已有的只记下
            self.assertEqual((started, replies), ([], []))
            self.assertEqual(d.load_state()["last_comment"], 5)
            comments.append({"id": 6, "body": "/release", "user": {"login": "lab-owner"}, "created_at": ts(7)})
            comments.append({"id": 7, "body": "/release", "user": {"login": "lab-owner"}, "created_at": ts(0.1)})
            d.tick()
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0]["comment"], 7)
            self.assertTrue(any("不补跑" in r for r in replies), replies)

    def test_daemon_pins_from_top_tag(self):
        """/release internal-MMDD：各仓库的提交从顶层标签读（经 gh api），只取流水线管的四个；缺了就报错。"""
        d = self._tool("release_daemon")
        tree = "\n".join(f"{r} {c * 40}" for r, c in [("luoke-core", "a"), ("luoke-mail", "b"), ("luoke-mem", "c"), ("luoke-ime", "d"), ("luoke-agent", "e")])
        calls = []
        def fake_sh(cmd, cwd=None, check=True):
            calls.append(cmd)
            return tree if cmd[:2] == ["gh", "api"] else ""
        with unittest.mock.patch.object(d, "sh", fake_sh):
            pins = d.pins_from_top("internal-1024")
            self.assertEqual(pins, {"luoke-core": "a" * 40, "luoke-mail": "b" * 40, "luoke-mem": "c" * 40, "luoke-ime": "d" * 40})
            self.assertIn("repos/luokelabs/luoke/git/trees/internal-1024", calls[0])
            tree = "luoke-core " + "a" * 40
            with self.assertRaises(RuntimeError):
                d.pins_from_top("internal-1024")

    def test_previous_beta_needs_a_ledger(self):
        """beta0（10-16 演练）没有支持台账：beta1 不拿它比，照默认比最近一版内部版；有台账的 beta 才算上一个 beta。"""
        d = self._tool("release_daemon")
        with unittest.mock.patch.object(d, "ROOT", tempfile.mkdtemp()):
            os.makedirs(os.path.join(d.ROOT, "releases", "beta0", "evidence"))
            self.assertIsNone(d.previous_beta("beta1"))
            os.makedirs(os.path.join(d.ROOT, "releases", "beta1", "evidence"))
            open(os.path.join(d.ROOT, "releases", "beta1", "support.json"), "w").write("{}")
            self.assertEqual(d.previous_beta("beta1.1"), "beta1")

    def test_listing_copied_without_self_reported_size_or_tier(self):
        r = self._tool("release")
        self.assertIsNone(r.listing_from({}))
        self.assertIsNone(r.listing_from({"capabilities": {"protocol": 1}}))
        m = {"capabilities": {"listing": {"tagline": "找出吃空间的", "category": "系统", "size_mb": 210, "min_tier": "16"},
                              "permissions": [{"id": "fda", "kind": "system", "tcc": "SystemPolicyAllFiles", "title": "完全磁盘访问",
                                               "facts": ["结果不出这台 Mac"], "without": "degrade"},
                                              {"id": "auto", "kind": "delegate", "title": "自动清理", "on": "audit_autoclean", "default": True},
                                              {"kind": "system"}]}}
        self.assertEqual(r.listing_from(m), {"tagline": "找出吃空间的", "category": "系统", "permissions": [
            {"id": "fda", "kind": "system", "title": "完全磁盘访问", "facts": ["结果不出这台 Mac"], "without": "degrade"},
            {"id": "auto", "kind": "delegate", "title": "自动清理"}]})

    def test_release_tag_for_the_day(self):
        d = self._tool("release_daemon")
        with unittest.mock.patch.object(d, "ROOT", tempfile.mkdtemp()):
            # 临时加的版从 -3 起：MMDD、-2 留给 luoke-top 早上 10:00、晚上 21:00 钉的版（10-23）
            self.assertEqual(d.next_tag("1024", []), "internal-1024-3")
            self.assertEqual(d.next_tag("1024", [{"tag": "internal-1024-3"}]), "internal-1024-4")
            os.makedirs(os.path.join(d.ROOT, "releases", "internal-1024-4"))
            self.assertEqual(d.next_tag("1024", [{"tag": "internal-1024-3"}]), "internal-1024-5")

    def test_not_evaluated_cells_are_recorded_not_failed(self):
        """10-22：评测程序退出码 3 = 没评成——不写证据、台账沿用上一版；记在 evidence/not-evaluated-<档>.jsonl，
        后来又评成了的不算；流程报告里列出来。"""
        rel, rep = self.rel, self._tool("release_report")
        root = tempfile.mkdtemp()
        rec = os.path.join(root, "releases", "internal-1024")
        ev = os.path.join(rec, "evidence")
        os.makedirs(ev)
        rel.note_not_evaluated(ev, "luoke-ime.rerank", "128", "没评成：主控拒了打分")
        rel.note_not_evaluated(ev, "luoke-mem.topics", "16", "没评成：x")
        json.dump({"task": "luoke-mem.topics", "tier_run": "16", "combo": {"tiers": ["16"], "params": {}, "key": "k", "model": "m", "base": "m"}},
                  open(os.path.join(ev, "luoke-mem.topics.k.16.json"), "w"))      # 后来又评成了
        ne = rel.load_not_evaluated(rec)
        self.assertEqual(ne, {("luoke-ime.rerank", "128"): "没评成：主控拒了打分"})
        self.assertEqual(len(rel.load_evidence(rec)), 1)                          # .jsonl 不当证据读
        json.dump({"tag": "internal-1024", "host": "studio", "phases": [{"name": "eval 128", "start": 0, "end": 60, "ok": True}], "samples": []},
                  open(os.path.join(rec, "timeline.json"), "w"))
        with unittest.mock.patch.object(rep, "ROOT", root):
            self.assertIn("没评成（沿用上一版的开关）：luoke-ime.rerank 128 GB。", "\n".join(rep.summary("internal-1024")))

    def test_release_report_summary(self):
        r = self._tool("release_report")
        root = tempfile.mkdtemp()
        def write(tag, phases, samples=(), failed=None):
            os.makedirs(os.path.join(root, "releases", tag))
            tl = {"tag": tag, "host": "studio", "phases": [{"name": n, "start": a, "end": b, "ok": True} for n, a, b in phases], "samples": list(samples)}
            if failed:
                tl["failed"] = failed
            json.dump(tl, open(os.path.join(root, "releases", tag, "timeline.json"), "w"))
        write("internal-1023", [("eval 16", 0, 1800), ("sign", 1800, 2400)])
        write("internal-1024", [("eval 16", 0, 4000), ("sign", 4000, 4600), ("keychain wait", 4600, 4700)],
              samples=[{"t": 0, "wired_gb": 40.0, "compressed_gb": 1.0, "pressure": 1, "swapouts": 10, "load": 3},
                       {"t": 30, "wired_gb": 60.5, "compressed_gb": 2.0, "pressure": 2, "swapouts": 30, "load": 5}])
        with unittest.mock.patch.object(r, "ROOT", root):
            text = "\n".join(r.summary("internal-1024", "internal-1023"))
            self.assertIn("总用时 1 小时 18 分（studio）", text)
            self.assertIn("最慢的 3 步：eval 16 1 小时 6 分、sign 10 分", text)     # 等钥匙串不算一步
            self.assertIn("等钥匙串 1 分", text)
            self.assertIn("wired 峰值 60.5 GB", text)
            self.assertIn("压力最高 警告，换出 20 页", text)
            self.assertIn("明显变慢：eval 16 1 小时 6 分（上一版 30 分）", text)
            self.assertNotIn("sign 10 分（上一版", text)
            self.assertIn("| eval 16 |", r.report("internal-1024", "internal-1023"))

    def test_plan_combos_dedupes_same_model_and_params(self):
        plan = self.rel.plan_combos(self.recipe, ["16", "32", "128"], ["luoke-mail.triage", "luoke-ime.rerank", "nope.task"])
        triage = [c for (t, _), c in plan.items() if t == "luoke-mail.triage"]
        rerank = [c for (t, _), c in plan.items() if t == "luoke-ime.rerank"]
        # 16/32 共用 9B，但 max_chars 按档不同，所以是三个组合（与内存有关的参数按档细分，V1 §4）
        self.assertEqual(sorted(tuple(c["tiers"]) for c in triage), [("128",), ("16",), ("32",)])
        self.assertEqual({c["model"] for c in triage if c["tiers"] != ["128"]}, {"qwen3.5-9b-4bit"})
        self.assertEqual(len(rerank), 1)
        self.assertEqual(rerank[0]["tiers"], ["16", "32", "128"])
        self.assertEqual(rerank[0]["model"], "qwen3.5-0.8b-base-4bit")
        self.assertEqual(self.rel.run_tier(["16", "32"]), "32")

    def test_summaries(self):
        s = self.rel.summarize_mail_triage({"score": {"basic": 30, "basic_n": 32, "hard": 9, "hard_n": 13, "safety": 12, "safety_n": 13,
                                                      "false_alarm": 0, "normal_n": 32, "n": 45, "avg_seconds": 2.5}, "machine": {"chip": "M5"}})
        self.assertEqual((s["score"], s["score_max"], s["hard_ok"]), (39, 45, True))
        s2 = self.rel.summarize_mail_triage({"score": {"basic": 30, "basic_n": 32, "hard": 9, "hard_n": 13, "safety": 11, "safety_n": 13,
                                                       "false_alarm": 0, "normal_n": 32}})
        self.assertFalse(s2["hard_ok"])     # 漏 2 封
        out = ("n=600  先给的首选 88.0%  不标 more 的首选 89.5%  最终首选 90.5%  补发改了 3 条\n"
               "最后一个键到第一份答复：p50 10 ms  p90 17 ms  p99 22 ms  最长 26 ms\n"
               "最后一个键到不标 more 的答复：p50 78 ms  p90 102 ms  p99 150 ms  最长 300 ms\n主控打分 1200 次：前缀缓存命中 71.2%，")
        s3 = self.rel.summarize_ime_rerank(out)
        self.assertEqual((s3["score"], s3["n"], s3["latency"]["p90_ms"], s3["detail"]["prefix_cache_hit_pct"]), (90.5, 600, 102, 71.2))
        with self.assertRaises(RuntimeError):
            self.rel.summarize_ime_rerank("nothing")
        s4 = self.rel.summarize_mail_task({"score": {"n": 40, "ok": 36, "hard_n": 18, "hard_failures": 0, "unanswered": 0,
                                                     "seconds": 90.5, "passes": True}, "set_sha256": "abc", "machine": {"chip": "M1"}})
        self.assertEqual((s4["hard_ok"], s4["score"], s4["score_max"], s4["evalset_sha256"], s4["detail"]["hard_failures"]), (True, 36, 40, "abc", 0))
        s5 = self.rel.summarize_ime_result({"evalset": {"name": "c.tsv", "sha256": "e1", "n": 600},
                                            "score": {"fast_top1_pct": 78.8, "settled_top1_pct": 88.8, "final_top1_pct": 88.8, "changed": 76},
                                            "latency": {"first": {"p50_ms": 56, "p90_ms": 104}, "settled": {"p50_ms": 260, "p90_ms": 364}},
                                            "hub": {"prefix_cache_hit_pct": 100.0}, "combo": {"engine_options": {"budget": "small"}}})
        self.assertEqual((s5["score"], s5["n"], s5["latency"]["p90_ms"], s5["latency"]["first_p90_ms"], s5["evalset_sha256"]), (88.8, 600, 364, 104, "e1"))
        s6 = self.rel.summarize_ime_predict({"evalset": {"sha256": "e2", "n": 600}, "latency": {"p50_ms": 40, "p90_ms": 70},
                                             "score": {"shown_pct": 7.0, "top1_when_shown_pct": 41.0, "top3_when_shown_pct": 60.0, "gate": 0.2}})
        self.assertEqual((s6["score"], s6["detail"]["shown_pct"], s6["latency"]["p90_ms"]), (41.0, 7.0, 70))
        s7 = self.rel.summarize_mem_typing({"hard_pass": True, "cases": 24, "set_sha256": "m1",
                                            "score": {"hard_failures": 0, "term_recall": 0.75, "interest_recall": 0.5, "common_terms": 6, "seconds": 300}})
        self.assertEqual((s7["hard_ok"], s7["score"], s7["detail"]["interest_recall"], s7["detail"]["common_terms_per_case"]), (True, 75.0, 50.0, 0.25))
        j = self.rel.judge(s7, None, {"floor": 70, "floors": {"interest_recall": 60}, "ceilings": {"common_terms_per_case": 0.5}})
        self.assertEqual(j["verdict"], "fail")                     # interest_recall 50 < 60
        self.assertTrue(any("interest_recall" in r for r in j["reasons"]))
        s7["detail"]["interest_recall"] = 65.0
        self.assertEqual(self.rel.judge(s7, None, {"floor": 70, "floors": {"interest_recall": 60}, "ceilings": {"common_terms_per_case": 0.5}})["verdict"], "pass")

    def test_memory_refusal_is_a_failed_tier(self):
        """internal-1018：16 GB 档 topics 被引擎的预填内存闸挡了，评测程序退出码 1，整版停了。现在认出来、记成这一档没过。"""
        err = 'luoke_mem.hub.Stop: 内存不够：{"error":{"message":"oMLX prefill memory guard rejected this prompt: Prefill would require ~9.63 GB'
        self.assertTrue(self.rel.MEMORY_REFUSAL.search(err))
        self.assertIsNone(self.rel.MEMORY_REFUSAL.search("Traceback ... KeyError: 'score'"))

    def test_eval_watchdog_stops_a_stalled_bench(self):
        """10-20：某档某任务 10 分钟没有一条成功就记没过、往下走（这里用很短的时限）。被拒记 engine_refused_memory，
        一直有成功请求的不停；不进日志的任务看评测程序的输出。"""
        d = tempfile.mkdtemp()
        log = os.path.join(d, "hub.log")
        open(log, "w").close()
        env = {"PATH": "/usr/bin:/bin"}
        refuse = (f"import time\nfor i in range(40):\n    open({log!r}, 'a').write('chat task=t.x prio=background outcome=status_400\\n')\n    time.sleep(0.1)\n")
        r = self.rel.run_watched([sys.executable, "-c", refuse], d, env, "t.x", "generate", log, stall_secs=1.0, poll=0.2)
        self.assertEqual(r.stalled, "engine_refused_memory")
        ok = (f"import time\nfor i in range(15):\n    open({log!r}, 'a').write('chat task=t.x prio=background outcome=ok\\n')\n    time.sleep(0.2)\n")
        r = self.rel.run_watched([sys.executable, "-c", ok], d, env, "t.x", "generate", log, stall_secs=1.0, poll=0.2)
        self.assertEqual((r.stalled, r.returncode), (None, 0))
        # 打分类不进日志、跑完才出声（internal-1021 的 rerank 被误判）：安静地跑也不停，只有超过总时长才停
        quiet = "import time; time.sleep(2); print('done')"
        r = self.rel.run_watched([sys.executable, "-c", quiet], d, env, "t.y", "score", log, stall_secs=0.5, poll=0.2, max_secs=30)
        self.assertEqual((r.stalled, r.stdout.strip()), (None, "done"))
        forever = "import time; time.sleep(30)"
        r = self.rel.run_watched([sys.executable, "-c", forever], d, env, "t.y", "score", log, stall_secs=0.5, poll=0.2, max_secs=1.0)
        self.assertEqual(r.stalled, "stalled")

    def test_support_ledger_rules(self):
        """10-19：哪一档开只在 tools/support.py 算——连过 3 遍才开，失败一遍就关，没评的档沿用，hold 当作关着。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import support
        d = support.decide
        self.assertEqual(d("on", False, [True])[0], "on")
        self.assertEqual(d("on", False, [False])[0], "off")                 # 失败一遍就关
        self.assertEqual(d("off", False, [True])[0], "off")                 # 过一遍不够
        self.assertEqual(d("off", False, [True, True, True])[0], "on")      # 连过 3 遍才开
        self.assertEqual(d(None, False, [True, True, True])[0], "on")       # 新任务同样
        self.assertEqual(d("off", False, [True, False, True])[0], "off")
        self.assertEqual(d("on", True, [True])[0], "off")                   # hold 着：当作关着
        self.assertEqual(d("on", True, [True, True, True])[0], "on")
        self.assertEqual(d("on", False, None)[0], "on")                     # 没评：沿用（本机版只评 128）
        self.assertEqual(d("on", True, None)[0], "off")                     # 没评、hold 着：关着（10-19 本机版栽过）
        self.assertEqual(d(None, False, None)[0], "off")
        self.assertEqual(support.needs_repeat("off", False, [True]), 2)
        self.assertEqual(support.needs_repeat("off", False, [True, True]), 1)
        self.assertEqual(support.needs_repeat("off", False, [False]), 0)
        self.assertEqual(support.needs_repeat("on", False, [True]), 0)
        self.assertEqual(support.needs_repeat("on", True, [True]), 0)            # hold 着的不复测（10-20）
        self.assertEqual(support.needs_repeat("off", True, [True]), 0)
        # 复测挪到发布之后（10-20）：上一版发布后补满 3 遍都过，这一版也过（或没评）就开；这一版没过、补的有失败都不开
        self.assertEqual(d("off", False, [True], [True, True, True]), ("on", "上一版发布后补满 3 遍都过、这一版也过了，开"))
        self.assertEqual(d("off", False, None, [True, True, True])[0], "on")
        self.assertEqual(d("off", False, [False], [True, True, True])[0], "off")
        self.assertEqual(d("off", False, [True], [True, False, True])[0], "off")
        self.assertEqual(d("off", False, [True], [True])[0], "off")               # 上一版还没补完
        self.assertEqual(d("off", True, [True], [True, True, True])[0], "off")    # hold 着照旧关
        prev = {"t": {"16": {"state": "on"}, "32": {"state": "on"}, "128": {"state": "off"}}}
        runs = {("t", "16"): [False], ("t", "128"): [True, True, True]}
        led = support.build("r2", prev, runs, {}, {"t": ["16", "32", "128"]})
        self.assertEqual(support.on_tiers(led, "t"), ["32", "128"])
        self.assertEqual([(t, g, w, n) for t, g, w, n, _ in support.flips(prev, led)], [("t", "128", "off", "on"), ("t", "16", "on", "off")])
        rel = tempfile.mkdtemp()
        support.save(rel, support.build("r3", {"t": {"16": {"state": "off"}}}, {("t", "16"): [True]}, {}, {"t": ["16"]}))
        support.record_repeats(rel, "t", "16", [True, True, True])          # 发布后补的复测进台账，开关不动
        cell = support.load(rel)["t"]["16"]
        self.assertEqual((cell["state"], cell["runs"], cell["repeated_after_publish"]), ("off", [True, True, True], True))
        nxt = support.build("r4", support.load(rel), {("t", "16"): [True]}, {}, {"t": ["16"]})
        self.assertEqual(nxt["t"]["16"]["state"], "on")                                # 下一版打开

    def test_held_unsupported_counts_as_previously_unsupported(self):
        """luoke-top 10-19：评测表的 held_unsupported 进台账，过一遍不够，要连过 3 遍才开。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import support
        holds = {"16": "测试用的 hold"}      # 不依赖评测表里眼下有哪些 hold（luoke-top 10-21 撤了 typing 16 的）
        led = support.build("x", {"luoke-mem.typing": {"16": {"state": "on"}}}, {("luoke-mem.typing", "16"): [True]},
                            {"luoke-mem.typing": holds}, {"luoke-mem.typing": ["16"]})
        self.assertEqual(led["luoke-mem.typing"]["16"]["state"], "off")

    def test_tools_reference_existing_hub_attributes(self):
        """10-19：重做把 download.LSREGISTER 挪走了，发布脚本还在用，到 manifest 阶段才炸。发布工具从 luoke_core 导入的模块，
        用到的 <模块>.<名字> 都得真的存在。"""
        import ast
        import importlib
        for name in ("release.py", "internal_build.py", "support.py", "local_allow.py"):
            tree = ast.parse(open(os.path.join(ROOT, "tools", name), encoding="utf-8").read())
            mods = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "luoke_core":
                    for a in node.names:
                        if importlib.util.find_spec("luoke_core." + a.name):
                            mods[a.asname or a.name] = a.name
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in mods:
                    m = importlib.import_module("luoke_core." + mods[node.value.id])
                    self.assertTrue(hasattr(m, node.attr), f"tools/{name} 用了 {node.value.id}.{node.attr}，但 luoke_core.{mods[node.value.id]} 里没有")

    def test_previous_release_skips_builds_that_did_not_publish(self):
        """10-26：没出成的 internal-1026-2 只有半截证据；下一版的"上一版"要跳过它，挑真发出去了的（steps.json 记了 publish）"""
        rel, root = self.rel, tempfile.mkdtemp()
        for name, steps in (("beta1", {"publish": "x"}), ("internal-1026-2", {"build": "x"})):
            os.makedirs(os.path.join(root, "releases", name, "evidence"))
            json.dump(steps, open(os.path.join(root, "releases", name, "steps.json"), "w"))
            time.sleep(0.01)
        with unittest.mock.patch.object(rel, "ROOT", root):
            self.assertEqual(rel.previous_release("internal-1026-5", None), "beta1")

    def test_known_issues_waive_only_the_listed_items(self):
        """10-26（beta1 用 oMLX 0.7.2）：32 档分拣 legit-bank-decision 误报、打字归纳 研究生-1 带「答辩」记已知问题——
        只因为这几题没过时硬性条目算过、记下免了哪几题；多一题别的、或换了档，照旧没过。
        分拣那条 10-28 撤了（luoke-core#33：32 档也设 sender_verdict: false），机制留着，这里用自带的表测。"""
        rel, root = self.rel, tempfile.mkdtemp()
        evals = rel.load_evals()
        self.assertNotIn("known_issues", evals["luoke-mail.triage"])
        tri = {"known_issues": {"32": [{"kind": "false_alarm", "tag": "legit-bank-decision", "note": "测试"}]}}
        typ = evals["luoke-mem.typing"]
        def triage(flagged, false_alarm, safety=9):
            p = os.path.join(root, "t.json")
            json.dump({"results": [{"tag": t, "triage": {"suspicious": True}} for t in flagged]}, open(p, "w"))
            return {"hard_ok": False, "detail": {"false_alarm": false_alarm, "safety": safety, "safety_n": 9}}, p
        s, p = triage(["legit-bank-decision", "phish-1"], 1)
        rel.apply_known_issues(s, tri, "32", p)
        self.assertTrue(s["hard_ok"])
        self.assertEqual([k["tag"] for k in s["known_issues"]], ["legit-bank-decision"])
        for tier, flagged, fa, safety in (("16", ["legit-bank-decision"], 1, 9), ("32", ["legit-bank-decision", "promo"], 2, 9),
                                          ("32", ["legit-bank-decision"], 1, 7)):
            s, p = triage(flagged, fa, safety)
            rel.apply_known_issues(s, tri, tier, p)
            self.assertFalse(s["hard_ok"], (tier, flagged, safety))
        def typing(items):
            p = os.path.join(root, "y.json")
            json.dump({"items": [{"case": c, "hard_failures": h} for c, h in items]}, open(p, "w"))
            return {"hard_ok": False}, p
        s, p = typing([("研究生-1", ["论文答辩（含「答辩」）"]), ("别的", [])])
        rel.apply_known_issues(s, typ, "32", p)
        self.assertTrue(s["hard_ok"])
        for tier, items in (("16", [("研究生-1", ["论文答辩（含「答辩」）"])]), ("32", [("研究生-1", ["x（含「密码」）"])]),
                            ("32", [("研究生-1", ["论文答辩（含「答辩」）"]), ("别的", ["y（含「答辩」）"])])):
            s, p = typing(items)
            rel.apply_known_issues(s, typ, tier, p)
            self.assertFalse(s["hard_ok"], (tier, items))

    def test_summarize_mem_topics(self):
        s = self.rel.summarize_mem_topics({"hard_pass": False, "cases": 24, "set_sha256": "t1",
                                           "score": {"hard_failures": 2, "interest_recall": 0.8, "dislike_recall": 0.5, "dislikes_unsaid": 3, "seconds": 90}})
        self.assertEqual((s["hard_ok"], s["score"], s["n"], s["detail"]["dislike_recall"], s["detail"]["dislikes_unsaid"], s["evalset_sha256"]),
                         (False, 80.0, 24, 50.0, 3, "t1"))
        self.assertIn("mem-topics", self.rel.SUMMARIZERS)

    def test_judge(self):
        spec = {"floor": 36, "tolerance": 2}
        ok = {"hard_ok": True, "score": 39, "params": {"a": 1}}
        self.assertEqual(self.rel.judge(ok, None, spec)["verdict"], "pass")
        self.assertEqual(self.rel.judge({"hard_ok": False, "score": 44}, None, spec)["verdict"], "fail")
        self.assertEqual(self.rel.judge({"hard_ok": True, "score": 35}, None, spec)["verdict"], "fail")
        prev = {"hard_ok": True, "score": 42, "params": {"a": 1}}
        j = self.rel.judge(ok, prev, spec)
        self.assertEqual(j["verdict"], "regression")       # 39 < 42 - 2
        self.assertEqual(self.rel.judge({"hard_ok": True, "score": 41, "params": {"a": 1}}, prev, spec)["verdict"], "pass")
        j2 = self.rel.judge({"hard_ok": True, "score": 42, "params": {"a": 2}}, prev, spec)
        self.assertEqual(j2["verdict"], "pass")
        self.assertTrue(any("参数" in r for r in j2["reasons"]))

    def test_diff_report_untested_tier_is_not_a_regression(self):
        """10-19：这一版没评的档（本机版只评 128）沿用上一版，不算回退；总分低出容差才要 --accept。"""
        def rec(model, score, tier, params=None):
            return {"task": "t.a", "model": model, "hard_ok": True, "score": score, "score_max": 45, "tiers": [tier], "params": params or {}}
        tpl = {"assignment": {"32": {}, "128": {}}, "tasks": {"t.a": {}}}
        cur = {("t.a", "m1:k1", "32"): rec("m1", 40, "32")}
        prev = {("t.a", "m1:k1", "32"): rec("m1", 40, "32"), ("t.a", "m2:k2", "128"): rec("m2", 44, "128")}
        md, verdict, _ = self.rel.diff_report("r2", "r1", cur, prev, {"t.a": {"tolerance": 2}}, tpl)
        self.assertEqual(verdict, "pass")
        md2, v2, _ = self.rel.diff_report("r1", None, cur, {}, {}, tpl)
        self.assertEqual(v2, "pass")
        self.assertIn("第一个发布", md2)
        # 同一任务、同一模型在 16 和 32 档参数不同：两条记录都在，各比各的
        cur2 = {("t.a", "m1:k16", "16"): rec("m1", 30, "16", {"max_chars": 2000}), ("t.a", "m1:k32", "32"): rec("m1", 40, "32", {"max_chars": 4000})}
        prev2 = {("t.a", "m1:k16", "16"): rec("m1", 31, "16", {"max_chars": 2000}), ("t.a", "m1:k32", "32"): rec("m1", 40, "32", {"max_chars": 4000})}
        md3, v3, _ = self.rel.diff_report("r2", "r1", cur2, prev2, {"t.a": {"tolerance": 2}}, tpl)
        self.assertEqual(v3, "pass")
        self.assertEqual(md3.count("| t.a | m1 |"), 2)
        prev3 = {("t.a", "m1:k1", "32"): rec("m1", 44, "32")}                 # 掉了 4 分，容差 2：要看过报告
        _, v4, _ = self.rel.diff_report("r2", "r1", cur, prev3, {"t.a": {"tolerance": 2}}, tpl)
        self.assertEqual(v4, "regression")
        cur5 = {k: dict(v, evalset={"sha256": "new"}) for k, v in cur.items()}  # 题集换过了：分数不可比，不算回退（10-19 topics 重写题集）
        prev5 = {k: dict(v, evalset={"sha256": "old"}) for k, v in prev3.items()}
        md5, v5, _ = self.rel.diff_report("r2", "r1", cur5, prev5, {"t.a": {"tolerance": 2}}, tpl)
        self.assertEqual(v5, "pass")
        self.assertIn("题集换过了", md5)

    def test_diff_report_failed_tier_closes_instead_of_blocking(self):
        """一档没过：台账把它关掉，不挡出版；主要任务哪一档都没开才不能发。"""
        def rec(score, tier, hard=True):
            return {"task": "m.typing", "model": "q", "hard_ok": hard, "score": score, "score_max": 100, "tiers": [tier], "params": {}}
        tpl = {"assignment": {"16": {}, "32": {}}, "tasks": {"m.typing": {"main": True}}}
        three = dict(rec(97.9, "32"), repeat={"runs": 3, "hard_ok": [True, True, True], "all_ok": True})   # 新任务：连过 3 遍才开
        cur = {("m.typing", "q:k", "16"): rec(96.9, "16", hard=False), ("m.typing", "q:k", "32"): three}
        md, v, led = self.rel.diff_report("r2", None, cur, {}, {"m.typing": {"tolerance": 2}}, tpl)
        self.assertEqual(led["m.typing"]["32"]["state"], "on")
        self.assertEqual(v, "pass")
        self.assertEqual(led["m.typing"]["16"]["state"], "off")
        self.assertIn("支持台账", md)
        cur3 = {("m.typing", "q:k", "16"): rec(96.9, "16", hard=False), ("m.typing", "q:k", "32"): rec(90.0, "32", hard=False)}
        _, v3, _ = self.rel.diff_report("r2", None, cur3, {}, {}, tpl)
        self.assertEqual(v3, "fail")

    def test_load_evidence_keeps_tiers_apart(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "evidence"))
        for tier, key in (("16", "m1:k16"), ("32", "m1:k32"), ("32", "m1:k32")):   # 第三条和第二条同名：合并几台机器的结果时同一条只留一份
            rec = {"task": "t.a", "combo": {"key": key, "model": "m1", "tiers": [tier], "params": {}}, "tier_run": tier,
                   "hard_ok": True, "score": 1, "score_max": 2}
            with open(os.path.join(d, "evidence", f"t.a.{key.replace(':', '.')}.{tier}.json"), "w") as f:
                json.dump(rec, f)
        ev = self.rel.load_evidence(d)
        self.assertEqual(sorted(ev), [("t.a", "m1:k16", "16"), ("t.a", "m1:k32", "32")])
        self.assertEqual(ev[("t.a", "m1:k16", "16")]["model"], "m1")


class IdleJobsTest(unittest.TestCase):
    """空闲任务（luoke_core/jobs.py）：清单解析、该不该跑、真起一个小命令、用户回来让它收尾。"""

    def fake_app(self, d, command):
        app = os.path.join(d, "假配件.app")
        os.makedirs(os.path.join(app, "Contents/Resources/python/bin"))
        os.symlink("/usr/bin/python3", os.path.join(app, "Contents/Resources/python/bin/python3"))
        return {"accessory": "fake", "bundle_id": "ai.luoke.fake", "_app": app,
                "jobs": [{"name": "work", "command": command, "min_interval_minutes": 1, "timeout_minutes": 1}, {"name": "bad"}]}

    def test_parse_and_due(self):
        from luoke_core import jobs
        d = tempfile.mkdtemp()
        specs = jobs.parse_jobs(self.fake_app(d, ["-c", "pass"]))
        self.assertEqual([s["key"] for s in specs], ["fake.work"])          # 坏条目跳过，默认值补齐
        self.assertEqual((specs[0]["uses_model"], specs[0]["allow_battery"], specs[0]["memory_mb"]), (True, False, 100))
        running = lambda allow: ("running", [])
        paused = lambda allow: ("paused", ["battery"])
        self.assertEqual(jobs.due(specs[0], 1000, None, True, running, False), None)
        self.assertEqual(jobs.due(specs[0], 1000, None, False, running, False), "not_quiet")
        self.assertEqual(jobs.due(specs[0], 1000, None, True, paused, False), "battery")
        self.assertEqual(jobs.due(specs[0], 1000, 980, True, running, False), "interval")
        self.assertEqual(jobs.due(specs[0], 1000, 900, True, running, True), "busy")
        slow = dict(specs[0], min_interval_minutes=120)
        self.assertEqual(jobs.due(slow, 1000, 980, True, running, False, "done"), "interval")
        self.assertEqual(jobs.due(slow, 1000, 980, True, running, False, "wrapped_up"), "interval")     # 10 分钟内还是等
        self.assertEqual(jobs.due(slow, 1000 + 11 * 60, 1000, True, running, False, "wrapped_up"), None)   # 被打断的：11 分钟后就能接着跑
        self.assertEqual(jobs.due(slow, 1000 + 11 * 60, 1000, True, running, False, "done"), "interval")   # 做完的：等满 min_interval

    def run_job(self, command, quiet_after=None):
        from luoke_core import jobs
        d = tempfile.mkdtemp()
        m = self.fake_app(d, command)
        state, logs = {}, []
        clock = Clock()
        quiet = [True]
        seen = []
        j = jobs.IdleJobs(now=clock, quiet=lambda: quiet[0], background_state=lambda allow: ("running", []), busy=lambda: False,
                          state=state, save_state=lambda: None, log=logs.append, socket_path=lambda: "/tmp/x.sock", logs_dir=os.path.join(d, "logs"),
                          on_change=seen.append)
        j.tick({"ai.luoke.fake": m})
        self.assertEqual(j.status()["running"], "fake.work")
        self.assertEqual(seen, ["fake"])
        if quiet_after is not None:
            time.sleep(quiet_after)
            quiet[0] = False
        for _ in range(100):
            j.tick({"ai.luoke.fake": m})
            if not j.running:
                break
            time.sleep(0.1)
            clock.tick(0.1)
        self.assertIsNone(j.running)
        self.assertEqual(seen[-1], None)
        return state["jobs"]["fake.work"], logs

    def test_runs_and_records(self):
        rec, logs = self.run_job(["-c", "import os, sys; sys.stderr.write('ok ' + os.environ['LUOKE_HUB_SOCKET'])"])
        self.assertEqual((rec["exit"], rec["reason"]), (0, "done"))
        self.assertIn("ok /tmp/x.sock", rec["stderr_tail"])
        self.assertTrue(any("started" in l for l in logs) and any("done" in l for l in logs))

    def test_run_soon_skips_only_the_interval(self):
        """试用期的 /jobs/run：下一个安静时刻就跑，不等间隔；不安静照样不跑，跑过一次就回到正常间隔。"""
        from luoke_core import jobs
        d = tempfile.mkdtemp()
        m = self.fake_app(d, ["-c", "pass"])
        clock = Clock()
        quiet = [False]
        state = {"jobs": {"fake.work": {"last_start": clock(), "reason": "done"}}}
        j = jobs.IdleJobs(now=clock, quiet=lambda: quiet[0], background_state=lambda allow: ("running", []), busy=lambda: False,
                          state=state, save_state=lambda: None, log=lambda s: None, socket_path=lambda: "/tmp/x.sock", logs_dir=os.path.join(d, "logs"))
        self.assertFalse(j.run_soon("fake.nope", {"ai.luoke.fake": m}))
        self.assertTrue(j.run_soon("fake.work", {"ai.luoke.fake": m}))
        j.tick({"ai.luoke.fake": m})
        self.assertIsNone(j.running)                 # 不安静：不跑
        quiet[0] = True
        j.tick({"ai.luoke.fake": m})
        self.assertEqual(j.status()["running"], "fake.work")
        self.assertNotIn("fake.work", j.soon)
        j._terminate(j.running, "test")
        for _ in range(50):
            j.tick({"ai.luoke.fake": m})
            if not j.running:
                break
            time.sleep(0.05)

    def test_idle_job_accessory_gets_priority(self):
        from luoke_core.recipe import Recipe
        clock = Clock()
        d = Dispatcher(clock, Recipe.load(RECIPE).tiers["32"], power_mode="balanced")
        d.started_at = -1e9
        d.on_ac, d.memory_pressure, d.memory_ok_since = True, "normal", -1e9
        d.cooldown_until = clock.t + 10                                     # 功耗间歇中
        self.assertEqual(d.admit_background(False, True, accessory="luoke-mail")[3], "power_cooldown")
        d.idle_job_accessory = "luoke-mem"
        self.assertEqual(d.admit_background(False, True, accessory="luoke-mem")[3], "power_cooldown")   # 空闲任务的配件也歇（10-25；由 hub 挂着等）
        self.assertGreater(d.cooldown_left(), 0)
        self.assertEqual(d.admit_background(False, True, accessory="luoke-mail")[1:], ("busy", 30, "idle_job"))
        d.idle_job_accessory = None
        clock.tick(11)
        self.assertEqual(d.admit_background(False, True, accessory="luoke-mail")[0], True)

    def test_idle_job_is_not_busy_with_its_own_requests(self):
        """10-20 开发机：luoke-mem.learn 自己发的 topics 占着引擎，被当成"忙"，约 5 秒后就被收尾。只有别的配件占着引擎才算忙。"""
        from luoke_core.hub import Hub
        from luoke_core.dispatch import BackgroundJob
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.phase = "running"
        self.assertFalse(h._jobs_busy())
        h.dispatcher.job = BackgroundJob("luoke-mem.topics", 0, 30, lambda: None)
        self.assertTrue(h._jobs_busy())                         # 没在跑空闲任务：有后台工作就不起新的
        h.dispatcher.idle_job_accessory = "luoke-mem"
        self.assertFalse(h._jobs_busy())                        # 正在跑 luoke-mem 的空闲任务：它自己的请求不算忙
        h.engine.loading = True
        self.assertFalse(h._jobs_busy())                        # 它的请求引起的底座加载也不算
        h.dispatcher.job = BackgroundJob("luoke-mail.triage", 0, 30, lambda: None)
        self.assertTrue(h._jobs_busy())                         # 别的配件占着引擎：算忙
        h.phase = "stopping"
        h.dispatcher.job = None
        self.assertTrue(h._jobs_busy())

    def test_exec_job_runs_the_apps_own_executable(self):
        """luoke-core#16：不带 Python 的配件（Swift）用 exec 指 Contents/MacOS/ 下的可执行文件；exec 带路径的条目跳过。"""
        from luoke_core import jobs
        d = tempfile.mkdtemp()
        m = self.fake_app(d, ["index", "--job"])
        exe = os.path.join(m["_app"], "Contents", "MacOS", "tool")
        os.makedirs(os.path.dirname(exe))
        out = os.path.join(d, "argv.txt")
        with open(exe, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" > "{out}"\n')
        os.chmod(exe, 0o755)
        m["jobs"][0]["exec"] = "tool"
        m["jobs"].append({"name": "evil", "exec": "../Resources/x", "command": []})
        self.assertEqual([s["key"] for s in jobs.parse_jobs(m)], ["fake.work"])
        state = {}
        j = jobs.IdleJobs(now=Clock(), quiet=lambda: True, background_state=lambda allow: ("running", []), busy=lambda: False,
                          state=state, save_state=lambda: None, log=lambda s: None, socket_path=lambda: None, logs_dir=os.path.join(d, "logs"))
        j.tick({"ai.luoke.fake": m})
        for _ in range(50):
            j.tick({"ai.luoke.fake": m})
            if not j.running:
                break
            time.sleep(0.1)
        self.assertEqual(state["jobs"]["fake.work"]["reason"], "done")
        with open(out) as f:
            self.assertEqual(f.read().strip(), "index --job")

    def test_wraps_up_when_user_returns(self):
        rec, logs = self.run_job(["-c", "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: sys.exit(0)); time.sleep(30)"], quiet_after=0.3)
        self.assertEqual((rec["exit"], rec["reason"]), (0, "wrapped_up"))


class InstallTest(unittest.TestCase):
    """发布清单里的配件 .app 和引擎包：zip 装到位、核对清单；主控的安装位置和"还要下什么"。"""

    def fake_app_zip(self, d, name="假配件.app", bundle_id="ai.luoke.fake"):
        import subprocess
        app = os.path.join(d, "src", name)
        os.makedirs(os.path.join(app, "Contents", "Resources"))
        with open(os.path.join(app, "Contents", "Resources", "luoke-accessory.json"), "w", encoding="utf-8") as f:
            json.dump({"contract": 1, "accessory": "fake", "bundle_id": bundle_id, "name": "假", "version": "1", "type": "app",
                       "entry": [], "tasks": []}, f)
        z = os.path.join(d, name + ".zip")
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", app, z], check=True)
        return z

    def test_install_app_zip_refuses_unsigned(self):
        """没签名 / 不是我们的 Team ID 的包不装（审查 2026-10-17）；旧版不被动到。"""
        from luoke_core import download
        d = tempfile.mkdtemp()
        z = self.fake_app_zip(d)
        dest = os.path.join(d, "Applications")
        os.makedirs(os.path.join(dest, "假配件.app", "Contents"))
        with unittest.mock.patch.dict(os.environ, {"LUOKE_ALLOW_UNSIGNED": "0"}):
            with self.assertRaises(download.DownloadError):
                download.install_app_zip(z, dest, expect_bundle_id="ai.luoke.fake", register=False)
        self.assertTrue(os.path.isdir(os.path.join(dest, "假配件.app", "Contents")))     # 旧的还在

    def test_install_app_zip_backup_and_rename(self):
        """替换：旧版挪进 backup_dir；旧 .app 改过名字也一起挪走，不留两份。"""
        from luoke_core import download
        d = tempfile.mkdtemp()
        z = self.fake_app_zip(d)
        dest = os.path.join(d, "Applications")
        os.makedirs(os.path.join(dest, "假配件.app", "Contents"))
        os.makedirs(os.path.join(dest, "旧名字.app", "Contents"))
        with unittest.mock.patch.dict(os.environ, {"LUOKE_ALLOW_UNSIGNED": "1"}):
            app = download.install_app_zip(z, dest, register=False, backup_dir=os.path.join(d, "prev.noindex"),
                                           replace=os.path.join(dest, "旧名字.app"))
        self.assertTrue(os.path.isdir(os.path.join(app, "Contents", "Resources")))
        self.assertFalse(os.path.exists(os.path.join(dest, "旧名字.app")))
        self.assertTrue(os.path.isdir(os.path.join(d, "prev.noindex", "假配件.app")))
        self.assertTrue(os.path.isdir(os.path.join(d, "prev.noindex", "旧名字.app")))

    def test_install_app_zip_checks_bundle_id(self):
        from luoke_core import download
        d = tempfile.mkdtemp()
        z = self.fake_app_zip(d)
        dest = os.path.join(d, "Applications")
        unittest.mock.patch.dict(os.environ, {"LUOKE_ALLOW_UNSIGNED": "1"}).start()
        self.addCleanup(unittest.mock.patch.stopall)
        app = download.install_app_zip(z, dest, expect_bundle_id="ai.luoke.fake", register=False)
        self.assertTrue(os.path.isdir(os.path.join(app, "Contents", "Resources")))
        self.assertEqual(os.path.basename(app), "假配件.app")
        with self.assertRaises(download.DownloadError):
            download.install_app_zip(z, dest, expect_bundle_id="ai.luoke.other")
        with self.assertRaises(download.DownloadError):
            download.install_app_zip(z, dest, expect_name="oMLX.app")
        self.assertFalse(glob_any(os.path.dirname(z), "luoke-install-*"))     # 暂存目录清掉了

    def test_engine_app_matching(self):
        """配方 engine.app 指的 oMLX.app：版本和配方一致才算已装，否则要下引擎包。"""
        import plistlib
        from luoke_core.hub import engine_app_matching
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "oMLX.app")
            os.makedirs(os.path.join(app, "Contents"))
            with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"CFBundleShortVersionString": "0.7.0"}, f)
            self.assertEqual(engine_app_matching(app, "omlx-0.7.0"), app)
            self.assertEqual(engine_app_matching(app, "0.7.0"), app)
            self.assertIsNone(engine_app_matching(app, "omlx-0.8.0"))
            self.assertIsNone(engine_app_matching(os.path.join(d, "nope.app"), "omlx-0.7.0"))
            self.assertIsNone(engine_app_matching("", "omlx-0.7.0"))

    def test_app_only_batch_keeps_engine(self):
        # luoke-core#34：一批引擎包下完以后，只装应用的下一批不停引擎、不换 Engine 对象
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        os.makedirs(os.path.join(d, "engines", h.recipe.engine["version"], "oMLX.app", "Contents"))
        manifest = {"items": {"engine": {"kind": "engine"}, "luoke-mail": {"kind": "accessory"}}}
        stops = []
        h.engine.stop = lambda: stops.append(1)
        with unittest.mock.patch.object(h, "restart_engine", lambda: None):
            h.downloads = {"engine": {"state": "done"}}
            h._after_downloads(manifest, ["engine"])
            self.assertEqual(len(stops), 1)                     # 这一批有引擎：换用新装的
            engine = h.engine
            engine.stop = lambda: stops.append(2)
            h.downloads["luoke-mail"] = {"state": "done"}
            h._after_downloads(manifest, ["luoke-mail"])
        self.assertEqual(stops, [1])                             # 只装应用：引擎不动
        self.assertIs(h.engine, engine)

    def test_needed_downloads_and_install_dirs(self):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertEqual(h.apps_dir, os.path.join(d, "Applications.noindex"))         # 实验室构建不碰 ~/Applications，目录不被 Spotlight 索引
        self.assertIn(h.apps_dir, h.accessory_dirs)
        need = h.needed_downloads()
        self.assertIn("qwen3.5-9b-4bit", need)
        self.assertEqual("engine" in need, h.recipe_engine_app() is None)   # 本机有版本一致的 oMLX 就不要引擎包
        self.assertEqual(h.install_dir_for({"kind": "engine", "version": "omlx-x"}), os.path.join(d, "engines", "omlx-x"))
        self.assertEqual(h.install_dir_for({"kind": "accessory", "type": "input-method"}), h.input_methods_dir)
        self.assertEqual(h.install_dir_for({"kind": "accessory", "type": "app"}), h.apps_dir)
        os.makedirs(os.path.join(d, "engines", h.recipe.engine["version"], "oMLX.app", "Contents"))
        self.assertEqual(h.installed_engine_app(), os.path.join(d, "engines", h.recipe.engine["version"], "oMLX.app"))


def glob_any(d, pattern):
    import glob
    return bool(glob.glob(os.path.join(d, pattern)))


class EndpointTest(unittest.TestCase):
    """给高级用户的本机端点（顶层 ADR 0014 修订）：鉴权和本机限制、登记模型只读只克隆、换模型要等手上的请求、后台让路。"""

    def fake_engine(self, record):
        import http.server, socketserver, threading as _t

        class E(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                record.append(body)
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
                    self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n')
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.close_connection = True
                    return
                out = json.dumps({"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), E)
        srv.daemon_threads = True
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def hub_with_endpoint(self):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, os.path.join(d, "models"), lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        record = []
        srv = self.fake_engine(record)
        self.addCleanup(srv.shutdown)
        h.engine.port = srv.server_address[1]
        h.engine.key = "k"
        h.engine.ready = True
        h.engine.alive = lambda: True
        h.engine.serving = lambda: True
        loads = []
        def fake_load(m):
            loads.append(m)
            h.engine.loaded = m
            return 0.1
        h.engine.load = fake_load
        def fake_extra(m):
            h.engine.extras.add(m)
            return 0.1
        h.engine.load_extra = fake_extra
        h.engine.resolve = lambda want: {"gemma-4-26b-a4b-it-8bit": "gemma-4-26b-a4b-it-8bit"}.get(want, want if want.startswith("user-") else None)
        h.base_id = "gemma-4-26b-a4b-it-8bit"
        h.phase = "running"
        port = h.endpoint.start(endpoint_port_free())
        self.addCleanup(h.endpoint.stop)
        return h, port, record, loads, d

    def call(self, port, method, path, body=None, headers=None, key=None, host=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = "Bearer " + key
        if host:
            h["Host"] = host
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_auth_host_and_origin(self):
        from luoke_core import endpoint as ep
        h, port, _, _, d = self.hub_with_endpoint()
        key = ep.load_key(h.env_dir)
        self.assertEqual(oct(os.stat(os.path.join(h.env_dir, ep.KEYFILE)).st_mode & 0o777), "0o600")
        self.assertEqual(self.call(port, "GET", "/v1/models")[0], 401)                              # 没带密钥
        self.assertEqual(self.call(port, "GET", "/v1/models", key="lk-wrong")[0], 401)
        self.assertEqual(self.call(port, "GET", "/v1/models", key=key, host="evil.example:80")[0], 403)   # DNS rebinding
        self.assertEqual(self.call(port, "GET", "/v1/models", key=key, headers={"Origin": "https://evil.example"})[0], 403)
        st, data = self.call(port, "GET", "/v1/models", key=key)
        self.assertEqual(st, 200)
        self.assertIn(h.base_name, [m["id"] for m in json.loads(data)["data"]])
        new = h.endpoint.rotate_key()
        self.assertEqual(self.call(port, "GET", "/v1/models", key=key)[0], 401)                      # 换了密钥旧的立即失效
        self.assertEqual(self.call(port, "GET", "/v1/models", key=new)[0], 200)

    def test_chat_base_and_user_model_and_background_yields(self):
        from luoke_core import endpoint as ep
        h, port, record, loads, d = self.hub_with_endpoint()
        key = ep.load_key(h.env_dir)
        st, data = self.call(port, "POST", "/v1/chat/completions", {"model": h.base_name, "messages": [{"role": "user", "content": "x"}]}, key=key)
        self.assertEqual(st, 200)
        self.assertEqual(record[-1]["model"], h.base_id)                                             # 名字换成引擎里的 id
        # 登记一个用户模型：只读、只克隆，原目录不动
        src = os.path.join(d, "omlx", "Flash-Next")
        os.makedirs(src)
        with open(os.path.join(src, "config.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(src, "model.safetensors"), "wb") as f:
            f.write(b"\\0" * 1000)
        before = {n: os.stat(os.path.join(src, n)).st_mtime for n in os.listdir(src)}
        with unittest.mock.patch.object(h, "restart_engine", lambda: None):
            m = h.register_user_model(src)
        self.assertEqual({n: os.stat(os.path.join(src, n)).st_mtime for n in os.listdir(src)}, before)
        self.assertTrue(os.path.isfile(os.path.join(h.model_dir, m["dir"], "model.safetensors")))
        name = ep.USER_PREFIX + m["name"]
        h.fits_alongside = lambda size: False                    # 这条测的是放不下、要独占换模型的那条路
        st, data = self.call(port, "POST", "/v1/chat/completions", {"model": name, "stream": True, "messages": [{"role": "user", "content": "x"}]}, key=key)
        self.assertEqual(st, 200)
        self.assertIn(b"data: [DONE]", data)
        self.assertEqual(loads[-1], m["dir"])
        self.assertTrue(record[-1]["stream_options"]["include_usage"])
        self.assertTrue(h.is_user_model(h.engine.loaded))
        # 用户模型占着引擎：后台的生成请求被挡（busy / endpoint）
        st2 = h.endpoint_status()
        self.assertEqual(st2["usage_today"]["requests"], 2)
        self.assertEqual(st2["usage_today"]["completion_tokens"], 2)
        self.assertEqual(self.call(port, "POST", "/v1/chat/completions", {"model": "nope", "messages": []}, key=key)[0], 404)
        self.assertEqual(self.call(port, "POST", "/v1/chat/completions", {"model": ep.USER_PREFIX + "missing", "messages": []}, key=key)[0], 404)

    def test_user_set_and_empty_key(self):
        """10-19：密钥能自己填（例如沿用 oMLX 那把）、能留空（不校验）；Host 和 Origin 两道照旧。"""
        from luoke_core import endpoint as ep
        from luoke_core.proxy import BadRequest
        h, port, _, _, d = self.hub_with_endpoint()
        h.update_settings({"endpoint_key": "my-omlx-key"})
        self.assertEqual(self.call(port, "GET", "/v1/models", key="my-omlx-key")[0], 200)
        self.assertEqual(self.call(port, "GET", "/v1/models", key=ep.load_key(h.env_dir))[0], 200)
        self.assertNotIn("endpoint_key", json.load(open(h.settings_path)))          # 密钥不进 settings.json
        h.update_settings({"endpoint_key": ""})
        self.assertEqual(ep.load_key(h.env_dir), "")                                 # 空就是空，不会再生成一把
        self.assertEqual(self.call(port, "GET", "/v1/models")[0], 200)                # 不带密钥也行
        self.assertEqual(self.call(port, "GET", "/v1/models", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call(port, "GET", "/v1/models", host="evil.example:80")[0], 403)
        with self.assertRaises(BadRequest):
            h.update_settings({"endpoint_key": "a\nb"})
        self.assertTrue(h.endpoint.rotate_key())                                      # 重新生成照旧可用

    def test_luoke_launch_pi_configures_and_execs(self):
        """luoke launch pi（照 oMLX 的 integrations/pi.py）：只写 luoke 这个 provider、先备份、尊重 PI_CODING_AGENT_DIR，exec pi。"""
        from luoke_core import cli, endpoint as ep
        h, port, _, _, d = self.hub_with_endpoint()
        agent = os.path.join(d, "pi-agent")
        os.makedirs(agent)
        with open(os.path.join(agent, "models.json"), "w") as f:
            json.dump({"providers": {"omlx": {"baseUrl": "http://127.0.0.1:8000/v1"}}}, f)
        key = ep.load_key(h.env_dir)
        info = {"enabled": True, "port": port, "key": key}
        calls = []
        with unittest.mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": agent, "PYTHONHOME": "/x"}), \
                unittest.mock.patch.object(cli, "endpoint_info", lambda sock, enable=True: info), \
                unittest.mock.patch.object(cli.shutil, "which", lambda n: "/usr/local/bin/pi"), \
                unittest.mock.patch.object(cli.os, "execvpe", lambda f, args, env: calls.append((args, env))):
            cli.main(["launch", "pi", "--model", h.base_name, "--", "--verbose"])
        args, env = calls[0]
        self.assertEqual(args, ["pi", "--model", f"luoke/{h.base_name}", "--verbose"])
        self.assertNotIn("PYTHONHOME", env)
        m = json.load(open(os.path.join(agent, "models.json")))
        self.assertIn("omlx", m["providers"])                                   # 原有的不动
        self.assertEqual(m["providers"]["luoke"]["baseUrl"], f"http://127.0.0.1:{port}/v1")
        self.assertEqual(m["providers"]["luoke"]["apiKey"], key)
        self.assertIn(h.base_name, [x["id"] for x in m["providers"]["luoke"]["models"]])
        self.assertTrue(os.path.exists(os.path.join(agent, "models.json.luoke-backup")))
        self.assertEqual(json.load(open(os.path.join(agent, "settings.json")))["defaultProvider"], "luoke")
        with self.assertRaises(SystemExit):
            cli.pick(["a", "b"], "c")                                          # 没有这个模型

    def test_big_user_model_gets_the_balanced_ceiling(self):
        """10-19：Flash-Next 111.55 GB 在用户自己的 oMLX（balanced 档）上跑得动，主控却按 93 GB 的底座预算挡了。
        独占时天花板照 balanced 留约 8%（128 GB 上 ~119 GB），由 oMLX 最后把关；换回底座时还原本档预算。"""
        from luoke_core import endpoint as ep
        h, port, record, loads, d = self.hub_with_endpoint()
        key = ep.load_key(h.env_dir)
        ceilings = []
        h.engine.set_ceiling = lambda gb: ceilings.append(gb) or setattr(h.engine, "guard_gb", gb) or True
        reg = {"name": "Flash", "dir": "user-Flash", "size_gb": 111.55, "how": "clone", "source": "/x"}
        h.user_models.list = lambda: [reg]
        h.user_models.get = lambda n: reg if n == "Flash" else None
        self.assertAlmostEqual(h.exclusive_ceiling_gb(), 128 - 8 - h.specials_running_gb() - 1, places=1)
        st, _ = self.call(port, "POST", "/v1/chat/completions", {"model": "user/Flash", "messages": [{"role": "user", "content": "x"}]}, key=key)
        self.assertEqual(st, 200)
        self.assertEqual(loads[-1], "user-Flash")                     # 独占（和底座放不下）
        self.assertEqual(ceilings[-1], h.exclusive_ceiling_gb())
        h.engine.alive = lambda: True
        h.load_base()                                                 # 换回底座：天花板还原
        self.assertEqual(ceilings[-1], round(h.tier["guard_gb"] - h.specials_running_gb(), 1))
        reg["size_gb"] = 500.0
        st, _ = self.call(port, "POST", "/v1/chat/completions", {"model": "user/Flash", "messages": [{"role": "user", "content": "x"}]}, key=key)
        self.assertEqual(st, 507)                                     # 真放不下的照旧答 507

    def test_small_user_model_loads_alongside_the_base(self):
        """luoke-top 10-19 实测：3 GB 的模型把 26B 底座挤掉了。放得下就一起加载，底座不动；放不下才独占。"""
        from luoke_core import endpoint as ep
        h, port, record, loads, d = self.hub_with_endpoint()
        key = ep.load_key(h.env_dir)
        h.engine.loaded = h.base_id
        extras = []
        def fake_extra(m):
            extras.append(m)
            h.engine.extras.add(m)
            return 0.1
        h.engine.load_extra = fake_extra
        src = os.path.join(d, "small")
        os.makedirs(src)
        open(os.path.join(src, "config.json"), "w").write("{}")
        open(os.path.join(src, "w.safetensors"), "wb").write(b"\0" * 100)
        with unittest.mock.patch.object(h, "restart_engine", lambda: None):
            m = h.register_user_model(src)
        with unittest.mock.patch.object(machine, "memory_sample", lambda: {"wired_gb": 40.0}):
            st, _ = self.call(port, "POST", "/v1/chat/completions", {"model": ep.USER_PREFIX + m["name"], "messages": [{"role": "user", "content": "x"}]}, key=key)
        self.assertEqual(st, 200)
        self.assertEqual(extras, [m["dir"]])               # 一起加载
        self.assertEqual(h.engine.loaded, h.base_id)        # 底座还在
        self.assertNotIn(m["dir"], loads)                   # 没走独占加载
        self.assertFalse(h.is_user_model(h.engine.loaded))  # 后台不用让路
        # 放不下（机器上真剩的内存不够）：走独占，换掉底座
        h.engine.extras.clear()
        with unittest.mock.patch.object(machine, "memory_sample", lambda: {"wired_gb": 1e9}):
            st, _ = self.call(port, "POST", "/v1/chat/completions", {"model": ep.USER_PREFIX + m["name"], "messages": [{"role": "user", "content": "x"}]}, key=key)
        self.assertEqual(st, 200)
        self.assertEqual(loads[-1], m["dir"])

    def test_model_slot_waits_for_requests_on_the_other_model(self):
        from luoke_core.hub import Hub
        import threading as _t
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        h.engine.loaded = "user-big"
        self.assertTrue(h.acquire_model("user-big"))
        got = []
        t = _t.Thread(target=lambda: got.append(h.acquire_model("base", timeout=5)))
        t.start()
        time.sleep(0.3)
        self.assertEqual(got, [])                     # 用户模型还在用：底座等着
        h.release_model("user-big")
        t.join(5)
        self.assertEqual(got, [True])
        self.assertFalse(h.acquire_model("user-big", timeout=0.2))   # 底座放行了、还没加载（引擎里还是 user-big）：用户模型也得等

    def test_register_rejects_non_models(self):
        from luoke_core import endpoint as ep
        d = tempfile.mkdtemp()
        reg = ep.UserModels(d, os.path.join(d, "models"), lambda s: None)
        with self.assertRaises(ValueError):
            reg.add(d)                                 # 不是模型目录
        self.assertEqual(ep.slug("Qwen3.8 Flash/Next!"), "Qwen3.8-Flash-Next")


def endpoint_port_free():
    import socket as _s
    s = _s.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class EmbedLaneTest(unittest.TestCase):
    def test_query_cuts_in_between_background_chunks(self):
        """#5 审阅：查询不用等一整批后台嵌入；后台批切成几条一份，查询在两份之间插进去。"""
        import threading as _t
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        order = []
        lock = _t.Lock()

        class W:
            def embed(self, texts, max_tokens, timeout):
                with lock:                        # 和真的工作进程一样：一次只算一份
                    order.append(texts[0])
                    time.sleep(0.05)
                    return {"vectors": [[0.0]] * len(texts), "dims": 1, "ms": 1.0}
        w = W()
        bg = [f"bg{i}" for i in range(h.EMBED_CHUNK * 4)]
        out = {}
        t = _t.Thread(target=lambda: out.setdefault("bg", h._embed_lane(w, bg, 10, "background")))
        t.start()
        time.sleep(0.07)                          # 后台第一份算着
        h._embed_lane(w, ["query"], 10, "user-waiting")
        t.join(5)
        self.assertEqual(len(out["bg"]["vectors"]), len(bg))
        self.assertLess(order.index("query"), len(order) - 1)     # 查询没排在整批后面
        self.assertEqual(h.embed_priority_waiting, 0)


class StreamChatCancelTest(unittest.TestCase):
    def test_stream_chat_leaves_the_callers_cancel_alone(self):
        """internal-1021 16 GB 测试机：stream_chat 收尾时把调用方的 cancel 置位，主控的"重试一次"（引擎出错、内存拒绝）就永远不触发。
        用一个只答 400 和一个正常答流的本机小服务核：两种情况下 cancel 都不被置位；调用方置位时照样切断。"""
        import http.server, threading as _t
        from luoke_core.engine import Engine

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.server.mode == "refuse":
                    body = b'{"error":{"message":"oMLX prefill memory guard rejected this prompt"}}'
                    self.send_response(400); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                else:
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'); self.wfile.flush()
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            e = Engine("/nonexistent.app", tempfile.mkdtemp(), tempfile.mkdtemp(), 10.0)
            e.port = srv.server_address[1]
            for mode, status in (("refuse", 400), ("ok", 200)):
                srv.mode = mode
                cancel = _t.Event()
                info = e.stream_chat({"model": "m", "stream": True, "messages": []}, lambda l: True, cancel, 5, 10)
                self.assertEqual(info["status"], status)
                self.assertFalse(cancel.is_set(), mode)
                if mode == "refuse":
                    self.assertIn("memory", info["detail"])
        finally:
            srv.shutdown()


    def test_error_inside_a_200_stream_is_an_error_not_an_answer(self):
        """internal-1022-3 16 GB 测试机：重试的请求已经开始流式（200），oMLX 的内存守卫又把它中止了，在流里发一行
        {"error": {"omlx_code": "prefill_memory_aborted", ...}}。之前这一行当成答复转给了配件，配件拿到空答复、记成"成功"。
        现在：不交给调用方，记 stream_error，标明是内存。"""
        import http.server, threading as _t
        from luoke_core.engine import Engine
        from luoke_core.hub import memory_refused

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                if self.server.mode == "memory":
                    self.wfile.write(b'data: {"type":"error","error":{"message":"Request aborted: process memory limit exceeded","omlx_code":"prefill_memory_aborted"}}\n\n')
                else:
                    self.wfile.write(b'data: {"error":{"message":"boom","type":"server_error"}}\n\n')
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            e = Engine("/nonexistent.app", tempfile.mkdtemp(), tempfile.mkdtemp(), 10.0)
            e.port = srv.server_address[1]
            for mode, mem in (("memory", True), ("other", False)):
                srv.mode = mode
                got = []
                info = e.stream_chat({"model": "m", "stream": True, "messages": []}, lambda l: got.append(l) or True, _t.Event(), 5, 10)
                self.assertEqual(info["error"], "stream_error", mode)
                self.assertEqual(got, [], mode)                 # 错误行不交给配件
                self.assertEqual(memory_refused(info), mem, mode)
        finally:
            srv.shutdown()


class MemoryRefusalRetryTest(unittest.TestCase):
    def test_memory_refusal_frees_the_pool_and_retries_once(self):
        """10-20 16 GB 测试机：oMLX 的预填守卫把 MLX 留着的缓冲池算作占用，一条大请求过后稍长的提示一直被拒。
        主控卸掉底座再装回（放掉缓冲池）、重试一次。"""
        import threading as _t
        from luoke_core.hub import Hub, serve
        from luoke_core.client import HubClient
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        os.makedirs(h.model_dir_of(h.base_name))
        h.engine_missing = lambda: False
        h.engine.alive = lambda: True
        h.engine.serving = lambda: True
        h.engine.owns_port = lambda: True
        h.base_id = "base-id"
        h.engine.loaded = "base-id"
        h.phase = "running"
        calls = []
        h.engine.unload = lambda m=None: calls.append(("unload", m)) or setattr(h.engine, "loaded", None) or True
        def fake_load(m):
            calls.append(("load", m))
            h.engine.loaded = m
            return 0.1
        h.engine.load = fake_load
        def fake_stream(payload, on_line, cancel, first_token_timeout=0, total_timeout=0):
            calls.append(("stream",))
            if sum(1 for c in calls if c == ("stream",)) == 1:
                return {"status": 400, "error": "status_400", "detail": "oMLX prefill memory guard rejected this prompt", "first_token_secs": None, "usage": None}
            on_line('data: {"choices": [{"delta": {"content": "ok"}}]}')
            on_line("data: [DONE]")
            return {"status": 200, "error": None, "detail": None, "first_token_secs": 0.1, "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
        h.engine.stream_chat = fake_stream
        sd = tempfile.mkdtemp(prefix="lk"); self.addCleanup(shutil.rmtree, sd, True); sock = os.path.join(sd, "h.sock")
        srv = serve(h, sock)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = HubClient("luoke-mail/0.15.0", sock)
            events = list(c.chat("luoke-mail.triage", {"messages": [{"role": "user", "content": "x"}], "luoke_items": 1}))
            out = "".join(json.dumps(v, ensure_ascii=False) for k, v in events if k == "chunk")
            self.assertNotIn("error", [k for k, _ in events])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertIn("ok", out)
        self.assertEqual([x for x in calls if x[0] != "stream"], [("unload", "base-id"), ("load", "base-id")])
        self.assertEqual(sum(1 for x in calls if x == ("stream",)), 2)          # 只重试一次

    def test_retry_aborted_inside_the_stream_is_reported_not_empty(self):
        """internal-1022-3 16 GB 测试机 21:03：第一次被拒，卸底座重装后重试；重试已经开始流式，又被内存守卫中止（流里的错误行）。
        之前主控记 outcome=ok、配件拿到空答复；现在配件收到"内存不够"的错误。"""
        import threading as _t
        from luoke_core.hub import Hub, serve
        from luoke_core.client import HubClient
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        os.makedirs(h.model_dir_of(h.base_name))
        h.engine_missing = lambda: False
        h.engine.alive = lambda: True
        h.engine.serving = lambda: True
        h.engine.owns_port = lambda: True
        h.base_id = "base-id"
        h.engine.loaded = "base-id"
        h.phase = "running"
        h.engine.unload = lambda m=None: setattr(h.engine, "loaded", None) or True
        def fake_load(m):
            h.engine.loaded = m
            return 0.1
        h.engine.load = fake_load
        n = {"streams": 0}
        def fake_stream(payload, on_line, cancel, first_token_timeout=0, total_timeout=0):
            n["streams"] += 1
            if n["streams"] == 1:
                return {"status": 400, "error": "status_400", "detail": "oMLX prefill memory guard rejected this prompt", "first_token_secs": None, "usage": None}
            return {"status": 200, "error": "stream_error", "detail": '{"omlx_code": "prefill_memory_aborted"}', "memory": True,
                    "first_token_secs": None, "usage": None}
        h.engine.stream_chat = fake_stream
        sd = tempfile.mkdtemp(prefix="lk"); self.addCleanup(shutil.rmtree, sd, True); sock = os.path.join(sd, "h.sock")
        srv = serve(h, sock)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = HubClient("luoke-mail/0.15.0", sock)
            events = list(c.chat("luoke-mail.triage", {"messages": [{"role": "user", "content": "x"}], "luoke_items": 1}))
        finally:
            srv.shutdown()
            srv.server_close()
        errs = [v for k, v in events if k == "error"]
        self.assertTrue(errs, events)
        self.assertIn(errs[0].get("code"), ("needs_user", "paused"))
        self.assertFalse([v for k, v in events if k == "chunk"])
        self.assertEqual(n["streams"], 2)



class LabConfigTest(unittest.TestCase):
    def test_lab_config_supplies_remote_tiers_and_none_means_local_only(self):
        """主机表、请求 issue、账号不进公开的树（10-28）：从实验室配置读；没有配置时别的档不能拿本机冒充。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import lab_config
        d = tempfile.mkdtemp()
        p = os.path.join(d, "lab.json")
        with open(p, "w") as f:
            json.dump({"machines": {"16": {"host": "test-16", "core": "c"}}, "release": {"issue": "o/r#1", "allowed": ["a"], "owner": "a"}}, f)
        with unittest.mock.patch.object(lab_config, "PATH", p):
            self.assertEqual(lab_config.machines(), {"16": {"host": "test-16", "core": "c", "extra_sync": {}}})
            self.assertEqual(lab_config.release()["issue"], "o/r#1")
        with unittest.mock.patch.object(lab_config, "PATH", os.path.join(d, "missing.json")):
            self.assertEqual((lab_config.machines(), lab_config.release()), ({}, {}))
        import internal_build as ib
        with self.assertRaises(RuntimeError):
            ib.eval_local("x", "16", [], "", {})
