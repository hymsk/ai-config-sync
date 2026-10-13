"""单元与回归测试：认证判定、备份修剪、配置解析。"""

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


class StepCombinationTest(unittest.TestCase):
    def test_only_six_operations_are_allowed(self):
        from ai_config_sync import cli
        allowed = {" ".join(c) for c in cli.ALLOWED}
        self.assertEqual(allowed, {
            "commit", "push", "sync", "pull",
            "commit push", "push sync", "pull sync"})

    def test_reversed_semantics_are_rejected_by_cli(self):
        import contextlib
        import io
        from ai_config_sync import cli
        # push commit / sync push 等异常语义直接拒绝（返回 2），不触碰安装目录
        for bad in (["push", "commit"], ["sync", "push"],
                    ["commit", "pull"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(bad), 2)

    def test_help_lists_all_combinations(self):
        import contextlib
        import io
        from ai_config_sync import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--help"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        for combo in cli.ALLOWED:
            self.assertIn(" ".join(combo), text)

    def test_no_args_prints_help_and_succeeds(self):
        import contextlib
        import io
        from ai_config_sync import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("usage: ai-config-sync", out.getvalue())

    def test_install_without_mode_is_usage_error(self):
        import contextlib
        import io
        from ai_config_sync import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["install"])
        self.assertEqual(rc, 2)
        self.assertIn("new", err.getvalue())
        self.assertIn("attach", err.getvalue())

    def test_display_width_counts_cjk_as_two(self):
        from ai_config_sync import cli
        self.assertEqual(cli._display_width("commit"), 6)
        self.assertEqual(cli._display_width("本机配置"), 8)
        self.assertEqual(cli._display_width(cli._pad("本机配置", 10)), 10)

    def test_action_table_columns_align(self):
        from ai_config_sync import cli
        lines = [l for l in cli._help_text().splitlines()
                 if l.startswith("  ") and "(" in l and "->" in l]
        self.assertEqual(len(lines), len(cli.ACTION_DESC))
        # 三列起点按显示宽度对齐：描述列与流向列起点唯一
        desc_cols = {cli._display_width(l[:l.index("(")]) for l in lines}
        self.assertEqual(len(desc_cols), 1)


if __name__ == "__main__":
    unittest.main()
