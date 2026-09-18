import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from efb_wechat_comwechat_slave.media_retry import MediaRetryManager


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
            warning=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        )
        self.sent = []
        self.fail_edit = False
        self.cdn_path = None

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

    def test_animated_sticker_retry_uses_downloaded_local_file(self):
        retry_id = self.create(
            "https://example.test/a.gif",
            media_type="animatedsticker",
        )
        seen = {}

        def convert(msg, _chat, _direct):
            seen.update(msg)
            return FakeMessage()

        with tempfile.NamedTemporaryFile() as downloaded, patch(
            "efb_wechat_comwechat_slave.media_retry.download_file",
            return_value=downloaded,
        ) as download, patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            side_effect=convert,
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            result = self.manager.retry(retry_id)
            downloaded_path = downloaded.name

        self.assertEqual(result, "媒体重试发送成功")
        download.assert_called_once_with(
            "https://example.test/a.gif",
            retry=1,
            timeout=5,
        )
        self.assertEqual(seen["filepath"], downloaded_path)


if __name__ == "__main__":
    unittest.main()
