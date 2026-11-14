"""Archive indexing, querying, diffing and verified single-member export."""

from __future__ import annotations

import os
import tarfile
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field

from .errors import (
    CorruptArchiveError,
    DuplicateMemberError,
    LimitExceededError,
    UnsupportedArchiveError,
    UnsafePathError,
)
from .hashing import stream_digest, stream_digest_and_copy
from .pathsan import depth_of, normalize_member_name, safe_join, resolve_link_target
from .records import IndexResult, Limits, MemberRecord

# ---------------------------------------------------------------------------
# Small internal helpers
# ---------------------------------------------------------------------------


def _decode_zip_name(
    info: zipfile.ZipInfo, metadata_encoding: str | None
) -> tuple[str, str | None]:
    """Recover a usable text name from a ZIP central-directory entry."""
    if info.flag_bits & 0x800:
        return info.filename, None
    # Without the UTF-8 flag the bytes are encoded in some legacy code page.
    # ``info.filename`` may already contain real text (e.g. stdlib decoded
    # raw bytes directly); try to recover the original bytes defensively.
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        raw = info.filename.encode("latin-1", errors="replace")
    if metadata_encoding is not None:
        try:
            return raw.decode(metadata_encoding), f"decoded as {metadata_encoding}"
        except UnicodeDecodeError:
            return (
                raw.decode("utf-8", errors="replace"),
                f"undecodable as {metadata_encoding}; replacement chars used",
            )
    try:
        return raw.decode("utf-8"), "re-decoded as utf-8"
    except UnicodeDecodeError:
        return info.filename, "cp437 fallback (no declared encoding)"


def _data_offset_zip(info: zipfile.ZipInfo) -> int:
    # Local file header: 30 fixed bytes + filename + extra field.
    return info.header_offset + 30 + len(info.filename) + len(info.extra)


def _finalize(result: IndexResult, counts: dict[str, int]) -> None:
    """Assign occurrence numbers, ambiguity flags and the name lookup table."""
    by_name: dict[str, list[MemberRecord]] = {}
    ambiguous: set[str] = set()
    for record in result.members:
        by_name.setdefault(record.name, []).append(record)
    for name, records in by_name.items():
        if len(records) > 1:
            ambiguous.add(name)
            for record in records:
                object.__setattr__(record, "ambiguous", True)
    result.by_name = by_name
    result.ambiguous_names = ambiguous


def _check_ratio(stored: int, expanded: int, limit: float) -> None:
    if stored > 0 and expanded / stored > limit:
        raise LimitExceededError(
            f"compression ratio {expanded / stored:.1f}x exceeds limit "
            f"{limit:.1f}x (possible compression bomb)"
        )


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------


