"""updater 隔离测试：tag 解析、无更新/有更新/无法快进/非 git 安装。"""

import tempfile
import unittest
from pathlib import Path

from ai_config_sync import gitops, updater
from ai_config_sync.config import ConfigError

from tests.helpers import git


def silent(*_args):
    return None


def make_repo(tmp, commits=("v0-base\n",)):
    """创建 bare 远端 + 检出 clone，返回 (remote, clone)。"""
    remote = Path(tmp) / "origin.git"
    git(["init", "--bare", "-b", "main", str(remote)])
    clone = Path(tmp) / "clone"
    git(["clone", str(remote), str(clone)])
    for msg in commits:
        (clone / "file.txt").write_text(msg, encoding="utf-8")
        git(["add", "."], cwd=clone)
        git(["commit", "-m", "c"], cwd=clone)
    git(["push", "origin", "main"], cwd=clone)
    return remote, clone


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _mkrepo(self):
        return make_repo(self.root)

    def _tag(self, clone, version):
        git(["tag", version], cwd=clone)
        git(["push", "origin", version], cwd=clone)

    def test_no_tags_no_update(self):
        _, clone = self._mkrepo()
        updated, tag = updater.check_and_update(str(clone), log=silent)
        self.assertFalse(updated)
        self.assertIsNone(tag)

    def test_older_tag_ignored(self):
        """tag 版本 <= 本地 __version__ → 不更新。"""
        _, clone = self._mkrepo()
        # 本地版本 0.3.0；打一个更低的 tag
        self._tag(clone, "v0.0.1")
        updated, tag = updater.check_and_update(str(clone), log=silent)
        self.assertFalse(updated)

    def _publish_new_version(self, remote, version):
        """模拟上游发新版：在远端追加提交并打 tag。"""
        work = self.root / ("upstream-" + version)
        git(["clone", str(remote), str(work)])
        (work / "file.txt").write_text(
            "release {}\n".format(version), encoding="utf-8")
        git(["add", "."], cwd=work)
        git(["commit", "-m", "release " + version], cwd=work)
        git(["tag", version], cwd=work)
        git(["push", "origin", "main", version], cwd=work)

    def test_newer_tag_updates(self):
        """更高 tag 存在 → 快进到该 tag。"""
        remote, clone = self._mkrepo()
        big = "v999.0.0"
        self._publish_new_version(remote, big)
        # 本地版本号解析为 (0,3,0)，(999,0,0) 更大 → 更新
        updated, tag = updater.check_and_update(str(clone), log=silent,
                                                timeout=10)
        self.assertTrue(updated)
        self.assertEqual(tag, big)
        self.assertEqual(gitops.head(clone),
                         gitops.ref_sha(clone, "refs/tags/" + big))

    def test_highest_tag_wins(self):
        """多个 tag 时取最高版本。"""
        remote, clone = self._mkrepo()
        self._publish_new_version(remote, "v998.0.0")
        self._publish_new_version(remote, "v999.0.0")
        updated, tag = updater.check_and_update(str(clone), log=silent,
                                                timeout=10)
        self.assertTrue(updated)
        self.assertEqual(tag, "v999.0.0")

    def test_diverged_install_errors(self):
        """本地领先（无法快进到 tag）→ UpdateError，不动现场。"""
        remote, clone = self._mkrepo()
        big = "v999.0.0"
        self._publish_new_version(remote, big)
        # 本地 clone 另外提交（与远端分叉）
        (clone / "local.txt").write_text("x\n", encoding="utf-8")
        git(["add", "."], cwd=clone)
        git(["commit", "-m", "local ahead"], cwd=clone)
        with self.assertRaises(updater.UpdateError):
            updater.check_and_update(str(clone), log=silent, timeout=10)
        # 现场保留：HEAD 未被移动
        self.assertIn("local ahead", git(["log", "-1", "--oneline"],
                                        cwd=clone))

    def test_non_git_install_returns_none(self):
        """非 git 检出安装 → (False, None)，不报错。"""
        plain = self.root / "plain"
        plain.mkdir()
        updated, tag = updater.check_and_update(str(plain), log=silent)
        self.assertFalse(updated)
        self.assertIsNone(tag)
        updated, tag = updater.check_and_update("", log=silent)
        self.assertFalse(updated)

    def test_unreachable_remote_silent(self):
        """远端不可达 → latest_tag 返回 None（自动检查场景不阻塞）。"""
        _, clone = self._mkrepo()
        # 指向一个不存在的本地路径
        git(["remote", "set-url", "origin",
             str(self.root / "nonexistent.git")], cwd=clone)
        tag = updater.latest_tag(str(clone), timeout=5)
        self.assertIsNone(tag)

    def test_version_parsing(self):
        self.assertEqual(updater._parse_version("v1.2.3"), (1, 2, 3))
        self.assertIsNone(updater._parse_version("1.2.3"))
        self.assertIsNone(updater._parse_version("v1.2"))
        self.assertIsNone(updater._parse_version("latest"))


if __name__ == "__main__":
    unittest.main()
