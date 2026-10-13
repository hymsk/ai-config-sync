"""单元与回归测试：认证判定、备份修剪、冲突 driver 幂等。"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_config_sync import applier, gitops
from ai_config_sync.config import parse_repo_config, ConfigError


def silent(*_args):
    return None


class ApplierValidateTest(unittest.TestCase):
    def test_toml_with_array_is_accepted(self):
        # legal TOML the bundled tomlite cannot parse (arrays) must not be rejected
        path = Path("x/config.toml")
        applier.validate(path, b'args = ["serve"]\n')

    def test_invalid_toml_rejected_when_strict_parser_present(self):
        from ai_config_sync import applier as a
        if a._toml_strict is None:
            self.skipTest("no strict TOML parser available")
        with self.assertRaises(applier.ApplyError):
            applier.validate(Path("x/config.toml"), b"key = = broken\n")

    def test_invalid_json_rejected(self):
        with self.assertRaises(applier.ApplyError):
            applier.validate(Path("x/a.json"), b"{not valid")

    def test_other_extensions_pass_through(self):
        applier.validate(Path("x/notes.md"), b"\x00 anything \xff")


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


class ConflictDriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "wt"
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ensure_writes_attributes_and_config(self):
        gitops.ensure_conflict_driver(self.wt)
        attrs = (self.wt / ".git" / "info" / "attributes").read_text(
            encoding="utf-8")
        self.assertIn("source/** merge=aics-conflict", attrs)
        self.assertIn(".sync-config/** merge=aics-conflict", attrs)
        check = subprocess.run(
            ["git", "-C", str(self.wt), "config", "merge.aics-conflict.driver"],
            capture_output=True, text=True)
        self.assertEqual(check.stdout.strip(), "false")

    def test_ensure_idempotent(self):
        gitops.ensure_conflict_driver(self.wt)
        attrs_path = self.wt / ".git" / "info" / "attributes"
        once = attrs_path.read_text(encoding="utf-8")
        gitops.ensure_conflict_driver(self.wt)
        self.assertEqual(attrs_path.read_text(encoding="utf-8"), once)
        self.assertEqual(once.count("source/**"), 1)

    def test_ensure_preserves_existing_attributes(self):
        attrs_path = self.wt / ".git" / "info" / "attributes"
        attrs_path.parent.mkdir(parents=True, exist_ok=True)
        attrs_path.write_text("custom/** -text\n", encoding="utf-8")
        gitops.ensure_conflict_driver(self.wt)
        text = attrs_path.read_text(encoding="utf-8")
        self.assertIn("custom/** -text", text)
        self.assertIn("source/** merge=aics-conflict", text)


class RepoConfigTest(unittest.TestCase):
    def test_repo_config_rejects_negative_backups(self):
        with self.assertRaises(ConfigError):
            parse_repo_config("max_backups_per_file = -1\n")

    def test_repo_config_defaults(self):
        cfg = parse_repo_config('max_backups_per_file = 3\n[git]\n'
                                'name = "x"\nemail = "y"\n')
        self.assertEqual(cfg.max_backups_per_file, 3)
        self.assertEqual(cfg.git_name, "x")
        self.assertEqual(cfg.git_email, "y")

    def test_repo_config_ignores_legacy_intervals(self):
        # interval/remote_check_interval 已随常驻模式移除，不再读取
        cfg = parse_repo_config("interval = 5\nremote_check_interval = 5\n")
        self.assertEqual(cfg.max_backups_per_file, 20)


if __name__ == "__main__":
    unittest.main()
