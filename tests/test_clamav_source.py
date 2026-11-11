"""tools/clamav_source.py 的测试：只用临时目录，下载用假的，不碰网络和 GitHub。
  python3 -m unittest tests.test_clamav_source"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import clamav_source as cs  # noqa: E402

TARBALL = b"clamav source"
PATCHES = [b"patch one", b"patch two"]
FORMULA = """class Clamav < Formula
  desc "Anti-virus software"
  revision 2
  head "https://github.com/Cisco-Talos/clamav.git", branch: "main"

  stable do
    url "https://github.com/Cisco-Talos/clamav/releases/download/clamav-1.5.4/clamav-1.5.4.tar.gz"
    mirror "https://www.clamav.net/downloads/production/clamav-1.5.4.tar.gz"
    sha256 "%s"

    # Backport support for OpenSSL 4
    patch do
      url "https://github.com/Cisco-Talos/clamav/commit/0097a6f99d9f6b78381e12b677a38fb62242c41a.patch?full_index=1"
      sha256 "%s"
      type :backport
      resolves "https://github.com/Cisco-Talos/clamav/pull/1731"
    end

    patch do
      url "https://github.com/Cisco-Talos/clamav/commit/729eaef96263970dede0808426374bb0597dae90.patch?full_index=1"
      sha256 "%s"
    end
  end

  bottle do
    sha256 arm64_tahoe: "61b43dc3270c1cadd015362c439a3ecbb0010f7305af7dfa955abdc219a34e21"
  end
