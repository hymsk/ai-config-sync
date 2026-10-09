"""端到端隔离测试：本地 bare 仓库作远端，注入式 Layout 模拟多机。"""

import os
import tempfile
import unittest
from pathlib import Path

from ai_config_sync import adopt, gitops, installer
from ai_config_sync.agent import Agent
from ai_config_sync.config import Layout

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
        self.a1 = Agent(boot_for(self.layout1, self.remote), self.layout1,
                        log=silent)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 辅助 ----------

    def _m2(self):
        layout2 = make_layout(self.root, "m2")
        installer.install_attach(layout2, str(self.remote), log=silent)
        return layout2, Agent(boot_for(layout2, self.remote), layout2,
                              log=silent)

    def _repo_blob(self, repo_path):
        clone = self.root / "inspect"
        if clone.exists():
            import shutil
            shutil.rmtree(clone)
        git(["clone", str(self.remote), str(clone)])
        return git(["show", "origin/main:" + repo_path], cwd=clone)

    def _stop_after(self, phase):
        """AICS_STOP_AFTER 环境变量上下文（测试钩子）。"""
        old = os.environ.get("AICS_STOP_AFTER")

        class _Ctx:
            def __enter__(self):
                os.environ["AICS_STOP_AFTER"] = phase

            def __exit__(self, *args):
                if old is None:
                    os.environ.pop("AICS_STOP_AFTER", None)
                else:
                    os.environ["AICS_STOP_AFTER"] = old
        return _Ctx()

    # ---------- 基础同步 ----------

    def test_capture_and_push(self):
        f = host_file(self.host, "conf/app.json", '{"a": 1}\n')
        status, _ = self.a1.run_once()
        self.assertEqual(status, "ok")
        self.assertEqual(
            self._repo_blob("source" + str(f)), '{"a": 1}\n')

    def test_remote_change_applied_with_backup(self):
        f = host_file(self.host, "conf/app.json", '{"a": 1}\n')
        self.assertEqual(self.a1.run_once()[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), '{"a": 2}\n')], "other machine edit")
        self.a1._last_fetch = 0.0  # 强制本轮执行远端检查
        status, _ = self.a1.run_once()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), '{"a": 2}\n')
        backups = list(self.layout1.backups_dir.rglob("app.json"))
        self.assertTrue(backups)
        self.assertEqual(backups[0].read_text(), '{"a": 1}\n')

    def test_local_edit_pushed_next_round(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.a1.run_once()[0], "ok")
        f.write_text("v1\n")
        status, _ = self.a1.run_once()
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "v1\n")

    def test_delete_recreated_from_repo(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.a1.run_once()[0], "ok")
        f.unlink()
        status, _ = self.a1.run_once()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "v0\n")

    def test_idle_when_no_changes(self):
        host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.a1.run_once()[0], "ok")
        self.a1._last_fetch = 0.0  # 强制到达远端检查路径
        self.assertEqual(self.a1.run_once()[0], "ok")
        self.assertEqual(self.a1.run_once()[0], "idle")

    # ---------- 冲突与恢复 ----------

    def test_conflict_flow_with_manual_resolution(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.a1.run_once()[0], "ok")

        # m1：本机提交未推送（stop after captured）
        f.write_text("v1-local\n")
        with self._stop_after("captured"):
            self.assertEqual(self.a1.run_once()[0], "stopped-captured")

        # 另一台服务器推送了不同修改
        _, a2 = self._m2()
        f.write_text("v2-remote\n")
        self.assertEqual(a2.run_once()[0], "ok")

        # m1 恢复：merge 冲突 → 暂停 + 标记 + 现场保留
        status, _ = self.a1.run_once()
        self.assertEqual(status, "paused-conflict")
        self.assertTrue(self.layout1.flag.exists())
        wt = self.layout1.default_worktree
        self.assertIn("source" + str(f), gitops.unmerged_paths(wt))
        self.assertEqual(f.read_text(), "v2-remote\n")  # 主机未被污染

        # 只删标记不解决 → 重新置标记
        self.layout1.flag.unlink()
        status, _ = self.a1.run_once()
        self.assertEqual(status, "paused-conflict")
        self.assertTrue(self.layout1.flag.exists())

        # 人工解决：仓库版 ours / 本机版 theirs
        resolved = self.layout1.default_worktree / ("source" + str(f))
        resolved.write_text("resolved\n")
        git(["add", "source" + str(f)], cwd=wt)
        git(["commit", "--no-edit"], cwd=wt)
        self.layout1.flag.unlink()

        # 冲突后恢复：主机仍是 v2-remote（=冲突时记录值）→ 写回解决结果
        status, _ = self.a1.run_once()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "resolved\n")

        # 下一轮推送解决提交
        self.assertEqual(self.a1.run_once()[0], "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "resolved\n")

    def test_midround_host_edit_not_overwritten(self):
        f = host_file(self.host, "conf/a.txt", "v0\n")
        self.assertEqual(self.a1.run_once()[0], "ok")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "v1-remote\n")], "remote edit")
        self.a1._last_fetch = 0.0  # 强制本轮执行远端检查
        with self._stop_after("pushed"):
            self.assertEqual(self.a1.run_once()[0], "stopped-pushed")
        # 写前复核窗口内本机被编辑
        f.write_text("local-edit\n")
        status, _ = self.a1.run_once()
        self.assertEqual(status, "waiting-local-stable")
        self.assertEqual(f.read_text(), "local-edit\n")
        # 下一轮把本机修改收集并推送
        self.assertEqual(self.a1.run_once()[0], "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "local-edit\n")

    def test_journal_recovery_pushed(self):
        f = self.host / "conf/new-file.txt"
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "from-remote\n")], "new remote file")
        with self._stop_after("pushed"):
            self.assertEqual(self.a1.run_once()[0], "stopped-pushed")
        self.assertFalse(f.exists())  # 尚未写回
        status, _ = self.a1.run_once()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "from-remote\n")

    # ---------- 双机并行 ----------

    def test_nonff_race_different_files(self):
        f1 = host_file(self.host, "m1/a.txt", "one\n")
        with self._stop_after("captured"):
            self.assertEqual(self.a1.run_once()[0], "stopped-captured")
        # 其他机器抢先推送了另一个文件
        manual_push(self.remote, self.root / "other",
                    [("source" + str(self.host / "m2/b.txt"), "two\n")],
                    "other machine")
        status, _ = self.a1.run_once()  # 恢复：fetch+merge+push
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f1)), "one\n")
        self.assertEqual(
            self._repo_blob("source" + str(self.host / "m2/b.txt")), "two\n")

    # ---------- 首轮采用 ----------

    def test_adopt_preview_and_prefer_repo(self):
        f = self.host / "conf/g.json"
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), '{"g": 9}\n')], "repo side")
        rows = dict((p, s) for s, p in adopt.preview(self.a1))
        self.assertEqual(rows[str(f)], "repo-only")
        status, _ = adopt.prefer_repo(self.a1)
        self.assertIn(status, ("ok", "idle"))
        self.assertEqual(f.read_text(), '{"g": 9}\n')

    def test_adopt_prefer_local_overrides_repo(self):
        f = host_file(self.host, "conf/k.txt", "host-version\n")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "repo-version\n")], "repo side")
        status, _ = adopt.prefer_local(self.a1)
        self.assertEqual(status, "ok")
        self.assertEqual(self._repo_blob("source" + str(f)), "host-version\n")

    def test_adopt_prefer_merge_conflict_then_resolve(self):
        f = host_file(self.host, "conf/h.txt", "host-version\n")
        manual_push(self.remote, self.root / "other",
                    [("source" + str(f), "repo-version\n")], "repo side")
        status, _ = adopt.prefer_merge(self.a1)
        self.assertEqual(status, "paused-conflict")
        self.assertTrue(self.layout1.flag.exists())
        wt = self.layout1.default_worktree
        content = (wt / ("source" + str(f))).read_text()
        self.assertIn("repo-version", content)
        self.assertIn("host-version", content)
        (wt / ("source" + str(f))).write_text("merged-result\n")
        git(["add", "source" + str(f)], cwd=wt)
        git(["commit", "--no-edit"], cwd=wt)
        self.layout1.flag.unlink()
        status, _ = self.a1.run_once()
        self.assertEqual(status, "ok")
        self.assertEqual(f.read_text(), "merged-result\n")
        self.assertEqual(self.a1.run_once()[0], "ok")
        self.assertEqual(self._repo_blob("source" + str(f)),
                         "merged-result\n")

    # ---------- 字节保真 ----------

    def test_crlf_bytes_preserved(self):
        old_global = os.environ.get("GIT_CONFIG_GLOBAL")
        cfg = self.root / "gitcfg"
        cfg.write_text("[core]\n\tautocrlf = input\n")
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
        try:
            f = host_file(self.host, "conf/crlf.txt", "a\r\nb\r\n")
            self.assertEqual(self.a1.run_once()[0], "ok")
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
        from ai_config_sync.config import load_bootstrap
        boot = load_bootstrap(layout)
        self.assertEqual(boot.branch, "main")


if __name__ == "__main__":
    unittest.main()
