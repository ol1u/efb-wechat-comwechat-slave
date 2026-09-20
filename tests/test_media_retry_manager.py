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
        self.created_at = {}

    def save_media_retry(self, token, payload, *, created_at):
        self.rows[token] = payload
        self.created_at[token] = created_at

    def get_media_retry(self, token):
        return self.rows.get(token)

    def delete_media_retry(self, token):
        self.rows.pop(token, None)
        self.created_at.pop(token, None)

    def list_media_retries(self):
        return [
            (token, self.rows[token])
            for token in sorted(
                self.rows,
                key=lambda token: (self.created_at[token], token),
                reverse=True,
            )
        ]

    def update_media_retry(self, token, payload):
        if token not in self.rows:
            return 0
        self.rows[token] = payload
        return 1


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
        self.logged_in = True

    def send_efb_msgs(self, messages, **kwargs):
        if kwargs.get("edit") and self.fail_edit:
            raise RuntimeError("master cannot edit media")
        self.sent.append((messages, kwargs))

    def GetMsgCdn(self, _msgid):
        return self.cdn_path

    def is_login(self):
        return self.logged_in


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
        with patch("efb_wechat_comwechat_slave.media_retry.time.time", return_value=100):
            retry_id = self.create("/missing/video.mp4")

        command = self.manager.command(retry_id)

        self.assertEqual(len(retry_id), 16)
        self.assertEqual(command.callable_name, "retry_media")
        self.assertLessEqual(
            len(json.dumps(command.kwargs, separators=(",", ":")).encode()),
            64,
        )
        self.assertEqual(self.channel.db.rows[retry_id]["placeholder_uid"], "123")
        self.assertEqual(
            self.channel.db.rows[retry_id]["_auto_retry"],
            {"attempts": 0, "next_at": 130},
        )

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

    def test_existing_source_edits_placeholder_without_redownloading(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "video.mp4")
            Path(path).write_bytes(b"video")
            self.channel.GetMsgCdn = Mock(
                side_effect=AssertionError("existing source must not be redownloaded")
            )
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
        self.channel.GetMsgCdn.assert_not_called()
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

    def test_concurrent_manual_retry_restores_command_without_running_twice(self):
        started = threading.Event()
        release = threading.Event()
        results = []
        retry_id = self.create("/missing/video.mp4")

        def blocked_retry(_retry_id, *, automatic=False):
            self.assertFalse(automatic)
            started.set()
            release.wait(timeout=1)
            return "done"

        with patch.object(self.manager, "_retry", side_effect=blocked_retry) as run, patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            worker = threading.Thread(
                target=lambda: results.append(self.manager.retry(retry_id)),
            )
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            duplicate = self.manager.retry(retry_id)
            release.set()
            worker.join(timeout=1)

        self.assertIsNone(duplicate)
        self.assertEqual(results, ["done"])
        run.assert_called_once_with(retry_id, automatic=False)
        messages, kwargs = self.channel.sent[0]
        self.assertTrue(kwargs["edit"])
        self.assertEqual(messages[0].commands[0].kwargs["retry_id"], retry_id)

    def test_automatic_retry_skips_when_token_is_already_running(self):
        retry_id = self.create("/missing/video.mp4")
        with self.manager._running_lock:
            self.manager._running.add(retry_id)

        with patch.object(self.manager, "_retry") as run:
            result = self.manager.retry(retry_id, automatic=True)

        self.assertIsNone(result)
        run.assert_not_called()

    def test_due_temporary_failure_advances_backoff_and_keeps_token(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.db.rows[retry_id]["_auto_retry"]["next_at"] = 1
        self.channel.cdn_path = None

        with patch("efb_wechat_comwechat_slave.media_retry.time.time", return_value=130), patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            self.assertTrue(self.manager.run_due_once())

        state = self.channel.db.rows[retry_id]["_auto_retry"]
        self.assertEqual(state, {"attempts": 1, "next_at": 250})
        self.assertEqual(
            self.channel.sent[0][0][0].commands[0].kwargs["retry_id"],
            retry_id,
        )

    def test_automatic_failures_exhaust_all_three_attempts(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.db.rows[retry_id]["_auto_retry"]["next_at"] = 1

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            for now, expected in (
                (100, {"attempts": 1, "next_at": 220}),
                (220, {"attempts": 2, "next_at": 820}),
                (820, {"attempts": 3, "next_at": None}),
            ):
                with patch(
                    "efb_wechat_comwechat_slave.media_retry.time.time",
                    return_value=now,
                ):
                    self.assertTrue(self.manager.run_due_once(now=now))
                self.assertEqual(
                    self.channel.db.rows[retry_id]["_auto_retry"],
                    expected,
                )

        self.assertIn(retry_id, self.channel.db.rows)
        self.assertEqual(len(self.channel.sent), 3)

    def test_due_success_consumes_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "video.mp4")
            Path(path).write_bytes(b"video")
            self.channel.cdn_path = path
            retry_id = self.create(path)
            self.channel.db.rows[retry_id]["_auto_retry"]["next_at"] = 1

            with patch("efb_wechat_comwechat_slave.media_retry.time.time", return_value=130), patch(
                "efb_wechat_comwechat_slave.media_retry.MsgProcess",
                return_value=FakeMessage(),
            ), patch.object(
                self.manager,
                "_build_context",
                return_value=(self.chat, self.author),
            ):
                self.assertTrue(self.manager.run_due_once())

        self.assertNotIn(retry_id, self.channel.db.rows)

    def test_due_permanent_sticker_failure_consumes_token(self):
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
        self.channel.db.rows[retry_id]["_auto_retry"]["next_at"] = 1

        with patch("efb_wechat_comwechat_slave.media_retry.time.time", return_value=130), patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            self.assertTrue(self.manager.run_due_once())

        self.assertNotIn(retry_id, self.channel.db.rows)

    def test_exhausted_retry_is_not_downloaded_and_keeps_token(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.db.rows[retry_id]["_auto_retry"] = {
            "attempts": 3,
            "next_at": None,
        }

        with patch.object(self.manager, "retry") as retry:
            self.assertFalse(self.manager.run_due_once(now=1000))

        retry.assert_not_called()
        self.assertIn(retry_id, self.channel.db.rows)

    def test_only_latest_due_retry_runs_per_scan(self):
        older = self.create("/missing/older.mp4", msgid=1)
        newer = self.create("/missing/newer.mp4", msgid=2)
        self.channel.db.rows[older]["_auto_retry"]["next_at"] = 1
        self.channel.db.rows[newer]["_auto_retry"]["next_at"] = 1

        with patch.object(self.manager, "retry") as retry:
            self.assertTrue(self.manager.run_due_once(now=1000))

        retry.assert_called_once_with(newer, automatic=True)

    def test_legacy_payload_is_initialized_without_immediate_retry(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.db.rows[retry_id].pop("_auto_retry")

        with patch.object(self.manager, "retry") as retry:
            self.assertFalse(self.manager.run_due_once(now=1000))

        retry.assert_not_called()
        self.assertEqual(
            self.channel.db.rows[retry_id]["_auto_retry"],
            {"attempts": 0, "next_at": 1030},
        )

    def test_new_manager_resumes_persisted_schedule(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.db.rows[retry_id]["_auto_retry"] = {
            "attempts": 1,
            "next_at": 500,
        }
        restored = MediaRetryManager(self.channel)

        with patch.object(restored, "retry") as retry:
            self.assertFalse(restored.run_due_once(now=499))
            self.assertTrue(restored.run_due_once(now=500))

        retry.assert_called_once_with(retry_id, automatic=True)

    def test_logged_out_regular_media_is_skipped_without_advancing(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.logged_in = False
        original = dict(self.channel.db.rows[retry_id]["_auto_retry"])

        with patch.object(self.manager, "retry") as retry:
            self.assertFalse(self.manager.run_due_once(now=1000))

        retry.assert_not_called()
        self.assertEqual(self.channel.db.rows[retry_id]["_auto_retry"], original)

    def test_logged_out_sticker_is_still_retried(self):
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
        self.channel.logged_in = False
        self.channel.db.rows[retry_id]["_auto_retry"]["next_at"] = 1

        with patch.object(self.manager, "retry") as retry:
            self.assertTrue(self.manager.run_due_once(now=1000))

        retry.assert_called_once_with(retry_id, automatic=True)

    def test_logged_out_sticker_share_is_still_retried(self):
        retry_id = self.manager.create(
            "/missing/sticker.gif",
            {
                "type": "share",
                "message": "<msg><appmsg><type>8</type></appmsg></msg>",
                "msgid": 123,
            },
            self.author,
            self.chat,
        )
        self.channel.logged_in = False
        self.channel.db.rows[retry_id]["_auto_retry"]["next_at"] = 1

        with patch.object(self.manager, "retry") as retry:
            self.assertTrue(self.manager.run_due_once(now=1000))

        retry.assert_called_once_with(retry_id, automatic=True)

    def test_manual_temporary_failure_delays_without_consuming_attempt(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.db.rows[retry_id]["_auto_retry"] = {
            "attempts": 1,
            "next_at": 100,
        }

        with patch("efb_wechat_comwechat_slave.media_retry.time.time", return_value=200), patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            self.assertIsNone(self.manager.retry(retry_id))

        self.assertEqual(
            self.channel.db.rows[retry_id]["_auto_retry"],
            {"attempts": 1, "next_at": 320},
        )

    def test_manual_failure_does_not_restart_exhausted_schedule(self):
        retry_id = self.create("/missing/video.mp4")
        self.channel.db.rows[retry_id]["_auto_retry"] = {
            "attempts": 3,
            "next_at": None,
        }

        with patch(
            "efb_wechat_comwechat_slave.media_retry.MsgProcess",
            return_value=FakeMessage(),
        ), patch.object(
            self.manager,
            "_build_context",
            return_value=(self.chat, self.author),
        ):
            self.assertIsNone(self.manager.retry(retry_id))

        self.assertEqual(
            self.channel.db.rows[retry_id]["_auto_retry"],
            {"attempts": 3, "next_at": None},
        )

    def test_worker_start_and_stop_are_idempotent(self):
        self.manager.start()
        thread = self.manager._worker
        self.manager.start()

        self.assertIs(self.manager._worker, thread)
        self.assertTrue(thread.is_alive())

        self.manager.stop()
        self.manager.stop()

        self.assertFalse(thread.is_alive())

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

    def test_permanent_sticker_failure_consumes_token_when_edit_fails(self):
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

        with patch.object(
            self.manager,
            "_edit_failure",
            side_effect=RuntimeError("downstream failed"),
        ):
            result = self.manager.retry(retry_id)

        self.assertEqual(result, "动态表情下载链接已失效，无法重试，请在手机端查看。")
        self.assertNotIn(retry_id, self.channel.db.rows)


if __name__ == "__main__":
    unittest.main()
