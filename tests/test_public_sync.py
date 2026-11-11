"""tools/public_sync.py 的测试：只用临时目录里的仓库（公开仓库、私有仓库都是本地的裸仓库），不碰任何远端。
  python3 -m unittest tests.test_public_sync"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import public_sync  # noqa: E402

ID = {"GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
      "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def sh(cwd, *args, env=None):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **ID, **(env or {})})
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)


def commit(repo, msg, env=None):
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", msg, env=env)
    return sh(repo, "rev-parse", "HEAD")


HOME = "/" + "Users"      # 拼起来写：这个文件随公开代码导出，不能自己命中 public_tree 的词表
LUOKE = {"GIT_AUTHOR_NAME": "Luoke Labs", "GIT_AUTHOR_EMAIL": "zhixian@luoke.ai", "GIT_COMMITTER_NAME": "Luoke Labs", "GIT_COMMITTER_EMAIL": "zhixian@luoke.ai"}
GITHUB = {"GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}
IP = "100." + "101.102.103"      # 内网地址，拼起来写（同上）
# 仓库自己的导出工具（core 的 tools/public_tree.py 那样）：去掉 internal/ 和它自己，查内网地址
MINI_PT = '''import re
LEAK = re.compile(r"\\b100\\.\\d+\\.\\d+\\.\\d+")
def public(files):
    return {p: v for p, v in files.items() if not p.startswith(("internal/", "tools/public_tree.py")) and not p.endswith("CLAUDE.md")}
def check(files):
    return [(p, n, l) for p in sorted(files) for n, l in enumerate(files[p][0].decode(errors="replace").splitlines(), 1) if LEAK.search(l)], []
'''


class PublicSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pub = os.path.join(self.tmp, "luoke-demo.git")
        self.priv = os.path.join(self.tmp, "luoke-demo-priv" "ate.git")
        for bare in (self.pub, self.priv):
            sh(self.tmp, "init", "-q", "--bare", "-b", "main", bare)
        # 私有仓库：main 是团队的历史（和公开仓库没有共同祖先）
        self.work = os.path.join(self.tmp, "work")
        sh(self.tmp, "init", "-q", "-b", "main", self.work)
        sh(self.work, "remote", "add", "origin", self.priv)
        write(self.work, "README.md", "demo\n")
        write(self.work, "pkg/a.py", "x = 1\n")
        write(self.work, "CLAUDE.md", "session rules\n")
        self.team = commit(self.work, "team history")
        sh(self.work, "push", "-q", "origin", "main")
        # 公开仓库：一个导出提交（作者 Luoke Labs，尾注写着它导出自的私有提交）
        self.initial = self.seed_public(f"initial public release\n\nExported-From: {self.team}")
        self.patterns = os.path.join(self.tmp, "leak-patterns")
        write(self.tmp, "leak-patterns", "# names\n\\bsomehost\\b\n")

    def seed_public(self, msg):
        """公开 main 换成一个提交（内容是私有 team history 的导出）。"""
        seed = tempfile.mkdtemp(dir=self.tmp)
        sh(seed, "init", "-q", "-b", "main")
        write(seed, "README.md", "demo\n")
        write(seed, "pkg/a.py", "x = 1\n")
        sha = commit(seed, msg, LUOKE)
        sh(seed, "push", "-q", "-f", self.pub, "main")
        return sha

    def run_cmd(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = public_sync.main([args[0], "--repo", self.work, "--patterns", self.patterns,
                                   "--public-url", self.pub, "--public-push-url", self.pub, *args[1:]])
        return rc, out.getvalue() + err.getvalue()

    def head(self, bare, ref):
        return sh(self.tmp, "--git-dir", bare, "rev-parse", ref)

    def init_public(self):
        rc, out = self.run_cmd("init")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh(self.work, "rev-parse", "public"), self.initial)        # 提交号和公开仓库一样
        sh(self.work, "push", "-q", "origin", "public")

    def test_init_then_sync_pushes_public_first_with_luoke_identity(self):
        rc, out = self.run_cmd("sync")
        self.assertEqual(rc, 1)
        self.assertIn("先跑 init", out)
        self.init_public()
        write(self.work, "pkg/a.py", "x = 2\n")
        commit(self.work, "change")
        rc, out = self.run_cmd("sync", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh(self.work, "rev-parse", "public"), self.initial)        # --dry-run 不写分支
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        new = self.head(self.pub, "main")
        self.assertEqual(new, self.head(self.priv, "public"))                      # 两边同一个提交
        self.assertEqual(sh(self.work, "rev-parse", f"{new}^"), self.initial)     # 只快进
        self.assertEqual(sh(self.work, "show", "-s", "--format=%an <%ae> | %cn <%ce>", new),
                         "Luoke Labs <zhixian@luoke.ai> | Luoke Labs <zhixian@luoke.ai>")
        self.assertTrue(sh(self.work, "show", "-s", "--format=%B", new).endswith(f"Exported-From: {sh(self.work, 'rev-parse', 'main')}"))
        files = sh(self.work, "ls-tree", "-r", "--name-only", new).split()
        self.assertNotIn("CLAUDE.md", files)                                        # 会话文件不导出
        self.assertLess(out.index("公开 main →"), out.index("私有 public →"))      # 先公开，再私有
        rc, out = self.run_cmd("sync")
        self.assertIn("不用同步", out)

    def test_leaks_stop_before_any_commit(self):
        self.init_public()
        write(self.work, "pkg/b.py", "# measured on somehost\n")
        write(self.work, "docs/x.md", f"path {HOME}/realname/luoke\n")
        commit(self.work, "leaky")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 1)
        self.assertIn("本机词表第 2 行", out)
        self.assertIn("本机路径", out)
        self.assertEqual(self.head(self.pub, "main"), self.initial)
        write(self.work, "pkg/b.py", "# measured on the test machine\n")
        write(self.work, "docs/x.md", f"path {HOME}/me/luoke\n")                    # 文档里的示例路径不算
        commit(self.work, "clean")
        self.assertEqual(self.run_cmd("scan")[0], 0)

    def test_external_change_must_be_carried_back_first(self):
        self.init_public()
        # 公开仓库里合了一个外部 PR（提交者是 GitHub，不是导出）
        ext = os.path.join(self.tmp, "ext")
        sh(self.tmp, "clone", "-q", self.pub, ext)
        write(ext, "pkg/a.py", "x = 1\ny = 'from outside'\n")
        pr = commit(ext, "external fix", {"GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"})
        sh(ext, "push", "-q", "origin", "main")
        write(self.work, "README.md", "demo v2\n")
        commit(self.work, "private change")
        rc, out = self.run_cmd("sync")
        self.assertEqual(rc, 1)
        self.assertIn("还没搬回私有 main", out)
        self.assertIn("pkg/a.py", out)
        sh(self.work, "fetch", "-q", self.pub, "main")
        sh(self.work, "cherry-pick", "-x", pr)                                     # 流程 C：cherry-pick -x 搬回
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        new = self.head(self.pub, "main")
        self.assertEqual(sh(self.work, "rev-parse", f"{new}^"), pr)               # 接在公开 main（外部 PR）后面
        self.assertEqual(self.head(self.priv, "public"), new)                     # 私有 public 一起快进

    def test_private_public_ahead_of_public_main_stops(self):
        self.init_public()
        write(self.work, "pkg/a.py", "x = 3\n")
        commit(self.work, "change")
        self.assertEqual(self.run_cmd("sync")[0], 0)
        sh(self.work, "push", "-q", "origin", "public")                            # 只推了私有一边（顺序反了）
        write(self.work, "pkg/a.py", "x = 4\n")
        commit(self.work, "change 2")
        rc, out = self.run_cmd("sync")
        self.assertEqual(rc, 1)
        self.assertIn("新：上次只推了私有一边", out)

    def test_private_push_failure_leaves_public_pushed_and_next_sync_catches_up(self):
        self.init_public()
        write(self.work, "pkg/a.py", "x = 5\n")
        commit(self.work, "change")
        hook = os.path.join(self.priv, "hooks", "pre-receive")
        write(self.priv, "hooks/pre-receive", "#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        self.assertIn("私有 public 没推上", out)
        self.assertNotEqual(self.head(self.pub, "main"), self.head(self.priv, "public"))
        os.remove(hook)
        rc, out = self.run_cmd("sync", "--push")                                    # 树没变：不用新提交
        self.assertIn("不用同步", out)
        # 私有 public 落后于公开 main 时 sync 照常（第 1 步认得出是祖先），推的时候一起快进
        write(self.work, "pkg/a.py", "x = 6\n")
        commit(self.work, "change 2")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.head(self.pub, "main"), self.head(self.priv, "public"))

    def test_sync_then_push_pushes_the_commit_written_locally(self):
        self.init_public()
        write(self.work, "pkg/a.py", "x = 7\n")
        commit(self.work, "change")
        rc, out = self.run_cmd("sync")                                              # 先写本地 public，给人看
        self.assertEqual(rc, 0, out)
        written = sh(self.work, "rev-parse", "public")
        rc, out = self.run_cmd("sync", "--push")                                    # 问过以后加 --push 再跑一次
        self.assertEqual(rc, 0, out)
        self.assertIn("用本地 public 上次写下的", out)                              # 同一秒重建的提交号也一样，所以看输出
        self.assertEqual(self.head(self.pub, "main"), written)                     # 推的就是看过的那个提交
        self.assertEqual(self.head(self.priv, "public"), written)
        # 写了本地 public 没推，私有 main 又动了：本地那个过时了，换成新的
        write(self.work, "pkg/a.py", "x = 8\n")
        commit(self.work, "change 2")
        self.assertEqual(self.run_cmd("sync")[0], 0)
        stale = sh(self.work, "rev-parse", "public")
        write(self.work, "pkg/a.py", "x = 9\n")
        commit(self.work, "change 3")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        self.assertIn("过时", out)
        self.assertNotEqual(self.head(self.pub, "main"), stale)
        self.assertEqual(sh(self.work, "show", f"{self.head(self.pub, 'main')}:pkg/a.py"), "x = 9")
        # 本地 public 上是别的东西（不是导出提交），照样停
        write(self.work, "pkg/a.py", "x = 10\n")
        commit(self.work, "change 4")
        sh(self.work, "branch", "-f", "public", "main")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 1)
        self.assertIn("本地 public", out)

    def test_exporting_an_older_or_side_commit_stops(self):
        self.init_public()
        old = sh(self.work, "rev-parse", "main")                                    # 公开线还停在这里（比如 mem 0.25.x）
        write(self.work, "pkg/a.py", "x = 'new'\n")
        commit(self.work, "new version")
        self.assertEqual(self.run_cmd("sync", "--push")[0], 0)
        exported = self.head(self.pub, "main")
        rc, out = self.run_cmd("sync", "--rev", old, "--push")                      # 再从旧提交导出：会把公开退回去
        self.assertEqual(rc, 1)
        self.assertIn("比上次导出的私有提交", out)
        self.assertNotIn("merge -s ours", out)                                      # 旧提交不该教人接上去
        self.assertEqual(self.head(self.pub, "main"), exported)
        new = sh(self.work, "rev-parse", "main")
        sh(self.work, "switch", "-q", "-c", "side", old)                             # 旧提交上接出来的另一条线
        write(self.work, "pkg/b.py", "y = 'side'\n")
        commit(self.work, "side line")
        rc, out = self.run_cmd("sync", "--rev", "side", "--push")
        self.assertEqual(rc, 1)
        self.assertIn("不是上次导出的私有提交", out)
        self.assertIn(f"1 个提交 side 里没有（{new[:7]}）", out)                       # 照着 merge -s ours 做，公开的 a.py 会退回去
        self.assertNotIn("merge -s ours", out)
        self.assertEqual(self.head(self.pub, "main"), exported)

    def test_new_line_from_main_replaces_a_patched_line_after_merge_ours(self):
        self.init_public()
        sh(self.work, "branch", "release/beta1")                                    # 公开线
        write(self.work, "pkg/a.py", "x = 'fix'\n")
        fix = commit(self.work, "fix on main")
        write(self.work, "pkg/big.py", "feature = 1\n")
        commit(self.work, "big feature on main")
        sh(self.work, "switch", "-q", "release/beta1")
        sh(self.work, "cherry-pick", "-x", fix)                                     # 补丁版
        self.assertEqual(self.run_cmd("sync", "--rev", "release/beta1", "--push")[0], 0)
        last = sh(self.work, "rev-parse", "release/beta1")
        sh(self.work, "switch", "-q", "-c", "release/beta2", "main")                # 功能版：从 main 切新线
        rc, out = self.run_cmd("sync", "--rev", "release/beta2", "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn(f"git merge -s ours {last[:7]}", out)
        sh(self.work, "merge", "-q", "-s", "ours", "-m", "release/beta2 replaces release/beta1", last)
        rc, out = self.run_cmd("sync", "--rev", "release/beta2", "--push")
        self.assertEqual(rc, 0, out)
        self.assertIn("pkg/big.py", sh(self.work, "ls-tree", "-r", "--name-only", self.head(self.pub, "main")).split())

    def test_hand_made_luoke_labs_commit_on_public_is_an_external_change(self):
        self.init_public()
        # 有人用 Luoke Labs 身份直接在公开 main 上提交（不是导出，没有 Exported-From）
        ext = os.path.join(self.tmp, "ext")
        sh(self.tmp, "clone", "-q", self.pub, ext)
        write(ext, "pkg/a.py", "x = 1\ny = 'hotfix on public'\n")
        commit(ext, "hotfix", LUOKE)
        sh(ext, "push", "-q", "origin", "main")
        write(self.work, "README.md", "demo v2\n")
        commit(self.work, "private change")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 1)
        self.assertIn("还没搬回私有 main", out)
        self.assertIn("pkg/a.py", out)
        self.assertIn("hotfix on public", sh(ext, "show", f"{self.head(self.pub, 'main')}:pkg/a.py"))

    def test_first_sync_without_exported_from_needs_base(self):
        self.initial = self.seed_public("initial public release")                  # 以前导出的：没有尾注
        self.init_public()
        write(self.work, "pkg/a.py", "x = 2\n")
        commit(self.work, "change")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 1)
        self.assertIn("--base-from", out)
        self.assertEqual(self.head(self.pub, "main"), self.initial)
        rc, out = self.run_cmd("sync", "--push", "--base", self.initial[:9])
        self.assertEqual(rc, 1)                                                     # 两个要一起给
        self.assertIn("--base-from", out)
        rc, out = self.run_cmd("sync", "--push", "--base", self.initial[:9], "--base-from", self.team[:9])
        self.assertEqual(rc, 0, out)
        new = self.head(self.pub, "main")
        self.assertTrue(sh(self.work, "show", "-s", "--format=%B", new).endswith(f"Exported-From: {sh(self.work, 'rev-parse', 'main')}"))
        write(self.work, "pkg/a.py", "x = 3\n")
        commit(self.work, "change 2")
        rc, out = self.run_cmd("sync", "--push", "--base", self.initial, "--base-from", self.team)
        self.assertEqual(rc, 1)                                                     # 有了导出提交就不能再指定
        self.assertIn("不用 --base", out)
        self.assertEqual(self.run_cmd("sync", "--push")[0], 0)
        rc, out = self.run_cmd("sync", "--base", self.initial, "--base-from", self.team)
        self.assertEqual(rc, 1)                                                     # 树没变也一样
        self.assertIn("不用 --base", out)

    def add_exporter(self):
        write(self.work, "tools/public_tree.py", MINI_PT)
        write(self.work, "internal/plan.txt", "not for public\n")
        commit(self.work, "exporter and an internal file")
        self.init_public()
        write(self.work, "pkg/a.py", "x = 2\n")
        commit(self.work, "change")

    def test_export_rules_come_from_rev_not_the_worktree(self):
        self.add_exporter()
        rc, out = self.run_cmd("sync")
        self.assertEqual(rc, 0, out)
        written = sh(self.work, "rev-parse", "public")
        os.remove(os.path.join(self.work, "tools", "public_tree.py"))               # 工作区里没有（或改过）导出工具
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        self.assertIn("用本地 public 上次写下的", out)
        self.assertEqual(self.head(self.pub, "main"), written)                     # 照 main 里的规则导出，就是写下的那个
        self.assertEqual(sh(self.work, "ls-tree", "-r", "--name-only", written).split(), ["README.md", "pkg/a.py"])
        sh(self.work, "checkout", "--", "tools/public_tree.py")
        sh(self.work, "switch", "-q", "public")                                     # public 检出着：sync 会挪动它，停
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 1)
        self.assertIn("检出着", out)
        self.assertEqual(self.run_cmd("sync", "--dry-run")[0], 0)

    def test_commit_message_is_checked_with_the_repo_rules_too(self):
        self.add_exporter()
        rc, out = self.run_cmd("sync", "--dry-run", "--message", f"measured on {IP}")
        self.assertEqual(rc, 1)
        self.assertIn("提交说明里有泄漏词", out)

    def test_exported_from_is_the_last_line_and_messages_cannot_carry_one(self):
        self.init_public()
        write(self.work, "pkg/a.py", "x = 'new'\n")
        new = commit(self.work, "new version")
        rc, out = self.run_cmd("sync", "--push", "--message", f"copied from the last one\n\nExported-From: {self.team}")
        self.assertEqual(rc, 1)
        self.assertIn("--message 里不能有 Exported-From", out)
        ext = os.path.join(self.tmp, "ext")
        sh(self.tmp, "clone", "-q", self.pub, ext)
        write(ext, "pkg/a.py", "x = 'new'\n")
        two = commit(ext, f"two lines\n\nExported-From: {self.team}\n\nExported-From: {new}", LUOKE)
        write(ext, "README.md", "demo 2\n")
        commit(ext, f"one in the middle\n\nExported-From: {new}\n\nnotes after it", LUOKE)
        self.assertEqual(public_sync.last_export(ext, "HEAD"), (two, new))          # 认最后一行；中间有一行的不算导出提交

    def test_carried_back_then_edited_again_is_not_an_external_change(self):
        self.init_public()
        ext = os.path.join(self.tmp, "ext")
        sh(self.tmp, "clone", "-q", self.pub, ext)
        write(ext, "pkg/a.py", "x = 1  # fixed outside\n")
        pr = commit(ext, "external fix", GITHUB)
        sh(ext, "push", "-q", "origin", "main")
        sh(self.work, "fetch", "-q", self.pub, "main")
        sh(self.work, "cherry-pick", "-x", pr)
        write(self.work, "pkg/a.py", "x = 2  # fixed outside, then improved\n")      # 搬回以后又改了同一行
        commit(self.work, "improve on the external fix")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        # GitHub 的合并提交，PR 里的提交一个个搬回，之后又改了
        sh(ext, "pull", "-q", "--ff-only", "origin", "main")
        sh(ext, "switch", "-q", "-c", "pr")
        write(ext, "README.md", "demo\nfrom a contributor\n")
        c1 = commit(ext, "pr 1")
        write(ext, "pkg/b.py", "y = 1\n")
        c2 = commit(ext, "pr 2")
        sh(ext, "switch", "-q", "main")
        sh(ext, "merge", "-q", "--no-ff", "-m", "Merge pull request", "pr", env=GITHUB)
        sh(ext, "push", "-q", "origin", "main")
        sh(self.work, "fetch", "-q", self.pub, "main")
        sh(self.work, "cherry-pick", "-x", c1, c2)
        write(self.work, "README.md", "demo\nfrom a contributor, edited\n")
        commit(self.work, "edit")
        rc, out = self.run_cmd("sync", "--push")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh(self.work, "show", f"{self.head(self.pub, 'main')}:README.md"), "demo\nfrom a contributor, edited")
        # 没搬回的照样停
        sh(ext, "pull", "-q", "--ff-only", "origin", "main")
        write(ext, "pkg/b.py", "y = 2\n")
        commit(ext, "not carried back", GITHUB)
        sh(ext, "push", "-q", "origin", "main")
        write(self.work, "README.md", "demo v3\n")
        commit(self.work, "more")
        rc, out = self.run_cmd("sync")
        self.assertEqual(rc, 1)
        self.assertIn("pkg/b.py", out)

    def test_message_with_leading_blank_line_reuses_the_written_commit(self):
        self.init_public()
        write(self.work, "pkg/a.py", "x = 2\n")
        commit(self.work, "change")
        self.assertEqual(self.run_cmd("sync", "--message", "\nrelease notes  ")[0], 0)
        written = sh(self.work, "rev-parse", "public")
        rc, out = self.run_cmd("sync", "--push", "--message", "\nrelease notes  ")
        self.assertEqual(rc, 0, out)
        self.assertIn("用本地 public 上次写下的", out)
        self.assertEqual(self.head(self.pub, "main"), written)

    def test_not_a_repo_is_a_clean_stop(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = public_sync.main(["scan", "--repo", self.tmp, "--patterns", self.patterns])
        self.assertEqual(rc, 1)
        self.assertTrue(out.getvalue().startswith("停："), out.getvalue())

    def test_generic_rules(self):
        """不靠本机词表也要拦的；文档、测试里常见的示例不拦。这里的样例都拼起来写，免得这个文件导出时自己命中。"""
        at, org = "@", "github.com/luokelabs/"
        caught = [f"qq 2847{'1'*6}{at}qq.com", f"mail ops{at}luoke.ai", "host build." + "lay" + "2.dev",
                  f"see https://{org}luoke-brand/issues/3", f"git{at}github.com:luoke" + "labs/luoke-core-priv" + "ate.git", "素材在 luoke" + "labs/luoke-brand",
                  "api.github.com/repos/luoke" + "labs/luoke-brand/issues", f"https://{org}content", f"ops{at}mail.luoke.ai", f"abc2024{at}vip.qq.com",
                  f"https://{org}luoke-scan-priv" + "ate/issues", f"https://{org}luoke-agent-priv" + "ate/issues/3", "见 luoke-search-priv" + "ate#12", "hf_" + "a1B2" * 9, "sk-ant-" + "api03-" + "x" * 30, "sk-proj-" + "Ab3" * 15, "AKIA" + "Z3X9" * 4]
        passed = [f"12345678{at}qq.com", f"me{at}gmail.com", public_sync.EXPORT_EMAIL, f"https://{org}luoke-core/issues/36",
                  f"{org}luoke-mem.git", f"https://{org}luoke-releases/releases", f"https://{org}luoke", f"见 https://{org}luoke-core.",
                  f"{org}luoke-mem.git.", f"https://{org}luoke-scan/issues", f"https://{org}luoke-agent/issues/3", f"{org}luoke-search.git", "https://huggingface.co/luoke" + "labs/Qwen3.5-9B-4bit/resolve/4ddb381/{path}", "sk-abc123def456ghi789jkl0",
                  "AKIA" + "IOSFODNN7EXAMPLE", "AKIA" + "ABCDEFGHIJKLMNOP", "luoke-agent 的主界面"]
        files = {f"c{i}.md": (s.encode() + b"\n", 0o644) for i, s in enumerate(caught)}
        files.update({f"p{i}.md": (s.encode() + b"\n", 0o644) for i, s in enumerate(passed)})
        files["gbk.txt"] = (("称呼：" + public_sync._OWNER_WORD + "\n").encode("gbk"), 0o644)   # 不是 UTF-8 的照样查
        hits = {p for p, _, _, _ in public_sync.scan(files, [])}
        self.assertEqual(hits, {f"c{i}.md" for i in range(len(caught))} | {"gbk.txt"})

    def test_scan_dir_flags_text_and_lab_user_paths_in_binaries(self):
        app = os.path.join(self.tmp, "X.app")
        write(app, "Contents/Resources/python/lib/python3.12/site-packages/luoke_x/a.py", "# 内部称呼 \u4e3b\u4eba 10-14\n")
        write(app, "Contents/Resources/python/lib/python3.12/site-packages/thirdparty/b.py", "# by somehost\n")   # 第三方不查
        write(app, "Contents/Resources/python/lib/tk9.0/fontchooser.tcl", "# Copyright by somehost\n")          # Python 带的 Tk 也不查
        os.makedirs(os.path.join(app, "Contents/Frameworks"))
        with open(os.path.join(app, "Contents/Frameworks/librime.dylib"), "wb") as f:
            f.write(f"\0\0{HOME}/somehost/dev/librime/src\0{HOME}/builder/x\0".encode())
        hits, notes = public_sync.scan_dir(app, public_sync.load_patterns(self.patterns))
        self.assertEqual(sorted(h[0] for h in hits), ["Contents/Frameworks/librime.dylib", "Contents/Resources/python/lib/python3.12/site-packages/luoke_x/a.py"])
        self.assertEqual([n[3] for n in notes], [f"{HOME}/builder/"])


if __name__ == "__main__":
    unittest.main()