def index_zip(
    path: str,
    limits: Limits | None = None,
    *,
    metadata_encoding: str | None = None,
) -> IndexResult:
    """Index a ZIP/JAR/APK-style archive without extracting to disk."""
    limits = limits or Limits()
    result = IndexResult(path=path, format="zip", limits=limits)
    counts: dict[str, int] = {}
    total_expanded = 0
    total_stored = 0
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise CorruptArchiveError(f"cannot open zip archive {path!r}: {exc}") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_members:
            raise LimitExceededError(
                f"member count {len(infos)} exceeds limit {limits.max_members}"
            )
        for info in infos:
            if len(result.members) >= limits.max_members:
                raise LimitExceededError(
                    f"member count exceeds limit {limits.max_members}"
                )

            raw_name, note = _decode_zip_name(info, metadata_encoding)
            name = normalize_member_name(raw_name)
            if name == ".":
                continue
            depth = depth_of(name)
            if depth > limits.max_depth:
                raise LimitExceededError(
                    f"directory depth {depth} of {name!r} exceeds "
                    f"limit {limits.max_depth}"
                )

            is_dir = info.is_dir()
            if info.file_size > limits.max_member_size and not is_dir:
                raise LimitExceededError(
                    f"member {name!r} size {info.file_size} exceeds "
                    f"single-member limit {limits.max_member_size}"
                )
            if info.compress_size > limits.max_member_compressed_size:
                raise LimitExceededError(
                    f"member {name!r} stored size {info.compress_size} "
                    f"exceeds limit {limits.max_member_compressed_size}"
                )
            if not is_dir and info.compress_size > 0:
                _check_ratio(
                    info.compress_size, info.file_size,
                    limits.max_compression_ratio,
                )
            total_expanded += info.file_size
            total_stored += info.compress_size
            if total_expanded > limits.max_total_size:
                raise LimitExceededError(
                    f"total uncompressed size exceeds limit "
                    f"{limits.max_total_size}"
                )

            occurrence = counts.get(name, 0)
            counts[name] = occurrence + 1

            crc = sha = None
            if is_dir:
                kind = "dir"
            else:
                kind = "file"
                try:
                    with zf.open(info) as fh:
                        size, crc, sha = stream_digest(
                            fh,
                            limits.max_member_size,
                            expected_size=info.file_size,
                        )
                except zipfile.BadZipFile as exc:
                    # Raised on CRC mismatch, bad local headers, truncation.
                    raise CorruptArchiveError(
                        f"member {name!r} corrupt (CRC/header mismatch): {exc}"
                    ) from exc
                except zlib.error as exc:
                    raise CorruptArchiveError(
                        f"member {name!r} corrupt (decompression failed): {exc}"
                    ) from exc
                except ValueError as exc:
                    raise LimitExceededError(str(exc)) from exc
                if crc != (info.CRC & 0xFFFFFFFF):
                    raise CorruptArchiveError(
                        f"member {name!r} CRC mismatch: header "
                        f"{info.CRC:08x} != computed {crc:08x}"
                    )

            result.members.append(
                MemberRecord(
                    name=name,
                    occurrence=occurrence,
                    kind=kind,
                    is_dir=is_dir,
                    depth=depth,
                    size=0 if is_dir else info.file_size,
                    compressed_size=info.compress_size,
                    offset=info.header_offset,
                    data_offset=None if is_dir else _data_offset_zip(info),
                    crc32=crc,
                    sha256=sha,
                    link_target=None,
                    resolved_target=None,
                    ambiguous=False,
                    encoding_note=note,
                )
            )

        _check_ratio(total_stored, total_expanded, limits.max_compression_ratio)
        result.metadata = {
            "total_stored_size": total_stored,
            "total_uncompressed_size": total_expanded,
            "compression": "deflate" if any(
                i.compress_type == zipfile.ZIP_DEFLATED for i in infos
            ) else "store",
        }
    _finalize(result, counts)
    return result


# ---------------------------------------------------------------------------
# TAR (optionally gz / bz2 / xz compressed)
# ---------------------------------------------------------------------------

_TAR_SPECIAL = {
    tarfile.CHRTYPE: "char-device",
    tarfile.BLKTYPE: "block-device",
    tarfile.FIFOTYPE: "fifo",
}


