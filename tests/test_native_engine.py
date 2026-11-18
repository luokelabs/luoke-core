"""引擎包是螺壳引擎（luoke-engine）时：引擎和三个工作进程起的都是那一个可执行文件。"""
import os
import tempfile
import unittest

from luoke_core.engine import Engine, engine_package, is_engine_version, native_engine
from luoke_core.scorer import EmbedWorker, ScoreWorker, TranscribeWorker


def package(root: str, name: str, binary: str) -> str:
    app = os.path.join(root, name)
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    open(os.path.join(app, "Contents", "MacOS", binary), "w").close()
    return app


class NativeEngineTest(unittest.TestCase):
    def test_native_package_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            native = package(d, "LuokeEngine.app", "luoke-engine")
            omlx = package(d, "oMLX.app", "omlx-cli")
            self.assertEqual(native_engine(native), os.path.join(native, "Contents", "MacOS", "luoke-engine"))
            self.assertIsNone(native_engine(omlx))
            self.assertIsNone(native_engine(os.path.join(d, "missing.app")))
            self.assertEqual(Engine(native, os.path.join(d, "b"), d, 8).cli, os.path.join(native, "Contents", "MacOS", "luoke-engine"))
            self.assertEqual(Engine(omlx, os.path.join(d, "b"), d, 8).cli, os.path.join(omlx, "Contents/MacOS/omlx-cli"))

    def test_recipe_version_names_the_package(self):
        self.assertEqual(engine_package("luoke-engine-0.1.0"),
                         {"app": "LuokeEngine.app", "bundle_id": "ai.luoke.engine", "name": "螺壳引擎", "max_body": 8 << 20,
                          "version": "0.1.0"})
        self.assertEqual(engine_package("omlx-0.7.2"), {"app": "oMLX.app", "bundle_id": "app.omlx", "name": "oMLX", "max_body": 32 << 20,
                                                           "version": "0.7.2"})
        self.assertEqual(engine_package("0.7.0")["app"], "oMLX.app")          # 早期没有前缀的配方
        self.assertTrue(is_engine_version("omlx-0.7.0") and is_engine_version("luoke-engine-0.1.0"))
        self.assertFalse(is_engine_version("notes"))

    def test_workers_start_the_engine_subcommands(self):
        exe = "/x/LuokeEngine.app/Contents/MacOS/luoke-engine"
        self.assertEqual(ScoreWorker("", "", "/m/score", 16, cache_mb=1024, native=exe)._argv(),
                         [exe, "score-worker", "/m/score", "--snapshots", "16", "--cache-mb", "1024"])
        self.assertEqual(EmbedWorker("", "", "/m/embed", cache_mb=512, native=exe)._argv(), [exe, "embed-worker", "/m/embed", "--cache-mb", "512"])
        self.assertEqual(EmbedWorker("", "", "/m/embed", cache_mb=512, media=True, native=exe)._argv(),
                         [exe, "embed-worker", "/m/embed", "--cache-mb", "512", "--media"])
        self.assertEqual(TranscribeWorker("", "", "/m/asr", cache_mb=512, native=exe)._argv(), [exe, "transcribe-worker", "/m/asr", "--cache-mb", "512"])

    def test_python_workers_are_unchanged_without_it(self):
        argv = ScoreWorker("/p/python3", "/p/site", "/m/score", 8, cache_mb=256)._argv()
        self.assertEqual(argv[:2], ["/p/python3", "-B"])
        self.assertTrue(argv[2].endswith("score_worker.py"))
        self.assertEqual(argv[3:], ["/m/score", "--snapshots", "8", "--cache-mb", "256"])
        self.assertTrue(EmbedWorker("/p/python3", "/p/site", "/m/e", media=True)._argv()[-1] == "--media")
        self.assertTrue(TranscribeWorker("/p/python3", "/p/site", "/m/a")._argv()[2].endswith("transcribe_worker.py"))


if __name__ == "__main__":
    unittest.main()
