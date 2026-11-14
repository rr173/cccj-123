"""TAR-specific indexer tests with synthesised archives."""

import gzip
import os
import tarfile
import tempfile
import unittest

from archindex import (
    CorruptArchiveError,
    LimitExceededError,
    Limits,
    UnsupportedArchiveError,
    UnsafePathError,
    index_tar,
)
from tests.archive_builders import (
    build_gbk_tar,
    build_tar,
    build_tar_with_links,
    tamper_tar_header,
    temp_archive,
    truncate_file,
)


class TarIndexBasicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-tar-")
        self.path = os.path.join(self.tmp, "a.tar")
        build_tar(
            self.path,
            {"README": b"hello\n", "src/main.py": b"print(1)\n",
             "zero": b""},
            dirs=("src",),
        )
        self.result = index_tar(self.path)

    def test_members_depth_and_kinds(self):
        by = {m.name: m for m in self.result.members}
        self.assertEqual(list(by), ["src", "README", "src/main.py", "zero"])
        self.assertEqual(by["src"].kind, "dir")
        self.assertTrue(by["src"].is_dir)
        self.assertEqual(by["src/main.py"].depth, 1)
        self.assertEqual(by["README"].depth, 0)

    def test_offsets_and_plain_tar_sizes(self):
        rec = self.result.get("README")
        self.assertEqual(rec.size, 6)
        # Plain TAR: stored length equals member length.
        self.assertEqual(rec.compressed_size, 6)
        self.assertIsNotNone(rec.offset)
        self.assertIsNotNone(rec.data_offset)
        self.assertGreaterEqual(rec.data_offset, rec.offset)
        self.assertIsNotNone(rec.sha256)
        self.assertIsNotNone(rec.crc32)


class DuplicateTarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-tardup-")
        self.path = os.path.join(self.tmp, "dup.tar")
        with tarfile.open(self.path, "w") as tar:
            data = [b"v1", b"v2-longer", b"v3"]
            for i, payload in enumerate(data):
                info = tarfile.TarInfo("dup")
                info.size = len(payload)
                info.mode = 0o644
                import io
                tar.addfile(info, io.BytesIO(payload))
        self.result = index_tar(self.path)

    def test_duplicates_flagged_in_order(self):
        dups = self.result.occurrences("dup")
        self.assertEqual([d.occurrence for d in dups], [0, 1, 2])
        self.assertTrue(all(d.ambiguous for d in dups))
        self.assertEqual([d.size for d in dups], [2, 9, 2])


class LinkMembersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-links-")
        self.path = os.path.join(self.tmp, "links.tar")
        build_tar_with_links(self.path, [
            ("dir", "dir", None),
            ("file", "dir/target.txt", b"data"),
            ("symlink", "dir/rel-link", "target.txt"),
            ("hardlink", "dir/hard", "dir/target.txt"),
        ])

    def test_safe_links_recorded_with_resolved_targets(self):
        result = index_tar(self.path)
        link = result.get("dir/rel-link")
        self.assertEqual(link.kind, "symlink")
        self.assertEqual(link.link_target, "target.txt")
        self.assertEqual(link.resolved_target, "dir/target.txt")
        hard = result.get("dir/hard")
        self.assertEqual(hard.kind, "hardlink")
        self.assertEqual(hard.resolved_target, "dir/target.txt")

    def test_absolute_and_escaping_links_rejected(self):
        bad1 = os.path.join(self.tmp, "abs-link.tar")
        build_tar_with_links(bad1, [
            ("symlink", "x", "/etc/passwd"),
        ])
        with self.assertRaises(UnsafePathError):
            index_tar(bad1)

        bad2 = os.path.join(self.tmp, "esc-link.tar")
        build_tar_with_links(bad2, [
            ("symlink", "a/b/link", "../../../etc/shadow"),
        ])
        with self.assertRaises(UnsafePathError):
            index_tar(bad2)

    def test_reject_links_policy(self):
        with self.assertRaises(UnsafePathError):
            index_tar(self.path, Limits(reject_links=True))

    def test_dotdot_link_and_nul_target_rejected(self):
        # A link whose target resolves exactly to the archive parent.
        p = os.path.join(self.tmp, "dotdot.tar")
        build_tar_with_links(p, [("symlink", "a/link", "..")])
        with self.assertRaises(UnsafePathError):
            index_tar(p)

    def test_link_pointing_at_sibling_file_resolves(self):
        p = os.path.join(self.tmp, "sib.tar")
        build_tar_with_links(p, [
            ("file", "dir/target.txt", b"body"),
            ("symlink", "dir/alias", "target.txt"),
        ])
        result = index_tar(p)
        self.assertEqual(
            result.get("dir/alias").resolved_target, "dir/target.txt")


class SpecialFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-special-")
        self.path = os.path.join(self.tmp, "sp.tar")
        build_tar_with_links(self.path, [
            ("special", "dev/null", None),
            ("file", "ok", b"x"),
        ])

    def test_special_rejected_by_default_allowed_when_disabled(self):
        with self.assertRaises(UnsupportedArchiveError):
            index_tar(self.path)
        result = index_tar(self.path, Limits(reject_special_files=False))
        self.assertEqual(result.get("dev/null").kind, "special")
        self.assertEqual(result.get("ok").kind, "file")


class UnsafeTarNamesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-tarunsafe-")

    def test_absolute_and_traversal_names(self):
        path = os.path.join(self.tmp, "abs.tar")
        build_tar(path, {"/etc/passwd": b"x"})
        with self.assertRaises(UnsafePathError):
            index_tar(path)

        path2 = os.path.join(self.tmp, "trav.tar")
        build_tar(path2, {"../escape": b"x"})
        with self.assertRaises(UnsafePathError):
            index_tar(path2)


class TarLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-tarlim-")

    def test_member_and_depth_limits(self):
        path = os.path.join(self.tmp, "many.tar")
        build_tar(path, {f"f{i}": b"x" for i in range(6)})
        with self.assertRaises(LimitExceededError):
            index_tar(path, Limits(max_members=3))

        path2 = os.path.join(self.tmp, "deep.tar")
        deep = "/".join(f"d{i}" for i in range(5)) + "/f"
        build_tar(path2, {deep: b"x"})
        with self.assertRaises(LimitExceededError):
            index_tar(path2, Limits(max_depth=2))

    def test_gzip_bomb_ratio_limit(self):
        # 2 MiB of zeros gzips to ~2 KiB on disk (tar headers included).
        src = os.path.join(self.tmp, "bomb.tar")
        payload = b"\x00" * (2 * 1024 * 1024)
        build_tar(src, {"z": payload})
        gz = os.path.join(self.tmp, "bomb.tar.gz")
        with open(src, "rb") as fi, gzip.open(gz, "wb", compresslevel=9) as fo:
            fo.write(fi.read())
        on_disk = os.path.getsize(gz)
        expanded = os.path.getsize(src)  # closest on-disk proxy for contents
        achieved = expanded // max(on_disk, 1)
        trigger = max(100, achieved // 2)
        with self.assertRaises(LimitExceededError):
            index_tar(gz, Limits(max_compression_ratio=trigger))
        result = index_tar(gz, Limits(max_compression_ratio=achieved * 2,
                                      max_member_size=1024 ** 3,
                                      max_total_size=1024 ** 3))
        self.assertEqual(result.metadata["compression"], "gz")
        self.assertGreater(expanded, trigger * on_disk)
        # Per-member compressed size is not available in a gzip stream.
        self.assertIsNone(result.get("z").compressed_size)
        self.assertIsNotNone(result.get("z").offset)


class TarEncodingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-tarenc-")

    def test_gbk_tar_decoded_with_explicit_encoding(self):
        path = os.path.join(self.tmp, "gbk.tar")
        build_gbk_tar(path, {"目录/文件.txt": "内容".encode("gbk")})
        result = index_tar(path, encoding="gbk")
        self.assertIn("目录/文件.txt", result.by_name)

    def test_utf8_tar_default(self):
        path = os.path.join(self.tmp, "u8.tar")
        build_tar(path, {"café.txt": "naïve".encode()})
        result = index_tar(path)
        self.assertIn("café.txt", result.by_name)


class CorruptTarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ax-tarcorrupt-")

    def test_bad_header_checksum(self):
        path = os.path.join(self.tmp, "bad.tar")
        build_tar(path, {"a": b"hello"})
        tamper_tar_header(path)
        with self.assertRaises(CorruptArchiveError):
            index_tar(path)

    def test_truncated_tar(self):
        path = os.path.join(self.tmp, "trunc.tar")
        build_tar(path, {"a": b"x" * 4096, "b": b"y" * 4096})
        truncate_file(path, 0.3)
        with self.assertRaises(CorruptArchiveError):
            index_tar(path)

    def test_truncated_gzip_tar(self):
        src = os.path.join(self.tmp, "x.tar")
        build_tar(src, {"a": b"z" * 8192})
        gz = os.path.join(self.tmp, "x.tar.gz")
        with open(src, "rb") as fi, gzip.open(gz, "wb") as fo:
            fo.write(fi.read())
        truncate_file(gz, 0.4)
        with self.assertRaises(CorruptArchiveError):
            index_tar(gz)


if __name__ == "__main__":
    unittest.main()
