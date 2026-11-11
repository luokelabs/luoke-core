"""主控起包外程序时的 disclaim 跳板（luoke_core/spawn.py）。只在临时目录里搭假的 .app，不起程序、不碰本机用户的数据。"""
import os
import plistlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from luoke_core import procs, spawn  # noqa: E402


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="luoke-spawn-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = os.path.join(self.dir, "螺壳.app")
        os.makedirs(os.path.join(self.app, "Contents", "MacOS"))
        os.makedirs(os.path.join(self.app, "Contents", "Resources", "python", "bin"))
        self.python = os.path.join(self.app, "Contents", "Resources", "python", "bin", "python3")
        open(self.python, "w").close()
        self.exe = os.path.join(self.app, "Contents", "MacOS", "LuokeCore")
        with open(self.exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.exe, 0o755)
        with open(os.path.join(self.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleExecutable": "LuokeCore"}, f)

    def test_bundled_python_goes_through_the_app(self):
        argv = spawn.disclaimed(["/usr/bin/true", "a"], executable=self.python)
        self.assertEqual(argv, [os.path.realpath(self.exe), spawn.FLAG, "/usr/bin/true", "a"])

    def test_other_python_runs_directly(self):
        self.assertIsNone(spawn.trampoline("/usr/bin/python3"))
        self.assertEqual(spawn.disclaimed(["/usr/bin/true"], executable="/usr/bin/python3"), ["/usr/bin/true"])

    def test_env_override_is_not_a_trampoline(self):
        os.environ["LUOKE_HUB_APP"] = self.app       # 测试、开发用的指向不算：旧版本体不认这个开关
        self.addCleanup(os.environ.pop, "LUOKE_HUB_APP", None)
        self.assertIsNone(spawn.trampoline("/usr/bin/python3"))

    def test_broken_app_runs_directly(self):
        with open(os.path.join(self.app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleExecutable": "../evil"}, f)
        self.assertIsNone(spawn.trampoline(self.python))
        os.remove(os.path.join(self.app, "Contents", "Info.plist"))
        self.assertIsNone(spawn.trampoline(self.python))

    def test_needs_absolute_program(self):
        with self.assertRaises(ValueError):
            spawn.disclaimed(["true"], executable=self.python)
        with self.assertRaises(ValueError):
            spawn.disclaimed([], executable=self.python)


class EnginePidfileTest(unittest.TestCase):
    """经跳板起的引擎：pidfile 要记换完以后的程序，不然下次回收孤儿引擎时对不上（review on #135）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="luoke-spawn-engine-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fake = os.path.join(self.dir, "LuokeCore")      # 假跳板：慢一点再换成要起的程序
        with open(self.fake, "w") as f:
            f.write('#!/bin/sh\nsleep "${SLOW:-0.4}"\nshift\nexec "$@"\n')
        os.chmod(self.fake, 0o755)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()

    def start(self, slow="0.4"):
        with unittest.mock.patch.object(spawn, "trampoline", return_value=self.fake):
            argv = spawn.disclaimed(["/bin/sleep", "30"])
        self.assertEqual(argv[:2], [self.fake, spawn.FLAG])
        p = subprocess.Popen(argv, env={"SLOW": slow, "PATH": "/usr/bin:/bin"})
        self.procs.append(p)
        return p

    def test_pidfile_records_the_program_not_the_trampoline(self):
        from luoke_core.engine import Engine
        e = Engine("/nonexistent.app", self.dir, self.dir, 10)
        e.proc = self.start()
        e._write_pidfile()
        with open(e.pidfile) as f:
            rec = json.load(f)
        self.assertEqual(rec["pid"], e.proc.pid)
        self.assertEqual(rec["exe"], "/bin/sleep")
        # 主控崩了、重起：新的 Engine 认得出上次的引擎，结束它
        again = Engine("/nonexistent.app", self.dir, self.dir, 10)
        again._reap_orphan()
        self.assertIsNotNone(e.proc.wait(5))

    def test_wait_exec_waits_through_a_transient_unreadable_read(self):
        """exec 到一半时命令行和启动时刻可能都短暂读不到：不能当成进程没了，要等到换完（#153 审查里 test_spawn 偶发挂的一种可能）。"""
        tramp = ["/bin/sh", self.fake, spawn.FLAG, "/bin/sleep", "30"]
        seq = iter([tramp, [], tramp, ["/bin/sleep", "30"]])
        start = iter([None, 1.0, 1.0])
        with unittest.mock.patch.object(procs, "argv", lambda pid: next(seq)), \
                unittest.mock.patch.object(procs, "start_time", lambda pid: next(start)), \
                unittest.mock.patch.object(spawn.time, "sleep", lambda s: None):
            self.assertTrue(spawn.wait_exec(1))
            self.assertEqual(list(seq), [])          # 读到换完以后的命令行才返回，不是在读不到的那一次
        # 真的没了：连着几次都读不到，照旧算完，不等到超时
        with unittest.mock.patch.object(procs, "argv", lambda pid: []), \
                unittest.mock.patch.object(procs, "start_time", lambda pid: None):
            self.assertTrue(spawn.wait_exec(1, timeout=5))

    def test_wait_exec_gives_up(self):
        p = self.start(slow="5")
        self.assertFalse(spawn.wait_exec(p.pid, timeout=0.2))
        self.assertIn(spawn.FLAG, procs.argv(p.pid))


if __name__ == "__main__":
    unittest.main()
