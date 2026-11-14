"""ZIP-specific indexer tests with synthesised archives."""

import os
import tempfile
import unittest
import zipfile

from archindex import (
    CorruptArchiveError,
    LimitExceededError,
    Limits,
    UnsafePathError,
    find_by_prefix,
    index_zip,
)
from tests.archive_builders import (
    build_zip,
    build_zip_with_duplicates,
    corrupt_central_crc,
    flip_utf8_and_reencode,
    mutate_stored_data,
    truncate_file,
    write_random_bytes,
    temp_archive,
)


class ZipIndexBasicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-zip-")
        self.path = os.path.join(self.tmp, "a.zip")
        build_zip(self.path, {
            "README": b"hello\n",
            "src/main.py": b"print(1)\n",
            "src/lib/util.py": b"def f(): pass\n",
            "empty/": b"",
            "zero.bin": b"",
        })
        self.result = index_zip(self.path)

    def test_member_count_and_order(self):
        names = [m.name for m in self.result.members]
        self.assertEqual(
            names,
            ["README", "src/main.py", "src/lib/util.py",
             "empty", "zero.bin"],
        )
        self.assertEqual(len(self.result), 5)

    def test_depth_recorded(self):
        by = {m.name: m for m in self.result.members}
        self.assertEqual(by["README"].depth, 0)
        self.assertEqual(by["src/main.py"].depth, 1)
        self.assertEqual(by["src/lib/util.py"].depth, 2)
        self.assertTrue(by["empty"].is_dir)
        self.assertEqual(by["empty"].kind, "dir")

    def test_sizes_offsets_digests(self):
        rec = self.result.get("README")
        self.assertEqual(rec.size, 6)
        self.assertEqual(rec.compressed_size, 6)  # STORED
        self.assertIsNotNone(rec.offset)
        self.assertIsNotNone(rec.data_offset)
        self.assertGreater(rec.data_offset, rec.offset)
        self.assertIsNotNone(rec.sha256) and self.assertEqual(len(rec.sha256), 64)
        self.assertIsNotNone(rec.crc32)
        self.assertFalse(rec.ambiguous)

    def test_prefix_query(self):
        names = [m.name for m in find_by_prefix(self.result, "src")]
        self.assertEqual(names, ["src/lib/util.py", "src/main.py"])
        names = [m.name for m in find_by_prefix(self.result, "src/")]
        self.assertEqual(names, ["src/lib/util.py", "src/main.py"])
        # Prefix must not match sibling names like "srcX".
        self.assertEqual(find_by_prefix(self.result, "zero.bi"), [])

    def test_to_dict_serializable(self):
        payload = self.result.to_dict()
        self.assertEqual(payload["format"], "zip")
        self.assertIsInstance(payload["members"], list)
        self.assertEqual(payload["members"][0]["crc32"],
                         f"{self.result.members[0].crc32:08x}")


class DuplicatePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-dup-")
        self.path = os.path.join(self.tmp, "dup.zip")
        build_zip_with_duplicates(self.path, [
            ("a/first", b"1"),
            ("a/dup", b"v1"),
            ("a/dup", b"v2-longer"),
            ("a/dup", b"v3"),
            ("a/other", b"x"),
        ])
        self.result = index_zip(self.path)

    def test_occurrence_order_and_ambiguity(self):
        dups = self.result.occurrences("a/dup")
        self.assertEqual([d.occurrence for d in dups], [0, 1, 2])
        self.assertTrue(all(d.ambiguous for d in dups))
        self.assertIn("a/dup", self.result.ambiguous_names)
        self.assertNotIn("a/first", self.result.ambiguous_names)
        # Full member ordering is preserved.
        self.assertEqual(
            [m.name for m in self.result.members],
            ["a/first", "a/dup", "a/dup", "a/dup", "a/other"],
        )

    def test_get_requires_occurrence_for_ambiguous(self):
        from archindex import DuplicateMemberError
        with self.assertRaises(DuplicateMemberError):
            self.result.get("a/dup")
        self.assertEqual(self.result.get("a/dup", occurrence=1).size, 9)

    def test_each_duplicate_has_own_digest(self):
        dups = self.result.occurrences("a/dup")
        self.assertEqual(len({d.sha256 for d in dups}), 3)


class UnsafeZipNamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-unsafe-")

    def test_traversal_member_rejected(self):
        path = os.path.join(self.tmp, "trav.zip")
        build_zip(path, {"../evil": b"x", "ok": b"y"})
        with self.assertRaises(UnsafePathError):
            index_zip(path)

    def test_absolute_member_rejected(self):
        path = os.path.join(self.tmp, "abs.zip")
        build_zip(path, {"/etc/passwd": b"x"})
        with self.assertRaises(UnsafePathError):
            index_zip(path)

    def test_nested_traversal_rejected(self):
        path = os.path.join(self.tmp, "nt.zip")
        build_zip(path, {"a/../../escape": b"x"})
        with self.assertRaises(UnsafePathError):
            index_zip(path)


class ZipBombLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-bomb-")

    def test_member_count_limit(self):
        path = os.path.join(self.tmp, "many.zip")
        build_zip(path, {f"f{i:04d}": b"x" for i in range(10)})
        with self.assertRaises(LimitExceededError):
            index_zip(path, Limits(max_members=5))

    def test_depth_limit(self):
        path = os.path.join(self.tmp, "deep.zip")
        build_zip(path, {"/".join(f"d{i}" for i in range(6)): b"x"})
        with self.assertRaises(LimitExceededError):
            index_zip(path, Limits(max_depth=3))

    def test_single_member_size_limit(self):
        path = os.path.join(self.tmp, "big.zip")
        build_zip(path, {"big": b"a" * 1024})
        with self.assertRaises(LimitExceededError):
            index_zip(path, Limits(max_member_size=512))

    def test_total_size_limit(self):
        path = os.path.join(self.tmp, "tot.zip")
        build_zip(path, {f"f{i}": b"a" * 400 for i in range(4)})
        with self.assertRaises(LimitExceededError):
            index_zip(path, Limits(max_total_size=1000))

    def test_compression_ratio_bomb(self):
        # 200 KiB of zeros deflates to roughly 200 bytes (>500x ratio).
        path = os.path.join(self.tmp, "bomb.zip")
        build_zip(path, {"z": b"\x00" * (200 * 1024)},
                  compression=zipfile.ZIP_DEFLATED)
        with self.assertRaises(LimitExceededError):
            index_zip(path, Limits(max_compression_ratio=100))
        # Relaxed limits index it fine and record both lengths.
        result = index_zip(path, Limits(max_compression_ratio=10_000,
                                        max_member_size=1024 ** 3))
        rec = result.get("z")
        self.assertGreater(rec.size, 100 * rec.compressed_size)

    def test_compressed_size_limit(self):
        path = os.path.join(self.tmp, "cs.zip")
        build_zip(path, {"z": b"abc" * 50},
                  compression=zipfile.ZIP_DEFLATED)
        with self.assertRaises(LimitExceededError):
            index_zip(path, Limits(max_member_compressed_size=1))

    def test_streaming_cap_below_declared_size(self):
        # Header claims 1024 bytes but the configured cap is smaller: the
        # streaming reader must stop the member rather than trust the header.
        path = os.path.join(self.tmp, "cap.zip")
        build_zip(path, {"z": b"q" * 4096})
        with self.assertRaises(LimitExceededError):
            index_zip(path, Limits(max_member_size=1024,
                                   max_total_size=1024 ** 3))


class EncodedNameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-enc-")

    def test_gbk_named_zip_without_utf8_flag(self):
        path = os.path.join(self.tmp, "gbk.zip")
        build_zip(path, {"文档/说明.txt": "内容".encode("gbk"),
                          "plain.txt": b"p"})
        flip_utf8_and_reencode(path, "gbk")

        # Default decoding cannot know GBK: explicit metadata_encoding works.
        result = index_zip(path, metadata_encoding="gbk")
        self.assertIn("文档/说明.txt", result.by_name)
        rec = result.get("文档/说明.txt")
        self.assertIn("gbk", (rec.encoding_note or ""))
        self.assertEqual(rec.depth, 1)

    def test_utf8_flagged_name_still_works(self):
        path = os.path.join(self.tmp, "u8.zip")
        build_zip(path, {"café/ñoño.txt": b"x"})
        result = index_zip(path)
        self.assertIn("café/ñoño.txt", result.by_name)
        self.assertIsNone(result.get("café/ñoño.txt").encoding_note)

    def test_legacy_encoding_fallback_without_hint(self):
        path = os.path.join(self.tmp, "gbk2.zip")
        build_zip(path, {"文档.txt": b"x"})
        flip_utf8_and_reencode(path, "gbk")
        # No encoding hint: must not raise; name recovery is best-effort.
        result = index_zip(path)
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result.members[0].encoding_note)


class CorruptZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-corrupt-")

    def test_bad_crc_detected(self):
        path = os.path.join(self.tmp, "badcrc.zip")
        build_zip(path, {"a": b"hello", "b": b"world"})
        corrupt_central_crc(path)
        with self.assertRaises(CorruptArchiveError):
            index_zip(path)

    def test_tampered_stored_content_detected(self):
        path = os.path.join(self.tmp, "tamper.zip")
        build_zip(path, {"a": b"hello"})
        mutate_stored_data(path, payload=b"HELLO")
        with self.assertRaises(CorruptArchiveError):
            index_zip(path)

    def test_truncated_zip_detected(self):
        path = os.path.join(self.tmp, "trunc.zip")
        build_zip(path, {"a": b"hello", "b": b"world" * 100})
        truncate_file(path, 0.4)
        with self.assertRaises(CorruptArchiveError):
            index_zip(path)

    def test_random_file_is_unsupported(self):
        path = os.path.join(self.tmp, "rand.bin")
        write_random_bytes(path)
        from archindex import UnsupportedArchiveError, index_archive
        with self.assertRaises((UnsupportedArchiveError, CorruptArchiveError)):
            index_archive(path)

    def test_missing_file(self):
        with self.assertRaises(CorruptArchiveError):
            index_zip(os.path.join(self.tmp, "nope.zip"))


if __name__ == "__main__":
    unittest.main()
