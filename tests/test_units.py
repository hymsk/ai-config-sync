"""单元与回归测试：认证判定、备份修剪、首轮采用推送失败。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_config_sync import adopt, applier, gitops, installer
from ai_config_sync.agent import Agent
from ai_config_sync.config import Layout

from tests.helpers import boot_for, host_file, make_bare, make_layout


def silent(*_args):
    return None


class GitopsAuthTest(unittest.TestCase):
    def test_remote_publickey_rejection_is_auth(self):
        self.assertTrue(gitops.is_auth_error(
            "git@host: Permission denied (publickey). fatal: Could not read "
            "from remote repository."))

    def test_local_fs_permission_denied_is_not_auth(self):
        # 工作树 .git 目录权限损坏等本地错误不得误判为远端认证失败
        self.assertFalse(gitops.is_auth_error(
            "error: unable to access '/var/lib/repo/.git/index': "
            "Permission denied"))

    def test_http_401_is_auth(self):
        self.assertTrue(gitops.is_auth_error(
            "remote: HTTP Basic: Access denied. The provided password or "
            "token is wrong or you require authentication."))
        self.assertTrue(gitops.is_auth_error("fatal: Authentication failed"))


class ApplierPruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backups = self.root / "backups"
        self.repo_path = "source/root/conf/a.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def _make_backup(self, run_id, content):
        dest = self.backups / run_id / self.repo_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        return dest

    def test_prune_keeps_recent_only(self):
        for i in range(5):
            self._make_backup("2026010{}T000000Z".format(i), "v{}\n".format(i))
        applier.prune_backups(self.backups, self.repo_path, keep=2)
        left = sorted(d.name for d in self.backups.iterdir())
        self.assertEqual(left, ["20260103T000000Z", "20260104T000000Z"])

    def test_prune_removes_empty_run_dirs(self):
        self._make_backup("20260101T000000Z", "v0\n")
        self._make_backup("20260102T000000Z", "v1\n")
        applier.prune_backups(self.backups, self.repo_path, keep=1)
        self.assertEqual(sorted(d.name for d in self.backups.iterdir()),
                         ["20260102T000000Z"])

    def test_zero_keep_disables_prune(self):
        for i in range(4):
            self._make_backup("2026010{}T000000Z".format(i), "v\n")
        applier.prune_backups(self.backups, self.repo_path, keep=0)
        self.assertEqual(len(list(self.backups.iterdir())), 4)

    def test_prune_does_not_touch_other_paths(self):
        self._make_backup("20260101T000000Z", "a\n")
        other = self.backups / "20260101T000000Z" / "source/root/conf/b.txt"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("b\n")
        applier.prune_backups(self.backups, self.repo_path, keep=0)
        self.assertTrue(other.exists())


class AdoptPushFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = make_bare(self.root)
        self.host = self.root / "host"
        self.host.mkdir()
        self.layout = make_layout(self.root, "m1")
        installer.install_new(self.layout, str(self.remote), branch="main",
                              log=silent)
        list_path = (self.layout.default_worktree / ".sync-config"
                     / "sync.list")
        list_path.write_text("{}/**\n".format(self.host), encoding="utf-8")
        gitops.add(self.layout.default_worktree, [".sync-config/sync.list"])
        gitops.commit(self.layout.default_worktree, "chore: manage test dir",
                      ("test-bot", "test-bot@invalid"))
        gitops.push(self.layout.default_worktree, "main")
        self.agent = Agent(boot_for(self.layout, self.remote), self.layout,
                           log=silent)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prefer_merge_push_failure_returns_transient(self):
        f = host_file(self.host, "conf/a.txt", "host-version\n")
        err = gitops.GitError("git 失败(128): push",
                              "fatal: unable to access: Could not resolve "
                              "host")
        with mock.patch.object(gitops, "push", side_effect=err):
            status, _ = adopt.prefer_merge(self.agent)
        self.assertEqual(status, "error-transient")
        # 合并结果已在本地 HEAD，主机文件未被破坏
        self.assertEqual(f.read_text(), "host-version\n")


if __name__ == "__main__":
    unittest.main()
