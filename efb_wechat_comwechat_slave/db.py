import json
import logging

from peewee import (
    BigIntegerField,
    CharField,
    Model,
    TextField,
)
from playhouse.sqliteq import SqliteQueueDatabase
from ehforwarderbot import utils

database = SqliteQueueDatabase(None, autostart=False)


class BaseModel(Model):
    class Meta:
        database = database


class GroupChatInfo(BaseModel):
    group_uid = CharField()
    wxid = CharField()
    group_alias = TextField()

    class Meta:
        indexes = (
            (('group_uid', 'wxid'), True),  # Unique index on group_uid and wxid
        )

class WxMsgLog(BaseModel):
    wx_msg_id = CharField()
    # 通常等于 wx_msg_id，在 efb master 发送富文本消息时会是多个 wx_msg_id 合并后的值
    efb_msg_id = CharField()
    wx_type = CharField()
    wxid = CharField()
    sender = CharField()
    xml = TextField()

    class Meta:
        indexes = (
            (('group_uid', 'wxid'), True),  # Unique index on group_uid and wxid
        )


class MediaRetry(BaseModel):
    token = CharField(primary_key=True)
    created_at = BigIntegerField(index=True)
    payload = TextField()

    class Meta:
        table_name = "media_retry"


class DatabaseManager:
    logger = logging.getLogger(__name__)

    def __init__(self, channel: "ComWeChatChannel"):
        base_path = utils.get_data_path(channel.channel_id)

        self.logger.debug("Loading database...")
        database_path = base_path / "wxdata.db"
        database.init(str(database_path))
        database.start()
        database.connect()
        self.logger.debug("Database loaded.")

        self.logger.debug("Checking database migration...")
        self._create()
        self.logger.debug("Database migration finished...")

    def stop_worker(self):
        database.stop()

    @staticmethod
    def _create():
        """
        Initializing tables.
        """
        database.create_tables([GroupChatInfo, MediaRetry], safe=True)
        cursor = database.execute_sql(
            """
            CREATE TRIGGER IF NOT EXISTS media_retry_keep_latest_200
            AFTER INSERT ON media_retry
            BEGIN
                DELETE FROM media_retry
                WHERE token IN (
                    SELECT token
                    FROM media_retry
                    ORDER BY created_at DESC, token DESC
                    LIMIT -1 OFFSET 200
                );
            END
            """
        )
        cursor.fetchall()

    @staticmethod
    def get_all_group_aliases():
        return list(GroupChatInfo.select())

    @staticmethod
    def update_group_alias(group_uid, wxid, alias):
        return GroupChatInfo.replace(
            group_uid = group_uid,
            wxid = wxid,
            group_alias = alias,
        ).execute()

    @staticmethod
    def save_media_retry(token, payload, *, created_at):
        return MediaRetry.replace(
            token=token,
            created_at=created_at,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ).execute()

    @staticmethod
    def get_media_retry(token):
        retry = MediaRetry.get_or_none(MediaRetry.token == token)
        if retry is None:
            return None
        return DatabaseManager._decode_media_retry(token, retry.payload)

    @staticmethod
    def list_media_retries():
        retries = []
        query = MediaRetry.select().order_by(
            MediaRetry.created_at.desc(),
            MediaRetry.token.desc(),
        )
        for retry in query:
            payload = DatabaseManager._decode_media_retry(
                retry.token,
                retry.payload,
            )
            if payload is not None:
                retries.append((retry.token, payload))
        return retries

    @staticmethod
    def update_media_retry(token, payload):
        return (
            MediaRetry.update(
                payload=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            .where(MediaRetry.token == token)
            .execute()
        )

    @staticmethod
    def _decode_media_retry(token, raw_payload):
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            DatabaseManager.logger.warning(
                "Ignoring invalid media retry payload: token=%s",
                token,
                exc_info=True,
            )
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def delete_media_retry(token):
        return MediaRetry.delete().where(MediaRetry.token == token).execute()
