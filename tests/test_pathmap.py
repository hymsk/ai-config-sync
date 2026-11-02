"""path_map 单元测试：~ 展开、段边界、最长前缀、撞车拒绝、双向映射。"""

import unittest

from ai_config_sync.pathmap import (
    PathMap, PathMapError, expand_tilde, home_dir, normalize)

LINUX_HOME = "/root"
GB_HOME = "C:/Users/name"


class NormalizeTest(unittest.TestCase):
    def test_backslashes_normalized(self):
        self.assertEqual(normalize("C:\\Users\\name\\a"), "C:/Users/name/a")

    def test_forward_slashes_untouched(self):
        self.assertEqual(normalize("/root/a"), "/root/a")


class ExpandTildeTest(unittest.TestCase):
    def test_expand_uses_injected_home(self):
        self.assertEqual(expand_tilde("~", GB_HOME), GB_HOME)
        self.assertEqual(expand_tilde("~/.config", GB_HOME),
                         GB_HOME + "/.config")

    def test_non_tilde_untouched(self):
        self.assertEqual(expand_tilde("/etc/x", GB_HOME), "/etc/x")
        self.assertEqual(expand_tilde("~user/x", GB_HOME), "~user/x")


class DefaultMapLinuxTest(unittest.TestCase):
    """默认 {"~": "__user_home__"}：home 显式走映射（不再与恒等布局重合）。"""

    def setUp(self):
        self.pm = PathMap(home=LINUX_HOME)

    def test_home_archives_to_user_home_dir(self):
        self.assertEqual(self.pm.to_repo("/root/.bashrc"),
                         "source/__user_home__/.bashrc")

    def test_unmapped_path_keeps_identity(self):
        self.assertEqual(self.pm.to_repo("/etc/foo.conf"),
                         "source/etc/foo.conf")

    def test_segment_boundary(self):
        # /root 不匹配 /rootdir（路径段边界）；/rootdir 走恒等映射
        self.assertEqual(self.pm.to_repo("/rootdir/x"), "source/rootdir/x")

    def test_to_host_round_trip(self):
        self.assertEqual(self.pm.to_host("source/__user_home__/.bashrc"),
                         "/root/.bashrc")
        self.assertEqual(self.pm.to_host("source/etc/foo.conf"),
                         "/etc/foo.conf")


class GitBashMapTest(unittest.TestCase):
    """Git Bash（Windows 原生 Python）：home 映射到 source/__user_home__/。"""

    def setUp(self):
        self.pm = PathMap(home=GB_HOME)  # 默认 raw {"~": "__user_home__"}

    def test_home_archives_to_source_root(self):
        self.assertEqual(
            self.pm.to_repo(GB_HOME + "/.config/opencode/AGENTS.md"),
            "source/__user_home__/.config/opencode/AGENTS.md")

    def test_backslash_host_path_normalized(self):
        self.assertEqual(self.pm.to_repo("C:\\Users\\name\\.bashrc"),
                         "source/__user_home__/.bashrc")

    def test_unmapped_path_keeps_identity(self):
        self.assertEqual(self.pm.to_repo("/etc/gitconfig"),
                         "source/etc/gitconfig")

    def test_to_host_uses_local_home(self):
        self.assertEqual(self.pm.to_host("source/__user_home__/.bashrc"),
                         GB_HOME + "/.bashrc")

    def test_identity_path_colliding_with_mapped_value_rejected(self):
        # 未命中键的 /__user_home__/x 恒等归档到 source/__user_home__/x，
        # 与映射值 __user_home__ 撞车 -> 显式配置错误
        with self.assertRaises(PathMapError):
            self.pm.to_repo("/__user_home__/x")


class LongestPrefixTest(unittest.TestCase):
    def test_nested_tilde_prefix_wins(self):
        pm = PathMap({"~": "root", "~/work": "work"}, home=LINUX_HOME)
        self.assertEqual(pm.to_repo("/root/work/a"), "source/work/a")
        self.assertEqual(pm.to_repo("/root/other/a"), "source/root/other/a")
        self.assertEqual(pm.to_host("source/work/a"), "/root/work/a")
        self.assertEqual(pm.to_host("source/root/other/a"),
                         "/root/other/a")

    def test_absolute_key_mapping(self):
        pm = PathMap({"~": "root", "/d/data": "data"}, home=GB_HOME)
        self.assertEqual(pm.to_repo("/d/data/conf/x"), "source/data/conf/x")
        self.assertEqual(pm.to_host("source/data/conf/x"), "/d/data/conf/x")


class PathMapValidationTest(unittest.TestCase):
    def test_empty_table_rejected(self):
        with self.assertRaises(PathMapError):
            PathMap({})

    def test_absolute_value_rejected(self):
        with self.assertRaises(PathMapError):
            PathMap({"~": "/root"})

    def test_dotdot_value_rejected(self):
        with self.assertRaises(PathMapError):
            PathMap({"~": "root/../evil"})

    def test_duplicate_value_rejected(self):
        with self.assertRaises(PathMapError):
            PathMap({"~": "root", "/home/u": "root"})

    def test_relative_key_rejected(self):
        with self.assertRaises(PathMapError):
            PathMap({"data": "d"})

    def test_root_mapping_rejected(self):
        with self.assertRaises(PathMapError):
            PathMap({"/": "all"})

    def test_default_uses_real_home(self):
        pm = PathMap()
        self.assertEqual(pm.to_repo(home_dir() + "/x"),
                         "source/__user_home__/x")


if __name__ == "__main__":
    unittest.main()
