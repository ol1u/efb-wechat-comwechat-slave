import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ehforwarderbot.message import Message, MessageCommand, MessageCommands
from ehforwarderbot.types import MessageID

from .ChatMgr import ChatMgr
from .CustomTypes import EFBGroupChat, EFBGroupMember, EFBPrivateChat
from .MsgProcess import MsgProcess
from .Utils import (
    MEDIA_WAIT_SECONDS,
    download_file,
    extract_sticker_url,
    resolve_hooked_wechat_image_path,
)


MEDIA_DELETE_TYPES = {"image", "video", "file", "share"}
MEDIA_RETRY_TYPES = MEDIA_DELETE_TYPES | {"animatedsticker"}
MEDIA_RETRY_FIELDS = (
    "type",
    "message",
    "msgid",
    "svrid",
    "sender",
    "self",
    "wxid",
    "extrainfo",
    "thumb_path",
)


class MediaRetryManager:
    def __init__(self, channel: Any) -> None:
        self.channel = channel
        self._running = set()
        self._running_lock = threading.Lock()

    def create(self, path, msg, author, chat, *, placeholder_uid=None):
        source = path or extract_sticker_url(msg)
        if not source:
            raise ValueError("media retry source is missing")
        retry_msg = {key: msg[key] for key in MEDIA_RETRY_FIELDS if key in msg}
        retry_msg["type"] = msg.get("type")
        retry_msg["filepath"] = path
        author_uid = getattr(author, "uid", None)
        payload = {
            "source": source,
            "type": msg.get("type"),
            "placeholder_uid": self.placeholder_uid(msg, placeholder_uid),
            "msg": retry_msg,
            "chat": {
                "uid": getattr(chat, "uid", None),
                "name": getattr(chat, "name", None),
            },
            "author": {
                "uid": author_uid,
                "name": getattr(author, "name", None),
                "alias": getattr(author, "alias", None),
                "is_self": bool(
                    (msg.get("self") and author_uid == msg.get("self"))
                    or (self.channel.wxid and author_uid == self.channel.wxid)
                ),
            },
        }
        retry_id = secrets.token_hex(8)
        self.channel.db.save_media_retry(retry_id, payload, created_at=time.time_ns())
        return retry_id

    @staticmethod
    def placeholder_uid(msg, uid=None):
        if uid is not None:
            return str(uid)
        for key in ("msgid", "svrid"):
            if msg.get(key) is not None:
                return str(msg[key])
        return str(time.time_ns())

    @staticmethod
    def command(retry_id):
        return MessageCommand(
            name="Retry",
            callable_name="retry_media",
            kwargs={"retry_id": retry_id},
        )

    def send_failure(self, path, msg, author, chat, text=None, uid=None):
        media_type = msg.get("type")
        if media_type not in MEDIA_RETRY_TYPES:
            return
        placeholder_uid = self.placeholder_uid(msg, uid)
        retry_id = self.create(
            path,
            msg,
            author,
            chat,
            placeholder_uid=placeholder_uid,
        )
        failed_msg = dict(msg)
        failed_msg["type"] = "text"
        failed_msg["message"] = text or f"[{media_type} 下载失败,请在手机端查看]"
        messages = MsgProcess(failed_msg, chat, self.channel.direct_transfer)
        commands = MessageCommands([self.command(retry_id)])
        for message in self._as_list(messages):
            message.commands = commands
        try:
            self.channel.send_efb_msgs(
                messages,
                author=author,
                chat=chat,
                uid=MessageID(placeholder_uid),
            )
        except Exception:
            self.channel.db.delete_media_retry(retry_id)
            raise

    def retry(self, retry_id):
        if not isinstance(retry_id, str):
            return "重试上下文已失效，请重新接收媒体"
        with self._running_lock:
            if retry_id in self._running:
                return "媒体正在重试，请稍候"
            self._running.add(retry_id)
        try:
            return self._retry(retry_id)
        finally:
            with self._running_lock:
                self._running.discard(retry_id)

    def _retry(self, retry_id):
        media = self.channel.db.get_media_retry(retry_id)
        if not isinstance(media, dict):
            return "重试上下文已失效，请重新接收媒体"

        source = media.get("source")
        media_type = media.get("type")
        placeholder_uid = media.get("placeholder_uid")
        if media_type not in MEDIA_RETRY_TYPES or not source or not placeholder_uid:
            return "不支持重试此媒体"

        temporary = None
        try:
            if media_type == "animatedsticker":
                temporary = download_file(source, retry=1, timeout=MEDIA_WAIT_SECONDS)
                media_path = temporary.name
            else:
                msg = media.get("msg") or {}
                msgid = msg.get("msgid") or msg.get("svrid")
                if msgid is None:
                    return "媒体重新下载失败，请稍后再试"
                restored_path = self.channel.GetMsgCdn(msgid)
                media_path = self._wait_for_media(restored_path, media_type)
                if media_path is None:
                    return "媒体重新下载失败，请稍后再试"

            msg = dict(media.get("msg") or {})
            msg["type"] = media_type
            msg["filepath"] = media_path
            chat, author = self._build_context(media)
            try:
                messages = MsgProcess(msg, chat, self.channel.direct_transfer)
                self.channel.send_efb_msgs(
                    messages,
                    author=author,
                    chat=chat,
                    uid=MessageID(placeholder_uid),
                    edit=True,
                    edit_media=True,
                )
            except Exception:
                self.channel.logger.warning(
                    "Failed to edit media placeholder; sending a reply: token=%s uid=%s",
                    retry_id,
                    placeholder_uid,
                    exc_info=True,
                )
                messages = MsgProcess(msg, chat, self.channel.direct_transfer)
                target = Message(uid=MessageID(placeholder_uid), chat=chat)
                for message in self._as_list(messages):
                    message.target = target
                self.channel.send_efb_msgs(
                    messages,
                    author=author,
                    chat=chat,
                    uid=MessageID(f"{placeholder_uid}-retry-{time.time_ns()}"),
                )
        except Exception:
            self.channel.logger.exception(
                "Failed to retry media: type=%s token=%s",
                media_type,
                retry_id,
            )
            return "媒体重试失败，请稍后再试"
        finally:
            if temporary is not None:
                temporary.close()

        if self.channel.delete_media_after_send and media_type in MEDIA_DELETE_TYPES:
            self.delete_files(source, media_path)
        self.channel.db.delete_media_retry(retry_id)
        return "媒体重试发送成功"

    def _wait_for_media(self, path, media_type):
        if not isinstance(path, str) or not path:
            return None
        deadline = time.monotonic() + MEDIA_WAIT_SECONDS
        while True:
            existing = self._existing_media_path(path, media_type)
            if existing is not None:
                return existing
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    @staticmethod
    def _existing_media_path(path, media_type):
        if media_type == "image":
            resolved = resolve_hooked_wechat_image_path(path)
            if resolved:
                return resolved
        return path if isinstance(path, str) and os.path.isfile(path) else None

    @staticmethod
    def _as_list(messages):
        return messages if isinstance(messages, list) else [messages]

    def delete_files(self, *paths):
        for path in {path for path in paths if path and os.path.isfile(path)}:
            try:
                os.remove(path)
            except OSError:
                self.channel.logger.warning(
                    "Failed to delete media attachment: %s",
                    path,
                    exc_info=True,
                )

    @staticmethod
    def _build_context(payload) -> Tuple[Any, Any]:
        chat_info = payload.get("chat") or {}
        author_info = payload.get("author") or {}
        chat_uid = chat_info.get("uid")
        chat_name = chat_info.get("name") or chat_uid
        author_uid = author_info.get("uid")
        if not chat_uid:
            raise ValueError("重试失败，缺少聊天信息")

        if "@chatroom" in chat_uid:
            chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                uid=chat_uid,
                name=chat_name,
            ))
            if author_info.get("is_self"):
                author = chat.self
            else:
                author = ChatMgr.build_efb_chat_as_member(chat, EFBGroupMember(
                    uid=author_uid,
                    name=author_info.get("name") or author_uid,
                    alias=author_info.get("alias"),
                ))
        else:
            chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                uid=chat_uid,
                name=chat_name,
            ))
            author = chat.self if author_info.get("is_self") else chat.other
        return chat, author
