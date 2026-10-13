"""端到端隔离测试：本地 bare 仓库作远端，注入式 Layout 模拟多机。

覆盖四个原子步骤（commit / push / sync / pull）与三个固定组合
（commit push / push sync / pull sync）。
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

IDENT = ("test-bot", "test-bot@invalid")


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
                      IDENT)
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

    def _remote_has(self, repo_path):
        import shutil
        clone = self.root / "inspect"
        if clone.exists():
            shutil.rmtree(clone)
        git(["clone", str(self.remote), str(clone)])
        rc = git(["ls-tree", "-r", "--name-only", "origin/main"], cwd=clone)
        return repo_path in rc.splitlines()

    def _manual_commit(self, repo_path, content, message="manual"):
        wt = self.layout1.default_worktree
        dest = wt / repo_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        gitops.add(wt, [repo_path])
        gitops.commit(wt, message, IDENT)

    # ---------- commit：本机文件 → 本地 config ----------

    def test_commit_archives_locally_without_push(self):
        f = host_file(self.host, "conf/app.json", '{"a": 1}\n')
        status, _ = self.s1.run(["commit"])
        self.assertEqual(status, "ok")
        # 本地 HEAD 已是归档内容
        blob = gitops.blob_at(self.layout1.default_worktree, "HEAD",
                              "source" + str(f))
        self.assertEqual(blob.decode(), '{"a": 1}\n')
        # 但尚未推送：远端不认识本地提交
        self.assertFalse(self._remote_has("source" + str(f)))

    def test_commit_clean_reports_noop(self):
        status, msg = self.s1.run(["commit"])
        self.assertEqual(status, "ok")
        self.assertIn("无变动", msg)

    def test_commit_rejects_dirty_worktree(self):
        wt = self.layout1.default_worktree
        (wt / "leftover.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["commit"])
        self.assertEqual(cm.exception.status, "error-dirty")

    def test_commit_mirror_removes_deleted_host_files(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        f.unlink()
        status, msg = self.s1.run(["commit"])
        self.assertEqual(status, "ok")
        self.assertIn("移除 1", msg)
        blob = gitops.blob_at(self.layout1.default_worktree, "HEAD",
                              "source" + str(f))
        self.assertIsNone(blob)

    def test_commit_missing_explicit_file_errors(self):
        missing = self.host / "conf" / "gone.txt"
        list_path = (self.layout1.default_worktree / ".sync-config"
                     / "sync.list")
        list_path.write_text(
            "{}/**\n{}\n".format(self.host, missing), encoding="utf-8")
        gitops.add(self.layout1.default_worktree,
                   [".sync-config/sync.list"])
        gitops.commit(self.layout1.default_worktree, "chore: add rule", IDENT)
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["commit"])
        self.assertEqual(cm.exception.status, "error-missing")

    def test_commit_missing_wildcard_prefix_dir_errors(self):
        gone = self.root / "etc-xxx"
        list_path = (self.layout1.default_worktree / ".sync-config"
                     / "sync.list")
        list_path.write_text(
            "{}/**\n{}\n".format(self.host, str(gone) + "/**"),
            encoding="utf-8")
        gitops.add(self.layout1.default_worktree,
                   [".sync-config/sync.list"])
        gitops.commit(self.layout1.default_worktree, "chore: add dir rule",
                      IDENT)
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["commit"])
        self.assertEqual(cm.exception.status, "error-missing")

    def test_commit_deleted_file_under_existing_prefix_silent(self):
        f = host_file(self.host, "conf/b.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        f.unlink()
        status, _ = self.s1.run(["commit"])
        self.assertEqual(status, "ok")
        blob = gitops.blob_at(self.layout1.default_worktree, "HEAD",
                              "source" + str(f))
        self.assertIsNone(blob)

    def test_commit_unstable_file_skipped(self):
        from unittest import mock
        host_file(self.host, "conf/a.txt", "v0\n")
        with mock.patch("ai_config_sync.collector.read_stable",
                        return_value=None):
            status, msg = self.s1.run(["commit"])
        self.assertEqual(status, "ok")
        self.assertIn("未稳定", msg)

    # ---------- push：本地 config → 远端 ----------

    def test_push_pushes_local_commits_only(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        status, msg = self.s1.run(["push"])
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "v0\n")

    def test_push_does_not_touch_host_files(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        self.assertEqual(self.s1.run(["push"])[0], "ok")
        # push 不读主机文件：把主机文件改脏也不影响 push
        f.write_text("dirty\n")
        self.assertEqual(self.s1.run(["push"])[0], "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "v0\n")

    def test_push_nothing_to_push_is_noop(self):
        status, msg = self.s1.run(["push"])
        self.assertEqual(status, "ok")
        self.assertIn("无本地提交", msg)

    def test_push_rejects_remote_ahead(self):
        # 本地无未推送提交，远端前进 → 本地落后
        manual_push(self.remote, self.root / "other",
                    [("source/other.txt", "remote\n")], "remote ahead")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["push"])
        self.assertEqual(cm.exception.status, "error-conflict")
        self.assertIn("落后", cm.exception.message)

    def test_push_rejects_diverged(self):
        host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source/other.txt", "remote\n")], "remote edit")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["push"])
        self.assertEqual(cm.exception.status, "error-conflict")

    def test_push_rejects_dirty_worktree(self):
        wt = self.layout1.default_worktree
        (wt / "leftover.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["push"])
        self.assertEqual(cm.exception.status, "error-dirty")

    # ---------- sync：本地 config → 本机 ----------

    def test_sync_writes_managed_files_to_host(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        f.unlink()
        status, _ = self.s1.run(["sync"])
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "v0\n")

    def test_sync_recreates_missing_file(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        f.unlink()
        self.assertEqual(self.s1.run(["sync"])[0], "ok")
        self.assertEqual(f.read_text(), "v0\n")

    def test_sync_does_not_delete_local_files(self):
        host_file(self.host, "conf/keep.txt", "keep\n")
        status, _ = self.s1.run(["sync"])
        self.assertEqual(status, "ok")
        self.assertTrue((self.host / "conf" / "keep.txt").is_file())

    def test_sync_same_content_no_write(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        mtime = f.stat().st_mtime_ns
        status, msg = self.s1.run(["sync"])
        self.assertEqual(status, "ok")
        self.assertIn("已一致 1", msg)
        self.assertEqual(f.stat().st_mtime_ns, mtime)

    def test_sync_invalid_json_skipped(self):
        f = self.host / "conf/bad.json"
        self._manual_commit("source" + str(f), "{not valid json", "bad json")
        status, msg = self.s1.run(["sync"])
        self.assertEqual(status, "ok")
        self.assertIn("跳过", msg)
        self.assertFalse(f.exists())

    # ---------- pull：远端 → 本地 config ----------

    def test_pull_fast_forwards_local_only(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "v1-remote\n")], "remote edit")
        status, msg = self.s1.run(["pull"])
        self.assertEqual(status, "ok")
        self.assertIn("快进", msg)
        # 本地 config 已更新，但主机文件未动
        blob = gitops.blob_at(self.layout1.default_worktree, "HEAD",
                              "source" + str(f))
        self.assertEqual(blob.decode(), "v1-remote\n")
        self.assertEqual(f.read_text(), "v0\n")

    def test_pull_up_to_date_is_noop(self):
        status, msg = self.s1.run(["pull"])
        self.assertEqual(status, "ok")
        self.assertIn("最新", msg)

    def test_pull_local_ahead_is_noop(self):
        host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")  # 本地领先
        status, msg = self.s1.run(["pull"])
        self.assertEqual(status, "ok")
        self.assertIn("领先", msg)

    def test_pull_rejects_diverged(self):
        host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source/other.txt", "remote\n")], "remote edit")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["pull"])
        self.assertEqual(cm.exception.status, "error-conflict")
        self.assertIn("分叉", cm.exception.message)

    def test_pull_rejects_dirty_worktree(self):
        wt = self.layout1.default_worktree
        (wt / "leftover.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["pull"])
        self.assertEqual(cm.exception.status, "error-dirty")

    # ---------- 组合 ----------

    def test_commit_push_archives_and_pushes(self):
        f = host_file(self.host, "conf/app.json", '{"a": 1}\n')
        status, _ = self.s1.run(["commit", "push"])
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), '{"a": 1}\n')

    def test_commit_push_local_edit_pushed(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        f.write_text("v1\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "v1\n")

    def test_commit_push_mirror_removes_deleted(self):
        """真镜像：本机删除的受管文件在 commit push 下从仓库移除。"""
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        f.unlink()
        status, msg = self.s1.run(["commit", "push"])
        self.assertEqual(status, "ok")
        self.assertIn("移除 1", msg)
        self.assertFalse(self._remote_has("source" + str(f)))

    def test_commit_push_remote_edit_conflicts_after_split(self):
        """拆分模型的代价：commit 不联网。远端前进后本地 commit 产生本地提交，
        此时 push 发现真分叉 → error-conflict（要覆盖远端须先 pull 再合并）。

        对比：本地优先的整条流水线是 pull →（人工取舍）→ commit push。
        """
        f = host_file(self.host, "conf/r.txt", "l1\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "l1\nREMOTE\n")], "remote tail edit")
        f.write_text("LOCAL\nl1\n")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["commit", "push"])
        self.assertEqual(cm.exception.status, "error-conflict")
        self.assertIn("分叉", cm.exception.message)

    def test_commit_push_remote_advanced_rejected(self):
        """独立步骤的代价：commit 不联网，push 才发现远端前进前方本机已有本地提交
        → error-conflict（分叉），保留现场交人工。"""
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source/other.txt", "remote\n")], "remote ahead")
        f.write_text("v1\n")
        with self.assertRaises(SyncError) as cm:
            self.s1.run(["commit", "push"])
        self.assertEqual(cm.exception.status, "error-conflict")

    def test_pull_sync_writes_remote_change_with_backup(self):
        f = host_file(self.host, "conf/app.json", '{"a": 1}\n')
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), '{"a": 2}\n')], "other machine edit")
        status, _ = self.s1.run(["pull", "sync"])
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), '{"a": 2}\n')
        backups = list(self.layout1.backups_dir.rglob("app.json"))
        self.assertTrue(backups)
        self.assertEqual(backups[0].read_text(), '{"a": 1}\n')

    def test_pull_sync_recreates_missing_file(self):
        """sync 不做前缀存在检查：本机缺失正是要重建的场景。"""
        f = host_file(self.host, "conf/c.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        f.unlink()
        status, _ = self.s1.run(["pull", "sync"])
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "v0\n")

    def test_pull_sync_does_not_delete_local_files(self):
        host_file(self.host, "conf/keep.txt", "keep\n")
        status, _ = self.s1.run(["pull", "sync"])
        self.assertEqual(status, "ok")
        self.assertTrue((self.host / "conf" / "keep.txt").is_file())

    def test_pull_sync_invalid_json_skipped(self):
        f = self.host / "conf/bad.json"
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "{not valid json")], "bad json")
        status, msg = self.s1.run(["pull", "sync"])
        self.assertEqual(status, "ok")
        self.assertIn("跳过", msg)
        self.assertFalse(f.exists())

    def test_push_sync_local_git_is_truth(self):
        """push sync：本地 git 为准 → 推送远端并落到本机文件。"""
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.s1.run(["commit"])[0], "ok")
        # 本地 git 手工改动（模拟 worktree 手工提交）
        self._manual_commit("source" + str(f), "local-git\n", "manual edit")
        status, _ = self.s1.run(["push", "sync"])
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "local-git\n")
        self.assertEqual(f.read_text(), "local-git\n")

    # ---------- 双机单向协作 ----------

    def test_two_hosts_commit_push_then_pull_sync(self):
        f = host_file(self.host, "conf/a.txt", "from-m1\n")
        self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
        layout2, s2 = self._m2()
        status, _ = s2.run(["pull", "sync"])
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "from-m1\n")

    def test_pull_sync_first_keeps_other_hosts_file(self):
        """多机协作流：先 pull sync 再 commit push，保留他机归档的文件。"""
        f = host_file(self.host, "m1/a.txt", "one\n")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(self.host / "m2/b.txt"), "two\n")],
                    "other machine")
        self.assertEqual(self.s1.run(["pull", "sync"])[0], "ok")
        self.assertEqual((self.host / "m2" / "b.txt").read_text(), "two\n")
        status, _ = self.s1.run(["commit", "push"])
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "one\n")
        self.assertEqual(
            self._repo_blob("source" + str(self.host / "m2/b.txt")), "two\n")

    # ---------- 其他 ----------

    def test_commit_push_symlink_skipped(self):
        f = host_file(self.host, "conf/real.txt", "real\n")
        link = self.host / "conf" / "link.txt"
        os.symlink(str(f), str(link))
        status, _ = self.s1.run(["commit", "push"])
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "real\n")
        self.assertFalse(self._remote_has("source" + str(link)))

    def test_commit_push_crlf_byte_fidelity(self):
        old_global = os.environ.get("GIT_CONFIG_GLOBAL")
        cfg = self.root / "gitcfg"
        cfg.write_text("[core]\n\tautocrlf = input\n")
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
        try:
            f = host_file(self.host, "conf/crlf.txt", "a\r\nb\r\n")
            self.assertEqual(self.s1.run(["commit", "push"])[0], "ok")
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
        layout2 = make_layout(self.root, "ins2")
        installer.install_new(layout2, str(remote2), branch="cfg2", force=True,
                              log=silent)
        self.assertTrue((layout2.default_worktree / ".sync-config"
                         / "config.toml").is_file())
        clone = self.root / "check2"
        git(["clone", str(remote2), str(clone)])
        self.assertEqual(git(["ls-remote", "--heads", str(remote2)]).count(
            "refs/heads/main"), 1)
        layout3 = make_layout(self.root, "ins3")
        with self.assertRaises(installer.InstallError):
            installer.install_new(layout3, str(remote2), branch="main",
                                  force=True, log=silent)
        with self.assertRaises(installer.InstallError):
            installer.install_attach(layout3, str(remote2), branch="nope",
                                     log=silent)
        with self.assertRaises(installer.InstallError):
            installer.install_new(layout2, str(remote2), branch="other",
                                  log=silent)

    def test_attach_default_branch_resolution(self):
        layout, _ = self._m2()
        boot = load_bootstrap(layout)
        self.assertEqual(boot.branch, "main")


if __name__ == "__main__":
    unittest.main()
