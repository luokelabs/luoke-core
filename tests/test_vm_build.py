"""tools/vm_build.py 宿主机这一侧：开关、放进 VM 的东西、起停 VM、收回产物。lume 用假的（一个 shell 脚本，VM 的「运行」就是直接写 out/）。
    python3 -m unittest tests.test_vm_build"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import vm_build as V  # noqa: E402

FAKE_LUME = r"""#!/bin/bash
F=$FAKE; cmd=$1; shift
case $cmd in
  ls) printf '['; sep=""; for s in $F/state.*; do [ -e "$s" ] || continue; n=${s##*/state.}
        printf '%s{"name":"%s","status":"%s"}' "$sep" "$n" "$(cat $s)"; sep=,; done; echo ']';;
  clone) echo stopped > $F/state.$2; echo "clone $1 $2" >> $F/calls;;
  set) echo "set $*" >> $F/calls;;
  run) n=$1; echo running > $F/state.$n
       for a in "$@"; do case $a in *:ro) job=${a%:ro};; */out) out=$a;; esac; done
       echo "run $n holds=$(ls $F/work/*.hold 2>/dev/null | wc -l | tr -d ' ')" >> $F/calls
       ls $job/src > $F/srcs
       [ -d $job/src/luoke-search/.git ] && echo "git leaked" >> $F/calls
       mkdir -p $out/stage && echo bin > $out/stage/luoke-search
       echo ${FAKE_RC:-0} > $out/rc; echo "guest log" > $out/run.log; echo stopped > $F/state.$n;;
  stop) echo "stop $1" >> $F/calls;;
  delete) rm -f $F/state.$1; echo "delete $1" >> $F/calls;;
