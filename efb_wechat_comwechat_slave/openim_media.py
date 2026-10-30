import os
import re
import subprocess
from typing import Any, Dict


OPENIM_MEDIA_TYPES = frozenset({"voice", "animatedsticker"})
_OPENIM_FROM_USERNAME_RE = re.compile(
    r"""\bfromusername\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


class OpenIMMediaError(RuntimeError):
    pass


def _is_openim_id(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().endswith("@openim")


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
    if str(response.get("msg")) != "1" or response.get("result") != "OK":
        detail = response.get("err_msg") or response.get("result") or response
        raise OpenIMMediaError(
            "{} download failed: {}".format(media_type, detail)
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
