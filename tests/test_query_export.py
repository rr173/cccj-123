"""Tests for prefix queries, index diffs and verified single-member export."""

import gzip
import os
import tempfile
import unittest
import zipfile

from archindex import (
    CorruptArchiveError,
    DuplicateMemberError,
    UnsupportedArchiveError,
    Limits,
    diff_indices,
    export_member,
    find_by_prefix,
    index_archive,
    index_tar,
    index_zip,
)
from tests.archive_builders import (
    build_tar,
    build_zip,
    build_zip_with_duplicates,
    mutate_stored_data,
)


class PrefixQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-q-")
        self.path = os.path.join(self.tmp, "q.zip")
        build_zip(self.path, {
            "a/b/f1": b"1",
            "a/b/f2": b"22",
            "a/bc": b"3",
            "top": b"4",
        })
        self.result = index_zip(self.path)

    def test_prefix_boundaries(self):
        self.assertEqual(
            sorted(m.name for m in find_by_prefix(self.result, "a/b")),
            ["a/b/f1", "a/b/f2"],
        )
        # Exact name match included; similar sibling excluded.
        self.assertEqual(
            [m.name for m in find_by_prefix(self.result, "a/bc")],
            ["a/bc"],
        )
        self.assertEqual(
            [m.name for m in find_by_prefix(self.result, "top")],
            ["top"],
        )
        self.assertEqual(find_by_prefix(self.result, "missing"), [])


class IndexDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-d-")
        self.left_p = os.path.join(self.tmp, "l.zip")
        self.right_p = os.path.join(self.tmp, "r.zip")
        build_zip(self.left_p, {
            "same": b"identical",
            "changed": b"before",
            "only_left": b"x",
        })
        build_zip(self.right_p, {
            "same": b"identical",
            "changed": b"after-different",
            "only_right": b"y",
        })
        self.left = index_zip(self.left_p)
        self.right = index_zip(self.right_p)

    def test_diff_categories(self):
        diff = diff_indices(self.left, self.right)
        self.assertEqual(diff.only_left, ["only_left"])
        self.assertEqual(diff.only_right, ["only_right"])
        self.assertEqual(len(diff.changed), 1)
        entry = diff.changed[0]
        self.assertEqual(entry["name"], "changed")
        self.assertIn("size", entry["reasons"])
        self.assertIn("digest", entry["reasons"])
        self.assertFalse(diff.identical)

    def test_identical_indices(self):
        other = index_zip(self.left_p)
        self.assertTrue(diff_indices(self.left, other).identical)

    def test_cross_format_diff_zip_vs_tar(self):
        tar_p = os.path.join(self.tmp, "l.tar")
        build_tar(tar_p, {"same": b"identical", "changed": b"before",
                          "only_left": b"x"})
        tar_index = index_tar(tar_p)
        diff = diff_indices(self.left, tar_index)
        self.assertTrue(diff.identical)
        self.assertEqual(diff.left_format, "zip")
        self.assertEqual(diff.right_format, "tar")

    def test_occurrence_count_change_detected(self):
        p1 = os.path.join(self.tmp, "d1.zip")
        p2 = os.path.join(self.tmp, "d2.zip")
        build_zip_with_duplicates(p1, [("d", b"a"), ("d", b"b")])
        build_zip(p2, {"d": b"a"})
        diff = diff_indices(index_zip(p1), index_zip(p2))
        self.assertIn("occurrence-count", diff.changed[0]["reasons"])


class ExportZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-ez-")
        self.archive = os.path.join(self.tmp, "src.zip")
        self.payloads = {
            "a/b/hello.txt": b"hello world\n",
            "a/other.bin": b"\x00\x01\x02\xff",
            "dir/": b"",
        }
        build_zip(self.archive, self.payloads)
        self.result = index_zip(self.archive)
        self.out = os.path.join(self.tmp, "out")

    def test_export_roundtrip_and_layout(self):
        out_path = export_member(self.result, "a/b/hello.txt", self.out)
        self.assertTrue(os.path.isfile(out_path))
        with open(out_path, "rb") as fh:
            self.assertEqual(fh.read(), b"hello world\n")
        # Containment: output stays under dest_dir.
        self.assertEqual(
            os.path.commonpath([os.path.realpath(self.out),
                                os.path.realpath(out_path)]),
            os.path.realpath(self.out),
        )

    def test_export_refuses_directory_and_overwrite(self):
        with self.assertRaises(UnsupportedArchiveError):
            export_member(self.result, "dir", self.out)
        first = export_member(self.result, "a/other.bin", self.out)
        with self.assertRaises(FileExistsError):
            export_member(self.result, "a/other.bin", self.out)
        again = export_member(self.result, "a/other.bin", self.out,
                              overwrite=True)
        self.assertEqual(again, first)

    def test_export_containment_against_intermediate_symlink(self):
        # A symlinked intermediate directory trying to escape the root must
        # be caught by realpath-based containment.
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        rooted = os.path.join(self.tmp, "rooted")
        os.makedirs(os.path.join(rooted, "a"))
        os.symlink(outside, os.path.join(rooted, "a", "b"))
        from archindex.errors import UnsafePathError as UPE
        with self.assertRaises(UPE):
            export_member(self.result, "a/b/hello.txt", rooted)
        self.assertEqual(os.listdir(outside), [])

    def test_ambiguous_export_needs_occurrence(self):
        p = os.path.join(self.tmp, "dup.zip")
        build_zip_with_duplicates(p, [("d", b"one"), ("d", b"two")])
        idx = index_zip(p)
        with self.assertRaises(DuplicateMemberError):
            export_member(idx, "d", self.out)
        path = export_member(idx, "d", self.out, occurrence=1)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"two")

    def test_digest_change_blocks_export(self):
        # Index first, then tamper the archive on disk: the re-validation
        # during export must refuse to write anything.
        before = export_member(self.result, "a/b/hello.txt", self.out,
                               overwrite=True)
        os.unlink(before)
        mutate_stored_data(self.archive, payload=b"XXXXXXXXXXX")
        with self.assertRaises(CorruptArchiveError):
            export_member(self.result, "a/b/hello.txt", self.out)
        # Nothing was left behind in the destination tree.
        self.assertFalse(
            os.path.exists(os.path.join(self.out, "a", "b", "hello.txt"))
        )

    def test_size_change_blocks_export(self):
        # Replace the archive with a longer payload under the same name.
        build_zip(self.archive, {"a/b/hello.txt": b"hello world\n" * 3})
        with self.assertRaises(CorruptArchiveError):
            export_member(self.result, "a/b/hello.txt", self.out)
        # The failed verification leaves no destination file behind.
        self.assertFalse(
            os.path.exists(os.path.join(self.out, "a", "b", "hello.txt"))
        )


class ExportTarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-et-")
        self.archive = os.path.join(self.tmp, "src.tar")
        build_tar(self.archive, {"a/f.txt": b"tar-payload",
                                  "a/g.txt": b"second"})
        self.result = index_tar(self.archive)
        self.out = os.path.join(self.tmp, "out")

    def test_export_tar_member(self):
        path = export_member(self.result, "a/f.txt", self.out)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"tar-payload")

    def test_export_from_compressed_tar(self):
        gz = os.path.join(self.tmp, "src.tar.gz")
        with open(self.archive, "rb") as fi, gzip.open(gz, "wb") as fo:
            fo.write(fi.read())
        idx = index_tar(gz, Limits(max_compression_ratio=100_000))
        path = export_member(idx, "a/g.txt", self.out + "-gz")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"second")

    def test_link_export_refused_by_default(self):
        from tests.archive_builders import build_tar_with_links
        p = os.path.join(self.tmp, "links.tar")
        build_tar_with_links(p, [
            ("file", "t", b"body"),
            ("symlink", "l", "t"),
        ])
        idx = index_tar(p)
        with self.assertRaises(UnsupportedArchiveError):
            export_member(idx, "l", self.out)


class UnifiedEntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-u-")

    def test_sniff_and_index(self):
        zp = os.path.join(self.tmp, "a.zip")
        tp = os.path.join(self.tmp, "a.tar")
        build_zip(zp, {"f": b"z"})
        build_tar(tp, {"f": b"t"})
        self.assertEqual(index_archive(zp).format, "zip")
        self.assertEqual(index_archive(tp).format, "tar")


if __name__ == "__main__":
    unittest.main()