esac
"""


class VMBuildTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        t = self.t
        os.makedirs(f"{t}/home/.lume/rel-image")
        open(f"{t}/home/.lume/rel-image/disk.img", "w").close()
        os.makedirs(f"{t}/work")
        os.makedirs(f"{t}/db")
        for n in ("daily-28170.cvd.sign", "daily.cvd", "main.cvd"):
            open(f"{t}/db/{n}", "w").close()
        with open(f"{t}/versions.json", "w") as f:       # 更新脚本用 sigtool 核过写的，和文件名里的可以不一样
            json.dump({"main": "63", "bytecode": "339", "daily": "28178"}, f)
        os.makedirs(f"{t}/acc/.git")
        os.makedirs(f"{t}/acc/scripts")
        with open(f"{t}/acc/.luoke-commit", "w") as f:
            f.write("a" * 40 + "\n")
        with open(f"{t}/lume", "w") as f:
            f.write(FAKE_LUME)
        os.chmod(f"{t}/lume", 0o755)
        self.lab = f"{t}/lab.json"
        self.write_lab({"enabled": True, "work_dir": f"{t}/work", "clamav_db": f"{t}/db"})
        self.saved = (V.LUME, dict(V.ENV), V.lab_config.PATH, os.environ.get("HOME"), V.time.sleep)
        V.LUME = f"{t}/lume"
        V.lab_config.PATH = self.lab          # lab_config 在导入时就定了路径
        V.ENV.update(FAKE=t, FAKE_RC="0")
        os.environ["HOME"] = f"{t}/home"
        V.time.sleep = lambda s: None

    def tearDown(self):
        V.LUME, env, V.lab_config.PATH, home, V.time.sleep = self.saved
        V.ENV.clear()
        V.ENV.update(env)
        if home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = home
        shutil.rmtree(self.t, ignore_errors=True)

    def write_lab(self, vm: dict):
        with open(self.lab, "w") as f:
            json.dump({"vm_build": vm}, f)

    def calls(self) -> str:
        with open(f"{self.t}/calls") as f:
            return f.read()

    def test_switch_defaults_off_and_internal_only(self):
        self.assertTrue(V.enabled("internal-1109-4"))
        self.assertTrue(V.enabled("local-1109-1200"))
        self.assertFalse(V.enabled("beta2"))
        self.assertFalse(V.enabled("beta2-rc1"))
        self.write_lab({"enabled": True, "for": ["internal-", "beta"]})
        self.assertTrue(V.enabled("beta2"))
        self.write_lab({})
        self.assertFalse(V.enabled("internal-1109"))
        os.remove(self.lab)
        self.assertFalse(V.enabled("internal-1109"))

    def test_clamav_version_from_versions_file(self):
        self.assertEqual(V.clamav_version(f"{self.t}/db"), "28178")
        self.assertEqual(V.clamav_version(f"{self.t}/db/"), "28178")
        os.remove(f"{self.t}/versions.json")
        self.assertEqual(V.clamav_version(f"{self.t}/db"), "")

    def test_image_version_changes_with_disk(self):
        v = V.image_version()
        self.assertTrue(v.startswith("rel-image@"))
        os.utime(f"{self.t}/home/.lume/rel-image/disk.img", (1, 1))
        self.assertEqual(V.image_version(), "rel-image@1")
        self.assertEqual(V.image_version("missing"), "")

    def test_build_copies_back_and_cleans_up(self):
        stage = f"{self.t}/stage"
        V.build_search(f"{self.t}/acc", stage)
        with open(f"{stage}/luoke-search") as f:
            self.assertEqual(f.read().strip(), "bin")
        c = self.calls()
        self.assertIn("holds=1", c)               # 编的时候挂着 hold，管家不起新的 CI VM
        self.assertNotIn("git leaked", c)          # 源码快照不带 .git
        self.assertIn("delete reljob-", c)
        self.assertEqual([f for f in os.listdir(f"{self.t}/work") if f.endswith(".hold")], [])
        self.assertEqual(V.vms(), [])

    def test_only_logs_kept_after_copy_back(self):
        V.build_search(f"{self.t}/acc", f"{self.t}/stage")
        (run,) = os.listdir(f"{self.t}/work/rel")
        self.assertEqual(sorted(os.listdir(f"{self.t}/work/rel/{run}/out")), ["job.json", "rc", "run.log"])
        self.assertFalse(os.path.exists(f"{self.t}/work/rel/{run}/job"))
        self.assertIn(f"-{os.getpid()}-", run)

    def test_leftovers_from_a_killed_run_are_cleaned(self):
        dead = 999999
        while V.alive(dead):
            dead += 1
        open(f"{self.t}/work/release-{dead}.hold", "w").close()
        open(f"{self.t}/work/release-{os.getpid()}.hold.keep", "w").close()
        with open(f"{self.t}/state.reljob-core-{dead}-120000", "w") as f:
            f.write("running")
        with open(f"{self.t}/state.cijob-1109-120000-0", "w") as f:
            f.write("running")
        os.makedirs(f"{self.t}/work/rel")
        for i in range(V.KEEP_RUNS + 3):
            os.makedirs(f"{self.t}/work/rel/old{i:02d}")
            os.utime(f"{self.t}/work/rel/old{i:02d}", (i, i))
        V.build_search(f"{self.t}/acc", f"{self.t}/stage")
        self.assertFalse(os.path.exists(f"{self.t}/work/release-{dead}.hold"))
        self.assertIn(f"delete reljob-core-{dead}-120000", self.calls())
        self.assertNotIn("delete cijob", self.calls())           # CI 的 VM 不碰
        runs = os.listdir(f"{self.t}/work/rel")
        self.assertLessEqual(len(runs), V.KEEP_RUNS + 1)
        self.assertNotIn("old00", runs)

    def test_tool_failures_are_in_errors(self):
        for e in (subprocess.CalledProcessError(1, "rsync"), subprocess.TimeoutExpired("lume", 600), FileNotFoundError("x")):
            self.assertIsInstance(e, V.ERRORS)

    def test_failure_raises_and_still_cleans_up(self):
        V.ENV["FAKE_RC"] = "3"
        with self.assertRaises(V.Stop) as e:
            V.build_search(f"{self.t}/acc", f"{self.t}/stage")
        self.assertIn("退出码 3", str(e.exception))
        self.assertIn("delete reljob-", self.calls())
        self.assertEqual([f for f in os.listdir(f"{self.t}/work") if f.endswith(".hold")], [])

    def test_no_image_stops_before_cloning(self):
        shutil.rmtree(f"{self.t}/home/.lume/rel-image")
        with self.assertRaises(V.Stop):
            V.build_search(f"{self.t}/acc", f"{self.t}/stage")
        self.assertFalse(os.path.exists(f"{self.t}/calls"))

    def test_scan_needs_virus_db(self):
        os.remove(f"{self.t}/versions.json")
        with self.assertRaises(V.Stop) as e:
            V.build_scan(f"{self.t}/acc", f"{self.t}/stage")
        self.assertIn("病毒库", str(e.exception))

    def test_waits_when_two_vms_running(self):
        for n in ("cijob-a", "cijob-b"):
            with open(f"{self.t}/state.{n}", "w") as f:
                f.write("running")
        with self.assertRaises(V.Stop) as e:
            V.wait_for_slot(max_wait=0)
        self.assertIn("2 台", str(e.exception))
        os.remove(f"{self.t}/state.cijob-b")
        V.wait_for_slot(max_wait=0)

    def test_core_is_git_archive_of_head(self):
        repo = f"{self.t}/core"
        os.makedirs(repo)
        run = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)  # noqa: E731
        run("init", "-q")
        with open(f"{repo}/tracked", "w") as f:
            f.write("x")
        run("add", "tracked")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
        open(f"{repo}/untracked", "w").close()
        job = V.Job("t")
        job.core(repo)
        self.assertEqual(sorted(os.listdir(f"{job.job}/src/luoke-core")), ["tracked"])
        self.assertEqual(len(job.record["sources"]["luoke-core"]), 40)


if __name__ == "__main__":
    unittest.main()