def index_tar(
    path: str,
    limits: Limits | None = None,
    *,
    encoding: str | None = None,
) -> IndexResult:
    """Index a TAR archive (plain or compressed) without extracting."""
    limits = limits or Limits()
    result = IndexResult(path=path, format="tar", limits=limits)
    counts: dict[str, int] = {}
    known_dirs: set[str] = set()
    total_expanded = 0
    compression = None

    try:
        tf = tarfile.open(
            name=path,
            mode="r:*",
            encoding=encoding or "utf-8",
            errors="surrogateescape",
        )
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise CorruptArchiveError(f"cannot open tar archive {path!r}: {exc}") from exc

    with tf:
        # With mode "r:*" tarfile auto-detects the stream and reports mode
        # "r"; the wrapper is visible from the fileobj type instead.
        fo_type = type(tf.fileobj).__name__.lower()
        if "gzip" in fo_type:
            compression = "gz"
        elif "bz2" in fo_type:
            compression = "bz2"
        elif "xz" in fo_type or "lzma" in fo_type:
            compression = "xz"
        else:
            compression = None
        result.metadata["compression"] = compression

        try:
            for tinfo in tf:
                if len(result.members) >= limits.max_members:
                    raise LimitExceededError(
                        f"member count exceeds limit {limits.max_members}"
                    )

                note = None
                raw_name = tinfo.name
                # surrogateescape maps undecodable bytes into U+DC80..U+DCFF
                if any(0xDC80 <= ord(c) <= 0xDCFF for c in raw_name):
                    note = "decoded with surrogateescape (metadata encoding?)"
                name = normalize_member_name(raw_name)
                if name == ".":
                    continue
                depth = depth_of(name)
                if depth > limits.max_depth:
                    raise LimitExceededError(
                        f"directory depth {depth} of {name!r} exceeds "
                        f"limit {limits.max_depth}"
                    )

                occurrence = counts.get(name, 0)
                counts[name] = occurrence + 1
                is_dir = tinfo.isdir()
                if is_dir:
                    known_dirs.add(name)

                link_target = resolved = None
                if tinfo.issym() or tinfo.islnk():
                    if limits.reject_links:
                        raise UnsafePathError(
                            f"link members are disabled by policy: {name!r}"
                        )
                    kind = "symlink" if tinfo.issym() else "hardlink"
                    link_target = tinfo.linkname
                    if tinfo.issym():
                        # Symbolic links are resolved relative to the link's
                        # own directory.
                        resolved, _ = resolve_link_target(
                            name, link_target, known_dirs)
                    else:
                        # Hardlink targets in TAR name another archive member
                        # using a root-relative virtual path.
                        resolved = normalize_member_name(link_target)
                elif tinfo.isreg():
                    kind = "file"
                elif is_dir:
                    kind = "dir"
                else:
                    kind = "special"
                    if limits.reject_special_files:
                        raise UnsupportedArchiveError(
                            f"special member {name!r} "
                            f"({_TAR_SPECIAL.get(tinfo.type, tinfo.type)!r}) "
                            "rejected by policy"
                        )

                size = tinfo.size if tinfo.isreg() else 0
                if size > limits.max_member_size:
                    raise LimitExceededError(
                        f"member {name!r} size {size} exceeds single-member "
                        f"limit {limits.max_member_size}"
                    )
                total_expanded += size
                if total_expanded > limits.max_total_size:
                    raise LimitExceededError(
                        "total uncompressed size exceeds limit "
                        f"{limits.max_total_size}"
                    )

                crc = sha = None
                if kind == "file":
                    extracted = tf.extractfile(tinfo)
                    if extracted is None:
                        raise CorruptArchiveError(
                            f"regular member {name!r} has no readable data"
                        )
                    try:
                        with extracted:
                            size_actual, crc, sha = stream_digest(
                                extracted,
                                limits.max_member_size,
                                expected_size=tinfo.size,
                            )
                    except (tarfile.TarError, zlib.error, EOFError) as exc:
                        raise CorruptArchiveError(
                            f"member {name!r} corrupt: {exc}"
                        ) from exc
                    except ValueError as exc:
                        raise LimitExceededError(str(exc)) from exc
                    size = size_actual

                result.members.append(
                    MemberRecord(
                        name=name,
                        occurrence=occurrence,
                        kind=kind,
                        is_dir=is_dir,
                        depth=depth,
                        size=size,
                        # Compressed TAR streams expose no per-member stored
                        # length; plain TAR member size equals stored size.
                        compressed_size=size if compression is None else None,
                        offset=tinfo.offset,
                        data_offset=tinfo.offset_data,
                        crc32=crc,
                        sha256=sha,
                        link_target=link_target,
                        resolved_target=resolved,
                        ambiguous=False,
                        encoding_note=note,
                    )
                )
        except (tarfile.TarError, EOFError) as exc:
            raise CorruptArchiveError(
                f"truncated or malformed tar stream: {exc}"
            ) from exc

        if compression is not None:
            on_disk = os.path.getsize(path)
            result.metadata["archive_file_size"] = on_disk
            _check_ratio(on_disk, total_expanded, limits.max_compression_ratio)
        result.metadata["total_uncompressed_size"] = total_expanded

    _finalize(result, counts)
    return result


# ---------------------------------------------------------------------------
# Format detection / unified entry point
# ---------------------------------------------------------------------------


