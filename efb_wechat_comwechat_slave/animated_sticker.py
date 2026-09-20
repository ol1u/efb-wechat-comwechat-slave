import base64
import hashlib
import html
import logging
import os
import re
import threading
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import requests


STICKER_CACHE_MAX_BYTES = 512 * 1024 * 1024
STICKER_CONNECT_TIMEOUT = 5
STICKER_READ_TIMEOUT = 30
PERMANENT_HTTP_STATUS = {400, 403, 404}


class StickerError(Exception):
    pass


class StickerTemporaryError(StickerError):
    pass


class StickerPendingError(StickerTemporaryError):
    pass


class StickerPermanentError(StickerError):
    pass


@dataclass(frozen=True)
class StickerMetadata:
    md5: str
    length: int
    url: Optional[str]

    @property
    def key(self) -> str:
        return "{}-{}".format(self.md5, self.length)


class _DownloadState:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.path: Optional[str] = None
        self.error: Optional[Exception] = None


def is_sticker_share(msg: dict) -> bool:
    if msg.get("type") != "share":
        return False
    try:
        root = ET.fromstring(msg.get("message") or "")
    except (ET.ParseError, TypeError):
        return False
    return root.findtext("./appmsg/type") == "8"


def _extract_share_metadata(message: str) -> StickerMetadata:
    try:
        root = ET.fromstring(message)
    except (ET.ParseError, TypeError) as exc:
        raise StickerPermanentError("sticker share XML is invalid") from exc
    appmsg = root.find("./appmsg")
    if appmsg is None or appmsg.findtext("type") != "8":
        raise StickerPermanentError("message is not a sticker share")
    attachment = appmsg.find("./appattach")
    if attachment is None:
        raise StickerPermanentError("sticker share attachment is missing")

    digest = attachment.findtext("emoticonmd5")
    declared_length = attachment.findtext("totallen")
    emojiinfo = attachment.findtext("emojiinfo") or ""
    url = None
    try:
        decoded = base64.b64decode(emojiinfo)
    except (ValueError, TypeError) as exc:
        raise StickerPermanentError("sticker share emoji info is invalid") from exc
    if isinstance(digest, str):
        for raw_url in re.findall(rb"https?://[^\x00-\x20<>\"]+", decoded):
            candidate = html.unescape(raw_url.decode("utf-8", errors="ignore"))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(candidate).query)
            if any(value.lower() == digest.lower() for value in query.get("m", ())):
                url = candidate
                break
    return StickerMetadata(
        md5=digest.lower() if isinstance(digest, str) else "",
        length=_validated_length(declared_length),
        url=url,
    )


def _validated_length(value: Optional[str]) -> int:
    try:
        length = int(value)
    except (TypeError, ValueError):
        raise StickerPermanentError("animated sticker length is missing or invalid")
    if length <= 0:
        raise StickerPermanentError("animated sticker length is invalid")
    return length


def extract_sticker_metadata(msg: dict) -> StickerMetadata:
    message = msg.get("message") or ""
    if msg.get("type") == "share":
        metadata = _extract_share_metadata(message)
        if not re.fullmatch(r"[0-9a-f]{32}", metadata.md5):
            raise StickerPermanentError("animated sticker MD5 is missing or invalid")
        return metadata

    def attribute(name: str) -> Optional[str]:
        match = re.search(r"\b{}\s*=\s*['\"]([^'\"]+)".format(name), message)
        return html.unescape(match.group(1)) if match else None

    digest = attribute("md5")
    declared_length = attribute("len")
    url = attribute("cdnurl") or msg.get("url")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", digest):
        raise StickerPermanentError("animated sticker MD5 is missing or invalid")
    length = _validated_length(declared_length)
    if not isinstance(url, str) or not url:
        url = None
    return StickerMetadata(md5=digest.lower(), length=length, url=url)


