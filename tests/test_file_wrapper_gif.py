import base64
import tempfile
import unittest

from ehforwarderbot import MsgType

from efb_wechat_comwechat_slave.MsgDeco import efb_file_wrapper

# 1x1 透明 GIF
GIF_1X1 = base64.b64decode(
    "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")


class FileWrapperGifTest(unittest.TestCase):
    def test_gif_content_becomes_animation(self):
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as f:
            f.write(GIF_1X1)
            path = f.name
        with open(path, "rb") as fh:
            msg = efb_file_wrapper(fh, "tmplu2b_yem.gif")
        self.assertEqual(msg.type, MsgType.Animation)
        self.assertIn("gif", msg.mime)

    def test_non_gif_content_stays_file(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"hello world, this is a plain text file")
            path = f.name
        with open(path, "rb") as fh:
            msg = efb_file_wrapper(fh, "notes.txt")
        self.assertEqual(msg.type, MsgType.File)


if __name__ == "__main__":
    unittest.main()
