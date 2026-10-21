import os
import tempfile
import unittest
from pathlib import Path

from ai_config_sync import sync_list


class ParseTest(unittest.TestCase):
    def test_non_absolute_rejected(self):
        with self.assertRaises(sync_list.SyncListError):
            sync_list.parse("root/.config/**\n")

    def test_root_rejected(self):
        with self.assertRaises(sync_list.SyncListError):
            sync_list.parse("/\n")

    def test_negation_and_order(self):
        rules = sync_list.parse("/a/**\n!/a/x.tmp\n")
        self.assertTrue(sync_list.match_path(rules, "/a/y"))
        self.assertFalse(sync_list.match_path(rules, "/a/x.tmp"))
        self.assertFalse(sync_list.match_path(rules, "/b/y"))

    def test_reinclude_wins_when_later(self):
        rules = sync_list.parse("/a/**\n!/a/*.tmp\n/a/keep.tmp\n")
        self.assertTrue(sync_list.match_path(rules, "/a/keep.tmp"))
        self.assertFalse(sync_list.match_path(rules, "/a/other.tmp"))

    def test_literal_dir_subtree(self):
        rules = sync_list.parse("/a/b\n")
        self.assertTrue(sync_list.match_path(rules, "/a/b/c/d"))
        self.assertFalse(sync_list.match_path(rules, "/a/bx"))

    def test_double_star_middle(self):
        rules = sync_list.parse("/a/**/x.md\n")
        self.assertTrue(sync_list.match_path(rules, "/a/x.md"))
        self.assertTrue(sync_list.match_path(rules, "/a/p/q/x.md"))
        self.assertFalse(sync_list.match_path(rules, "/a/p/y.md"))


class SymlinkDeclTest(unittest.TestCase):
    def test_decl_parsed_and_last_wins(self):
        rules = sync_list.parse(
            "/a/link -> /a/target1\n/a/link -> /a/target2\n")
        decls = sync_list.symlink_decls(rules)
        self.assertEqual(decls, {"/a/link": "/a/target2"})

    def test_decl_not_matched_as_file_rule(self):
        # 声明行不参与文件匹配
        rules = sync_list.parse("/a/link -> /a/target\n")
        self.assertFalse(sync_list.match_path(rules, "/a/link"))

    def test_decl_must_be_absolute(self):
        with self.assertRaises(sync_list.SyncListError):
            sync_list.parse("link -> /a/target\n")
        with self.assertRaises(sync_list.SyncListError):
            sync_list.parse("/a/link -> target\n")

    def test_decl_rejects_wildcards(self):
        with self.assertRaises(sync_list.SyncListError):
            sync_list.parse("/a/li*nk -> /a/target\n")

    def test_decl_to_forbidden_root_rejected(self):
        rules = sync_list.parse("/a/link -> /a/t\n")
        decls = sync_list.symlink_decls(rules)
        with self.assertRaises(sync_list.SyncListError):
            sync_list.check_symlink_decls({"/proc/link": "/a/t"}, [])
        with self.assertRaises(sync_list.SyncListError):
            sync_list.check_symlink_decls({"/x/.git/link": "/a/t"}, [])
        sync_list.check_symlink_decls(decls, [])  # 合法声明不抛错

    def test_decl_in_hard_zone_rejected_at_expand(self):
        zone = "/etc/ai-config-sync"
        rules = sync_list.parse("{}/x -> /a/t\n".format(zone))
        with self.assertRaises(sync_list.SyncListError):
            sync_list.expand(rules, [zone])

    def test_declared_path_excluded_from_files(self):
        base = Path(tempfile.mkdtemp())
        try:
            target = base / "real.txt"
            target.write_text("x")
            link = base / "link.txt"
            os.symlink(str(target), str(link))
            rules = sync_list.parse(
                "{b}/**\n{l} -> {t}\n".format(b=base, l=link, t=target))
            exp = sync_list.expand(rules, [])
            self.assertIn(str(target), exp.files)
            self.assertNotIn(str(link), exp.files)
        finally:
            os.unlink(link)
            import shutil
            shutil.rmtree(base)


class ExpandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.zone = self.base / "zone"
        (self.zone / "inner").mkdir(parents=True)
        (self.zone / "inner" / "f").write_text("z")
        self.tree = self.base / "tree"
        (self.tree / ".git").mkdir(parents=True)
        (self.tree / ".git" / "config").write_text("g")
        (self.tree / "sub").mkdir(parents=True)
        (self.tree / "a.md").write_text("a")
        (self.tree / "b.tmp").write_text("b")
        (self.tree / "sub" / "c.md").write_text("c")
        (self.tree / "link.md").symlink_to(self.tree / "a.md")

    def tearDown(self):
        self.tmp.cleanup()

    def _rules(self, pattern):
        return sync_list.parse(pattern.format(t=self.tree))

    def test_expand_filters_and_negates(self):
        rules = self._rules("{t}/**\n!{t}/*.tmp\n")
        exp = sync_list.expand(rules, [str(self.zone)])
        files = set(exp.files)
        self.assertIn(str(self.tree / "a.md"), files)
        self.assertIn(str(self.tree / "sub" / "c.md"), files)
        self.assertNotIn(str(self.tree / "b.tmp"), files)
        self.assertNotIn(str(self.tree / ".git" / "config"), files)
        self.assertNotIn(str(self.tree / "link.md"), files)  # 软链接跳过
        self.assertEqual(exp.files[str(self.tree / "a.md")],
                         "source" + str(self.tree / "a.md"))

    def test_rule_targeting_hard_zone_rejected(self):
        rules = sync_list.parse("{z}/**\n".format(z=self.zone))
        with self.assertRaises(sync_list.SyncListError):
            sync_list.expand(rules, [str(self.zone)])

    def test_broad_rule_covers_zone_filtered(self):
        rules = sync_list.parse("{}/**\n".format(self.base))
        exp = sync_list.expand(rules, [str(self.zone)])
        self.assertNotIn(str(self.zone / "inner" / "f"), set(exp.files))
        self.assertTrue(any("skipped" in w or "跳过" in w
                            for w in exp.warnings))

    def test_git_rule_rejected(self):
        rules = self._rules("{t}/.git/**\n")
        with self.assertRaises(sync_list.SyncListError):
            sync_list.expand(rules, [])

    def test_forbidden_root_rejected(self):
        rules = sync_list.parse("/proc/**\n")
        with self.assertRaises(sync_list.SyncListError):
            sync_list.expand(rules, [])


if __name__ == "__main__":
    unittest.main()