end
""" % tuple(hashlib.sha256(b).hexdigest() for b in [TARBALL] + PATCHES)
P1 = "clamav-1.5.4_2-patch1-0097a6f99d9f.patch"
P2 = "clamav-1.5.4_2-patch2-729eaef96263.patch"
BODY = {"clamav-1.5.4.tar.gz": TARBALL, "0097a6f99d9f6b78381e12b677a38fb62242c41a.patch": PATCHES[0],
        "729eaef96263970dede0808426374bb0597dae90.patch": PATCHES[1]}


def serve(calls, broken=()):
    """假的下载：照 url 的文件名给内容；broken 里的给错的内容。"""
    def fetch(url, dst):
        calls.append(url)
        name = url.split("?")[0].rsplit("/", 1)[-1]
        with open(dst, "wb") as f:
            f.write(b"something else" if name in broken else BODY[name])
    return fetch


def release(tag, when, *names, draft=False):
    return {"tag_name": tag, "html_url": f"https://github.com/o/r/releases/tag/{tag}", "published_at": when, "draft": draft,
            "assets": [{"name": n, "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{n}"} for n in names]}


class ClamavSourceTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def app(self, tools):
        app = os.path.join(self.d, "scan.app")
        os.makedirs(os.path.join(app, "Contents", "Resources"), exist_ok=True)
        with open(os.path.join(app, "Contents", "Resources", "bundled-tools.json"), "w") as f:
            json.dump(tools, f)
        return app

    def keg(self, version="1.5.4_2", text=FORMULA):
        tools = os.path.join(self.d, "brew")
        brew = os.path.join(tools, "Cellar", "clamav", version, ".brew")
        os.makedirs(brew, exist_ok=True)
        with open(os.path.join(brew, "clamav.rb"), "w") as f:
            f.write(text)
        return tools

    def test_version_comes_from_the_bundled_tools_list(self):
        app = self.app([{"file": "Contents/MacOS/ai.luoke.scan.clamscan", "formula": "clamav", "version": "1.5.4_2"},
                        {"file": "Contents/Frameworks/libclamav.12.dylib", "formula": "clamav", "version": "1.5.4_2"},
                        {"file": "Contents/MacOS/ai.luoke.scan.yara", "formula": "yara", "version": "4.6.0"}])
        self.assertEqual(cs.clamav_version(app), "1.5.4_2")
        self.assertIsNone(cs.clamav_version(self.app([{"file": "x", "formula": "yara", "version": "4.6.0"}])))
        with self.assertRaises(cs.SourceError):      # 两个版本混在一个包里：说不清附哪一份
            cs.clamav_version(self.app([{"formula": "clamav", "version": "1.5.4_2"}, {"formula": "clamav", "version": "1.5.3"}]))
        with self.assertRaises(cs.SourceError):      # 清单不是一列文件
            cs.clamav_version(self.app({"formula": "clamav"}))
        for where in (("luoke-scan", "Resources", "scan"), ("螺壳.app", "Contents", "Resources", "scan")):   # 0.3.0 起打进螺壳.app
            d = os.path.join(self.d, *where)
            os.makedirs(d)
            with open(os.path.join(d, "bundled-tools.json"), "w", encoding="utf-8") as f:
                json.dump([{"file": "Helpers/clamscan", "formula": "clamav", "version": "1.5.4_3"}], f)
            self.assertEqual(cs.clamav_version(os.path.join(self.d, where[0])), "1.5.4_3")
            shutil.rmtree(os.path.join(self.d, where[0]))
        shutil.rmtree(os.path.join(self.d, "scan.app"))
        with self.assertRaises(cs.SourceError):      # 包里没有清单：不能当成不带 ClamAV
            cs.clamav_version(os.path.join(self.d, "scan.app"))

    def test_formula_gives_the_stable_tarball_not_head_or_a_patch(self):
        url, sha = cs.formula_source(FORMULA)
        self.assertTrue(url.endswith("/clamav-1.5.4.tar.gz"))
        self.assertEqual(sha, hashlib.sha256(TARBALL).hexdigest())
        self.assertEqual(cs.asset_names("1.5.4_2"), ("clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb"))
        head = FORMULA.replace('  head "https://github.com/Cisco-Talos/clamav.git", branch: "main"\n',
                               '  head do\n    url "https://github.com/Cisco-Talos/clamav.git", branch: "main"\n  end\n')
        self.assertEqual(cs.formula_source(head), (url, sha))                       # head 写成一块时也不取它的 url

    def test_formula_patches_in_order_and_odd_ones_stop(self):
        patches = cs.formula_patches(FORMULA)
        self.assertEqual([s for _, s in patches], [hashlib.sha256(b).hexdigest() for b in PATCHES])
        self.assertEqual([cs.patch_name("1.5.4_2", n, u) for n, (u, _) in enumerate(patches, 1)], [P1, P2])
        head = FORMULA.replace('  head "https://github.com/Cisco-Talos/clamav.git", branch: "main"\n',
                               '  head do\n    url "https://github.com/Cisco-Talos/clamav.git"\n    patch do\n'
                               '      url "https://example.invalid/head.patch"\n      sha256 "%s"\n    end\n  end\n' % ("0" * 64))
        self.assertEqual(len(cs.formula_patches(head)), 2)                       # head 那块的补丁不算
        self.assertEqual(len(cs.formula_patches(FORMULA.replace("  end\n\n  bottle", "    patch :DATA\n  end\n\n  bottle"))), 2)
        for odd in ('    patch :p0, "inline"\n', '    resource "x" do\n      url "https://example.invalid/x.tar.gz"\n    end\n'):
            with self.assertRaises(cs.SourceError):
                cs.formula_patches(FORMULA.replace("  end\n\n  bottle", odd + "  end\n\n  bottle"))

    def test_source_is_downloaded_once_and_checked_every_time(self):
        tools, cache, calls = self.keg(), os.path.join(self.d, "cache"), []
        fetch = serve(calls)
        files = cs.ensure_source("1.5.4_2", tools, cache, fetch)
        self.assertEqual([os.path.basename(f) for f in files], ["clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb", P1, P2])
        self.assertEqual(open(files[1]).read(), FORMULA)
        self.assertEqual(open(files[3], "rb").read(), PATCHES[1])
        cs.ensure_source("1.5.4_2", tools, cache, fetch)
        self.assertEqual(len(calls), 3)               # 留着的那份对得上就不再下
        for f in (files[0], files[2]):                 # 被改过：重新下
            with open(f, "wb") as out:
                out.write(b"tampered")
        cs.ensure_source("1.5.4_2", tools, cache, fetch)
        self.assertEqual(len(calls), 5)
        self.assertEqual(open(files[2], "rb").read(), PATCHES[0])
        changed = FORMULA.replace('  desc "Anti-virus software"', '  desc "Anti-virus software (reinstalled)"')
        self.keg(text=changed)                          # 同一个版本重装、配方变了：留着的配方换成 keg 里这一份
        cs.ensure_source("1.5.4_2", tools, cache, fetch)
        with open(files[1]) as f:
            self.assertEqual(f.read(), changed)

    def test_wrong_download_or_missing_formula_stops(self):
        cache = os.path.join(self.d, "cache")

        bad = serve([], broken=("clamav-1.5.4.tar.gz",))
        with self.assertRaises(cs.SourceError):
            cs.ensure_source("1.5.4_2", self.keg(), cache, bad)
        self.assertEqual(sorted(os.listdir(os.path.join(cache, "1.5.4_2"))), ["clamav-1.5.4_2.rb"])   # 下错的不留
        with self.assertRaises(cs.SourceError):      # 补丁对不上和源码包对不上一样：停
            cs.ensure_source("1.5.4_2", self.keg(), cache, serve([], broken=("729eaef96263970dede0808426374bb0597dae90.patch",)))
        self.assertNotIn(P2, os.listdir(os.path.join(cache, "1.5.4_2")))
        with self.assertRaises(cs.SourceError):      # keg 里没有配方
            cs.ensure_source("1.5.5", self.keg(), cache, bad)

    def test_first_release_with_both_files_is_where_a_version_lives(self):
        found = cs.published([
            release("beta3", "2026-12-01T00:00:00Z", "Luoke.dmg", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb"),
            release("beta2", "2026-11-06T00:00:00Z", "Luoke.dmg", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb"),
            release("beta2.1", "2026-11-10T00:00:00Z", "clamav-1.5.5_1.rb"),                 # 只有配方，不算
            release("beta4", "2026-11-02T00:00:00Z", "clamav-1.5.6.tar.gz", "clamav-1.5.6.rb", draft=True)])
        self.assertEqual(list(found), ["1.5.4_2"])
        self.assertEqual(found["1.5.4_2"]["tag"], "beta2")
        self.assertTrue(found["1.5.4_2"]["tarball"].endswith("/beta2/clamav-1.5.4.tar.gz"))
        # 要求补丁都在：beta2 缺补丁，补丁齐的 beta3 才算；补丁照序号排
        need = {"1.5.4_2": ["clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb", P1, P2]}
        found = cs.published([
            release("beta2", "2026-11-06T00:00:00Z", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb", P1),
            release("beta3", "2026-12-01T00:00:00Z", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb", P2, P1)], need)
        self.assertEqual(found["1.5.4_2"]["tag"], "beta3")
        self.assertEqual([n for n, _ in found["1.5.4_2"]["patches"]], [P1, P2])
        # 换了 ClamAV 版本以后（不再要求 1.5.4_2 的补丁），旧版本那一行照样指补丁附全了的 beta3，不退回缺补丁的 beta2
        found = cs.published([
            release("beta2", "2026-11-06T00:00:00Z", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb", P1),
            release("beta3", "2026-12-01T00:00:00Z", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb", P2, P1),
            release("beta4", "2026-12-20T00:00:00Z", "clamav-1.5.4.tar.gz", "clamav-1.5.4_2.rb", P1, P2)])
        self.assertEqual(found["1.5.4_2"]["tag"], "beta3")                         # 一样全的取最早的

    def test_index_and_notes(self):
        found = {"1.5.4_2": cs.planned("luokelabs/luoke-releases", "beta2", "1.5.4_2", (P1, P2))}
        text = cs.render_index(found)
        self.assertIn("| 1.5.4_2 | [beta2](https://github.com/luokelabs/luoke-releases/releases/tag/beta2) |", text)
        self.assertIn("releases/download/beta2/clamav-1.5.4.tar.gz", text)
        self.assertIn(f"[{P1}](https://github.com/luokelabs/luoke-releases/releases/download/beta2/{P1})<br>[{P2}]", text)
        self.assertIn("不会删", text)
        cs.render_index({"1.5.4-rc_1": found["1.5.4_2"], "1.5.4_2": found["1.5.4_2"]})   # 版本号里有字母也能排
        self.assertIn("附在本页", cs.notes_line("1.5.4_2", True, None))
        self.assertIn("2 个补丁", cs.notes_line("1.5.4_2", True, None, 2))
        line = cs.notes_line("1.5.4_2", False, found["1.5.4_2"])
        self.assertIn("beta2", line)
        self.assertIn(cs.INDEX_URL, line)


if __name__ == "__main__":
    unittest.main()
