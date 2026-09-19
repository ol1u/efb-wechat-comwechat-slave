import base64
import hashlib
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from efb_wechat_comwechat_slave.media_retry import MediaRetryManager
from efb_wechat_comwechat_slave.animated_sticker import (
    StickerPermanentError,
    StickerTemporaryError,
)


class FakeStore:
    def __init__(self):
        self.rows = {}

    def save_media_retry(self, token, payload, *, created_at):
        self.rows[token] = payload

    def get_media_retry(self, token):
        return self.rows.get(token)

    def delete_media_retry(self, token):
        self.rows.pop(token, None)


class FakeMessage:
    def __init__(self):
        self.commands = None
        self.target = None
        self.file = None


class FakeChannel:
    def __init__(self):
        self.db = FakeStore()
        self.direct_transfer = False
        self.delete_media_after_send = False
        self.wxid = "wxid_self"
        self.logger = types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        )
        self.sent = []
        self.fail_edit = False
        self.cdn_path = None
        self.sticker_cache = Mock()

    def send_efb_msgs(self, messages, **kwargs):
        if kwargs.get("edit") and self.fail_edit:
            raise RuntimeError("master cannot edit media")
        self.sent.append((messages, kwargs))

    def GetMsgCdn(self, _msgid):
        return self.cdn_path


