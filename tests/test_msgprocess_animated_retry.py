import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from efb_wechat_comwechat_slave.MsgProcess import MsgProcess


class TestAnimatedStickerProcessing(unittest.TestCase):
    def test_missing_sticker_url_fails_cleanly(self):
        with self.assertRaisesRegex(ValueError, "animated sticker URL is missing"):
            MsgProcess({"type": "animatedsticker"}, chat=None)

    def test_initial_download_uses_sticker_cache_resolver(self):
        msg = {
            "type": "animatedsticker",
            "message": '<emoji cdnurl="https://example.test/a.gif" />',
        }
        resolver = Mock(return_value="/cache/sticker")

        with patch(
            "efb_wechat_comwechat_slave.MsgProcess.load_local_file_to_temp",
            return_value="local-file",
        ) as load, patch(
            "efb_wechat_comwechat_slave.MsgProcess.efb_image_wrapper",
            return_value="image",
        ) as wrap:
            result = MsgProcess(msg, chat=None, animated_sticker_resolver=resolver)

        self.assertEqual(result, "image")
        resolver.assert_called_once_with(msg)
        load.assert_called_once_with("/cache/sticker")
        wrap.assert_called_once_with("local-file")

    def test_retry_uses_downloaded_local_sticker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sticker.gif"
            path.write_bytes(b"gif")
            msg = {
                "type": "animatedsticker",
                "message": '<emoji cdnurl="https://example.test/a.gif" />',
                "filepath": str(path),
            }

            with patch(
                "efb_wechat_comwechat_slave.MsgProcess.download_file",
            ) as download, patch(
                "efb_wechat_comwechat_slave.MsgProcess.load_local_file_to_temp",
                return_value="local-file",
            ) as load, patch(
                "efb_wechat_comwechat_slave.MsgProcess.efb_image_wrapper",
                return_value="image",
            ):
                result = MsgProcess(msg, chat=None)

        self.assertEqual(result, "image")
        download.assert_not_called()
        load.assert_called_once_with(str(path))

    def test_cached_sticker_uses_shared_temp_even_with_direct_transfer(self):
        msg = {"type": "animatedsticker"}
        resolver = Mock(return_value="/cache/sticker")

        with patch(
            "efb_wechat_comwechat_slave.MsgProcess.load_local_file_to_temp",
            return_value="shared-temp",
        ) as load, patch(
            "efb_wechat_comwechat_slave.MsgProcess.efb_image_wrapper",
            return_value="image",
        ):
            result = MsgProcess(
                msg,
                chat=None,
                direct_transfer=True,
                animated_sticker_resolver=resolver,
            )

        self.assertEqual(result, "image")
        load.assert_called_once_with("/cache/sticker")

    def test_cached_gif_sticker_uses_a_gif_suffixed_shared_temp_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "md5-length"
            path.write_bytes(b"GIF89a" + b"\x00" * 16)
            seen = {}

            def wrap(file):
                seen["name"] = file.name
                seen["content"] = file.read()
                file.close()
                return "animation"

            with patch(
                "efb_wechat_comwechat_slave.MsgProcess.efb_image_wrapper",
                side_effect=wrap,
            ):
                result = MsgProcess(
                    {"type": "animatedsticker", "filepath": str(path)},
                    chat=None,
                )

        self.assertEqual(result, "animation")
        self.assertEqual(Path(seen["name"]).suffix, ".gif")
        self.assertTrue(seen["content"].startswith(b"GIF89a"))


if __name__ == "__main__":
    unittest.main()
