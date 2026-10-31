"""Bounded local cache lookup for media in forwarded WeChat records."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# WeChat cache directories contain a mix of media and database files.  The
# exact size and MD5 check below is authoritative; this list only prevents a
# broad filesystem walk from opening unrelated files.
DEFAULT_MEDIA_EXTENSIONS = frozenset(
    {
        ".dat",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".heic",
        ".mp4",
        ".mov",
        ".m4v",
        ".avi",
        ".mkv",
        ".webm",
        ".amr",
        ".silk",
        ".mp3",
        ".wav",
        ".ogg",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".rar",
        ".7z",
        ".txt",
        ".bin",
    }
)

_XOR_HEADERS = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),
)


def forwarded_media_xor_key(header: bytes) -> Optional[int]:
    """Return WeChat's single-byte XOR key when the header is recognisable."""
    header = bytes(header[:32])
    if not header:
        return None
    for signature, media_format in _XOR_HEADERS:
        if len(header) < len(signature):
            continue
        key = header[0] ^ signature[0]
        if key and all(
            header[index] ^ key == value
            for index, value in enumerate(signature)
        ):
            if media_format != "webp":
                return key
            if len(header) >= 12 and all(
                header[index] ^ key == value
                for index, value in enumerate(b"WEBP", start=8)
            ):
                return key

    # MP4/MOV files have the size field before the ``ftyp`` marker, so the
    # XOR key is derived from byte 4 instead of byte 0.
    if len(header) >= 12:
        key = header[4] ^ ord("f")
        if key and all(
            header[index] ^ key == value
            for index, value in enumerate(b"ftyp", start=4)
        ):
            return key
    return None


def forwarded_media_extension(
    header: bytes,
    xor_key: Optional[int] = None,
) -> str:
    """Identify common WeChat media formats from a plain or XOR header."""
    header = bytes(header[:32])
    key = 0 if xor_key is None else int(xor_key)
    decoded = bytes(value ^ key for value in header)

    if decoded.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if decoded.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if (
        len(decoded) >= 12
        and decoded[:4] == b"RIFF"
        and decoded[8:12] == b"WEBP"
    ):
        return "webp"
    if len(decoded) >= 12 and decoded[4:8] == b"ftyp":
        return "mp4"
    return ""


@dataclass(frozen=True)
class ForwardedMediaResolution:
    """A resolved media file and the public note that should accompany it."""

    source: Any
    filename: str = ""
    is_thumbnail: bool = False
    note: str = ""
    media_type: Optional[str] = None
    xor_encoded: bool = False


class ForwardedMediaUnavailable(RuntimeError):
    """Raised with a short, user-safe reason when no media can be resolved."""


@dataclass(frozen=True)
class _CacheFile:
    path: str
    size: int
    mtime_ns: int
    extension: str


@dataclass(frozen=True)
class _CacheMatch:
    file: _CacheFile
    xor_key: Optional[int]


