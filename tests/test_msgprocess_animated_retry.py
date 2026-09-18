import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from efb_wechat_comwechat_slave.MsgProcess import MsgProcess


class TestAnimatedStickerProcessing(unittest.TestCase):
    def test_missing_sticker_url_fails_cleanly(self):
        with self.assertRaisesRegex(ValueError, "animated sticker URL is missing"):
            MsgProcess({"type": "animatedsticker"}, chat=None)

    def test_initial_download_uses_single_five_second_attempt(self):
        downloaded = object()
        msg = {
            "type": "animatedsticker",
            "message": '<emoji cdnurl="https://example.test/a.gif" />',
        }

        with patch(
            "efb_wechat_comwechat_slave.MsgProcess.download_file",
            return_value=downloaded,
        ) as download, patch(
            "efb_wechat_comwechat_slave.MsgProcess.efb_image_wrapper",
            return_value="image",
        ) as wrap:
            result = MsgProcess(msg, chat=None)

        self.assertEqual(result, "image")
        download.assert_called_once_with(
            "https://example.test/a.gif",
            retry=1,
            timeout=5,
        )
        wrap.assert_called_once_with(downloaded)

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
                "efb_wechat_comwechat_slave.MsgProcess.load_local_file_for_transfer",
                return_value="local-file",
            ) as load, patch(
                "efb_wechat_comwechat_slave.MsgProcess.efb_image_wrapper",
                return_value="image",
            ):
                result = MsgProcess(msg, chat=None)

        self.assertEqual(result, "image")
        download.assert_not_called()
        load.assert_called_once_with(str(path), False)


if __name__ == "__main__":
    unittest.main()