class AnimatedStickerCache:
    def __init__(self, cache_dir: Path, max_bytes: int = STICKER_CACHE_MAX_BYTES) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_bytes = max_bytes
        self.logger = logging.getLogger(__name__)
        self._downloads: Dict[str, _DownloadState] = {}
        self._lock = threading.Lock()
        self._trim_lock = threading.Lock()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._remove_partial_files()
        self._trim()

    def get_or_download(self, msg: dict, wait: float = 5) -> str:
        metadata = extract_sticker_metadata(msg)
        cached = self.cache_dir / metadata.key
        if cached.is_file():
            self._touch(cached)
            return str(cached)
        if metadata.url is None:
            raise StickerPermanentError("animated sticker URL is missing")

        with self._lock:
            state = self._downloads.get(metadata.key)
            if state is None:
                state = _DownloadState()
                self._downloads[metadata.key] = state
                threading.Thread(
                    target=self._download,
                    args=(metadata, state),
                    name="animated-sticker-{}".format(metadata.key[:12]),
                    daemon=True,
                ).start()

        if not state.event.wait(timeout=max(wait, 0)):
            raise StickerPendingError("animated sticker download is still running")
        if state.error is not None:
            raise state.error
        if state.path is None:
            raise StickerTemporaryError("animated sticker download produced no file")
        return state.path

    def _download(self, metadata: StickerMetadata, state: _DownloadState) -> None:
        target = self.cache_dir / metadata.key
        partial = self.cache_dir / "{}.{}.part".format(metadata.key, uuid.uuid4().hex)
        response = None
        try:
            response = requests.get(
                metadata.url,
                stream=True,
                timeout=(STICKER_CONNECT_TIMEOUT, STICKER_READ_TIMEOUT),
            )
            if response.status_code in PERMANENT_HTTP_STATUS:
                raise StickerPermanentError(
                    "animated sticker CDN returned HTTP {}".format(response.status_code)
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise StickerTemporaryError(
                    "animated sticker CDN returned HTTP {}".format(response.status_code)
                )
            if response.status_code < 200 or response.status_code >= 300:
                raise StickerTemporaryError(
                    "animated sticker CDN returned HTTP {}".format(response.status_code)
                )

            digest = hashlib.md5()
            length = 0
            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    length += len(chunk)

            if length == 0:
                raise StickerTemporaryError("animated sticker CDN returned an empty file")
            if digest.hexdigest() != metadata.md5:
                raise StickerTemporaryError("animated sticker MD5 mismatch")

            os.replace(str(partial), str(target))
            self._touch(target)
            self._trim(protected=target)
            state.path = str(target)
        except StickerError as exc:
            self.logger.warning(
                "Animated sticker download failed: key=%s error=%s",
                metadata.key,
                exc,
            )
            state.error = exc
        except requests.RequestException as exc:
            self.logger.warning(
                "Animated sticker request failed: key=%s error=%s",
                metadata.key,
                exc,
            )
            state.error = StickerTemporaryError(str(exc))
        except Exception as exc:
            self.logger.warning(
                "Unexpected animated sticker download failure: key=%s",
                metadata.key,
                exc_info=True,
            )
            state.error = StickerTemporaryError(str(exc))
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    self.logger.debug(
                        "Failed to close animated sticker response: key=%s",
                        metadata.key,
                        exc_info=True,
                    )
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                self.logger.warning(
                    "Failed to remove animated sticker partial file: key=%s",
                    metadata.key,
                    exc_info=True,
                )
            with self._lock:
                self._downloads.pop(metadata.key, None)
            state.event.set()

    @staticmethod
    def _touch(path: Path) -> None:
        try:
            os.utime(str(path), None)
        except OSError:
            pass

    def _trim(self, protected: Optional[Path] = None) -> None:
        with self._trim_lock:
            try:
                files = [
                    item
                    for item in self.cache_dir.iterdir()
                    if item.is_file() and not item.name.endswith(".part")
                ]
                total = sum(item.stat().st_size for item in files)
                for item in sorted(files, key=lambda path: path.stat().st_mtime):
                    if total <= self.max_bytes:
                        break
                    if protected is not None and item == protected:
                        continue
                    size = item.stat().st_size
                    item.unlink()
                    total -= size
            except OSError:
                self.logger.warning("Failed to trim animated sticker cache", exc_info=True)

    def _remove_partial_files(self) -> None:
        for partial in self.cache_dir.glob("*.part"):
            try:
                partial.unlink()
            except OSError:
                self.logger.warning(
                    "Failed to remove stale animated sticker partial file: %s",
                    partial.name,
                    exc_info=True,
                )
