import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, Optional


OPENIM_MEDIA_TYPES = frozenset({"voice", "animatedsticker"})
OPENIM_CHAT_SUFFIXES = ("@openim", "@im.chatroom")
_OPENIM_FROM_USERNAME_RE = re.compile(
    r"""\bfromusername\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_OPENIM_DB_LOCK = threading.Lock()


class OpenIMMediaError(RuntimeError):
    pass


class OpenIMReferenceError(RuntimeError):
    pass


def is_openim_chat_id(value: Any) -> bool:
    """Return whether a chat ID is an enterprise WeCom conversation."""
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return any(normalized.endswith(suffix) for suffix in OPENIM_CHAT_SUFFIXES)


def _is_openim_id(value: Any) -> bool:
    return is_openim_chat_id(value)


def is_openim_message(msg: Dict[str, Any]) -> bool:
    """Return whether a hook message belongs to an enterprise WeCom contact."""
    if _is_openim_id(msg.get("sender")) or _is_openim_id(msg.get("wxid")):
        return True
    match = _OPENIM_FROM_USERNAME_RE.search(str(msg.get("message") or ""))
    return bool(match and _is_openim_id(match.group(1)))


def is_openim_media_message(msg: Dict[str, Any]) -> bool:
    return (
        msg.get("type") in OPENIM_MEDIA_TYPES
        and is_openim_message(msg)
    )


def _sql_string(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _reset_cached_db_handles(bot: Any) -> None:
    api = getattr(bot, "api", None)
    if api is not None:
        try:
            api.db_handle = 0
        except Exception:
            pass


def _query_openim_rows(bot: Any, sql: str):
    last_error = None
    for attempt in range(2):
        try:
            with _OPENIM_DB_LOCK:
                handle = bot.GetDBHandle("OpenIMMsg.db")
                response = bot.QueryDatabase(db_handle=handle, sql=sql)
            if isinstance(response, dict) and response.get("result") == "OK":
                data = response.get("data")
                if not isinstance(data, list) or not data:
                    return [], []
                if not isinstance(data[0], list):
                    raise OpenIMReferenceError(
                        "OpenIMMsg query returned an invalid header"
                    )
                return data[0], data[1:]

            detail = response
            if isinstance(response, dict):
                detail = response.get("err_msg") or response
            last_error = OpenIMReferenceError(
                "OpenIMMsg query failed: {}".format(detail)
            )
            if attempt == 0 and "database handle unavailable" in str(detail):
                _reset_cached_db_handles(bot)
                continue
            raise last_error
        except OpenIMReferenceError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                _reset_cached_db_handles(bot)
                continue
            raise OpenIMReferenceError(
                "OpenIMMsg query failed: {}".format(exc)
            ) from exc
    raise OpenIMReferenceError(
        "OpenIMMsg query failed: {}".format(last_error)
    )


def _query_records(bot: Any, sql: str):
    header, rows = _query_openim_rows(bot, sql)
    records = []
    for row in rows:
        if isinstance(row, list) and len(row) == len(header):
            records.append(dict(zip(header, row)))
    return records


def openim_local_cursor(bot: Any, talker: str) -> int:
    """Return the newest localId currently stored for one OpenIM chat."""
    records = _query_records(
        bot,
        "select coalesce(max(localId),0) as max_local_id from ChatCRMsg "
        "where strTalker={};".format(_sql_string(talker)),
    )
    if not records:
        return 0
    try:
        return max(0, int(records[0].get("max_local_id") or 0))
    except (TypeError, ValueError) as exc:
        raise OpenIMReferenceError(
            "OpenIMMsg returned an invalid localId cursor"
        ) from exc


def find_openim_sent_reference(
    bot: Any,
    talker: str,
    after_local_id: int,
    *,
    expected_type: Optional[int] = None,
    expected_content: Optional[str] = None,
) -> Optional[str]:
    """Find the MsgSvrID of an outgoing OpenIM row newer than the cursor."""
    try:
        cursor = max(0, int(after_local_id))
    except (TypeError, ValueError) as exc:
        raise OpenIMReferenceError("invalid OpenIM localId cursor") from exc

    records = _query_records(
        bot,
        "select localId,MsgSvrID,type,strContent,IsSender from ChatCRMsg "
        "where strTalker={} and IsSender=1 and localId>{} "
        "order by localId asc;".format(_sql_string(talker), cursor),
    )
    for record in records:
        if str(record.get("IsSender", "1")) != "1":
            continue
        if expected_type is not None and str(record.get("type")) != str(expected_type):
            continue
        if (
            expected_content is not None
            and str(record.get("strContent") or "") != expected_content
        ):
            continue
        msgid = str(record.get("MsgSvrID") or "").strip()
        if msgid.isdecimal() and int(msgid) > 0:
            return msgid
    return None


def wait_for_openim_sent_reference(
    bot: Any,
    talker: str,
    after_local_id: int,
    *,
    timeout: float = 5.0,
    interval: float = 0.2,
    expected_type: Optional[int] = None,
    expected_content: Optional[str] = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> Optional[str]:
    """Poll ChatCRMsg until the outgoing row for this send is visible."""
    deadline = monotonic() + max(0.0, float(timeout))
    interval = max(0.02, float(interval))
    while True:
        reference = find_openim_sent_reference(
            bot,
            talker,
            after_local_id,
            expected_type=expected_type,
            expected_content=expected_content,
        )
        if reference is not None:
            return reference
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        sleep(min(interval, remaining))


def windows_to_wsl_path(path: str, is_wsl: bool) -> str:
    """Map a path returned by the Windows hook to the WSL view of that file."""
    if not is_wsl or not isinstance(path, str) or not path:
        return path
    if path.startswith("/"):
        return path

    try:
        result = subprocess.run(
            ["wslpath", "-u", path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    match = _WINDOWS_DRIVE_PATH_RE.match(path)
    if not match:
        return path
    drive, remainder = match.groups()
    remainder = remainder.replace("\\", "/")
    return "/mnt/{}/{}".format(drive.lower(), remainder)


def _response_path(response: Any, media_type: str) -> str:
    if not isinstance(response, dict):
        raise OpenIMMediaError(
            "{} download returned an invalid response".format(media_type)
        )
    if str(response.get("msg")) != "1":
        detail = response.get("err_msg") or response
        raise OpenIMMediaError(
            "{} download failed: {}".format(media_type, detail)
        )
    if response.get("result") != "OK":
        raise OpenIMMediaError(
            "{} download failed: {}".format(media_type, response)
        )
    path = response.get("path")
    if not isinstance(path, str) or not path.strip():
        raise OpenIMMediaError(
            "{} download returned no path".format(media_type)
        )
    return path


def download_openim_media(
    bot: Any,
    msg: Dict[str, Any],
    *,
    is_wsl: bool = False,
) -> str:
    """Download one enterprise voice or animated sticker through type 53/54."""
    media_type = msg.get("type")
    if media_type not in OPENIM_MEDIA_TYPES:
        raise OpenIMMediaError(
            "unsupported OpenIM media type: {}".format(media_type)
        )

    msgid = msg.get("msgid")
    if msgid is None or str(msgid).strip() == "":
        raise OpenIMMediaError("{} message has no msgid".format(media_type))
    msgid = str(msgid)

    if media_type == "voice":
        response = bot.GetVoiceMsg(msgid=msgid)
    else:
        content = msg.get("message")
        if not isinstance(content, str) or not content.strip():
            raise OpenIMMediaError("animated sticker XML is missing")
        response = bot.GetEmojiMsg(msgid=msgid, content=content)

    path = _response_path(response, media_type)
    local_path = windows_to_wsl_path(path, is_wsl)
    if not os.path.isfile(local_path):
        raise OpenIMMediaError(
            "{} download path is unavailable: {}".format(media_type, local_path)
        )
    return local_path