def _sniff_format(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError as exc:
        raise CorruptArchiveError(f"cannot read archive {path!r}: {exc}") from exc
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") \
            or head.startswith(b"PK\x07\x08"):
        return "zip"
    if tarfile.is_tarfile(path):
        return "tar"
    # gzip/bzip2/xz wrapped tar: tarfile.is_tarfile already handles these,
    # so anything else is unsupported.
    raise UnsupportedArchiveError(f"unrecognised archive format: {path!r}")


def index_archive(
    path: str,
    limits: Limits | None = None,
    *,
    metadata_encoding: str | None = None,
) -> IndexResult:
    """Sniff the format and index whichever archive type is found."""
    fmt = _sniff_format(path)
    if fmt == "zip":
        return index_zip(path, limits, metadata_encoding=metadata_encoding)
    return index_tar(path, limits, encoding=metadata_encoding)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def find_by_prefix(
    result: IndexResult,
    prefix: str,
    *,
    normalize: bool = True,
) -> list[MemberRecord]:
    """Return members whose canonical path starts with ``prefix``.

    A prefix matches an exact name or any path beneath that directory,
    e.g. prefix ``"a/b"`` matches ``"a/b"`` and ``"a/b/c"`` but not
    ``"a/bc"``.  Results are ordered by path then occurrence.
    """
    key = normalize_member_name(prefix) if normalize else prefix.rstrip("/")
    matched = [
        m for m in result.members
        if m.name == key or m.name.startswith(key + "/")
    ]
    return sorted(matched, key=lambda m: (m.name, m.occurrence))


@dataclass(slots=True)
class IndexDiff:
    """Result of comparing two indices by canonical logical path."""

    left_format: str
    right_format: str
    only_left: list[str] = field(default_factory=list)
    only_right: list[str] = field(default_factory=list)
    #: list of dicts: name, kind of change and per-occurrence detail
    changed: list[dict] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.only_left or self.only_right or self.changed)


def diff_indices(left: IndexResult, right: IndexResult) -> IndexDiff:
    """Compare two indices, digest based and occurrence aware."""
    diff = IndexDiff(left_format=left.format, right_format=right.format)
    names = sorted(set(left.by_name) | set(right.by_name))
    for name in names:
        l_recs = left.by_name.get(name, [])
        r_recs = right.by_name.get(name, [])
        if not l_recs:
            diff.only_right.append(name)
            continue
        if not r_recs:
            diff.only_left.append(name)
            continue
        changes: list[dict] = {}
        entry: dict | None = None
        if len(l_recs) != len(r_recs):
            entry = {
                "name": name,
                "reasons": ["occurrence-count"],
                "left_occurrences": len(l_recs),
                "right_occurrences": len(r_recs),
                "details": [],
            }
        for i in range(min(len(l_recs), len(r_recs))):
            l, r = l_recs[i], r_recs[i]
            reasons = []
            if l.kind != r.kind:
                reasons.append("kind")
            if l.size != r.size:
                reasons.append("size")
            if l.sha256 != r.sha256 or l.crc32 != r.crc32:
                reasons.append("digest")
            if reasons:
                if entry is None:
                    entry = {
                        "name": name,
                        "reasons": [],
                        "details": [],
                    }
                entry["details"].append(
                    {
                        "occurrence": i,
                        "reasons": reasons,
                        "left": l.to_dict(),
                        "right": r.to_dict(),
                    }
                )
                for reason in reasons:
                    if reason not in entry["reasons"]:
                        entry["reasons"].append(reason)
        if entry is not None:
            diff.changed.append(entry)
    return diff


# ---------------------------------------------------------------------------
# Verified single-member export
# ---------------------------------------------------------------------------


def _relocate_zip_member(
    zf: zipfile.ZipFile, name: str, occurrence: int
) -> zipfile.ZipInfo:
    seen = 0
    for info in zf.infolist():
        if normalize_member_name(info.filename) == name:
            if seen == occurrence:
                return info
            seen += 1
    raise CorruptArchiveError(f"member {name!r}#{occurrence} vanished on re-open")


