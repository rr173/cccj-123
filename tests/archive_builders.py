"""Helpers synthesising temporary archives for the test-suite.

All archives are freshly built inside each test's temporary directory; they
are untrusted inputs and the production package never extracts them.
"""

from __future__ import annotations

import os
import struct
import tarfile
import tempfile
import zipfile

LOCAL_SIG = b"PK\x03\x04"
CENTRAL_SIG = b"PK\x01\x02"
EOCD_SIG = b"PK\x05\x06"


# --------------------------------------------------------------------------
# ZIP builders
# --------------------------------------------------------------------------


def build_zip(path: str, members: dict[str, bytes], *,
              compression: int = zipfile.ZIP_STORED) -> str:
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def build_zip_with_duplicates(path: str, entries) -> str:
    """``entries`` is an ordered list of ``(name, data)`` allowing repeats."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


def flip_utf8_and_reencode(path: str, encoding: str = "gbk") -> None:
    """Rewrite a ZIP: drop the UTF-8 flag and store names in ``encoding``.

    Both local and central headers are patched; all offsets in the central
    directory and EOCD record are shifted consistently.
    """
    with open(path, "rb") as fh:
        blob = bytearray(fh.read())

    out = bytearray()
    central = bytearray()
    offset_shifts: dict[int, int] = {}
    pos = 0
    while pos < len(blob) and blob[pos:pos + 4] == LOCAL_SIG:
        start = pos
        (version, flags, method, mtime, mdate, crc, csize, usize,
         nlen, elen) = struct.unpack_from("<HHHHHIIIHH", blob, pos + 4)
        # Fixed local header is 30 bytes (26 after the 4-byte signature).
        assert struct.calcsize("<HHHHHIIIHH") == 26
        name = bytes(blob[pos + 30: pos + 30 + nlen])
        extra = bytes(blob[pos + 30 + nlen: pos + 30 + nlen + elen])
        data_start = pos + 30 + nlen + elen
        data_end = data_start + csize
        text = name.decode("utf-8")
        new_name = text.encode(encoding)
        new_flags = flags & ~0x800
        local_offset = len(out)
        offset_shifts[start] = local_offset
        out += LOCAL_SIG + struct.pack(
            "<HHHHHIIIHH",
            version, new_flags, method, mtime, mdate,
            crc, csize, usize, len(new_name), len(extra),
        )
        assert struct.calcsize("<HHHHHIIIHH") == 26
        out += new_name + extra
        out += blob[data_start:data_end]
        pos = data_end
        # optional data descriptor follows when flag bit 3 is set; our
        # test archives never use streaming mode, so skip it.

    cd_start = pos
    while pos < len(blob) and blob[pos:pos + 4] == CENTRAL_SIG:
        (vm, vn, flags, method, mtime, mdate, crc, csize, usize,
         nlen, elen, clen, disk, iattr, eattr, lho) = struct.unpack_from(
            "<HHHHHHIIIHHHHHII", blob, pos + 4)
        # Central fixed fields are 42 bytes after the 4-byte signature.
        assert struct.calcsize("<HHHHHHIIIHHHHHII") == 42
        name = bytes(blob[pos + 46: pos + 46 + nlen])
        extra = bytes(blob[pos + 46 + nlen: pos + 46 + nlen + elen])
        comment = bytes(blob[pos + 46 + nlen + elen:
                             pos + 46 + nlen + elen + clen])
        new_name = name.decode("utf-8").encode(encoding)
        new_lho = offset_shifts[lho]
        central += CENTRAL_SIG + struct.pack(
            "<HHHHHHIIIHHHHHII", vm, vn,
            flags & ~0x800, method, mtime, mdate, crc, csize, usize,
            len(new_name), len(extra), len(comment), disk, iattr,
            eattr, new_lho,
        )
        assert struct.calcsize("<HHHHHHIIIHHHHHII") == 42
        central += new_name + extra + comment
        pos += 46 + nlen + elen + clen

    assert blob[pos:pos + 4] == EOCD_SIG
    (disk, cdisk, ndisk, total, _cd_size, _cd_off, clen) = struct.unpack_from(
        "<HHHHIIH", blob, pos + 4)
    new_cd_off = len(out)
    out += central
    out += EOCD_SIG + struct.pack(
        "<HHHHIIH", disk, cdisk, ndisk, total,
        len(central), new_cd_off, clen,
    )
    with open(path, "wb") as fh:
        fh.write(out)


def corrupt_central_crc(path: str, member_index: int = 0) -> None:
    """Flip a CRC-32 value inside a central directory entry."""
    with open(path, "rb") as fh:
        blob = bytearray(fh.read())
    pos = blob.find(CENTRAL_SIG)
    for _ in range(member_index):
        pos = blob.find(CENTRAL_SIG, pos + 4)
    nlen = struct.unpack_from("<H", blob, pos + 28)[0]
    elen = struct.unpack_from("<H", blob, pos + 30)[0]
    clen = struct.unpack_from("<H", blob, pos + 32)[0]
    crc_pos = pos + 16
    blob[crc_pos] ^= 0xFF
    with open(path, "wb") as fh:
        fh.write(blob)
    return nlen + elen + clen  # unused, kept for clarity


def mutate_stored_data(path: str, member_index: int = 0,
                       payload: bytes = b"TAMPERED") -> None:
    """Overwrite bytes of a STORED member's data region in place."""
    with open(path, "r+b") as fh:
        blob = fh.read()
    pos = blob.find(LOCAL_SIG)
    for _ in range(member_index):
        pos = blob.find(LOCAL_SIG, pos + 4)
    nlen, elen = struct.unpack_from("<HH", blob, pos + 26)
    data_pos = pos + 30 + nlen + elen
    with open(path, "r+b") as fh:
        fh.seek(data_pos)
        fh.write(payload)