class ForwardedMediaCache:
    """Find a cache file by exact original size and MD5.

    The snapshot is intentionally bounded by roots, depth, file count and
    elapsed time.  Files are hashed only after their size matches exactly.
    """

    def __init__(
        self,
        roots: Iterable[str],
        *,
        max_files: int = 30000,
        max_depth: int = 8,
        max_scan_seconds: float = 3.0,
        max_match_seconds: float = 8.0,
        snapshot_ttl: float = 30.0,
        extensions: Optional[Iterable[str]] = None,
    ) -> None:
        self.roots = self._normalize_roots(roots)
        self.max_files = max(0, int(max_files))
        self.max_depth = max(0, int(max_depth))
        self.max_scan_seconds = max(0.0, float(max_scan_seconds))
        self.max_match_seconds = max(0.0, float(max_match_seconds))
        self.snapshot_ttl = max(0.0, float(snapshot_ttl))
        if extensions is None:
            self.extensions = DEFAULT_MEDIA_EXTENSIONS
        else:
            self.extensions = frozenset(
                str(extension).lower()
                if str(extension).startswith(".")
                else ".{}".format(str(extension).lower())
                for extension in extensions
            )

        self._lock = threading.Lock()
        self._index: Dict[int, Tuple[_CacheFile, ...]] = {}
        self._snapshot_at = 0.0
        self._built = False
        self._truncated = False
        self._lookup_incomplete = False
        self._hash_cache: Dict[
            Tuple[str, int, int],
            Tuple[Optional[str], Optional[str], Optional[int]],
        ] = {}

    @property
    def complete(self) -> bool:
        """Whether the latest snapshot and lookup covered every candidate."""
        with self._lock:
            return self._built and not self._truncated and not self._lookup_incomplete

    @staticmethod
    def _normalize_roots(roots: Iterable[str]) -> Tuple[str, ...]:
        result: List[str] = []
        for root in roots:
            if not root:
                continue
            try:
                normalized = os.path.realpath(os.fspath(root))
            except (OSError, TypeError, ValueError):
                continue
            if normalized not in result:
                result.append(normalized)
        return tuple(result)

    @staticmethod
    def _normalize_md5(value: object) -> str:
        text = str(value or "").strip()
        return text.lower() if _MD5_RE.match(text) else ""

    @staticmethod
    def _normalize_size(value: object) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            size = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return size if size > 0 else None

    def find(
        self,
        size: object = None,
        md5: object = None,
        *,
        thumbnail_size: object = None,
        thumbnail_md5: object = None,
        extensions: Iterable[str] = (),
        preferred_extensions: Iterable[str] = (),
    ) -> Optional[ForwardedMediaResolution]:
        """Return an exact full or thumbnail match, or ``None``.

        Full media always wins over a thumbnail.  The extension list only
        changes ordering; size and MD5 remain authoritative.
        """
        full_size = self._normalize_size(size)
        full_md5 = self._normalize_md5(md5)
        thumb_size = self._normalize_size(thumbnail_size)
        thumb_md5 = self._normalize_md5(thumbnail_md5)
        can_match_full = full_size is not None and bool(full_md5)
        can_match_thumbnail = thumb_size is not None and bool(thumb_md5)
        if not can_match_full and not can_match_thumbnail:
            return None
        preferred = self._normalize_extensions(
            tuple(extensions or ()) + tuple(preferred_extensions or ())
        )

        with self._lock:
            now = time.monotonic()
            if not self._built or now - self._snapshot_at >= self.snapshot_ttl:
                self._build_index()
            self._lookup_incomplete = False

            full = (
                self._find_match(full_size, full_md5, preferred)
                if can_match_full
                else None
            )
            if full is not None:
                return ForwardedMediaResolution(
                    source=full.file.path,
                    filename=os.path.basename(full.file.path),
                    xor_encoded=full.xor_key is not None,
                )

            thumbnail = (
                self._find_match(thumb_size, thumb_md5, preferred)
                if can_match_thumbnail
                else None
            )
            if thumbnail is not None:
                return ForwardedMediaResolution(
                    source=thumbnail.file.path,
                    filename=os.path.basename(thumbnail.file.path),
                    is_thumbnail=True,
                    note="仅恢复缩略图，原文件不可用",
                    media_type="image",
                    xor_encoded=thumbnail.xor_key is not None,
                )
            return None

    def _find_match(
        self,
        size: Optional[int],
        digest: str,
        preferred: frozenset,
    ) -> Optional[_CacheMatch]:
        if size is None or not digest:
            return None
        candidates = self._index.get(size, ())
        if not candidates:
            return None
        ordered = sorted(
            candidates,
            key=lambda item: (
                item.extension not in preferred,
                -item.mtime_ns,
                item.path,
            ),
        )
        deadline = time.monotonic() + self.max_match_seconds
        for candidate in ordered:
            if time.monotonic() >= deadline:
                self._lookup_incomplete = True
                break
            match = self._md5_matches(candidate, digest, deadline)
            if match is not None:
                return match
            if time.monotonic() >= deadline:
                self._lookup_incomplete = True
                break
        return None

    @classmethod
    def _normalize_extensions(
        cls,
        extensions: Iterable[str],
    ) -> frozenset:
        return frozenset(
            str(extension).lower()
            if str(extension).startswith(".")
            else ".{}".format(str(extension).lower())
            for extension in extensions
        )

    def _build_index(self) -> None:
        deadline = time.monotonic() + self.max_scan_seconds
        index: Dict[int, List[_CacheFile]] = {}
        visited = 0
        truncated = False

        for root in self.roots:
            if visited >= self.max_files or time.monotonic() >= deadline:
                truncated = True
                break
            if not os.path.isdir(root):
                continue

            stack: List[Tuple[str, int]] = [(root, 0)]
            while stack:
                if visited >= self.max_files or time.monotonic() >= deadline:
                    truncated = True
                    break
                directory, depth = stack.pop()
                try:
                    iterator = os.scandir(directory)
                except OSError:
                    truncated = True
                    continue
                with iterator:
                    for entry in iterator:
                        if visited >= self.max_files or time.monotonic() >= deadline:
                            truncated = True
                            break
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if depth < self.max_depth:
                                    stack.append((entry.path, depth + 1))
                                else:
                                    truncated = True
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            extension = os.path.splitext(entry.name)[1].lower()
                            if extension not in self.extensions:
                                continue
                            stat_result = entry.stat(follow_symlinks=False)
                        except OSError:
                            truncated = True
                            continue
                        visited += 1
                        if stat_result.st_size <= 0:
                            continue
                        cache_file = _CacheFile(
                            path=entry.path,
                            size=stat_result.st_size,
                            mtime_ns=stat_result.st_mtime_ns,
                            extension=extension,
                        )
                        index.setdefault(cache_file.size, []).append(cache_file)

        self._index = {
            size: tuple(files) for size, files in index.items()
        }
        self._snapshot_at = time.monotonic()
        self._built = True
        self._truncated = truncated
        self._lookup_incomplete = False
        if len(self._hash_cache) > 4096:
            self._hash_cache.clear()

    def _md5_matches(
        self,
        candidate: _CacheFile,
        expected_md5: str,
        deadline: float,
    ) -> Optional[_CacheMatch]:
        key = (candidate.path, candidate.size, candidate.mtime_ns)
        if key in self._hash_cache:
            raw_digest, decoded_digest, xor_key = self._hash_cache[key]
            if raw_digest == expected_md5:
                return _CacheMatch(candidate, None)
            if decoded_digest == expected_md5:
                return _CacheMatch(candidate, xor_key)
            return None

        raw_digest = hashlib.md5(usedforsecurity=False)
        decoded_digest = hashlib.md5(usedforsecurity=False)
        total = 0
        xor_key: Optional[int] = None
        header = b""
        translation = None
        try:
            with open(candidate.path, "rb") as source:
                header = source.read(32)
                source.seek(0)
                xor_key = forwarded_media_xor_key(header)
                if xor_key is not None:
                    translation = bytes(
                        value ^ xor_key for value in range(256)
                    )
                while True:
                    if time.monotonic() >= deadline:
                        return None
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > candidate.size:
                        self._hash_cache[key] = (None, None, None)
                        return None
                    raw_digest.update(chunk)
                    if translation is not None:
                        decoded_digest.update(chunk.translate(translation))
        except OSError:
            self._hash_cache[key] = (None, None, None)
            return None

        if total != candidate.size:
            self._hash_cache[key] = (None, None, None)
            return None
        actual_raw = raw_digest.hexdigest()
        actual_decoded = (
            decoded_digest.hexdigest() if xor_key is not None else None
        )
        self._hash_cache[key] = (actual_raw, actual_decoded, xor_key)
        if actual_raw == expected_md5:
            return _CacheMatch(candidate, None)
        if actual_decoded == expected_md5:
            return _CacheMatch(candidate, xor_key)
        return None