def _relocate_tar_member(
    tf: tarfile.TarFile, name: str, occurrence: int
) -> tarfile.TarInfo:
    seen = 0
    for tinfo in tf.getmembers():
        if normalize_member_name(tinfo.name) == name:
            if seen == occurrence:
                return tinfo
            seen += 1
    raise CorruptArchiveError(f"member {name!r}#{occurrence} vanished on re-open")


def export_member(
    result: IndexResult,
    name: str,
    dest_dir: str,
    *,
    occurrence: int | None = None,
    overwrite: bool = False,
    allow_link: bool = False,
    archive_path: str | None = None,
) -> str:
    """Verify digests then export exactly one member into ``dest_dir``.

    The member is streamed into a temporary file *inside* ``dest_dir`` and
    only renamed into place after its size, CRC-32 and SHA-256 all match the
    previously built index.  No file content is written if verification
    fails.  Returns the absolute destination path.
    """
    record = result.get(name, occurrence)
    if record.is_dir:
        raise UnsupportedArchiveError(
            f"{name!r} is a directory; only single members can be exported"
        )
    if record.kind in ("symlink", "hardlink") and not allow_link:
        raise UnsupportedArchiveError(
            f"refusing to export link member {name!r} "
            "(pass allow_link=True to write the link target as a file)"
        )

    source = archive_path or result.path
    if not source:
        raise ValueError(
            "index has no archive path; pass archive_path= explicitly"
        )

    os.makedirs(dest_dir, exist_ok=True)
    destination = safe_join(dest_dir, record.name)
    if os.path.lexists(destination) and not overwrite:
        raise FileExistsError(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    # Re-open independently and stream again, re-verifying every digest.
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".archindex-", dir=dest_dir)
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            if result.format == "zip":
                try:
                    zf = zipfile.ZipFile(source)
                except (zipfile.BadZipFile, OSError) as exc:
                    raise CorruptArchiveError(str(exc)) from exc
                with zf:
                    info = _relocate_zip_member(zf, record.name,
                                                record.occurrence)
                    try:
                        with zf.open(info) as fh:
                            size, crc, sha = stream_digest_and_copy(
                                fh, out,
                                result.limits.max_member_size,
                                expected_size=record.size,
                            )
                    except zipfile.BadZipFile as exc:
                        raise CorruptArchiveError(
                            f"export verification failed: {exc}"
                        ) from exc
                    except ValueError as exc:
                        raise CorruptArchiveError(
                            f"export verification failed: {exc}"
                        ) from exc
            else:
                try:
                    tf = tarfile.open(source, mode="r:*")
                except (tarfile.TarError, OSError) as exc:
                    raise CorruptArchiveError(str(exc)) from exc
                with tf:
                    tinfo = _relocate_tar_member(tf, record.name,
                                                 record.occurrence)
                    extracted = tf.extractfile(tinfo)
                    if extracted is None:
                        raise CorruptArchiveError(
                            f"member {name!r} is not a readable file"
                        )
                    try:
                        with extracted:
                            size, crc, sha = stream_digest_and_copy(
                                extracted, out,
                                result.limits.max_member_size,
                                expected_size=record.size,
                            )
                    except (tarfile.TarError, zlib.error, EOFError) as exc:
                        raise CorruptArchiveError(
                            f"export verification failed: {exc}"
                        ) from exc
                    except ValueError as exc:
                        raise CorruptArchiveError(
                            f"export verification failed: {exc}"
                        ) from exc

            if size != record.size:
                raise CorruptArchiveError(
                    f"size changed since indexing for {name!r}: "
                    f"{record.size} -> {size}"
                )
            if record.crc32 is not None and crc != record.crc32:
                raise CorruptArchiveError(
                    f"CRC-32 changed since indexing for {name!r}: "
                    f"{record.crc32:08x} -> {crc:08x}"
                )
            if record.sha256 is not None and sha != record.sha256:
                raise CorruptArchiveError(
                    f"SHA-256 changed since indexing for {name!r}: "
                    f"{record.sha256} -> {sha}"
                )
            out.flush()
            os.fsync(out.fileno())

        os.replace(tmp_path, destination)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return destination
