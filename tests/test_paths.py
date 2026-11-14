"""Tests for normalisation, traversal rejection and link-target safety."""

import unittest

from archindex.errors import UnsafePathError
from archindex.pathsan import depth_of, normalize_member_name, resolve_link_target


class NormalizeTest(unittest.TestCase):
    def test_regular_paths_normalized(self):
        self.assertEqual(normalize_member_name("a/b/c.txt"), "a/b/c.txt")
        self.assertEqual(normalize_member_name("./a//b/./c"), "a/b/c")
        self.assertEqual(normalize_member_name("a/b/"), "a/b")

    def test_depth(self):
        self.assertEqual(depth_of("a"), 0)
        self.assertEqual(depth_of("a/b"), 1)
        self.assertEqual(depth_of("a/b/c"), 3 - 1)
        self.assertEqual(depth_of("a/b/c/"), 2)

    def test_absolute_rejected(self):
        for bad in ("/etc/passwd", "//host/share", "/", "///x"):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafePathError):
                    normalize_member_name(bad)

    def test_drive_letter_rejected(self):
        with self.assertRaises(UnsafePathError):
            normalize_member_name("C:Windows/system32")

    def test_traversal_rejected(self):
        for bad in ("../secret", "a/../../b", "..", "a/../.."):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafePathError):
                    normalize_member_name(bad)

    def test_internal_dotdot_that_stays_inside(self):
        # a/../b collapses to b and is safe
        self.assertEqual(normalize_member_name("a/../b"), "b")

    def test_backslash_and_empty(self):
        with self.assertRaises(UnsafePathError):
            normalize_member_name("")
        with self.assertRaises(UnsafePathError):
            normalize_member_name("   ")
        # Backslash stays literal, must not become an escaping separator.
        self.assertEqual(normalize_member_name("a\\b"), "a\\b")
        with self.assertRaises(UnsafePathError):
            normalize_member_name("..\\..\\x")

    def test_nul_and_control_chars(self):
        with self.assertRaises(UnsafePathError):
            normalize_member_name("a\x00b")
        with self.assertRaises(UnsafePathError):
            normalize_member_name("a\x01b")


class LinkTargetTest(unittest.TestCase):
    def test_relative_link_inside_root(self):
        resolved, _ = resolve_link_target(
            "dir/link", "../dir2/file", known_dirs=set())
        self.assertEqual(resolved, "dir2/file")

    def test_absolute_link_rejected(self):
        with self.assertRaises(UnsafePathError):
            resolve_link_target("dir/link", "/etc/passwd", set())
        with self.assertRaises(UnsafePathError):
            resolve_link_target("link", "C:/Windows", set())

    def test_escaping_relative_link_rejected(self):
        with self.assertRaises(UnsafePathError):
            resolve_link_target("a/b/link", "../../../etc", set())
        with self.assertRaises(UnsafePathError):
            resolve_link_target("link", "..", set())

    def test_directory_marker(self):
        resolved, is_dir = resolve_link_target(
            "dir/link", "sub", known_dirs={"dir/sub"})
        self.assertEqual(resolved, "dir/sub")
        self.assertTrue(is_dir)


if __name__ == "__main__":
    unittest.main()