def truncate_file(path: str, keep_ratio: float = 0.5) -> None:
    size = os.path.getsize(path)
    with open(path, "r+b") as fh:
        fh.truncate(int(size * keep_ratio))


def write_random_bytes(path: str, size: int = 2048) -> None:
    with open(path, "wb") as fh:
        fh.write(os.urandom(size))


# --------------------------------------------------------------------------
# TAR builders
# --------------------------------------------------------------------------


def _add(tar: tarfile.TarFile, name: str, data: bytes | None = None,
         kind: str = tarfile.REGTYPE, link: str | None = None) -> None:
    info = tarfile.TarInfo(name)
    if kind == tarfile.DIRTYPE:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
        tar.addfile(info)
        return
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.type = kind
        info.linkname = link or ""
        info.mode = 0o777
        info.size = 0
        tar.addfile(info)
        return
    if kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
        info.type = kind
        info.mode = 0o644
        info.size = 0
        tar.addfile(info)
        return
    payload = data or b""
    info.size = len(payload)
    info.mode = 0o644
    tar.addfile(info, __import__("io").BytesIO(payload))


def build_tar(path: str, members: dict[str, bytes], *,
              mode: str = "w", dirs: tuple[str, ...] = ()) -> str:
    with tarfile.open(path, mode) as tar:
        for d in dirs:
            _add(tar, d, kind=tarfile.DIRTYPE)
        for name, data in members.items():
            _add(tar, name, data)
    return path


def build_tar_with_links(path: str, entries) -> str:
    """``entries``: list of tuples (kind, name, data/link-target)."""
    import io
    with tarfile.open(path, "w") as tar:
        for item in entries:
            kind, name, value = item
            if kind == "file":
                info = tarfile.TarInfo(name)
                info.size = len(value)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(value))
            elif kind == "dir":
                _add(tar, name, kind=tarfile.DIRTYPE)
            elif kind == "symlink":
                _add(tar, name, kind=tarfile.SYMTYPE, link=value)
            elif kind == "hardlink":
                _add(tar, name, kind=tarfile.LNKTYPE, link=value)
            elif kind == "special":
                _add(tar, name, kind=tarfile.CHRTYPE, link=value)
    return path


def build_gbk_tar(path: str, members: dict[str, bytes]) -> str:
    """Write a tar whose name fields are raw GBK bytes."""
    with tarfile.open(path, "w", encoding="gbk") as tar:
        for name, data in members.items():
            _add(tar, name, data)
    return path


def tamper_tar_header(path: str) -> None:
    """Corrupt the first TAR header's checksum field."""
    with open(path, "r+b") as fh:
        fh.seek(148)
        fh.write(b"0000000\x00")


def temp_archive(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path