class TestMediaRetryManager(unittest.TestCase):
    def setUp(self):
        self.channel = FakeChannel()
        self.manager = MediaRetryManager(self.channel)
        self.chat = types.SimpleNamespace(uid="wxid_friend", name="Friend")
        self.author = types.SimpleNamespace(uid="wxid_friend", name="Friend", alias=None)

    def create(self, path, media_type="video", msgid=123):
        return self.manager.create(
            path,
            {
                "type": media_type,
                "filepath": path,
                "msgid": msgid,
                "sender": "wxid_friend",
                "self": "wxid_self",
            },
            self.author,
            self.chat,
        )

    def test_command_uses_short_token_and_sqlite_payload(self):
        retry_id = self.create("/missing/video.mp4")

        command = self.manager.command(retry_id)

        self.assertEqual(len(retry_id), 16)
        self.assertEqual(command.callable_name, "retry_media")
        self.assertLessEqual(
            len(json.dumps(command.kwargs, separators=(",", ":")).encode()),
            64,
        )
        self.assertEqual(self.channel.db.rows[retry_id]["placeholder_uid"], "123")

    def test_missing_message_id_uses_generated_placeholder_uid(self):
        retry_id = self.manager.create(
            "/missing/video.mp4",
            {"type": "video"},
            self.author,
            self.chat,
        )

        placeholder_uid = self.channel.db.rows[retry_id]["placeholder_uid"]

        self.assertNotEqual(placeholder_uid, "None")
        self.assertTrue(placeholder_uid.isdigit())

    def test_failed_placeholder_send_discards_unreachable_retry(self):
        self.channel.send_efb_msgs = Mock(side_effect=RuntimeError("downstream failed"))

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), self.assertRaisesRegex(RuntimeError, "downstream failed"):
            self.manager.send_failure(
                "/missing/video.mp4",
                {"type": "video", "msgid": 123},
                self.author,
                self.chat,
            )

        self.assertEqual(self.channel.db.rows, {})

    def test_success_redownloads_and_edits_placeholder_then_consumes_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "video.mp4")
            Path(path).write_bytes(b"video")
            self.channel.GetMsgCdn = Mock(return_value=path)
            retry_id = self.create(path)

            with patch(
                "efb_wechat_comwechat_slave.media_retry.MsgProcess",
                return_value=FakeMessage(),
            ), patch.object(
                self.manager,
                "_build_context",
                return_value=(self.chat, self.author),
            ):
                result = self.manager.retry(retry_id)

        self.assertEqual(result, "媒体重试发送成功")
        _message, kwargs = self.channel.sent[0]
        self.assertEqual(kwargs["uid"], "123")
        self.assertTrue(kwargs["edit"])
        self.assertTrue(kwargs["edit_media"])
        self.channel.GetMsgCdn.assert_called_once_with(123)
        self.assertNotIn(retry_id, self.channel.db.rows)

    def test_edit_failure_sends_media_as_reply_to_placeholder(self):
        self.channel.fail_edit = True
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "video.mp4")
            Path(path).write_bytes(b"video")
            self.channel.cdn_path = path
            retry_id = self.create(path)

            with patch(
                "efb_wechat_comwechat_slave.media_retry.MsgProcess",
                side_effect=[FakeMessage(), FakeMessage()],
            ), patch.object(
                self.manager,
                "_build_context",
                return_value=(self.chat, self.author),
            ):
                result = self.manager.retry(retry_id)

        self.assertEqual(result, "媒体重试发送成功")
        reply, kwargs = self.channel.sent[0]
        self.assertEqual(reply.target.uid, "123")
        self.assertIs(reply.target.chat, self.chat)
        self.assertNotIn("edit", kwargs)
        self.assertNotIn(retry_id, self.channel.db.rows)

    def test_missing_media_uses_get_msg_cdn_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            restored = str(Path(tmpdir) / "restored.mp4")
            Path(restored).write_bytes(b"video")
            self.channel.cdn_path = restored
            retry_id = self.create(str(Path(tmpdir) / "missing.mp4"), msgid=456)

            seen = {}

            def convert(msg, _chat, _direct):
                seen.update(msg)
                return FakeMessage()

            with patch(
                "efb_wechat_comwechat_slave.media_retry.MsgProcess",
                side_effect=convert,
            ), patch.object(
                self.manager,
                "_build_context",
                return_value=(self.chat, self.author),
            ):
                result = self.manager.retry(retry_id)

        self.assertEqual(result, "媒体重试发送成功")
        self.assertEqual(seen["filepath"], restored)

    def test_failed_edit_and_reply_keep_token(self):
        self.channel.fail_edit = True
        self.channel.send_efb_msgs = Mock(side_effect=RuntimeError("downstream failed"))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "video.mp4")
            Path(path).write_bytes(b"video")
            self.channel.cdn_path = path
            retry_id = self.create(path)

            with patch(
                "efb_wechat_comwechat_slave.media_retry.MsgProcess",
                side_effect=[FakeMessage(), FakeMessage()],
            ), patch.object(
                self.manager,
                "_build_context",
                return_value=(self.chat, self.author),
            ):
                result = self.manager.retry(retry_id)

        self.assertEqual(result, "媒体重试失败，请稍后再试")
        self.assertIn(retry_id, self.channel.db.rows)

    def test_concurrent_retry_returns_without_running_twice(self):
        started = threading.Event()
        release = threading.Event()
        results = []

        def blocked_retry(_retry_id):
            started.set()
            release.wait(timeout=1)
            return "done"

        with patch.object(self.manager, "_retry", side_effect=blocked_retry) as run:
            worker = threading.Thread(
                target=lambda: results.append(self.manager.retry("token")),
            )
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            duplicate = self.manager.retry("token")
            release.set()
            worker.join(timeout=1)

        self.assertEqual(duplicate, "媒体正在重试，请稍候")
        self.assertEqual(results, ["done"])
        run.assert_called_once_with("token")

    def test_animated_sticker_source_uses_xml_url_instead_of_filepath(self):
        retry_id = self.manager.create(
            "opaque-wechat-file-id",
            {
                "type": "animatedsticker",
                "message": '<emoji cdnurl="https://example.test/a.gif" />',
                "msgid": 123,
            },
            self.author,
            self.chat,
        )

        self.assertEqual(
            self.channel.db.rows[retry_id]["source"],
            "https://example.test/a.gif",
        )

    def test_cached_animated_sticker_retry_does_not_require_url(self):
        msg = {
            "type": "animatedsticker",
            "message": '<emoji md5="b3d6e13019b0571658e5c2c8e8b6d7a9" len="3" />',
            "msgid": 123,
        }
        retry_id = self.manager.create("opaque", msg, self.author, self.chat)
        self.channel.sticker_cache.get_or_download.return_value = "/cache/sticker"

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            result = self.manager.retry(retry_id)

        self.assertEqual(result, "媒体重试发送成功")

    def test_animated_sticker_retry_uses_shared_cache(self):
        msg = {
            "type": "animatedsticker",
            "message": '<emoji cdnurl="https://example.test/a.gif" />',
            "msgid": 123,
        }
        retry_id = self.manager.create("opaque-wechat-file-id", msg, self.author, self.chat)
        seen = {}

        def convert(msg, _chat, _direct):
            seen.update(msg)
            return FakeMessage()

        self.channel.sticker_cache.get_or_download.return_value = "/cache/sticker"
        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            side_effect=convert,
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            result = self.manager.retry(retry_id)

        self.assertEqual(result, "媒体重试发送成功")
        self.channel.sticker_cache.get_or_download.assert_called_once_with(
            dict(msg, filepath="opaque-wechat-file-id"),
            wait=5,
        )
        self.assertEqual(seen["filepath"], "/cache/sticker")

    def test_temporary_sticker_failure_edits_placeholder_and_restores_command(self):
        retry_id = self.manager.create(
            "opaque",
            {
                "type": "animatedsticker",
                "message": '<emoji cdnurl="https://example.test/a.gif" />',
                "msgid": 123,
            },
            self.author,
            self.chat,
        )
        self.channel.sticker_cache.get_or_download.side_effect = StickerTemporaryError(
            "temporary"
        )

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            result = self.manager.retry(retry_id)

        self.assertIsNone(result)
        messages, kwargs = self.channel.sent[0]
        message = messages[0]
        self.assertEqual(kwargs["uid"], "123")
        self.assertTrue(kwargs["edit"])
        self.assertTrue(message.commands)
        self.assertEqual(message.commands[0].kwargs["retry_id"], retry_id)
        self.assertIn(retry_id, self.channel.db.rows)

    def test_share_sticker_uses_sticker_cache_without_get_cdn(self):
        content = b"gif"
        digest = hashlib.md5(content).hexdigest()
        url = "https://example.test/sticker?m={}".format(digest)
        emojiinfo = base64.b64encode(url.encode()).decode()
        msg = {
            "type": "share",
            "message": (
                "<msg><appmsg><type>8</type><appattach>"
                "<totallen>{}</totallen><emoticonmd5>{}</emoticonmd5>"
                "<emojiinfo>{}</emojiinfo>"
                "</appattach></appmsg></msg>"
            ).format(len(content), digest, emojiinfo),
            "msgid": 123,
        }
        retry_id = self.manager.create("/missing/sticker.gif", msg, self.author, self.chat)
        self.channel.GetMsgCdn = Mock(
            side_effect=AssertionError("sticker share must not call GetMsgCdn")
        )
        self.channel.sticker_cache.get_or_download.return_value = "/cache/sticker"
        seen = {}

        def convert(converted, _chat, _direct):
            seen.update(converted)
            return FakeMessage()

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            side_effect=convert,
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ), patch.object(self.manager, "delete_files") as delete_files:
            result = self.manager.retry(retry_id)

        self.assertEqual(result, "媒体重试发送成功")
        self.channel.GetMsgCdn.assert_not_called()
        self.channel.sticker_cache.get_or_download.assert_called_once_with(
            dict(msg, filepath="/missing/sticker.gif"),
            wait=5,
        )
        self.assertEqual(seen["type"], "animatedsticker")
        self.assertEqual(seen["filepath"], "/cache/sticker")
        delete_files.assert_not_called()

    def test_regular_share_does_not_use_sticker_cache_when_get_cdn_fails(self):
        retry_id = self.manager.create(
            "/missing/file",
            {
                "type": "share",
                "message": "<msg><appmsg><type>6</type></appmsg></msg>",
                "msgid": 123,
            },
            self.author,
            self.chat,
        )

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            result = self.manager.retry(retry_id)

        self.assertIsNone(result)
        self.channel.sticker_cache.get_or_download.assert_not_called()

    def test_permanent_sticker_failure_edits_placeholder_and_consumes_token(self):
        retry_id = self.manager.create(
            "opaque",
            {
                "type": "animatedsticker",
                "message": '<emoji cdnurl="https://example.test/a.gif" />',
                "msgid": 123,
            },
            self.author,
            self.chat,
        )
        self.channel.sticker_cache.get_or_download.side_effect = StickerPermanentError(
            "expired"
        )

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            result = self.manager.retry(retry_id)

        self.assertEqual(result, "动态表情下载链接已失效，无法重试，请在手机端查看。")
        messages, kwargs = self.channel.sent[0]
        message = messages[0]
        self.assertEqual(kwargs["uid"], "123")
        self.assertTrue(kwargs["edit"])
        self.assertFalse(message.commands)
        self.assertNotIn(retry_id, self.channel.db.rows)


if __name__ == "__main__":
    unittest.main()
