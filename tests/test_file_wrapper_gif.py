import base64
import tempfile
import unittest
from unittest.mock import patch

from ehforwarderbot import MsgType

from efb_wechat_comwechat_slave.MsgDeco import efb_file_wrapper, efb_image_wrapper

# 1x1 透明 GIF
GIF_1X1 = base64.b64decode(
    "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")


class FileWrapperGifTest(unittest.TestCase):
    @patch(
        "efb_wechat_comwechat_slave.MsgDeco.magic.from_file",
        return_value="video/mp4",
    )
    def test_video_sticker_can_force_animation(self, _from_file):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2")
            path = f.name
        with open(path, "rb") as fh:
            msg = efb_image_wrapper(fh, force_animation=True)
        self.assertEqual(msg.type, MsgType.Animation)
        self.assertEqual(msg.mime, "video/mp4")

    def test_gif_content_becomes_animation(self):
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
            f.write(GIF_1X1)
            path = f.name
        with open(path, "rb") as fh:
            msg = efb_file_wrapper(fh, "tmplu2b_yem.gif")
        self.assertEqual(msg.type, MsgType.Animation)
        self.assertIn("gif", msg.mime)

    def test_jpeg_content_becomes_image(self):
        # 1x1 JPEG
        jpeg_1x1 = base64.b64decode(
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
            "2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/"
            "8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/"
            "8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AvwH/2Q==")
        with tempfile.NamedTemporaryFile(suffix=".jpeg", delete=False) as f:
            f.write(jpeg_1x1)
            path = f.name
        with open(path, "rb") as fh:
            msg = efb_file_wrapper(fh, "tmpzk54g62r.jpeg")
        self.assertEqual(msg.type, MsgType.Image)
        self.assertIn("jpeg", msg.mime)

    def test_non_gif_content_stays_file(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"hello world, this is a plain text file")
            path = f.name
        with open(path, "rb") as fh:
            msg = efb_file_wrapper(fh, "notes.txt")
        self.assertEqual(msg.type, MsgType.File)


if __name__ == "__main__":
    unittest.main()
