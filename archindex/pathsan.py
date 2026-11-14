"""Pure path normalisation and link-target safety logic.

Nothing in this module touches the real file system; it only reasons about
logical paths *inside* an archive so that a crafted archive can never reach
outside of its virtual root.
"""

from __future__ import annotations

import posixpath

from .errors import UnsafePathError

# Control characters (except TAB) are never acceptable in a member name.
_CONTROL_BYTES = {c for c in range(0x20) if c != 0x09} | {0x7F}


def _reject_control_chars(raw: str) -> None:
    for ch in raw:
        if ord(ch) in _CONTROL_BYTES:
            raise UnsafePathError(f"control character in member name: {raw!r}")
        if "\x00" in raw:
            raise UnsafePathError(f"NUL byte in member name: {raw!r}")


def normalize_member_name(raw: str | bytes) -> str:
    """Normalise a raw archive member name to a canonical logical path.

    The result is a POSIX style, root-relative path (no leading ``/``), with
    backslashes treated as ordinary characters (ZIP semantics) and no
    ``.`` / ``..`` components that escape the archive root.

    Raises :class:`UnsafePathError` for absolute paths, drive letters,
    NUL bytes, control characters or traversal sequences that escape root.
    """
    if isinstance(raw, bytes):
        # Bytes names mean the archive metadata did not declare an encoding.
        # Callers decode first, but keep a defensive fallback.
        raw = raw.decode("utf-8", "surrogateescape")

    if not raw or raw.strip() == "":
        raise UnsafePathError("empty member name")

    _reject_control_chars(raw)

    # NUL-terminated names (C strings in TAR headers) must not hide a
    # different trailing path.
    if "\x00" in raw:
        raise UnsafePathError(f"NUL byte in member name: {raw!r}")

    # Windows drive letters (e.g. "C:foo") are absolute on some targets.
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise UnsafePathError(f"drive-letter member name is absolute: {raw!r}")

    # A leading slash (or several) denotes an absolute POSIX path.
    if raw.startswith("/"):
        raise UnsafePathError(f"absolute member name rejected: {raw!r}")

    # Backslashes are *not* separators in POSIX/TAR and must not silently
    # become separators: keep them as ordinary characters.  At the same time,
    # Windows-style traversal spellings ("..\..\x") must still be rejected.
    if "\\" in raw:
        for component in raw.split("/"):
            for part in component.split("\\"):
                if part == "..":
                    raise UnsafePathError(
                        f"path traversal escapes archive root: {raw!r}"
                    )
        normalized = posixpath.normpath(raw)
    else:
        normalized = posixpath.normpath(raw)

    # normpath collapses "//" and "./" but keeps traversal markers visible.
    if normalized == ".." or normalized.startswith("../") or normalized == "/":
        raise UnsafePathError(
            f"path traversal escapes archive root: {raw!r}"
        )
    if normalized.startswith("/"):
        raise UnsafePathError(f"absolute member name rejected: {raw!r}")

    # Re-verify component by component after normalisation.
    parts = normalized.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            if part == "..":
                raise UnsafePathError(
                    f"path traversal escapes archive root: {raw!r}"
                )
            if part == "":
                raise UnsafePathError(f"empty path component in: {raw!r}")
    return normalized


def depth_of(normalized_path: str) -> int:
    """Directory nesting depth of a canonical path.

    ``"a"`` -> 0, ``"a/b"`` -> 1, ``"a/b/c"`` -> 2.  Directory entries keep
    the same rule (their trailing slash is stripped before calling).
    """
    path = normalized_path.rstrip("/")
    if not path:
        return 0
    return path.count("/")


def safe_join(root: str, normalized_path: str) -> str:
    """Join a canonical member path onto an export root, checking containment.

    Raises :class:`UnsafePathError` if the resulting path would escape
    ``root``.
    """
    import os

    root_real = os.path.realpath(root)
    destination = os.path.join(root_real, *normalized_path.split("/"))
    # The destination may not exist yet, so walk up to the first existing
    # ancestor and ensure containment there as well.
    parent = os.path.dirname(destination)
    parent_real = os.path.realpath(parent)
    if (
        parent_real != root_real
        and not parent_real.startswith(root_real + os.sep)
    ):
        raise UnsafePathError(
            f"export destination escapes root: {normalized_path!r}"
        )
    return destination


def resolve_link_target(
    link_name: str,
    link_target: str,
    known_dirs: set[str],
) -> tuple[str, bool]:
    """Resolve a symbolic link's logical target inside the archive.

    ``link_name`` is the already-normalised member name of the link itself.
    ``link_target`` is the raw target string stored in the link entry.

    Returns ``(resolved_normalized_path, is_directory)`` or raises
    :class:`UnsafePathError` when the link points outside the archive root
    (absolute targets or traversal sequences escaping root).
    """
    target = link_target
    if "\x00" in target:
        raise UnsafePathError(f"NUL byte in link target: {link_target!r}")
    # Absolute links always point outside the archive's virtual root.
    if target.startswith("/") or (
        len(target) >= 2 and target[1] == ":" and target[0].isalpha()
    ):
        raise UnsafePathError(
            f"absolute link target rejected: {link_name!r} -> {link_target!r}"
        )

    base_dir = posixpath.dirname(link_name)
    joined = posixpath.join(base_dir, target.replace("\\", "/"))
    normalized = posixpath.normpath(joined)
    if normalized in (".", "..") or normalized.startswith("../"):
        raise UnsafePathError(
            f"link target escapes archive root: "
            f"{link_name!r} -> {link_target!r}"
        )
    if normalized.startswith("/"):
        raise UnsafePathError(
            f"absolute link target rejected: {link_name!r} -> {link_target!r}"
        )
    return normalized, normalized in known_dirs
