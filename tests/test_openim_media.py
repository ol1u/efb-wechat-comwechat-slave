import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = (
    Path(__file__).parents[1]
    / "efb_wechat_comwechat_slave"
    / "openim_media.py"
)
SPEC = importlib.util.spec_from_file_location("openim_media", MODULE_PATH)
openim_media = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(openim_media)

OpenIMMediaError = openim_media.OpenIMMediaError
download_openim_media = openim_media.download_openim_media
is_openim_media_message = openim_media.is_openim_media_message
is_openim_message = openim_media.is_openim_message
windows_to_wsl_path = openim_media.windows_to_wsl_path


class OpenIMMessageDetectionTest(unittest.TestCase):
    def test_detects_openim_sender(self):
        self.assertTrue(is_openim_message({
            "sender": "25984985276141769@openim",
            "wxid": "25984985276141769@openim",
        }))

    def test_detects_openim_from_message_xml(self):
        self.assertTrue(is_openim_message({
            "sender": "123@chatroom",
            "message": (
                '<msg><emoji fromusername="25984985276141769@openim" '
                'tousername="wxid_self"/></msg>'
            ),
        }))

    def test_private_wechat_media_is_not_matched(self):
        self.assertFalse(is_openim_message({
            "sender": "wxid_friend",
            "wxid": "wxid_friend",
            "message": '<msg><voicemsg fromusername="wxid_friend"/></msg>',
        }))
        self.assertFalse(is_openim_media_message({
            "type": "voice",
            "sender": "wxid_friend",
        }))

    def test_text_mention_of_openim_is_not_an_id(self):
        self.assertFalse(is_openim_message({
            "sender": "wxid_friend",
            "message": "联系 someone@openim 获取文件",
        }))


class OpenIMMediaDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tempdir.name) / "media.silk")
        Path(self.path).write_bytes(b"#!SILK_V3")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_voice_uses_msgid_only(self):
        bot = Mock()
        bot.GetVoiceMsg.return_value = {
            "msg": 1,
            "result": "OK",
            "path": self.path,
        }
        msg = {"type": "voice", "msgid": 2185471521836615090}

        result = download_openim_media(bot, msg)

        self.assertEqual(result, self.path)
        bot.GetVoiceMsg.assert_called_once_with(
            msgid="2185471521836615090"
        )
        bot.GetEmojiMsg.assert_not_called()

    def test_animated_sticker_passes_original_xml(self):
        bot = Mock()
        bot.GetEmojiMsg.return_value = {
            "msg": 1,
            "result": "OK",
            "path": self.path,
        }
        xml = '<msg><emoji tpurl="https://example.test/a" /></msg>'
        msg = {
            "type": "animatedsticker",
            "msgid": "123",
            "message": xml,
        }

        result = download_openim_media(bot, msg)

        self.assertEqual(result, self.path)
        bot.GetEmojiMsg.assert_called_once_with(msgid="123", content=xml)
        bot.GetVoiceMsg.assert_not_called()

    def test_native_error_is_raised(self):
        bot = Mock()
        bot.GetVoiceMsg.return_value = {
            "msg": 0,
            "result": "ERROR",
            "err_msg": "voice media not found",
        }

        with self.assertRaisesRegex(OpenIMMediaError, "voice media not found"):
            download_openim_media(
                bot,
                {"type": "voice", "msgid": "123"},
            )


class WindowsPathConversionTest(unittest.TestCase):
    @patch.object(
        openim_media.subprocess,
        "run",
        side_effect=FileNotFoundError,
    )
    def test_falls_back_to_mnt_path(self, _run):
        self.assertEqual(
            windows_to_wsl_path(
                r"C:\Users\123\Documents\WeChat Files\voice.silk",
                True,
            ),
            "/mnt/c/Users/123/Documents/WeChat Files/voice.silk",
        )

    def test_non_wsl_path_is_unchanged(self):
        path = r"C:\Users\123\voice.silk"
        self.assertEqual(windows_to_wsl_path(path, False), path)


if __name__ == "__main__":
    unittest.main()
