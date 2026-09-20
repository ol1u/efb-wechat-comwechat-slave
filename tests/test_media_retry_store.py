import json
import tempfile
import unittest
from pathlib import Path

from efb_wechat_comwechat_slave import db as db_module


class TestMediaRetryStore(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        database_path = Path(self.tempdir.name) / "wxdata.db"
        db_module.database.init(str(database_path))
        db_module.database.start()
        db_module.database.connect()
        db_module.DatabaseManager._create()

    def tearDown(self):
        db_module.database.stop()
        self.tempdir.cleanup()

    def test_retry_payload_is_durable_until_consumed(self):
        payload = {"type": "video", "path": "/media/video.mp4"}

        db_module.DatabaseManager.save_media_retry("token-1", payload, created_at=1)

        self.assertEqual(
            db_module.DatabaseManager.get_media_retry("token-1"),
            payload,
        )
        db_module.DatabaseManager.delete_media_retry("token-1")
        self.assertIsNone(db_module.DatabaseManager.get_media_retry("token-1"))

    def test_retry_store_keeps_only_latest_200_rows(self):
        for index in range(205):
            db_module.DatabaseManager.save_media_retry(
                f"token-{index}",
                {"index": index},
                created_at=index,
            )

        self.assertEqual(db_module.MediaRetry.select().count(), 200)
        self.assertIsNone(db_module.DatabaseManager.get_media_retry("token-0"))
        self.assertEqual(
            db_module.DatabaseManager.get_media_retry("token-204"),
            {"index": 204},
        )

    def test_retry_payload_is_stored_as_json(self):
        payload = {"type": "animatedsticker", "url": "https://example.test/a.gif"}

        db_module.DatabaseManager.save_media_retry("token-json", payload, created_at=1)

        row = db_module.MediaRetry.get_by_id("token-json")
        self.assertEqual(json.loads(row.payload), payload)

    def test_list_retries_returns_valid_payloads_latest_first(self):
        db_module.DatabaseManager.save_media_retry(
            "older", {"type": "video"}, created_at=1
        )
        db_module.DatabaseManager.save_media_retry(
            "newer", {"type": "image"}, created_at=2
        )
        db_module.MediaRetry.create(token="invalid", created_at=3, payload="[]")

        self.assertEqual(
            db_module.DatabaseManager.list_media_retries(),
            [("newer", {"type": "image"}), ("older", {"type": "video"})],
        )

    def test_update_retry_preserves_created_at_and_does_not_recreate(self):
        db_module.DatabaseManager.save_media_retry(
            "token-update", {"attempts": 0}, created_at=10
        )

        self.assertEqual(
            db_module.DatabaseManager.update_media_retry(
                "token-update", {"attempts": 1}
            ),
            1,
        )
        row = db_module.MediaRetry.get_by_id("token-update")
        self.assertEqual(row.created_at, 10)
        self.assertEqual(json.loads(row.payload), {"attempts": 1})

        db_module.DatabaseManager.delete_media_retry("token-update")
        self.assertEqual(
            db_module.DatabaseManager.update_media_retry(
                "token-update", {"attempts": 2}
            ),
            0,
        )
        self.assertIsNone(db_module.DatabaseManager.get_media_retry("token-update"))


if __name__ == "__main__":
    unittest.main()
