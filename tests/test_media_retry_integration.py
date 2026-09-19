import logging
import time
import types
import unittest
from unittest.mock import Mock, patch

from efb_wechat_comwechat_slave.ComWechat import ComWeChatChannel
from efb_wechat_comwechat_slave.media_retry import MEDIA_WAIT_SECONDS, MediaRetryManager
from ehforwarderbot.message import Message


class FakeMessage:
    def __init__(self):
        self.commands = None


class TestMediaRetryIntegration(unittest.TestCase):
    def test_pending_media_uses_fixed_five_second_timeout(self):
        path = "/missing/video.mp4"
        message = {
            "type": "video",
            "filepath": path,
            "timestamp": int(time.time()) - MEDIA_WAIT_SECONDS - 1,
            "msgid": 123,
        }
        retry_manager = types.SimpleNamespace(
            send_failure=Mock(),
            delete_files=Mock(),
        )
        channel = types.SimpleNamespace(
            file_msg={path: (message, "author", "chat")},
            media_retries=retry_manager,
            direct_transfer=False,
            delete_media_after_send=False,
            time_out=300,
            send_efb_msgs=Mock(),
            _voice_database_names=Mock(return_value=[]),
        )

        with patch(
            "efb_wechat_comwechat_slave.ComWechat.MsgProcess",
            return_value=FakeMessage(),
        ):
            ComWeChatChannel._process_pending_file(channel, path)

        retry_manager.send_failure.assert_called_once_with(
            path,
            message,
            "author",
            "chat",
            text="[video 下载超时,请在手机端查看]",
        )
        channel.send_efb_msgs.assert_not_called()
        self.assertNotIn(path, channel.file_msg)

    def test_animated_sticker_failure_creates_retry_placeholder(self):
        retry_manager = types.SimpleNamespace(send_failure=Mock())
        channel = types.SimpleNamespace(
            cache={},
            direct_transfer=False,
            delete_media_after_send=False,
            media_retries=retry_manager,
            logger=logging.getLogger("test-media-retry"),
            _message_references=Mock(return_value=[]),
            _resolve_animated_sticker=Mock(),
            _schedule_mark_as_read=Mock(),
            send_efb_msgs=Mock(),
        )
        msg = {
            "type": "animatedsticker",
            "message": '<emoji cdnurl="https://example.test/a.gif" />',
            "filepath": "",
            "msgid": 456,
            "isSendMsg": 0,
        }

        with patch(
            "efb_wechat_comwechat_slave.ComWechat.MsgProcess",
            side_effect=OSError("download failed"),
        ), patch(
            "efb_wechat_comwechat_slave.ComWechat.coordinator",
            types.SimpleNamespace(
                master=types.SimpleNamespace(get_message_by_id=Mock(return_value=None)),
            ),
        ):
            ComWeChatChannel.handle_msg(channel, msg, "author", "chat")

        retry_manager.send_failure.assert_called_once_with("", msg, "author", "chat")

    def test_get_msg_cdn_maps_upstream_path_to_mount(self):
        channel = types.SimpleNamespace(
            bot=types.SimpleNamespace(
                GetCdn=Mock(return_value={
                    "result": "OK",
                    "msg": 1,
                    "path": r"C:\Users\user\My Documents\WeChat Files\wxid\FileStorage\Video\a.mp4",
                    "download_state": "pending",
                }),
            ),
            base_path=r"C:\Users\user\My Documents\WeChat Files",
            dir="/mnt/wechat/",
            logger=logging.getLogger("test-media-retry"),
        )

        path = ComWeChatChannel.GetMsgCdn(channel, 789)

        self.assertEqual(path, "/mnt/wechat/wxid/FileStorage/Video/a.mp4")
        channel.bot.GetCdn.assert_called_once_with(msgid=789)

    def test_retry_media_delegates_to_manager(self):
        manager = types.SimpleNamespace(retry=Mock(return_value="done"))
        channel = types.SimpleNamespace(media_retries=manager)

        result = ComWeChatChannel.retry_media(channel, "token")

        self.assertEqual(result, "done")
        manager.retry.assert_called_once_with("token")

    def test_failed_edit_closes_attachment_before_reply_fallback(self):
        message = Message()
        message.file = Mock()
        coordinator = types.SimpleNamespace(
            master=object(),
            send_message=Mock(side_effect=RuntimeError("edit failed")),
        )

        with patch("efb_wechat_comwechat_slave.ComWechat.coordinator", coordinator):
            with self.assertRaisesRegex(RuntimeError, "edit failed"):
                ComWeChatChannel.send_efb_msgs(message, edit=True)

        message.file.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
