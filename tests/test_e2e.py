"""端到端隔离测试：本地 bare 仓库作远端，注入式 Layout 模拟多机。

覆盖两个手动单向操作：push（本机 → 仓库）与 pull（仓库 → 本机）。
"""

import os
import tempfile
import unittest
from pathlib import Path

from ai_config_sync import gitops, installer
from ai_config_sync.config import load_bootstrap
from ai_config_sync.engine import Sync, SyncError

from tests.helpers import boot_for, git, host_file, make_bare, make_layout, \
    manual_push


def silent(*_args):
    return None


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = make_bare(self.root)
        self.host = self.root / "host"
        self.host.mkdir()

        self.layout1 = make_layout(self.root, "m1")
        installer.install_new(self.layout1, str(self.remote), branch="main",
                              log=silent)
        # 纳入清单：管理测试 host 目录
        list_path = (self.layout1.default_worktree / ".sync-config"
                     / "sync.list")
        list_path.write_text("{}/**\n".format(self.host), encoding="utf-8")
        gitops.add(self.layout1.default_worktree,
                   [".sync-config/sync.list"])
        gitops.commit(self.layout1.default_worktree, "chore: manage test dir",
                      ("test-bot", "test-bot@invalid"))
        gitops.push(self.layout1.default_worktree, "main")
        self.s1 = Sync(boot_for(self.layout1, self.remote), self.layout1,
                       log=silent)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 辅助 ----------

    def _m2(self):
        layout2 = make_layout(self.root, "m2")
        installer.install_attach(layout2, str(self.remote), log=silent)
        return layout2, Sync(boot_for(layout2, self.remote), layout2,
                             log=silent)

    def _repo_blob(self, repo_path):
        import shutil
        clone = self.root / "inspect"
        if clone.exists():
            shutil.rmtree(clone)
        git(["clone", str(self.remote), str(clone)])
        return git(["show", "origin/main:" + repo_path], cwd=clone)

    # ---------- push：本机 → 仓库 ----------

    def test_push_archives_and_pushes(self):
        f = host_file(self.host, "conf/app.json", '{"a": 1}\n')
        status, _ = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), '{"a": 1}\n')

    def test_push_local_edit_pushed(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        f.write_text("v1\n")
        status, _ = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "v1\n")

    def test_push_removes_deleted_host_files(self):
        """真镜像：本机删除的受管文件在 push 时从仓库移除。"""
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        f.unlink()
        status, msg = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertIn("移除 1", msg)
        with self.assertRaises(AssertionError):
            self._repo_blob("source" + str(f))

    def test_push_missing_explicit_file_errors(self):
        """显式文件规则指向的本机文件缺失 → error-missing。"""
        missing = self.host / "conf" / "gone.txt"
        list_path = (self.layout1.default_worktree / ".sync-config"
                     / "sync.list")
        list_path.write_text(
            "{}/**\n{}\n".format(self.host, missing), encoding="utf-8")
        gitops.add(self.layout1.default_worktree,
                   [".sync-config/sync.list"])
        gitops.commit(self.layout1.default_worktree, "chore: add rule",
                      ("test-bot", "test-bot@invalid"))
        gitops.push(self.layout1.default_worktree, "main")
        with self.assertRaises(SyncError) as cm:
            self.s1.push()
        self.assertEqual(cm.exception.status, "error-missing")

    def test_push_missing_wildcard_prefix_dir_errors(self):
        """目录前缀整体不存在（如 /etc/xxx/** 而 /etc/xxx 没了）→ error-missing。"""
        gone = self.root / "etc-xxx"
        list_path = (self.layout1.default_worktree / ".sync-config"
                     / "sync.list")
        list_path.write_text(
            "{}/**\n{}\n".format(self.host, str(gone) + "/**"),
            encoding="utf-8")
        gitops.add(self.layout1.default_worktree,
                   [".sync-config/sync.list"])
        gitops.commit(self.layout1.default_worktree, "chore: add dir rule",
                      ("test-bot", "test-bot@invalid"))
        gitops.push(self.layout1.default_worktree, "main")
        with self.assertRaises(SyncError) as cm:
            self.s1.push()
        self.assertEqual(cm.exception.status, "error-missing")

    def test_push_deleted_file_under_existing_prefix_silent(self):
        """前缀存在、目录内具体文件被删 → 静默移除，不报 error-missing。"""
        f = host_file(self.host, "conf/b.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        f.unlink()
        status, msg = self.s1.push()
        self.assertEqual(status, "ok")
        with self.assertRaises(AssertionError):
            self._repo_blob("source" + str(f))

    def test_pull_does_not_require_prefix_existence(self):
        """pull 不做前缀存在检查：本机缺失正是要重建的场景。"""
        f = host_file(self.host, "conf/c.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        f.unlink()
        status, _ = self.s1.pull()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "v0\n")

    def test_push_when_clean_reports_noop(self):
        host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        status, msg = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertIn("0 个文件", msg)

    def test_push_rejects_dirty_worktree(self):
        wt = self.layout1.default_worktree
        (wt / "leftover.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(SyncError) as cm:
            self.s1.push()
        self.assertEqual(cm.exception.status, "error-dirty")

    def test_push_unstable_file_skipped(self):
        from unittest import mock
        host_file(self.host, "conf/a.txt", "v0\n")
        with mock.patch("ai_config_sync.collector.read_stable",
                        return_value=None):
            status, msg = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertIn("未稳定", msg)

    def test_push_nonff_aligns_then_pushes(self):
        """远端先前进：fetch 后本地只快进对齐，再提交推送。

        注意镜像语义：本机没有 m2/b.txt，push 后它被从仓库移除
        （真镜像 = 命中清单但本机不存在的文件从仓库删掉）。
        """
        f = host_file(self.host, "m1/a.txt", "one\n")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(self.host / "m2/b.txt"), "two\n")],
                    "other machine")
        status, msg = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "one\n")
        self.assertIn("移除 1", msg)
        with self.assertRaises(AssertionError):
            self._repo_blob("source" + str(self.host / "m2/b.txt"))

    def test_push_pull_first_keeps_other_hosts_file(self):
        """多机协作流：先 pull 再 push，则保留他机归档的文件。"""
        f = host_file(self.host, "m1/a.txt", "one\n")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(self.host / "m2/b.txt"), "two\n")],
                    "other machine")
        # 先 pull：b.txt 写回本机（仓库有、清单命中、本机缺失 → 重建）
        self.assertEqual(self.s1.pull()[0], "ok")
        self.assertEqual((self.host / "m2" / "b.txt").read_text(), "two\n")
        # 再 push：b.txt 已存在于本机，不会被移除
        status, msg = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "one\n")
        self.assertEqual(
            self._repo_blob("source" + str(self.host / "m2/b.txt")), "two\n")

    def test_push_remote_edit_overwritten_by_local_mirror(self):
        """同文件双侧修改：无 merge，push 以本机为准覆盖远端（本地优先）。"""
        f = host_file(self.host, "conf/r.txt",
                      "l1\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n")
        self.assertEqual(self.s1.push()[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f),
                      "l1\nl2\nl3\nl4\nl5\nl6\nl7\nL8-REMOTE\n")],
                    "remote tail edit")
        f.write_text("L1-LOCAL\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n")
        status, _ = self.s1.push()
        self.assertEqual(status, "ok")
        # 远端最终为本机内容：镜像语义，本地优先
        self.assertEqual(
            self._repo_blob("source" + str(f)),
            "L1-LOCAL\nl2\nl3\nl4\nl5\nl6\nl7\nl8\n")

    def test_diverged_worktree_errors(self):
        """本地与远端分叉（人工在 worktree 提交了改动）→ error-conflict。"""
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        # 人工在 worktree 提交 + 远端也前进 → 真分叉
        wt = self.layout1.default_worktree
        (wt / "README.md").write_text("manual\n", encoding="utf-8")
        gitops.add(wt, ["README.md"])
        gitops.commit(wt, "manual edit", ("test-bot", "test-bot@invalid"))
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "v-remote\n")], "other machine")
        with self.assertRaises(SyncError) as cm:
            self.s1.push()
        self.assertEqual(cm.exception.status, "error-conflict")
        self.assertIn("分叉", cm.exception.message)

    def test_push_symlink_skipped(self):
        f = host_file(self.host, "conf/real.txt", "real\n")
        link = self.host / "conf" / "link.txt"
        os.symlink(str(f), str(link))
        status, _ = self.s1.push()
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "real\n")
        # 软链接未被归档：远端该路径不存在
        import shutil
        clone = self.root / "inspect2"
        shutil.rmtree(clone, ignore_errors=True)
        git(["clone", str(self.remote), str(clone)])
        listing = git(["ls-tree", "-r", "--name-only", "origin/main"], cwd=clone)
        self.assertNotIn("source" + str(link), listing)

    # ---------- pull：仓库 → 本机 ----------

    def test_pull_writes_remote_change_with_backup(self):
        f = host_file(self.host, "conf/app.json", '{"a": 1}\n')
        self.assertEqual(self.s1.push()[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), '{"a": 2}\n')], "other machine edit")
        status, _ = self.s1.pull()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), '{"a": 2}\n')
        backups = list(self.layout1.backups_dir.rglob("app.json"))
        self.assertTrue(backups)
        self.assertEqual(backups[0].read_text(), '{"a": 1}\n')

    def test_pull_recreates_missing_file(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        f.unlink()
        status, _ = self.s1.pull()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "v0\n")

    def test_pull_does_not_delete_local_files(self):
        """本机独有、仓库没有的文件在 pull 下保留。"""
        host_file(self.host, "conf/keep.txt", "keep\n")
        status, _ = self.s1.pull()
        self.assertEqual(status, "ok")
        self.assertTrue((self.host / "conf" / "keep.txt").is_file())

    def test_pull_invalid_json_skipped(self):
        f = self.host / "conf/bad.json"
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "{not valid json")], "bad json")
        status, msg = self.s1.pull()
        self.assertEqual(status, "ok")
        self.assertIn("跳过", msg)
        self.assertFalse(f.exists())

    def test_pull_same_content_no_write(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.push()[0], "ok")
        mtime = f.stat().st_mtime_ns
        status, msg = self.s1.pull()
        self.assertEqual(status, "ok")
        self.assertIn("已一致 1", msg)
        self.assertEqual(f.stat().st_mtime_ns, mtime)

    # ---------- 双机单向协作 ----------

    def test_two_hosts_push_then_pull(self):
        f = host_file(self.host, "conf/a.txt", "from-m1\n")
        self.assertEqual(self.s1.push()[0], "ok")
        layout2, s2 = self._m2()
        # m2 的清单来自仓库（继承 host 目录），pull 后得到 m1 的内容
        status, _ = s2.pull()
        self.assertEqual(status, "ok")
        # m2 上同一 host 路径即同一绝对路径（多机共用清单）
        self.assertEqual(f.read_text(), "from-m1\n")

    def test_push_symlink_skipped(self):
        old_global = os.environ.get("GIT_CONFIG_GLOBAL")
        cfg = self.root / "gitcfg"
        cfg.write_text("[core]\n\tautocrlf = input\n")
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
        try:
            f = host_file(self.host, "conf/crlf.txt", "a\r\nb\r\n")
            self.assertEqual(self.s1.push()[0], "ok")
            self.assertEqual(self._repo_blob("source" + str(f)), "a\r\nb\r\n")
        finally:
            if old_global is None:
                os.environ.pop("GIT_CONFIG_GLOBAL", None)
            else:
                os.environ["GIT_CONFIG_GLOBAL"] = old_global

    # ---------- 安装 ----------

    def test_install_rules(self):
        remote2 = make_bare(self.root, "second.git")
        layout = make_layout(self.root, "ins")
        manual_push(remote2, self.root / "seed2",
                    [("some/file.txt", "x\n")], "make non-empty")

        with self.assertRaises(installer.InstallError):
            installer.install_new(layout, str(remote2), branch="main",
                                  log=silent)
        # --force 孤儿分支播种：纯新增，不碰已有分支
        layout2 = make_layout(self.root, "ins2")
        installer.install_new(layout2, str(remote2), branch="cfg2", force=True,
                              log=silent)
        self.assertTrue((layout2.default_worktree / ".sync-config"
                         / "config.toml").is_file())
        clone = self.root / "check2"
        git(["clone", str(remote2), str(clone)])
        self.assertEqual(git(["ls-remote", "--heads", str(remote2)]).count(
            "refs/heads/main"), 1)
        # 已存在分支 + --force 仍拒绝
        layout3 = make_layout(self.root, "ins3")
        with self.assertRaises(installer.InstallError):
            installer.install_new(layout3, str(remote2), branch="main",
                                  force=True, log=silent)
        # attach 分支不存在拒绝
        with self.assertRaises(installer.InstallError):
            installer.install_attach(layout3, str(remote2), branch="nope",
                                     log=silent)
        # bootstrap 已存在拒绝重装
        with self.assertRaises(installer.InstallError):
            installer.install_new(layout2, str(remote2), branch="other",
                                  log=silent)

    def test_attach_default_branch_resolution(self):
        layout, _ = self._m2()
        boot = load_bootstrap(layout)
        self.assertEqual(boot.branch, "main")


if __name__ == "__main__":
    unittest.main()
