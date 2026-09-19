import base64
import hashlib
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from efb_wechat_comwechat_slave.animated_sticker import (
    AnimatedStickerCache,
    StickerPendingError,
    StickerPermanentError,
    StickerTemporaryError,
    extract_sticker_metadata,
)


class FakeResponse:
    def __init__(self, content=b"gif", status_code=200, release=None):
        self.content = content
        self.status_code = status_code
        self.release = release

    def iter_content(self, chunk_size):
        if self.release is not None:
            self.release.wait(timeout=1)
        yield self.content

    def close(self):
        pass


def sticker_msg(content=b"gif", url="https://example.test/sticker"):
    return {
        "type": "animatedsticker",
        "message": (
            '<emoji cdnurl="{}" md5="{}" len="{}" type="2" />'.format(
                url,
                hashlib.md5(content).hexdigest(),
                len(content),
            )
        ),
    }


def sticker_share_msg(content=b"gif", url="https://example.test/sticker"):
    digest = hashlib.md5(content).hexdigest()
    other_url = "https://example.test/thumbnail?m={}".format("0" * 32)
    emojiinfo = base64.b64encode(
        b"\x0a" + url.encode() + b"?m=" + digest.encode() + b"\x00" + other_url.encode()
    ).decode()
    return {
        "type": "share",
        "message": (
            "<msg><appmsg><type>8</type><appattach>"
            "<totallen>{}</totallen><emoticonmd5>{}</emoticonmd5>"
            "<emojiinfo>{}</emojiinfo>"
            "</appattach></appmsg></msg>"
        ).format(len(content), digest, emojiinfo),
    }


class TestAnimatedStickerCache(unittest.TestCase):
    def test_pending_download_is_a_temporary_failure(self):
        self.assertTrue(issubclass(StickerPendingError, StickerTemporaryError))

    def test_extracts_required_metadata(self):
        metadata = extract_sticker_metadata(sticker_msg())

        self.assertEqual(metadata.length, 3)
        self.assertEqual(metadata.url, "https://example.test/sticker")
        self.assertEqual(metadata.md5, hashlib.md5(b"gif").hexdigest())

    def test_extracts_share_sticker_metadata_and_matching_cdn_url(self):
        metadata = extract_sticker_metadata(sticker_share_msg())

        self.assertEqual(metadata.length, 3)
        self.assertEqual(
            metadata.url,
            "https://example.test/sticker?m={}".format(
                hashlib.md5(b"gif").hexdigest()
            ),
        )
        self.assertEqual(metadata.md5, hashlib.md5(b"gif").hexdigest())

    def test_exact_cache_hit_skips_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AnimatedStickerCache(Path(tmpdir))
            metadata = extract_sticker_metadata(sticker_msg())
            expected = Path(tmpdir) / "{}-{}".format(metadata.md5, metadata.length)
            expected.write_bytes(b"gif")

            with patch(
                "efb_wechat_comwechat_slave.animated_sticker.requests.get"
            ) as request:
                path = cache.get_or_download(sticker_msg(), wait=0.1)

        self.assertEqual(path, str(expected))
        request.assert_not_called()

    def test_exact_cache_hit_does_not_require_url(self):
        msg = sticker_msg()
        msg["message"] = re.sub(r' cdnurl="[^"]+"', "", msg["message"])
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AnimatedStickerCache(Path(tmpdir))
            metadata = extract_sticker_metadata(msg)
            expected = Path(tmpdir) / "{}-{}".format(metadata.md5, metadata.length)
            expected.write_bytes(b"gif")

            path = cache.get_or_download(msg, wait=0.1)

        self.assertEqual(path, str(expected))

    def test_other_declared_length_does_not_hit_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AnimatedStickerCache(Path(tmpdir))
            metadata = extract_sticker_metadata(sticker_msg())
            Path(tmpdir, "{}-{}".format(metadata.md5, metadata.length + 1)).write_bytes(
                b"gif"
            )

            with patch(
                "efb_wechat_comwechat_slave.animated_sticker.requests.get",
                return_value=FakeResponse(),
            ) as request:
                path = cache.get_or_download(sticker_msg(), wait=1)

        self.assertEqual(Path(path).name, "{}-{}".format(metadata.md5, metadata.length))
        request.assert_called_once()

    def test_wait_timeout_keeps_single_background_download_running(self):
        release = threading.Event()
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AnimatedStickerCache(Path(tmpdir))
            with patch(
                "efb_wechat_comwechat_slave.animated_sticker.requests.get",
                return_value=FakeResponse(release=release),
            ) as request:
                with self.assertRaises(StickerPendingError):
                    cache.get_or_download(sticker_msg(), wait=0.01)
                with self.assertRaises(StickerPendingError):
                    cache.get_or_download(sticker_msg(), wait=0.01)
                release.set()
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    try:
                        path = cache.get_or_download(sticker_msg(), wait=0.05)
                        break
                    except StickerPendingError:
                        pass
                else:
                    self.fail("background sticker download did not finish")

                self.assertTrue(Path(path).is_file())

        request.assert_called_once()

    def test_permanent_http_errors_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "efb_wechat_comwechat_slave.animated_sticker.requests.get",
            return_value=FakeResponse(status_code=403),
        ):
            cache = AnimatedStickerCache(Path(tmpdir))
            with self.assertRaises(StickerPermanentError):
                cache.get_or_download(sticker_msg(), wait=1)

    def test_temporary_http_and_validation_errors_do_not_publish(self):
        cases = (
            FakeResponse(status_code=503),
            FakeResponse(content=b""),
            FakeResponse(content=b"bad"),
        )
        for response in cases:
            with self.subTest(status=response.status_code, content=response.content):
                with tempfile.TemporaryDirectory() as tmpdir, patch(
                    "efb_wechat_comwechat_slave.animated_sticker.requests.get",
                    return_value=response,
                ):
                    cache = AnimatedStickerCache(Path(tmpdir))
                    with self.assertRaises(StickerTemporaryError):
                        cache.get_or_download(sticker_msg(), wait=1)
                    self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_lru_removes_oldest_files_after_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            oldest = Path(tmpdir) / "old-1"
            oldest.write_bytes(b"1234")
            time.sleep(0.01)
            cache = AnimatedStickerCache(Path(tmpdir), max_bytes=5)
            with patch(
                "efb_wechat_comwechat_slave.animated_sticker.requests.get",
                return_value=FakeResponse(),
            ):
                current = cache.get_or_download(sticker_msg(), wait=1)

            self.assertFalse(oldest.exists())
            self.assertTrue(Path(current).exists())


if __name__ == "__main__":
    unittest.main()
