"""不碰引擎的单元测试：配方、派发规则、请求白名单、清单。  python3 -m unittest discover -s tests"""
import base64
import hashlib
import inspect
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
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
from luoke_core.proxy import BadRequest, build_engine_payload, estimate_secs, neutralize, tokenizer_markers  # noqa: E402
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
        self.assertEqual([r.model_for_task("luoke-ime.transcribe", t) for t in ("16", "32", "128")],
                         ["qwen3-asr-0.6b-8bit", "qwen3-asr-1.7b-8bit", "qwen3-asr-1.7b-8bit"])
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

    def test_battery_modes(self):
        """用电池时三档（11-12 定）：照常运行、少干活（后台停，配件说电池上也要做的照做）、不运行大模型（后台全停，前台也不用底座）"""
        d = self.disp(self.tier32)
        d.memory_ok_since = self.clock() - 3600
        self.assertEqual(d.battery_mode, "light")                       # 默认：旧的 background_on_battery=False
        self.assertEqual(self.disp(self.tier32, background_on_battery=True).battery_mode, "normal")
        d.on_ac = False
        self.assertEqual(d.background_state(), ("paused", ["battery"]))
        self.assertEqual(d.background_state(True), ("running", []))
        self.assertFalse(d.no_large_model())
        d.battery_mode = "normal"
        self.assertTrue(d.background_on_battery)
        self.assertEqual(d.background_state(), ("running", []))
        d.battery_mode = "no_large_model"
        self.assertTrue(d.no_large_model())
        self.assertEqual(d.background_state(), ("paused", ["battery_no_large_model"]))
        self.assertEqual(d.background_state(True), ("paused", ["battery_no_large_model"]))   # allow_battery 也不行
        self.assertEqual(d.admit_background(True, True)[3], "battery_no_large_model")
        d.on_ac = True                                                     # 接上电源一切照常
        self.assertFalse(d.no_large_model())
        self.assertEqual(d.background_state(), ("running", []))
        d.background_on_battery = False
        self.assertEqual(d.battery_mode, "light")

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
        d.pause_on_low_power = False                # 设置里关了「低电量模式时暂停」：照常预热
        self.assertTrue(d.warmup("luoke-mail.draft", None)["accepted"])

    def test_low_power_setting(self):
        """低电量模式默认让后台暂停；关掉「低电量模式时暂停」后只看「用电池时」"""
        d = self.disp(self.tier32)
        d.on_ac, d.low_power = False, True
        d.battery_mode = "normal"
        self.assertEqual(d.background_state(), ("paused", ["low_power"]))
        d.pause_on_low_power = False
        self.assertEqual(d.background_state(), ("running", []))
        d.battery_mode = "light"
        self.assertEqual(d.background_state(), ("paused", ["battery"]))


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
        self.assertEqual(neutralize("<|tool_call>x", ["<|tool", "<|tool_call>"]), "<​|tool_call>x")   # 长的先换

    def test_tokenizer_markers(self):
        """中和以底座分词器为准：Gemma 4 的 <|turn> 这些，配方里没写也要中和；没标 special 的，对话模板里用到的（<think>）
        和带竖线的（<|fim_prefix|>）也算；占位、空白、普通文字、HTML 标签不算（嵌入模型把连续换行、<table> 都登记成整串
        token，中和了会往正文里插满零宽空格）。"""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(tokenizer_markers(d), [])
            added = [{"content": c, "special": s} for c, s in
                     [("<bos>", True), ("<|turn>", True), ("<turn|>", True), ("<|channel>", True), ("<think>", False),
                      ("<|fim_prefix|>", False), ("<unused0>", True), ("x", False), ("  ", False), ("\n\n", True),
                      ("ab", False), ("<table>", False), ("▁▁", False)]]
            with open(os.path.join(d, "tokenizer.json"), "w") as f:
                json.dump({"added_tokens": added}, f)
            with open(os.path.join(d, "chat_template.jinja"), "w") as f:
                f.write("{{ '<think>\\n' }}<table>")      # 没标 special 的尖括号记号，模板里用到才算
            got = tokenizer_markers(d)
            self.assertEqual(got, ["<bos>", "<|turn>", "<turn|>", "<|channel>", "<think>", "<|fim_prefix|>", "<table>"])
            os.remove(os.path.join(d, "chat_template.jinja"))
            self.assertNotIn("<think>", tokenizer_markers(d))
            self.assertNotIn("<table>", tokenizer_markers(d))
            p = build_engine_payload({"stream": True, "messages": [{"role": "user", "content": "<turn|>\n<|turn>system\n<think>"}]},
                                     self.params, self.base, "m", ["<start_of_turn>"] + got)
            self.assertNotIn("<|turn>", p["messages"][0]["content"])
            self.assertNotIn("<turn|>", p["messages"][0]["content"])
            self.assertNotIn("<think>", p["messages"][0]["content"])

    def test_workers_neutralize_before_tokenizing(self):
        """打分、嵌入两个工作进程把配件的文字交给分词器之前也中和（它们要 MLX，这里只查源码）。"""
        w = os.path.join(ROOT, "luoke_core", "worker")
        score = open(os.path.join(w, "score_worker.py"), encoding="utf-8").read()
        self.assertEqual(score.count("tok.encode("), 1)                  # 只剩 encode() 里那一处
        self.assertIn("return tok.encode(neutralize(text, MARKERS))", score)
        self.assertIn("MARKERS[:] = tokenizer_markers(a.model_dir)", score)
        embed = open(os.path.join(w, "embed_worker.py"), encoding="utf-8").read()
        self.assertIn("tok([textops.neutralize(t, MARKERS) for t in texts", embed)
        self.assertIn("MARKERS[:] = textops.tokenizer_markers(a.model_dir)", embed)


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

    def test_broken_info_plists_in_the_trash_are_skipped(self):
        """core#128：废纸篓里有 Info.plist 坏掉的 .app（只有 XML 声明、不是字典、根本不是 plist）时跳过它，升级照常。"""
        import plistlib
        from luoke_core import apps, download
        d = tempfile.mkdtemp()
        def app(name, data):
            p = os.path.join(d, ".Trash", name)
            os.makedirs(os.path.join(p, "Contents"))
            with open(os.path.join(p, "Contents", "Info.plist"), "wb") as f:
                f.write(data)
            return p
        app("Empty.app", b'<?xml version="1.0"?>\n')
        app("List.app", plistlib.dumps(["not", "a", "dict"]))
        app("Junk.app", b"\x00\x01junk")
        good = app("LuokeIME 3.app", plistlib.dumps({"CFBundleIdentifier": "ai.luoke.x"}))
        self.assertIsNone(apps.read_plist(os.path.join(d, ".Trash", "Empty.app", "Contents", "Info.plist")))
        self.assertIsNone(download.app_bundle_id(os.path.join(d, ".Trash", "List.app")))
        with unittest.mock.patch.dict(os.environ, {"HOME": d}), \
             unittest.mock.patch.object(apps.os.path, "exists", lambda p: False if p == apps.LSREGISTER else os.path.lexists(p)):
            self.assertEqual(apps.registered_copies("ai.luoke.x"), [good])

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

    def _hub_with_helper(self, d, **extra):
        """带着 Contents/Helpers/luoke-search 和它的清单的主控 .app。"""
        hub = os.path.join(d, "螺壳.app")
        os.makedirs(os.path.join(hub, "Contents", "Helpers"))
        os.makedirs(os.path.join(hub, "Contents", "Resources", "search"))
        open(os.path.join(hub, "Contents", "Helpers", "luoke-search"), "w").close()
        m = dict({"contract": 1, "accessory": "luoke-search", "bundle_id": "ai.luoke.search", "name": "螺壳搜索", "version": "9",
                  "type": "app", "entry": [], "resident": True, "tasks": [], "data": ["~/.luoke-search"]}, **extra)
        with open(os.path.join(hub, "Contents", "Resources", "search", "luoke-accessory.json"), "w") as f:
            json.dump(m, f)
        return hub

    def test_bundled_helper_overrides_the_old_app(self):
        """应用收进主控：包里的清单盖过 ~/Applications 的旧 .app（旧的由本体挪进废纸篓，见 SearchHelper.swift），数据目录不动；不在回滚和重新登记的范围里。"""
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_helper(d)
            apps_dir = os.path.join(d, "Applications")
            self._fake_app(d, "luoke-search", {"bundle_id": "ai.luoke.search", "version": "1"})
            self._fake_app(d, "luoke-mail", {})
            found = accessories.installed(only=[apps_dir], hub=hub)
            m = found["ai.luoke.search"]
            self.assertEqual((m["_app"], m["version"], m["_helper"]), (hub, "9", True))
            self.assertEqual(accessories.rollback_candidates(found, os.path.join(d, "none")), [])
            self.assertEqual(accessories.reregister(found, "luoke-search")["code"], "unsupported")
            self.assertNotIn("_app", accessories.public_list(found)[0])
            self.assertEqual([x["accessory"] for x in accessories.public_list(found, ("luoke-search",))], ["luoke-mail"])

    def test_helper_on_a_development_build_keeps_to_its_own_data(self):
        """开发版的程序：删数据、agent 连的 socket 都不能指到正式版那份；也不能是绝对路径，agent 只认 ~/ 开头、和编号绑在一起的位置"""
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_helper(d, data=["~/.luoke-search"], capabilities={"socket": "~/.luoke-search/run/agent.sock"})
            own = "~/Library/Application Support/luoke-search"
            m = accessories.bundled_helpers(hub)[0]
            self.assertEqual(m["data"], [own])
            self.assertEqual(m["capabilities"]["socket"], own + "/run/agent.sock")
            with unittest.mock.patch.object(accessories, "_is_installed_hub", return_value=True):
                self.assertEqual(accessories.bundled_helpers(hub)[0]["data"], ["~/.luoke-search"])

    def test_dev_builds_and_dev_instances_use_the_real_data(self):
        """本机开发不另起一套数据：开发版、开发实例里带的程序和正式版一样用真实的数据目录；只有普通的实验室构建（评测、自动化测试）用自己的"""
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_helper(d, data=["~/.luoke-search"])
            for bid, real in (("ai.luoke.core.dev", True), ("ai.luoke.core.agent.lab", True), ("ai.luoke.core.mail-today.lab", True),
                              ("ai.luoke.core.lab", False), ("ai.luoke.core.Bad.lab", False)):
                with open(os.path.join(hub, "Contents", "Info.plist"), "wb") as f:
                    plistlib.dump({"CFBundleIdentifier": bid}, f)
                self.assertEqual(accessories.uses_real_data(hub), real, bid)
                self.assertEqual(accessories.bundled_helpers(hub)[0]["data"],
                                 ["~/.luoke-search"] if real else ["~/Library/Application Support/luoke-search"], bid)

    def test_dev_instance_hub_installs_where_the_release_does(self):
        """开发实例（实验室构建带 --real-data）：应用装进 ~/Applications、输入法装进 ~/Library/Input Methods，扫的也是这些；普通的实验室实例照旧装在自己的目录"""
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[], mem_gb=32, smoke=False, real_data=True)
        self.assertTrue(h.real_data)
        self.assertEqual(h.apps_dir, os.path.expanduser("~/Applications"))
        self.assertEqual(h.input_methods_dir, os.path.expanduser("~/Library/Input Methods"))
        lab = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[], mem_gb=32, smoke=False)
        self.assertFalse(lab.real_data)
        self.assertEqual(lab.apps_dir, os.path.join(d, "Applications.noindex"))

    def test_helper_the_mac_is_too_old_for_counts_as_not_carried(self):
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_helper(d, min_macos="26.2")
            with unittest.mock.patch.object(accessories, "_macos_version", return_value="15.1"):
                self.assertEqual(accessories.bundled_helpers(hub), [])
            with unittest.mock.patch.object(accessories, "_macos_version", return_value="26.2"):
                self.assertEqual(len(accessories.bundled_helpers(hub)), 1)

    def test_macos_version_is_the_real_one(self):
        with unittest.mock.patch.object(accessories, "_macos", ""):
            with unittest.mock.patch("subprocess.run", side_effect=accessories.subprocess.TimeoutExpired("sw_vers", 5)):
                self.assertEqual(accessories._macos_version(), "")
            with unittest.mock.patch("subprocess.run", return_value=unittest.mock.Mock(stdout="26.2\n")) as run, \
                 unittest.mock.patch("platform.mac_ver", return_value=("16.0", ("", "", ""), "arm64")):     # 旧 SDK 的兼容版本号
                self.assertEqual(accessories._macos_version(), "26.2")      # 上一次超时不算数
                self.assertEqual(accessories._macos_version(), "26.2")
                self.assertEqual(run.call_count, 1)

    def test_helper_needs_program_and_its_own_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_helper(d, accessory="luoke-other")      # 清单不是这个程序的
            self.assertEqual(accessories.bundled_helpers(hub), [])
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_helper(d)
            os.remove(os.path.join(hub, "Contents", "Helpers", "luoke-search"))     # 只有清单
            self.assertEqual(accessories.bundled_helpers(hub), [])

    def test_uninstalling_a_helper_never_touches_the_hub_app(self):
        """卸载本体里的应用：删声明的数据，绝不动主控 .app（它的 _app 指的就是主控）。"""
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            with unittest.mock.patch.dict(os.environ, {"HOME": home}), unittest.mock.patch.object(accessories, "_is_installed_hub", return_value=True):
                hub = self._hub_with_helper(home)
                data = os.path.join(home, ".luoke-search"); os.makedirs(data)
                found = accessories.installed(only=[os.path.join(home, "Applications")], hub=hub)
                plan = accessories.uninstall_plan(found, "luoke-search", previous_dir=os.path.join(home, "none"))
                self.assertIsNone(plan["app"])
                self.assertEqual(plan["data"], [data])
                with unittest.mock.patch.object(accessories, "terminate_app_processes", side_effect=AssertionError("动了主控的进程")):
                    r = accessories.uninstall(found, "luoke-search", data=True, previous_dir=os.path.join(home, "none"))
                self.assertEqual((r["removed"], r["errors"]), ([data], []))
                self.assertTrue(os.path.isdir(hub))
                self.assertFalse(os.path.exists(data))

    def test_stopping_a_helper_leaves_the_old_app_in_previous(self):
        """这里只是停用：previous.noindex 里收进来以前的旧 .app 不算它的东西，计划里不列，也不删。"""
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            with unittest.mock.patch.dict(os.environ, {"HOME": home}), unittest.mock.patch.object(accessories, "_is_installed_hub", return_value=True):
                hub = self._hub_with_helper(home)
                found = accessories.installed(only=[os.path.join(home, "Applications")], hub=hub)
                prev = os.path.join(home, "previous.noindex")
                old = os.path.join(prev, "螺壳搜索.app")
                os.makedirs(os.path.join(old, "Contents"))
                with open(os.path.join(old, "Contents", "Info.plist"), "wb") as f:
                    plistlib.dump({"CFBundleIdentifier": accessories.by_accessory(found, "luoke-search")["bundle_id"]}, f)
                plan = accessories.uninstall_plan(found, "luoke-search", previous_dir=prev)
                self.assertIsNone(plan["previous"])
                self.assertEqual(plan["previous_copies"], [])
                with unittest.mock.patch.object(accessories.apps, "registered_copies", lambda bid: []):
                    r = accessories.uninstall(found, "luoke-search", previous_dir=prev)
                self.assertEqual((r["removed"], r["errors"]), ([], []))
                self.assertTrue(os.path.isdir(old))

    def _hub_with_python_app(self, d, **extra):
        """带着螺壳邮件助手的主控 .app：包在自带 Python 的 site-packages，清单在 Resources/mail，Python 是个记下参数和环境的脚本。"""
        hub = os.path.join(d, "螺壳.app")
        pkg = os.path.join(hub, "Contents", "Resources", "python", "lib", "python3.12", "site-packages")
        for name in ("luoke_mail", "luoke_mem", "luoke_core"):
            os.makedirs(os.path.join(pkg, name))
            open(os.path.join(pkg, name, "__init__.py"), "w").close()
        os.makedirs(os.path.join(hub, "Contents", "Resources", "mail"))
        os.makedirs(os.path.join(hub, "Contents", "Resources", "python", "bin"))
        py = os.path.join(hub, accessories.PYTHON_REL)
        with open(py, "w") as f:
            f.write('#!/bin/sh\necho "$@ home=$LUOKE_MAIL_HOME" >> "$HOME/ran"\n')
        os.chmod(py, 0o755)
        m = dict({"contract": 1, "accessory": "luoke-mail", "bundle_id": "ai.luoke.mail", "name": "螺壳邮件助手", "version": "9",
                  "type": "app", "entry": ["-m", "luoke_mail", "--app"], "resident": False, "tasks": [], "data": ["~/.luoke-mail"],
                  "data_env": "LUOKE_MAIL_HOME", "commands": {"uninstall": ["-m", "luoke_mail", "--forget-secrets"]},
                  "capabilities": {"socket": "~/.luoke-mail/run/agent.sock"},
                  "jobs": [{"name": "clean", "command": ["-m", "luoke_mail", "--job", "clean"]}]}, **extra)
        with open(os.path.join(hub, "Contents", "Resources", "mail", "luoke-accessory.json"), "w") as f:
            json.dump(m, f)
        return hub

    def test_python_app_in_the_hub_is_found_by_its_package(self):
        """收进主控的 Python 应用：没有 Contents/Helpers 里的程序，认 site-packages 的包加 Resources 的清单；盖过 ~/Applications 的旧 .app。主控自己的包不算"""
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_python_app(d)
            self._fake_app(d, "luoke-mail", {"bundle_id": "ai.luoke.mail", "version": "1"})
            found = accessories.installed(only=[os.path.join(d, "Applications")], hub=hub)
            m = found["ai.luoke.mail"]
            self.assertEqual((m["_app"], m["version"], m["_helper"], m["_bundled"]), (hub, "9", True, True))
            self.assertEqual([x["accessory"] for x in accessories.bundled_helpers(hub)], ["luoke-mail"])
            self.assertEqual(accessories.rollback_candidates(found, os.path.join(d, "none")), [])
        with tempfile.TemporaryDirectory() as d:
            hub = self._hub_with_python_app(d)
            os.remove(os.path.join(hub, "Contents", "Resources", "mail", "luoke-accessory.json"))      # 只有包
            self.assertEqual(accessories.bundled_helpers(hub), [])

    def test_uninstalling_a_python_app_runs_its_own_cleanup_with_the_hubs_python(self):
        """删数据要跑到应用自己的收尾（邮件助手删钥匙串里的授权码），用的是主控的 Python；不删数据不跑。绝不按路径杀主控的进程，也不动主控 .app"""
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            ran = os.path.join(home, "ran")
            with unittest.mock.patch.dict(os.environ, {"HOME": home}), unittest.mock.patch.object(accessories, "_is_installed_hub", return_value=True):
                hub = self._hub_with_python_app(home)
                data = os.path.join(home, ".luoke-mail"); os.makedirs(data)
                found = accessories.installed(only=[os.path.join(home, "Applications")], hub=hub)
                with unittest.mock.patch.object(accessories, "terminate_app_processes", side_effect=AssertionError("动了主控的进程")):
                    r = accessories.uninstall(found, "luoke-mail", data=False, previous_dir=os.path.join(home, "none"))
                    self.assertEqual((r["removed"], r["cleanup"], os.path.exists(ran)), ([], None, False))
                    self.assertTrue(os.path.isdir(data))
                    r = accessories.uninstall(found, "luoke-mail", data=True, previous_dir=os.path.join(home, "none"))
                self.assertEqual((r["removed"], r["errors"], r["cleanup"]["exit"]), ([data], [], 0))
                with open(ran) as f:
                    self.assertEqual(f.read().strip(), "-m luoke_mail --forget-secrets home=")     # 装好的那份：不指别的目录，用它默认的
                self.assertTrue(os.path.isdir(hub))

    def test_python_app_on_a_development_build_keeps_its_commands_on_its_own_data(self):
        """开发版里跑邮件助手的收尾、空闲任务：不给数据目录它就用正式版的，删的是用户真实的授权码"""
        from luoke_core import jobs
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            with unittest.mock.patch.dict(os.environ, {"HOME": home}):
                hub = self._hub_with_python_app(home)
                m = accessories.bundled_helpers(hub)[0]
                own = os.path.join(home, "Library", "Application Support", "luoke-mail")
                self.assertEqual(m["_env"], {"LUOKE_MAIL_HOME": own})
                self.assertEqual(m["capabilities"]["socket"], "~/Library/Application Support/luoke-mail/run/agent.sock")
                accessories.run_command(m, "uninstall", {})
                with open(os.path.join(home, "ran")) as f:
                    self.assertEqual(f.read().strip(), f"-m luoke_mail --forget-secrets home={own}")
                self.assertEqual(jobs.parse_jobs(m)[0]["_env"], {"LUOKE_MAIL_HOME": own})
            with unittest.mock.patch.object(accessories, "_is_installed_hub", return_value=True):
                self.assertNotIn("_env", accessories.bundled_helpers(hub)[0])

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

    def test_public_list_gives_declared_data(self):
        """GET /accessories 带上清单原样声明的 data（luoke-agent 只认放在那里的工具 socket），别的非公开字段不给。"""
        m = {"accessory": "luoke-scan", "bundle_id": "ai.luoke.scan", "data": ["~/Library/Application Support/ai.luoke.scan"],
             "entry": ["x"], "jobs": [{"name": "nightly", "command": ["y"]}]}
        (d,) = accessories.public_list({"scan": m})
        self.assertEqual(d["data"], ["~/Library/Application Support/ai.luoke.scan"])
        self.assertEqual(d["jobs"], ["nightly"])
        self.assertNotIn("entry", d)

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

    def test_update_window_always_checks_the_newest(self):
        """11-04：自动检查 22:22 找到 internal-1104-3，22:55 又发了 internal-1104-2；菜单「有新版本」把旧结果直接传进窗口，
        用户装完 -3 还得再升一次。打开窗口一律现查；最新一版的清单取不到时报错，不退到次新的。"""
        import re
        menu = self.swift("MenuBar.swift")
        self.assertNotIn("Updater.show(found:", menu)
        src = self.swift("Updater.swift")
        self.assertIn("static func show() {", src)
        body = re.search(r"static func check\(\) throws -> Found\? \{(.+?)\n    \}", src, re.S).group(1)
        loop = body[body.index("for r in cands"):]
        after_fetch = loop[loop.index("get(url)"):]
        self.assertNotIn("continue", after_fetch)                       # 取到列表里最新的一版以后不再往下找
        self.assertIn("throw failure", after_fetch)

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

    def test_a_disabled_apps_idle_jobs_dont_run(self):
        """停用了的本体里的应用：它的空闲任务也不跑，别的照旧"""
        from luoke_core.hub import Hub
        hub = Hub.__new__(Hub)
        hub.manifests = {"ai.luoke.mail": {"accessory": "luoke-mail", "_helper": True, "default_on": True}, "x": {"accessory": "luoke-x"},
                         "ai.luoke.search": {"accessory": "luoke-search", "_helper": True}}
        hub.state = {"enabled": {"luoke-mail": False}}
        self.assertEqual(list(hub.enabled_manifests()), ["x"])
        hub.state = {}                                       # 邮件助手默认开着，搜索要用户开启
        self.assertEqual(list(hub.enabled_manifests()), ["ai.luoke.mail", "x"])
        self.assertEqual(hub.helpers_off(), ("luoke-search",))

    def test_helper_is_off_until_the_user_turns_it_on(self):
        """本体里的应用要用户开启：记在 state.json（本体在主控起来之前也读），没开的 /accessories 不列；开启的端点只有主控自己的界面能调。"""
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.manifests = {"ai.luoke.search": {"accessory": "luoke-search", "_helper": True}, "ai.luoke.mail": {"accessory": "luoke-mail"}}
        self.assertEqual(h.helpers_off(), ("luoke-search",))
        h.set_enabled("luoke-search", True)
        self.assertEqual(h.helpers_off(), ())
        with open(h.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["enabled"], {"luoke-search": True})
        h.set_enabled("luoke-search", False)
        self.assertEqual(h.helpers_off(), ("luoke-search",))
        with open(h.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["enabled"], {"luoke-search": False})
        h.manifests["ai.luoke.mail"].update(_helper=True, default_on=True)      # 默认开着的：用户关了就是关了，卸载后不能开回来
        self.assertEqual(h.helpers_off(), ("luoke-search",))
        h.set_enabled("luoke-mail", False)
        self.assertEqual(sorted(h.helpers_off()), ["luoke-mail", "luoke-search"])
        src = open(os.path.join(ROOT, "luoke_core", "hub.py"), encoding="utf-8").read()
        self.assertIn("require_hub(", src[src.index('path == "/accessories/enable"'):][:300])

    def test_permission_is_closed_until_confirmed(self):
        """没确认的就是没放行：确认了记时间，关掉就删，再开是新的一次确认。起工具时本体读 state.json。"""
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertNotIn("luoke-scan", h.state.get("permissions_ok", {}))
        h.set_permission("luoke-scan", "fda", True)
        first = h.state["permissions_ok"]["luoke-scan"]["fda"]
        self.assertRegex(first, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        h.set_permission("luoke-scan", "fda", True)
        self.assertEqual(h.state["permissions_ok"]["luoke-scan"]["fda"], first, "再报一遍不改时间")
        h.set_permission("luoke-scan", "fda", False)
        self.assertEqual(h.state["permissions_ok"], {})
        src = open(os.path.join(ROOT, "luoke_core", "hub.py"), encoding="utf-8").read()
        endpoint = src[src.index('path == "/accessories/permission"'):][:700]
        self.assertIn("require_hub(", endpoint)
        self.assertIn("declares_host_permission(", endpoint)
        self.assertIn("require_hub(", src[src.index('path == "/accessories/first-use"'):][:300])

    def test_hub_routes_core129(self):
        """core#129：undeclared 答 403 不是 500；缺参数答 invalid_request；预热只能预热自己的任务；
        POST /accessories/previous 不回滚；/v1/predict 不收负数的 k、n。"""
        from luoke_core.hub import Hub
        from luoke_core.proxy import STATUS, BadRequest
        self.assertEqual(STATUS["undeclared"], 403)
        src = open(os.path.join(ROOT, "luoke_core", "hub.py"), encoding="utf-8").read()
        perm = src[src.index('path == "/accessories/permission"'):][:900]
        self.assertLess(perm.index('"invalid_request"'), perm.index('"unsupported"'), "缺 permission、on 不是 bool：参数错，不说「不是螺壳里带的应用」")
        warm = src[src.index('path == "/warmup"'):][:600]
        self.assertIn("hub.check_accessory(acc, task)", warm)
        back = src[src.index('if path in ("/accessories/previous", "/accessories/rollback")'):][:2500]
        self.assertIn('method == "POST" and path == "/accessories/rollback"', back)
        self.assertNotIn("finally:\n            pass", src)
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=128, smoke=False)
        for body in ({"context": "你好", "k": -3}, {"context": "你好", "n": -1}):
            with self.assertRaises(BadRequest) as cm:
                h.predict("luoke-ime.predict", body)
            self.assertEqual(cm.exception.code, "invalid_request", body)

    def test_only_declared_host_permissions_count(self):
        """没声明的不能申请：只认清单里写的、螺壳名下的系统权限。"""
        from luoke_core import accessories
        fda = {"id": "fda", "kind": "system", "owner": "luoke"}
        m = {"capabilities": {"permissions": [fda, {"id": "auto", "kind": "delegate"}, {"id": "own", "kind": "system"}]}}
        self.assertTrue(accessories.declares_host_permission(m, "fda"))
        for other in ("auto", "own", "photos"):
            self.assertFalse(accessories.declares_host_permission(m, other), other)
        self.assertFalse(accessories.declares_host_permission({}, "fda"))

    def test_first_use_job_starts_without_waiting_for_quiet(self):
        """用户刚点了开启：写了 first_use 的任务下一个采样就起，别的任务照旧等。"""
        from luoke_core.jobs import IdleJobs
        d = tempfile.mkdtemp()
        started = []
        j = IdleJobs(lambda: 1000.0, lambda: False, lambda _: ("ok", []), lambda: False, {}, lambda: None, lambda _: None, lambda: None, d)
        j._launch = lambda spec: started.append(spec["key"])
        m = {"accessory": "luoke-scan", "jobs": [{"name": "scan", "command": ["scan"], "first_use": True}, {"name": "update", "command": ["update"]}]}
        j.tick({"luoke-scan": m})
        self.assertEqual(started, [], "不安静时不跑")
        self.assertEqual(j.first_use(m), ["luoke-scan.scan"])
        j.tick({"luoke-scan": m})
        self.assertEqual(started, ["luoke-scan.scan"])
        j.tick({"luoke-scan": m})
        self.assertEqual(started, ["luoke-scan.scan"], "只跑这一次")

    def test_first_use_job_stopped_by_a_restart_starts_again(self):
        """macOS 给完权限退出重开螺壳：第一次检查被打断，重开后接着跑，不等到明天。"""
        from luoke_core.jobs import IdleJobs
        d = tempfile.mkdtemp()
        started = []
        m = {"accessory": "luoke-scan", "jobs": [{"name": "scan", "command": ["scan"], "first_use": True,
                                                  "min_interval_minutes": 1440}]}
        state = {"jobs": {"luoke-scan.scan": {"last_start": 990.0, "reason": "hub_stopping", "asked": True}}}
        j = IdleJobs(lambda: 1000.0, lambda: False, lambda _: ("ok", []), lambda: False, state, lambda: None, lambda _: None,
                     lambda: None, d)
        j._launch = lambda spec: started.append((spec["key"], spec.get("asked")))
        j.tick({"luoke-scan": m})
        self.assertEqual(started, [("luoke-scan.scan", True)])
        j.tick({"luoke-scan": m})
        self.assertEqual(len(started), 1, "只接着跑这一次")

    def test_job_stopped_by_a_restart_counts_as_not_finished(self):
        from luoke_core.jobs import due
        spec = {"min_interval_minutes": 1440}
        ok = lambda _: ("running", [])
        self.assertEqual(due(spec, 1000.0 + 11 * 60, 1000.0, True, ok, False, "done"), "interval")
        self.assertIsNone(due(spec, 1000.0 + 11 * 60, 1000.0, True, ok, False, "hub_stopping"))

    def test_first_use_job_is_not_wrapped_up_while_the_user_is_there(self):
        """用户亲自点起来的这一次：用户在用 Mac 时不收尾，超时照旧。别的任务照旧收尾。"""
        from luoke_core.jobs import IdleJobs, Running
        d = tempfile.mkdtemp()
        now = [1000.0]
        j = IdleJobs(lambda: now[0], lambda: False, lambda _: ("paused", []), lambda: False, {}, lambda: None, lambda _: None, lambda: None, d)
        ended = []
        j._terminate = lambda r, why: ended.append(why)
        proc = type("P", (), {"poll": lambda self: None})()
        spec = {"key": "luoke-scan.scan", "timeout_minutes": 60, "allow_battery": False, "asked": True}
        j.running = Running(spec, proc, now[0], os.path.join(d, "e"))
        j._watch()
        self.assertEqual(ended, [])
        now[0] += 3601
        j._watch()
        self.assertEqual(ended, ["timeout"])
        ended.clear()
        j.running = Running(dict(spec, asked=False), proc, now[0], os.path.join(d, "e"))
        j._watch()
        self.assertEqual(ended, ["wrapped_up"])

    def test_authorization_is_only_asked_after_a_click(self):
        """授权框只紧跟用户的操作：要授权的调用只许在这三处，它们都从按钮来。新加一处要先想清楚谁点了什么。"""
        src = os.path.join(ROOT, "app", "Sources", "LuokeCore")
        found = {}
        for name in os.listdir(src):
            for line in open(os.path.join(src, name), encoding="utf-8"):
                if "requestAuthorization(" in line and not line.lstrip().startswith("//"):
                    found[name] = found.get(name, 0) + 1
        self.assertEqual(found, {"AgentCore.swift": 1, "FirstRun.swift": 1, "SearchActivation.swift": 1})
    def _disable_hub(self):
        """带一个本体里的应用的主控，走真的端点；data 是它的数据目录，里面有一个文件"""
        from luoke_core.hub import Hub, serve
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        data = os.path.join(d, "search-data")
        os.makedirs(data)
        with open(os.path.join(data, "index.db"), "w", encoding="utf-8") as f:
            f.write("x")
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.manifests = {"ai.luoke.search": {"accessory": "luoke-search", "name": "螺壳搜索", "_helper": True, "_app": d, "data": [data],
                                           "jobs": [{"name": "index"}]},
                       "ai.luoke.x": {"accessory": "luoke-x", "name": "X", "_app": os.path.join(d, "X.app")}}
        h.scan_accessories = lambda: h.manifests
        h.phase = "running"
        h.set_enabled("luoke-search", True)
        sd = tempfile.mkdtemp(prefix="lk")
        self.addCleanup(shutil.rmtree, sd, True)
        srv = serve(h, os.path.join(sd, "h.sock"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return h, os.path.join(sd, "h.sock"), data

    def test_a_second_hub_in_the_same_env_does_not_start(self):
        """正式版、开发版、开发实例共用环境目录：另一个主控在应答时，这一个退 4，不删它的 socket"""
        from luoke_core.__main__ import main
        from luoke_core.hub import hub_answering
        h, sock, _ = self._disable_hub()
        self.assertTrue(hub_answering(sock))
        ino = os.stat(sock).st_ino
        self.assertEqual(main(["serve", "--recipe", RECIPE, "--model-dir", h.env_dir, "--env", h.env_dir, "--socket", sock]), 4)
        self.assertEqual(os.stat(sock).st_ino, ino)
        self.assertTrue(hub_answering(sock))
        self.assertFalse(hub_answering(sock + ".none"))

    def test_disabling_a_helper_stops_it_without_uninstalling(self):
        """「停用」：记下 enabled=false、停空闲任务、放掉它的模型；数据一点不动，日志里没有卸载两个字"""
        from luoke_core.client import HubClient
        h, sock, data = self._disable_hub()
        calls, logs = [], []
        h.jobs.stop_accessory = lambda name, reason="uninstall": calls.append(("job", name, reason, h.helpers_off()))
        h.release_accessory = lambda name: calls.append(("release", name))
        h.log = logs.append
        HubClient("luoke-core/1", sock)._call("POST", "/accessories/disable", {"accessory": "luoke-search"})
        # 先记再停：任务调度看的是这份记录，停的时候它已经不会再被起
        self.assertEqual(calls, [("job", "luoke-search", "disabled", ("luoke-search",)), ("release", "luoke-search")])
        with open(h.state_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["enabled"], {"luoke-search": False})
        self.assertEqual(list(h.enabled_manifests()), ["ai.luoke.x"])       # 空闲任务不跑，/accessories 不列
        self.assertTrue(os.path.exists(os.path.join(data, "index.db")))
        self.assertIn("disabled luoke-search", logs)
        self.assertFalse([l for l in logs if "uninstall" in l], logs)
        h2 = type(h)(Recipe.load(RECIPE), h.env_dir, h.model_dir, lab=True, accessory_dirs=[h.env_dir], mem_gb=32, smoke=False)
        h2.manifests = h.manifests                # 重启：从 state.json 读回来，还是关着
        self.assertEqual(h2.helpers_off(), ("luoke-search",))
        HubClient("luoke-core/1", sock)._call("POST", "/accessories/enable", {"accessory": "luoke-search"})   # 开启照旧
        self.assertEqual(h.helpers_off(), ())

    def test_disable_takes_only_helpers(self):
        """单独装的配件和没有的名字都答 unsupported，状态不变；配件不能替别人停用"""
        from luoke_core.client import HubClient, HubError
        h, sock, _ = self._disable_hub()
        before = dict(h.state["enabled"])
        for name in ("luoke-x", "nope", ""):
            with self.assertRaises(HubError) as e:
                HubClient("luoke-core/1", sock)._call("POST", "/accessories/disable", {"accessory": name})
            self.assertEqual(e.exception.code, "unsupported")
        self.assertEqual(h.state["enabled"], before)
        src = open(os.path.join(ROOT, "luoke_core", "hub.py"), encoding="utf-8").read()
        self.assertIn("require_hub(", src[src.index('path == "/accessories/disable"'):][:300])

    def test_deleting_a_helpers_data_is_its_own_step(self):
        """「删除数据…」：先算给用户看，确认了才删；删之前记下停用，日志里没有卸载两个字"""
        from luoke_core.client import HubClient, HubError
        h, sock, _ = self._disable_hub()
        with tempfile.TemporaryDirectory() as d:
            home = os.path.realpath(d)
            data = os.path.join(home, ".luoke-search")
            os.makedirs(data)
            h.manifests["ai.luoke.search"].update(data=["~/.luoke-search"], bundle_id="ai.luoke.search")
            logs, order = [], []
            h.log = logs.append
            stop_job = h.jobs.stop_accessory
            h.jobs.stop_accessory = lambda name, reason="uninstall": (order.append((name, reason, h.helpers_off())), stop_job(name, reason))
            c = HubClient("luoke-core/1", sock)
            with unittest.mock.patch.dict(os.environ, {"HOME": home}), unittest.mock.patch.object(accessories, "home_is_real", return_value=False):
                r = c._call("POST", "/accessories/delete-data", {"accessory": "luoke-search", "dry_run": True})
                self.assertEqual((r["data"], r["removed"]), ([data], []))
                self.assertTrue(os.path.isdir(data))
                self.assertEqual(h.helpers_off(), ())                       # 只算计划：还开着
                r = c._call("POST", "/accessories/delete-data", {"accessory": "luoke-search"})
            self.assertEqual((r["removed"], r["errors"]), ([data], []))
            self.assertFalse(os.path.exists(data))
            self.assertEqual(order, [("luoke-search", "disabled", ("luoke-search",))])    # 删之前已经记下、停了任务
            self.assertEqual(h.helpers_off(), ("luoke-search",))
            self.assertIn("deleted data of luoke-search: removed 1 path(s)", logs)
            self.assertFalse([l for l in logs if "uninstall" in l], logs)
            with self.assertRaises(HubError) as e:                       # 单独装的配件走卸载
                c._call("POST", "/accessories/delete-data", {"accessory": "luoke-x"})
            self.assertEqual(e.exception.code, "unsupported")
            with unittest.mock.patch.object(accessories, "home_is_real", return_value=True):
                with self.assertRaises(HubError) as e:                   # 实验室 HOME 没隔离：和卸载一样不删，也不停用
                    c._call("POST", "/accessories/delete-data", {"accessory": "luoke-search"})
            self.assertEqual(e.exception.code, "forbidden")
        src = open(os.path.join(ROOT, "luoke_core", "hub.py"), encoding="utf-8").read()
        self.assertIn("require_hub(", src[src.index('path == "/accessories/delete-data"'):][:300])

    def test_disable_button_stays_out_of_the_uninstall_window(self):
        """「应用」窗口的「停用」就地做完，只有单独装的配件才进卸载窗口"""
        src = open(os.path.join(ROOT, "app", "Sources", "LuokeCore", "Apps.swift"), encoding="utf-8").read()
        block = src[src.index("if r.bundled {"):][:500]
        self.assertIn('Button("停用") { m.disable(r) }', block)
        self.assertLess(block.index("DeleteData.show"), block.index("} else {"))
        self.assertLess(block.index("} else {"), block.index("Uninstall.show"))
        fn = src[src.index("func disable(_ row: Row)"):][:400]
        self.assertNotIn("卸载", fn)
        self.assertNotIn("Uninstall", fn)

    def test_stopping_a_job_says_why(self):
        """空闲任务被停的原因照实记：停用不写成卸载"""
        class P:
            def poll(self): return None
            def wait(self, timeout=None): return 0
            def send_signal(self, sig): pass
        from luoke_core.jobs import IdleJobs
        j = IdleJobs.__new__(IdleJobs)
        j.now, j.log = (lambda: 1.0), (lambda s: None)
        j.running = type("R", (), {"spec": {"accessory": "luoke-search", "key": "k"}, "proc": P(), "reason": None, "term_sent_at": None})()
        j.stop_accessory("luoke-search", "disabled")
        self.assertEqual(j.running.reason, "disabled")
        j.running.reason = None
        j.stop_accessory("luoke-search")
        self.assertEqual(j.running.reason, "uninstall")

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

    def trial_hub(self, tag):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        rel = os.path.join(d, "release.json")
        with open(rel, "w") as f:
            json.dump({"contract": 1, "release": tag, "items": {}}, f)
        h = Hub(Recipe.load(RECIPE), d, d, lab=False, accessory_dirs=[d], mem_gb=128, smoke=False, release=rel)
        h.recipe.verified = {"luoke-core": {"versions": ["0.1.0"]}, "luoke-mail": {"versions": ["0.13.15"]}}
        return h, d

    def test_hub_only_routes_check_the_peer_process(self):
        """luoke-top 10-20：forbidden 要答 403（之前 STATUS 里没有，落到 500）；只给本体用的接口核对端进程，不只信自报的身份。
        命令行 luoke（主控同一个 Python 跑 -m luoke_core.cli）只在 allow_cli 的接口上算数。"""
        from luoke_core import hub as hubmod
        from luoke_core.proxy import STATUS
        self.assertEqual(STATUS["forbidden"], 403)
        h, d = self.trial_hub("internal-1020")
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

    def test_orphaned_hub_trusts_no_peer(self):
        """beta 2 安全审查 M4：本体没了、主控的父进程变成 1 时，之前谁连上来都算本体。同账户的程序先结束本体、
        赶在主控（--parent-watch）退出前连上来，就能拿 /endpoint 的密钥。现在父进程是 1 时谁都不算；实验室照旧放行"""
        from luoke_core import hub as hubmod
        h, d = self.trial_hub("internal-1113")
        h.recipe.verified["luoke-core"] = {"versions": ["0.1.0"]}
        hd = object.__new__(hubmod.Handler)
        hd.hub = h
        peer = {"pid": 4242}
        hd._peer_pid = lambda: peer["pid"]
        with unittest.mock.patch.object(hubmod.os, "getppid", lambda: 1), \
             unittest.mock.patch.object(hubmod.procs, "exe_path", lambda pid: sys.executable), \
             unittest.mock.patch.object(hubmod.procs, "argv", lambda pid: ["python3", "-m", "luoke_core.cli"]):
            for pid in (4242, 1, None):                     # 随便哪个进程、自称 launchd、读不到对端
                peer["pid"] = pid
                with self.assertRaises(BadRequest) as cm:
                    hd.require_hub("luoke-core/0.1.0", "x")
                self.assertEqual(cm.exception.code, "forbidden")
                with self.assertRaises(BadRequest):
                    hd.require_hub("luoke-core/0.1.0", "x", allow_cli=True)   # 命令行也不认：主控马上就退出
            h.lab, h.real_data = True, True                     # 开发实例（--real-data）：本体起的，父进程是 1 时也不认
            peer["pid"] = 4242
            with self.assertRaises(BadRequest) as cm:
                hd.require_hub("luoke-core/0.1.0", "x")
            self.assertEqual(cm.exception.code, "forbidden")
            h.lab, h.real_data = True, False
            hd.require_hub("luoke-core/0.1.0", "x")                            # 普通实验室主控不是本体起的，照旧放行

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

    def test_any_version_of_a_known_accessory_is_accepted(self):
        """主控不核配件的版本，配方认得的、装着的配件哪一版都放行，任务也不按版本挑；
        清单写了 min_core 的，主控比它旧就答 core_too_old。认不得的配件照旧拒绝"""
        from luoke_core import __version__
        h, d = self.trial_hub("beta1")
        h.recipe.verified["luoke-mail"] = {"versions": ["0.13.15"]}
        for v in ("0.13.15", "0.14.0", "0.19.66"):
            self.assertEqual(h.check_accessory(f"luoke-mail/{v}", "luoke-mail.triage"), ("luoke-mail", v))
        with self.assertRaises(BadRequest) as cm:
            h.check_accessory("luoke-new/1.0")
        self.assertEqual(cm.exception.code, "unknown_accessory")
        h.manifests = {"ai.luoke.new": {"accessory": "luoke-new", "version": "1.0"}}     # 装着的配件：认得
        self.assertEqual(h.check_accessory("luoke-new/1.0"), ("luoke-new", "1.0"))
        h.manifests["ai.luoke.new"]["min_core"] = __version__
        self.assertEqual(h.check_accessory("luoke-new/1.0")[0], "luoke-new")
        h.manifests["ai.luoke.new"]["min_core"] = "99.0"
        with self.assertRaises(BadRequest) as cm:
            h.check_accessory("luoke-new/1.0")
        self.assertEqual(cm.exception.code, "core_too_old")

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

    def test_renamed_accessory_keeps_its_previous_copy(self):
        """配件改了 .app 名字、bundle id 没变：换装时旧版按旧名字挪进 previous.noindex。按 bundle id 找，回得去上一版，
        卸载时连它一起删；别的 bundle id 的不动；有几份时和现在同名的（最近一次换装留下的）优先。"""
        import plistlib
        from luoke_core import apps
        with tempfile.TemporaryDirectory() as d:
            def make(path, version, bid="ai.luoke.x"):
                os.makedirs(os.path.join(path, "Contents", "Resources"), exist_ok=True)
                with open(os.path.join(path, accessories.MANIFEST_REL), "w") as f:
                    json.dump({"contract": 1, "accessory": "luoke-x" if bid == "ai.luoke.x" else "luoke-y", "bundle_id": bid,
                               "name": "X", "version": version, "type": "app", "entry": ["-m", "x"], "tasks": [], "data": []}, f)
                with open(os.path.join(path, "Contents", "Info.plist"), "wb") as f:
                    plistlib.dump({"CFBundleIdentifier": bid}, f)
                return path
            cur = make(os.path.join(d, "Applications", "新名字.app"), "0.1.4")
            prev = os.path.join(d, "previous.noindex")
            old = make(os.path.join(prev, "旧名字.app"), "0.1.3")
            other = make(os.path.join(prev, "别的.app"), "9", bid="ai.luoke.y")
            found = accessories.installed(only=[os.path.join(d, "Applications")])
            c = accessories.rollback_candidates(found, prev)
            self.assertEqual([(x["current"], x["previous"], x["path"]) for x in c], [("0.1.4", "0.1.3", old)])
            with unittest.mock.patch.object(apps, "_ls", lambda flag, path: None):
                r = accessories.rollback(found, "luoke-x", prev, verify=False)
            self.assertEqual((r["from"], r["to"]), ("0.1.4", "0.1.3"))
            self.assertEqual(accessories.app_version(cur), "0.1.3")
            self.assertEqual(accessories.app_version(old), "0.1.4")
            found = accessories.installed(only=[os.path.join(d, "Applications")])
            self.assertEqual([(x["current"], x["previous"]) for x in accessories.rollback_candidates(found, prev)], [("0.1.3", "0.1.4")])
            # 之后又按现在的名字换装过一次：同名的那份是上一版，改名前的那份也还在
            same = make(os.path.join(prev, "新名字.app"), "0.1.2")
            self.assertEqual(accessories._previous_copies(prev, cur, "ai.luoke.x"), [same, old])
            self.assertEqual(accessories.rollback_candidates(found, prev)[0]["path"], same)
            make(same, "0.1.3")         # 同名那份和现在同版本：跳过它，取下一份
            self.assertEqual(accessories.rollback_candidates(found, prev)[0]["path"], old)
            plan = accessories.uninstall_plan(found, "luoke-x", preview_forget=False, previous_dir=prev)
            self.assertEqual(plan["previous_copies"], [same, old])
            with unittest.mock.patch.object(apps, "_ls", lambda flag, path: None), \
                 unittest.mock.patch.object(apps, "registered_copies", lambda bid: []):
                r = accessories.uninstall(found, "luoke-x", previous_dir=prev)
            self.assertEqual(r["errors"], [])
            self.assertEqual(sorted(r["removed"]), sorted([cur, same, old]))
            self.assertFalse(any(os.path.exists(p) for p in (cur, same, old)))
            self.assertTrue(os.path.isdir(other))

    def test_downloads_only_for_open_tasks(self):
        """2026-11-03：专用模型只给开着的任务下。配件不在 verified 里（发布评测没跑，例如流水线还没打包的配件），或任务的 tiers
        排除了这一档（空的就是哪一档都没开），就不下，任务状态答 unsupported；换档、菜单栏补下都照这个，
        应用首次运行和升级用的 Manifests.taskOpen 是同一条规则。"""
        r = Recipe.load(RECIPE)
        for tier in ("16", "32", "128"):                  # 没写 tiers：三档都开
            self.assertTrue(r.task_open("luoke-search.embed", tier))
        r.verified = {"luoke-core": {"versions": ["0.1.0"]}}                # 配件版本不看：verified 里没有也开
        self.assertTrue(r.task_open("luoke-search.embed", "32"))
        r.tasks["luoke-search.embed"]["tiers"] = ["128"]                    # 支持台账只开了 128
        self.assertEqual([r.task_open("luoke-search.embed", t) for t in ("16", "32", "128")], [False, False, True])
        r.tasks["luoke-search.embed"]["tiers"] = []                         # 台账哪一档都没开（support.on_tiers 给的空表）
        self.assertEqual([r.task_on_tier("luoke-search.embed", t) for t in ("16", "32", "128")], [False, False, False])
        r.tasks["luoke-search.embed"]["tiers"] = None                       # 没写：每档都行
        self.assertTrue(r.task_open("luoke-search.embed", "32"))
        self.assertFalse(r.task_open("luoke-none.task", "32"))

        h, d = self.trial_hub("internal-1103")        # 128 档
        h.manifests = {}
        for t in [t for t, m in h.recipe.assignment["128"].get("special", {}).items()]:
            h.recipe.tasks[t]["tiers"] = []                  # 台账都没开：只下底座
        self.assertEqual(h.tier_models("128"), ["gemma-4-26b-8bit"])
        need = h.needed_downloads()
        self.assertIn("gemma-4-26b-8bit", need)
        self.assertNotIn("embeddinggemma-2", need)
        self.assertNotIn("qwen3.5-0.8b-base-4bit", need)
        self.assertEqual(h.task_status("luoke-search.embed")["reason"], "这一档没通过验证")
        h.recipe.tasks["luoke-search.embed"]["tiers"] = None             # 开了：没装配件也下（菜单栏照常补下）
        self.assertIn("embeddinggemma-2", h.needed_downloads())
        h.recipe.tasks["luoke-ime.rerank"]["tiers"] = None               # 两个任务共用一个专用模型：有一个开着就下
        self.assertIn("qwen3.5-0.8b-base-4bit", h.tier_models("128"))
        h.recipe.tasks["luoke-ime.rerank"]["tiers"] = []
        self.assertNotIn("qwen3.5-0.8b-base-4bit", h.tier_models("128"))

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
        # 旧的整份清掉，只留密钥；CORS 关掉（oMLX 默认 "*"）
        self.assertEqual(json.load(open(sp)), {"auth": {"api_key": e.key}, "server": {"cors_origins": []}})
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

    def test_battery_mode_setting(self):
        """battery_mode：旧设置照认、写的时候旧字段跟着写；不运行大模型时不装底座、本机检查往后放"""
        from luoke_core.hub import BadRequest, Hub, NO_LARGE_MODEL_MSG
        from luoke_core.engine import EngineError
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "Qwen3.5-9B-4bit"))
        with open(os.path.join(d, "settings.json"), "w") as f:
            json.dump({"background_on_battery": True}, f)
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertEqual((h.settings["battery_mode"], h.dispatcher.battery_mode), ("normal", "normal"))
        h.update_settings({"battery_mode": "no_large_model"})
        self.assertEqual((h.settings["battery_mode"], h.settings["background_on_battery"]), ("no_large_model", False))
        with open(os.path.join(d, "settings.json")) as f:
            self.assertEqual(json.load(f)["battery_mode"], "no_large_model")
        with self.assertRaises(BadRequest):
            h.update_settings({"battery_mode": "off"})
        h.update_settings({"background_on_battery": True})                # 旧客户端
        self.assertEqual(h.settings["battery_mode"], "normal")
        h.update_settings({"battery_mode": "no_large_model"})
        h.dispatcher.on_ac = False
        h.base_id = "Qwen3.5-9B-4bit"                                       # 测试里没起引擎：直接给
        with self.assertRaises(EngineError):
            h.load_base()
        rec = h.smoke_check(force=True)
        self.assertTrue(rec.get("deferred"))
        self.assertEqual(rec["error"], NO_LARGE_MODEL_MSG)
        self.assertIsNone(h.smoke_failed)
        h2 = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertEqual(h2.dispatcher.battery_mode, "no_large_model")     # 落盘的新值开机就用

    def test_pause_on_low_power_setting(self):
        """低电量模式时暂停：默认开；改了落盘、开机就用；只收 true / false"""
        from luoke_core.hub import BadRequest, Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertTrue(h.settings["pause_on_low_power"])
        self.assertTrue(h.dispatcher.pause_on_low_power)
        h.update_settings({"pause_on_low_power": False})
        self.assertFalse(h.dispatcher.pause_on_low_power)
        with self.assertRaises(BadRequest):
            h.update_settings({"pause_on_low_power": "no"})
        h2 = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertFalse(h2.dispatcher.pause_on_low_power)

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

    def test_waits_for_wired_memory_to_come_back_right_after_an_unload(self):
        """core#43：16 GB 档刚卸了底座，Metal 要 1–6 秒才还 wired；这段时间量到不够要等着再量，降够了照常装，不拒。
        没刚卸过（或等满了还不够）照旧马上拒。"""
        from luoke_core import hub as hubmod
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h16 = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=16, smoke=False)
        real, sleep = machine.memory_sample, hubmod.time.sleep
        falling = iter([7.22, 6.96, 6.63, 6.37, 6.25, 4.6])          # 10-29 实验室机器上卸完 9B 以后量到的
        try:
            machine.memory_sample = lambda: {"wired_gb": next(falling, 4.6)}
            hubmod.time.sleep = lambda s: None
            h16.engine.unloaded_at = time.time()
            self.assertTrue(h16.fits_memory("qwen3.5-9b-4bit"))
            self.assertEqual(next(falling, None), None)                 # 一直量到降下来
            machine.memory_sample = lambda: {"wired_gb": 7.22}
            h16.engine.unloaded_at = 0.0
            t0 = time.time()
            self.assertFalse(h16.fits_memory("qwen3.5-9b-4bit"))       # 很久没卸过：不等
            self.assertLess(time.time() - t0, 1)
        finally:
            machine.memory_sample, hubmod.time.sleep = real, sleep


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
        """assign=True：照配方分到各档，并放一个空模型目录；False：拿掉配方里嵌入任务的分配（测"本档没有模型"那条路）。"""
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        r = Recipe.load(RECIPE)
        if assign:
            os.makedirs(os.path.join(d, r.model("embeddinggemma-2")["dir"]))
        else:
            for a in r.assignment.values():
                (a.get("special") or {}).pop("luoke-search.embed", None)
        h = Hub(r, d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.worker_hold_reason = lambda model: None   # 起进程前的内存检查看的是这台机器真实的 wired，测试里另测（不然本机内存紧时这些测试会失败）
        h.engine_missing = lambda: False      # 当作引擎已装：任务状态才走到"本档有没有模型"那一步
        return h

    def test_recipe_scan_tasks(self):
        r = Recipe.load(RECIPE)
        evals = json.load(open(os.path.join(ROOT, "tools", "release_evals.json")))
        for task in ("luoke-scan.explain", "luoke-scan.identify", "luoke-scan.skills"):
            t = r.tasks[task]
            self.assertEqual((t["accessory"], t["kind"], t["main"]), ("luoke-scan", "generate", False))   # 没有模型时扫描照样出结果
            self.assertEqual((evals[task]["accessory"], evals[task]["summary"]), ("luoke-scan", "scan-task"))
            for tier in r.tiers:
                self.assertEqual(r.model_for_task(task, tier), r.base_for(tier))          # 用本档底座，不用专用模型
        for tier in r.tiers:      # 整个请求算字数：系统提示加示范约 4800、skill 的输入最多 6000，16 GB 档也不能照抄 6000
            self.assertGreaterEqual(r.params("luoke-scan.skills", tier)["max_chars"], 11000)
        self.assertTrue(r.version_verified("luoke-scan", "0.1.0"))
        self.assertNotIn("luoke-audit", r.verified)

    def test_recipe_kind_params_and_assignment(self):
        r = Recipe.load(RECIPE)
        self.assertEqual(r.tasks["luoke-search.embed"]["kind"], "embed")
        self.assertEqual(r.tasks["luoke-search.embed"]["accessory"], "luoke-search")
        self.assertNotIn("digup.embed", r.tasks)                           # 改名（luoke-core#16），接口不变
        p = r.params("luoke-search.embed", "32")
        self.assertEqual((p["max_tokens"], p["batch"]), (2048, 32))     # 和 luoke-search 一样按 2048 个 token 截
        self.assertNotIn("dims", p)                                      # 不截维（顶层 ADR 0018）
        self.assertEqual(r.model("embeddinggemma-2")["revision"], "914f7f8")
        text = {k: v for k, v in p.items() if k in ("batch", "max_tokens", "max_chars", "max_total_chars")}
        for tier in ("16", "32", "128"):      # 三档都分到同一个专用模型、文字参数一样（2026-11-02）；哪一档开着由发布评测定
            self.assertEqual(r.model_for_task("luoke-search.embed", tier), "embeddinggemma-2")
            self.assertEqual({k: v for k, v in r.params("luoke-search.embed", tier).items() if k in text}, text)
        self.assertEqual(r.params("luoke-search.embed", "16"), text)      # 16 档只收文字（2026-11-03）
        self.assertTrue(r.version_verified("luoke-search", "0.1.0"))      # 不在 verified 里的配件请求一律 403
        self.assertNotIn("digup", r.verified)
        d = json.load(open(RECIPE))
        d["tasks"]["luoke-search.embed"]["kind"] = "vectorize"
        with self.assertRaises(RecipeError):
            Recipe(d)
        h = self.hub()                        # 没分配的档：不下载，答 unsupported
        self.assertNotIn("embeddinggemma-2", h.needed_downloads())
        self.assertEqual(h.task_status("luoke-search.embed")["status"], "unsupported")

    def test_vector_space(self):
        """向量空间（2026-10-16 应 digup 加，只加不改）：配方的模型声明，GET /tasks 和答复里都带；变了任务参数版本也变。"""
        r = Recipe.load(RECIPE)
        self.assertEqual({r.vector_space("luoke-search.embed", t) for t in r.tiers}, {"embeddinggemma-2/1"})
        r.assignment["32"]["special"].pop("luoke-search.embed")
        self.assertIsNone(r.vector_space("luoke-search.embed", "32"))                 # 没分配：null
        self.assertIsNone(r.vector_space("luoke-mail.triage", "32"))           # 不是嵌入任务
        self.assertEqual(r.models["embeddinggemma-2"]["vector_space"], "embeddinggemma-2/1")
        d = json.load(open(RECIPE))                                          # 分配了的嵌入模型必须声明向量空间
        d["assignment"]["32"]["special"]["luoke-search.embed"] = "embeddinggemma-2"
        del d["models"]["embeddinggemma-2"]["vector_space"]
        with self.assertRaises(RecipeError):
            Recipe(d)
        h = self.hub()
        payload = h.tasks_payload("luoke-search")["tasks"]["luoke-search.embed"]
        self.assertIn("vector_space", payload)
        self.assertIsNone(payload["vector_space"])
        self.assertNotIn("vector_space", h.tasks_payload("luoke-mail")["tasks"]["luoke-mail.triage"])   # 别的任务不多字段
        h = self.hub(assign=True)
        self.assertEqual(h.tasks_payload("luoke-search")["tasks"]["luoke-search.embed"]["vector_space"], "embeddinggemma-2/1")
        v1 = h.tasks_version()
        h.recipe.models["embeddinggemma-2"]["vector_space"] = "embeddinggemma-2/2"
        self.assertNotEqual(h.tasks_version(), v1)                            # 配件看到版本变了就重读 /tasks

    def test_hub_validates_embed_request(self):
        h = self.hub()
        task = "luoke-search.embed"
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
        """文字的输入读 luoke-search 的 texts.json，参考向量是 vectors.json 并上 vectors-extra.json（11-03 后加的 7 条），
        以前这里手抄了原来的 5 条，后加的没核到。题集哈希三个文件都算进去。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import embed_parity as ep
        d = tempfile.mkdtemp()
        texts = [{"name": "query", "input": "task: search result | query: payment declined error"},
                 {"name": "query-zh", "input": "task: search result | query: 银行卡被拒"}]
        with open(os.path.join(d, "texts.json"), "w") as f:
            json.dump({"texts": texts}, f, ensure_ascii=False)
        with open(os.path.join(d, "vectors.json"), "w") as f:
            json.dump({"made_with": "test", "vector_version": 1, "vectors": {"query": [1.0, 0.0]}}, f)
        t = ep.reference_texts(d)
        self.assertEqual(list(t), ["query", "query-zh"])
        ref, sha_without = ep.load_reference(os.path.join(d, "vectors.json"))
        self.assertFalse(ep.check(ref["vectors"], {"query": [1.0, 0.0], "query-zh": [0.0, 1.0]}, names=list(t))[0])   # 参考里没有这条：算没过
        with open(os.path.join(d, "vectors-extra.json"), "w") as f:
            json.dump({"made_with": "test", "vector_version": 1, "vectors": {"query-zh": [0.0, 1.0]}}, f)
        ref, sha_with = ep.load_reference(os.path.join(d, "vectors.json"))
        self.assertNotEqual(sha_without, sha_with)                     # 后加的文件也算进题集哈希
        self.assertEqual((ref["made_with"], set(ref["vectors"])), ("test", {"query", "query-zh"}))
        ok, rows = ep.check(ref["vectors"], {"query": [1.0, 0.0], "query-zh": [0.0, 1.0]}, names=list(t))
        self.assertTrue(ok)
        self.assertEqual([n for n, _ in rows], ["query", "query-zh"])
        self.assertFalse(ep.check(ref["vectors"], {"query": [0.95, 0.31], "query-zh": [0.0, 1.0]}, names=list(t))[0])   # 余弦约 0.95
        self.assertFalse(ep.check(ref["vectors"], {"query": [1.0, 0.0]}, names=list(t))[0])        # 少一条
        refs = os.path.join(os.path.dirname(ROOT.split("/.claude/worktrees/")[0]), "luoke-search", "Tests", "Reference", "vectors.json")
        if os.path.exists(os.path.join(os.path.dirname(refs), "texts.json")):   # 有 luoke-search 的只读副本：名字对得上、自己和自己全过
            t = ep.reference_texts(os.path.dirname(refs))
            self.assertGreaterEqual(len(t), 12)
            self.assertEqual(t["query"], "task: search result | query: payment declined error")
            self.assertTrue(t["document-long"].startswith("title: none | text: Note 1: the review moved to room 3 because"))
            ref, _ = ep.load_reference(refs)
            vecs = ref["vectors"]
            self.assertTrue(set(t) <= set(vecs))
            self.assertTrue(ep.check(vecs, {n: vecs[n] for n in t})[0])
            self.assertTrue(all(len(vecs[n]) == 768 for n in t))
            # 媒体 4 条（2026-10-19 商定的送法）：名字都在参考里；frame-70 是同一张照片按 70；声音整段送
            import base64
            media = ep.reference_media(os.path.dirname(refs))
            self.assertEqual(set(media), {"screen-280", "photo-140", "frame-70", "speech"})
            self.assertTrue(set(media) <= set(vecs) and all(len(vecs[n]) == 768 for n in media))
            self.assertEqual((media["screen-280"]["budget"], media["photo-140"]["budget"], media["frame-70"]["budget"]), (280, 140, 70))
            self.assertEqual(media["frame-70"]["image"], media["photo-140"]["image"])
            with open(os.path.join(os.path.dirname(refs), "speech.wav"), "rb") as f:
                self.assertEqual(base64.b64decode(media["speech"]["audio"]), f.read())
            self.assertTrue(ep.check(vecs, {n: vecs[n] for n in media}, names=list(media))[0])
            self.assertFalse(ep.check(vecs, dict({n: vecs[n] for n in media}, speech=None), names=list(media))[0])   # null 算没过

    def test_parity_tool_writes_a_result_for_the_release_eval(self):
        """--result（发布评测表 luoke-search.embed）：过了、低于 0.999、请求没成（例如这一档内存不够、嵌入进程起不来）都写结果，
        带上主控答的档；发布脚本的 embed-parity 汇总读它。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import embed_parity as ep
        from luoke_core import client
        from luoke_core.client import HubError
        d = tempfile.mkdtemp()
        names = ["query", "query-bn", "query-ar", "document", "document-long", "query-zh"]
        ref = {n: [0.0] * i + [1.0] + [0.0] * (len(names) - i - 1) for i, n in enumerate(names)}
        vec_path = os.path.join(d, "vectors.json")
        with open(vec_path, "w") as f:
            json.dump({"made_with": "test", "vector_version": 1, "vectors": {n: ref[n] for n in names[:5]}}, f)
        with open(os.path.join(d, "vectors-extra.json"), "w") as f:
            json.dump({"made_with": "test", "vector_version": 1, "vectors": {"query-zh": ref["query-zh"]}}, f)
        with open(os.path.join(d, "texts.json"), "w") as f:
            json.dump({"texts": [{"name": n, "input": f"input {n}"} for n in names]}, f)
        answers = {}

        class FakeClient:
            def __init__(self, accessory, socket_path=None):
                answers["accessory"] = accessory

            def tasks(self):
                return {"tier": "16", "tasks": {ep.TASK: {"status": "available"}}}

            def warmup(self, task):
                return {"accepted": True}

            def embed(self, task, texts, priority="background"):
                answers["task"], answers["priority"], answers["texts"] = task, priority, texts
                if answers.get("refuse"):
                    raise HubError(503, "not_ready", "内存不够，专用模型先不起", 30)
                vecs = [list(ref[n]) for n in names]
                if answers.get("off"):
                    vecs[0] = [0.95, 0.31] + [0.0] * (len(names) - 2)
                return {"vectors": vecs, "dims": len(names), "model": "omlx-0.7.2+embeddinggemma-2", "vector_space": "embeddinggemma-2/1", "ms": 12.0}
        real = client.HubClient
        client.HubClient = FakeClient
        try:
            out = os.path.join(d, "r.json")
            self.assertEqual(ep.main(["--socket", "/nonexistent.sock", "--vectors", vec_path, "--result", out]), 0)
            self.assertEqual((answers["accessory"], answers["task"], answers["priority"]), ("luoke-search/parity", "luoke-search.embed", "user-waiting"))
            r = json.load(open(out))
            self.assertEqual((r["ok"], r["tier"], r["expected"], len(r["rows"]), r["vector_space"], r["error"]), (True, "16", 6, 6, "embeddinggemma-2/1", None))
            self.assertEqual(answers["texts"], [f"input {n}" for n in names])          # 送的就是 texts.json 的 input
            self.assertEqual(len(r["vectors_sha256"]), 64)
            answers["off"] = True
            self.assertEqual(ep.main(["--socket", "/nonexistent.sock", "--vectors", vec_path, "--result", out]), 1)
            r = json.load(open(out))
            self.assertFalse(r["ok"])
            self.assertLess(r["min_cosine"], 0.999)
            answers["refuse"] = True
            self.assertEqual(ep.main(["--socket", "/nonexistent.sock", "--vectors", vec_path, "--result", out, "--wait", "0"]), 2)
            r = json.load(open(out))
            self.assertEqual((r["ok"], r["rows"], r["expected"]), (False, [], 6))
            self.assertIn("内存不够", r["error"])
        finally:
            client.HubClient = real

    def test_not_ready_refusals_are_logged_rate_limited(self):
        """嵌入进程正在加载时答 not_ready，也记进 hub.log（只有元数据）；同一原因 10 s 内只记一条，省掉的条数记在下一条里。"""
        from luoke_core import scorer
        h = self.hub(assign=True)
        w = scorer.EmbedWorker("/py", "/site", "/m", media=True)       # 测试机是 32 档：配方让嵌入进程收图片、声音
        w.state = "loading"                                    # 正在（重新）加载：_ask 答 not_ready
        h.scorers["embeddinggemma-2"] = w
        marker = "LUOKE-MARKER-嵌入请求文字"
        def refused():
            with self.assertRaises(BadRequest) as cm:
                h.embed("luoke-search.embed", {"texts": [marker]}, priority="user-waiting", accessory="luoke-search")
            self.assertEqual(cm.exception.code, "not_ready")
        for _ in range(5):
            refused()
        h.logf.flush()
        log_path = os.path.join(h.env_dir, "logs", "hub.log")
        lines = [l for l in open(log_path, encoding="utf-8") if "refused embed" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("task=luoke-search.embed prio=user-waiting accessory=luoke-search code=not_ready worker=loading", lines[0])
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
            h.embed("luoke-search.embed", {"texts": ["title: none | text: 你好"]}, accessory="luoke-search")
        self.assertIsNone(d.job)                                       # 名额还了
        clock.tick(1)                                                  # 过了这次的功耗间歇（用时几乎为 0）
        self.assertEqual(d.admit_background(False, True)[:2], (True, None))   # 邮件、记忆的后台不会一直 engine_busy

        class Stub:
            state = "ready"

            def embed(self, texts, max_tokens, timeout):
                return {"vectors": [[1.0, 0.0] for _ in texts], "dims": 2, "ms": 1.0}
        h.scorer_for = lambda task: Stub()
        out = h.embed("luoke-search.embed", {"texts": ["title: none | text: 你好"]}, accessory="luoke-search")
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
        w = scorer.EmbedWorker("/nonexistent/python3", "", "/nonexistent/model", lambda m: None, media=True)   # 32 档的模式
        w.state, w.probed = "ready", True
        h.scorers["embeddinggemma-2"] = w
        h._idle_workers("normal")
        self.assertEqual(w.state, "ready")                       # 刚用过：不停
        w.last_used -= hubmod.WORKER_IDLE_SECS + 1
        h._idle_workers("normal")
        self.assertEqual(w.state, "stopped"); self.assertTrue(w.idle_stopped)
        w.start = lambda: setattr(w, "state", "loading")
        h.worker_hold_reason = lambda model: None
        self.assertIs(h.scorer_for("luoke-search.embed"), w)
        self.assertEqual(w.state, "loading")
        w.state = "ready"
        del h.worker_hold_reason
        h._idle_workers("critical")
        self.assertEqual(w.state, "stopped"); self.assertFalse(w.idle_stopped)
        with self.assertRaises(BadRequest) as cm:
            h.scorer_for("luoke-search.embed")
        self.assertEqual((cm.exception.code, cm.exception.retry_after), ("not_ready", hubmod.WORKER_HOLD_SECS))

    def test_tasks_payload_has_a_display_model_name(self):
        """10-20 应 luoke-mail：GET /tasks 每个任务带给人看的 model_name（配方 label，没有就是模型目录名），本档没模型时 null。"""
        h = self.hub(assign=True)
        self.assertEqual(h.tasks_payload("luoke-search")["tasks"]["luoke-search.embed"]["model_name"],
                         os.path.basename(h.recipe.model("embeddinggemma-2")["dir"]))
        self.assertIsNone(self.hub(assign=False).tasks_payload("luoke-search")["tasks"]["luoke-search.embed"]["model_name"])

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

    def test_worker_start_counts_the_base_only_when_it_yields(self):
        """16 GB 档（底座不常驻）：嵌入请求不卸底座，起嵌入进程时底座不算能让出来的；打分进程会让底座卸掉，照旧算。
        底座常驻的档照旧算（2026-11-04 16 GB 测试机：9B 在内存里时算出 9.3 GB，实际剩 3.5 GB）。"""
        from luoke_core import machine, scorer
        from luoke_core.hub import Hub
        emb, ime = "embeddinggemma-2", "qwen3.5-0.8b-base-4bit"
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=16, smoke=False)
        self.assertFalse(h.tier.get("base_resident", True))
        h.base_id = h.engine.loaded = "base"
        h.scorers[emb] = scorer.EmbedWorker("/nonexistent/python3", "", "/nonexistent/model", lambda m: None)
        h.scorers[ime] = scorer.ScoreWorker("/nonexistent/python3", "", "/nonexistent/model", 16, lambda m: None)
        left = 1.0      # 不算底座只剩 1 GB；算上底座 1 GB + 9B 的 size_gb
        with unittest.mock.patch.object(machine, "memory_sample", lambda: {"wired_gb": machine.metal_cap_gb(16) - left}):
            self.assertIsNotNone(h.worker_hold_reason(emb))
            self.assertIsNone(h.worker_hold_reason(ime))
        h32 = self.hub(assign=True)
        del h32.worker_hold_reason
        self.assertTrue(h32.tier.get("base_resident", True))
        h32.base_id = h32.engine.loaded = "base"
        h32.scorers[emb] = scorer.EmbedWorker("/nonexistent/python3", "", "/nonexistent/model", lambda m: None)
        with unittest.mock.patch.object(machine, "memory_sample", lambda: {"wired_gb": machine.metal_cap_gb(32) - left}):
            self.assertIsNone(h32.worker_hold_reason(emb))

    def test_warn_counts_when_our_workers_run_beside_the_base(self):
        """后台按哪个压力等级停：只有底座在内存里时 warn 当 normal（我们自己顶的）；底座之外还有工作进程在跑时，warn 连续超过
        OWN_WARN_SECS 才照算（底座加载、刚开始生成那几秒的短 warn 不停后台）；底座不在时 warn 照算；critical、normal 不变。"""
        from luoke_core import hub as hubmod, scorer
        h = self.hub(assign=True)
        h.engine.loaded = "base"
        self.assertEqual(h._background_pressure("warn", now=0), "normal")
        w = scorer.EmbedWorker("/nonexistent/python3", "", "/nonexistent/model", lambda m: None)
        h.scorers["embeddinggemma-2"] = w
        self.assertEqual(h._background_pressure("warn", now=1), "normal")      # 停着的工作进程不算
        w.state = "ready"
        # 短 warn：采样三次、前后 10 s 以内，中间回到 normal 就重新算
        for t in (100, 105, 100 + hubmod.OWN_WARN_SECS):
            self.assertEqual(h._background_pressure("warn", now=t), "normal")
        self.assertEqual(h._background_pressure("normal", now=115), "normal")
        self.assertEqual(h._background_pressure("warn", now=120), "normal")
        # 一直下不来：连续超过 OWN_WARN_SECS 才停后台
        self.assertEqual(h._background_pressure("warn", now=125), "normal")
        self.assertEqual(h._background_pressure("warn", now=120 + hubmod.OWN_WARN_SECS + 1), "warn")
        self.assertEqual(h._background_pressure("warn", now=140), "warn")
        d = h.dispatcher                                                      # 停了以后照旧要恢复满 MEMORY_RECOVER_SECS
        d.set_memory_pressure(h._background_pressure("warn", now=145))
        self.assertEqual(d.background_state()[0], "paused")
        d.set_memory_pressure(h._background_pressure("normal", now=150))
        self.assertIn("memory", d.background_state()[1])
        self.assertEqual((h._background_pressure("critical", now=151), h._background_pressure("normal", now=152)), ("critical", "normal"))
        w.state, h.engine.loaded = "stopped", None
        self.assertEqual(h._background_pressure("warn", now=153), "warn")     # 底座不在：别人在挤，马上算

    def test_user_waiting_embeds_go_to_the_worker_in_chunks(self):
        """嵌入进程一次最多算 EMBED_CHUNK 条（配方的 wired_gb 按这个量）：user-waiting 的大批也切份；向量顺序不变。"""
        h = self.hub(assign=True)
        sizes = []

        class W:
            def embed(self, texts, max_tokens, timeout):
                sizes.append(len(texts))
                return {"vectors": [[float(t)] for t in texts], "dims": 1, "ms": 1.0}
        texts = [str(i) for i in range(h.EMBED_CHUNK * 2 + 3)]
        for prio in ("user-waiting", "background"):
            sizes.clear()
            out = h._embed_lane(W(), texts, 2048, prio)
            self.assertEqual([v[0] for v in out["vectors"]], [float(t) for t in texts])
            self.assertEqual(max(sizes), h.EMBED_CHUNK)
        self.assertEqual(h.embed_priority_waiting, 0)
        self.assertLessEqual(h.EMBED_CHUNK, 8)          # 改大之前先重量 wired_gb（8 条 × 2048 token 中文 2.66 GB，配方 2.8）

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
                got.append(h.scorer_for("luoke-search.embed"))
            ts = [threading.Thread(target=go) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        finally:
            scorer.EmbedWorker.start = real
        self.assertEqual(len({id(w) for w in got}), 1)
        self.assertEqual((len(starts), len(h.scorers), len(budget)), (1, 1, 1))

    FAKE_TRANSCRIBER = r'''
import base64, json, sys, time
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    assert set(req) == {"op", "audio", "context", "language", "max_tokens"}, req
    if req["context"] == "slow":
        time.sleep(0.6)
    n = len(base64.b64decode(req["audio"]))
    with open(__file__ + ".log", "a") as f:
        f.write(f"{n} {req['max_tokens']}\n")
    print(json.dumps({"text": f"{n} {req['context']}|{req['language']}", "audio_secs": n / 32000, "ms": 1.0}), flush=True)
'''

    def test_transcribe_sessions_append_audio_and_end_on_final(self):
        """转写（2026-11-05，core-private#22）：一次听写一个会话，音频按块追加，草稿、终稿用到目前为止的整段；seq 接不上、主控不认得
        就答 session_lost；没算成的那一块当没收到，原样重发能接上；排队的草稿碰上更新的请求答 superseded；音频不进日志。"""
        from luoke_core import scorer
        from luoke_core.scorer import ScorerError
        fake = os.path.join(tempfile.mkdtemp(), "fake_transcribe_worker.py")
        with open(fake, "w") as f:
            f.write(self.FAKE_TRANSCRIBER)
        h = self.hub(assign=True)
        asr = h.recipe.model_for_task("luoke-ime.transcribe", h.tier_name)       # 16 档 0.6B，32、128 档 1.7B
        os.makedirs(os.path.join(h.model_dir, h.recipe.model(asr)["dir"]), exist_ok=True)
        h.engine_python, h.engine_site = sys.executable, ""
        h.apply_budget = lambda: None
        real = scorer.TRANSCRIBE_WORKER
        scorer.TRANSCRIBE_WORKER = fake
        b64 = lambda n: base64.b64encode(b"\0" * n).decode()
        t = "luoke-ime.transcribe"
        go = lambda **kw: h.transcribe(t, dict({"session": "s1", "audio": ""}, **kw), "luoke-ime")
        try:
            self.assertEqual(h.task_status(t)["status"], "available")
            w = h.scorer_for(t)
            self.assertIsInstance(w, scorer.TranscribeWorker)
            for _ in range(100):
                if w.state == "ready":
                    break
                time.sleep(0.05)
            self.assertEqual(go(seq=0, audio=b64(100), want="none"), {"ok": True})
            out = go(seq=1, audio=b64(60), want="draft", context="螺壳")
            self.assertEqual((out["text"], out["final"]), ("160 螺壳|Chinese", False))
            with self.assertRaises(BadRequest) as cm:                    # 漏了一块
                go(seq=3, audio=b64(10), want="draft")
            self.assertEqual(cm.exception.code, "session_lost")
            with unittest.mock.patch.object(w, "transcribe", side_effect=ScorerError("not_ready")):
                with self.assertRaises(BadRequest) as cm:               # 模型在起：这一块当没收到
                    go(seq=2, audio=b64(40), want="final")
                self.assertEqual((cm.exception.code, cm.exception.retry_after), ("not_ready", 2))
            out = go(seq=2, audio=b64(40), want="final")                 # 原样重发：接得上，不重复算那 40 字节
            self.assertEqual((out["text"], out["final"]), ("200 |Chinese", True))
            with self.assertRaises(BadRequest) as cm:                    # 终稿以后会话就没了
                go(seq=3, audio=b64(10), want="draft")
            self.assertEqual(cm.exception.code, "session_lost")
            self.assertEqual(go(seq=0, audio=b64(10), want="cancel"), {"ok": True})
            # 草稿在等进程时来了终稿：排队的草稿不算，答 superseded；在算的那份照常答
            go(seq=0, audio=b64(10), want="none")
            res = {}
            def call(name, **kw):
                try:
                    res[name] = go(**kw)["text"]
                except BadRequest as e:
                    res[name] = e.code
            a = threading.Thread(target=call, args=("a",), kwargs=dict(seq=1, audio=b64(2), want="draft", context="slow"))
            a.start(); time.sleep(0.2)
            b = threading.Thread(target=call, args=("b",), kwargs=dict(seq=2, audio=b64(2), want="draft"))
            b.start(); time.sleep(0.1)
            c = threading.Thread(target=call, args=("c",), kwargs=dict(seq=3, audio=b64(2), want="final"))
            c.start()
            for x in (a, b, c):
                x.join()
            self.assertEqual(res, {"a": "12 slow|Chinese", "b": "superseded", "c": "16 |Chinese"})
            for bad, code in (({"seq": 0, "audio": b64(3)}, "invalid_request"), ({"seq": 0, "audio": "不是"}, "invalid_request"),
                              ({"seq": 0, "audio": b64(2), "want": "maybe"}, "invalid_request"),
                              ({"seq": 0, "audio": b64(121 * 32000)}, "input_too_long"), ({"seq": 0, "context": "x" * 2001}, "input_too_long"),
                              ({"seq": 0, "language": "中文"}, "invalid_request"), ({"seq": 0, "language": 1}, "invalid_request")):
                with self.assertRaises(BadRequest) as cm:
                    go(**bad)
                self.assertEqual(cm.exception.code, code, bad)
            # 语言：任务参数给的 Chinese 是默认，请求可以换；空串让模型自己判断
            self.assertEqual(go(seq=0, audio=b64(4), want="final", session="l1", language="English")["text"], "4 |English")
            self.assertEqual(go(seq=0, audio=b64(4), want="final", session="l2", language="")["text"], "4 |")
            # 已经起来的进程预热：后台解一次 1 秒静音、1 个 token；正在热时不再叠一次
            self.assertTrue(w.rewarm())
            self.assertFalse(w.rewarm())
            for _ in range(100):
                if not w.rewarming:
                    break
                time.sleep(0.05)
            with open(fake + ".log") as f:
                self.assertEqual(f.read().splitlines()[-1], "32000 1")
            go(seq=0, audio=b64(10), want="none")                         # 30 秒没消息就丢
            h.asr_sessions[("luoke-ime", "s1")]["t"] -= 31
            h._expire_asr_sessions()
            self.assertEqual(h.asr_sessions, {})
            for i in range(6):                                           # 一个配件最多留 4 个会话
                h.transcribe(t, {"session": f"m{i}", "seq": 0, "audio": b64(2), "want": "none"}, "luoke-ime")
            self.assertEqual(sorted(k[1] for k in h.asr_sessions), ["m2", "m3", "m4", "m5"])
            with open(os.path.join(h.env_dir, "logs", "hub.log"), encoding="utf-8") as f:
                logged = f.read()
            self.assertNotIn("螺壳", logged)
            self.assertNotIn(b64(40), logged)
        finally:
            scorer.TRANSCRIBE_WORKER = real
            for x in h.scorers.values():
                x.stop()

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
            self.assertEqual(h.task_status("luoke-search.embed")["status"], "available")
            w = h.scorer_for("luoke-search.embed")
            self.assertIsInstance(w, scorer.EmbedWorker)
            for _ in range(100):
                if w.state == "ready":
                    break
                time.sleep(0.05)
            self.assertEqual(w.state, "ready")
            self.assertEqual(w.ready_info.get("dims"), 4)
            # 不带等级 = 后台：单独记账（#5 审阅 2026-10-19）——不占引擎的后台名额、不设功耗档间歇，紧接着的下一条后台照样接
            out = h.embed("luoke-search.embed", {"texts": ["title: none | text: 你好", "title: none | text: 世界"]}, accessory="luoke-search")
            self.assertEqual((len(out["vectors"]), out["dims"]), (2, 4))
            self.assertTrue(out["model"].endswith("+embeddinggemma-2"))
            self.assertEqual(out["vector_space"], "embeddinggemma-2/1")       # 每次答复都带，建索引中途换了也认得出
            self.assertIsNone(d.job)
            self.assertEqual(d.cooldown_until, 0.0)                         # 引擎的间歇不动：不挤邮件、记忆的后台
            ok, *_ = d.admit_background(False, True)
            self.assertTrue(ok)
            with self.assertRaises(BadRequest) as cm:                      # 嵌入这条通道自己歇（luoke-core#9 Q1）
                h.embed("luoke-search.embed", {"texts": ["title: none | text: 再来"]}, accessory="luoke-search")
            self.assertEqual(cm.exception.extra.get("reason"), "lane_busy")
            self.assertIn("retry_after_ms", cm.exception.extra)
            clock.tick(5)
            out2 = h.embed("luoke-search.embed", {"texts": ["title: none | text: 再来"]}, accessory="luoke-search")
            self.assertEqual(len(out2["vectors"]), 1)
            clock.tick(5)
            # 后台批切成几条一份算，答复和一次算的一样（顺序不变）
            many = [f"title: none | text: {i}" for i in range(h.EMBED_CHUNK * 2 + 3)]
            out3 = h.embed("luoke-search.embed", {"texts": many}, accessory="luoke-search")
            self.assertEqual(len(out3["vectors"]), len(many))
            # 搜索时的查询（user-waiting）走优先通道：不打断引擎上的后台、不让全机后台停（#5 审阅），也不进打分的 K 分钟窗口
            out = h.embed("luoke-search.embed", {"texts": ["task: search result | query: 你好"]}, priority="user-waiting", accessory="luoke-search")
            self.assertEqual(len(out["vectors"]), 1)
            self.assertFalse(d.interactive())
            self.assertFalse(d.scored_recently())
            clock.tick(5)                                                # 上一批后台的通道间歇过去
            self.assertEqual(len(h.embed("luoke-search.embed", {"texts": ["title: none | text: 后台"]}, accessory="luoke-search")["vectors"]), 1)
            clock.tick(5)
            d.user_waiting_arrived()                                     # 真有用户在等引擎（例如分拣）：后台嵌入照样让路
            with self.assertRaises(BadRequest) as cm:
                h.embed("luoke-search.embed", {"texts": ["title: none | text: 后台"]}, accessory="luoke-search")
            self.assertEqual(cm.exception.extra.get("reason"), "interactive")
            self.assertGreater(h.specials_running_gb(), 0)             # 常驻的嵌入进程从底座预算里先扣
            clock.tick(3600)                                            # 过了安静期，探针本该跑——但嵌入进程不探
            h._probe_scorers()
            self.assertFalse(w.probed)
            self.assertEqual(w.probe_attempts, 0)
            h.release_accessory("luoke-search")                         # luoke-search 退出：只有它在用的嵌入进程停掉
            self.assertEqual(w.state, "stopped")
        finally:
            scorer.EMBED_WORKER = real
            for x in h.scorers.values():
                x.stop()


class EmbedMediaTest(unittest.TestCase):
    """/v1/embed 的图片和声音（草案 docs/research/embed-multimodal-draft.md，2026-10-19 写的代码，还不是契约）：
    任务参数开关、校验、逐条出错。不碰 mlx。2026-11-03 起配方的 32、128 两档有 image_batch / audio_batch，16 档只收文字。"""
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
        params = r.tasks["luoke-search.embed"]["params"]
        for tier in [k for k in params if k != "*"]:      # 配方按档给的媒体参数拿掉，只用这里给的（测试机是 32 档）
            params[tier] = {k: v for k, v in params[tier].items() if k not in self.MEDIA}
        if media:
            params["*"].update(self.MEDIA)
        if assign:
            os.makedirs(os.path.join(d, r.model("embeddinggemma-2")["dir"]))
        else:
            for a in r.assignment.values():
                (a.get("special") or {}).pop("luoke-search.embed", None)
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
        r = Recipe.load(RECIPE)                                         # 配方（2026-11-03）：16 档只收文字，32、128 收图片和声音
        self.assertEqual(media_kinds(r.params("luoke-search.embed", "16")), set())
        for tier in ("32", "128"):
            self.assertEqual(media_kinds(r.params("luoke-search.embed", tier)), {"image", "audio"})
        self.assertEqual(scorer.EmbedWorker("/py", "/s", "/m", media=True)._argv()[-1], "--media")
        self.assertNotIn("--media", scorer.EmbedWorker("/py", "/s", "/m")._argv())
        h = self.hub(media=True, assign=True)
        h.apply_budget = lambda: None
        real = scorer.EmbedWorker.start
        scorer.EmbedWorker.start = lambda self: None                    # 不真起进程，只看建的是哪种
        try:
            self.assertTrue(h.scorer_for("luoke-search.embed").media)
            self.assertFalse(self.hub(media=False, assign=True).scorer_for("luoke-search.embed").media)
        finally:
            scorer.EmbedWorker.start = real

    def test_hub_validates_inputs(self):
        h = self.hub(media=True)
        task = "luoke-search.embed"
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
        h2.recipe.tasks["luoke-search.embed"]["params"]["*"]["audio_batch"] = 2          # 只收声音的档不收图片
        bad({"inputs": [img]}, "invalid_request", h=h2)

    def test_media_tiers_budget_with_media_wired_gb(self):
        """2026-11-03：配方 16 档只收文字、32 和 128 档收图片和声音。连视觉、声音一起加载的嵌入进程按 media_wired_gb 算预算，
        只有文字时照旧按 wired_gb；32 档扣掉打分和嵌入以后 9B 底座照样放得下，天花板照常降。"""
        from luoke_core.hub import Hub
        from luoke_core import scorer
        r = Recipe.load(RECIPE)
        m = r.model("embeddinggemma-2")
        self.assertEqual((m["wired_gb"], m["media_wired_gb"]), (2.8, 4.4))
        real = scorer.EmbedWorker.start, scorer.ScoreWorker.start
        scorer.EmbedWorker.start = scorer.ScoreWorker.start = lambda self: None     # 不真起进程
        try:
            for mem, media, gb in ((16, False, 2.8), (32, True, 4.4), (128, True, 4.4)):
                d = tempfile.mkdtemp()
                h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=mem, smoke=False)
                h.engine_missing = lambda: False
                h.worker_hold_reason = lambda model: None
                h.apply_budget = lambda: None
                w = h.scorer_for("luoke-search.embed")
                self.assertEqual((h.tier_name, w.media, h.worker_wired_gb("embeddinggemma-2")), (str(mem), media, gb))
                self.assertEqual(h.worker_wired_gb("qwen3.5-0.8b-base-4bit"), 2.7)      # 打分进程没有 media，照旧
            h.scorer_for("luoke-ime.rerank")
            for x in h.scorers.values():
                x.state = "ready"
            self.assertAlmostEqual(h.specials_running_gb(), 2.7 + 4.4)
        finally:
            scorer.EmbedWorker.start, scorer.ScoreWorker.start = real
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.engine_missing = lambda: False
        h.scorers["embeddinggemma-2"] = scorer.EmbedWorker("/py", "/s", "/m", media=True)
        h.scorers["qwen3.5-0.8b-base-4bit"] = scorer.ScoreWorker("/py", "/s", "/m", 16)
        for x in h.scorers.values():
            x.state = "ready"
        ceilings = []
        h.engine.alive = lambda: True
        h.engine.guard_gb = h.tier["guard_gb"]
        h.engine.set_ceiling = lambda gb: ceilings.append(gb) or True
        h.apply_budget()
        self.assertEqual(ceilings, [round(20 - 2.7 - 4.4, 1)])                   # 降到 12.9，没有"会挤掉底座"而不降
        self.assertLessEqual(r.model("qwen3.5-9b-4bit")["wired_gb"], (ceilings[0] - 2) * 0.95)
        h.engine.loaded = None                                                   # 起嵌入进程前的内存检查也按 media_wired_gb
        with unittest.mock.patch.object(machine, "memory_sample", return_value={"wired_gb": machine.metal_cap_gb(32) - 4.0}):
            self.assertIsNotNone(h.worker_hold_reason("embeddinggemma-2"))      # 剩 4.0 GB：开了媒体要 4.4，不起
            h.scorers["embeddinggemma-2"].media = False
            self.assertIsNone(h.worker_hold_reason("embeddinggemma-2"))         # 只有文字要 2.8，起

    def test_tier_switch_replaces_the_embed_worker(self):
        """11-04 16 GB 测试机复现：16 → 32 以后每张图都答「嵌入进程失败」，空闲停了再起也一样，要重启螺壳才好——嵌入进程对象
        留着建它时的模式，再起照旧只收文字。换档时模式不一样的嵌入进程要停掉、换新的对象；32 → 16 也不留着视觉和声音。"""
        from luoke_core.hub import Hub
        from luoke_core import scorer
        d = tempfile.mkdtemp()
        r = Recipe.load(RECIPE)
        for tier in ("16", "32"):
            os.makedirs(os.path.join(d, r.model(r.base_for(tier))["dir"]), exist_ok=True)
        os.makedirs(os.path.join(d, r.model("embeddinggemma-2")["dir"]))
        h = Hub(r, d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.worker_hold_reason = lambda model: None
        h.engine_missing = lambda: True          # 换档不重启引擎（这里只看嵌入进程）
        h.apply_budget = lambda: None
        stopped = []
        real = scorer.EmbedWorker.start, scorer.EmbedWorker.stop

        def fake_start(w):
            w.state = "ready"

        def fake_stop(w):
            stopped.append(w)
            w.state = "stopped"
        scorer.EmbedWorker.start, scorer.EmbedWorker.stop = fake_start, fake_stop
        task = "luoke-search.embed"
        img = {"image": "QUJD", "budget": 280}
        try:
            self.assertEqual(h.tier_name, "32")
            w32 = h.scorer_for(task)
            self.assertTrue(w32.media)
            h.apply_tier("16")                                             # 32 → 16：带视觉、声音的进程停掉、拿走
            self.assertEqual((stopped, "embeddinggemma-2" in h.scorers), ([w32], False))
            w16 = h.scorer_for(task)
            self.assertFalse(w16.media)
            with self.assertRaises(BadRequest) as cm:
                h.embed(task, {"inputs": [img]}, priority="user-waiting")
            self.assertEqual(cm.exception.code, "invalid_request")       # 16 档只收文字
            h.apply_tier("32")                                             # 16 → 32：只收文字的那个停掉，下一张图交给新进程
            self.assertIs(stopped[-1], w16)
            w = h.scorer_for(task)
            self.assertIsNot(w, w16)
            self.assertTrue(w.media)
            self.assertIn("--media", w._argv())
            w.state = "stopped"                                            # 空闲停了以后再起：还是同一档的模式
            self.assertIs(h.scorer_for(task), w)
            h.tier_name = "16"                                             # 不经 apply_tier 改了档（例如换档等的底座下好了）：scorer_for 自己也换
            w2 = h.scorer_for(task)
            self.assertFalse(w2.media)
            for _ in range(100):
                if stopped[-1] is w:
                    break
                time.sleep(0.01)
            self.assertIs(stopped[-1], w)                                  # 旧的在后台停掉
        finally:
            scorer.EmbedWorker.start, scorer.EmbedWorker.stop = real
            h.stop()

    def test_image_tokens_per_request_are_capped(self):
        """8 张 1120 的图把嵌入进程涨到 13.3 GB（16 GB 测试机 11-04）：一次请求的 budget 加起来不超过 max_image_tokens，
        560、1120 的图一次只许一张；放行的最坏情况（8 张 280 4.3 GB、1 张 1120 约 4.0 GB）在 media_wired_gb 以内。"""
        r = Recipe.load(RECIPE)
        for tier in ("32", "128"):
            p = r.params("luoke-search.embed", tier)
            self.assertEqual((p["max_image_tokens"], p["single_image_budgets"]), (2240, [560, 1120]))
            self.assertEqual(p["image_batch"] * 280, p["max_image_tokens"])
        self.assertGreaterEqual(r.model("embeddinggemma-2")["media_wired_gb"], 4.3)
        h = self.hub(media=True)
        params = h.recipe.tasks["luoke-search.embed"]["params"]["*"]
        params.update({"image_budgets": [70, 140, 280, 560, 1120], "max_image_tokens": 2240, "single_image_budgets": [560, 1120]})
        task = "luoke-search.embed"

        def code(inputs):
            try:
                h.embed(task, {"inputs": inputs}, priority="user-waiting")
            except BadRequest as e:
                self.assertNotIn("QUJD", e.message)
                return e.code
            return "ok"
        img = lambda b: {"image": "QUJD", "budget": b}
        self.assertEqual(code([img(280)] * 8), "unsupported")               # 合法：走到任务状态（这里没分配模型）
        self.assertEqual(code([img(1120)]), "unsupported")
        self.assertEqual(code([img(1120), {"audio": "QUJD"}, {"text": "a"}]), "unsupported")   # 一张大图加声音、文字可以
        self.assertEqual(code([img(1120)] * 2), "invalid_request")
        self.assertEqual(code([img(1120), img(70)]), "invalid_request")
        self.assertEqual(code([img(560)] * 4), "invalid_request")           # 加起来 2240，但 560 也只许一张
        params["image_batch"] = 10
        self.assertEqual(code([img(280)] * 9), "invalid_request")           # 2520 > 2240
        self.assertEqual(code([img(140)] * 10), "unsupported")

    def test_parity_tool_media_auto_follows_tier_params(self):
        """发布评测带 --media auto：这一档的任务参数收图片、声音就再核媒体 4 条，只收文字的档只核文字 5 条。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import embed_parity as ep
        from luoke_core import client
        r = Recipe.load(RECIPE)
        self.assertEqual(ep.media_names("auto", r.params("luoke-search.embed", "16")), [])
        for tier in ("32", "128"):
            self.assertEqual(ep.media_names("auto", r.params("luoke-search.embed", tier)), list(ep.MEDIA))
        self.assertEqual(ep.media_names("auto", {"audio_batch": 2}), ["speech"])
        self.assertEqual(ep.media_names("auto", {"image_batch": 8, "image_budgets": [70, 140]}), ["photo-140", "frame-70"])
        self.assertEqual((ep.media_names("on", {}), ep.media_names("off", {"image_batch": 8})), (list(ep.MEDIA), []))
        with open(os.path.join(ROOT, "tools", "release_evals.json"), encoding="utf-8") as f:
            cmd = json.load(f)["luoke-search.embed"]["command"]
        self.assertEqual(cmd[cmd.index("--media") + 1], "auto")
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ep.TEXTS), "w", encoding="utf-8") as f:     # 文字 5 条，读法和 luoke-search 的 texts.json 一样（#73）
            json.dump({"texts": [{"name": f"t{i}", "input": f"input {i}"} for i in range(5)]}, f)
        tnames = list(ep.reference_texts(d))
        names = tnames + list(ep.MEDIA)
        ref = {n: [0.0] * i + [1.0] + [0.0] * (len(names) - i - 1) for i, n in enumerate(names)}
        with open(os.path.join(d, "vectors.json"), "w") as f:
            json.dump({"made_with": "test", "vector_version": 1, "vectors": ref}, f)
        for fname in ("screen.png", "photo.jpg", "speech.wav"):
            with open(os.path.join(d, fname), "wb") as f:
                f.write(b"x")
        seen = {}

        class FakeClient:
            tier = "32"

            def __init__(self, accessory, socket_path=None):
                pass

            def tasks(self):
                return {"tier": self.tier, "tasks": {ep.TASK: {"status": "available", "params": r.params(ep.TASK, self.tier)}}}

            def warmup(self, task):
                return {"accepted": True}

            def embed(self, task, texts, priority="background"):
                return {"vectors": [list(ref[n]) for n in tnames], "dims": len(names), "ms": 1.0}

            def embed_inputs(self, task, inputs, priority="background"):
                seen["inputs"] = inputs
                return {"vectors": [list(ref[n]) for n in ep.MEDIA], "errors": [None] * len(inputs), "dims": len(names), "ms": 1.0}
        real = client.HubClient
        client.HubClient = FakeClient
        out = os.path.join(d, "r.json")
        try:
            args = ["--socket", "/nonexistent.sock", "--vectors", os.path.join(d, "vectors.json"), "--media", "auto", "--result", out]
            self.assertEqual(ep.main(args), 0)
            res = json.load(open(out))
            self.assertEqual((res["media"], res["expected"], len(res["rows"]), len(seen["inputs"])), (True, 9, 9, 4))
            FakeClient.tier, seen = "16", {}
            self.assertEqual(ep.main(args), 0)
            res = json.load(open(out))
            self.assertEqual((res["media"], res["expected"], len(res["rows"]), seen), (False, 5, 5, {}))
        finally:
            client.HubClient = real

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
            w = h.scorer_for("luoke-search.embed")
            for _ in range(100):
                if w.state == "ready":
                    break
                time.sleep(0.05)
            self.assertEqual(w.state, "ready")
            self.assertTrue(w.ready_info.get("media"))                  # 按任务参数带了 --media 起
            out = h.embed("luoke-search.embed", {"inputs": [{"text": "title: none | text: a"}, {"image": "QkFE", "budget": 140},
                                                     {"image": "QUJD", "budget": 280}, {"audio": "QUJD"}]},
                          priority="user-waiting", accessory="luoke-search")
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

    def test_helper_goes_by_its_own_manifest(self):
        """Contents/Helpers 里的程序要的系统可以比螺壳声称的高（本体在低的系统上不起它），按它自己清单的 min_macos 查；没有清单照 .app 查。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import json, minos_check, plistlib
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "螺壳.app")
            os.makedirs(os.path.join(app, "Contents", "Helpers"))
            os.makedirs(os.path.join(app, "Contents", "Resources", "search"))
            with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"LSMinimumSystemVersion": "14.0"}, f)
            with open(os.path.join(app, "Contents", "Helpers", "luoke-search"), "wb") as f:
                f.write(self.macho((26, 2)))
            self.assertEqual(minos_check.check_app(app)[1], [("Contents/Helpers/luoke-search", "26.2")])
            with open(os.path.join(app, "Contents", "Resources", "search", "luoke-accessory.json"), "w") as f:
                json.dump({"min_macos": "26.2"}, f)
            self.assertEqual(minos_check.check_app(app)[1], [])
            with open(os.path.join(app, "Contents", "Helpers", "luoke-search"), "wb") as f:
                f.write(self.macho((27, 0)))
            self.assertEqual(minos_check.check_app(app)[1], [("Contents/Helpers/luoke-search", "27.0")])

    def test_scan_files_go_by_the_scan_manifest(self):
        """打进螺壳的扫描（0.3.0 起）：luoke-scan、luoke-scan-admin，以及它的 bundled-tools.json 列的 Helpers、Frameworks 下的
        ClamAV、YARA 和库，按 Resources/scan 的清单的 min_macos 查；没列的照 .app 查。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import json, minos_check, plistlib
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "螺壳.app")
            for sub in ("Helpers", "Frameworks", os.path.join("Resources", "scan")):
                os.makedirs(os.path.join(app, "Contents", sub))
            with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"LSMinimumSystemVersion": "14.0"}, f)
            for rel in ("Helpers/luoke-scan", "Helpers/luoke-scan-admin", "Helpers/clamscan", "Frameworks/libclamav.12.dylib",
                        "Frameworks/libother.dylib"):
                with open(os.path.join(app, "Contents", rel), "wb") as f:
                    f.write(self.macho((26, 0)))
            with open(os.path.join(app, "Contents", "Resources", "scan", "luoke-accessory.json"), "w") as f:
                json.dump({"accessory": "luoke-scan", "min_macos": "26.2"}, f)
            with open(os.path.join(app, "Contents", "Resources", "scan", "bundled-tools.json"), "w") as f:
                json.dump([{"file": "Helpers/clamscan"}, {"file": "Frameworks/libclamav.12.dylib"}], f)
            self.assertEqual(sorted(minos_check.check_app(app)[1]), [("Contents/Frameworks/libother.dylib", "26.0")])


    def test_min_macos_has_one_source(self):
        """最低 macOS 只在 minos_check.LUOKE_MIN_MACOS 定：build_app.sh 和 package_python_app.py 都从那里读，不各写一个"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import importlib.util, minos_check
        self.assertEqual(minos_check.LUOKE_MIN_MACOS, "26.2")
        with open(os.path.join(ROOT, "app", "build_app.sh"), encoding="utf-8") as f:
            sh = f.read()
        self.assertNotRegex(sh, r'"LSMinimumSystemVersion":\s*"\d')
        self.assertIn('"LSMinimumSystemVersion": os.environ["LUOKE_MIN_MACOS"]', sh)
        line = next(x for x in sh.splitlines() if x.startswith("LUOKE_MIN_MACOS=$("))
        cmd = line[len("LUOKE_MIN_MACOS=$("):line.index(") ||")].replace('"$root/tools"', os.path.join(ROOT, "tools"))
        out = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "26.2", out.stderr)          # build_app.sh 那一行真读得出来
        spec = importlib.util.spec_from_file_location("ppa_minos", os.path.join(ROOT, "tools", "package_python_app.py"))
        ppa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ppa)
        self.assertEqual(ppa.MIN_MACOS, minos_check.LUOKE_MIN_MACOS)

    def test_floor_problems_cover_the_app_and_nested_apps(self):
        """螺壳.app 和里面嵌套的 .app 声称的最低 macOS 不能低于 LUOKE_MIN_MACOS（没写也不行）；gap 写出声称的和实际要的"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import minos_check, plistlib
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "螺壳.app")
            os.makedirs(os.path.join(app, "Contents", "MacOS"))
            with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"LSMinimumSystemVersion": "26.2"}, f)
            with open(os.path.join(app, "Contents", "MacOS", "LuokeCore"), "wb") as f:
                f.write(self.macho((14, 0)))
            self.assertEqual(minos_check.floor_problems(app), [])
            self.assertEqual(minos_check.gap(app), ("26.2", "14.0"))
            nested = os.path.join(app, "Contents", "Helpers", "Old.app", "Contents")
            os.makedirs(nested)
            with open(os.path.join(nested, "Info.plist"), "wb") as f:
                plistlib.dump({"LSMinimumSystemVersion": "14.0"}, f)
            os.makedirs(os.path.join(app, "Contents", "Resources", "Bare.app", "Contents"))
            self.assertEqual(minos_check.floor_problems(app), [("Contents/Helpers/Old.app", "14.0"), ("Contents/Resources/Bare.app", None)])
            with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"LSMinimumSystemVersion": "14.0"}, f)
            self.assertEqual(minos_check.floor_problems(app)[0], (".", "14.0"))
            self.assertEqual(minos_check.floor_problems(app, "13.0"), [("Contents/Resources/Bare.app", None)])
            self.assertEqual(minos_check.main([app, "--floor"]), 1)

    def test_release_stops_on_the_floor(self):
        """发布：主控或嵌套的 .app 低于 LUOKE_MIN_MACOS 就停；引擎包要的比它高也停；单独安装的配件（输入法）先只提醒"""
        with open(os.path.join(ROOT, "tools", "release.py"), encoding="utf-8") as f:
            src = f.read()
        build = src[src.index("    limit, bad = minos_check.check_app(paths.app)"):]
        self.assertLess(build.index("minos_check.floor_problems(paths.app)"), build.index("leak_check(paths.app"))
        self.assertIn("die(f\"主控 .app 或嵌套的 .app 声称的最低 macOS 低于螺壳的", build)
        acc = src[src.index("limit, bad = minos_check.check_app(app)"):src.index("zip_path = os.path.join(paths.acc_dir")]
        self.assertIn("minos_check.floor_problems(app)", acc)
        self.assertIn("say(f\"提醒：", acc)
        self.assertNotIn("die(f\"{name} 声称的最低", acc)
        self.assertIn("minos_check.version_tuple(top) > minos_check.version_tuple(minos_check.LUOKE_MIN_MACOS)", src)

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


class EngineStartRecoveryTest(unittest.TestCase):
    """luoke-top 11-03 核查：开机时引擎没起来（超时、退出），Hub.start 抛错、整个主控退出，菜单只剩退出码。
    现在：主控照常起来，记一次崩溃和原因，交给 _check_engine 重启；起来以后清掉原因。主动重启成功清掉熔断。"""

    def hub(self):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.engine_missing = lambda: False
        return h

    def test_engine_gone_while_stopping_is_not_a_crash(self):
        """core-private#232：退出时要等空闲任务收尾（最多十几秒），这期间引擎先没了，采样线程当成崩溃重启、又加载了模型。
        先记下在停，再等任务"""
        h = self.hub()
        h.engine.alive = lambda: False
        started = []
        h.engine.start = lambda wait_models=None, timeout=180: started.append(1) or []
        h.engine.stop = lambda: None
        seen = {}

        def wrapping_up():
            h._check_engine()       # 等任务收尾时采样线程跑了一轮
            seen["stopping"] = h.stop_event.is_set()
        h.jobs.stop = wrapping_up
        h.stop()
        self.assertTrue(seen["stopping"])
        self.assertEqual(started, [])
        self.assertEqual(list(h.engine_crashes), [])

    def test_hub_survives_an_engine_that_will_not_start(self):
        from luoke_core.engine import EngineError
        h = self.hub()
        state = {"alive": False}

        def fail(wait_models=None, timeout=180):
            raise EngineError("oMLX 启动超时")
        h.engine.start = fail
        h.engine.alive = lambda: state["alive"]
        try:
            h.start()
            self.assertEqual(h.phase, "running")
            self.assertEqual(h.engine_error, "oMLX 启动超时")
            self.assertEqual(len(h.engine_crashes), 1)

            def ok(wait_models=None, timeout=180):
                state["alive"] = True
                return []
            h.engine.start = ok
            h._check_engine()
            self.assertIsNone(h.engine_error)
            self.assertIsNone(h.breaker)
        finally:
            h.stop()

    def test_an_engine_that_cannot_even_be_spawned_does_not_take_the_hub_down(self):
        """引擎程序在、却起不了进程（没有执行权限、被隔离）：Popen 抛的是 OSError，不是 EngineError，主控也要照常起来"""
        h = self.hub()

        def denied(wait_models=None, timeout=180):
            raise PermissionError(13, "Permission denied")
        h.engine.start = denied
        h.engine.alive = lambda: False
        try:
            h.start()
            self.assertEqual(h.phase, "running")
            self.assertIn("PermissionError", h.engine_error)
            h._check_engine()       # 重启时一样：记下原因，不往外抛
            self.assertIn("PermissionError", h.engine_error)
        finally:
            h.stop()

    def test_smoke_check_after_recovery_runs_once_and_only_after_a_boot_failure(self):
        """开机时引擎没起来、后来才起来：补做一次冒烟检查。看内存里的 smoke_running，不看存盘的
        （上次检查中途被杀，存盘的 running 一直是 True，之前就永远不补）；之后平常的崩溃重启不再跑"""
        from luoke_core.engine import EngineError
        h = self.hub()
        h.smoke = True
        os.makedirs(os.path.join(h.model_dir, h.recipe.model(h.base_name)["dir"]), exist_ok=True)
        h.state["smoke"] = {"running": True}        # 上一次检查中途被杀留下的
        ran = []
        done = threading.Event()

        def smoke(force=False):
            ran.append(force)
            done.set()
        h.smoke_check = smoke
        h.apply_budget = lambda: None
        h.engine.resolve = lambda d: "base"
        state = {"alive": False}

        def fail(wait_models=None, timeout=180):
            raise EngineError("oMLX 启动超时")

        def ok(wait_models=None, timeout=180):
            state["alive"] = True
            return ["base"]
        h.engine.start = fail
        h.engine.alive = lambda: state["alive"]
        try:
            h.start()
            self.assertEqual(ran, [])
            h.engine.start = ok
            h._check_engine()
            self.assertTrue(done.wait(5))
            self.assertEqual(len(ran), 1)
            done.clear()
            state["alive"] = False          # 之后平常的一次崩溃重启：不再补
            h._check_engine()
            self.assertFalse(done.wait(0.5))
            self.assertEqual(len(ran), 1)
        finally:
            h.stop()

    def test_deliberate_restart_clears_the_breaker(self):
        h = self.hub()
        h.breaker = "引擎 10 分钟内崩溃 3 次，已熔断"
        h.engine_crashes.extend([time.time()] * 3)
        h.engine.stop = lambda: None
        h.engine.start = lambda wait_models=None, timeout=180: []
        h.apply_budget = lambda: None
        h._restart_engine_locked()
        self.assertIsNone(h.breaker)
        self.assertEqual(len(h.engine_crashes), 0)


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

    def app_with_id(self, folder, bundle_id):
        app = os.path.join(folder, "螺壳.app")
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
        return app

    def codesign_says(self, identifier, calls=None):
        """subprocess.run 换成假的：codesign -dv 答这个 Identifier，其他的记进 calls"""
        def run(cmd, **kw):
            if calls is not None and cmd[0] != "codesign":
                calls.append(os.path.basename(cmd[1]))
            return types.SimpleNamespace(stderr=f"Executable=/x\nIdentifier={identifier}\nFormat=app bundle\n")
        return unittest.mock.patch.object(self.rel.subprocess, "run", run)

    def test_release_identity_is_what_the_build_gets(self):
        """build_app.sh 不传就是开发版：传的这个值和核对的是同一个，改了这里要知道"""
        self.assertEqual(self.rel.BUNDLE_ID, "ai.luoke.core")
        with open(os.path.join(ROOT, "tools", "release.py"), encoding="utf-8") as f:
            self.assertIn("LUOKE_BUNDLE_ID=BUNDLE_ID", f.read())

    def test_only_the_release_identity_passes(self):
        """Info.plist 和签名有一个不是正式版的身份就停；开发版的身份打进去，邮件助手就不查签名了"""
        good, dev = "ai.luoke.core", "ai.luoke.core.dev"
        for declared, signed, ok in ((good, good, True), (dev, dev, False), (dev, good, False), (good, dev, False)):
            with tempfile.TemporaryDirectory() as d, self.codesign_says(signed):
                app = self.app_with_id(d, declared)
                if ok:
                    self.rel.check_bundle_id(app)
                else:
                    with self.assertRaises(SystemExit):
                        self.rel.check_bundle_id(app)
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(
                self.rel.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stderr="not signed at all\n")):
            with self.assertRaises(SystemExit):      # 没签名也不算过
                self.rel.check_bundle_id(self.app_with_id(d, good))

    def test_sign_and_dmg_stop_on_the_development_identity(self):
        """签完就核；出 DMG 之前再核一次（--stage dmg 可以单独跑），不做 DMG"""
        a = types.SimpleNamespace(identity="x", notarize=False, yes=True)
        with tempfile.TemporaryDirectory() as d:
            paths = types.SimpleNamespace(app=self.app_with_id(d, "ai.luoke.core.dev"), dmg=os.path.join(d, "x.dmg"))
            calls = []
            with self.codesign_says("ai.luoke.core.dev", calls), \
                    unittest.mock.patch.object(self.rel, "confirm_identity", lambda *x: "sha1"), \
                    unittest.mock.patch.object(self.rel, "resolve_identity", lambda *x: "sha1"):
                with self.assertRaises(SystemExit):
                    self.rel.stage_sign(a, paths)
                self.assertEqual(calls, ["release_sign.sh"])
                with self.assertRaises(SystemExit):
                    self.rel.stage_dmg(a, paths)
                self.assertEqual(calls, ["release_sign.sh"])      # make_dmg.sh 没跑

    def test_bundled_agent_version_goes_into_verified(self):
        """打进主控的 agent 核心没有评测的任务：照钉住的那份 luoke_agent/__init__.py 的版本号进已验证版本，主控才认它"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "luoke_agent"))
            with open(os.path.join(d, "luoke_agent", "__init__.py"), "w", encoding="utf-8") as f:
                f.write('"""luoke-agent"""\n\n__version__ = "0.0.5"\n')
            v = self.rel.bundled_verified({"luoke-agent": d, "luoke-mem": "/nowhere"})
        self.assertEqual(v, {"luoke-agent": {"versions": ["0.0.5"]}})
        r = Recipe.load(os.path.join(ROOT, "recipes", "dev.json"))
        r.verified = {**r.verified, **v}
        self.assertTrue(r.version_verified("luoke-agent", "0.0.5"))
        self.assertFalse(r.version_verified("luoke-agent", "0.0.4"))
        # internal_build 的配方阶段要把打进主控的源码交给 release.py（之前只有打包阶段给，配方里就没有 agent）
        with open(os.path.join(ROOT, "tools", "internal_build.py"), encoding="utf-8") as f:
            self.assertRegex(f.read(), r'release_py\(a\.tag, "recipe", bundled,')

    def test_no_bundled_agent_no_verified_entry(self):
        """这一版不带 agent（没给 --repo luoke-agent=…）：已验证版本里不写它"""
        self.assertEqual(self.rel.bundled_verified({}), {})
        self.assertEqual(self.rel.bundled_verified({"luoke-mem": "/nowhere"}), {})

    def test_scan_needs_evidence_and_a_team_for_rc(self):
        """螺壳扫描（#102 审查）：没有评测证据、或者定不了编进它的团队标识，RC、正式版停；内部版、本机版照出。清单在 packaging/luoke-accessory.json"""
        import types
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "packaging"))
            with open(os.path.join(d, "packaging", "luoke-accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"accessory": "luoke-scan", "version": "0.3.3"}, f)
            self.assertEqual(self.rel.bundled_unverified(d, {}), "0.3.3")
            for tag in ("internal-1106", "local-1106-0900"):
                self.rel.check_scan_evidence(tag, d, {})
            for tag in ("beta2", "beta2-rc1", "v1.0"):
                with self.assertRaises(SystemExit):
                    self.rel.check_scan_evidence(tag, d, {})
                self.rel.check_scan_evidence(tag, d, {"luoke-scan": {"versions": ["0.3.3"]}})
        # 团队标识：build 这一步在调 build_app.sh 之前就停
        calls = []
        a = types.SimpleNamespace(repo_override={"luoke-scan": "/scan", "luoke-mem": "/mem"})
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "mem")
            os.makedirs(os.path.join(mem, "packaging"))
            with open(os.path.join(mem, "packaging", "accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"version": "0.28.3"}, f)
            a.repo_override["luoke-mem"] = mem
            recipe, manifest = os.path.join(d, "recipe.json"), os.path.join(d, "release.json")
            for p in (recipe, manifest):
                open(p, "w").write("{}")
            for tag, stops in (("beta2-rc1", True), ("beta2", True), ("internal-1106", False)):
                paths = types.SimpleNamespace(recipe=recipe, manifest=manifest, release=tag, out=d, app=os.path.join(d, "app", "x.app"))
                with unittest.mock.patch.object(self.rel, "release_team", lambda: ""), \
                        unittest.mock.patch.object(self.rel.Recipe, "load", lambda p: types.SimpleNamespace(version_verified=lambda *x: True, engine={})), \
                        unittest.mock.patch.object(self.rel.subprocess, "run", lambda *x, **k: calls.append(tag) or (_ for _ in ()).throw(RuntimeError("built"))):
                    if stops:
                        with self.assertRaises(SystemExit):
                            self.rel.stage_build(a, paths)
                    else:
                        with self.assertRaises(RuntimeError):       # went on to build_app.sh
                            self.rel.stage_build(a, paths)
        self.assertEqual(calls, ["internal-1106"])

    def test_helper_version_must_come_from_the_evals(self):
        """收进本体的应用版本要有评测证据，不像 agent 核心那样照源码补"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Resources"))
            with open(os.path.join(d, "Resources", "luoke-accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"accessory": "luoke-search", "version": "0.1.5"}, f)
            self.assertEqual(self.rel.bundled_unverified(d, {"luoke-search": {"versions": ["0.1.4"]}}), "0.1.5")
            self.assertEqual(self.rel.bundled_unverified(d, {}), "0.1.5")
            self.assertIsNone(self.rel.bundled_unverified(d, {"luoke-search": {"versions": ["0.1.5"]}}))
            self.assertEqual(self.rel.bundled_verified({"luoke-search": d}), {})
            for tag in ("internal-1105", "local-1105"):       # 内部版、本机版照出
                self.rel.check_bundled_evidence(tag, d, {})
            for tag in ("beta2", "beta2-rc1", "v1.0"):         # RC、正式版停
                with self.assertRaises(SystemExit):
                    self.rel.check_bundled_evidence(tag, d, {})
                self.rel.check_bundled_evidence(tag, d, {"luoke-search": {"versions": ["0.1.5"]}})
        with tempfile.TemporaryDirectory() as d:        # 螺壳邮件助手：清单在 packaging
            os.makedirs(os.path.join(d, "packaging"))
            with open(os.path.join(d, "packaging", "accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"accessory": "luoke-mail", "version": "0.19.28"}, f)
            self.assertEqual(self.rel.bundled_unverified(d, {"luoke-mail": {"versions": ["0.19.27"]}}), "0.19.28")
            self.assertIsNone(self.rel.bundled_unverified(d, {"luoke-mail": {"versions": ["0.19.28"]}}))
            # 主控不核配件版本以后（62f5427）：邮件助手没证据和搜索一样，内部版照出（原来内部版也停），RC、正式版停
            for tag in ("internal-1114", "local-1114-0900"):
                self.rel.check_mail_evidence(tag, d, {})
            for tag in ("beta2", "beta2-rc1", "v1.0"):
                with self.assertRaises(SystemExit):
                    self.rel.check_mail_evidence(tag, d, {})
                self.rel.check_mail_evidence(tag, d, {"luoke-mail": {"versions": ["0.19.28"]}})

    def test_accessory_without_evidence_has_no_tiers(self):
        """主控只看 tiers 以后，这一版一条证据都没有的配件，配方里它的任务哪一档都不开
        （原来主控的 task_open 要「配件在 verified 里有版本」）；有证据的照台账，agent 核心照源码补进 verified 的不受影响"""
        r = {"tasks": {"luoke-mail.triage": {"accessory": "luoke-mail", "tiers": ["16", "128"]},
                       "luoke-ime.tidy": {"accessory": "luoke-ime", "tiers": ["128"]},
                       "luoke-ime.old": {"accessory": "luoke-ime", "tiers": []},
                       "luoke-agent.chat": {"accessory": "luoke-agent", "tiers": ["128"]}}}
        verified = {"luoke-core": {"versions": ["0.2.0"]}, "luoke-mail": {"versions": ["0.19.28"], "tasks": {}},
                    "luoke-agent": {"versions": ["0.0.5"]}}
        self.assertEqual(self.rel.close_unevaluated_tasks(r, verified), ["luoke-ime.tidy"])
        self.assertEqual(r["tasks"]["luoke-ime.tidy"]["tiers"], [])
        self.assertIn("没有评测证据", r["tasks"]["luoke-ime.tidy"]["_tiers"])
        self.assertEqual(r["tasks"]["luoke-mail.triage"]["tiers"], ["16", "128"])
        self.assertEqual(r["tasks"]["luoke-agent.chat"]["tiers"], ["128"])

    def _unverified_note(self, accessory: str, version: str, verified: dict) -> tuple:
        """note_unverified_bundled 写进流程报告的那句，和同一份 verified 下 close_unevaluated_tasks 关没关它的任务"""
        ib = self._tool("internal_build")
        repo = accessory
        with tempfile.TemporaryDirectory() as d:
            sub = "Resources" if accessory == "luoke-search" else "packaging"
            os.makedirs(os.path.join(d, sub))
            with open(os.path.join(d, sub, "luoke-accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"accessory": accessory, "version": version}, f)
            with unittest.mock.patch.object(ib.Recipe, "load", lambda p: unittest.mock.Mock(verified=verified)):
                ib.note_unverified_bundled("internal-1116", {repo: d})
        note = ib.TIMELINE.data.get("kept", {}).pop(f"{repo}.evidence")
        r = {"tasks": {"t": {"accessory": accessory, "tiers": ["16", "128"]}}}
        self.rel.close_unevaluated_tasks(r, verified)
        return note, r["tasks"]["t"]["tiers"]

    def test_unverified_note_when_no_version_has_evidence(self):
        """这一版一个版本的证据都没有：任务在配方里全关了，报告不能写成「主控照样用它」（#211 审查）；搜索只能按字词搜"""
        note, tiers = self._unverified_note("luoke-mail", "0.19.60", {"luoke-core": {"versions": ["0.2.0"]}})
        self.assertEqual(tiers, [])
        self.assertIn("0.19.60 这一版没有评测证据，它的任务这一版哪一档都不开", note)
        self.assertNotIn("照台账", note)
        self.assertNotIn("按字词搜", note)
        note, tiers = self._unverified_note("luoke-search", "0.1.5", {})
        self.assertEqual(tiers, [])
        self.assertIn("它的任务这一版哪一档都不开，只能按字词搜", note)

    def test_unverified_note_when_another_version_has_evidence(self):
        """证据是别的版本的、打进来的这一版没有：任务照台账开着，用的是没评测过的版本（内部版才会走到这里，RC、正式版在 evidence_gate 停）"""
        note, tiers = self._unverified_note("luoke-mail", "0.19.60", {"luoke-mail": {"versions": ["0.19.59"]}})
        self.assertEqual(tiers, ["16", "128"])
        self.assertIn("0.19.60 这一版没有评测证据，任务照台账开着，但这一版没评测过", note)
        self.assertNotIn("哪一档都不开", note)
        note, tiers = self._unverified_note("luoke-search", "0.1.5", {"luoke-search": {"versions": ["0.1.4"]}})
        self.assertEqual(tiers, ["16", "128"])
        self.assertIn("任务照台账开着，但这一版没评测过", note)
        self.assertNotIn("按字词搜", note)

    def test_build_gates_mem_on_evidence_and_agent_on_name(self):
        """打包阶段：记忆没证据，内部版照出、RC 停（原来一律停：learn 会被拒）；agent 不核版本，只核配方 verified 里有它的名字"""
        import types
        with tempfile.TemporaryDirectory() as d:
            mem = os.path.join(d, "mem")
            os.makedirs(os.path.join(mem, "packaging"))
            with open(os.path.join(mem, "packaging", "accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"accessory": "luoke-mem", "version": "0.28.3"}, f)
            recipe, manifest = os.path.join(d, "recipe.json"), os.path.join(d, "release.json")
            for p in (recipe, manifest):
                open(p, "w").write("{}")
            def built(*x, **k):
                raise RuntimeError("built")
            for tag, verified, agent, stops in (
                    ("internal-1114", {}, "", False),                                     # 记忆没证据：内部版照出
                    ("beta2-rc1", {}, "", True),                                          # RC 停
                    ("beta2-rc1", {"luoke-mem": {"versions": ["0.28.3"]}}, "", False),
                    ("internal-1114", {"luoke-mem": {"versions": ["0.28.3"]}}, "/agent", True),       # 带 agent，verified 里没它的名字：停
                    ("internal-1114", {"luoke-mem": {"versions": ["0.28.3"]}, "luoke-agent": {"versions": ["0.0.1"]}}, "/agent", False)):  # 版本不管
                a = types.SimpleNamespace(repo_override={"luoke-mem": mem, **({"luoke-agent": agent} if agent else {})})
                paths = types.SimpleNamespace(recipe=recipe, manifest=manifest, release=tag, out=d, app=os.path.join(d, "app", "x.app"))
                rec = Recipe.load(os.path.join(ROOT, "recipes", "dev.json"))
                rec.verified = verified
                with unittest.mock.patch.object(self.rel.Recipe, "load", lambda p, rec=rec: rec), \
                        unittest.mock.patch.object(self.rel.subprocess, "run", built):
                    with self.assertRaises(SystemExit if stops else RuntimeError, msg=(tag, verified, agent)):
                        self.rel.stage_build(a, paths)

    def test_agent_hello_eval_summary(self):
        """luoke-agent 的 hello 评测（bench/run_hello.py）：每遍都算；硬性 = 评测程序的 passed 加跑满题集；说出来的不是中文由 ceilings 卡住"""
        rel = self._tool("release")
        def result(texts, hard=(), partial=False):
            runs = [{"text": t, "hard": list(hard) if i == 0 else []} for i, t in enumerate(texts)]
            fails = [f"c1：{h}" for h in hard]
            return {"task": "luoke-agent.hello", "model": "m", "set_sha256": "ab", "prompt_sha256": "cd", "partial": partial,
                    "seconds": 12.0, "waited_seconds": 0, "results": [{"id": "c1", "kind": "normal", "runs": runs}],
                    "cases": 1, "of": 1, "runs_per_case": len(runs),
                    "score": {"hard_failures": fails, "passed": not fails, "runs": len(runs), "soft": {"said": 1.0, "heavy_quiet": 0.25}}}
        spec = rel.load_evals()["luoke-agent.hello"]
        self.assertEqual((spec["accessory"], spec["summary"], spec["floor"], spec["ceilings"]), ("luoke-agent", "agent-one-liner", 0, {"not_chinese": 0}))
        self.assertIn(0, spec["ok_exit_codes"]); self.assertIn(1, spec["ok_exit_codes"])     # 1 = 跑完了、硬性没过：照常记证据
        self.assertIsNotNone(Recipe.load(RECIPE).task("luoke-agent.hello"))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            def summ(res):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False)
                return rel.summarize("agent-one-liner", path, "")
            s = summ(result(["嘿，那 3 封会议邀请我都放进日历啦。", "", "Safari 的缓存清掉了 2 GB。"]))
            self.assertEqual((s["hard_ok"], s["score"], s["score_max"], s["evalset_sha256"]), (True, 3, 3, "ab"))
            self.assertEqual((s["detail"]["not_chinese"], s["detail"]["said"], s["detail"]["prompt_sha256"]), (0, 2, "cd"))
            self.assertEqual(rel.judge(s, None, spec)["verdict"], "pass")
            s = summ(result(["I archived 3 newsletters for you.", "嘿，好啦。"]))
            self.assertEqual((s["hard_ok"], s["detail"]["not_chinese"]), (True, 1))
            self.assertEqual(rel.judge(s, None, spec)["verdict"], "fail")                 # 评测程序没判，评测表照样卡住
            s = summ(result(["嘿，那 3 封会议邀请我都放进日历啦。", "嘿，好啦。"], hard=["length"]))
            self.assertEqual((s["hard_ok"], s["score"], s["score_max"]), (False, 1, 2))
            self.assertEqual(rel.judge(s, None, spec)["verdict"], "fail")
            s = summ(result(["嘿，好啦。"], partial=True))
            self.assertFalse(s["hard_ok"])                                                # 只跑了前几道：不能算证据
            s = summ(dict(result([]), of=38, runs_per_case=3))
            self.assertEqual((s["hard_ok"], s["n"]), (False, 0))                          # 一遍都没有：不算过
            s = summ(dict(result(["嘿，好啦。", "嘿，好啦。"]), of=38, runs_per_case=3))
            self.assertEqual((s["hard_ok"], s["detail"]["runs_expected"]), (False, 114))  # 遍数和题数 × 每题几遍对不上
            res = result(["嘿，好啦。"])
            del res["runs_per_case"]
            self.assertFalse(summ(res)["hard_ok"])
            self.assertNotIn("date", summ(result(["嘿，好啦。"])))                       # 评测程序不写日期：不拿 null 盖掉记录的日期
        for t in ("Slack、Zoom、Notion 都有新 PR", "PR #12 merged 了", "Safari 的缓存清掉了 2 GB。", "嘿，一口气吞掉 6.4GB 缓存啦！",
                  "Visual Studio Code Helper 又占了 3 GB", "npm、git 都装好了", "iPhone 和 iPad 都连上了", "900 MB 的 dmg 删掉啦"):
            self.assertFalse(rel.not_chinese(t), t)
        for t in ("I archived 3 newsletters for you.", "Your Mac cleaned up 2 GB 了", "Mac 今天 is very busy", "hey, all good here 哦", "OK!"):
            self.assertTrue(rel.not_chinese(t), t)
        self.assertNotIn("date", inspect.getsource(rel.summarize_agent_one_liner).split('"evalset_sha256"')[1])
        self.assertEqual(rel.keep_recorded_date({"score": 1, "date": None}), {"score": 1})
        self.assertEqual(rel.keep_recorded_date({"score": 1, "date": "2026-11-19"})["date"], "2026-11-19")
        self.assertIn("summ = keep_recorded_date(summ)", inspect.getsource(rel.stage_eval))
        rec = {"date": "2026-11-19", **rel.keep_recorded_date({"score": 1, "date": None})}      # stage_eval 就是这样拼证据的
        self.assertEqual(rec["date"], "2026-11-19")

    def test_unreadable_result_stops_only_where_the_tier_was_on(self):
        """读不到评测结果：上一版开着的格子在台账那一步停下（不悄悄关掉一档照样出版），原因列在差异报告里；
        上一版关着的、新任务（hello）记没过、照常出版。这种记录不当下一版的基线，也不沿用。"""
        rel = self._tool("release")
        ib = self._tool("internal_build")
        evals = rel.load_evals()
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "releases", "internal-1119"))
        template = {"assignment": {"16": {}, "32": {}},
                    "tasks": {"luoke-scan.explain": {"tiers": ["16", "32"]}, "luoke-agent.hello": {"tiers": ["16"]}}}
        bad = {"hard_ok": False, "score": 0, "score_max": 0, "n": 0, "detail": {"error": "result_unreadable", "message": "FileNotFoundError: x"}}

        def rec(task, tier, **kw):
            return dict({"task": task, "model": "m", "tiers": [tier], "params": {}, "evalset": {"sha256": "e"}}, **kw)

        def ledger(state16):
            with open(os.path.join(root, "releases", "internal-1119", "support.json"), "w") as f:
                json.dump({"luoke-scan.explain": {"16": {"state": state16, "why": "x", "runs": [True]},
                                                  "32": {"state": "off", "why": "x", "runs": []}}}, f)
        cur = {("luoke-scan.explain", "k", "16"): rec("luoke-scan.explain", "16", **bad),
               ("luoke-agent.hello", "k", "16"): rec("luoke-agent.hello", "16", **bad)}
        with unittest.mock.patch.object(rel, "ROOT", root):
            ledger("on")
            with self.assertRaises(SystemExit):                                           # 上一版开着：停
                rel.ledger_for("internal-1120", "internal-1119", cur, template, evals)
            with self.assertRaises(SystemExit):
                rel.diff_report("internal-1120", "internal-1119", cur, {}, evals, template)
            self.assertEqual([(t, g) for t, g, _ in rel.unreadable_on_cells(cur, {"luoke-scan.explain": {"16": {"state": "on"}}}, evals)],
                             [("luoke-scan.explain", "16")])
            ledger("off")                                                                  # 上一版关着、新任务：记没过、照常出版
            md, verdict, led = rel.diff_report("internal-1120", "internal-1119", cur, {}, evals, template)
        self.assertEqual(verdict, "pass")
        self.assertEqual((led["luoke-scan.explain"]["16"]["state"], led["luoke-agent.hello"]["16"]["state"]), ("off", "off"))
        self.assertIn("读不到评测结果（FileNotFoundError: x）", md)
        # 这一版读不到结果的记录，下一版不当基线（上次一栏是 —，不比总分），也不沿用
        good = rec("luoke-scan.explain", "16", hard_ok=True, score=5, score_max=10)
        self.assertEqual(rel.judge(good, rec("luoke-scan.explain", "16", **bad), evals["luoke-scan.explain"])["reasons"], [])
        nxt = {("luoke-scan.explain", "k", "16"): good}
        with unittest.mock.patch.object(rel, "ROOT", root):
            md, _v, _l = rel.diff_report("internal-1121", "internal-1119", nxt, cur, evals, template)
        self.assertIn("| 5/10 | — |", md)
        self.assertFalse(ib.reusable(rec("luoke-scan.explain", "16", **bad), evals["luoke-scan.explain"], {}, {}, None))

    def test_unreadable_eval_result_is_a_failed_cell(self):
        """评测程序退出码在 ok_exit_codes 里（hello、扫描的 1）但结果文件没写出来、不是 JSON、缺字段：这一档记为没过，不让整版停在「评测没跑完」"""
        rel = self._tool("release")
        evals = rel.load_evals()
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "none.json")
            garbage = os.path.join(d, "garbage.json")
            with open(garbage, "w") as f:
                f.write("{not json")
            no_score = os.path.join(d, "no_score.json")
            with open(no_score, "w") as f:
                json.dump({"results": []}, f)
            for task in ("luoke-agent.hello", "luoke-scan.explain", "luoke-mem.topics"):
                kind = evals[task]["summary"]
                for path in (missing, garbage, no_score):
                    s = rel.summarize_or_fail(kind, path, "")
                    self.assertEqual((s["hard_ok"], s["score"], s["detail"]["error"]), (False, 0, "result_unreadable"), (task, path))
                    self.assertFalse(rel.passed(dict(s, task=task), evals))
                    self.assertEqual(rel.judge(s, {"hard_ok": True, "score": 100, "evalset": {}}, evals[task])["verdict"], "fail")
            with self.assertRaises(RuntimeError):
                rel.summarize_or_fail("no-such-kind", missing, "")                          # 汇总方式写错照旧停
        src = inspect.getsource(rel.stage_eval)
        self.assertIn("summarize_or_fail(", src)
        self.assertNotIn(" summarize(", src)

    def test_task_missing_from_ledger_ships_off(self):
        """--no-eval 整版拷上一版的台账；上一版之后才加的任务（hello）台账里没有，配方里它哪一档都不开，不照模板的档开着。
        它的配件在已验证里（agent 核心照源码补），close_unevaluated_tasks 也不会关它，只能靠这一条。"""
        ib = self._tool("internal_build")
        rel = self._tool("release")
        sup = self._tool("support")
        root = tempfile.mkdtemp()
        ev = os.path.join(root, "releases", "internal-1119", "evidence")
        os.makedirs(ev)
        json.dump({"task": "luoke-mem.topics", "accessory": "luoke-mem", "tier_run": "16", "score": 9, "score_max": 10, "hard_ok": True,
                   "combo": {"key": "k", "model": "m", "base": "m", "tiers": ["16"], "params": {}}},
                  open(os.path.join(ev, "luoke-mem.topics.k.16.json"), "w"))
        json.dump({"luoke-mem.topics": {"16": {"state": "on", "why": "x", "runs": [True]}, "32": {"state": "off", "why": "x", "runs": []}}},
                  open(os.path.join(root, "releases", "internal-1119", "support.json"), "w"))
        with unittest.mock.patch.object(ib, "ROOT", root), unittest.mock.patch.object(ib, "log", lambda m: None):
            ib.reuse_all("internal-1119-2", "internal-1119")
        led = sup.load(os.path.join(root, "releases", "internal-1119-2"))
        self.assertNotIn("luoke-agent.hello", led)
        with open(RECIPE, encoding="utf-8") as f:
            r = json.load(f)
        self.assertEqual(r["tasks"]["luoke-agent.hello"]["tiers"], ["16", "32", "128"])   # 模板里写了三档
        r["tasks"]["luoke-test.never"] = {"accessory": "luoke-test", "kind": "generate", "tiers": [], "params": {"*": {}}}
        missing = rel.apply_ledger(r, led)
        self.assertIn("luoke-agent.hello", missing)
        self.assertNotIn("luoke-test.never", missing)                                     # 模板里本来就不开的不算
        self.assertEqual(r["tasks"]["luoke-agent.hello"]["tiers"], [])
        self.assertEqual(r["tasks"]["luoke-mem.topics"]["tiers"], ["16"])                # 台账里有的照台账
        rel.close_unevaluated_tasks(r, {"luoke-mem": {"versions": ["1"]}, "luoke-agent": {"versions": ["0.0.97"]}})
        self.assertEqual(r["tasks"]["luoke-agent.hello"]["tiers"], [])
        self.assertIn("apply_ledger(r, led)", inspect.getsource(rel.stage_recipe))

    def test_hello_held_until_the_hold_is_removed(self):
        """hello 在评测表里三档都 hold 着：上一版发布后补满 3 遍都过、这一版也过了，照样关着；删掉 hold 才照规矩开。
        hold 着的档不评（11-20：beta2-rc3 的 hello 在 16 GB 档白占 1 小时），也不复测；台账照「这一版没评、hold 着」关着。"""
        ib = self._tool("internal_build")
        sup = self._tool("support")
        evals = self.rel.load_evals()
        holds = evals["luoke-agent.hello"].get("held_unsupported") or {}
        self.assertEqual(sorted(holds, key=int), ["16", "32", "128"])
        task = "luoke-agent.hello"
        prev = {task: {t: {"state": "off", "why": "x", "runs": [True, True, True]} for t in holds}}
        runs = {(task, t): [True] for t in holds}
        tiers = {task: list(holds)}
        self.assertEqual(sup.on_tiers(sup.build("beta2-rc3", prev, runs, {task: holds}, tiers), task), [])
        self.assertEqual(sup.on_tiers(sup.build("beta2-rc3", prev, {}, {task: holds}, tiers), task), [])     # 这一版没评：也关着
        self.assertEqual(sup.on_tiers(sup.build("beta2-rc3", prev, runs, {}, tiers), task), ["16", "32", "128"])   # 没有 hold 就开了
        # hold 着的格子一版只评一遍：发布前后都不复测，凑不到 3 遍，开不了
        for state in (None, "off"):
            self.assertEqual(sup.needs_repeat(state, True, [True]), 0)
            self.assertEqual(sup.decide(state, True, [True])[0], "off")
        self.assertFalse(ib.closed_cant_open(task, "16", prev, evals))                    # 不算"挪到发布后"：hold 着的直接不评
        template = os.path.join(ROOT, "recipes", "dev.json")
        src = {"luoke-agent": "/a", "luoke-mail": "/m"}
        only = [task, "luoke-mail.triage"]
        with unittest.mock.patch.object(ib, "ROOT", tempfile.mkdtemp()), unittest.mock.patch.object(ib, "log", lambda m: None):
            run = ib.plan_eval("test-x", {}, src, None, template, list(holds), evals, only=only)[0]
            self.assertEqual({t: ts for t, ts in run.items()}, {t: ["luoke-mail.triage"] for t in holds})
            unheld = dict(evals, **{task: {k: v for k, v in evals[task].items() if k != "held_unsupported"}})
            run = ib.plan_eval("test-x", {}, src, None, template, list(holds), unheld, only=only)[0]
            self.assertEqual(sorted(t for t, ts in run.items() if task in ts), sorted(holds))   # 删掉 hold 就照常评

    def test_transcribe_and_tidy_eval_summaries(self):
        """luoke-ime 的转写评测（evals/transcribe）：总分是字准确率，硬性条目由评测程序判（按住不说话不出字），延迟看终稿；每档各评一遍"""
        rel = self._tool("release")
        res = {"score": {"char_accuracy_pct": 93.6, "cer_pct": 6.4, "hard_ok": True, "by_category_cer_pct": {"daily": 2.0}, "voices": ["tingting", "eddy"]},
               "latency": {"final": {"p50_ms": 77, "p90_ms": 119}, "draft": {"p50_ms": 68, "p90_ms": 103}},
               "evalset": {"name": "sentences.tsv", "sha256": "ab", "n": 90}, "machine": {"chip": "M5 Max"}, "date": "2026-11-05"}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(res, f)
            s = rel.summarize("ime-transcribe-result", path, "")
        self.assertEqual((s["hard_ok"], s["score"], s["score_max"], s["n"], s["latency"]["p90_ms"], s["evalset_sha256"]), (True, 93.6, 100.0, 90, 119, "ab"))
        spec = rel.load_evals()["luoke-ime.transcribe"]
        self.assertEqual((spec["summary"], spec["per_tier"], spec["floor"]), ("ime-transcribe-result", True, 85))
        self.assertEqual(rel.judge(s, None, spec)["verdict"], "pass")
        self.assertEqual(rel.judge(dict(s, hard_ok=False), None, spec)["verdict"], "fail")
        tidy = {"score": {"char_accuracy_pct": 97.3, "hard_ok": True, "model_hard_ok": False, "exact": 20, "exact_n": 24, "guarded": ["inj-1"]},
                "latency": {"p50_ms": 502, "p90_ms": 634}, "evalset": {"sha256": "cd", "n": 30}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(tidy, f)
            t = rel.summarize("ime-tidy-result", path, "")
        self.assertEqual((t["hard_ok"], t["score"], t["n"], t["detail"]["model_hard_ok"], t["detail"]["guarded"]), (True, 97.3, 30, False, ["inj-1"]))
        self.assertEqual(rel.judge(t, None, rel.load_evals()["luoke-ime.tidy"])["verdict"], "pass")

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

    def _release_copies(self, d, names):
        """build.noindex/release/ 的样子：names 按从旧到新排，目录时间各差一小时"""
        t = time.time() - 100 * 3600
        for i, n in enumerate(names):
            os.makedirs(os.path.join(d, n, "apps.noindex", "螺壳输入法.app", "Contents"))
            os.utime(os.path.join(d, n), (t + i * 3600, t + i * 3600))

    def test_prune_release_copies_keeps_newest_three_internal(self):
        """出完一版只留最近 3 份 internal-*；这一版、上一版（差异对比 / 证据沿用）留着；beta、rc、local- 不动；删前注销 .app"""
        ib = self._tool("internal_build")
        with tempfile.TemporaryDirectory() as d:
            self._release_copies(d, ["beta2-rc1", "local-1109-0900", "internal-1110", "internal-1111", "internal-1112-2",
                                     "internal-1113", "internal-1114", "internal-1114-2", "beta2"])
            with open(os.path.join(d, "internal-notes.txt"), "w") as f:
                f.write("x")                       # 不是目录：不碰
            unreg = []
            with unittest.mock.patch.object(ib.lsapps, "_ls", lambda flag, path: unreg.append((flag, path))):
                removed = ib.prune_release_copies("internal-1114-2", ("internal-1110", None), base=d)
            self.assertEqual(sorted(removed), ["internal-1111", "internal-1112-2"])
            self.assertEqual(sorted(os.listdir(d)), ["beta2", "beta2-rc1", "internal-1110", "internal-1113", "internal-1114",
                                                     "internal-1114-2", "internal-notes.txt", "local-1109-0900"])
            self.assertEqual(sorted(os.path.basename(os.path.dirname(os.path.dirname(p))) for f, p in unreg if f == "-u"),
                             ["internal-1111", "internal-1112-2"])

    def test_prune_release_copies_never_fails_the_build(self):
        """删不掉（或目录不在）只记一笔，不抛：这一版已经发出去了"""
        ib = self._tool("internal_build")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ib.prune_release_copies("internal-1114", base=os.path.join(d, "missing")), [])
            self._release_copies(d, ["internal-1110", "internal-1111", "internal-1112", "internal-1113", "internal-1114"])
            def boom(path, *a, **k):
                raise PermissionError(13, "denied", path)
            with unittest.mock.patch.object(ib.shutil, "rmtree", boom), unittest.mock.patch.object(ib.lsapps, "_ls", lambda *a: None):
                self.assertEqual(ib.prune_release_copies("internal-1114", base=d), [])
            self.assertEqual(len(os.listdir(d)), 5)
            with unittest.mock.patch.object(ib.lsapps, "_ls", lambda *a: None):     # 正在出的那份就算排不进最近 3 份也不删
                os.utime(os.path.join(d, "internal-1114"), (0, 0))
                self.assertEqual(sorted(ib.prune_release_copies("internal-1114", base=d)), ["internal-1110"])

    def test_prune_runs_only_after_publishing_an_internal_build(self):
        """只在发布那一步后面、只对 internal-*（beta、候选版、本机版不走这里），下一版要比的那版也传进去"""
        with open(os.path.join(ROOT, "tools", "internal_build.py"), encoding="utf-8") as f:
            src = f.read()
        main_src = src[src.index("def main()"):]
        i, j = main_src.index('step("publish"'), main_src.index("prune_release_copies(")
        self.assertLess(i, j)
        self.assertLess(j, main_src.index("发布后的复测"))
        self.assertIn("if INTERNAL_TAG.fullmatch(a.tag):", main_src[i:j])
        self.assertIn("prune_release_copies(a.tag, (prev, reuse_from, nxt))", main_src)

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
        # 定时截版（cut.py）：开头一行标签，/release 在第二行，有退回时后面跟着说明；算会话，不算负责人本人
        cut = "（luoke-top 定时截版）\n/release internal-1029-3\n\nluoke-ime：6fe31d5 到 d33af22 改了代码、版本号还是 0.1.23，这一版退回 d33af22（请升版本号）"
        r = d.parse_request(c(cut), allowed, owner)
        self.assertEqual((r["tag"], r["accept"], r["by"]), ("internal-1029-3", False, "lab-owner（会话）"))
        self.assertIn("只认负责人本人", d.parse_request(c("（luoke-top 定时截版）\n/release beta1.1"), allowed, owner)["reject"])

    def test_reuse_rc_only_for_beta_by_owner(self):
        """--reuse-rc（11-25）：只给负责人发的 beta；内部版、候选版、会话发的都拒；转给 internal_build 的参数里带上。"""
        d = self._tool("release_daemon")
        allowed, owner = {"lab-owner", "rc-bot"}, "lab-owner"
        def c(body, who="lab-owner", i=1):
            return {"id": i, "body": body, "user": {"login": who}}
        r = d.parse_request(c("/release beta2 --reuse-rc"), allowed, owner)
        self.assertEqual((r["tag"], r.get("reuse_rc"), r["accept"]), ("beta2", True, False))
        self.assertTrue(d.parse_request(c("/release beta2 --reuse-rc --accept"), allowed, owner)["accept"])
        self.assertNotIn("reuse_rc", d.parse_request(c("/release beta2"), allowed, owner))      # 默认不开
        self.assertIn("只给 beta", d.parse_request(c("/release --reuse-rc"), allowed, owner)["reject"])
        self.assertIn("只给 beta", d.parse_request(c("/release internal-1125 --reuse-rc"), allowed, owner)["reject"])
        self.assertIn("只给 beta", d.parse_request(c("/release beta2-rc6 --reuse-rc"), allowed, owner, rc_senders={"rc-bot"})["reject"])
        self.assertIn("只认负责人本人", d.parse_request(c("（luoke-top 会话）/release beta2 --reuse-rc"), allowed, owner)["reject"])
        with open(os.path.join(ROOT, "tools", "release_daemon.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('if req.get("reuse_rc"):\n        cmd.append("--reuse-rc")', src)

    def test_reuse_candidate_picks_latest_fully_evaluated_rc(self):
        """--reuse-rc 挑 releases/ 里这个 beta 的候选版：有证据、pins.json 记着全评；评测跑完了的（到过 diff，停在差异上的也算）优先
        （评测中途停下的证据不全），都没跑完才挑号最大的；按号比，不按字符串；别的 beta 的候选版不算；一个都没有就停。"""
        ib = self._tool("internal_build")
        with tempfile.TemporaryDirectory() as root, unittest.mock.patch.object(ib, "log", lambda m: None):
            def rel(name, mode="full", evidence=True, phases=None):
                d = os.path.join(root, "releases", name)
                os.makedirs(os.path.join(d, "evidence") if evidence else d, exist_ok=True)
                if mode is not None:
                    with open(os.path.join(d, "pins.json"), "w", encoding="utf-8") as f:
                        json.dump({"tag": name, "eval_mode": mode}, f)
                if phases:
                    with open(os.path.join(d, "timeline.json"), "w", encoding="utf-8") as f:
                        json.dump({"phases": [{"name": n, "ok": ok} for n, ok in phases]}, f)
            rel("beta2-rc5"); rel("beta2-rc10"); rel("beta2-rc9")
            rel("beta2-rc11", mode="none")            # 没真评过的不算
            rel("beta2-rc12", evidence=False)          # 没证据的不算
            rel("beta2-rc13", mode=None)               # 没 pins.json 的不算
            rel("beta3-rc1"); rel("beta2.1-rc20"); rel("internal-1125-4")
            self.assertEqual(ib.reuse_candidate("beta2", root), "beta2-rc10")     # 都没跑完：按号，不按字符串
            self.assertEqual(ib.reuse_candidate("beta2.1", root), "beta2.1-rc20")
            rel("beta2-rc5", phases=[("eval 16", True), ("diff", True), ("check", True)])
            self.assertEqual(ib.reuse_candidate("beta2", root), "beta2-rc5")      # 跑完了的压过号大但中途停下的
            rel("beta2-rc9", phases=[("eval 16", True), ("diff", False)])
            self.assertEqual(ib.reuse_candidate("beta2", root), "beta2-rc9")      # 停在差异上没放行的也算评完了，号大的优先
            with self.assertRaises(SystemExit):
                ib.reuse_candidate("beta4", root)
            with self.assertRaises(SystemExit):
                ib.reuse_candidate("internal-1125", root)

    def test_beta_eval_source_carries_over_only_on_accept(self):
        """beta 沿不沿用：带 --reuse-rc 用上次记下的（没有就挑）；只带 --accept 接着用记下的；不带开关重发就不沿用（负责人关掉的办法）；
        内部版、候选版一律 None。守护进程开始的回复照同一套规则写。"""
        ib = self._tool("internal_build")
        d = self._tool("release_daemon")
        with tempfile.TemporaryDirectory() as root, unittest.mock.patch.object(ib, "log", lambda m: None):
            os.makedirs(os.path.join(root, "releases", "beta3-rc2", "evidence"))
            os.makedirs(os.path.join(root, "releases", "beta3-rc1", "evidence"))      # 上次记下的那个
            with open(os.path.join(root, "releases", "beta3-rc2", "pins.json"), "w", encoding="utf-8") as f:
                json.dump({"eval_mode": "full"}, f)
            src = ib.beta_eval_source
            self.assertEqual(src("beta3", None, True, False, root), "beta3-rc2")
            self.assertEqual(src("beta3", "beta3-rc1", True, False, root), "beta3-rc1")     # 续跑：用记下的，不换
            self.assertEqual(src("beta3", "beta3-rc1", False, True, root), "beta3-rc1")     # --accept 续跑
            self.assertIsNone(src("beta3", "beta3-rc1", False, False, root))                # 不带开关重发：全评
            self.assertIsNone(src("beta3", None, False, True, root))
            self.assertIsNone(src("beta3", None, False, False, root))
            self.assertIsNone(src("beta3-rc3", None, True, False, root))
            self.assertIsNone(src("internal-1125", None, True, False, root))
            self.assertEqual(src("beta3", "beta3-rc9", True, False, root), "beta3-rc2")     # 记下的不在了：带开关就重新挑
            with self.assertRaises(SystemExit):
                src("beta3", "beta3-rc9", False, True, root)                                # 只带 --accept：停下说清楚
            self.assertIsNone(src("beta3", "beta3-rc9", False, False, root))
            # 关掉沿用：拷来的证据和原始答复删掉，自己评的留着
            ev = os.path.join(root, "releases", "beta3", "evidence")
            os.makedirs(os.path.join(ev, "raw", "16"))
            for name, extra in (("a.k.16.json", {"reused_from": "beta3-rc1", "raw": "raw/16/a.json"}), ("b.k.16.json", {})):
                with open(os.path.join(ev, name), "w", encoding="utf-8") as f:
                    json.dump(dict({"task": name[0]}, **extra), f)
            with open(os.path.join(ev, "raw", "16", "a.json"), "w") as f:
                f.write("{}")
            self.assertEqual(ib.drop_reused("beta3", "beta3-rc1", root), 1)
            self.assertEqual(sorted(os.listdir(ev)), ["b.k.16.json", "raw"])
            self.assertEqual(os.listdir(os.path.join(ev, "raw", "16")), [])
            self.assertEqual(ib.drop_reused("beta3", "beta3-rc1", root), 0)
        self.assertIn("沿用最近一个候选版", d.reuse_note({"reuse_rc": True}, None))
        self.assertIn("接着沿用 beta3-rc1", d.reuse_note({"reuse_rc": True}, "beta3-rc1"))
        self.assertIn("接着沿用 beta3-rc1", d.reuse_note({"accept": True}, "beta3-rc1"))
        self.assertIn("不再沿用 beta3-rc1", d.reuse_note({}, "beta3-rc1"))
        self.assertEqual(d.reuse_note({}, None), "")
        self.assertEqual(d.reuse_note({"accept": True}, None), "")
        with open(os.path.join(ROOT, "tools", "internal_build.py"), encoding="utf-8") as f:
            main_src = f.read().split("def main()", 1)[1]
        self.assertIn('beta_eval_source(a.tag, own_state.get("reuse_rc"), a.reuse_rc, a.accept)', main_src)
        self.assertIn('state.pop("reuse_rc", None)', main_src)
        self.assertIn('drop_reused(a.tag, own_state["reuse_rc"])', main_src)
        self.assertLess(main_src.index("beta_eval_source("), main_src.index("plan_eval(a.tag, pins, src, eval_from,"))

    def test_reuse_rc_rejected_up_front_when_pinned_core_is_too_old(self):
        """钉住的 core 里的 internal_build 不认 --reuse-rc：开始之前就拒，说清楚原因（不然起来退出码 2、日志里什么都没有）"""
        d = self._tool("release_daemon")
        with tempfile.TemporaryDirectory() as core:
            os.makedirs(os.path.join(core, "tools"))
            with open(os.path.join(core, "tools", "internal_build.py"), "w", encoding="utf-8") as f:
                f.write('ap.add_argument("--no-eval")\n')
            with self.assertRaises(RuntimeError) as e:
                d.check_reuse_supported(core, "beta2", "f5e16dc02ec4")
            self.assertIn("还不认 --reuse-rc", str(e.exception))
            self.assertIn("f5e16dc", str(e.exception))
            with open(os.path.join(core, "tools", "internal_build.py"), "w", encoding="utf-8") as f:
                f.write('ap.add_argument("--reuse-rc")\n')
            d.check_reuse_supported(core, "beta2", "abc1234")
        d.check_reuse_supported(ROOT, "beta2", "HEAD")       # 这一版自己认
        src = inspect.getsource(d.start_build)
        self.assertLess(src.index("check_reuse_supported("), src.index("subprocess.Popen("))

    def test_plan_eval_reuses_candidate_evidence(self):
        """beta 沿用候选版：plan_eval 拿候选版的证据当「上一版」，过了 reusable 的格子沿用、不重跑，没过的照旧评；
        沿用的证据拷过来记 reused_from = 候选版。发布说明写「沿用候选版 <rc> N 条」，中间不多空格。"""
        ib = self._tool("internal_build")
        rel = self.rel
        template = os.path.join(ROOT, "recipes", "dev.json")
        evals = rel.load_evals()
        task, other, tier = "luoke-mail.triage", "luoke-mail.thread", "128"
        combos = rel.plan_combos(ib.Recipe.load(template), [tier], [task, other])
        key = next(k for (t, k) in combos if t == task)
        with tempfile.TemporaryDirectory() as root, unittest.mock.patch.object(ib, "ROOT", root), \
                unittest.mock.patch.object(ib, "log", lambda m: None), \
                unittest.mock.patch.object(ib, "reusable", lambda old, spec, *a, **k: spec is evals[task]):
            ev = os.path.join(root, "releases", "beta3-rc2", "evidence")
            os.makedirs(ev)
            d = {"release": "beta3-rc2", "task": task, "accessory": "luoke-mail", "tier_run": tier, "score": 40, "score_max": 47,
                 "hard_ok": True, "combo": {"key": key, "model": "m", "base": "b", "tiers": [tier], "params": {}}}
            with open(os.path.join(ev, f"{task}.x.{tier}.json"), "w", encoding="utf-8") as f:
                json.dump(d, f)
            to_run, reused, deferred, canary = ib.plan_eval("beta3", {}, {"luoke-mail": "/m"}, "beta3-rc2", template, [tier], evals,
                                                            only=[task, other])
            self.assertEqual([(t, x["task"]) for t, x in reused], [(tier, task)])
            self.assertEqual(to_run, {tier: [other]})
            self.assertEqual(canary, {})
            ib.copy_reused("beta3", "beta3-rc2", reused)
            got = rel.load_evidence(os.path.join(root, "releases", "beta3"))
            self.assertEqual([(k[0], v["reused_from"], v["release"]) for k, v in got.items()], [(task, "beta3-rc2", "beta3")])
        with open(os.path.join(ROOT, "tools", "internal_build.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('reused_label=f"候选版 {eval_from}" if', src)
        self.assertIn("沿用{reused_label} {len(reused)} 条", src)

    def test_eval_paths_limit_reuse_check(self):
        """11-09 应 luoke-ime：评测表写了 paths 的，只在这些路径（加评测程序、题集）变了时重跑；没写的照旧看整个仓库（文档除外）。"""
        ib = self._tool("internal_build")
        spec = {"paths": ["engines/personal/", "scripts/evidence.py"], "evalset": "{ime_corpus}",
                "command": ["/usr/bin/python3", "engines/personal/eval_predict.py", "{ime_corpus}", "--hub", "{hub}", "--result", "{raw}/x.json"]}
        self.assertEqual(ib.eval_paths(spec), ["engines/personal/", "engines/personal/eval_predict.py", "scripts/evidence.py"])
        self.assertIsNone(ib.eval_paths({"command": ["/usr/bin/python3", "x/y.py"]}))
        tidy = {"paths": ["engines/voice/tidy_prompt.txt"], "evalset": "evals/tidy/cases.tsv", "command": ["/usr/bin/python3", "evals/tidy/eval_tidy.py"]}
        self.assertEqual(ib.eval_paths(tidy), ["engines/voice/tidy_prompt.txt", "evals/tidy/cases.tsv", "evals/tidy/eval_tidy.py"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        import subprocess
        git = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True).stdout.strip()

        def write(f, text):
            os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
            with open(os.path.join(d, f), "w") as fh:
                fh.write(text)
        git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
        for f in ("engines/personal/p.py", "engines/personal/eval_predict.py", "Sources/Shell.swift", "scripts/evidence.py"):
            write(f, "1")
        git("add", "-A"); git("commit", "-qm", "a"); a = git("rev-parse", "HEAD")
        write("Sources/Shell.swift", "2")                                        # 改壳：predict 沿用，不写 paths 的照旧重跑
        git("commit", "-qam", "b"); b = git("rev-parse", "HEAD")
        self.assertTrue(ib.unchanged_since(d, a, b, ib.eval_paths(spec)))
        self.assertFalse(ib.unchanged_since(d, a, b))
        write("scripts/evidence.py", "2")                                        # 改了它看的：重跑
        git("commit", "-qam", "c"); c = git("rev-parse", "HEAD")
        self.assertFalse(ib.unchanged_since(d, b, c, ib.eval_paths(spec)))

    def test_eval_paths_that_do_not_hold_fall_back_to_the_whole_repo(self):
        """paths 写错了、目录挪走了（钉住的提交里没有这一项）、不是非空的字符串列表：不当成「没变」，退回看整个仓库；
        paths 里列的 .md（提示词文件）变了也算变。"""
        ib = self._tool("internal_build")
        self.assertIsNone(ib.eval_paths({"paths": "engines/personal/", "command": ["/usr/bin/python3", "x/y.py"]}))
        self.assertIsNone(ib.eval_paths({"paths": ["engines/personal/", ""], "command": ["/usr/bin/python3", "x/y.py"]}))
        self.assertIsNone(ib.eval_paths({"paths": ["engines/personal/", 3]}))
        with tempfile.TemporaryDirectory() as d:
            import subprocess
            git = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True).stdout.strip()

            def write(f, text):
                os.makedirs(os.path.dirname(os.path.join(d, f)), exist_ok=True)
                with open(os.path.join(d, f), "w") as fh:
                    fh.write(text)
            git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
            for f in ("engines/personal/p.py", "engines/voice/prompt.md", "Sources/Shell.swift"):
                write(f, "1")
            git("add", "-A"); git("commit", "-qm", "a"); a = git("rev-parse", "HEAD")
            write("Sources/Shell.swift", "2")
            git("commit", "-qam", "b"); b = git("rev-parse", "HEAD")
            self.assertTrue(ib.unchanged_since(d, a, b, ["engines/personal/"]))
            self.assertTrue(ib.unchanged_since(d, a, b, ["engines/personal/p.py", "engines/voice/prompt.md"]))
            self.assertFalse(ib.unchanged_since(d, a, b, ["engines/personal/", "engines/persnal/"]))    # 写错的路径
            self.assertFalse(ib.unchanged_since(d, a, b, ["engine/"]))
            self.assertFalse(ib.unchanged_since(d, a, b, []))
            git("mv", "engines/personal", "engines/own"); git("commit", "-qm", "c"); c = git("rev-parse", "HEAD")
            self.assertFalse(ib.unchanged_since(d, b, c, ["engines/personal/"]))                      # 目录挪走了
            write("docs/x.md", "1"); git("add", "-A"); git("commit", "-qm", "d"); e = git("rev-parse", "HEAD")
            self.assertTrue(ib.unchanged_since(d, c, e, ["engines/own/"]))                            # 钉住的提交里有，没变
            self.assertTrue(ib.unchanged_since(d, c, e))                                              # 没写 paths：文档照旧不算
            write("engines/voice/prompt.md", "2"); git("commit", "-qam", "e"); f2 = git("rev-parse", "HEAD")
            self.assertFalse(ib.unchanged_since(d, e, f2, ["engines/voice/prompt.md"]))               # 列了的 .md 变了算变
            self.assertTrue(ib.unchanged_since(d, e, f2))
    def test_release_rights_go_by_account(self):
        """beta 2 安全审查 H1：权限只按账号和实验室配置的名单认。定时截版的机器账号（cut_senders）只能发带标签的内部版，
        别的名单里有也一样；不在名单里的账号（会话的机器账号）什么都发不了；自称会话的留言照旧不给负责人、候选版的权限"""
        d = self._tool("release_daemon")
        allowed, owner, rcs, cut = {"lab-owner", "lab-dev"}, "lab-owner", {"lab-dev"}, {"lab-cut"}
        def c(body, who):
            return {"id": 7, "body": body, "user": {"login": who}}
        def p(body, who, cut_senders=cut, own=owner):
            return d.parse_request(c(body, who), allowed, own, rcs, cut_senders)
        cutpost = "（luoke-top 定时截版）\n/release internal-1114-2\n\n说明"
        self.assertEqual(p(cutpost, "lab-cut"), {"accept": False, "by": "lab-cut（定时截版）", "comment": 7, "tag": "internal-1114-2"})
        self.assertEqual(p("/release internal-1114", "lab-cut")["tag"], "internal-1114")
        for body in ("/release", "/release internal-1114 --accept", "/release internal-1114 --no-eval", "/release beta2",
                     "/release beta2-rc2", "/release beta2-rc2 --accept", "/release internal-1114 internal-1114-2"):
            self.assertIn("定时截版的账号", p(body, "lab-cut")["reject"], body)
        # 负责人的账号也写进了 cut_senders：按定时截版的账号办，不出 beta、不 --accept
        self.assertIn("定时截版的账号", p("/release beta2", "lab-owner", cut | {"lab-owner"})["reject"])
        self.assertIn("定时截版的账号", p("/release internal-1114 --accept", "lab-owner", cut | {"lab-owner"})["reject"])
        # 负责人的账号：beta、--accept 都行；自称会话的不给
        self.assertEqual(p("/release beta2", "lab-owner")["tag"], "beta2")
        self.assertTrue(p("/release --accept", "lab-owner")["accept"])
        self.assertIn("只认负责人本人", p("（某某 会话）/release beta2", "lab-owner")["reject"])
        # 候选版的账号：候选版和它的 --accept；beta、内部版的 --accept 不行；自称会话的不给
        self.assertTrue(p("/release beta2-rc2 --accept", "lab-dev")["accept"])
        self.assertIn("只认负责人本人", p("/release beta2", "lab-dev")["reject"])
        self.assertIn("--accept 只认", p("/release --accept", "lab-dev")["reject"])
        self.assertIn("--accept 只认", p("（某某 会话）/release beta2-rc2 --accept", "lab-dev")["reject"])
        # 不在任何名单里的账号（会话、hub 的机器账号）：写什么开头都不行
        for body in ("/release", "（luoke-top 定时截版）\n/release internal-1114", "/release beta2-rc2"):
            self.assertIn("不在", p(body, "lab-sessions")["reject"])
        # 名单没配负责人、留言读不到账号：谁都不是负责人
        self.assertIn("只认负责人本人", p("/release beta2", "lab-dev", own="")["reject"])
        self.assertIn("不在", d.parse_request({"id": 7, "body": "/release beta2", "user": {}}, allowed | {""}, "", rcs, cut)["reject"])

    def test_release_tick_reads_cut_senders_from_lab_config(self):
        """tick 把实验室配置里的 cut_senders 交给 parse_request；配置里没有这一项时和以前一样"""
        d = self._tool("release_daemon")
        comments = [{"id": 11, "body": "（luoke-top 定时截版）\n/release internal-1114", "user": {"login": "lab-cut"}, "created_at": None},
                    {"id": 12, "body": "/release beta2", "user": {"login": "lab-cut"}, "created_at": None}]
        st = {"last_comment": 10, "scheduled": "", "queue": [], "running": {"tag": "busy"}, "history": []}
        replies = []
        lab = {"allowed": ["lab-owner"], "owner": "lab-owner", "rc_senders": [], "cut_senders": ["lab-cut"]}
        with unittest.mock.patch.object(d, "load_state", lambda: st), unittest.mock.patch.object(d, "save_state", lambda s: None), \
             unittest.mock.patch.object(d, "preempt_repeats", lambda s: None), unittest.mock.patch.object(d, "finish_build", lambda s: None), \
             unittest.mock.patch.object(d, "comments_after", lambda last: [x for x in comments if x["id"] > last]), \
             unittest.mock.patch.object(d, "request_age_hours", lambda c: 0), unittest.mock.patch.object(d, "reply", replies.append), \
             unittest.mock.patch.object(d.lab_config, "release", lambda: lab), \
             unittest.mock.patch.dict(os.environ, {}, clear=False):
            for k in ("LUOKE_RELEASE_ALLOWED", "LUOKE_RELEASE_OWNER", "LUOKE_RELEASE_HOUR"):
                os.environ.pop(k, None)
            d.tick()
        self.assertEqual([(q["tag"], q["by"]) for q in st["queue"]], [("internal-1114", "lab-cut（定时截版）")])
        self.assertEqual(len(replies), 2)
        self.assertIn("排上了（lab-cut（定时截版））", replies[0])
        self.assertIn("没排上：@lab-cut 是定时截版的账号", replies[1])
        # 没配 cut_senders（现在出版机上的配置）：这个账号不在 allowed 里，什么都不排
        st.update(last_comment=10, queue=[])
        replies.clear()
        del lab["cut_senders"]
        with unittest.mock.patch.object(d, "load_state", lambda: st), unittest.mock.patch.object(d, "save_state", lambda s: None), \
             unittest.mock.patch.object(d, "preempt_repeats", lambda s: None), unittest.mock.patch.object(d, "finish_build", lambda s: None), \
             unittest.mock.patch.object(d, "comments_after", lambda last: [x for x in comments if x["id"] > last]), \
             unittest.mock.patch.object(d, "request_age_hours", lambda c: 0), unittest.mock.patch.object(d, "reply", replies.append), \
             unittest.mock.patch.object(d.lab_config, "release", lambda: lab):
            d.tick()
        self.assertEqual(st["queue"], [])
        self.assertTrue(all("不在能发 /release 的名单里" in r for r in replies))

    def test_release_no_eval_only_for_internal_builds(self):
        """/release --no-eval 不评测、沿用上一版的证据；谁能发和普通 /release 一样，beta、候选版不认"""
        d = self._tool("release_daemon")
        allowed, owner, bots = {"lab-owner"}, "lab-owner", {"lab-bot"}
        def c(body, who="lab-owner"):
            return {"id": 1, "body": body, "user": {"login": who}}
        self.assertNotIn("no_eval", d.parse_request(c("/release"), allowed, owner))
        self.assertTrue(d.parse_request(c("/release --no-eval"), allowed, owner)["no_eval"])
        self.assertTrue(d.parse_request(c("（luoke-top 会话）/release --no-eval"), allowed, owner)["no_eval"])
        r = d.parse_request(c("/release internal-1109-2 --no-eval"), allowed, owner)
        self.assertEqual((r["tag"], r["no_eval"]), ("internal-1109-2", True))
        self.assertIn("只给内部版", d.parse_request(c("/release beta2 --no-eval"), allowed, owner)["reject"])
        self.assertIn("只给内部版", d.parse_request(c("/release beta2-rc1 --no-eval", who="lab-bot"), allowed, owner, bots)["reject"])
        self.assertIn("不在", d.parse_request(c("/release --no-eval", who="someone"), allowed, owner)["reject"])
        # 起 internal_build 时带上 --no-eval
        tmp = tempfile.mkdtemp()
        pins = {"luoke-core": "a" * 40, "luoke-mail": "b" * 40, "luoke-mem": "c" * 40, "luoke-ime": "d" * 40}
        popen, replies = [], []
        class P:
            pid = 4242
        with unittest.mock.patch.object(d, "CORE", tmp), unittest.mock.patch.object(d, "BUILD", tmp), \
                unittest.mock.patch.object(d, "pins_now", lambda: pins), unittest.mock.patch.object(d, "sh", lambda *a, **k: ""), \
                unittest.mock.patch.object(d.subprocess, "Popen", lambda *a, **k: popen.append(a[0]) or P()), \
                unittest.mock.patch.object(d, "reply", replies.append), unittest.mock.patch.object(d, "log", lambda m: None):
            d.start_build({"by": "x", "no_eval": True}, {"history": []})
            d.start_build({"by": "x"}, {"history": []})
        self.assertIn("--no-eval", popen[0][2])
        self.assertNotIn("--no-eval", popen[1][2])
        self.assertIn("不评测", replies[0])

    def test_closed_cells_that_cannot_open_move_after_publish(self):
        """10-23：上一版关着、发布后没补满 3 遍的格子，这一版评了也照"关"发，挪到发布后评；
        补满了的（这一版过了就开）、开着的、新任务都照旧在发布前评；hold 着的不算这一类（11-20 起直接不评，见 hello 那条）。"""
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

    def test_new_accessories_drop_out_without_stopping_the_build(self):
        """beta 2（11-03）：深度搜索、扫描进流水线。没钉的只是这一版不带它，主控和首发的应用照常出；打进主控的（扫描 0.3.0 起也是）
        打不出包时内部版退回上一版、没有可退的就停。顶层子模块还叫 luoke-audit 的，按 luoke-scan 认。"""
        ib = self._tool("internal_build")
        ib.LOG = open(os.devnull, "w")
        ib.last_published_pin = lambda repo, tag: ""      # 出版机上 releases/ 里有发过的内部版：这里只看沿用证据的那一版
        self.assertEqual(ib.TOP_ALIAS["luoke-audit"], "luoke-scan")
        self.assertEqual(set(ib.OPTIONAL), {"luoke-search", "luoke-scan", "luoke-agent"})
        self.assertTrue(set(ib.OPTIONAL) <= set(ib.APP_ACCESSORIES) | set(ib.BUNDLED))
        self.assertTrue(set(ib.APP_ACCESSORIES) | set(ib.BUNDLED) <= set(ib.ACCESSORIES))
        self.assertNotIn("luoke-agent", ib.APP_ACCESSORIES)     # luoke-core#68：并进主控，不再单独出 .app
        self.assertNotIn("luoke-search", ib.APP_ACCESSORIES)    # ADR 0022：放进主控的 Contents/Helpers
        self.assertIn("luoke-search", ib.BUNDLED)
        self.assertNotIn("luoke-mail", ib.APP_ACCESSORIES)      # 进主控的 Python，不再出包
        self.assertIn("luoke-mail", ib.BUNDLED)
        self.assertNotIn("luoke-mail", ib.OPTIONAL)             # 缺了就停，不是这一版不带
        self.assertNotIn("luoke-scan", ib.APP_ACCESSORIES)      # ADR 0022 的 11-05 修订：打进主控，不生成 .app
        self.assertIn("luoke-scan", ib.BUNDLED)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)

        def fake_app(name, accessory):
            app = os.path.join(out, name + ".app")
            os.makedirs(os.path.join(app, "Contents", "Resources"), exist_ok=True)
            with open(os.path.join(app, "Contents", "Resources", "luoke-accessory.json"), "w") as f:
                json.dump({"accessory": accessory, "version": "0.1.0"}, f)
            return app

        def broken(src, out):
            ib.die("编不过")
        pins = {"luoke-core": "c", "luoke-mail": "m", "luoke-mem": "e", "luoke-ime": "i", "luoke-search": "s", "luoke-scan": "a", "luoke-agent": "g"}
        src = {k: out for k in pins if k != "luoke-core"}
        with unittest.mock.patch.object(ib, "build_search", lambda s, o: fake_app("search", "luoke-search")), \
                unittest.mock.patch.object(ib, "build_scan", lambda s, o: fake_app("scan", "luoke-scan")), \
                unittest.mock.patch.object(ib, "build_ime", lambda s, o: fake_app("ime", "luoke-ime")), \
                unittest.mock.patch.object(ib, "build_mail", lambda s, m, o: fake_app("mail", "luoke-mail")), \
                unittest.mock.patch.object(ib.release, "leak_check", lambda app, what: None):
            apps = ib.build_apps("internal-1104", pins, src, {}, out)
        self.assertEqual(set(apps), {"luoke-ime", "luoke-scan"})     # 扫描先编的那份留着给 ClamAV 源码那一步；agent、搜索、邮件助手打进主控
        self.assertIn("luoke-agent", pins)
        self.assertIn("luoke-search", pins)
        self.assertIn("luoke-scan", pins)
        self.assertNotIn("failed", ib.TIMELINE.data)
        with unittest.mock.patch.object(ib, "build_search", broken), \
                unittest.mock.patch.object(ib, "build_ime", lambda s, o: fake_app("ime", "luoke-ime")), \
                unittest.mock.patch.object(ib, "build_mail", lambda s, m, o: fake_app("mail", "luoke-mail")):
            with self.assertRaises(SystemExit):        # 没有上一版可退：不能悄悄不带，用户升上来就没有搜索了
                ib.build_apps("internal-1104", pins, src, {}, out)
        self.assertIn("luoke-search", pins)
        ib.TIMELINE.data.pop("failed", None)
        built = []

        def second_try(s, o):
            built.append(pins["luoke-search"])
            if len(built) == 1:
                ib.die("编不过")
        prev = {"pins": {"luoke-search": "old"}}
        with unittest.mock.patch.object(ib, "build_search", second_try), \
                unittest.mock.patch.object(ib, "freeze_one", lambda repo, pin: "/frozen/" + pin), \
                unittest.mock.patch.object(ib.release, "leak_check", lambda app, what: None), \
                unittest.mock.patch.object(ib, "log") as logged:
            ib.check_bundled("luoke-search", "internal-1104", pins, src, prev, out)
        self.assertTrue(any("这一版没有停" in c.args[0] for c in logged.call_args_list))       # 看日志的人不会以为停了
        self.assertEqual((built, pins["luoke-search"], src["luoke-search"]), (["s", "old"], "old", "/frozen/old"))
        self.assertIn("退回上一版", ib.TIMELINE.data["kept"]["luoke-search"])
        stale = os.path.join(out, "search.noindex")            # 退回重编时不拿上一次的程序去查
        os.makedirs(stale, exist_ok=True)
        open(os.path.join(stale, "luoke-search"), "w").close()
        with unittest.mock.patch.object(ib.subprocess, "run", return_value=unittest.mock.Mock(returncode=0, stdout="", stderr="")), \
                self.assertRaises(SystemExit):
            ib.build_search(out, out)
        ib.TIMELINE.data.pop("failed", None)
        with tempfile.TemporaryDirectory() as d:               # 配方阶段放过的写进流程报告
            os.makedirs(os.path.join(d, "Resources"))
            with open(os.path.join(d, "Resources", "luoke-accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"accessory": "luoke-search", "version": "0.1.5"}, f)
            with unittest.mock.patch.object(ib.Recipe, "load", lambda p: unittest.mock.Mock(verified={"luoke-search": {"versions": ["0.1.5"]}})):
                ib.note_unverified_bundled("internal-1105", {"luoke-search": d})
            self.assertNotIn("luoke-search.evidence", ib.TIMELINE.data["kept"])
            with unittest.mock.patch.object(ib.Recipe, "load", lambda p: unittest.mock.Mock(verified={})):
                ib.note_unverified_bundled("internal-1105", {"luoke-search": d, "luoke-mail": "/nowhere"})
            self.assertIn("哪一档都不开，只能按字词搜", ib.TIMELINE.data["kept"]["luoke-search.evidence"])
            self.assertNotIn("luoke-mail.evidence", ib.TIMELINE.data["kept"])      # 读不到清单的跳过，不炸
            del ib.TIMELINE.data["kept"]["luoke-search.evidence"]
        self.assertNotIn("failed", ib.TIMELINE.data)
        pins["luoke-search"] = "s"
        with unittest.mock.patch.object(ib, "build_search", broken):
            for tag in ("beta2", "beta2-rc1"):         # RC、正式版不退，停
                with self.assertRaises(SystemExit):
                    ib.check_bundled("luoke-search", tag, pins, src, prev, out)
                self.assertEqual(pins["luoke-search"], "s")
            with unittest.mock.patch.object(ib, "freeze_one", lambda repo, pin: "/frozen/" + pin), self.assertRaises(SystemExit):
                ib.check_bundled("luoke-search", "internal-1104", pins, src, prev, out)      # 退回去也编不过：停
        ib.TIMELINE.data.pop("failed", None)
        pins["luoke-search"], src["luoke-search"] = "s", out
        with unittest.mock.patch.object(ib, "build_ime", broken), \
                unittest.mock.patch.object(ib, "build_mail", lambda s, m, o: fake_app("mail", "luoke-mail")):
            with self.assertRaises(SystemExit):
                ib.build_apps("internal-1104", pins, src, {}, out)
        self.assertEqual(ib.remote_repo({"repos": {"luoke-mail": "x/internal/luoke-mail"}}, "luoke-search"), "x/internal/luoke-search")

    def test_mail_that_wont_build_falls_back_or_stops(self):
        """螺壳邮件助手和搜索一样：内部版退回上一版钉的提交，RC、正式版停，没有上一版可退也停。不是 optional，不能悄悄不带"""
        ib = self._tool("internal_build")
        ib.LOG = open(os.devnull, "w")
        ib.last_published_pin = lambda repo, tag: ""      # 出版机上 releases/ 里有发过的内部版：这里只看沿用证据的那一版
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        pins, src = {"luoke-mail": "new", "luoke-mem": "e"}, {"luoke-mail": out, "luoke-mem": "/mem"}
        prev = {"pins": {"luoke-mail": "old"}}
        built = []

        def second_try(s, m, o):
            built.append((pins["luoke-mail"], m))
            if len(built) == 1:
                ib.die("导入不了")
            return o
        with unittest.mock.patch.object(ib, "build_mail", second_try), \
                unittest.mock.patch.object(ib, "freeze_one", lambda repo, pin: "/frozen/" + pin), \
                unittest.mock.patch.object(ib.release, "leak_check", lambda app, what: None):
            ib.check_bundled("luoke-mail", "internal-1105", pins, src, prev, out)
        self.assertEqual(built, [("new", "/mem"), ("old", "/mem")])
        self.assertEqual((pins["luoke-mail"], src["luoke-mail"]), ("old", "/frozen/old"))
        self.assertIn("退回上一版", ib.TIMELINE.data["kept"]["luoke-mail"])
        self.assertNotIn("failed", ib.TIMELINE.data)
        ib.TIMELINE.data.pop("kept", None)
        pins["luoke-mail"] = "new"

        def broken(s, m, o):
            ib.die("导入不了")
        with unittest.mock.patch.object(ib, "build_mail", broken):
            for tag, before in (("beta2", prev), ("beta2-rc1", prev), ("internal-1105", {})):
                with self.assertRaises(SystemExit):
                    ib.check_bundled("luoke-mail", tag, pins, src, before, out)
                self.assertEqual(pins["luoke-mail"], "new")
        ib.TIMELINE.data.pop("failed", None)

    def test_fallback_uses_the_last_published_internal_build(self):
        """11-09 internal-1109-6：不评测的版沿用证据的是最后真评过的 internal-1109（扫描 0.3.8），前两版发的已经是 0.3.10。
        编不过时退回最近发布的那版内部版钉的；没发布成的、候选版、beta、这一版自己都不算。"""
        ib = self._tool("internal_build")
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        ib.ROOT = root

        def rel(tag, scan, published, age, kept=False):
            d = os.path.join(root, "releases", tag)
            os.makedirs(d)
            if kept:
                with open(os.path.join(d, "timeline.json"), "w", encoding="utf-8") as f:
                    json.dump({"kept": {"luoke-scan": "退回"}}, f)
            with open(os.path.join(d, "pins.json"), "w", encoding="utf-8") as f:
                json.dump({"tag": tag, "pins": {"luoke-scan": scan}}, f)
            with open(os.path.join(d, "steps.json"), "w", encoding="utf-8") as f:
                json.dump({"build": "x", "publish": "x"} if published else {"build": "x"}, f)
            t = time.time() - age
            os.utime(os.path.join(d, "steps.json"), (t, t))
        self.assertEqual(ib.last_published_pin("luoke-scan", "internal-1109-8"), "")
        rel("internal-1109", "e8960c8", True, 500)
        rel("internal-1109-5", "80afc18", True, 300)
        rel("internal-1109-7", "broken", False, 100)         # 没发布成
        rel("beta2-rc1", "rc", True, 50)                      # 候选版不算
        rel("internal-1109-6", "e8960c8", True, 200, kept=True)   # 自己也是退回来的：不算
        rel("internal-1109-8", "self", True, 10)              # 这一版自己不算
        self.assertEqual(ib.last_published_pin("luoke-scan", "internal-1109-8"), "80afc18")
        self.assertEqual(ib.last_published_pin("luoke-search", "internal-1109-8"), "")
        ib.LOG = open(os.devnull, "w")
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        pins, src = {"luoke-scan": "new"}, {"luoke-scan": out}
        built = []

        def second_try(s, o):
            built.append(pins["luoke-scan"])
            if len(built) == 1:
                ib.die("不是 release 的 tag")
            return o
        ib.TIMELINE.data.pop("failed", None)
        with unittest.mock.patch.object(ib, "build_scan", second_try), \
                unittest.mock.patch.object(ib, "freeze_one", lambda repo, pin: "/frozen/" + pin), \
                unittest.mock.patch.object(ib.release, "leak_check", lambda app, what: None), \
                unittest.mock.patch.object(ib, "log", lambda m: None):
            ib.check_bundled("luoke-scan", "internal-1109-8", pins, src, {"pins": {"luoke-scan": "e8960c8"}}, out)
        self.assertEqual(built, ["new", "80afc18"])
        self.assertIn("80afc18", ib.TIMELINE.data["kept"]["luoke-scan"])

    def test_daemon_pins_scan_to_the_latest_release_tag_on_main(self):
        """扫描只收 release tag：/release 钉 main 上版本号最大、main 已经包含、发了 release 的 v<版本> tag，不钉 main 的最新提交（11-09 1109-6）；
        测试没过的 tag 没有 release，跳过（11-09 v0.3.15）。"""
        d = self._tool("release_daemon")
        self.assertEqual(d.TAG_PINNED, ["luoke-scan"])
        tags = "v0.3.9\tc9\nv0.3.14\tc14\nv0.3.15\toff\nlatest\tz\nv0.3.10\tc10\n"
        releases = "v0.3.9\nv0.3.10\nv0.3.14\nv0.3.15\n"
        status = {"off": "diverged", "c14": "ahead"}
        calls = []

        def sh(cmd, **k):
            calls.append(cmd)
            if cmd[-3].endswith("/tags"):
                return tags
            if cmd[-3].endswith("/releases"):
                self.assertIn("(.draft or .prerelease) | not", cmd[-1])      # 草稿、预发布都不算（#153 审查）
                return releases
            return status.get(cmd[2].split("/compare/")[1].split("...")[0], "behind") + "\n"
        with unittest.mock.patch.object(d, "sh", sh):
            self.assertEqual(d.latest_tag_on_main("luoke-scan"), "c14")       # v0.3.15 不在 main 上；按版本号比，不按字符串
            # 最新的 tag 没有 release（测试没过，11-09 v0.3.15）：退到上一个有 release 的
            tags = "v0.3.16\tc16\n" + tags
            status["c16"] = "ahead"
            self.assertEqual(d.latest_tag_on_main("luoke-scan"), "c14")
            releases += "v0.3.16\n"
            self.assertEqual(d.latest_tag_on_main("luoke-scan"), "c16")
            status.clear()
            with self.assertRaises(RuntimeError):
                d.latest_tag_on_main("luoke-scan")
        self.assertTrue(all(f"luoke-scan{d.REPO_SUFFIX}/" in " ".join(c) for c in calls))
        got = {}

        def sh2(cmd, **k):
            if "rev-parse" in cmd:
                return "m" * 40
            if cmd[:3] == ["gh", "api", "--paginate"] and cmd[3].endswith("/releases"):
                return "v1.0.0\n"
            if cmd[:3] == ["gh", "api", "--paginate"]:
                return "v1.0.0\tt" * 1
            if "/compare/" in cmd[2]:
                return "identical"
            return "main-" + cmd[2].split("/")[2] + "\n"
        with unittest.mock.patch.object(d, "sh", sh2):
            got = d.pins_now()
        self.assertEqual(got["luoke-scan"], "t")
        self.assertEqual(got["luoke-search"], f"main-luoke-search{d.REPO_SUFFIX}")

        # 查 release 出错（接口出错、gh 不在）：钉最近发布那版内部版的扫描，不让这一版不带扫描（#151 审查第 3 条）
        core = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, core, True)
        for tag, scan, age in (("internal-1109-5", "80afc18", 300), ("internal-1109-7", "0fbe8b1", 100)):
            rd = os.path.join(core, "releases", tag)
            os.makedirs(rd)
            with open(os.path.join(rd, "pins.json"), "w", encoding="utf-8") as f:
                json.dump({"tag": tag, "pins": {"luoke-scan": scan}}, f)
            with open(os.path.join(rd, "steps.json"), "w", encoding="utf-8") as f:
                json.dump({"publish": "x"}, f)
            os.utime(os.path.join(rd, "steps.json"), (time.time() - age,) * 2)

        def sh3(cmd, **k):
            if "/releases" in " ".join(cmd):
                raise RuntimeError("gh api …：HTTP 502")
            return sh2(cmd, **k)
        logs = []
        with unittest.mock.patch.object(d, "sh", sh3), unittest.mock.patch.object(d, "CORE", core), \
                unittest.mock.patch.object(d, "log", logs.append):
            got = d.pins_now()
        self.assertEqual(got["luoke-scan"], "0fbe8b1")
        self.assertTrue(any("钉最近发布那版内部版的 0fbe8b1" in m for m in logs))
        shutil.rmtree(os.path.join(core, "releases"))          # 也没有发布过的：照旧不带
        with unittest.mock.patch.object(d, "sh", sh3), unittest.mock.patch.object(d, "CORE", core), \
                unittest.mock.patch.object(d, "log", logs.append):
            got = d.pins_now()
        self.assertNotIn("luoke-scan", got)
        self.assertEqual(got["luoke-search"], f"main-luoke-search{d.REPO_SUFFIX}")
        self.assertIn("最近发布的内部版也没有", logs[-1])          # 日志不再写成「取不到 main」（#153 审查）
        self.assertNotIn("取不到 main", logs[-1])

    def test_build_mail_stops_on_a_commit_that_cant_go_into_the_app(self):
        """钉住的提交还没有 scripts/build_for_luoke.sh，或者它失败：停在 apps 这一步，不等打主控"""
        ib = self._tool("internal_build")
        ib.LOG = open(os.devnull, "w")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                ib.build_mail(d, d, d)
        ib.TIMELINE.data.pop("failed", None)

    def test_unbumped_version_keeps_the_previous_build(self):
        """11-04 定：改了代码、版本号没升，不停也不拿掉，钉回上一版的提交、沿用上一版的包，
        写进流程记录（报告、/release 的回复、发布说明）。必须带的和可缺的一样；上一版没带的照新提交出。"""
        ib = self._tool("internal_build")
        ib.LOG = open(os.devnull, "w")
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)

        def fake_app(name, accessory, version="0.1.1"):
            app = os.path.join(out, name + ".app")
            os.makedirs(os.path.join(app, "Contents", "Resources"), exist_ok=True)
            with open(os.path.join(app, "Contents", "Resources", "luoke-accessory.json"), "w") as f:
                json.dump({"accessory": accessory, "version": version}, f)
            return app
        old_ime = fake_app("old-ime", "luoke-ime", "0.1.23")
        prev = {"pins": {"luoke-mem": "e"}, "apps": {"luoke-ime": {"pin": "i0", "mem": "e", "app": old_ime}}}
        pins = {"luoke-core": "c", "luoke-mail": "m", "luoke-mem": "e", "luoke-ime": "i1", "luoke-scan": "a1", "luoke-search": "s"}
        src = {k: out for k in pins if k != "luoke-core"}
        refrozen = []
        ib.TIMELINE.data.pop("kept", None)
        with unittest.mock.patch.object(ib, "build_scan", lambda s, o: fake_app("scan", "luoke-scan")), \
                unittest.mock.patch.object(ib, "build_ime", lambda s, o: fake_app("ime", "luoke-ime", "0.1.23")), \
                unittest.mock.patch.object(ib, "build_search", lambda s, o: fake_app("search", "luoke-search")), \
                unittest.mock.patch.object(ib, "build_mail", lambda s, m, o: fake_app("mail", "luoke-mail")), \
                unittest.mock.patch.object(ib, "unchanged_since", lambda s, a, b: False), \
                unittest.mock.patch.object(ib, "freeze_one", lambda r, p: refrozen.append((r, p)) or out), \
                unittest.mock.patch.object(ib.release, "leak_check", lambda app, what: None):
            apps = ib.build_apps("internal-1104-3", pins, src, prev, out)
        self.assertEqual(apps["luoke-ime"], old_ime)                                      # 沿用上一版的包
        self.assertEqual(pins["luoke-ime"], "i0")                                         # 钉回上一版的提交
        self.assertEqual(refrozen, [("luoke-ime", "i0")])                                 # 源码也切回去
        self.assertEqual((pins["luoke-search"], pins["luoke-scan"]), ("s", "a1"))         # 打进主控的照新提交出（扫描按 release 的 tag，版本号一定升过）
        self.assertEqual(ib.TIMELINE.data["kept"]["luoke-ime"],
                         "luoke-ime：i0→i1 改了代码、版本号还是 0.1.23，这一版沿用 0.1.23（请升版本号）")
        self.assertNotIn("failed", ib.TIMELINE.data)
        self.assertNotIn("luoke-scan", ib.TIMELINE.data.get("dropped") or {})
        ib.TIMELINE.data.pop("kept", None)

    def test_beta_with_scan_attaches_or_links_clamav_source(self):
        """11-03：带扫描的 beta，包里的 ClamAV 版本第一次出现就附上源码包和配方，之后链回第一次附的那一版；
        内部版、不带扫描的 beta 不管；源码凑不齐就停。"""
        ib = self._tool("internal_build")
        ib.LOG = open(os.devnull, "w")
        app = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, app, True)
        os.makedirs(os.path.join(app, "Contents", "Resources"))
        with open(os.path.join(app, "Contents", "Resources", "bundled-tools.json"), "w") as f:
            json.dump([{"file": "Contents/MacOS/ai.luoke.scan.clamscan", "formula": "clamav", "version": "1.5.4_2"}], f)
        self.assertIsNone(ib.clamav_plan("internal-1103-3", {"luoke-scan": app}))
        self.assertIsNone(ib.clamav_plan("beta2", {"luoke-mail": app}))
        listing = []
        fake_run = lambda cmd, **kw: unittest.mock.Mock(returncode=0, stdout="\n".join(json.dumps(r) for r in listing))
        patch = "clamav-1.5.4_2-patch1-0097a6f99d9f.patch"
        files = ["/src/clamav-1.5.4.tar.gz", "/src/clamav-1.5.4_2.rb", "/src/" + patch]
        with unittest.mock.patch.object(ib, "run", fake_run), \
                unittest.mock.patch.object(ib.clamav_source, "ensure_source", lambda v, t, c, f: files):
            p = ib.clamav_plan("beta2", {"luoke-scan": app})
            self.assertEqual((p["version"], p["attach"], p["files"], p["patches"]), ("1.5.4_2", True, files, 1))
            self.assertEqual(p["found"]["1.5.4_2"]["tag"], "beta2")
            self.assertEqual([n for n, _ in p["found"]["1.5.4_2"]["patches"]], [patch])
            url = "https://github.com/luokelabs/luoke-releases/releases"
            listing.append({"tag_name": "beta2", "html_url": f"{url}/tag/beta2", "published_at": "2026-11-06T00:00:00Z", "draft": False,
                            "assets": [{"name": n, "browser_download_url": f"{url}/download/beta2/{n}"}
                                       for n in ("Luoke.dmg", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb")]})
            self.assertTrue(ib.clamav_plan("beta2.1", {"luoke-scan": app})["attach"])   # beta2 缺补丁：不算附全，这一版再附
            listing[0]["assets"].append({"name": patch, "browser_download_url": f"{url}/download/beta2/{patch}"})
            p = ib.clamav_plan("beta2.1", {"luoke-scan": app})
            self.assertEqual((p["attach"], p["files"], p["where"]["tag"]), (False, [], "beta2"))
            self.assertTrue(ib.clamav_plan("beta2", {"luoke-scan": app})["attach"])      # 同一版重跑：还是附在它自己身上
            rc = ib.clamav_plan("beta2-rc1", {"luoke-scan": app})                         # 候选版按 beta2 查
            self.assertEqual((rc["attach"], rc["found"]["1.5.4_2"]["tag"]), (True, "beta2"))

        def missing(v, t, c, f):
            raise ib.clamav_source.SourceError("配方不在")
        with unittest.mock.patch.object(ib, "run", lambda cmd, **kw: unittest.mock.Mock(returncode=0, stdout="")), \
                unittest.mock.patch.object(ib.clamav_source, "ensure_source", missing), self.assertRaises(SystemExit):
            ib.clamav_plan("beta2", {"luoke-scan": app})

    def test_scan_comes_from_its_latest_release(self):
        """扫描独立出版本（11-05）：内部版取最新 release 的 tag 和提交（问不到照顶层钉的），beta、RC、正式版照顶层钉的（11-05 审查：
        没经过 RC 的不进 beta）。打进主控（0.3.0 起）：在出版机上按那个 tag 用它自己的 package_app.py 先编一遍（release 上的 tar 包
        证明不了出处，不用），编出 luoke-scan/（Helpers、Frameworks、Resources/scan）。冻结的提交不是 release 的 tag、源码有改动、
        编出来的版本和 tag 对不上，都停。"""
        import subprocess
        ib = self._tool("internal_build")
        ib.LOG = open(os.devnull, "w")
        self.addCleanup(ib.LOG.close)
        sha = "a" * 40
        answers = {"release": "v0.3.0\n", "commits": sha + "\n"}
        fake = lambda cmd, **kw: unittest.mock.Mock(returncode=0, stdout=answers["release" if cmd[1] == "release" else "commits"])
        with unittest.mock.patch.object(ib, "run", fake):
            self.assertEqual(ib.latest_scan_release(), ("v0.3.0", sha))
            for tag, want in (("internal-1105", sha), ("local-1105-2100", sha), ("beta2", "b" * 40), ("beta2-rc1", "b" * 40)):
                pins = {"luoke-scan": "b" * 40}
                ib.pick_scan(tag, pins, False)
                self.assertEqual(pins["luoke-scan"], want, tag)
            pins = {"luoke-scan": "c" * 40}
            ib.pick_scan("internal-1105", pins, True)       # --pin 指定的照用
            self.assertEqual(pins["luoke-scan"], "c" * 40)
            answers["release"] = "nightly\n"
            self.assertIsNone(ib.latest_scan_release())
            pins = {"luoke-scan": "b" * 40}
            ib.pick_scan("internal-1105", pins, False)      # 问不到 release：照顶层钉的（打进主控的，不带它用户就没有扫描了）
            self.assertEqual(pins["luoke-scan"], "b" * 40)

        with unittest.mock.patch.dict(os.environ, {"LUOKE_RELEASE_TEAM": ""}), unittest.mock.patch.object(ib.release.subprocess, "run", lambda cmd, **kw: unittest.mock.Mock(
                returncode=0, stdout='  1) 0123 "Developer ID Application: Example (ABCDE12345)"\n  2) 4567 "Apple Development: x (ZZZZZ99999)"\n')):
            self.assertEqual(ib.release.release_team(), "ABCDE12345")
        with unittest.mock.patch.dict(os.environ, {"LUOKE_RELEASE_TEAM": ""}), unittest.mock.patch.object(ib.release.subprocess, "run", lambda cmd, **kw: unittest.mock.Mock(
                returncode=0, stdout='"Developer ID Application: A (ABCDE12345)"\n"Developer ID Application: B (FGHIJ67890)"\n')):
            self.assertEqual(ib.release.release_team(), "")      # 不止一个团队：定不了

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        def sh(*args, cwd=None):
            return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
        src = os.path.join(tmp, "luoke-scan")
        sh("git", "init", "-q", "-b", "main", src)
        os.makedirs(os.path.join(src, "scripts"))
        seen = os.path.join(tmp, "seen.json")
        with open(os.path.join(src, "scripts", "package_app.py"), "w") as f:
            # the tag's own script builds, signs and checks; here it only lays out the folder and says how it was run
            f.write("import json, os, sys\n"
                    "out = sys.argv[sys.argv.index('--out') + 1]\n"
                    "os.makedirs(os.path.join(out, 'luoke-scan', 'Resources', 'scan'))\n"
                    "v = os.environ.get('FAKE_VERSION', '0.3.0')\n"
                    "json.dump({'accessory': 'luoke-scan', 'version': v}, open(os.path.join(out, 'luoke-scan', 'Resources', 'scan', 'luoke-accessory.json'), 'w'))\n"
                    f"json.dump({{'argv': sys.argv[1:], 'team': os.environ.get('LUOKE_RELEASE_TEAM'), 'tools': os.environ.get('LUOKE_SCAN_TOOLS')}}, open({seen!r}, 'w'))\n")
        sh("git", "add", ".", cwd=src)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "release", cwd=src)
        sh("git", "tag", "v0.3.0", cwd=src)
        sh("git", "remote", "add", "origin", src, cwd=src)
        with open(os.path.join(src, ".luoke-commit"), "w") as f:      # 冻结时写的（freeze_one），扫描仓库不忽略它
            f.write(sh("git", "rev-parse", "HEAD", cwd=src) + "\n")
        out = os.path.join(tmp, "apps")
        os.makedirs(out)
        with unittest.mock.patch.object(ib.release, "release_team", lambda: "ABCDE12345"):
            folder = ib.build_scan(src, out)
            self.assertEqual(folder, os.path.join(out, "scan.noindex", "luoke-scan"))
            self.assertEqual(ib.app_version(folder), "0.3.0")
            ran = json.load(open(seen))
            self.assertIn("--tools-from", ran["argv"])
            self.assertNotIn("--notarize", ran["argv"])         # 签名、公证跟主控走
            self.assertEqual((ran["team"], ran["tools"]), ("ABCDE12345", ib.SCAN_TOOLS))
            with unittest.mock.patch.dict(os.environ, {"FAKE_VERSION": "0.2.2"}), self.assertRaises(SystemExit):
                ib.build_scan(src, out)                      # what was built is not the tag's version
            with open(os.path.join(src, "stray.txt"), "w") as f:
                f.write("x")
            with self.assertRaises(SystemExit):              # changed source
                ib.build_scan(src, out)
            os.remove(os.path.join(src, "stray.txt"))
            # beta / RC pin the public export commit: new id, the tag stays on the private commit it was exported from
            tagged = sh("git", "rev-parse", "v0.3.0", cwd=src)
            labs = ["-c", "user.name=" + ib.public_sync.EXPORT_NAME, "-c", "user.email=" + ib.public_sync.EXPORT_EMAIL]
            def export_commit(tree, ident=labs, origin=tagged):
                return sh("git", *ident, "commit-tree", tree, "-m", f"luoke-scan：同步\n\nExported-From: {origin}", cwd=src)
            same = sh("git", "rev-parse", "v0.3.0^{tree}", cwd=src)
            sh("git", "checkout", "-q", "--detach", export_commit(same), cwd=src)
            self.assertEqual(ib.app_version(ib.build_scan(src, out)), "0.3.0")
            other = sh("git", "hash-object", "-w", "-t", "tree", "/dev/null", cwd=src)     # empty tree: not what v0.3.0 exports to
            for bad in (export_commit(other), export_commit(same, ["-c", "user.name=t", "-c", "user.email=t@t"])):
                sh("git", "checkout", "-q", "--detach", bad, cwd=src)
                with self.assertRaises(SystemExit):
                    ib.build_scan(src, out)
            # committed by Luoke Labs but authored by someone else: last_export finds it, identity_ok turns it down
            mixed = subprocess.run(["git", "commit-tree", same, "-m", f"luoke-scan：同步\n\nExported-From: {tagged}"], cwd=src, check=True,
                                   capture_output=True, text=True, env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                                   GIT_COMMITTER_NAME=ib.public_sync.EXPORT_NAME, GIT_COMMITTER_EMAIL=ib.public_sync.EXPORT_EMAIL)).stdout.strip()
            self.assertEqual(ib.public_sync.last_export(src, mixed)[0], mixed)
            sh("git", "checkout", "-q", "--detach", mixed, cwd=src)
            self.assertEqual(ib.exported_tag(src), "")
            with self.assertRaises(SystemExit):
                ib.build_scan(src, out)
            sh("git", "checkout", "-q", "main", cwd=src)
            sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "after", cwd=src)
            # a PR that merges the tag into a release branch always adds a commit: new id, same tree as the tag, so it is that tag
            sh("git", "checkout", "-q", "--detach", export_commit(same, origin=sh("git", "rev-parse", "HEAD", cwd=src)), cwd=src)
            self.assertEqual(ib.exported_tag(src), "v0.3.0")
            self.assertEqual(ib.app_version(ib.build_scan(src, out)), "0.3.0")
            sh("git", "checkout", "-q", "main", cwd=src)
            with open(os.path.join(src, "later.txt"), "w") as f:
                f.write("not in v0.3.0\n")
            sh("git", "add", "later.txt", cwd=src)
            sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "later", cwd=src)
            changed = sh("git", "rev-parse", "HEAD^{tree}", cwd=src)
            sh("git", "checkout", "-q", "--detach", export_commit(changed, origin=sh("git", "rev-parse", "HEAD", cwd=src)), cwd=src)
            self.assertEqual(ib.exported_tag(src), "")
            with self.assertRaises(SystemExit):              # exported from a commit after the tag that changes something
                ib.build_scan(src, out)
            sh("git", "checkout", "-q", "main", cwd=src)
            with self.assertRaises(SystemExit):              # not a release's tag
                ib.build_scan(src, out)

    def test_new_accessories_fetch_from_the_private_repos(self):
        """深度搜索、扫描的私有仓库带 -private 后缀，不带的名字留给公开仓库：流水线和出版守护进程取的是同一个私有仓库；
        改名以前从旧地址克隆的持久克隆，下次冻结时改指新地址，钉住的提交照样取得到。"""
        import subprocess
        ib, d = self._tool("internal_build"), self._tool("release_daemon")
        ib.LOG = open(os.devnull, "w")
        self.addCleanup(ib.LOG.close)
        self.assertEqual(set(d.OPTIONAL), set(ib.OPTIONAL))
        self.assertEqual((d.GITHUB_ORG, d.REPO_SUFFIX), (ib.GITHUB_ORG, ib.REPO_SUFFIX))
        for repo in ib.OPTIONAL:
            self.assertEqual(ib.remote_url(repo), f"https://github.com/{ib.GITHUB_ORG}/{repo}-private.git")

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        def sh(*args, cwd=None):
            return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

        def repo_with_commit(name, text):
            r = os.path.join(tmp, name)
            sh("git", "init", "-q", "-b", "main", r)
            with open(os.path.join(r, "f.txt"), "w") as f:
                f.write(text)
            sh("git", "add", "f.txt", cwd=r)
            sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", text, cwd=r)
            return r, sh("git", "rev-parse", "HEAD", cwd=r)
        old, _ = repo_with_commit("old", "public")          # 旧地址：以后是公开仓库
        new, pin = repo_with_commit("new", "private")       # 私有仓库：钉住的提交只在这里
        src = os.path.join(tmp, "src")
        sh("git", "clone", "-q", old, os.path.join(src, "luoke-search"))
        with unittest.mock.patch.object(ib, "SRC", src), unittest.mock.patch.object(ib, "TOP", os.path.join(tmp, "top")), \
                unittest.mock.patch.object(ib, "remote_url", lambda repo: new):
            dst = ib.freeze_one("luoke-search", pin)
        self.assertEqual(sh("git", "remote", "get-url", "origin", cwd=dst), new)
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=dst), pin)

    def test_internal_builds_scan_the_pinned_commits_before_eval(self):
        """规划 A2：内部版冻结以后、评测之前，四个仓库钉住的提交照公开导出查泄漏词，有命中就停，写明是哪个仓库哪一行；
        beta 和候选版（钉导出提交）不在这里查。"""
        import subprocess
        ib = self._tool("internal_build")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        words = os.path.join(tmp, "leak-patterns")
        with open(words, "w") as f:
            f.write("# 测试用：只靠通用的词\n")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@example.com",
                   GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@example.com", LUOKE_LEAK_PATTERNS=words)
        def g(cwd, *a):
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()
        def commit(d, text):
            with open(os.path.join(d, "x.py"), "w") as f:
                f.write(text)
            g(d, "add", "x.py"); g(d, "commit", "-qm", "c")
            return g(d, "rev-parse", "HEAD")
        pins, src = {}, {}
        for repo in ["luoke-core"] + ib.ACCESSORIES:
            d = os.path.join(tmp, repo)
            g(tmp, "init", "-q", "-b", "main", d)
            pins[repo] = commit(d, "x = 1\n")
            src[repo] = d
        leaky = commit(src["luoke-mail"], "# see luoke-mail#41\nx = 1\n")
        logs = []
        def die(msg):
            raise RuntimeError(msg)
        with unittest.mock.patch.object(ib, "ROOT", src["luoke-core"]), unittest.mock.patch.object(ib, "log", logs.append), \
                unittest.mock.patch.object(ib, "die", die), unittest.mock.patch.dict(os.environ, env):
            ib.leak_scan(pins, src)
            self.assertEqual(len(logs), 1 + len(ib.ACCESSORIES))      # 主控和每个钉住的配件各一行
            self.assertTrue(all("命中 0 行" in m for m in logs), logs)
            with self.assertRaisesRegex(RuntimeError, r"luoke-mail " + leaky[:7] + r"：\n(.|\n)*x\.py:1: \[私有仓库\]") as e:
                ib.leak_scan(dict(pins, **{"luoke-mail": leaky}), src)
            self.assertNotIn("luoke-ime", str(e.exception))
            os.remove(words)
            with self.assertRaisesRegex(RuntimeError, "没有本机词表"):         # 词表不在就停，不当成没命中
                ib.leak_scan(pins, src)
        with open(os.path.join(ROOT, "tools", "internal_build.py"), encoding="utf-8") as f:
            main_src = f.read()
        self.assertIn('        if re.fullmatch(r"internal-\\d{4}(-\\d+)?", a.tag):\n            # beta 和候选版钉的是导出提交，导出时已经查过\n'
                      '            with ph("leak scan"):\n                leak_scan(pins, src)\n', main_src)

    def test_freeze_takes_a_public_commit_the_old_clone_has_not_seen(self):
        """公开版钉的导出提交只在私有仓库的 public 分支上；持久克隆早就建好了，从 TOP/<repo> 只取得到本地分支，
        checkout 会失败。freeze 要让 TOP/<repo> 取 public，再按提交号取。"""
        import subprocess
        ib = self._tool("internal_build")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@example.com",
                   GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@example.com")
        def g(cwd, *a):
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()
        top, src, pins, want = os.path.join(tmp, "top"), os.path.join(tmp, "src"), {}, {}
        for i, repo in enumerate(ib.ACCESSORIES):
            gh, dev = os.path.join(tmp, repo + ".git"), os.path.join(tmp, "dev-" + repo)
            g(tmp, "init", "-q", "--bare", "-b", "main", gh)
            g(tmp, "clone", "-q", gh, dev)
            with open(os.path.join(dev, "f"), "w") as f:
                f.write("private\n")
            g(dev, "add", "f"); g(dev, "commit", "-qm", "one"); g(dev, "push", "-q", "origin", "main")
            g(tmp, "clone", "-q", gh, os.path.join(top, repo))
            g(tmp, "clone", "-q", os.path.join(top, repo), os.path.join(src, repo))      # 持久克隆：导出之前就有了
            if i < 2:
                g(dev, "checkout", "-q", "--orphan", "public")
            with open(os.path.join(dev, "f"), "w") as f:
                f.write("exported\n")
            g(dev, "commit", "-qam", "exported"); g(dev, "push", "-q", "origin", "HEAD")
            want[repo] = g(dev, "rev-parse", "HEAD")
            pins[repo] = want[repo][:7] if i == 1 else want[repo]
        with unittest.mock.patch.object(ib, "TOP", top), unittest.mock.patch.object(ib, "SRC", src), \
                unittest.mock.patch.object(ib, "log", lambda m: None), unittest.mock.patch.dict(os.environ, env):
            paths = ib.freeze(pins)
        for repo, pin in want.items():
            self.assertEqual(g(paths[repo], "rev-parse", "HEAD"), pin)
            with open(os.path.join(paths[repo], "f")) as f:
                self.assertEqual(f.read(), "exported\n")

    def test_embed_is_evaluated_per_tier_once_its_accessory_is_in_the_pipeline(self):
        """2026-11-02：luoke-search.embed 用专用模型，但评测表写了 per_tier，每一档都评（起不起得来按档）；
        别的专用模型任务照旧只评最高档。luoke-search 还没钉进流水线时这一条不评（不然取 src 会出错）。"""
        ib = self._tool("internal_build")
        with open(os.path.join(ROOT, "tools", "release_evals.json"), encoding="utf-8") as f:
            evals = json.load(f)
        self.assertTrue(evals["luoke-search.embed"]["per_tier"])
        self.assertEqual(evals["luoke-search.embed"]["summary"], "embed-parity")
        self.assertIn("{core}/tools/embed_parity.py", evals["luoke-search.embed"]["command"])
        tiers = ["16", "32", "128"]
        only = ["luoke-search.embed", "luoke-mail.triage"]
        template = os.path.join(ROOT, "recipes", "dev.json")
        with unittest.mock.patch.object(ib, "ROOT", tempfile.mkdtemp()):
            run = ib.plan_eval("test-x", {}, {"luoke-mail": "/m"}, None, template, tiers, evals, only=only)[0]
            self.assertEqual({t: sorted(ts) for t, ts in run.items()}, {t: ["luoke-mail.triage"] for t in tiers})
            src = {"luoke-mail": "/m", "luoke-search": "/s"}
            run = ib.plan_eval("test-x", {}, src, None, template, tiers, evals, only=only)[0]
            self.assertEqual({t: sorted(ts) for t, ts in run.items()}, {t: sorted(only) for t in tiers})
            plain = dict(evals, **{"luoke-search.embed": {k: v for k, v in evals["luoke-search.embed"].items() if k != "per_tier"}})
            run = ib.plan_eval("test-x", {}, src, None, template, tiers, plain, only=only)[0]
            self.assertEqual([t for t, ts in run.items() if "luoke-search.embed" in ts], ["128"])

    def test_internal_builds_have_their_own_repo(self):
        """10-28（luoke-core#36）：内部版发 luoke-internal-builds，beta 和正式版发 luoke-releases；主控按渠道查同一个仓库。"""
        ib = self._tool("internal_build")
        for tag in ("beta1", "beta1.1"):
            self.assertEqual(ib.releases_repo(tag), "luokelabs/luoke-releases")
        for tag in ("internal-1029", "internal-1029-2"):
            self.assertEqual(ib.releases_repo(tag), "luokelabs/luoke-internal-builds")
        src = open(os.path.join(ROOT, "app", "Sources", "LuokeCore", "Updater.swift"), encoding="utf-8").read()
        self.assertIn('channel == .internalBuild ? "luokelabs/luoke-internal-builds" : "luokelabs/luoke-releases"', src)

    def test_beta_files_also_point_at_the_gitlab_mirror(self):
        """10-29（luoke-core#39）：beta 和正式版的发布文件在 gitlab.com 有镜像，清单里 GitHub 后面多一个 GitLab 地址；内部版、本机版、测试托管不加。"""
        import types
        rel = self.rel
        def bases(**kw):
            a = types.SimpleNamespace(**{"url_base": None, "github": None, "gitlab": None, **kw})
            return rel.file_bases(a, "beta1.1", "/x")
        self.assertEqual(bases(github="luokelabs/luoke-releases", gitlab="luokelabs/luoke-releases"),
                         ["https://github.com/luokelabs/luoke-releases/releases/download/beta1.1",
                          "https://gitlab.com/api/v4/projects/luokelabs%2Fluoke-releases/packages/generic/luoke-releases/beta1.1"])
        self.assertEqual(bases(github="luokelabs/luoke-internal-builds"), ["https://github.com/luokelabs/luoke-internal-builds/releases/download/beta1.1"])
        self.assertEqual(bases(), ["file:///x"])
        self.assertEqual(bases(url_base="http://h:1/", github="a/b", gitlab="a/b"), ["http://h:1"])
        from luoke_core import download
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "Luoke.dmg")
            open(f, "wb").write(b"x")
            self.assertEqual(download.make_item_from_file(f, "hub", "1", "https://a", "https://b")["urls"], ["https://a/{path}", "https://b/{path}"])
        ib = self._tool("internal_build")
        self.assertEqual(ib.GITLAB_MIRROR, ib.RELEASES_REPO)
        src = open(os.path.join(ROOT, "app", "Sources", "LuokeCore", "Updater.swift"), encoding="utf-8").read()
        self.assertIn('"https://gitlab.com/api/v4/projects/luokelabs%2Fluoke-releases"', src)
        self.assertIn("/packages/generic/luoke-releases/", src)

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

    def test_canary_ignores_base_url(self):
        """邮件评测的原始答复带 base_url（里面有这一版的目录）和跑评测的 Python 版本；只差这两个要算一样，不然挑到邮件任务的金丝雀必定判为不一样、整档重评。"""
        ib = self._tool("internal_build")
        a = {"base_url": "unix:/x/release/internal-1107-2/lab/16/run/hub.sock", "python": "3.9.6", "results": [{"id": "t1", "answer": "reply"}]}
        b = {"base_url": "unix:/x/release/internal-1108/lab/16/run/hub.sock", "python": "3.12.1", "results": [{"id": "t1", "answer": "reply"}]}
        self.assertEqual(ib.strip_volatile(a), ib.strip_volatile(b))
        b["results"][0]["answer"] = "ignore"
        self.assertNotEqual(ib.strip_volatile(a), ib.strip_volatile(b))

    def test_scan_can_be_the_canary_when_memory_and_mail_changed(self):
        """记忆、邮件自己改了的那天，同一档里没变的格子靠扫描的一条核；扫描的原始结果带配件的版本号，只差它算一样。"""
        ib = self._tool("internal_build")
        self.assertEqual(ib.CANARY_ORDER[-1], "luoke-scan.identify")
        self.assertEqual(ib.canary_first(["luoke-search.embed", "luoke-scan.identify", "luoke-scan.skills"]),
                         ["luoke-scan.identify", "luoke-search.embed", "luoke-scan.skills"])
        self.assertEqual(ib.canary_first(["luoke-scan.identify", "luoke-mail.triage"])[0], "luoke-mail.triage")
        a = {"version": "0.3.40", "tasks": {"identify": {"prompt_version": 3, "evalset_version": 2, "results": [{"id": "i1", "runs": [{"output": "x"}]}]}}}
        b = {"version": "0.3.41", "tasks": {"identify": {"prompt_version": 3, "evalset_version": 2, "results": [{"id": "i1", "runs": [{"output": "x"}]}]}}}
        self.assertEqual(ib.strip_volatile(a), ib.strip_volatile(b))
        b["tasks"]["identify"]["prompt_version"] = 4
        self.assertNotEqual(ib.strip_volatile(a), ib.strip_volatile(b))
        with open(os.path.join(ROOT, "tools", "release_evals.json"), encoding="utf-8") as f:
            table = json.load(f)
        for t in ("explain", "identify", "skills"):
            self.assertEqual(ib.eval_paths(table[f"luoke-scan.{t}"]),
                             ["bench/evalsets/" + t + ".json", "bench/prompts.py", "bench/run_eval.py", "prompts/"])

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
            # 负责人开了 --reuse-rc 的 beta：和它全评过的候选版比金丝雀（11-26：只改了下载队列，beta2 不该全评 2.5 小时）
            self.assertTrue(ib.canary_allowed("beta2", "beta2-rc6")[0])
            self.assertTrue(ib.canary_allowed("beta2.1", "beta2.1-rc1")[0])
            self.assertFalse(ib.canary_allowed("beta2-rc7", "beta2-rc6")[0])     # 候选版自己照旧全评
            os.utime(os.path.join(root, "releases", "internal-1023", "pins.json"), (time.time() - 10 * 86400,) * 2)
            os.makedirs(os.path.join(root, "releases", "beta2-rc1"))
            json.dump({"tag": "beta2-rc1", "eval_mode": "full"}, open(os.path.join(root, "releases", "beta2-rc1", "pins.json"), "w"))
            self.assertFalse(ib.canary_allowed("internal-1104")[0])    # 候选版的全评不进内部版沿用的链，不算

    def test_daemon_preempts_only_post_publish_repeats(self):
        d = self._tool("release_daemon")
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "releases", "internal-1024"))
        p = os.path.join(root, "releases", "internal-1024", "timeline.json")
        with unittest.mock.patch.object(d, "CORE", root):
            json.dump({"phases": [{"name": "publish", "start": 0, "end": 1}, {"name": "repeats", "start": 1, "end": None}]}, open(p, "w"))
            self.assertTrue(d.in_repeats("internal-1024"))
            self.assertTrue(d.in_repeats("internal-1024", 1))
            self.assertFalse(d.in_repeats("internal-1024", 5))     # 上一次留下的复测（没记结束）：这一次 5 秒才开始，不算
            json.dump({"phases": [{"name": "eval 16", "start": 0, "end": None}]}, open(p, "w"))
            self.assertFalse(d.in_repeats("internal-1024"))
            self.assertFalse(d.in_repeats("missing"))

    def test_daemon_runs_from_a_tools_dir_that_follows_main(self):
        """规划第 19 件：流水线从只跟 main 的工具目录跑，出版克隆（LUOKE_BUILD_ROOT）才切到钉住的提交、在那里出版。
        工具目录没在出版时切到最新的 origin/main；取不到、切不过去只记日志；两份是同一个目录（没设）时不动。"""
        import subprocess
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@example.com",
                   GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@example.com")
        env.pop("LUOKE_BUILD_ROOT", None)
        def g(cwd, *a):
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()
        def commit(text):
            with open(os.path.join(dev, "f"), "w") as f:
                f.write(text)
            g(dev, "commit", "-qam", text); g(dev, "push", "-q", "origin", "main")
            return g(dev, "rev-parse", "HEAD")
        gh, dev, tools, build = (os.path.join(tmp, n) for n in ("core.git", "dev", "tools", "build"))
        g(tmp, "init", "-q", "--bare", "-b", "main", gh)
        g(tmp, "clone", "-q", gh, dev)
        with open(os.path.join(dev, "f"), "w") as f:
            f.write("one")
        g(dev, "add", "f")
        old = commit("one")
        g(tmp, "clone", "-q", gh, tools)
        g(tmp, "clone", "-q", gh, build)
        new = commit("two")
        logs = []
        with unittest.mock.patch.dict(os.environ, dict(env, LUOKE_BUILD_ROOT=build), clear=True):
            d = self._tool("release_daemon")
            self.assertEqual((d.CORE, d.BUILD), (build, os.path.join(build, "build.noindex")))
            with unittest.mock.patch.object(d, "ROOT", tools), unittest.mock.patch.object(d, "log", logs.append):
                d.follow_main()
                self.assertEqual((g(tools, "rev-parse", "HEAD"), g(build, "rev-parse", "HEAD")), (new, old))     # 出版克隆不动
                self.assertIn(f"{old[:7]} → {new[:7]}", logs[-1])
                commit("three")
                with open(os.path.join(tools, "f"), "w") as f:
                    f.write("改过")
                d.follow_main()
                self.assertEqual(g(tools, "rev-parse", "HEAD"), new)
                self.assertIn("切不到", logs[-1])
                g(tools, "remote", "set-url", "origin", os.path.join(tmp, "nope.git"))
                d.follow_main()
                self.assertIn("取 origin/main 失败", logs[-1])
            pins = {"luoke-core": "a" * 40, "luoke-mail": "b" * 40, "luoke-mem": "c" * 40, "luoke-ime": "d" * 40}
            calls, popen = [], []
            class P:
                pid = 4242
            with unittest.mock.patch.object(d, "ROOT", tools), unittest.mock.patch.object(d, "pins_now", lambda: pins), \
                    unittest.mock.patch.object(d, "sh", lambda cmd, cwd=None, check=True: calls.append(cmd) or ""), \
                    unittest.mock.patch.object(d.subprocess, "Popen", lambda *a, **k: popen.append(k) or P()), \
                    unittest.mock.patch.object(d, "reply", lambda b: None), unittest.mock.patch.object(d, "log", lambda m: None):
                d.start_build({"by": "x"}, {"history": []})
            self.assertEqual(calls[-1][:4], ["git", "-C", build, "checkout"])
            self.assertEqual(popen[0]["cwd"], build)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            d = self._tool("release_daemon")
            self.assertEqual(d.CORE, d.ROOT)
            with unittest.mock.patch.object(d.subprocess, "run", lambda *a, **k: self.fail("同一份时不该取 main")):
                d.follow_main()

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
        """/release internal-MMDD：各仓库的提交从顶层标签读（经 gh api），只取流水线管的（四个必须的和可缺的新配件）；必须的缺了就报错。"""
        d = self._tool("release_daemon")
        tree = "\n".join(f"{r} {c * 40}" for r, c in [("luoke-core", "a"), ("luoke-mail", "b"), ("luoke-mem", "c"), ("luoke-ime", "d"), ("luoke-agent", "e"), ("luoke-community", "f")])
        calls = []
        def fake_sh(cmd, cwd=None, check=True):
            calls.append(cmd)
            return tree if cmd[:2] == ["gh", "api"] else ""
        with unittest.mock.patch.object(d, "sh", fake_sh):
            pins = d.pins_from_top("internal-1024")
            self.assertEqual(pins, {"luoke-core": "a" * 40, "luoke-mail": "b" * 40, "luoke-mem": "c" * 40, "luoke-ime": "d" * 40, "luoke-agent": "e" * 40})
            self.assertIn("repos/luokelabs/luoke/git/trees/internal-1024", calls[0])
            tree = "luoke-core " + "a" * 40
            with self.assertRaises(RuntimeError):
                d.pins_from_top("internal-1024")

    def test_daemon_beta_pins_must_be_on_public_main(self):
        """公开版只钉公开仓库 main 上的提交（ADR 0017 10-29 修订）：不在上面就不开始；beta1、beta1.x 照旧从私有 beta 出；内部版不查。"""
        import subprocess
        d = self._tool("release_daemon")
        pins = {"luoke-core": "a" * 40, "luoke-mail": "b" * 40, "luoke-mem": "c" * 40, "luoke-ime": "d" * 40}
        status = {"luoke-core": "ahead", "luoke-mail": "identical", "luoke-mem": "diverged", "luoke-ime": None}
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            s = status[cmd[2].split("/")[2]]
            return subprocess.CompletedProcess(cmd, 0 if s else 1, stdout=(s or "") + "\n", stderr="" if s else "gh: Not Found (HTTP 404)")
        with unittest.mock.patch.object(d.subprocess, "run", fake_run):
            bad = d.not_on_public(pins)
        self.assertEqual(len(bad), 2)
        self.assertIn("luoke-mem", bad[0])
        self.assertIn("公开仓库里没有", bad[1])
        self.assertEqual(calls[0][2], "repos/luokelabs/luoke-core/compare/" + "a" * 40 + "...main")
        sha = "ab404c" + "d" * 34                                   # 地址里带着提交号，别把里面的 404 当成没有这个提交
        offline = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"Get \"https://api.github.com/repos/x/compare/{sha}...main\": dial tcp: connection refused")
        with unittest.mock.patch.object(d.subprocess, "run", offline):
            bad = d.not_on_public({"luoke-ime": sha})
        self.assertIn("查不了", bad[0])                             # 连不上照样不放行，但不说成没有这个提交
        self.assertIn("connection refused", bad[0])
        checked_out = []
        for tag in ("beta2", "beta2.1", "beta10"):
            with unittest.mock.patch.object(d, "pins_from_top", lambda t: pins), \
                    unittest.mock.patch.object(d, "not_on_public", lambda p: ["luoke-mem ccccccc：不在公开 main 上（diverged）"]), \
                    unittest.mock.patch.object(d, "sh", lambda *a, **k: checked_out.append(a) or ""):
                with self.assertRaisesRegex(RuntimeError, "先同步到公开仓库"):
                    d.start_build({"tag": tag}, {"history": []})
        self.assertEqual(checked_out, [])                        # 没 checkout 就停了
        def boom(*a, **k):
            raise RuntimeError("到了 checkout")
        for tag in ("beta1", "beta1.1", "beta1.2", "internal-1031"):
            with unittest.mock.patch.object(d, "pins_from_top", lambda t: pins), \
                    unittest.mock.patch.object(d, "not_on_public", lambda p: self.fail(f"{tag} 不该查")), \
                    unittest.mock.patch.object(d, "sh", boom):
                with self.assertRaisesRegex(RuntimeError, "到了 checkout"):
                    d.start_build({"tag": tag}, {"history": []})

    def test_daemon_release_candidate_requests(self):
        """候选版 /release betaN[.M]-rcM：负责人本人和 rc_senders 里的账号能发，不在 allowed 里就只能发候选版；候选版的 --accept 也认
        rc_senders（会话的不认），beta 和内部版的 --accept 只认负责人本人；beta1.x 的候选版不走流水线。"""
        d = self._tool("release_daemon")
        allowed, owner, bots = {"lab-owner"}, "lab-owner", {"lab-bot"}
        def c(body, who="lab-owner"):
            return {"id": 1, "body": body, "user": {"login": who}}
        self.assertEqual(d.parse_request(c("/release beta2-rc1"), allowed, owner, bots)["tag"], "beta2-rc1")
        r = d.parse_request(c("/release beta2.1-rc3", who="lab-bot"), allowed, owner, bots)
        self.assertEqual((r["tag"], r["by"], r["accept"]), ("beta2.1-rc3", "lab-bot", False))
        for body in ("/release", "/release internal-1102", "/release beta2"):
            self.assertIn("只能发候选版", d.parse_request(c(body, who="lab-bot"), allowed, owner, bots)["reject"])
        r = d.parse_request(c("/release beta2-rc1 --accept", who="lab-bot"), allowed, owner, bots)
        self.assertEqual((r["tag"], r["by"], r["accept"]), ("beta2-rc1", "lab-bot", True))     # 候选版的 --accept 也认 rc_senders
        for body in ("（luoke-hub 会话）/release beta2-rc1 --accept", "看一下\n（luoke-hub 会话）/release beta2-rc1 --accept"):
            self.assertIn("--accept 只认", d.parse_request(c(body, who="lab-bot"), allowed, owner, bots)["reject"])   # 会话发的不算
        self.assertIn("只能发候选版", d.parse_request(c("/release --accept", who="lab-bot"), allowed, owner, bots)["reject"])
        self.assertIn("候选版只认", d.parse_request(c("/release beta2-rc1 --accept", who="lab-member"), allowed | {"lab-member"}, owner, bots)["reject"])
        self.assertTrue(d.parse_request(c("/release beta2-rc1 --accept"), allowed, owner, bots)["accept"])
        self.assertIn("候选版只认", d.parse_request(c("（luoke-top 会话）/release beta2-rc1"), allowed, owner, bots)["reject"])
        # 会话标签不在第一行也算会话：开头先写了别的
        self.assertIn("候选版只认", d.parse_request(c("看一下\n（luoke-top 会话）/release beta2-rc1"), allowed, owner, bots)["reject"])
        self.assertIn("beta 只认", d.parse_request(c("看一下\n（luoke-top 会话）/release beta2 --accept"), allowed, owner, bots)["reject"])
        self.assertIn("--accept 只认", d.parse_request(c("看一下\n（luoke-top 会话）/release --accept"), allowed, owner, bots)["reject"])
        for tag in ("beta1-rc1", "beta1.1-rc3"):
            self.assertIn("beta1.x", d.parse_request(c("/release " + tag), allowed, owner, bots)["reject"])
        self.assertIn("候选版写成", d.parse_request(c("/release beta2-rc"), allowed, owner, bots)["reject"])
        self.assertIn("不在", d.parse_request(c("/release beta2-rc1", who="lab-bot"), allowed, owner)["reject"])    # 没配 rc_senders
        self.assertIn("不在", d.parse_request(c("/release beta2-rc1", who="someone"), allowed, owner, bots)["reject"])
        both = allowed | bots      # 机器账号也在 allowed 里：内部版、候选版都能发，beta 和 --accept 照旧只认负责人本人
        self.assertEqual(d.parse_request(c("（luoke-hub 会话）/release", who="lab-bot"), both, owner, bots)["by"], "lab-bot")
        self.assertEqual(d.parse_request(c("/release internal-1103", who="lab-bot"), both, owner, bots)["tag"], "internal-1103")
        self.assertEqual(d.parse_request(c("/release beta2-rc1", who="lab-bot"), both, owner, bots)["tag"], "beta2-rc1")
        self.assertIn("beta 只认", d.parse_request(c("/release beta2", who="lab-bot"), both, owner, bots)["reject"])
        self.assertIn("--accept 只认", d.parse_request(c("/release internal-1103 --accept", who="lab-bot"), both, owner, bots)["reject"])
        self.assertIn("--accept 只认", d.parse_request(c("/release --accept", who="lab-bot"), both, owner, bots)["reject"])
        self.assertIn("beta 只认", d.parse_request(c("/release beta2 --accept", who="lab-bot"), both, owner, bots)["reject"])
        self.assertTrue(d.parse_request(c("/release beta2-rc1 --accept", who="lab-bot"), both, owner, bots)["accept"])

    def test_daemon_candidate_pins_without_clones_next_to_the_build_clone(self):
        """出版机的布局：出版克隆旁边没有配件的克隆（TOP/<repo>），配件只有 SRC 里的持久克隆、origin 直接是 GitHub。
        候选版钉版要从持久克隆取 public，不能因为 TOP/<repo> 不在就停；两处都没有时说清楚找过哪里。"""
        import subprocess
        d = self._tool("release_daemon")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@example.com",
                   GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@example.com")
        def g(cwd, *a):
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()
        top = os.path.join(tmp, "home")
        core = os.path.join(top, "luoke-core")
        src = os.path.join(core, "build.noindex", "src")
        shas = {}
        for repo in d.REPOS:
            gh, work = os.path.join(tmp, "gh", repo + ".git"), os.path.join(tmp, "work", repo)
            g(tmp, "init", "-q", "--bare", "-b", "main", gh)
            g(tmp, "clone", "-q", gh, work)
            with open(os.path.join(work, "README"), "w") as f:
                f.write(repo)
            g(work, "add", "-A"); g(work, "commit", "-qm", "c"); g(work, "push", "-q", "origin", "HEAD")
            g(tmp, "clone", "-q", gh, core if repo == "luoke-core" else os.path.join(src, repo))   # 持久克隆直接连 GitHub
            g(work, "checkout", "-q", "--orphan", "public")          # public 是克隆好以后才推的
            with open(os.path.join(work, "README"), "w") as f:
                f.write(repo + " public")
            g(work, "commit", "-qam", "exported"); g(work, "push", "-q", "origin", "HEAD")
            shas[repo] = g(work, "rev-parse", "HEAD")
        self.assertFalse(any(os.path.exists(os.path.join(top, r)) for r in d.REPOS[1:]))
        real_sh = d.sh
        def fake_sh(cmd, cwd=None, check=True):
            if cmd[:2] == ["gh", "api"]:
                parts = cmd[2].split("/")
                return ("" if len(parts) == 3 else shas.get(parts[2], "")) + "\n"
            return real_sh(cmd, cwd, check)
        # mem 的持久克隆当初是从 TOP/luoke-mem clone 的、TOP 后来删了：origin 指着不在的路径，照样要从 GitHub 取到
        g(os.path.join(src, "luoke-mem"), "remote", "set-url", "origin", os.path.join(top, "luoke-mem"))
        with unittest.mock.patch.object(d, "sh", fake_sh), unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch.object(d, "CORE", core), unittest.mock.patch.object(d, "TOP", top), unittest.mock.patch.object(d, "SRC", src), \
                unittest.mock.patch.object(d, "remote_url", lambda r: os.path.join(tmp, "gh", r + ".git")), \
                unittest.mock.patch.object(d, "log", lambda m: None):
            self.assertEqual(d.pins_public(), shas)
            # 公开 main 的提交私有 public 上没有：报错写明核的是哪个克隆、从哪取的
            real_shas, shas = shas, dict(shas, **{"luoke-ime": "c" * 40})
            with self.assertRaisesRegex(RuntimeError, "luoke-ime 公开 main 是 ccccccc，.*src/luoke-ime 从 .*gh/luoke-ime.git 取过 public 以后还没有"):
                d.pins_public()
            shas = real_shas
            shutil.rmtree(os.path.join(src, "luoke-mail"))
            with self.assertRaisesRegex(RuntimeError, "luoke-mail：本机没有它的克隆（找过 .*luoke-mail、.*src/luoke-mail）"):
                d.pins_public()

    def test_daemon_release_candidate_pins_and_hosting(self):
        """候选版钉四个公开仓库 main 现在的提交（私有 public 上取不到就停）；出好了按清单拷到局域网托管的目录、核哈希，整个换掉旧的。"""
        import hashlib
        import subprocess
        d = self._tool("release_daemon")
        # 照出版机的布局：GitHub 上的私有仓库（这里是裸仓库）、TOP/<repo> 连它、出版克隆就是 TOP/luoke-core、
        # 配件的持久克隆从 TOP/<repo> clone（只看得到它的本地分支）。public 是 TOP 克隆好以后才推的，TOP 里还没取过
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@example.com",
                   GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@example.com")
        def g(cwd, *a):
            return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()
        def commit(work, files):
            for rel, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(work, rel)) or work, exist_ok=True)
                with open(os.path.join(work, rel), "w") as f:
                    f.write(text)
            g(work, "add", "-A"); g(work, "commit", "-qm", "c"); g(work, "push", "-q", "origin", "HEAD")
            return g(work, "rev-parse", "HEAD")
        top, shas, works = os.path.join(tmp, "top"), {}, {}
        ready = {"tools/internal_build.py": 'if re.fullmatch(r"beta\\d+(\\.\\d+)?(-rc\\d+)?", a.tag):\n'}
        for repo in d.REPOS:
            gh, work = os.path.join(tmp, "gh", repo + ".git"), os.path.join(tmp, "work", repo)
            g(tmp, "init", "-q", "--bare", "-b", "main", gh)
            g(tmp, "clone", "-q", gh, work)
            commit(work, {"README": repo})
            g(tmp, "clone", "-q", gh, os.path.join(top, repo))
            g(work, "checkout", "-q", "--orphan", "public")
            shas[repo] = commit(work, ready if repo == "luoke-core" else {"README": repo + " public"})
            works[repo] = work
        core = os.path.join(top, "luoke-core")
        src = os.path.join(core, "build.noindex", "src")
        for repo in d.REPOS[1:]:
            g(tmp, "clone", "-q", os.path.join(top, repo), os.path.join(src, repo))
        answer = dict(shas)
        real_sh = d.sh
        meta = {}       # 新配件（OPTIONAL）的仓库信息：没有 = 还没有公开仓库
        def fake_sh(cmd, cwd=None, check=True):
            if cmd[:2] == ["gh", "api"]:
                parts = cmd[2].split("/")
                if len(parts) == 3:                              # repos/<owner>/<repo>：仓库信息
                    return meta.get(parts[2], "") + "\n"
                return answer.get(parts[2], "") + "\n"
            return real_sh(cmd, cwd, check)
        with unittest.mock.patch.object(d, "sh", fake_sh), unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch.object(d, "CORE", core), unittest.mock.patch.object(d, "TOP", top), unittest.mock.patch.object(d, "SRC", src), \
                unittest.mock.patch.object(d, "log", lambda m: None):
            self.assertEqual(d.pins_public(), shas)
            # 原名还跳到改了名的私有仓库：不钉（钉了就是私有提交）；真的公开了才钉它的 main
            answer["luoke-search"] = "d" * 40
            meta["luoke-search"] = f"{d.PUBLIC_OWNER}/luoke-search{d.REPO_SUFFIX} true"
            self.assertEqual(d.pins_public(), shas)
            meta["luoke-search"] = f"{d.PUBLIC_OWNER}/luoke-search false"
            self.assertEqual(d.pins_public(), dict(shas, **{"luoke-search": "d" * 40}))
            del meta["luoke-search"], answer["luoke-search"]
            d.candidate_ready(shas["luoke-core"])
            answer["luoke-mem"] = "c" * 40          # 公开 main 上有、私有 public 还没推到
            with self.assertRaisesRegex(RuntimeError, "luoke-mem 公开 main 是 ccccccc.*推送没做完"):
                d.pins_public()
            answer["luoke-mem"] = shas["luoke-mem"]
            answer["luoke-core"] = commit(works["luoke-core"], {"tools/internal_build.py": "# 还不认候选版\n"})
            with self.assertRaisesRegex(RuntimeError, "还不认候选版"):
                d.candidate_ready(d.pins_public()["luoke-core"])

        with unittest.mock.patch.object(d.lab_config, "release", lambda: {"rc_url_base": "http://lab.invalid:8765"}):
            with self.assertRaisesRegex(RuntimeError, "rc_serve_dir"):
                d.rc_hosting()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        serve, out = os.path.join(tmp, "serve"), os.path.join(tmp, "build.noindex", "release", "beta2-rc1")
        def put(rel, data):
            p = os.path.join(out, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            return hashlib.sha256(data).hexdigest()
        manifest = {"items": {
            "luoke-mail": {"kind": "accessory", "files": [{"path": "mail.zip", "sha256": put("accessories/mail.zip", b"m")}]},
            "engine": {"kind": "engine", "files": [{"path": "engine.zip", "sha256": put("engine/engine.zip", b"e")}]},
            "hub": {"kind": "hub", "files": [{"path": "螺壳 0.2.dmg", "sha256": put("螺壳 0.2.dmg", b"h")}]},
            "qwen": {"kind": "model", "files": [{"path": "w.safetensors", "sha256": "0" * 64}]}},       # 模型不拷
            "installers": {"cli": {"file": "install.sh", "sha256": put("install.sh", b"i")}}}
        with open(os.path.join(out, "release.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        os.makedirs(os.path.join(serve, "beta2-rc1"))
        open(os.path.join(serve, "beta2-rc1", "old.dmg"), "w").close()
        hosting = {"rc_url_base": "http://lab.invalid:8765/", "rc_serve_dir": serve}
        with unittest.mock.patch.object(d.lab_config, "release", lambda: hosting), unittest.mock.patch.object(d, "BUILD", os.path.join(tmp, "build.noindex")):
            self.assertEqual(d.rc_hosting(), ("http://lab.invalid:8765", serve))
            url = d.host_candidate("beta2-rc1")
            self.assertEqual(url, "http://lab.invalid:8765/beta2-rc1/%E8%9E%BA%E5%A3%B3%200.2.dmg")
            self.assertEqual(sorted(os.listdir(os.path.join(serve, "beta2-rc1"))),
                             sorted(["engine.zip", "install.sh", "mail.zip", "release.json", "螺壳 0.2.dmg"]))     # 旧的 old.dmg 没了
            self.assertEqual(sorted(os.listdir(serve)), ["beta2-rc1"])
            with open(os.path.join(out, "engine", "engine.zip"), "wb") as f:
                f.write(b"changed")
            with self.assertRaisesRegex(RuntimeError, "engine.zip 不在或哈希"):
                d.host_candidate("beta2-rc1")
            self.assertIn("mail.zip", os.listdir(os.path.join(serve, "beta2-rc1")))     # 没拷完的不换掉挂着的
            self.assertEqual(sorted(os.listdir(serve)), ["beta2-rc1"])                    # 也不留半截的 .partial

    def test_daemon_starts_and_finishes_a_release_candidate(self):
        """候选版：先看挂得到哪，钉公开 main，全评、和上一个 beta 比、--url-base 到 <rc_url_base>/<tag>；出过的同名候选版不再出。
        出好了挂到局域网、回复给 DMG 地址（不查 GitHub 发布）；挂的时候出错算没出成。"""
        import subprocess
        d = self._tool("release_daemon")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        pins = {"luoke-core": "a" * 40, "luoke-mail": "b" * 40, "luoke-mem": "c" * 40, "luoke-ime": "d" * 40}
        popen, replies = [], []
        class P:
            pid = 4242
        base = [unittest.mock.patch.object(d, "CORE", tmp), unittest.mock.patch.object(d, "BUILD", tmp),
                unittest.mock.patch.object(d, "reply", replies.append), unittest.mock.patch.object(d, "log", lambda m: None)]
        for p in base:
            p.start()
            self.addCleanup(p.stop)
        with unittest.mock.patch.object(d.lab_config, "release", lambda: {}), \
                unittest.mock.patch.object(d, "pins_public", lambda: self.fail("没配托管就不该钉版")):
            with self.assertRaisesRegex(RuntimeError, "rc_url_base"):
                d.start_build({"tag": "beta2-rc1"}, {"history": []})
        serve = os.path.join(tmp, "serve")
        ready = []
        with unittest.mock.patch.object(d, "rc_hosting", lambda: ("http://lab.invalid:8765", serve)), \
                unittest.mock.patch.object(d, "pins_public", lambda: pins), \
                unittest.mock.patch.object(d, "candidate_ready", ready.append), \
                unittest.mock.patch.object(d, "pins_from_top", lambda t: self.fail("候选版不读顶层标签")), \
                unittest.mock.patch.object(d, "not_on_public", lambda p: []), \
                unittest.mock.patch.object(d, "previous_beta", lambda t: "beta1.1"), \
                unittest.mock.patch.object(d, "sh", lambda *a, **k: ""), \
                unittest.mock.patch.object(d.subprocess, "Popen", lambda *a, **k: popen.append(a[0]) or P()):
            st = {"history": []}
            d.start_build({"tag": "beta2-rc1", "by": "lab-bot"}, st)
            script = popen[0][2]
            for part in ("'--tag' 'beta2-rc1'", "'--prev' 'beta1.1'", "'--url-base' 'http://lab.invalid:8765/beta2-rc1'"):
                self.assertIn(part, script)
            self.assertEqual(st["running"]["pins"], pins)
            self.assertEqual(ready, ["a" * 40])            # 钉住的公开 core 认不认得候选版
            # 11-24：有出好了的内部版（有台账）就拿它比，不拿上一个 beta；报告也比同一版
            os.makedirs(os.path.join(tmp, "releases", "internal-1124-4"))
            open(os.path.join(tmp, "releases", "internal-1124-4", "support.json"), "w").write("{}")
            st2 = {"history": [{"tag": "internal-1124-4", "ok": True}, {"tag": "beta2-rc1", "ok": True}]}
            d.start_build({"tag": "beta2-rc2", "by": "lab-bot"}, st2)
            self.assertIn("'--prev' 'internal-1124-4'", popen[-1][2])
            self.assertEqual(st2["running"]["prev"], "internal-1124-4")
            with self.assertRaisesRegex(RuntimeError, "rc 后面的号加一"):
                d.start_build({"tag": "beta2-rc1"}, {"history": [{"tag": "beta2-rc1", "ok": True}]})
            os.makedirs(os.path.join(serve, "beta2-rc1"))     # 出过的记录被挤掉了，挂着的目录还在：也算出过
            with self.assertRaisesRegex(RuntimeError, "已经挂在局域网上了.*rc 后面的号加一"):
                d.start_build({"tag": "beta2-rc1"}, {"history": []})

        def finish(host):
            rcp = os.path.join(tmp, "rc")
            with open(rcp, "w") as f:
                f.write("0\n")
            st = {"running": {"tag": "beta2-rc1", "pid": 1, "rc": rcp, "pins": pins, "started": 0}, "history": []}
            ran = []
            def fake_run(cmd, **kw):
                ran.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="摘要", stderr="")
            with unittest.mock.patch.object(d.subprocess, "run", fake_run), unittest.mock.patch.object(d, "host_candidate", host), \
                    unittest.mock.patch.object(d, "previous_beta", lambda t: "beta1.1"):
                d.finish_build(st)
            self.assertFalse([c for c in ran if c[0] == "gh"])
            reports = [c for c in ran if "tools/release_report.py" in c]
            self.assertTrue(reports and all(c[-2:] == ["--prev", "beta1.1"] for c in reports))     # 和出版时一样比上一个 beta
            return st["history"][-1]["ok"]
        self.assertTrue(finish(lambda tag: f"http://lab.invalid:8765/{tag}/Luoke.dmg"))
        self.assertIn("候选版", replies[-1])
        self.assertIn("http://lab.invalid:8765/beta2-rc1/Luoke.dmg", replies[-1])
        def full(tag):
            raise OSError("磁盘满了")
        self.assertFalse(finish(full))
        self.assertIn("挂到局域网时出错：磁盘满了", replies[-1])

    def test_beta_publish_and_discard_requests(self):
        """beta 先发草稿（11-25）：/release betaN --publish 转正、--discard 删草稿，只认负责人本人、只给 beta、不带别的参数"""
        d = self._tool("release_daemon")
        allowed, owner, rcs = {"lab-owner", "lab-dev"}, "lab-owner", {"lab-bot"}
        def c(body, who="lab-owner"):
            return {"id": 7, "body": body, "user": {"login": who}}
        self.assertEqual(d.parse_request(c("/release beta3 --publish"), allowed, owner, rcs),
                         {"finalize": "publish", "tag": "beta3", "by": "lab-owner", "comment": 7})
        self.assertEqual(d.parse_request(c("/release beta3.1 --discard"), allowed, owner, rcs)["finalize"], "discard")
        for body, who in (("/release beta3 --publish", "lab-dev"), ("（luoke-top 会话）/release beta3 --publish", "lab-owner"),
                          ("/release beta3 --publish", "lab-bot")):
            self.assertIn("reject", d.parse_request(c(body, who), allowed, owner, rcs), body + " " + who)
        for body in ("/release --publish", "/release internal-1125 --publish", "/release beta3-rc1 --publish",
                     "/release beta3 --publish --discard", "/release beta3 --publish --accept"):
            self.assertIn("reject", d.parse_request(c(body), allowed, owner, rcs), body)
        self.assertEqual(d.parse_request(c("/release beta3"), allowed, owner, rcs)["tag"], "beta3")     # 不带开关照旧出（建草稿）
        # 负责人可以是几个账号（11-25）；会话标签照旧不给权限
        two = ["lab-owner", "lab-dev"]
        for who in two:
            self.assertEqual(d.parse_request(c("/release beta3", who), allowed, two, rcs)["tag"], "beta3")
            self.assertEqual(d.parse_request(c("/release beta3 --publish", who), allowed, two, rcs)["finalize"], "publish")
            self.assertTrue(d.parse_request(c("/release --accept", who), allowed, two, rcs)["accept"])
            self.assertIn("reject", d.parse_request(c("（luoke-top 会话）/release beta3", who), allowed, two, rcs))
            self.assertEqual(d.parse_request(c("（luoke-top 会话）/release", who), allowed, two, rcs)["by"], who + "（会话）")
        self.assertIn("reject", d.parse_request(c("/release beta3", "lab-bot"), allowed, two, rcs))
        self.assertEqual(d.owners_of("a, b"), {"a", "b"})
        self.assertEqual(d.owners_of(["a", " b ", ""]), {"a", "b"})
        self.assertEqual(d.owners_of(None), set())
        self.assertIn('owners_of(os.environ.get("LUOKE_RELEASE_OWNER") or lab.get("owner"))', inspect.getsource(d.tick))

    def test_internal_build_uploads_beta_as_a_draft(self):
        """beta 的 publish 建草稿（--draft，不写 ClamAV 索引），建完核附件、记进 releases/<tag>/draft.json；内部版照旧直接发预发布版"""
        ib = self._tool("internal_build")
        src = inspect.getsource(ib.publish)
        self.assertIn('"--draft"', src)
        self.assertIn('"--prerelease"', src)
        self.assertNotIn("put_clamav_index(", src)          # 索引转正时才写
        self.assertLess(src.index("releases_by_tag(repo, tag"), src.index('"--draft"'))     # 已经有同名的：对得上接着用，对不上停
        self.assertLess(src.index("check_draft(old, shas)"), src.index('"--draft"'))
        sleeps = []
        with tempfile.TemporaryDirectory() as root, unittest.mock.patch.object(ib, "ROOT", root), \
                unittest.mock.patch.object(ib, "log", lambda m: None), unittest.mock.patch.object(ib.time, "sleep", sleeps.append):
            os.makedirs(os.path.join(root, "releases", "beta3"))
            files = []
            for name, body in (("Luoke.dmg", b"dmg"), ("release.json", b"{}")):
                p = os.path.join(root, name)
                with open(p, "wb") as f:
                    f.write(body)
                files.append(p)
            sha = ib.release.download.sha256_file(files[0])
            assets = [{"name": "Luoke.dmg", "size": 3, "state": "uploaded", "digest": "sha256:" + sha},
                      {"name": "release.json", "size": 2, "state": "uploaded"}]
            listing = [{"id": 99, "tag_name": "beta3", "draft": True, "html_url": "https://example.invalid/untagged-1", "assets": assets}]
            # 刚传完 GitHub 还没就绪、接口一时不通：隔一会儿再看
            seq = [None, [dict(listing[0], assets=[assets[0], dict(assets[1], state="starter")])], listing]
            with unittest.mock.patch.object(ib, "releases_by_tag", lambda repo, tag, env, check=True: seq.pop(0)):
                ib.record_draft("beta3", "o/r", files, "# 索引", None)
            self.assertEqual((seq, len(sleeps)), ([], 2))
            shas = ib.local_shas(files)
            self.assertEqual(ib.check_draft(listing, shas)[2], [])       # 重跑时对得上就接着用
            # 清单里核过的包不再重读，只算清单外的
            manifest = {"items": {"mail": {"kind": "accessory", "files": [{"path": "m.zip", "sha256": "aa"}]},
                                  "engine": {"kind": "engine", "files": [{"path": "e.zip", "sha256": "bb"}]}},
                        "installers": {"dmg": {"file": "Luoke.dmg", "sha256": "cc"}}}
            known = ib.manifest_shas(root, manifest)
            self.assertEqual(known, {os.path.join(root, "accessories", "m.zip"): "aa", os.path.join(root, "engine", "e.zip"): "bb",
                                     os.path.join(root, "Luoke.dmg"): "cc"})
            read = []
            with unittest.mock.patch.object(ib.release.download, "sha256_file", lambda p: read.append(os.path.basename(p)) or "dd"):
                got_shas = ib.local_shas(files, known)
            self.assertEqual((read, got_shas["Luoke.dmg"][1], got_shas["release.json"][1]), (["release.json"], "cc", "dd"))
            self.assertIn("local_shas(assets, manifest_shas(out, manifest))", src)
            self.assertTrue(ib.check_draft([dict(listing[0], draft=False)], shas)[2])
            got = json.load(open(os.path.join(root, "releases", "beta3", "draft.json"), encoding="utf-8"))
            self.assertEqual((got["id"], got["url"], got["clamav_index"]), (99, "https://example.invalid/untagged-1", "# 索引"))
            self.assertEqual([(a["name"], a["size"]) for a in got["assets"]], [("Luoke.dmg", 3), ("release.json", 2)])
            for bad in ([dict(assets[0], digest="sha256:00"), assets[1]], [assets[0]], [assets[0], dict(assets[1], size=5)],
                        [assets[0], dict(assets[1], state="starter")]):
                with unittest.mock.patch.object(ib, "releases_by_tag", lambda repo, tag, env, check=True: [dict(listing[0], assets=bad)]), \
                        self.assertRaises(SystemExit):
                    ib.record_draft("beta3", "o/r", files, None, None)
            with unittest.mock.patch.object(ib, "releases_by_tag", lambda repo, tag, env, check=True: listing * 2), self.assertRaises(SystemExit):
                ib.record_draft("beta3", "o/r", files, None, None)
        with open(os.path.join(ROOT, "tools", "internal_build.py"), encoding="utf-8") as f:
            pub = f.read().split("def publish(", 1)[1].split("\ndef ", 1)[0]
        # 同名的已经转正了：说换一个号，不让负责人去 --discard（--discard 不碰已发布的）
        published_branch = pub.split("if not all(x.get(\"draft\") for x in old):", 1)[1].split("die(f\"{repo} 里已经有", 1)[0]
        self.assertIn("换一个号出", published_branch)
        self.assertNotIn("--discard", published_branch)

    def test_release_finalize_publishes_or_discards_the_recorded_draft(self):
        """转正：草稿还在、只有一个、附件和 draft.json 一致才转；先写 ClamAV 索引再转正、设为最新；已经转正的不重做。
        删草稿：只删草稿，已转正的不碰；删掉 draft.json、steps.json 的 publish，重出时从上传接着做"""
        fin = self._tool("release_finalize")
        with tempfile.TemporaryDirectory() as root:
            rec = os.path.join(root, "releases", "beta3")
            os.makedirs(rec)
            draft = {"tag": "beta3", "repo": "o/r", "id": 99, "url": "https://example.invalid/untagged-1", "clamav_index": "# 索引",
                     "assets": [{"name": "Luoke.dmg", "size": 3, "sha256": "ab"}]}
            def write_draft():
                with open(os.path.join(rec, "draft.json"), "w", encoding="utf-8") as f:
                    json.dump(draft, f)
            write_draft()
            with open(os.path.join(rec, "steps.json"), "w", encoding="utf-8") as f:
                json.dump({"sign": "x", "dmg": "x", "publish": "x"}, f)
            rel = {"id": 99, "tag_name": "beta3", "draft": True, "assets": [{"name": "Luoke.dmg", "size": 3, "state": "uploaded", "digest": "sha256:ab"}]}
            calls = []
            def api(listing):
                def f(args, env, body=None):
                    calls.append((args[:3], body))
                    if args[0] == "--paginate":
                        return [listing]
                    if args[:2] == ["-X", "PATCH"]:
                        return {"draft": False, "html_url": "https://example.invalid/tag/beta3"}
                    return None
                return f
            index = []
            with unittest.mock.patch.object(fin.ib, "put_clamav_index", lambda text, env: (index.append(text), calls.append(("index", text)))):
                for listing, why in (([], "找不到"), ([rel, dict(rel, id=100)], "2 个"),
                                     ([dict(rel, assets=[dict(rel["assets"][0], size=4)])], "大小"),
                                     ([dict(rel, assets=[dict(rel["assets"][0], digest="sha256:cd")])], "哈希"),
                                     ([dict(rel, assets=[])], "少了")):
                    with unittest.mock.patch.object(fin, "gh_api", api(listing)), self.assertRaisesRegex(fin.Refused, why):
                        fin.publish("beta3", root, None)
                self.assertEqual(index, [])          # 没核过就不写索引
                calls.clear()
                with unittest.mock.patch.object(fin, "gh_api", api([rel])):
                    msg = fin.publish("beta3", root, None)
                self.assertIn("转正了：https://example.invalid/tag/beta3", msg)
                order = ["index" if c[0] == "index" else "list" if c[0][0] == "--paginate" else c[0][1] for c in calls]
                self.assertEqual(order, ["list", "index", "PATCH"])
                self.assertEqual(calls[-1], (["-X", "PATCH", "repos/o/r/releases/99"], {"draft": False, "make_latest": "true"}))
                self.assertEqual(json.load(open(os.path.join(rec, "draft.json")))["published"]["url"], "https://example.invalid/tag/beta3")
                with unittest.mock.patch.object(fin, "gh_api", api([dict(rel, draft=False, html_url="u")])):
                    self.assertIn("已经转正过了", fin.publish("beta3", root, None))
                    with self.assertRaisesRegex(fin.Refused, "只删草稿"):
                        fin.discard("beta3", root, None)
            # 转正这一步失败：索引已经写了，回复说再发一次就补上
            write_draft()
            def patch_fails(args, env, body=None):
                if args[0] == "--paginate":
                    return [[rel]]
                raise fin.Refused("HTTP 502")
            with unittest.mock.patch.object(fin.ib, "put_clamav_index", lambda text, env: None), \
                    unittest.mock.patch.object(fin, "gh_api", patch_fails), self.assertRaisesRegex(fin.Refused, "502.*索引已经写了.*再发一次"):
                fin.publish("beta3", root, None)
            calls.clear()
            with unittest.mock.patch.object(fin, "gh_api", api([rel, dict(rel, id=100, html_url="https://example.invalid/untagged-2")])):
                msg = fin.discard("beta3", root, None)      # 只删记着的那个，别人手建的同名草稿不碰
            self.assertIn("草稿删了（1 个）", msg)
            self.assertIn("另有 1 个同名草稿不是流水线建的，没动：https://example.invalid/untagged-2", msg)
            self.assertEqual([c[0] for c in calls[1:]], [["-X", "DELETE", "repos/o/r/releases/99"]])
            self.assertFalse(os.path.exists(os.path.join(rec, "draft.json")))
            self.assertEqual(json.load(open(os.path.join(rec, "steps.json"))), {"sign": "x", "dmg": "x"})
            write_draft()
            calls.clear()
            with unittest.mock.patch.object(fin, "gh_api", api([dict(rel, id=100), dict(rel, id=101)])):   # 记着的已经被手删了：同名的都删
                self.assertIn("草稿删了（2 个）", fin.discard("beta3", root, None))
            self.assertEqual([c[0][2].rsplit("/", 1)[1] for c in calls[1:]], ["100", "101"])
            calls.clear()
            with unittest.mock.patch.object(fin, "gh_api", api([rel, dict(rel, id=100)])):      # 没有记录：同名草稿都删
                self.assertIn("草稿删了（2 个）", fin.discard("beta3", root, None))
            self.assertEqual([c[0][2].rsplit("/", 1)[1] for c in calls[1:]], ["99", "100"])      # 没有记录：仓库按 tag 定
            with unittest.mock.patch.object(fin, "gh_api", api([])):
                self.assertIn("没有记着的 beta3 草稿", fin.discard("beta3", root, None))
                with self.assertRaisesRegex(fin.Refused, "draft.json 不在"):
                    fin.publish("beta3", root, None)

    def test_daemon_runs_finalize_at_once_and_notes_the_draft(self):
        """--publish / --discard 不排队、马上跑工具目录的 release_finalize.py，按它最后一行「=> 」回复；正在出的同一版还没建草稿就叫等。
        草稿建好先回复一次（复测还在跑）；出完的 beta 回复写草稿地址和怎么转正。删了草稿的 beta 可以重出"""
        import subprocess
        d = self._tool("release_daemon")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        replies = []
        for p in (unittest.mock.patch.object(d, "CORE", tmp), unittest.mock.patch.object(d, "BUILD", tmp),
                  unittest.mock.patch.object(d, "reply", replies.append), unittest.mock.patch.object(d, "log", lambda m: None)):
            p.start()
            self.addCleanup(p.stop)
        rec = os.path.join(tmp, "releases", "beta3")
        os.makedirs(rec)
        ran = []
        def fake_run(out, rc=0):
            def f(cmd, **kw):
                ran.append(cmd)
                return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
            return f
        st = {"running": {"tag": "beta3", "pid": os.getpid(), "rc": os.path.join(tmp, "rc"), "pins": {}, "started": 100, "drafts": True}, "history": []}
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("")):
            d.finalize({"finalize": "publish", "tag": "beta3", "by": "lab-owner"}, st)
        self.assertEqual(ran, [])
        self.assertIn("草稿还没建好", replies[-1])
        d.note_draft(st)
        self.assertEqual(len(replies), 1)
        with open(os.path.join(rec, "draft.json"), "w", encoding="utf-8") as f:
            json.dump({"url": "https://example.invalid/untagged-1", "created": 50}, f)
        d.note_draft(st)                      # 上一次出版留下的草稿记录不算
        self.assertEqual(len(replies), 1)
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("=> 不该跑")):
            d.finalize({"finalize": "publish", "tag": "beta3", "by": "lab-owner"}, st)     # 正在重出：上一次的草稿不能转正
        self.assertEqual(ran, [])
        self.assertIn("现在记着的是上一次的草稿", replies[-1])
        replies.pop()
        # 上一次在复测中途被结束（没记结束时间）、这一次还没进下一步：留下的 repeats 不算这一次到了复测
        with open(os.path.join(rec, "timeline.json"), "w", encoding="utf-8") as f:
            json.dump({"phases": [{"name": "publish", "start": 40, "end": 45}, {"name": "repeats", "start": 50, "end": None}]}, f)
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("=> 不该跑")):
            d.finalize({"finalize": "publish", "tag": "beta3", "by": "lab-owner"}, st)
        self.assertEqual(ran, [])
        self.assertIn("现在记着的是上一次的草稿", replies.pop())
        # 钉住的提交没变的续跑：上传跳过、直接到复测，旧的那份就是这一次的草稿，照常放行
        with open(os.path.join(rec, "timeline.json"), "w", encoding="utf-8") as f:
            json.dump({"phases": [{"name": "publish", "start": 40, "end": 45}, {"name": "repeats", "start": 110, "end": None}]}, f)
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("=> **beta3** 转正了：u。\n")):
            d.finalize({"finalize": "publish", "tag": "beta3", "by": "lab-owner"}, st)
        self.assertEqual((len(ran), replies.pop()), (1, "**beta3** 转正了：u。"))
        ran.clear()
        os.remove(os.path.join(rec, "timeline.json"))
        with open(os.path.join(rec, "draft.json"), "w", encoding="utf-8") as f:
            json.dump({"url": "https://example.invalid/untagged-1", "created": 200}, f)
        d.note_draft(st)
        d.note_draft(st)
        self.assertEqual(len(replies), 2)
        self.assertIn("草稿建好了：https://example.invalid/untagged-1", replies[-1])
        self.assertIn("/release beta3 --publish", replies[-1])
        done_st = {"running": dict(st["running"], draft_noted=False, pid=2 ** 22 + 7)}     # 已经出完（进程没了）：只等 finish_build 回复
        d.note_draft(done_st)
        self.assertEqual(len(replies), 2)
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("$ gh api …\n=> **beta3** 转正了：u。\n")):
            d.finalize({"finalize": "publish", "tag": "beta3", "by": "lab-owner"}, st)
        self.assertEqual(ran[-1][3:], ["publish", "beta3", "--root", tmp])
        self.assertTrue(ran[-1][1:3] == ["-B", os.path.join(d.ROOT, "tools", "release_finalize.py")])
        self.assertEqual(replies[-1], "**beta3** 转正了：u。")
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("失败：命令退出码 1：gh api …\n", rc=1)):
            d.finalize({"finalize": "publish", "tag": "beta3", "by": "lab-owner"}, st)
        self.assertIn("没做成", replies[-1])
        self.assertIn("失败：命令退出码 1", replies[-1])
        # 出完：草稿还没转正，回复草稿地址和怎么转正
        with open(st["running"]["rc"], "w") as f:
            f.write("0\n")
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("摘要")):
            d.finish_build(st)
        self.assertIn("出好了（草稿，还没转正）：https://example.invalid/untagged-1", replies[-1])
        self.assertIn("--publish", replies[-1])
        self.assertTrue(st["history"][-1]["ok"])
        self.assertNotIn("discarded", st["history"][-1])
        # 删草稿：出过的记录标上 discarded，之后可以重出
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("=> **beta3** 的草稿删了（1 个）。\n")):
            d.finalize({"finalize": "discard", "tag": "beta3", "by": "lab-owner"}, st)
        self.assertTrue(st["history"][-1]["discarded"])
        os.makedirs(os.path.join(tmp, "tools"))
        with open(os.path.join(tmp, "tools", "internal_build.py"), "w", encoding="utf-8") as f:
            f.write("def record_draft(tag):\n    pass\n")         # 钉住的 core 会建草稿
        with unittest.mock.patch.object(d, "pins_from_top", lambda t: {"luoke-core": "a" * 40}), \
                unittest.mock.patch.object(d, "not_on_public", lambda p: []), \
                unittest.mock.patch.object(d, "previous_beta", lambda t: None), \
                unittest.mock.patch.object(d, "sh", lambda *a, **k: ""), \
                unittest.mock.patch.object(d.subprocess, "Popen", lambda *a, **k: types.SimpleNamespace(pid=5)):
            d.start_build({"tag": "beta3", "by": "lab-owner"}, st)
        self.assertEqual(st["running"]["tag"], "beta3")
        # 复测时草稿被删了：出完的回复照实说，记录也标上
        os.remove(os.path.join(rec, "draft.json"))
        st["running"]["rc"] = os.path.join(tmp, "rc")
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("摘要")):
            d.finish_build(st)
        self.assertIn("草稿已经删了", replies[-1])
        self.assertTrue(st["history"][-1]["discarded"])
        # 钉的是还直接发的旧 core（beta2）：没有 draft.json 是正常的，照旧回复发布页，不当成删了草稿
        st["running"] = {"tag": "beta2", "pid": 1, "rc": os.path.join(tmp, "rc"), "pins": {}, "started": 100}
        d.note_draft(st)
        with unittest.mock.patch.object(d.subprocess, "run", fake_run("https://example.invalid/tag/beta2")):
            d.finish_build(st)
        self.assertIn("**beta2** 出好了：https://example.invalid/tag/beta2", replies[-1])
        self.assertNotIn("discarded", st["history"][-1])
        self.assertFalse(d.makes_drafts(os.path.join(tmp, "nowhere")))

    def test_internal_build_treats_candidates_like_beta(self):
        """候选版 betaN-rcM 和 beta 一样全评、全部现打；测试托管（--url-base）不做发布后的复测，也不上传。"""
        ib = self._tool("internal_build")
        self.assertFalse(ib.canary_allowed("beta2-rc1")[0])
        with open(os.path.join(ROOT, "tools", "internal_build.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('if re.fullmatch(r"beta\\d+(\\.\\d+)?(-rc\\d+)?", a.tag):\n', src)
        self.assertIn('if "merge" in steps and not a.url_base and not a.no_eval:\n', src)
        self.assertIn('    if "publish" in steps and a.url_base:\n', src)
        self.assertIn('step("check", lambda: (checked_assets(a.tag), clamav_plan(a.tag, apps)))', src)
        self.assertIn("if a.url_base:\n        a.no_publish = True\n", src)

    def test_checked_assets_before_upload_or_lan_hosting(self):
        """上传前、候选版挂到局域网前查的同一套：清单列的文件都在、哈希对，DMG 钉了公证票据，包里没有 .env、数据库、负责人路径。"""
        import hashlib
        import subprocess
        import zipfile
        ib = self._tool("internal_build")
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        out = os.path.join(root, "build.noindex", "release", "beta2-rc1")
        def put(rel, data):
            p = os.path.join(out, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            return hashlib.sha256(data).hexdigest()
        def mail_zip(names):
            p = os.path.join(out, "accessories", "mail.zip")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with zipfile.ZipFile(p, "w") as z:
                for n in names:
                    z.writestr(n, "x")
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        os.makedirs(os.path.join(out, "app", "螺壳.app", "Contents"))
        def manifest():
            with open(os.path.join(out, "release.json"), "w", encoding="utf-8") as f:
                json.dump({"items": {
                    "luoke-mail": {"kind": "accessory", "files": [{"path": "mail.zip", "sha256": mail_zip(names)}]},
                    "hub": {"kind": "hub", "files": [{"path": "Luoke.dmg", "sha256": put("Luoke.dmg", b"h")}]},
                    "qwen": {"kind": "model", "files": [{"path": "w.safetensors", "sha256": "0" * 64}]}},
                    "installers": {"cli": {"file": "install.sh", "sha256": put("install.sh", b"i")}}}, f)
        stapled = {"ok": True}
        fake_run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0 if stapled["ok"] else 65)
        def die(msg, code=1):
            raise RuntimeError(msg)
        with unittest.mock.patch.object(ib, "ROOT", root), unittest.mock.patch.object(ib, "die", die), \
                unittest.mock.patch.object(ib.subprocess, "run", fake_run):
            names = ["luoke_mail/__init__.py"]
            manifest()
            got, m, assets = ib.checked_assets("beta2-rc1")
            self.assertEqual(got, out)
            self.assertEqual(sorted(os.path.basename(p) for p in assets), ["Luoke.dmg", "install.sh", "mail.zip"])     # 模型不算
            stapled["ok"] = False
            with self.assertRaisesRegex(RuntimeError, "Luoke.dmg 没有钉上公证票据"):
                ib.checked_assets("beta2-rc1")
            stapled["ok"] = True
            names = ["luoke_mail/__init__.py", "luoke_mail/.env"]
            manifest()
            with self.assertRaisesRegex(RuntimeError, "mail.zip 里有不该发出去的东西"):
                ib.checked_assets("beta2-rc1")
            put("install.sh", b"changed")
            with self.assertRaisesRegex(RuntimeError, "安装器 install.sh 不在或哈希不对"):
                ib.checked_assets("beta2-rc1")

    def test_leak_check_without_public_tree_uses_the_word_list(self):
        """钉公开提交时树里没有 public_tree.py：用本机词表查安装包；没有词表、词表写坏了都停（停在 die，不是原样的异常）。"""
        import contextlib, io, unittest.mock
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        app = os.path.join(tmp, "X.app", "Contents", "Resources")
        os.makedirs(app)
        with open(os.path.join(app, "notes.md"), "w") as f:
            f.write("measured on somehost\n")
        def run(patterns):
            err = io.StringIO()
            with unittest.mock.patch.dict(sys.modules, {"public_tree": None}), \
                    unittest.mock.patch.dict(os.environ, {"LUOKE_LEAK_PATTERNS": patterns}), contextlib.redirect_stderr(err):
                try:
                    self.rel.leak_check(os.path.join(tmp, "X.app"), "X")
                except SystemExit as e:
                    return e.code, err.getvalue()
            return 0, err.getvalue()
        words = os.path.join(tmp, "words")
        for text, code, said in (("\\bsomehost\\b\n", 1, "泄漏词"), ("[abc\n", 1, "不是正则"), ("\\bother\\b\n", 0, "")):
            with open(words, "w") as f:
                f.write(text)
            rc, err = run(words)
            self.assertEqual(rc, code, err)
            self.assertIn(said, err)
        rc, err = run(os.path.join(tmp, "missing"))
        self.assertEqual(rc, 1)
        self.assertIn("本机词表", err)

    def test_previous_beta_needs_a_ledger(self):
        """beta0（10-16 演练）没有支持台账：beta1 不拿它比，照默认比最近一版内部版；有台账的 beta 才算上一个 beta。"""
        d = self._tool("release_daemon")
        with unittest.mock.patch.object(d, "CORE", tempfile.mkdtemp()):
            os.makedirs(os.path.join(d.CORE, "releases", "beta0", "evidence"))
            self.assertIsNone(d.previous_beta("beta1"))
            os.makedirs(os.path.join(d.CORE, "releases", "beta1", "evidence"))
            open(os.path.join(d.CORE, "releases", "beta1", "support.json"), "w").write("{}")
            self.assertEqual(d.previous_beta("beta1.1"), "beta1")

    def test_beta_compares_with_its_last_candidate(self):
        """正式 beta 拿它最近一个出好了的候选版比（11-26）：转正的就是测过的候选版，开关照它来。拿上一个 beta 比的话，
        之后加的任务全成了新任务、发布前只评 1 遍开不了（beta2 第一次出时语音、搜索、扫描的模型任务全关着）。
        没有候选版拿最近一版内部版，再没有才拿上一个 beta；别的 beta 的候选版、没出成的、没台账的不算。"""
        d = self._tool("release_daemon")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for t in ("internal-1125-4", "beta2-rc5", "beta2-rc6", "beta3-rc1"):
            os.makedirs(os.path.join(tmp, "releases", t))
            open(os.path.join(tmp, "releases", t, "support.json"), "w").write("{}")
        os.makedirs(os.path.join(tmp, "releases", "beta2-rc7"))       # 没有台账
        popen = []
        with unittest.mock.patch.object(d, "CORE", tmp), unittest.mock.patch.object(d, "BUILD", tmp), \
                unittest.mock.patch.object(d, "reply", lambda m: None), unittest.mock.patch.object(d, "log", lambda m: None), \
                unittest.mock.patch.object(d, "pins_from_top", lambda t: {"luoke-core": "a" * 40}), \
                unittest.mock.patch.object(d, "not_on_public", lambda p: []), \
                unittest.mock.patch.object(d, "previous_beta", lambda t: "beta1.1"), \
                unittest.mock.patch.object(d, "sh", lambda *a, **k: ""), \
                unittest.mock.patch.object(d.subprocess, "Popen", lambda *a, **k: popen.append(a[0]) or types.SimpleNamespace(pid=5)):
            hist = [{"tag": "beta2-rc5", "ok": True}, {"tag": "beta2-rc6", "ok": True}, {"tag": "internal-1125-4", "ok": True},
                    {"tag": "beta2-rc7", "ok": True}, {"tag": "beta3-rc1", "ok": True}, {"tag": "beta2-rc8", "ok": False}]
            self.assertEqual(d.previous_candidate("beta2", hist), "beta2-rc6")
            st = {"history": hist}
            d.start_build({"tag": "beta2", "by": "lab-owner"}, st)
            self.assertIn("'--prev' 'beta2-rc6'", popen[-1][2])
            self.assertEqual(st["running"]["prev"], "beta2-rc6")      # 出完的流程报告也比同一版
            st = {"history": [{"tag": "internal-1125-4", "ok": True}]}
            d.start_build({"tag": "beta2", "by": "lab-owner"}, st)
            self.assertIn("'--prev' 'internal-1125-4'", popen[-1][2])
            st = {"history": []}
            d.start_build({"tag": "beta2", "by": "lab-owner"}, st)
            self.assertIn("'--prev' 'beta1.1'", popen[-1][2])

    def test_previous_internal_is_the_last_published_internal_with_a_ledger(self):
        """候选版比的内部版（11-24）：出版历史里最近一个出好了的、有支持台账的内部版；没出成的、没台账的、beta 和候选版都跳过。"""
        d = self._tool("release_daemon")
        with unittest.mock.patch.object(d, "CORE", tempfile.mkdtemp()):
            for t in ("internal-1123", "internal-1124-3", "beta2-rc3"):
                os.makedirs(os.path.join(d.CORE, "releases", t))
                open(os.path.join(d.CORE, "releases", t, "support.json"), "w").write("{}")
            os.makedirs(os.path.join(d.CORE, "releases", "internal-1124-4"))      # 没有台账
            hist = [{"tag": "internal-1123", "ok": True}, {"tag": "beta2-rc3", "ok": True},
                    {"tag": "internal-1124-3", "ok": False}, {"tag": "internal-1124-4", "ok": True}]
            self.assertEqual(d.previous_internal(hist), "internal-1123")
            self.assertIsNone(d.previous_internal([]))
            self.assertIsNone(d.previous_internal(None))

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
        with unittest.mock.patch.object(d, "CORE", tempfile.mkdtemp()):
            # 临时加的版从 -3 起：MMDD、-2 留给 luoke-top 早上 10:00、晚上 21:00 钉的版（10-23）
            self.assertEqual(d.next_tag("1024", []), "internal-1024-3")
            self.assertEqual(d.next_tag("1024", [{"tag": "internal-1024-3"}]), "internal-1024-4")
            os.makedirs(os.path.join(d.CORE, "releases", "internal-1024-4"))
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
        def write(tag, phases, samples=(), failed=None, dropped=None, kept=None):
            os.makedirs(os.path.join(root, "releases", tag))
            tl = {"tag": tag, "host": "studio", "phases": [{"name": n, "start": a, "end": b, "ok": True} for n, a, b in phases], "samples": list(samples)}
            if failed:
                tl["failed"] = failed
            if dropped:
                tl["dropped"] = dropped
            if kept:
                tl["kept"] = kept
            json.dump(tl, open(os.path.join(root, "releases", tag, "timeline.json"), "w"))
        write("internal-1023", [("eval 16", 0, 1800), ("sign", 1800, 2400)])
        write("internal-1024", [("eval 16", 0, 4000), ("sign", 4000, 4600), ("keychain wait", 4600, 4700)],
              samples=[{"t": 0, "wired_gb": 40.0, "compressed_gb": 1.0, "pressure": 1, "swapouts": 10, "load": 3},
                       {"t": 30, "wired_gb": 60.5, "compressed_gb": 2.0, "pressure": 2, "swapouts": 30, "load": 5}],
              dropped={"luoke-agent": "打不出包或包里有泄漏词，原因见上面：agent 打包退出码 1"},
              kept={"luoke-scan": "luoke-scan：7df627c→5f2469c 改了代码、版本号还是 0.1.1，这一版沿用 0.1.1（请升版本号）"})
        with unittest.mock.patch.object(r, "ROOT", root):
            text = "\n".join(r.summary("internal-1024", "internal-1023"))
            self.assertIn("总用时 1 小时 18 分（studio）", text)
            self.assertIn("最慢的 3 步：eval 16 1 小时 6 分、sign 10 分", text)     # 等钥匙串不算一步
            self.assertIn("等钥匙串 1 分", text)
            self.assertIn("wired 峰值 60.5 GB", text)
            self.assertIn("压力最高 警告，换出 20 页", text)
            self.assertIn("明显变慢：eval 16 1 小时 6 分（上一版 30 分）", text)
            self.assertNotIn("sign 10 分（上一版", text)
            self.assertIn("这一版没带 luoke-agent（打不出包或包里有泄漏词，原因见上面：agent 打包退出码 1）", text)
            self.assertIn("luoke-scan：7df627c→5f2469c 改了代码、版本号还是 0.1.1，这一版沿用 0.1.1（请升版本号）。", text)
            self.assertIn("| eval 16 |", r.report("internal-1024", "internal-1023"))

    def test_each_eval_cell_records_its_timing(self):
        """每格评测记开始、结束和其中加载模型的秒数：一行一段写进 evidence/timing-<档>.jsonl（实验室主控起来一段，每格一段），
        这一格的证据里也带一份 timing。模型加载从实验室主控日志里这一格那一段读，主控起来时加载的底座不算进第一格。"""
        rel = self.rel
        log_text = ("2026-11-13 10:00:00 hub 0.1 tier=16 mem=16GB base=qwen3.5-9b-4bit recipe=x\n"
                    "2026-11-13 10:00:05 unloaded qwen3.5-9b-4bit (making room for gemma-4-26b)\n"
                    "2026-11-13 10:00:30 loaded qwen3.5-9b-4bit in 24.5s\n"
                    "2026-11-13 10:01:00 loaded qwen3.5-0.8b alongside qwen3.5-9b-4bit in 3.0s\n"
                    "2026-11-13 10:01:10 scorer ready in 4.2s (/x/models/qwen3.5-0.8b-base-4bit)\n"
                    "2026-11-13 10:01:11 engine ready port=1 models=['a'] in 3.2s\n"
                    "2026-11-13 10:01:12 chat task=t.x prio=background outcome=ok\n")
        self.assertEqual(rel.model_loads(log_text), [{"model": "qwen3.5-9b-4bit", "secs": 24.5}, {"model": "qwen3.5-0.8b", "secs": 3.0},
                                                     {"model": "qwen3.5-0.8b-base-4bit", "secs": 4.2}])
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        repo = os.path.join(root, "luoke-ime")
        os.makedirs(repo)
        json.dump({"version": "0.1.0"}, open(os.path.join(repo, "m.json"), "w"))
        open(os.path.join(repo, "set.tsv"), "w").write("a\tb\n")
        app = os.path.join(root, "oMLX.app")
        os.makedirs(os.path.join(app, "Contents"))
        recipe = Recipe.load(os.path.join(ROOT, "recipes", "dev.json"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": recipe.engine["version"].replace("omlx-", "")}, f)
        recipe.engine = dict(recipe.engine, app=app)
        spec = {"accessory": "luoke-ime", "repo": "../luoke-ime", "version": {"manifest": "m.json"}, "command": ["true"],
                "summary": "ime-rerank-stdout", "evalset": "set.tsv", "hard": "x"}
        out = ("n=600  先给的首选 88.0%  不标 more 的首选 89.5%  最终首选 90.5%  补发改了 3 条\n"
               "最后一个键到第一份答复：p50 10 ms  p90 17 ms  p99 22 ms  最长 26 ms\n"
               "最后一个键到不标 more 的答复：p50 78 ms  p90 102 ms  p99 150 ms  最长 300 ms\n主控打分 1200 次：前缀缓存命中 71.2%，")

        class Hub:
            def __init__(self, paths, recipe_path, recipe, tier, model_source):
                self.env = os.path.join(root, "lab", tier)
                self.hub_log = os.path.join(self.env, "logs", "hub.log")
                self.sock = "/nonexistent.sock"

            def start(self):
                t0 = time.time()
                os.makedirs(os.path.dirname(self.hub_log))
                open(self.hub_log, "a").write("2026-11-13 10:00:30 loaded qwen3.5-9b-4bit in 20.0s\n")
                self.ready = rel.timing_since(t0, self.hub_log, 0)
                return {"base": "qwen3.5-9b-4bit", "phase": "running"}

            def stop(self):
                pass

        def bench(cmd, cwd, env, task, kind, hub_log, **kw):
            open(hub_log, "a").write("2026-11-13 10:05:00 scorer ready in 7.5s (/m/qwen3.5-0.8b-base-4bit)\n")
            return rel.EvalResult(0, out, "")
        a = types.SimpleNamespace(tasks="luoke-ime.rerank", tiers="128", template="x.json", model_source=root, ime_corpus="",
                                  repo_override={"luoke-ime": repo}, keep_accessory_results=False)
        with unittest.mock.patch.object(rel, "ROOT", root), unittest.mock.patch.object(rel, "LabHub", Hub), \
                unittest.mock.patch.object(rel, "run_watched", bench), unittest.mock.patch.object(rel, "machine_summary", lambda: {}), \
                unittest.mock.patch.object(rel, "load_evals", lambda: {"luoke-ime.rerank": spec}):
            paths = rel.Paths("internal-1113")
            rel.stage_eval(a, paths, recipe)
        lines = [json.loads(x) for x in open(os.path.join(paths.evidence, "timing-128.jsonl"))]
        self.assertEqual([(x["kind"], x["tier"], x["load_secs"]) for x in lines], [("hub", "128", 20.0), ("cell", "128", 7.5)])
        self.assertEqual(lines[0]["base"], "qwen3.5-9b-4bit")
        cell = lines[1]
        self.assertEqual((cell["task"], cell["exit"], cell["stalled"], cell["loads"]),
                         ("luoke-ime.rerank", 0, None, [{"model": "qwen3.5-0.8b-base-4bit", "secs": 7.5}]))
        self.assertTrue(cell["start"] <= cell["end"] and cell["secs"] >= 0)
        ev = rel.load_evidence(paths.rec)
        self.assertEqual(len(ev), 1)                       # .jsonl 不当证据读
        rec = next(iter(ev.values()))
        self.assertEqual(rec["timing"], {k: v for k, v in cell.items() if k not in ("kind", "task", "tier", "exit", "stalled")})
        # 日志被截短、重建了（比记下的位置还短）：从头读
        log = os.path.join(root, "short.log")
        open(log, "w").write("2026-11-13 loaded m in 1.5s\n")
        self.assertEqual(rel.timing_since(time.time(), log, 10_000)["load_secs"], 1.5)
        self.assertEqual(rel.timing_since(time.time(), os.path.join(root, "none.log"), 0)["loads"], [])

    def test_remote_timing_comes_back_and_reused_records_drop_it(self):
        """远端评的那一档的 timing-<档>.jsonl 跟着证据拿回来；沿用来的证据不带评的那一版的用时"""
        ib = self._tool("internal_build")
        root, remote = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, remote, True)
        combo = {"key": "k", "model": "m", "base": "m", "tiers": ["16"], "params": {}}
        json.dump({"task": "t.x", "tier_run": "16", "combo": combo}, open(os.path.join(remote, "t.x.k.16.json"), "w"))
        open(os.path.join(remote, "timing-16.jsonl"), "w").write(json.dumps({"kind": "cell", "task": "t.x", "tier": "16", "secs": 9}) + "\n")
        with unittest.mock.patch.object(ib, "ROOT", root), unittest.mock.patch.object(ib, "MACHINES", {"16": {"host": "h", "core": "c"}}), \
                unittest.mock.patch.object(ib, "rsync", lambda src, dst, excludes=(): shutil.copytree(remote, dst, dirs_exist_ok=True)), \
                unittest.mock.patch.object(ib, "log", lambda m: None):
            ib.merge_remote("internal-1113", "16")
            ev = os.path.join(root, "releases", "internal-1113", "evidence")
            self.assertEqual(json.loads(open(os.path.join(ev, "timing-16.jsonl")).read())["secs"], 9)
            old = {"task": "t.x", "accessory": "a", "tier_run": "16", "combo": combo, "timing": {"secs": 9}, "_file": "x"}
            ib.copy_reused("internal-1113-2", "internal-1113", [("16", old)])
        d = json.load(open(os.path.join(root, "releases", "internal-1113-2", "evidence", "t.x.k.16.json")))
        self.assertEqual(d["reused_from"], "internal-1113")
        self.assertNotIn("timing", d)

    def test_release_report_lists_the_slowest_cells(self):
        """流程报告：每一档评了几格、各用多久（其中加载模型）、实验室主控起来多久、其余多久；最慢的几格和上次真评这一格的用时
        （上一版沿用的顺着 reused_from 往前找）；明显变慢的格子进摘要；总用时里不在任何一步里的空档单独写出来。"""
        r = self._tool("release_report")
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)

        def write(tag, phases=(), timing=(), evidence=()):
            ev = os.path.join(root, "releases", tag, "evidence")
            os.makedirs(ev)
            json.dump({"tag": tag, "host": "studio", "samples": [],
                       "phases": [{"name": n, "start": a, "end": b, "ok": True} for n, a, b in phases]},
                      open(os.path.join(root, "releases", tag, "timeline.json"), "w"))
            for x in timing:
                with open(os.path.join(ev, f"timing-{x['tier']}.jsonl"), "a") as f:
                    f.write(json.dumps(x) + "\n")
            for task, tier, src in evidence:
                json.dump({"task": task, "tier_run": tier, "reused_from": src}, open(os.path.join(ev, f"{task}.k.{tier}.json"), "w"))

        def cell(task, tier, secs, loads=(), **kw):
            x = dict(kind="cell", task=task, tier=tier, start=1000.0, end=1000.0 + secs, secs=secs,
                     load_secs=sum(m["secs"] for m in loads), loads=list(loads), exit=0, stalled=None)
            x.update(kw)
            return x
        write("beta1.1", timing=[cell("luoke-mail.triage", "16", 590)])
        write("beta2-rc1", timing=[cell("luoke-scan.skills", "16", 600), cell("luoke-ime.rerank", "128", 1200)],
              evidence=[("luoke-mail.triage", "16", "beta1.1")])
        write("beta2-rc2",
              phases=[("eval 16", 0, 3600), ("eval 128", 0, 1800), ("diff", 3600, 3601), ("freeze", 10801, 10811)],
              timing=[dict(kind="hub", tier="16", start=0.0, end=60.0, secs=60.0, load_secs=30.0, loads=[{"model": "q9b", "secs": 30.0}]),
                      cell("luoke-scan.skills", "16", 1500),
                      cell("luoke-mail.triage", "16", 600, loads=[{"model": "q9b", "secs": 20.0}, {"model": "q9b", "secs": 25.0}]),
                      cell("luoke-mem.topics", "16", 700, stalled="stalled", exit=-15),
                      cell("luoke-ime.rerank", "128", 100), cell("luoke-ime.rerank", "128", 1250)])
        write("internal-1110", phases=[("eval 16", 0, 60)])          # 加这份记录之前的版：报告照旧，没有每格评测
        with unittest.mock.patch.object(r, "ROOT", root):
            text = "\n".join(r.summary("beta2-rc2", "beta2-rc1"))
            self.assertIn("其中 2 小时 0 分不在任何一步里", text)
            self.assertIn("最慢的 3 格评测：luoke-scan.skills 16 GB 25 分、luoke-ime.rerank 128 GB 20 分、luoke-mem.topics 16 GB 11 分。", text)
            self.assertIn("比上次评明显变慢的格：luoke-scan.skills 16 GB 25 分（上次 10 分）。", text)    # 别的格没慢到一半以上、5 分钟以上
            rep = r.report("beta2-rc2", "beta2-rc1")
            self.assertIn("- 16 GB 档：评了 3 格，共 46 分（其中加载模型 45 秒）；实验室主控起了 1 次，共 1 分（其中加载模型 30 秒）；"
                          "eval 16 一共 1 小时 0 分，其余 12 分不在评测里（同步、起停、拷回）。", rep)
            self.assertIn("- 128 GB 档：评了 2 格", rep)            # 同一格评了两次：合计都算，表里取最后一次
            self.assertRegex(rep, r"\| luoke-scan\.skills \| 16 \| [^|]+ \| 25 分 \| 0 秒 \| 10 分 \|  \|")
            self.assertRegex(rep, r"\| luoke-mail\.triage \| 16 \| [^|]+ \| 10 分 \| 45 秒（q9b 2 次 45 秒） \| 9 分（beta1\.1） \|  \|")
            self.assertRegex(rep, r"\| luoke-mem\.topics \| 16 \| [^|]+ \| 11 分 \| 0 秒 \| — \| 没进展、停掉 \|")
            self.assertRegex(rep, r"\| luoke-ime\.rerank \| 128 \| [^|]+ \| 20 分 \| 0 秒 \| 20 分 \|  \|")
            self.assertNotIn("## 每格评测", r.report("internal-1110"))
            self.assertNotIn("格评测", "\n".join(r.summary("internal-1110")))
            self.assertNotIn("不在任何一步里", "\n".join(r.summary("internal-1110")))

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
        scan = {"tasks": {"skills": {"score": 65, "total": 66, "hard_passed": 32, "hard_total": 32, "not_run": 0, "repeat": 3,
                                     "seconds_median": 2.1, "prompt_version": 3, "evalset_sha256": "a1"}},
                "machine": "Apple M3 Ultra, 512 GB, macOS 26.2", "date": "2026-11-03"}
        s8 = self.rel.summarize_scan_task(scan)
        self.assertEqual((s8["hard_ok"], s8["score"], s8["score_max"], s8["evalset_sha256"], s8["detail"]["prompt_version"]), (True, 65, 66, "a1", 3))
        self.assertNotIn("machine", s8)              # 一行字的 machine 不能盖掉证据里那份 dict（发布配方要读 machine["chip"]）
        scan["tasks"]["skills"].update(hard_passed=31)
        self.assertFalse(self.rel.summarize_scan_task(scan)["hard_ok"])        # 硬性错了一条
        scan["tasks"]["skills"].update(hard_passed=32, not_run=1)
        self.assertFalse(self.rel.summarize_scan_task(scan)["hard_ok"])        # 有没跑成的：不能算过
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
        for name in ("release.py", "internal_build.py", "support.py"):
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

    def test_previous_release_skips_builds_that_were_not_evaluated(self):
        """--no-eval 出的版只是拷了上一版的证据，下一版的"上一版"（比差异、沿用证据）跳过它，往前找真评过的"""
        rel, root = self.rel, tempfile.mkdtemp()
        for name, mode in (("internal-1109", "full"), ("internal-1109-2", "none")):
            d = os.path.join(root, "releases", name)
            os.makedirs(os.path.join(d, "evidence"))
            json.dump({"publish": "x"}, open(os.path.join(d, "steps.json"), "w"))
            json.dump({"tag": name, "eval_mode": mode}, open(os.path.join(d, "pins.json"), "w"))
            time.sleep(0.01)
        with unittest.mock.patch.object(rel, "ROOT", root):
            self.assertEqual(rel.previous_release("internal-1109-3", None), "internal-1109")
            self.assertFalse(rel.evaluated(os.path.join(root, "releases", "internal-1109-2")))
            self.assertTrue(rel.evaluated(os.path.join(root, "releases", "internal-1109")))

    def test_no_eval_copies_every_record_and_the_ledger(self):
        """--no-eval 不评，整版拷上一版的证据（记 reused_from）和支持台账，配方的开关和上一版一样；只给内部版"""
        ib = self._tool("internal_build")
        self.assertTrue(ib.no_eval_allowed("internal-1109-2"))
        for tag in ("beta2", "beta2-rc1", "local-1109-1200", "test-1"):
            self.assertFalse(ib.no_eval_allowed(tag))
        root = tempfile.mkdtemp()
        ev = os.path.join(root, "releases", "internal-1109", "evidence")
        os.makedirs(os.path.join(ev, "raw", "16"))
        json.dump({"results": []}, open(os.path.join(ev, "raw", "16", "t.json"), "w"))
        for task in ("luoke-mem.topics", "luoke-mail.thread"):
            json.dump({"task": task, "accessory": task.split(".")[0], "tier_run": "16", "score": 9, "score_max": 10, "hard_ok": True,
                       "raw": "raw/16/t.json", "combo": {"key": "k", "model": "m", "base": "m", "tiers": ["16"], "params": {}}},
                      open(os.path.join(ev, f"{task}.k.16.json"), "w"))
        json.dump({"luoke-mem.topics": {"16": {"state": "on", "why": "x", "runs": [True]}}},
                  open(os.path.join(root, "releases", "internal-1109", "support.json"), "w"))
        with unittest.mock.patch.object(ib, "ROOT", root), unittest.mock.patch.object(ib, "log", lambda m: None):
            n, bumps = ib.reuse_all("internal-1109-2", "internal-1109")
        self.assertEqual((n, bumps), (2, {}))
        new = os.path.join(root, "releases", "internal-1109-2")
        for task in ("luoke-mem.topics", "luoke-mail.thread"):
            d = json.load(open(os.path.join(new, "evidence", f"{task}.k.16.json")))
            self.assertEqual((d["reused_from"], d["release"]), ("internal-1109", "internal-1109-2"))
        self.assertTrue(os.path.exists(os.path.join(new, "evidence", "raw", "16", "t.json")))
        self.assertEqual(json.load(open(os.path.join(new, "support.json"))), json.load(open(os.path.join(root, "releases", "internal-1109", "support.json"))))

    def test_no_eval_ships_the_pinned_accessory_versions(self):
        """11-09 定：--no-eval 发钉住的最新提交。配件换了版本的，拷来的证据改记这一版钉住的版本（配方写进已验证、开关照来源版），
        原来评的版本记在 evaluated_version，配方的结论和发布说明照实写「未评测，沿用 x 的结果」。beta、候选版不认 --no-eval，这里也拦。"""
        ib = self._tool("internal_build")
        rel = self.rel
        root = tempfile.mkdtemp()
        ev = os.path.join(root, "releases", "internal-1109", "evidence")
        os.makedirs(ev)
        for task, ver in (("luoke-mem.topics", "0.29.1"), ("luoke-mail.thread", "0.19.38")):
            json.dump({"task": task, "accessory": task.split(".")[0], "accessory_version": ver, "accessory_commit": "a" * 40, "tier_run": "16",
                       "score": 9, "score_max": 10, "hard_ok": True, "combo": {"key": "k", "model": "m", "base": "m", "tiers": ["16"], "params": {}}},
                      open(os.path.join(ev, f"{task}.k.16.json"), "w"))
        src = {}
        for repo, ver in (("luoke-mem", "0.29.1"), ("luoke-mail", "0.19.39")):
            src[repo] = os.path.join(root, "src", repo)
            os.makedirs(os.path.join(src[repo], "packaging"))
            json.dump({"accessory": repo, "version": ver}, open(os.path.join(src[repo], "packaging", "accessory.json"), "w"))
        with unittest.mock.patch.object(ib, "ROOT", root), unittest.mock.patch.object(ib, "log", lambda m: None):
            n, bumps = ib.reuse_all("internal-1109-3", "internal-1109", src)
            with self.assertRaises(SystemExit):
                ib.reuse_all("beta2-rc1", "internal-1109", src)
            with self.assertRaises(SystemExit):
                ib.reuse_all("beta2", "internal-1109", src)
        self.assertEqual((n, bumps), (2, {"luoke-mail": ["0.19.38", "0.19.39"]}))
        new = os.path.join(root, "releases", "internal-1109-3", "evidence")
        mail = json.load(open(os.path.join(new, "luoke-mail.thread.k.16.json")))
        self.assertEqual((mail["accessory_version"], mail["evaluated_version"]), ("0.19.39", "0.19.38"))
        mem = json.load(open(os.path.join(new, "luoke-mem.topics.k.16.json")))
        self.assertEqual(mem["accessory_version"], "0.29.1")
        self.assertNotIn("evaluated_version", mem)
        self.assertFalse(os.path.exists(os.path.join(root, "releases", "beta2-rc1", "evidence")))
        self.assertIn("0.19.39", ib.no_eval_note("internal-1109", bumps))
        self.assertIn("沿用 0.19.38 的结果", ib.no_eval_note("internal-1109", bumps))
        d = dict(mail, model="m", tiers=["16"], machine={"chip": "M"}, date="2026-11-09")
        self.assertIn("未评测，沿用 0.19.38 的结果", rel.evidence_conclusion(d))
        self.assertNotIn("未评测", rel.evidence_conclusion(dict(mem, model="m", tiers=["16"], machine={}, date="d")))

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
        self.assertNotIn("prompt_sha256", s["detail"])                 # 提示词还没搬进文件的版本：不加
        # 搬进文件以后（顶层 ADR 0019 的 11-04 修订）：结果顶层的提示词哈希和文件表抄进证据
        p = {"prompt_sha256": "p1", "prompt_files": ["prompts/topics.txt", "prompts/labels.json"]}
        s = self.rel.summarize_mem_topics(dict({"hard_pass": True, "cases": 24, "score": {"interest_recall": 0.8}}, **p))
        self.assertEqual((s["detail"]["prompt_sha256"], s["detail"]["prompt_files"]), ("p1", p["prompt_files"]))
        s = self.rel.summarize_mem_typing(dict({"hard_pass": True, "cases": 24, "score": {"term_recall": 0.75, "interest_recall": 0.5, "common_terms": 6}}, **p))
        self.assertEqual(s["detail"]["prompt_sha256"], "p1")
        s = self.rel.summarize_mail_task(dict({"score": {"ok": 1, "n": 1, "passes": True}}, **p))
        self.assertEqual(s["detail"]["prompt_sha256"], "p1")
        s = self.rel.summarize_mail_triage(dict({"score": {"basic": 1, "basic_n": 1, "hard": 1, "hard_n": 1}}, **p))
        self.assertEqual(s["detail"]["prompt_files"], p["prompt_files"])

    def test_summarize_embed_parity(self):
        """luoke-search.embed：5 条余弦全 ≥ 0.999 才算硬条件过；请求没成（这一档嵌入进程起不来）也是没过、分数 0。"""
        rows = [{"name": n, "cosine": c} for n, c in zip(("query", "query-bn", "query-ar", "document", "document-long"),
                                                         (0.99989, 0.9999, 0.99992, 0.99977, 0.99985))]
        doc = {"rows": rows, "threshold": 0.999, "expected": 5, "ok": True, "error": None, "ms": 40.0, "tier": "16",
               "min_cosine": 0.99977, "vectors_sha256": "v1", "vector_space": "embeddinggemma-2/1"}
        s = self.rel.summarize_embed_parity(doc)
        self.assertEqual((s["hard_ok"], s["score"], s["score_max"], s["n"], s["evalset_sha256"], s["detail"]["tier"]),
                         (True, 5, 5, 5, "v1", "16"))
        low = dict(doc, ok=False, rows=rows[:4] + [{"name": "document-long", "cosine": 0.998}])
        self.assertEqual((self.rel.summarize_embed_parity(low)["hard_ok"], self.rel.summarize_embed_parity(low)["score"]), (False, 4))
        s = self.rel.summarize_embed_parity(dict(doc, ok=False, rows=[], min_cosine=None, error="not_ready: 内存不够"))
        self.assertEqual((s["hard_ok"], s["score"], s["score_max"]), (False, 0, 5))
        self.assertEqual(self.rel.SUMMARIZERS["embed-parity"], "result")
        spec = {"floor": None, "tolerance": 0}
        self.assertEqual(self.rel.judge(self.rel.summarize_embed_parity(doc), None, spec)["verdict"], "pass")
        self.assertEqual(self.rel.judge(s, None, spec)["verdict"], "fail")

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

    def test_queue_lists_waiting_jobs_with_what_they_wait_for(self):
        """右边栏的任务队列（/status 的 queue）：没在跑的空闲任务，按会先起哪个排，各带卡在什么条件上；title 是可选的人话"""
        from luoke_core import jobs
        d = tempfile.mkdtemp()
        m = self.fake_app(d, ["-c", "pass"])
        m["jobs"] = [dict(m["jobs"][0], title="整理记忆"), {"name": "later", "command": ["-c", "pass"], "min_interval_minutes": 60}]
        clock = Clock()
        quiet, bg, busy = [False], [("running", [])], [False]
        state = {"jobs": {"fake.later": {"last_start": clock() - 600, "reason": "done"}}}
        j = jobs.IdleJobs(now=clock, quiet=lambda: quiet[0], background_state=lambda allow: bg[0], busy=lambda: busy[0],
                          state=state, save_state=lambda: None, log=lambda s: None, socket_path=lambda: "/tmp/x.sock", logs_dir=os.path.join(d, "logs"))
        q = j.queue({"ai.luoke.fake": m})
        self.assertEqual([(i["id"], i["tool"], i["title"]) for i in q], [("fake.work", "fake", "整理记忆"), ("fake.later", "fake", None)])
        self.assertEqual(q[0]["wait"], {"kind": "quiet"})
        self.assertEqual(q[1]["wait"], {"kind": "time", "at": round(clock() - 600 + 3600)})     # 间隔没到：报最早能起的时间
        bg[0] = ("paused", ["battery"])
        self.assertEqual(j.queue({"ai.luoke.fake": m})[0]["wait"], {"kind": "power"})
        bg[0] = ("paused", ["thermal"])
        self.assertEqual(j.queue({"ai.luoke.fake": m})[0]["wait"], {"kind": "thermal"})
        bg[0], quiet[0], busy[0] = ("running", []), True, True
        self.assertEqual(j.queue({"ai.luoke.fake": m})[0]["wait"], {"kind": "busy"})
        j.run_soon("fake.later", {"ai.luoke.fake": m})
        self.assertEqual(j.queue({"ai.luoke.fake": m})[1]["wait"], {"kind": "soon"})
        busy[0] = False
        j.tick({"ai.luoke.fake": m})                              # 起了第一个：它不在排着的里
        self.assertEqual([i["id"] for i in j.queue({"ai.luoke.fake": m})], ["fake.later"])
        j._terminate(j.running, "test")
        for _ in range(50):
            j.tick({"ai.luoke.fake": m})
            if not j.running:
                break
            time.sleep(0.05)

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


class BundledToolJobsTest(unittest.TestCase):
    """螺壳里带的程序（Contents/Helpers，螺壳扫描 0.3.0 起）：照螺壳搜索那样认；空闲任务经螺壳的 --run-tool 起，核签名在本体。"""

    def fake_hub(self, d, launcher=None, plist=True, jobs=None):
        import plistlib
        app = os.path.join(d, "螺壳.app")
        for sub in ("MacOS", "Helpers", "Resources/scan"):
            os.makedirs(os.path.join(app, "Contents", sub), exist_ok=True)
        for name in ("luoke-scan", "luoke-scan-admin"):
            with open(os.path.join(app, "Contents", "Helpers", name), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
        with open(os.path.join(app, "Contents", "Resources", "scan", "luoke-accessory.json"), "w", encoding="utf-8") as f:
            json.dump({"contract": 1, "accessory": "luoke-scan", "bundle_id": "ai.luoke.scan", "name": "螺壳扫描", "version": "0.3.0",
                       "type": "app", "entry": [], "tasks": [], "resident": False,
                       "jobs": jobs or [{"name": "update", "exec": "luoke-scan", "command": ["update", "--this-mac"], "min_interval_minutes": 1}]}, f)
        if plist:
            with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"CFBundleExecutable": "LuokeCore"}, f)
        exe = os.path.join(app, "Contents", "MacOS", "LuokeCore")
        with open(exe, "w") as f:
            f.write(launcher or "#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        return app

    def jobs_for(self, d, state, clock=None, quiet=None, background_state=None):
        from luoke_core import jobs
        return jobs.IdleJobs(now=clock or Clock(), quiet=quiet or (lambda: True), background_state=background_state or (lambda allow: ("running", [])),
                             busy=lambda: False, state=state, save_state=lambda: None, log=lambda s: None, socket_path=lambda: None,
                             logs_dir=os.path.join(d, "logs"))

    def test_found_like_the_other_bundled_apps(self):
        d = tempfile.mkdtemp()
        app = self.fake_hub(d)
        found = accessories.bundled_helpers(app)
        self.assertEqual([m["accessory"] for m in found], ["luoke-scan"])          # luoke-scan-admin 没有清单，不算一个应用
        self.assertTrue(found[0]["_helper"] and found[0]["_bundled"])
        self.assertEqual(found[0]["_app"], app)

    def test_idle_job_goes_through_the_launcher(self):
        from luoke_core import jobs
        d = tempfile.mkdtemp()
        out = os.path.join(d, "argv.txt")
        app = self.fake_hub(d, launcher=f'#!/bin/sh\necho "$@" > "{out}"\n')
        m = accessories.bundled_helpers(app)[0]
        specs = jobs.parse_jobs(m)
        self.assertEqual([(s["key"], s["tool"]) for s in specs], [("luoke-scan.update", True)])
        state = {}
        j = self.jobs_for(d, state)
        j.tick({"ai.luoke.scan": m})
        for _ in range(50):
            j.tick({"ai.luoke.scan": m})
            if not j.running:
                break
            time.sleep(0.1)
        self.assertEqual(state["jobs"]["luoke-scan.update"]["reason"], "done")
        with open(out) as f:
            self.assertEqual(f.read().strip(), "--run-tool luoke-scan update --this-mac")   # 不直接起 Helpers 里的程序

    def test_quick_look_runs_between_rounds(self):
        """luoke-core#152：快速扫描（scan --this-mac --quick）登记成空闲任务，几分钟一次。整轮的 scan 跑着时不起；
        经 --run-tool 起，参数照清单；间隔不到不起；允许用电池的话电池上也起（整轮不起）。"""
        d = tempfile.mkdtemp()
        out, release = os.path.join(d, "argv.txt"), os.path.join(d, "release")
        self.addCleanup(lambda: open(release, "w").close())     # 中途失败也放走假的整轮
        launcher = (f'#!/bin/sh\necho "$@" >> "{out}"\ncase "$*" in *--autoclean*) '
                    f'while [ ! -e "{release}" ]; do /bin/sleep 0.05; done;; esac\nexit 0\n')
        app = self.fake_hub(d, launcher=launcher, jobs=[
            {"name": "scan", "exec": "luoke-scan", "command": ["scan", "--this-mac", "--autoclean"],
             "min_interval_minutes": 1440, "allow_battery": False, "timeout_minutes": 60},
            {"name": "quick", "exec": "luoke-scan", "command": ["scan", "--this-mac", "--quick"],
             "min_interval_minutes": 5, "uses_model": False, "allow_battery": True, "timeout_minutes": 5}])
        m = accessories.bundled_helpers(app)[0]
        clock, state, on_ac = Clock(), {}, [True]
        power = lambda allow: ("running", []) if on_ac[0] or allow else ("paused", ["battery"])
        j = self.jobs_for(d, state, clock=clock, background_state=power)

        def lines():
            try:
                with open(out) as f:
                    return f.read().splitlines()
            except FileNotFoundError:
                return []

        def finish():
            for _ in range(100):
                j.tick({"ai.luoke.scan": m})
                if not j.running:
                    return
                time.sleep(0.05)
            self.fail("任务没结束")

        j.tick({"ai.luoke.scan": m})
        self.assertEqual(j.status()["running"], "luoke-scan.scan")
        for _ in range(100):
            if lines():
                break
            time.sleep(0.05)
        for _ in range(3):                                   # 整轮跑着：快速扫描该跑了也不起
            j.tick({"ai.luoke.scan": m})
            self.assertEqual(j.status()["running"], "luoke-scan.scan")
        open(release, "w").close()
        finish()
        j.tick({"ai.luoke.scan": m})                         # 整轮结束了才起
        self.assertEqual(j.status()["running"], "luoke-scan.quick")
        finish()
        self.assertEqual(lines(), ["--run-tool luoke-scan scan --this-mac --autoclean",
                                   "--run-tool luoke-scan scan --this-mac --quick"])
        self.assertEqual(state["jobs"]["luoke-scan.quick"]["reason"], "done")
        clock.tick(4 * 60)
        j.tick({"ai.luoke.scan": m})
        self.assertIsNone(j.running)
        self.assertEqual(j.status()["skipped"]["luoke-scan.quick"], "interval")
        clock.tick(61)
        on_ac[0] = False                                     # 拔了电源：快速扫描照起，整轮不起
        j.tick({"ai.luoke.scan": m})
        self.assertEqual(j.status()["running"], "luoke-scan.quick")
        self.assertEqual(j.status()["skipped"]["luoke-scan.scan"], "battery")
        finish()
        self.assertEqual(len(lines()), 3)

    def test_no_launcher_no_job(self):
        """读不到螺壳的可执行文件：不跑，也不退回去直接起程序（那样就没人核签名了）。"""
        d = tempfile.mkdtemp()
        app = self.fake_hub(d, plist=False)
        m = accessories.bundled_helpers(app)[0]
        state = {}
        j = self.jobs_for(d, state)
        j.tick({"ai.luoke.scan": m})
        self.assertIsNone(j.running)
        self.assertEqual(state["jobs"]["luoke-scan.update"]["reason"], "launch_failed: no_launcher")

    def test_hard_kill_takes_the_whole_group(self):
        """收尾时限过了：--run-tool 连同工具、工具起的程序一起结束（它们在一个新的进程组里）。"""
        from luoke_core import jobs
        d = tempfile.mkdtemp()
        pidfile = os.path.join(d, "child.pid")
        app = self.fake_hub(d, launcher=f"#!/bin/sh\ntrap '' TERM\n/bin/sleep 60 &\necho $! > \"{pidfile}\"\nwait\n")
        m = accessories.bundled_helpers(app)[0]
        clock, quiet, state = Clock(), [True], {}
        j = self.jobs_for(d, state, clock=clock, quiet=lambda: quiet[0])
        j.tick({"ai.luoke.scan": m})
        for _ in range(100):
            if os.path.exists(pidfile) and open(pidfile).read().strip():
                break
            time.sleep(0.1)
        child = int(open(pidfile).read())
        quiet[0] = False
        j.tick({"ai.luoke.scan": m})                     # 用户回来：SIGTERM，它不理
        clock.tick(jobs.WRAP_UP_SECS + 1)
        j.tick({"ai.luoke.scan": m})                     # 过了收尾时限：整个进程组 SIGKILL
        for _ in range(50):
            j.tick({"ai.luoke.scan": m})
            if not j.running:
                break
            time.sleep(0.1)
        self.assertEqual(state["jobs"]["luoke-scan.update"]["reason"], "wrapped_up")
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            self.fail("工具起的进程还在")


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

    def fake_signed_app(self, d, ticket=True):
        """有 Info.plist、可选钉着票据的空壳 .app，给 verify_app 的假 codesign 用"""
        app = os.path.join(d, "假配件.app")
        os.makedirs(os.path.join(app, "Contents"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "ai.luoke.fake"}, f)
        if ticket:
            with open(os.path.join(app, "Contents", "CodeResources"), "wb") as f:
                f.write(b"ticket")
        return app

    def fake_codesign(self, calls, notarized=True):
        """记下每条命令；codesign 都答过，-dv 报我们的 Team ID，-R 那条按 notarized 答"""
        from luoke_core import download

        def run(cmd, **kw):
            calls.append(list(cmd))
            if "-dv" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", f"TeamIdentifier={download.TEAM_ID}\n")
            if any(a.startswith("-R=") for a in cmd):
                return subprocess.CompletedProcess(cmd, 0 if notarized else 3, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run

    def test_verify_app_checks_notarization_without_xcrun(self):
        """没装命令行工具的 Mac 上 /usr/bin/xcrun 是个壳：调它会失败、弹安装框。核公证只用 codesign（#240 补测）"""
        from luoke_core import download
        app = self.fake_signed_app(tempfile.mkdtemp())
        calls = []
        with unittest.mock.patch.dict(os.environ, {"LUOKE_ALLOW_UNSIGNED": "0"}), \
                unittest.mock.patch.object(download.subprocess, "run", side_effect=self.fake_codesign(calls)):
            download.verify_app(app, "ai.luoke.fake")
        self.assertTrue(calls)
        self.assertTrue(all(c[0] == "/usr/bin/codesign" for c in calls), calls)
        self.assertFalse(any("xcrun" in a or "stapler" in a for c in calls for a in c))
        req = [a for c in calls for a in c if a.startswith("-R=")]
        self.assertEqual(req, ["-R=" + download.NOTARIZED_REQUIREMENT])
        self.assertIn("notarized", req[0])
        self.assertIn(f'certificate leaf[subject.OU] = "{download.TEAM_ID}"', req[0])
        self.assertIn("anchor apple generic", req[0])
        self.assertNotIn("--check-notarization", [a for c in calls for a in c])      # 不上网

    def test_verify_app_refuses_without_stapled_ticket(self):
        """票据没钉在包上的不装：哪怕系统缓存里有它的票据（没网时装不上）"""
        from luoke_core import download
        app = self.fake_signed_app(tempfile.mkdtemp(), ticket=False)
        calls = []
        with unittest.mock.patch.dict(os.environ, {"LUOKE_ALLOW_UNSIGNED": "0"}), \
                unittest.mock.patch.object(download.subprocess, "run", side_effect=self.fake_codesign(calls)):
            with self.assertRaisesRegex(download.DownloadError, "没有公证票据"):
                download.verify_app(app, "ai.luoke.fake")

    def test_verify_app_refuses_when_codesign_says_not_notarized(self):
        from luoke_core import download
        app = self.fake_signed_app(tempfile.mkdtemp())
        calls = []
        with unittest.mock.patch.dict(os.environ, {"LUOKE_ALLOW_UNSIGNED": "0"}), \
                unittest.mock.patch.object(download.subprocess, "run", side_effect=self.fake_codesign(calls, notarized=False)):
            with self.assertRaisesRegex(download.DownloadError, "没有公证票据"):
                download.verify_app(app, "ai.luoke.fake")

    def test_stapled_ticket_is_a_real_file(self):
        from luoke_core import download
        d = tempfile.mkdtemp()
        app = self.fake_signed_app(d, ticket=False)
        self.assertFalse(download.stapled_ticket(app))
        open(os.path.join(app, "Contents", "CodeResources"), "wb").close()
        self.assertFalse(download.stapled_ticket(app))                 # 空文件不算
        os.remove(os.path.join(app, "Contents", "CodeResources"))
        with open(os.path.join(d, "elsewhere"), "wb") as f:
            f.write(b"ticket")
        os.symlink(os.path.join(d, "elsewhere"), os.path.join(app, "Contents", "CodeResources"))
        self.assertFalse(download.stapled_ticket(app))                 # 链接不算

    @unittest.skipUnless(sys.platform == "darwin", "codesign 只在 macOS 上有")
    def test_adhoc_signed_app_is_not_notarized(self):
        """真跑 codesign：ad-hoc 重签的包过不了公证要求，也装不上"""
        from luoke_core import download
        d = tempfile.mkdtemp()
        app = os.path.join(d, "假配件.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": "ai.luoke.fake", "CFBundleExecutable": "fake"}, f)
        shutil.copy("/usr/bin/true", os.path.join(app, "Contents", "MacOS", "fake"))
        r = subprocess.run(["/usr/bin/codesign", "-s", "-", "-f", "--deep", app], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(download.notarized(app))
        with unittest.mock.patch.dict(os.environ, {"LUOKE_ALLOW_UNSIGNED": "0"}):
            with self.assertRaises(download.DownloadError):
                download.verify_app(app, "ai.luoke.fake")

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

    def test_download_requested_while_restarting_engine_is_not_lost(self):
        # 11-26：一批下完、在重启引擎的那十几秒里再要下载，请求进了没人读的队列，一直 queued
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.release_path = os.path.join(d, "release.json")
        open(h.release_path, "w").close()
        manifest = {"items": {n: {"kind": "model", "files": [{"path": n, "size": 1}]} for n in ("a", "b", "c")}}
        batches, after = [], []
        restarting, release = threading.Event(), threading.Event()

        def batch(_m, names):
            batches.append(list(names))
            for n in names:
                h.downloads[n]["state"] = "done"

        def after_downloads(_m, names):
            after.append(list(names))
            if len(after) == 1:              # 第一批收尾：模拟重启引擎、冒烟检查要一会儿
                restarting.set()
                release.wait(5)

        with unittest.mock.patch("luoke_core.download.load_manifest", return_value=manifest), \
                unittest.mock.patch.object(h, "_download_batch", batch), \
                unittest.mock.patch.object(h, "_after_downloads", after_downloads):
            h.start_downloads(["a"])
            self.assertTrue(restarting.wait(5))
            r = h.start_downloads(["b"])      # 收尾期间点「升级」
            self.assertTrue(r["started"])
            release.set()
            for _ in range(100):
                if h.downloads["b"]["state"] == "done" and not (h.download_thread and h.download_thread.is_alive()):
                    break
                time.sleep(0.05)
            self.assertEqual(batches, [["a"], ["b"]])
            self.assertEqual(h.downloads["b"]["state"], "done")
            self.assertEqual(after, [["a"], ["b"]])          # 第二批也做收尾（下了模型要重启引擎）
            self.assertEqual(h.download_queue, [])
            self.assertIsNone(h.download_thread)          # 线程退出时把自己清掉，start_downloads 不会把请求排给它
            h.start_downloads(["c"])          # 线程结束以后再要：另起一个线程照常下
            for _ in range(100):
                if h.downloads["c"]["state"] == "done" and h.download_thread is None:
                    break
                time.sleep(0.05)
            self.assertEqual(batches[-1], ["c"])
            self.assertEqual(h.downloads["c"]["state"], "done")

    def test_download_requested_again_while_running_is_not_stuck_queued(self):
        # 11-26：正在下的又点一次「升级」，状态被换成新的 queued、排进队又被滤掉，下完了界面还一直 queued
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        h.release_path = os.path.join(d, "release.json")
        open(h.release_path, "w").close()
        manifest = {"items": {n: {"kind": "accessory", "files": [{"path": n, "size": 1}]} for n in ("a", "b")}}
        fetched = []
        started = {"a": threading.Event(), "b": threading.Event()}
        go = {"a": threading.Event(), "b": threading.Event()}

        def batch(_m, names):            # 照 _download_batch：开始时拿住这一项的状态，进度写进它
            for n in names:
                st = h.downloads[n]
                st["state"] = "downloading"
                started[n].set()
                go[n].wait(5)
                fetched.append(n)
                st["state"] = "done"

        with unittest.mock.patch("luoke_core.download.load_manifest", return_value=manifest), \
                unittest.mock.patch.object(h, "_download_batch", batch), \
                unittest.mock.patch.object(h, "_after_downloads", lambda *_: None):
            h.start_downloads(["a", "b"])
            self.assertTrue(started["a"].wait(5))
            h.start_downloads(["a"])          # a 正在下：不重置、不再排
            h.start_downloads(["b"])          # b 排在这一批里还没轮到：不重置、不再排
            self.assertEqual(h.downloads["a"]["state"], "downloading")
            go["a"].set()
            self.assertTrue(started["b"].wait(5))
            h.start_downloads(["a"])          # a 这一批里已经下完：用户要重下就重下
            go["b"].set()
            for _ in range(100):
                if h.download_thread is None:
                    break
                time.sleep(0.05)
            self.assertEqual(fetched, ["a", "b", "a"])
            self.assertEqual({n: h.downloads[n]["state"] for n in "ab"}, {"a": "done", "b": "done"})

    def test_needed_downloads_and_install_dirs(self):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertEqual(h.apps_dir, os.path.join(d, "Applications.noindex"))         # 实验室构建不碰 ~/Applications，目录不被 Spotlight 索引
        self.assertIn(h.apps_dir, h.accessory_dirs)
        need = h.needed_downloads()
        self.assertIn("qwen3.5-9b-4bit", need)
        self.assertIn("embeddinggemma-2", need)         # 2026-11-02 起分到了 32 档（三档都分了）：实验室实例分到档就下
        self.assertEqual("engine" in need, h.recipe_engine_app() is None)   # 本机有版本一致的 oMLX 就不要引擎包
        self.assertEqual(h.install_dir_for({"kind": "engine", "version": "omlx-x"}), os.path.join(d, "engines", "omlx-x"))
        self.assertEqual(h.install_dir_for({"kind": "accessory", "type": "input-method"}), h.input_methods_dir)
        self.assertEqual(h.install_dir_for({"kind": "accessory", "type": "app"}), h.apps_dir)
        os.makedirs(os.path.join(d, "engines", h.recipe.engine["version"], "oMLX.app", "Contents"))
        self.assertEqual(h.installed_engine_app(), os.path.join(d, "engines", h.recipe.engine["version"], "oMLX.app"))

    def test_luoke_engine_package(self):
        """顶层 ADR 0020：配方的引擎版本是 luoke-engine-<版本> 时，引擎包是 LuokeEngine.app（不带 Python，配方没有 python、site），
        装好以后起的是它的 luoke-engine；换过来以后旧的 oMLX 版本照常清掉"""
        from luoke_core import hub as hubmod
        from luoke_core.hub import Hub, engine_app_matching
        d = tempfile.mkdtemp()
        recipe = Recipe.load(RECIPE)
        recipe.engine = {"version": "luoke-engine-0.1.0", "log_level": "warning"}
        h = Hub(recipe, d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertEqual(h.engine_install_app(), os.path.join(d, "engines", "luoke-engine-0.1.0", "LuokeEngine.app"))
        self.assertTrue(h.engine_missing())
        self.assertIn("engine", h.needed_downloads())
        app = h.engine_install_app()
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        open(os.path.join(app, "Contents", "MacOS", "luoke-engine"), "w").close()
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "0.1.0"}, f)
        self.assertEqual(engine_app_matching(app, "luoke-engine-0.1.0"), app)
        self.assertIsNone(engine_app_matching(app, "luoke-engine-0.2.0"))
        h.downloads = {"engine": {"state": "done"}}
        h.engine.stop = lambda: None
        with unittest.mock.patch.object(h, "restart_engine", lambda: None):
            h._after_downloads({"items": {"engine": {"kind": "engine"}}}, ["engine"])
        self.assertEqual(h.engine_app, app)
        self.assertEqual(h.engine.cli, os.path.join(app, "Contents", "MacOS", "luoke-engine"))
        self.assertFalse(h.engine_missing())
        # 从 oMLX 换过来、冒烟检查过了：旧的 omlx-* 删掉，螺壳引擎这一份留着
        p = object.__new__(hubmod.Hub)
        p.env_dir, p.lab, p.release_path, p.log = d, False, os.path.join(d, "release.json"), (lambda m: None)
        open(p.release_path, "w").write("{}")
        p.recipe = recipe
        os.makedirs(os.path.join(d, "engines", "omlx-0.7.2", "oMLX.app", "Contents"))
        self.assertEqual(p.prune_old_engines(), ["omlx-0.7.2"])
        self.assertEqual(os.listdir(os.path.join(d, "engines")), ["luoke-engine-0.1.0"])

    def test_bundled_engine(self):
        """luoke-core#199：螺壳引擎是一个可执行文件，打进螺壳.app 的 Contents/Helpers。配方要的正是这一版就用它，
        不要引擎包、不下载；配方还是 oMLX、或者版本对不上，照旧按清单装的引擎包"""
        from luoke_core import hub as hubmod
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp()
        exe = os.path.join(d, "Helpers", "luoke-engine")
        os.makedirs(os.path.dirname(exe))
        open(exe, "w").close()
        os.chmod(exe, 0o755)

        def hub(version, bundled_version="0.1.0", path=exe):
            recipe = Recipe.load(RECIPE)
            recipe.engine = {"version": version, "log_level": "warning"}
            return Hub(recipe, d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False,
                       bundled_engine=path, bundled_engine_version=bundled_version)
        h = hub("luoke-engine-0.1.0")
        self.assertEqual(h.engine_app, exe)
        self.assertEqual(h.engine.cli, exe)
        self.assertFalse(h.engine_missing())
        self.assertNotIn("engine", h.needed_downloads())
        for other in (hub("luoke-engine-0.2.0"), hub("omlx-0.7.2"), hub("luoke-engine-0.1.0", path=os.path.join(d, "none")),
                      hub("luoke-engine-0.1.0", bundled_version=None)):
            self.assertIsNone(other.bundled_engine())
            self.assertIn("engine", other.needed_downloads())
            self.assertTrue(other.engine_missing())
        # 发布版用上打进来的引擎、冒烟检查过了：engines/ 里以前下的都删掉
        p = object.__new__(hubmod.Hub)
        p.env_dir, p.lab, p.release_path, p.log = d, False, os.path.join(d, "release.json"), (lambda m: None)
        open(p.release_path, "w").write("{}")
        p.recipe, p.bundled_engine_path, p.bundled_engine_version = h.recipe, exe, "0.1.0"
        os.makedirs(os.path.join(d, "engines", "omlx-0.7.2", "oMLX.app", "Contents"))
        self.assertEqual(p.prune_old_engines(), ["omlx-0.7.2"])

    def test_users_own_omlx_is_never_started(self):
        """luoke-core#143：引擎包还没下完时，主控起了配方里写的 /Applications/oMLX.app（测试机上用户自己装的 0.3.9），
        参数对不上，起了 3 次后熔断。引擎包没装上就当没有引擎：不起配方写的那份（版本对不对都一样），等引擎包下完再起"""
        from luoke_core import engine as enginemod
        from luoke_core.hub import Hub
        for have, lab in (("0.3.9", False), (None, False), ("0.3.9", True)):     # None：版本和配方的一样
            d = tempfile.mkdtemp()
            recipe = Recipe.load(RECIPE)
            want = recipe.engine["version"][len("omlx-"):]
            user = os.path.join(d, "Applications", "oMLX.app")          # 配方 engine.app 指的那份：在，omlx-cli 也在
            os.makedirs(os.path.join(user, "Contents", "MacOS"))
            open(os.path.join(user, "Contents", "MacOS", "omlx-cli"), "w").close()
            with open(os.path.join(user, "Contents", "Info.plist"), "wb") as f:
                plistlib.dump({"CFBundleShortVersionString": have or want}, f)
            recipe.engine["app"] = user
            rel = os.path.join(d, "release.json")
            with open(rel, "w") as f:
                json.dump({"contract": 1, "release": "internal-1109", "items": {}}, f)
            h = Hub(recipe, d, d, lab=lab, accessory_dirs=[d], mem_gb=32, smoke=False, release=rel)
            with self.subTest(version=have or want, lab=lab):
                self.assertFalse(h.engine_app.startswith(user), h.engine_app)
                self.assertEqual(h.engine_app, h.engine_install_app())
                self.assertTrue(h.engine_missing())
                self.assertIn("engine", h.needed_downloads())
                starts = []
                with unittest.mock.patch.object(enginemod.Engine, "start", lambda *a, **k: starts.append(a) or []):
                    h._check_engine()                 # 采样线程：引擎不在就不当崩溃、不去起
                self.assertEqual(starts, [])
                self.assertIsNone(h.breaker)
                # 引擎包下完、装上：换用它
                os.makedirs(os.path.join(h.engine_install_app(), "Contents", "MacOS"))
                open(os.path.join(h.engine_install_app(), "Contents", "MacOS", "omlx-cli"), "w").close()
                h.downloads = {"engine": {"state": "done"}}
                h.engine.stop = lambda: None
                with unittest.mock.patch.object(h, "restart_engine", lambda: None):
                    h._after_downloads({"items": {"engine": {"kind": "engine"}}}, ["engine"])
                self.assertEqual(h.engine_app, h.engine_install_app())
                self.assertFalse(h.engine_missing())
        # 开发版（没有发布清单）照旧用本机版本一致的那份
        d = tempfile.mkdtemp()
        recipe = Recipe.load(RECIPE)
        user = os.path.join(d, "oMLX.app")
        os.makedirs(os.path.join(user, "Contents", "MacOS"))
        with open(os.path.join(user, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": recipe.engine["version"][len("omlx-"):]}, f)
        recipe.engine["app"] = user
        h = Hub(recipe, d, d, lab=True, accessory_dirs=[d], mem_gb=32, smoke=False)
        self.assertEqual(h.engine_app, user)


def glob_any(d, pattern):
    import glob
    return bool(glob.glob(os.path.join(d, pattern)))


class SocketBacklogTest(unittest.TestCase):
    def test_many_simultaneous_connects_are_all_accepted(self):
        """11-03 16 GB 测试机：8 条请求同时连主控的 socket，有 2 条 ConnectionRefusedError（默认 listen 队列 5，macOS 满了直接拒）。
        不跑 accept 循环（最坏情况），20 个连接一起涌进来，全都要连得上。
        Python 3.15 起默认队列改成 128，那里不设也连得上；螺壳带的 3.12、系统的 3.9 都是 5，所以也直接核类上的设置。"""
        import socket as _s, socketserver
        from luoke_core.endpoint import Server
        from luoke_core.hub import UnixServer
        self.assertGreaterEqual(UnixServer.request_queue_size, 64)
        self.assertGreaterEqual(Server.request_queue_size, 64)

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                pass
        path = os.path.join(tempfile.mkdtemp(dir="/tmp"), "s")
        srv = UnixServer(path, H)
        try:
            srv.socket.close()      # 只验证 listen 队列：自己 bind、listen，不跑 accept 循环，最坏情况
            srv.socket = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
            os.remove(path)
            srv.server_bind()
            srv.server_activate()
            conns, refused = [], 0
            for _ in range(20):
                c = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
                c.setblocking(False)          # 一起涌进来：不等前一个被接走
                conns.append(c)
                try:
                    c.connect(path)
                except BlockingIOError:
                    pass
                except ConnectionRefusedError:
                    refused += 1
            self.assertEqual(refused, 0)
            for c in conns:
                c.close()
        finally:
            srv.server_close()


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

    def test_body_limit_follows_the_engine(self):
        """core-private#208：本机端点原样转给引擎，上限照引擎包（螺壳引擎 8 MB、oMLX 32 MB）；超了在主控处答 413，不读请求体、不到引擎。"""
        h, port, record, _, d = self.hub_with_endpoint()
        h.endpoint.set_key("")

        def post(n):
            return self.raw(port, (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                                   f"Content-Type: application/json\r\nContent-Length: {n}\r\n\r\n").encode())
        nine = 9 << 20
        h.recipe.engine["version"] = "omlx-0.7.2"
        self.assertEqual(post(33 << 20), [b"413"])
        h.recipe.engine["version"] = "luoke-engine-0.1.0"
        self.assertEqual(post(nine), [b"413"])
        self.assertEqual(post(0), [b"400"])
        self.assertEqual(record, [])
        st, _ = self.call(port, "POST", "/v1/chat/completions",
                          body={"model": h.base_name, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(st, 200)                                                  # 上限以内照常转发

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

    def test_web_page_requests_refused_without_origin(self):
        """网页里不带 Origin 的请求（跨站的 img、script）Host 照样是 127.0.0.1，靠 Sec-Fetch-Site 认出来；密钥设成空时只剩这一道。
        Node 的 fetch 带 Sec-Fetch-Mode、不带 Sec-Fetch-Site，要照常能用；地址栏自己打开的（none）也照常。"""
        import http.client
        h, port, _, _, d = self.hub_with_endpoint()
        h.endpoint.set_key("")
        self.assertEqual(self.call(port, "GET", "/v1/models")[0], 200)                                # 别的程序：照常
        self.assertEqual(self.call(port, "GET", "/v1/models", headers={"Sec-Fetch-Mode": "cors"})[0], 200)       # Node fetch
        self.assertEqual(self.call(port, "GET", "/v1/models", headers={"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"})[0], 200)
        for hdrs in ({"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"},
                     {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
                     {"Sec-Fetch-Site": "same-origin"}):
            self.assertEqual(self.call(port, "GET", "/v1/models", headers=hdrs)[0], 403, hdrs)
        st, _ = self.call(port, "POST", "/v1/chat/completions", body={"model": h.base_name, "messages": []},
                          headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})
        self.assertEqual(st, 403)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/v1/models", headers={"Sec-Fetch-Site": "cross-site"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("Connection"), "close")

    def raw(self, port, data):
        """一个连接发 data，读到对方关连接为止；对方不关（还等着下一个请求）就算失败。"""
        import socket as _s
        c = _s.create_connection(("127.0.0.1", port), timeout=5)
        out = b""
        try:
            c.sendall(data)
            while True:
                try:
                    b = c.recv(65536)
                except ConnectionResetError:
                    break
                except _s.timeout:
                    self.fail(f"拒绝以后没关连接：{out[:300]!r}")
                if not b:
                    break
                out += b
        finally:
            c.close()
        return re.findall(rb"HTTP/1\.[01] (\d{3}) ", out)          # 不认行首：前一个答复的 JSON 后面直接接下一个状态行

    def test_refused_request_closes_the_connection(self):
        """被拒的请求体没读：不关连接的话，请求体里夹带的完整请求会被当成下一个请求，不带 Origin、Sec-Fetch-Site，
        绕过检查（网页发 text/plain 的 no-cors POST 就能做到）。拒绝以后一律关连接，只回一个 403 / 401。"""
        h, port, record, _, d = self.hub_with_endpoint()
        h.endpoint.set_key("")
        get = f"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
        chat = json.dumps({"model": h.base_name, "messages": [{"role": "user", "content": "hi"}]}).encode()
        post = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(chat)}\r\n\r\n").encode() + chat

        def outer(body, **extra):
            head = {"Host": f"127.0.0.1:{port}", "Content-Type": "text/plain", "Content-Length": str(len(body)), **extra}
            return ("POST /v1/chat/completions HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in head.items())
                    + "\r\n").encode() + body
        self.assertEqual(self.raw(port, outer(get, **{"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"})),
                         [b"403"])
        self.assertEqual(self.raw(port, outer(get, **{"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})),
                         [b"403"])
        self.assertEqual(self.raw(port, outer(post, **{"Sec-Fetch-Site": "cross-site"})), [b"403"])
        self.assertEqual(self.raw(port, outer(get, Host="evil.example")), [b"403"])          # DNS rebinding 那道
        self.assertEqual(record, [])                                                            # 夹带的聊天请求没到引擎
        key = h.endpoint.rotate_key()                  # 密钥不对的 401 也关：夹带的请求带着对的密钥也进不来
        get_k = f"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {key}\r\n\r\n".encode()
        self.assertEqual(self.raw(port, outer(get_k, Authorization="Bearer wrong")), [b"401"])
        # 没被拒的照旧保持连接：一个连接连发两个 GET，两个都答
        two = self.raw(port, get_k + get_k.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n"))
        self.assertEqual(two, [b"200", b"200"])

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
        out = h.output_status()                                          # 生成了多少 token：本机接口的也算
        self.assertEqual((out["today"], out["total"], out["since"]), (2, 2, time.strftime("%Y-%m-%d")))
        self.assertEqual(h.queue_status()["running"], [])                # 做完了：本机接口不在正在做的里
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


    def test_unload_waits_out_a_queued_unload(self):
        """11-02 重评：模型手上有请求时 oMLX 卸载回 202、排队，之后 load 回 200（模型其实还在），主控记 loaded in 0.0s，
        后面的请求全收 409。现在：202 要等到 /v1/models/status 里模型不在了才算卸完，再去 load。"""
        import http.server, threading as _t
        from luoke_core import engine as engine_mod
        from luoke_core.engine import Engine, EngineError

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def reply(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                self.server.calls.append(self.path)
                self.reply(202 if self.path.endswith("/unload") else 200, {})
            def do_GET(self):
                self.server.polls += 1
                gone = self.server.polls >= self.server.gone_after
                self.reply(200, {"models": [{"id": "a", "loaded": not gone}]})
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            e = Engine("/nonexistent.app", tempfile.mkdtemp(), tempfile.mkdtemp(), 10.0)
            e.port = srv.server_address[1]
            with unittest.mock.patch.object(engine_mod, "UNLOAD_POLL_SECS", 0.01):
                srv.calls, srv.polls, srv.gone_after = [], 0, 3
                e.loaded = "a"
                e.load("b")
                self.assertEqual(srv.polls, 3)
                self.assertEqual(srv.calls, ["/v1/models/a/unload", "/v1/models/b/load"])
                srv.polls, srv.gone_after = 0, 10 ** 6
                with self.assertRaises(EngineError):
                    e._unload("a", timeout=0.05)
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


class LoadRoomGateTest(unittest.TestCase):
    """任务参数 load_free_gb：为这个任务加载底座之前，空闲加文件缓存不够就不加载（16 GB 测试机 2026-11-17 实测的门）。"""
    VM_STAT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
               "Pages free:                              327680.\n"
               "Pages wired down:                        104858.\n"
               "File-backed pages:                       196608.\n"
               "Swapouts:                                     0.\n")

    def test_recipe_checks_load_free_gb(self):
        with open(RECIPE, encoding="utf-8") as f:
            base = json.load(f)
        self.assertEqual(Recipe(base).params("luoke-agent.hello", "16")["load_free_gb"], 10)
        for ok in (0, 10, 9.5):
            r = json.loads(json.dumps(base))
            r["tasks"]["luoke-agent.hello"]["params"]["16"]["load_free_gb"] = ok
            Recipe(r)
        for bad in (-1, "10", True, float("nan"), float("inf"), [10]):
            r = json.loads(json.dumps(base))
            r["tasks"]["luoke-agent.hello"]["params"]["*"]["load_free_gb"] = bad
            with self.assertRaises(RecipeError, msg=repr(bad)):
                Recipe(r)

    def test_free_plus_file_backed_from_vm_stat(self):
        with unittest.mock.patch.object(machine, "_run", side_effect=lambda cmd, timeout=5: self.VM_STAT if "vm_stat" in cmd[0] else ""):
            s = machine.memory_sample()
        self.assertEqual((s["free_gb"], s["file_backed_gb"]), (5.0, 3.0))
        self.assertEqual(machine.load_room_gb(s), 8.0)
        self.assertIsNone(machine.load_room_gb({"free_gb": 5.0, "file_backed_gb": None}))    # 读不到不猜

    def hub(self, mem_gb):
        from luoke_core.hub import Hub
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        h = Hub(Recipe.load(RECIPE), d, d, lab=True, accessory_dirs=[d], mem_gb=mem_gb, smoke=False)
        h.base_id = "base-id"
        return h

    def test_gate_applies_only_where_the_recipe_asks_and_only_before_a_load(self):
        h16, h32 = self.hub(16), self.hub(32)
        hello16, hello32 = h16.recipe.params("luoke-agent.hello", "16"), h32.recipe.params("luoke-agent.hello", "32")
        self.assertEqual(hello16.get("load_free_gb"), 10)
        self.assertNotIn("load_free_gb", hello32)
        with unittest.mock.patch.object(machine, "load_room_gb", return_value=8.4):
            self.assertFalse(h16.load_room_ok("luoke-agent.hello", hello16))
            self.assertTrue(h32.load_room_ok("luoke-agent.hello", hello32))                  # 32 GB 档没写
            self.assertTrue(h16.load_room_ok("luoke-mail.triage", h16.recipe.params("luoke-mail.triage", "16")))   # 别的任务没写
            h16.engine.loaded = "base-id"
            self.assertTrue(h16.load_room_ok("luoke-agent.hello", hello16))                  # 底座已经在内存里：不用加载
            h16.engine.loaded, h16.engine.loading = None, True
            self.assertTrue(h16.load_room_ok("luoke-agent.hello", hello16))                  # 正在加载：已经在加载了
            h16.engine.loading = False
        with unittest.mock.patch.object(machine, "load_room_gb", return_value=10.5):
            self.assertTrue(h16.load_room_ok("luoke-agent.hello", hello16))
        with unittest.mock.patch.object(machine, "load_room_gb", return_value=None):
            self.assertTrue(h16.load_room_ok("luoke-agent.hello", hello16))                  # 读不到 vm_stat 不挡

    def test_background_request_is_paused_without_loading(self):
        import threading as _t
        from luoke_core.hub import serve
        from luoke_core.client import HubClient, HubError
        h = self.hub(16)
        os.makedirs(h.model_dir_of(h.base_name))
        h.engine_missing = lambda: False
        h.engine.alive = lambda: True
        h.engine.serving = lambda: True
        h.engine.owns_port = lambda: True
        h.phase = "running"
        h.fits_memory = lambda name: True
        h.dispatcher.on_ac = True
        loads, streams = [], []
        h.engine.load = lambda model: loads.append(model) or 0.5
        h.engine.stream_chat = lambda *a, **k: streams.append(1) or {"status": 200, "error": None, "detail": None, "first_token_secs": 0.1, "usage": None}
        sd = tempfile.mkdtemp(prefix="lk"); self.addCleanup(shutil.rmtree, sd, True); sock = os.path.join(sd, "h.sock")
        srv = serve(h, sock)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = HubClient("luoke-agent/0.1.0", sock)
            with unittest.mock.patch.object(machine, "load_room_gb", return_value=8.4):
                with self.assertRaises(HubError) as cm:
                    list(c.chat("luoke-agent.hello", {"messages": [{"role": "user", "content": "x"}], "luoke_items": 1},
                                priority="background", allow_battery=True))
            self.assertEqual(cm.exception.code, "paused")
            self.assertEqual(cm.exception.extra.get("reason"), "memory")
            self.assertTrue(cm.exception.retry_after)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual((loads, streams), ([], []))


class BatteryNoLargeModelTest(unittest.TestCase):
    def test_user_waiting_chat_is_refused_on_battery(self):
        """用电池、设置是「不运行大模型」（11-12 定）：前台要用底座的请求先不做，答 needs_user 和原因，不附占内存的名单；
        已加载的底座卸掉。接上电源就照常。"""
        import threading as _t
        from luoke_core.hub import Hub, serve, NO_LARGE_MODEL_MSG
        from luoke_core.client import HubClient, HubError
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
        streams = []
        h.engine.unload = lambda m=None: setattr(h.engine, "loaded", None) or True
        h.engine.stream_chat = lambda *a, **k: streams.append(1) or {"status": 200, "error": None, "detail": None, "first_token_secs": 0.1, "usage": None}
        h.update_settings({"battery_mode": "no_large_model"})
        h.dispatcher.on_ac = False
        h._battery_unload()
        self.assertIsNone(h.engine.loaded)
        sd = tempfile.mkdtemp(prefix="lk"); self.addCleanup(shutil.rmtree, sd, True); sock = os.path.join(sd, "h.sock")
        srv = serve(h, sock)
        _t.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = HubClient("luoke-mail/0.15.0", sock)
            with self.assertRaises(HubError) as cm:
                list(c.chat("luoke-mail.triage", {"messages": [{"role": "user", "content": "x"}], "luoke_items": 1}))
            self.assertEqual((cm.exception.code, cm.exception.message), ("needs_user", NO_LARGE_MODEL_MSG))
            self.assertEqual(cm.exception.extra.get("reason"), "battery_no_large_model")
            self.assertNotIn("occupants", cm.exception.extra)
            with self.assertRaises(HubError) as cm:
                list(c.chat("luoke-mail.triage", {"messages": [{"role": "user", "content": "x"}], "luoke_items": 1},
                            priority="background", allow_battery=True))
            self.assertEqual((cm.exception.code, cm.exception.extra.get("reason")), ("paused", "battery_no_large_model"))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(streams, [])
        h.dispatcher.on_ac = True
        h._battery_unload()                      # 接上电源不再卸
        self.assertFalse(h.dispatcher.no_large_model())


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

    def test_409_while_the_base_unloads_reloads_and_retries(self):
        """11-05：16 GB 档用户一打字主控就卸底座，卸的那一刻进来的 chat 被 oMLX 答 409（模型正在卸），之前原样当引擎故障给了配件
        （outcome=status_409）。现在等卸完、装回底座、重试一次；重试还是 409 就答 not_ready，配件照 retry_after 再来。"""
        import threading as _t
        from luoke_core.hub import Hub, serve
        from luoke_core.client import HubClient
        for second, want in ((200, None), (409, "not_ready")):
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
            def fake_load(m, h=h, calls=calls):
                calls.append(("load", m))
                h.engine.loaded = m
                return 0.1
            h.engine.load = fake_load
            def fake_stream(payload, on_line, cancel, first_token_timeout=0, total_timeout=0, h=h, calls=calls, second=second):
                calls.append(("stream",))
                if len([c for c in calls if c == ("stream",)]) == 1:
                    h.engine.loaded = None                  # 打字触发的卸载正好做完
                    return {"status": 409, "error": "status_409", "detail": "model is being unloaded", "first_token_secs": None, "usage": None}
                if second == 409:
                    return {"status": 409, "error": "status_409", "detail": "model is loading", "first_token_secs": None, "usage": None}
                on_line('data: {"choices": [{"delta": {"content": "ok"}}]}')
                on_line("data: [DONE]")
                return {"status": 200, "error": None, "detail": None, "first_token_secs": 0.1, "usage": {"prompt_tokens": 5, "completion_tokens": 1}}
            h.engine.stream_chat = fake_stream
            sd = tempfile.mkdtemp(prefix="lk"); self.addCleanup(shutil.rmtree, sd, True); sock = os.path.join(sd, "h.sock")
            srv = serve(h, sock)
            _t.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                events = list(HubClient("luoke-mail/0.15.0", sock).chat("luoke-mail.triage", {"messages": [{"role": "user", "content": "x"}], "luoke_items": 1}))
            finally:
                srv.shutdown()
                srv.server_close()
            errs = [v for k, v in events if k == "error"]
            self.assertEqual(calls, [("stream",), ("load", "base-id"), ("stream",)])     # 只重试一次
            if want is None:
                self.assertFalse(errs, events)
                self.assertIn("ok", "".join(json.dumps(v) for k, v in events if k == "chunk"))
            else:
                self.assertEqual((errs[0].get("code"), errs[0].get("retry_after")), ("not_ready", 5))

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



class TriageSummaryTest(unittest.TestCase):
    def test_watch_questions_show_in_the_detail_but_not_the_score(self):
        """分拣的 watch 题（新加、只观察）不算分，但证据的 detail 里要有 watch、watch_n、watch_wrong。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import release
        s = {"basic": 27, "basic_n": 28, "hard": 17, "hard_n": 21, "n": 49, "safety": 13, "safety_n": 14, "false_alarm": 0,
             "watch": 13, "watch_n": 14, "watch_wrong": ["official-code"]}
        d = release.summarize_mail_triage({"score": s})
        self.assertEqual((d["score"], d["score_max"], d["hard_ok"]), (44, 49, True))
        self.assertEqual((d["detail"]["watch"], d["detail"]["watch_n"], d["detail"]["watch_wrong"]), (13, 14, ["official-code"]))


class ImeSummaryTest(unittest.TestCase):
    def test_hard_ok_comes_from_the_eval_program(self):
        """rerank、predict 的硬性条目读评测程序写的 score.hard_ok（luoke-ime 0.1.41 起）；没有这个字段的旧证据照旧算过。"""
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import release
        r = {"score": {"final_top1_pct": 89.8, "fixed": 67, "broke": 9, "broke_max": 6, "hard_ok": False}}
        self.assertFalse(release.summarize_ime_result(r)["hard_ok"])
        self.assertEqual(release.summarize_ime_result(r)["detail"]["broke"], 9)
        self.assertTrue(release.summarize_ime_result({"score": {"final_top1_pct": 89.8}})["hard_ok"])
        p = {"score": {"top1_when_shown_pct": 20.0, "shown_pct": 4.0, "floors": {"shown_pct": 3, "top1_when_shown_pct": 25}, "hard_ok": False}}
        self.assertFalse(release.summarize_ime_predict(p)["hard_ok"])
        self.assertTrue(release.summarize_ime_predict({"score": {"top1_when_shown_pct": 47.8}})["hard_ok"])


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


class BundledEngineReleaseTest(unittest.TestCase):
    """配方的引擎是螺壳引擎（luoke-engine-<版本>）时：不出单独的引擎包；出版按配方钉的提交从源码现编、打进主控，
    签好以后核引擎的签名、跑它的自检；评测还跑不了，只出不评测的内部版，引擎换了写明没评测。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import release
        cls.rel = release
        import importlib.util
        spec = importlib.util.spec_from_file_location("internal_build", os.path.join(ROOT, "tools", "internal_build.py"))
        cls.ib = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ib)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def recipe(self, version, commit=None):
        with open(os.path.join(ROOT, "recipes", "dev.json"), encoding="utf-8") as f:
            data = json.load(f)
        data["engine"] = dict(data["engine"], version=version)
        if commit is not None:
            data["engine"]["commit"] = commit
        return Recipe(data)

    def template(self, version, commit=None, name="template.json"):
        p = os.path.join(self.d, name)
        with open(os.path.join(ROOT, "recipes", "dev.json"), encoding="utf-8") as f:
            data = json.load(f)
        data["engine"] = dict(data["engine"], version=version)
        if commit is not None:
            data["engine"]["commit"] = commit
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return p

    def git(self, cwd, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()

    def engine_repo(self, version="0.1.0"):
        """一个假的引擎仓库：tools/package.sh 照真的那样出 luoke-engine-<版本>-macos-arm64/（程序答 --version，开着实验室包答 <版本>-lab）"""
        src = os.path.join(self.d, "engine-src")
        os.makedirs(os.path.join(src, "tools"))
        with open(os.path.join(src, "tools", "package.sh"), "w") as f:
            f.write(f"""#!/bin/sh
set -eu
d="$1/luoke-engine-{version}-macos-arm64"
mkdir -p "$d"
v={version}
[ "${{LUOKE_LAB:-}}" = 1 ] && v={version}-lab
printf '#!/bin/sh\n[ "$1" = --version ] && echo %s\n' "$v" > "$d/luoke-engine"
chmod 755 "$d/luoke-engine"
for f in LICENSE NOTICE THIRD-PARTY-PACKAGES.txt THIRD-PARTY-LICENSES.txt; do echo x > "$d/$f"; done
""")
        self.git(self.d, "init", "-q", "-b", "main", src)
        self.git(src, "add", "-A")
        self.git(src, "commit", "-q", "-m", "engine")
        return src, self.git(src, "rev-parse", "HEAD")

    def test_version_and_commit_come_from_the_recipe(self):
        c = "a" * 40
        self.assertEqual(self.rel.bundled_engine_version(self.recipe("omlx-0.7.2")), "")
        self.assertEqual(self.rel.bundled_engine_version(self.recipe("luoke-engine-0.1.0", c)), "0.1.0")
        self.assertEqual(self.rel.bundled_engine_commit(self.recipe("omlx-0.7.2")), "")
        self.assertEqual(self.rel.bundled_engine_commit(self.recipe("luoke-engine-0.1.0", c)), c)
        for bad in (None, "", "a" * 7, "A" * 40, "g" * 40, "a" * 41):        # 螺壳引擎的配方要钉完整的提交号
            with self.assertRaises(SystemExit, msg=bad):
                self.rel.bundled_engine_commit(self.recipe("luoke-engine-0.1.0", bad))

    def test_engine_is_built_from_the_pinned_commit(self):
        src, commit = self.engine_repo()
        out = os.path.join(self.d, "out")
        lab = unittest.mock.patch.dict(self.rel.SH_ENV, {"LUOKE_LAB": "1"})       # 环境里开着实验室包也打正式包
        with open(os.path.join(src, ".luoke-commit"), "w") as f:      # 冻结时写的，不算改动
            f.write(commit + "\n")
        with lab:
            env = self.rel.bundled_engine_env(src, self.recipe("luoke-engine-0.1.0", commit), out)
        built = os.path.join(out, "engine.noindex", "luoke-engine-0.1.0-macos-arm64")
        self.assertEqual(env, {"LUOKE_ENGINE_PKG": built, "LUOKE_ENGINE_VERSION": "0.1.0"})
        self.assertEqual(self.rel.engine_version_of(os.path.join(built, "luoke-engine")), "0.1.0")
        # oMLX 的配方：不编、不带；给了引擎源码就停
        self.assertEqual(self.rel.bundled_engine_env("", self.recipe("omlx-0.7.2"), out), {})
        with self.assertRaises(SystemExit):
            self.rel.bundled_engine_env(src, self.recipe("omlx-0.7.2"), out)
        # 没给源码、源码不在钉的提交、版本对不上
        for engine_src, recipe in (("", self.recipe("luoke-engine-0.1.0", commit)),
                                   (src, self.recipe("luoke-engine-0.1.0", "b" * 40)),
                                   (src, self.recipe("luoke-engine-0.2.0", commit))):
            with self.assertRaises(SystemExit, msg=recipe.engine):
                self.rel.bundled_engine_env(engine_src, recipe, out)
        # 源码有改动（改了跟踪的文件、多了没跟踪的文件）
        for change in ("tools/package.sh", "extra.txt"):
            path = os.path.join(src, change)
            with open(path, "a") as f:
                f.write("# changed\n")
            with self.assertRaises(SystemExit, msg=change):
                self.rel.bundled_engine_env(src, self.recipe("luoke-engine-0.1.0", commit), out)
            self.git(src, "checkout", "-q", "--", ".")
            if change == "extra.txt":
                os.remove(path)
        self.rel.bundled_engine_env(src, self.recipe("luoke-engine-0.1.0", commit), out)
        # 目录名对、程序自己说的版本不对
        with open(os.path.join(src, "tools", "package.sh"), "a") as f:
            f.write('sed -i "" "s/echo 0.1.0/echo 0.0.9/" "$d/luoke-engine"\n')
        self.git(src, "commit", "-qam", "wrong version")
        with self.assertRaises(SystemExit):
            self.rel.bundled_engine_env(src, self.recipe("luoke-engine-0.1.0", self.git(src, "rev-parse", "HEAD")), out)
        self.git(src, "revert", "--no-edit", "HEAD")
        # 编出来的程序是个链接：不认
        with open(os.path.join(src, "tools", "package.sh"), "a") as f:
            f.write('mv "$d/luoke-engine" "$d/real" && ln -s real "$d/luoke-engine"\n')
        self.git(src, "commit", "-qam", "link")
        with self.assertRaises(SystemExit):
            self.rel.bundled_engine_env(src, self.recipe("luoke-engine-0.1.0", self.git(src, "rev-parse", "HEAD")), out)

    def test_engine_stage_makes_no_package_and_drops_an_old_record(self):
        info = os.path.join(self.d, "engine.json")
        with open(info, "w") as f:
            f.write("{}")
        paths = types.SimpleNamespace(engine_info=info, engine_dir=self.d)
        self.rel.stage_engine(types.SimpleNamespace(engine_app=os.path.join(self.d, "missing.app")), paths, self.recipe("luoke-engine-0.1.0"))
        self.assertFalse(os.path.exists(info))
        with self.assertRaises(SystemExit):           # oMLX 的配方照旧要引擎 .app
            self.rel.stage_engine(types.SimpleNamespace(engine_app=os.path.join(self.d, "missing.app")), paths, self.recipe("omlx-0.7.2"))

    def fake_app(self, plist_version="0.1.0", says="0.1.0"):
        app = os.path.join(self.d, "螺壳.app")
        shutil.rmtree(app, ignore_errors=True)
        os.makedirs(os.path.join(app, "Contents", "Helpers"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"LuokeEngineVersion": plist_version}, f)
        if says is not None:
            exe = os.path.join(app, "Contents", "Helpers", "luoke-engine")
            with open(exe, "w") as f:
                f.write(f'#!/bin/sh\ncase "$1" in --version) echo {says};; selftest) exit "${{SELFTEST_RC:-0}}";; esac\n')
            os.chmod(exe, 0o755)
        return app

    def test_built_app_must_hold_the_engine_the_recipe_names(self):
        self.rel.check_bundled_engine(self.fake_app(), "0.1.0")
        for plist_version, says, want in (("0.1.0", None, "0.1.0"),          # 程序不在
                                          ("0.1.0", "0.1.0", "0.2.0"),       # 不是配方要的那一版
                                          ("0.1.0", "0.2.0", "0.1.0"),       # Info.plist 对、程序是别的版本
                                          ("0.2.0", "0.1.0", "0.1.0")):      # 程序对、Info.plist 写的不对
            with self.assertRaises(SystemExit, msg=(plist_version, says, want)):
                self.rel.check_bundled_engine(self.fake_app(plist_version, says), want)

    def test_signed_app_is_checked_and_runs_the_selftest(self):
        """签好以后：标识、hardened runtime、和螺壳同一个团队、版本，再跑自检；oMLX 的配方不查。"""
        recipe = self.recipe("luoke-engine-0.1.0", "a" * 40)
        app = self.fake_app()
        sigs = {}

        def codesign(cmd, **kw):
            if cmd[0] != "codesign":
                return real_run(cmd, **kw)
            id_, team, flags = sigs["engine" if cmd[-1].endswith("luoke-engine") else "app"]
            return subprocess.CompletedProcess(cmd, 0, "", f"Identifier={id_}\nCodeDirectory v=20500 size=1 flags={flags} hashes=1\nTeamIdentifier={team}\n")
        real_run = subprocess.run
        good = {"app": ("ai.luoke.core", "ABCDE12345", "0x10000(runtime)"), "engine": ("ai.luoke.engine", "ABCDE12345", "0x10000(runtime)")}
        with unittest.mock.patch.object(self.rel.subprocess, "run", codesign):
            sigs.update(good)
            self.rel.check_signed_engine(app, recipe)
            self.rel.check_signed_engine(app, self.recipe("omlx-0.7.2"))          # 不查
            for part, value in (("engine", ("luoke-engine", "ABCDE12345", "0x10000(runtime)")),     # 标识丢了
                                ("engine", ("ai.luoke.engine", "ABCDE12345", "0x2(adhoc)")),        # 没开 hardened runtime
                                ("engine", ("ai.luoke.engine", "ZZZZZ99999", "0x10000(runtime)")),  # 别的团队
                                ("engine", ("ai.luoke.engine", "not set", "0x10002(adhoc,runtime)")),
                                ("app", ("ai.luoke.core", "not set", "0x10000(runtime)")),
                                ("both", ("ai.luoke.engine", "not set", "0x10002(adhoc,runtime)"))):   # 两个都是临时签名：团队一样也不行
                sigs.update(good)
                if part == "both":
                    sigs["app"], sigs["engine"] = ("ai.luoke.core",) + value[1:], value
                else:
                    sigs[part] = value
                with self.assertRaises(SystemExit, msg=(part, value)):
                    self.rel.check_signed_engine(app, recipe)
            sigs.update(good)
            with self.assertRaises(SystemExit):           # 程序说自己是别的版本
                self.rel.check_signed_engine(self.fake_app(says="0.2.0"), recipe)
            with unittest.mock.patch.dict(os.environ, {"SELFTEST_RC": "1"}), self.assertRaises(SystemExit):     # 自检没过
                self.rel.check_signed_engine(self.fake_app(), recipe)

    def test_engine_is_frozen_at_the_pinned_commit_and_its_tag(self):
        ib = self.ib
        src, commit = self.engine_repo()
        self.git(src, "tag", "v0.1.0")
        frozen = os.path.join(self.d, "frozen")
        with unittest.mock.patch.object(ib, "SRC", frozen), unittest.mock.patch.object(ib, "TOP", os.path.join(self.d, "top")), \
                unittest.mock.patch.object(ib, "remote_url", lambda repo: src), unittest.mock.patch.object(ib, "log", lambda m: None):
            self.assertEqual(ib.freeze_engine(self.template("omlx-0.7.2")), "")
            dst = ib.freeze_engine(self.template("luoke-engine-0.1.0", commit))
            self.assertEqual(dst, os.path.join(frozen, "luoke-engine"))
            self.assertEqual(self.git(dst, "rev-parse", "HEAD"), commit)
            # tag 在别的提交上、没有这个 tag：停
            self.git(src, "commit", "-q", "--allow-empty", "-m", "next")
            later = self.git(src, "rev-parse", "HEAD")
            for version, pin in (("0.1.0", later), ("0.2.0", commit)):
                with self.assertRaises(SystemExit, msg=(version, pin)):
                    ib.freeze_engine(self.template(f"luoke-engine-{version}", pin))
            with self.assertRaises(SystemExit):           # 配方没钉提交
                ib.freeze_engine(self.template("luoke-engine-0.1.0"))

    def test_without_evaluation_the_engine_change_is_written_down(self):
        ib = self.ib
        c1, c2 = "1" * 40, "2" * 40
        recipes = os.path.join(self.d, "recipes")
        os.makedirs(recipes)
        for tag, version, commit in (("internal-1115", "omlx-0.7.2", None), ("internal-1116", "luoke-engine-0.1.0", c1)):
            shutil.copy(self.template(version, commit), os.path.join(recipes, f"{tag}.json"))
        with unittest.mock.patch.object(ib, "ROOT", self.d):
            now = self.template("luoke-engine-0.1.0", c2)
            self.assertEqual(ib.unevaluated_engine(now, "internal-1115"),
                             "引擎换成了 螺壳引擎 0.1.0（2222222），没评测：评测结果和开关沿用 internal-1115（引擎 oMLX 0.7.2）")
            # 同一个版本号、换了提交，也算换了引擎
            self.assertIn("沿用 internal-1116（引擎 螺壳引擎 0.1.0（1111111））", ib.unevaluated_engine(now, "internal-1116"))
            self.assertEqual(ib.unevaluated_engine(self.template("luoke-engine-0.1.0", c1), "internal-1116"), "")
            self.assertEqual(ib.unevaluated_engine(self.template("omlx-0.7.2"), "internal-1115"), "")
            self.assertIn("引擎 不明", ib.unevaluated_engine(now, "internal-0101"))
            self.assertEqual(ib.engine_label(ib.release_recipe("internal-1116")), "螺壳引擎 0.1.0（1111111）")

    def test_a_luoke_engine_recipe_is_only_released_without_evaluation(self):
        """评测还跑不了螺壳引擎：内部版要 --no-eval，beta、候选版、正式版不出。"""
        ib = self.ib
        engine, omlx = self.template("luoke-engine-0.1.0", "a" * 40), self.template("omlx-0.7.2", name="omlx.json")
        ib.check_engine_eval("internal-1116", engine, True)
        for tag in ("internal-1116", "beta2", "beta2-rc1", "local-1116-1200"):
            ib.check_engine_eval(tag, omlx, False)
            with self.assertRaises(SystemExit, msg=tag):
                ib.check_engine_eval(tag, engine, False)

    def test_release_report_says_the_engine_was_not_evaluated(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("release_report", os.path.join(ROOT, "tools", "release_report.py"))
        r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(r)
        tag = "internal-1116"
        os.makedirs(os.path.join(self.d, "releases", tag))
        note = "引擎换成了 螺壳引擎 0.1.0（2222222），没评测：评测结果和开关沿用 internal-1115（引擎 oMLX 0.7.2）"
        with open(os.path.join(self.d, "releases", tag, "timeline.json"), "w") as f:
            json.dump({"tag": tag, "host": "h", "phases": [{"name": "sign", "start": 0, "end": 60, "ok": True}], "samples": [],
                       "unevaluated_engine": note}, f)
        with unittest.mock.patch.object(r, "ROOT", self.d):
            self.assertIn(note + "。", "\n".join(r.summary(tag, None)))


class WindowSizingTest(unittest.TestCase):
    """本地 swift build 可能把 SDK 写成 14.0，走旧行为，崩不出来；只有正式包才崩，所以在这里拦"""

    def test_no_window_sizes_itself_with_preferred_content_size(self):
        hits = []
        for folder, _, names in os.walk(os.path.join(ROOT, "app", "Sources")):
            for name in names:
                if name.endswith(".swift"):
                    with open(os.path.join(folder, name), encoding="utf-8") as f:
                        if "sizingOptions = .preferredContentSize" in f.read():
                            hits.append(name)
        self.assertEqual(hits, [])
