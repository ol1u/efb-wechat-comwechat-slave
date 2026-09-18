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


if __name__ == "__main__":
    unittest.main()
