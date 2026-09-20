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
from .animated_sticker import (
    StickerPermanentError,
    StickerTemporaryError,
    is_sticker_share,
)
from .Utils import (
    MEDIA_WAIT_SECONDS,
    extract_sticker_url,
    resolve_hooked_wechat_image_path,
)


MEDIA_DELETE_TYPES = {"image", "video", "file", "share"}
MEDIA_RETRY_TYPES = MEDIA_DELETE_TYPES | {"animatedsticker"}
MEDIA_TYPE_NAMES = {
    "image": "图片",
    "video": "视频",
    "file": "文件",
    "share": "媒体",
    "animatedsticker": "动态表情",
}
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
    "url",
)
AUTO_RETRY_DELAYS = (30, 120, 600)
AUTO_RETRY_IDLE_SECONDS = 1
AUTO_RETRY_STATE_KEY = "_auto_retry"


class MediaRetryManager:
    def __init__(self, channel: Any) -> None:
        self.channel = channel
        self._running = set()
        self._running_lock = threading.Lock()
        self._worker = None
        self._worker_lock = threading.Lock()
        self._stop_event = threading.Event()

    def create(self, path, msg, author, chat, *, placeholder_uid=None):
        source = (
            extract_sticker_url(msg)
            if msg.get("type") == "animatedsticker"
            else path
        )
        if not source and msg.get("type") != "animatedsticker":
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
            AUTO_RETRY_STATE_KEY: {
                "attempts": 0,
                "next_at": time.time() + AUTO_RETRY_DELAYS[0],
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

    def retry(self, retry_id, *, automatic=False):
        if not isinstance(retry_id, str):
            return "重试上下文已失效，请重新接收媒体"
        with self._running_lock:
            if retry_id in self._running:
                duplicate = True
            else:
                duplicate = False
                self._running.add(retry_id)
        if duplicate:
            if automatic:
                return None
            media = self.channel.db.get_media_retry(retry_id)
            if isinstance(media, dict):
                return self._temporary_failure(
                    retry_id,
                    media,
                    automatic=False,
                )
            return "重试上下文已失效，请重新接收媒体"
        try:
            return self._retry(retry_id, automatic=automatic)
        finally:
            with self._running_lock:
                self._running.discard(retry_id)

    def _retry(self, retry_id, *, automatic=False):
        media = self.channel.db.get_media_retry(retry_id)
        if not isinstance(media, dict):
            return "重试上下文已失效，请重新接收媒体"

        source = media.get("source")
        media_type = media.get("type")
        placeholder_uid = media.get("placeholder_uid")
        if (
            media_type not in MEDIA_RETRY_TYPES
            or not placeholder_uid
            or (media_type != "animatedsticker" and not source)
        ):
            return "不支持重试此媒体"

        try:
            msg = dict(media.get("msg") or {})
            conversion_type = media_type
            if media_type == "animatedsticker" or (
                media_type == "share" and is_sticker_share(msg)
            ):
                media_path = self.channel.sticker_cache.get_or_download(
                    dict(msg),
                    wait=MEDIA_WAIT_SECONDS,
                )
                conversion_type = "animatedsticker"
            else:
                media_path = self._existing_media_path(source, media_type)
                if media_path is None:
                    msgid = msg.get("msgid") or msg.get("svrid")
                    if msgid is None:
                        return self._temporary_failure(
                            retry_id,
                            media,
                            automatic=automatic,
                        )
                    restored_path = self.channel.GetMsgCdn(msgid)
                    media_path = self._wait_for_media(restored_path, media_type)
                    if media_path is None:
                        return self._temporary_failure(
                            retry_id,
                            media,
                            automatic=automatic,
                        )

            msg["type"] = conversion_type
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
        except StickerPermanentError:
            self.channel.logger.info(
                "Animated sticker retry is permanently unavailable: token=%s",
                retry_id,
            )
            return self._permanent_sticker_failure(retry_id, media)
        except StickerTemporaryError:
            self.channel.logger.warning(
                "Animated sticker retry remains unavailable: token=%s",
                retry_id,
                exc_info=True,
            )
            return self._temporary_failure(
                retry_id,
                media,
                automatic=automatic,
            )
        except Exception:
            self.channel.logger.exception(
                "Failed to retry media: type=%s token=%s",
                media_type,
                retry_id,
            )
            return self._temporary_failure(
                retry_id,
                media,
                automatic=automatic,
            )

        if (
            self.channel.delete_media_after_send
            and media_type in MEDIA_DELETE_TYPES
            and conversion_type != "animatedsticker"
        ):
            self.delete_files(source, media_path)
        self.channel.db.delete_media_retry(retry_id)
        return "媒体重试发送成功"

    def _temporary_failure(self, retry_id, media, *, automatic=False):
        media_name = MEDIA_TYPE_NAMES.get(media.get("type"), "媒体")
        text = f"{media_name}重新下载失败，请稍后再试"
        self._reschedule(retry_id, media, automatic=automatic)
        try:
            self._edit_failure(media, text, command=self.command(retry_id))
        except Exception:
            self.channel.logger.exception(
                "Failed to restore retry command: token=%s",
                retry_id,
            )
            return f"{media_name}重试失败，请稍后再试"
        return None

    def _reschedule(self, retry_id, media, *, automatic):
        payload = dict(media)
        state = self._auto_retry_state(payload)
        attempts = state["attempts"] if state is not None else 0
        if automatic:
            attempts += 1
        next_at = (
            time.time() + AUTO_RETRY_DELAYS[attempts]
            if attempts < len(AUTO_RETRY_DELAYS)
            else None
        )
        payload[AUTO_RETRY_STATE_KEY] = {
            "attempts": attempts,
            "next_at": next_at,
        }
        self.channel.db.update_media_retry(retry_id, payload)

    @staticmethod
    def _auto_retry_state(media):
        state = media.get(AUTO_RETRY_STATE_KEY)
        if not isinstance(state, dict):
            return None
        attempts = state.get("attempts")
        next_at = state.get("next_at")
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or attempts < 0
            or (
                next_at is not None
                and (
                    isinstance(next_at, bool)
                    or not isinstance(next_at, (int, float))
                )
            )
        ):
            return None
        return {"attempts": attempts, "next_at": next_at}

    @staticmethod
    def _requires_native(media):
        media_type = media.get("type")
        if media_type == "animatedsticker":
            return False
        msg = media.get("msg") or {}
        return not (media_type == "share" and is_sticker_share(msg))

    def _is_logged_in(self):
        is_login = getattr(self.channel, "is_login", None)
        if not callable(is_login):
            return True
        try:
            return bool(is_login())
        except Exception:
            self.channel.logger.warning(
                "Failed to check login state before automatic media retry",
                exc_info=True,
            )
            return False

    def run_due_once(self, *, now=None):
        current_time = time.time() if now is None else now
        for retry_id, media in self.channel.db.list_media_retries():
            state = self._auto_retry_state(media)
            if state is None:
                payload = dict(media)
                payload[AUTO_RETRY_STATE_KEY] = {
                    "attempts": 0,
                    "next_at": current_time + AUTO_RETRY_DELAYS[0],
                }
                self.channel.db.update_media_retry(retry_id, payload)
                continue
            if (
                state["attempts"] >= len(AUTO_RETRY_DELAYS)
                or state["next_at"] is None
                or state["next_at"] > current_time
            ):
                continue
            if self._requires_native(media) and not self._is_logged_in():
                continue
            self.retry(retry_id, automatic=True)
            return True
        return False

    def start(self):
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event = threading.Event()
            self._worker = threading.Thread(
                target=self._run_worker,
                name="comwechat-media-retry",
                daemon=True,
            )
            self._worker.start()

    def stop(self):
        with self._worker_lock:
            worker = self._worker
            if worker is None:
                return
            self._stop_event.set()
        if worker is not threading.current_thread():
            worker.join()
        with self._worker_lock:
            if self._worker is worker:
                self._worker = None

    def _run_worker(self):
        while not self._stop_event.is_set():
            try:
                self.run_due_once()
            except Exception:
                self.channel.logger.exception(
                    "Automatic media retry worker failed"
                )
            self._stop_event.wait(AUTO_RETRY_IDLE_SECONDS)

    def _permanent_sticker_failure(self, retry_id, media):
        text = "动态表情下载链接已失效，无法重试，请在手机端查看。"
        try:
            self._edit_failure(media, text)
        except Exception:
            self.channel.logger.exception(
                "Failed to edit permanently unavailable sticker placeholder: token=%s",
                retry_id,
            )
        finally:
            self.channel.db.delete_media_retry(retry_id)
        return text

    def _edit_failure(self, media, text, command=None):
        chat, author = self._build_context(media)
        failed_msg = dict(media.get("msg") or {})
        failed_msg["type"] = "text"
        failed_msg["message"] = text
        messages = self._as_list(
            MsgProcess(failed_msg, chat, self.channel.direct_transfer)
        )
        commands = MessageCommands([command]) if command is not None else None
        for message in messages:
            message.commands = commands
        self.channel.send_efb_msgs(
            messages,
            author=author,
            chat=chat,
            uid=MessageID(media["placeholder_uid"]),
            edit=True,
        )

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
