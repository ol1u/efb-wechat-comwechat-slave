import logging
import os
import tempfile
import html
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Tuple, List, Union, IO
from urllib.parse import urlparse
import magic
from lxml import etree
from functools import partial
from traceback import print_exc
import re , json

from ehforwarderbot import MsgType, Chat, coordinator
from ehforwarderbot.chat import ChatMember
from ehforwarderbot.message import Substitutions, Message, LinkAttribute, LocationAttribute
from ehforwarderbot.types import MessageID

from .ChatMgr import ChatMgr
from .CustomTypes import EFBGroupChat, EFBPrivateChat
from .Utils import compress_image_if_large, load_local_file_to_temp
from .chat_history import (
    ChatHistoryParseError,
    count_history_items,
    item_meta,
    item_text,
    parse_forwarded_message,
)
from .forwarded_media_cache import (
    ForwardedMediaResolution,
    ForwardedMediaUnavailable,
    forwarded_media_extension,
    forwarded_media_xor_key,
)

QUOTE_DIVIDER = " - - - - - - - - - - - - - - - "

def qutoed_text(qutoed_text: str, text: str, prefix: str = "") -> str:
    if QUOTE_DIVIDER in qutoed_text:
        qutoed_text = qutoed_text.split(QUOTE_DIVIDER)[-1]
    return f"「{prefix}{qutoed_text}」\n{QUOTE_DIVIDER}\n{text}"

def parse_chat_history(xml, level: int = 1) -> list[dict]:
    res = []
    datalist_element = xml.find('.//datalist')
    if datalist_element is not None:
        for dataitem in datalist_element.findall('dataitem'):
            #TODO 想办法下载图片文件等
            data = {
                'datatype': dataitem.get('datatype'),
                # 'dataid': dataitem.get('dataid'),
                # 'messageuuid': dataitem.find('.//messageuuid').text if dataitem.find('.//messageuuid') is not None else '',
                # 'cdnthumburl': dataitem.find('.//cdnthumburl').text if dataitem.find('.//cdnthumburl') is not None else '',
                'datatitle': dataitem.find('.//datatitle').text if dataitem.find(
                    './/datatitle') is not None else '',
                'sourcetime': dataitem.find('.//sourcetime').text if dataitem.find(
                    './/sourcetime') is not None else '',
                # 'fromnewmsgid': dataitem.find('.//fromnewmsgid').text if dataitem.find('.//fromnewmsgid') is not None else '',
                # 'datasize': dataitem.find('.//datasize').text if dataitem.find('.//datasize') is not None else '',
                # 'thumbfullmd5': dataitem.find('.//thumbfullmd5').text if dataitem.find('.//thumbfullmd5') is not None else '',
                'datafmt': dataitem.find('.//datafmt').text if dataitem.find(
                    './/datafmt') is not None else '',
                # 'cdnthumbkey': dataitem.find('.//cdnthumbkey').text if dataitem.find('.//cdnthumbkey') is not None else '',
                'sourcename': dataitem.find('.//sourcename').text if dataitem.find(
                    './/sourcename') is not None else '',
                'sourceheadurl': dataitem.find('.//sourceheadurl').text if dataitem.find(
                    './/sourceheadurl') is not None else '',
                'datadesc': dataitem.find('.//datadesc').text if dataitem.find(
                    './/datadesc') is not None else '',
                'children': [],
            }

            prefix = f"{data['sourcename']}: "
            count = 8 * level
            if data['datatype'] == '1':
                data['placeholder'] = data['datadesc']
            elif data['datatype'] == '2':
                data['placeholder'] = '[Photo]'
            elif data['datatype'] == '4':
                data['placeholder'] = '[Video]'
            elif data['datatype'] == '5':
                data['placeholder'] = f"[Link] {data['datatitle']}"
            elif data['datatype'] == '8':
                data['placeholder'] = f"[File] {data['datatitle']}"
            elif data['datatype'] == '17':
                data['placeholder'] = f"\n{' ' * count}[Chat History]"
                for i in parse_chat_history(dataitem.find('recordxml/recordinfo'), level + 1):
                    data['placeholder'] += f"\n{' ' * count}{i['formatted']}"
                    data['children'] = i
                data['placeholder'] += f"\n{' ' * count}[Chat History]"
            elif data['datatype'] == '19':
                data['placeholder'] = f"[Mini Program] {data['datatitle']}"
            else:
                data['placeholder'] = data['datadesc'] or data['datatitle']

            data['formatted'] = f"{prefix} {data['placeholder']}"

            res.append(data)
    return res

def efb_text_simple_wrapper(text: str, ats: Union[Mapping[Tuple[int, int], Union[Chat, ChatMember]], None] = None) -> Message:
    """
    A simple EFB message wrapper for plain text. Emojis are presented as is (plain text).
    :param text: The content of the message
    :param ats: The substitutions of at messages, must follow the Substitution format when not None
                [[begin_index, end_index], {Chat or ChatMember}]
    :return: EFB Message
    """
    efb_msg = Message(
        type=MsgType.Text,
        text=text
    )
    if ats:
        efb_msg.substitutions = Substitutions(ats)
    return efb_msg

def efb_image_wrapper(
    file: IO,
    filename: str = None,
    text: str = None,
    force_animation: bool = False,
) -> Union[Message, List[Message]]:
    """
    A EFB message wrapper for images.
    :param file: The file handle
    :param filename: The actual filename
    :param text: The attached text
    :param force_animation: Send a video-backed sticker as an animation
    :return: EFB Message or list of EFB Messages
    """
    efb_msg = Message()
    efb_msg.file = file
    mime = magic.from_file(file.name, mime=True)
    if isinstance(mime, bytes):
        mime = mime.decode()

    if force_animation or "gif" in mime:
        efb_msg.type = MsgType.Animation
    else:
        efb_msg.type = MsgType.Image

    if filename:
        efb_msg.filename = filename
    else:
        efb_msg.filename = file.name
        efb_msg.filename += '.' + str(mime).split('/')[1]

    if text:
        efb_msg.text = text

    efb_msg.path = efb_msg.file.name
    efb_msg.mime = mime
    return efb_msg

def efb_video_wrapper(file: IO, filename: str = None, text: str = None) -> Message:
    """
    A EFB message wrapper for voices.
    :param file: The file handle
    :param filename: The actual filename
    :param text: The attached text
    :return: EFB Message
    """
    efb_msg = Message()
    efb_msg.type = MsgType.Video
    efb_msg.file = file
    mime = magic.from_file(file.name, mime=True)
    if isinstance(mime, bytes):
        mime = mime.decode()
    if filename:
        efb_msg.filename = filename
    else:
        efb_msg.filename = file.name
        efb_msg.filename += '.' + str(mime).split('/')[1]  # Add extension suffix
    efb_msg.path = efb_msg.file.name
    efb_msg.mime = mime
    if text:
        efb_msg.text = text
    return efb_msg

def efb_file_wrapper(file: IO, filename: str = None, text: str = None) -> Message:
    """
    A EFB message wrapper for voices.
    :param file: The file handle
    :param filename: The actual filename
    :param text: The attached text
    :return: EFB Message
    """
    efb_msg = Message()
    efb_msg.file = file
    mime = magic.from_file(file.name, mime=True)
    if isinstance(mime, bytes):
        mime = mime.decode()
    # 与 efb_image_wrapper 一致:按内容类型决定 TG 里的展示形式,
    # 让当文件发的图片/动图也能内联显示,而不是文件卡片。
    if "gif" in mime:
        efb_msg.type = MsgType.Animation
    elif mime.startswith("image/"):
        efb_msg.type = MsgType.Image
    else:
        efb_msg.type = MsgType.File
    if filename:
        efb_msg.filename = filename
    else:
        efb_msg.filename = file.name
        efb_msg.filename += '.' + str(mime).split('/')[1]  # Add extension suffix
    efb_msg.path = efb_msg.file.name
    efb_msg.mime = mime
    if text:
        efb_msg.text = text
    return efb_msg

def efb_mp_post_wrapper(item: etree.Element, show_name: str = None) -> Message:
    url = cover = None
    title = digest = result_text = ''
    try:
        title = item.find('title').text
        url = item.find('url').text
        digest = item.find('digest').text
        cover = item.find('cover').text
    except Exception:
        print_exc()

    digest += f'\n- - - - from {show_name}' if show_name else ''
    if title and url:
        attribute = LinkAttribute(
            title=title,
            description=digest,
            url=url,
            image=cover,
        )
        return Message(
            attributes=attribute,
            type=MsgType.Link,
            text=result_text,
            vendor_specific={'is_mp': True},
        )
    # 部分公众号通知没有url信息
    return Message(
        type=MsgType.Text,
        text=f'{title}\n  - - - - - - - - - - - - - - - \n{digest}' if digest else str(title),
    )

def _open_forwarded_media(
    resolved: Any,
    fallback_name: str,
) -> Tuple[IO, str, Optional[ForwardedMediaResolution]]:
    """Normalize a resolver result to an open file and display metadata."""
    resolution = None
    filename = fallback_name
    source = resolved
    if isinstance(resolved, ForwardedMediaResolution):
        resolution = resolved
        source = resolved.source
        if resolved.filename:
            filename = str(resolved.filename)
    elif isinstance(resolved, tuple):
        source = resolved[0]
        if len(resolved) > 1 and resolved[1]:
            filename = str(resolved[1])
            resolution = ForwardedMediaResolution(
                source=source,
                filename=filename,
            )
    if hasattr(source, "read") and hasattr(source, "name"):
        try:
            source.seek(0)
        except (AttributeError, OSError):
            pass
        file = source
    else:
        path = os.fspath(source)
        if not filename or filename == fallback_name:
            if resolution is None or not resolution.filename:
                filename = os.path.basename(path) or fallback_name
        file = load_local_file_to_temp(path)
    file, filename = _decode_forwarded_media(
        file,
        filename,
        fallback_name=fallback_name,
    )
    return file, filename, resolution


def _decode_forwarded_media(
    file: IO,
    filename: str,
    *,
    fallback_name: str = "",
) -> Tuple[IO, str]:
    """Remove WeChat's single-byte XOR wrapper when a media header is known."""
    try:
        file.seek(0)
        header = file.read(32)
        file.seek(0)
    except (AttributeError, OSError, ValueError):
        return file, filename

    xor_key = forwarded_media_xor_key(header)
    media_extension = forwarded_media_extension(header, xor_key)
    if xor_key is not None:
        output = tempfile.NamedTemporaryFile(
            prefix="efb-forwarded-decoded-",
            suffix=".{}".format(media_extension or "bin"),
        )
        translation = bytes(value ^ xor_key for value in range(256))
        try:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk.translate(translation))
            output.seek(0)
        except Exception:
            output.close()
            raise
        try:
            file.close()
        except Exception:
            pass
        file = output

    current_extension = os.path.splitext(filename)[1].lower()
    if media_extension and current_extension in ("", ".dat", ".bin"):
        fallback_extension = os.path.splitext(fallback_name)[1].lower()
        name_source = (
            fallback_name
            if fallback_name and fallback_extension not in ("", ".dat", ".bin")
            else filename
        )
        stem = os.path.splitext(os.path.basename(name_source))[0]
        filename = "{}.{}".format(stem or "forwarded-media", media_extension)
    return file, filename

def _forwarded_media_filename(item: dict, media_type: str) -> str:
    extension = (item.get("datafmt") or "").strip().lstrip(".").lower()
    if extension == "pic":
        extension = "jpg"
    if not extension:
        extension = {
            "image": "jpg",
            "video": "mp4",
            "file": "bin",
        }.get(media_type, "bin")
    stem = item.get("dataid") or "forwarded-{}".format(item.get("datatype", "item"))
    return "{}.{}".format(stem, extension)

def _build_forwarded_item_messages(
    item: dict,
    media_resolver: Optional[Callable[[dict], Any]],
    parent_header: Optional[Message] = None,
) -> List[Message]:
    datatype = item.get("datatype")
    messages = []

    if datatype == "17":
        nested_header = Message(
            type=MsgType.Text,
            text=item_text(item),
            vendor_specific={"is_forwarded": True},
        )
        if parent_header is not None:
            nested_header.target = parent_header
        messages.append(nested_header)
        for child in item.get("children") or []:
            messages.extend(_build_forwarded_item_messages(
                child,
                media_resolver,
                parent_header=nested_header,
            ))
        return messages

    has_emoji = datatype == "1" and bool(item.get("emoji_urls"))
    is_image = datatype == "2" or has_emoji
    is_video = datatype == "4"
    is_file = datatype == "8"
    if not (is_image or is_video or is_file):
        return [Message(
            type=MsgType.Text,
            text=item_text(item),
            vendor_specific={"is_forwarded": True},
        )]

    media_type = "image" if is_image else "video" if is_video else "file"
    if media_resolver is None:
        return [Message(
            type=MsgType.Text,
            text=item_text(item) + "\n[{}下载失败：当前通道不支持媒体解析]".format(
                item.get("type_name") or media_type
            ),
            vendor_specific={"is_forwarded": True},
        )]

    try:
        resolved = media_resolver(item)
        fallback_name = _forwarded_media_filename(item, media_type)
        file, filename, resolution = _open_forwarded_media(
            resolved,
            fallback_name,
        )
        resolved_type = (
            resolution.media_type
            if resolution is not None and resolution.media_type
            else media_type
        )
        if resolution is not None and resolution.is_thumbnail:
            resolved_type = "image"
        caption = item_meta(item)
        if item.get("datatitle") and item["datatitle"] != item.get("type_name"):
            caption += "\n{}".format(item["datatitle"])
        if resolution is not None and resolution.note:
            caption += "\n[{}]".format(resolution.note)
        if resolved_type == "image":
            file = compress_image_if_large(file)
            efb_msg = efb_image_wrapper(file, filename=filename, text=caption)
        elif resolved_type == "video":
            efb_msg = efb_video_wrapper(file, filename=filename, text=caption)
        else:
            efb_msg = efb_file_wrapper(file, filename=filename, text=caption)
        vendor_specific = dict(getattr(efb_msg, "vendor_specific", None) or {})
        vendor_specific["is_forwarded"] = True
        efb_msg.vendor_specific = vendor_specific
        return [efb_msg]
    except ForwardedMediaUnavailable as exc:
        reason = str(exc).strip() or "未找到可用媒体"
        logging.getLogger("comwechat").warning(
            "合并转发媒体不可用: display_index=%s datatype=%s reason=%s",
            item.get("display_index"),
            datatype,
            reason,
        )
        return [Message(
            type=MsgType.Text,
            text=item_text(item) + "\n[{}下载失败：{}，请在微信查看原文]".format(
                item.get("type_name") or media_type,
                reason,
            ),
            vendor_specific={"is_forwarded": True},
        )]
    except Exception as exc:
        logging.getLogger("comwechat").warning(
            "合并转发媒体处理失败: display_index=%s datatype=%s exception=%s",
            item.get("display_index"),
            datatype,
            type(exc).__name__,
        )
        return [Message(
            type=MsgType.Text,
            text=item_text(item) + "\n[{}下载失败：媒体读取失败，请在微信查看原文]".format(
                item.get("type_name") or media_type,
            ),
            vendor_specific={"is_forwarded": True},
        )]

def efb_forwarded_chat_wrapper(
    text: str,
    media_resolver: Optional[Callable[[dict], Any]] = None,
) -> List[Message]:
    parsed = parse_forwarded_message(text)
    items = parsed["items"]
    item_count = parsed.get("item_count")
    if item_count is None:
        item_count = count_history_items(items)
    header_lines = [
        parsed["title"],
        "顶层 {} 条 · 含嵌套共 {} 条".format(len(items), item_count),
    ]
    global_header = Message(
        type=MsgType.Text,
        text="\n".join(header_lines),
        vendor_specific={"is_forwarded": True},
    )
    messages = [global_header]
    for item in items:
        messages.extend(_build_forwarded_item_messages(
            item,
            media_resolver,
            parent_header=global_header,
        ))
    return messages


_GENERIC_SHARE_TYPES = {1, 2, 3, 4, 33, 36, 51, 63}
_SHARE_TYPE_NAMES = {
    1: "网页/应用分享",
    2: "其他分享",
    3: "音乐分享",
    4: "小红书等内容分享",
    5: "公众号文章",
    33: "小程序/服务分享",
    36: "应用活动分享",
    51: "视频号视频",
    63: "视频号直播",
}
_SHARE_UNKNOWN_TYPE_NAME = "其他分享"
_SHARE_LINK_LABEL = "打开原链接"
_SHARE_THUMBNAIL_FIELDS = (
    "thumburl",
    "thumb_url",
    "cdnthumburl",
    "cdnmidimgurl",
    "cdnmidimageurl",
    "cdnbigimageurl",
    "cover",
    "coverurl",
    "headurl",
    "avatar",
    "weappiconurl",
)
_SHARE_CAPTION_LIMIT = 950


@dataclass(frozen=True)
class ShareCard:
    appmsg_type: Optional[int]
    title: str = ""
    description: str = ""
    url: Optional[str] = None
    source: str = ""
    author: str = ""
    thumbnail_urls: Tuple[str, ...] = ()
    is_mp: bool = False


def _share_clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(value).replace("\x00", "").strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3].strip()
    return value


def _share_first_text(xml: etree._Element, paths: Tuple[str, ...]) -> str:
    for path in paths:
        values = xml.xpath(path)
        if not values:
            continue
        value = values[0]
        cleaned = _share_clean_text(
            value if isinstance(value, str) else getattr(value, "text", "")
        )
        if cleaned:
            return cleaned
    return ""


def _share_http_url(value: Any) -> Optional[str]:
    cleaned = _share_clean_text(value)
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return cleaned


def _share_thumbnail_urls(xml: etree._Element) -> Tuple[str, ...]:
    candidates: List[str] = []
    for field in _SHARE_THUMBNAIL_FIELDS:
        for element in xml.iter():
            if not isinstance(element.tag, str):
                continue
            if etree.QName(element).localname.lower() != field:
                continue
            url = _share_http_url(element.text)
            if url and url not in candidates:
                candidates.append(url)
    return tuple(candidates)


def is_supported_share_thumbnail(file: IO) -> bool:
    try:
        mime = magic.from_file(file.name, mime=True)
        if isinstance(mime, bytes):
            mime = mime.decode(errors="replace")
        normalized_mime = str(mime).lower()
        return normalized_mime.startswith("image/") and "svg" not in normalized_mime
    except Exception:
        return False


def _share_card_from_xml(
    xml: etree._Element,
    appmsg_type: Optional[int],
) -> ShareCard:
    title = _share_first_text(
        xml,
        (
            "/msg/appmsg/title/text()",
            "/msg/appmsg/finderFeed/title/text()",
            "/msg/appmsg/finderLive/title/text()",
        ),
    )
    description = _share_first_text(
        xml,
        (
            "/msg/appmsg/des/text()",
            "/msg/appmsg/finderFeed/desc/text()",
            "/msg/appmsg/finderLive/desc/text()",
        ),
    )
    source = _share_first_text(
        xml,
        (
            "/msg/appinfo/appname/text()",
            "/msg/appmsg/sourcedisplayname/text()",
            "/msg/appmsg/sourceusername/text()",
        ),
    )
    author = _share_first_text(xml, ("/msg/appmsg/finderFeed/nickname/text()",))
    url = _share_http_url(_share_first_text(xml, ("/msg/appmsg/url/text()",)))
    thumbnail_urls = _share_thumbnail_urls(xml)
    is_mp = appmsg_type in {5, 33, 51, 63} or bool(
        _share_first_text(xml, ("/msg/appmsg/sourceusername/text()",))
    )
    return ShareCard(
        appmsg_type=appmsg_type,
        title=title,
        description=description,
        url=url,
        source=source,
        author=author,
        thumbnail_urls=thumbnail_urls,
        is_mp=is_mp,
    )


def _share_single_line(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) > limit:
        return value[: max(0, limit - 3)].rstrip() + "..."
    return value


def _share_summary(
    card: ShareCard,
    has_thumbnail: Optional[bool] = None,
    *,
    include_link: bool = False,
) -> str:
    type_label = str(card.appmsg_type) if card.appmsg_type is not None else "未知"
    type_name = _SHARE_TYPE_NAMES.get(card.appmsg_type, _SHARE_UNKNOWN_TYPE_NAME)
    title = _share_single_line(card.title, 240)
    description = _share_single_line(card.description, 500)
    source = _share_single_line(card.source, 120)
    if has_thumbnail is None:
        has_thumbnail = bool(card.thumbnail_urls)
    lines = [f"微信分享消息（type={type_label} · {type_name}）"]
    if include_link and card.url:
        lines.append(f"🔗 {_SHARE_LINK_LABEL}")
    lines.extend([
        f"标题：{title or '无'}",
        f"描述：{description or '无'}",
        f"来源：{source or '无'}",
    ])
    if card.author:
        lines.append(f"作者：{_share_single_line(card.author, 80) or '无'}")
    lines.append(f"缩略图：{'有' if has_thumbnail else '无'}")
    if not title and not description and not card.url:
        lines.append("（无可用内容，请在微信查看原文）")
    return "\n".join(lines)


def _truncate_share_caption(summary: str) -> str:
    if len(summary) <= _SHARE_CAPTION_LIMIT:
        return summary
    return summary[: max(0, _SHARE_CAPTION_LIMIT - 3)].rstrip() + "..."


def _share_base_message(card: ShareCard, summary: str) -> Message:
    if card.url:
        attribute = LinkAttribute(
            title="打开原链接",
            description=None,
            url=card.url,
            image=card.thumbnail_urls[0] if card.thumbnail_urls else None,
        )
        return Message(
            attributes=attribute,
            type=MsgType.Link,
            text=summary,
            vendor_specific={"is_mp": card.is_mp},
        )
    return Message(
        type=MsgType.Text,
        text=summary,
        vendor_specific={"is_mp": card.is_mp},
    )


def _build_share_message(
    xml: etree._Element,
    message: dict,
    appmsg_type: Optional[int],
    thumbnail_resolver: Optional[Callable[[dict, ShareCard], Any]] = None,
) -> Union[Message, List[Message]]:
    card = _share_card_from_xml(xml, appmsg_type)
    thumbnail = None
    if thumbnail_resolver is not None:
        try:
            thumbnail = thumbnail_resolver(message, card)
        except Exception:
            logging.getLogger("comwechat").debug(
                "分享消息缩略图获取失败: type=%s", appmsg_type, exc_info=True
            )
        if thumbnail is not None and not is_supported_share_thumbnail(thumbnail):
            try:
                thumbnail.close()
            except Exception:
                pass
            thumbnail = None

    has_thumbnail = bool(card.thumbnail_urls) or thumbnail is not None
    include_link = thumbnail is not None and bool(card.url)
    summary = _share_summary(
        card,
        has_thumbnail=has_thumbnail,
        include_link=include_link,
    )
    base_message = _share_base_message(card, summary)
    if thumbnail is None:
        return base_message

    thumbnail = compress_image_if_large(thumbnail)
    image_message = efb_image_wrapper(thumbnail)
    if card.url:
        image_message.text = _truncate_share_caption(summary)
        image_message.vendor_specific = {
            "is_mp": card.is_mp,
            "html_link": {
                "text": _SHARE_LINK_LABEL,
                "url": card.url,
            },
        }
        return image_message

    image_message.vendor_specific = {"is_mp": card.is_mp}
    if len(summary) <= _SHARE_CAPTION_LIMIT:
        image_message.text = summary
        return image_message

    # Telegram captions are limited; keep the complete summary in its own
    # message and attach the thumbnail immediately after it.
    return [base_message, image_message]


def efb_share_link_wrapper(
    message: dict,
    chat,
    media_resolver: Optional[Callable[[dict], Any]] = None,
    thumbnail_resolver: Optional[Callable[[dict, ShareCard], Any]] = None,
) -> Union[Message, List[Message]]:
    """
    处理msgType49消息 - 复合xml, xml 中 //appmsg/type 指示具体消息类型.
    /msg/appmsg/type
    已知：
    //appmsg/type = 1 : 百度网盘分享
    //appmsg/type = 2 : 微信运动
    //appmsg/type = 3 : 音乐分享
    //appmsg/type = 4 : 小红书分享
    //appmsg/type = 5 : 链接（公众号文章）
    //appmsg/type = 6 : 文件 （收到文件的第二个提示【文件下载完成】)，也有可能 msgType = 10000 【【提示文件有风险】没有任何有用标识，无法判断是否与前面哪条消息有关联】
    //appmsg/type = 8 : 未解析图片消息
    //appmsg/type = 17 : 实时位置共享
    //appmsg/type = 19 : 合并转发的聊天记录
    //appmsg/type = 21 : 微信运动
    //appmsg/type = 24 : 从收藏中分享的笔记
    //appmsg/type = 33 : 美团外卖/腾讯微证券
    //appmsg/type = 35 : 消息同步
    //appmsg/type = 36 : 京东农场，滴滴打车
    //appmsg/type = 51 : 视频（微信视频号分享）
    //appmsg/type = 53 : 转账过期退还通知
    //appmsg/type = 57 : 引用(回复)消息，未细致研究哪个参数是被引用的消息 id
    //appmsg/type = 63 : 直播（微信视频号分享）
    //appmsg/type = 74 : 文件 (收到文件的第一个提示)
    //appmsg/type = 87 : 群公告
    //appmsg/type = 2000 : 转账
    :param message: The message
    :return: EFB Message
    """

    text: str = message['message']
    try:
        xml = etree.fromstring(text)
    except (etree.XMLSyntaxError, TypeError, ValueError):
        logging.getLogger("comwechat").warning(
            "分享消息 XML 解析失败，退回安全提示", exc_info=True
        )
        return efb_text_simple_wrapper("微信分享消息解析失败，请在微信查看原文")
    result_text = ""
    appmsg_type = None
    try:
        appmsg_type = int(xml.xpath('/msg/appmsg/type/text()')[0])
        type = appmsg_type
        if type in _GENERIC_SHARE_TYPES:
            return _build_share_message(
                xml,
                message,
                type,
                thumbnail_resolver=thumbnail_resolver,
            )
        elif type == 5: # xml链接
            if len(xml.xpath('/msg/appmsg/showtype/text()'))!=0:
                showtype = int(xml.xpath('/msg/appmsg/showtype/text()')[0])
            else:
                showtype = 0
            if showtype == 0: # 消息对话中的(测试的是从公众号转发给好友, 不排除其他情况)
                return _build_share_message(
                    xml,
                    message,
                    type,
                    thumbnail_resolver=thumbnail_resolver,
                )
            elif showtype == 1: # 公众号发的推送
                items = xml.xpath('//item')
                show_name = xml.xpath('//publisher/nickname/text()')[0] if '@app' in text else ''
                efb_msg = list(map(partial(efb_mp_post_wrapper, show_name=show_name), items))
        elif type == 8:
            efb_msg = Message(
                type=MsgType.Unsupported,
                text='未解密的微信表情消息，请在微信查看原文',
            )
        elif type == 17:
            msg_title = xml.xpath('/msg/appmsg/title/text()')[0]
            efb_msg = Message(
                type=MsgType.Text,
                text=msg_title,
            )
        elif type == 19: # 合并转发的聊天记录
            try:
                msg_title = xml.xpath('/msg/appmsg/title/text()')[0]
            except:
                msg_title = ""
            try:
                return efb_forwarded_chat_wrapper(
                    text,
                    media_resolver=media_resolver,
                )
            except ChatHistoryParseError:
                logging.getLogger("comwechat").warning(
                    "合并转发记录解析失败，退回微信摘要", exc_info=True
                )
                forward_content = xml.xpath('/msg/appmsg/des/text()')[0]
                result_text += f"{msg_title}\n\n{forward_content}"
                efb_msg = Message(
                    type=MsgType.Text,
                    text= result_text,
                    vendor_specific={ "is_forwarded": True }
                )
        elif type == 21: # 微信运动
            msg_title = xml.xpath('/msg/appmsg/title/text()')[0].strip("<![CDATA[夺得").strip("冠军]]>")
            if '排行' not in msg_title:
                msg_title = msg_title.strip()
                efb_msg = Message(
                    type=MsgType.Text,
                    text= msg_title ,
                )
            else:
                rank = xml.xpath('/msg/appmsg/hardwareinfo/messagenodeinfo/rankinfo/rank/rankdisplay/text()')[0].strip("<![CDATA[").strip("]]>")
                steps = xml.xpath('/msg/appmsg/hardwareinfo/messagenodeinfo/rankinfo/score/scoredisplay/text()')[0].strip("<![CDATA[").strip("]]>")
                result_text += f"{msg_title}\n\n排名: {rank}\n步数: {steps}"
                efb_msg = Message(
                    type=MsgType.Text,
                    text=result_text,
                    vendor_specific={ "is_wechatsport": True }
                )
        elif type == 24:
            desc = xml.xpath('/msg/appmsg/des/text()')[0]
            recorditem = xml.xpath('/msg/appmsg/recorditem/text()')[0]
            xml = etree.fromstring(recorditem)
            datadesc = xml.xpath('/recordinfo/datalist/dataitem/datadesc/text()')[0]
            efb_msg = Message(
                type=MsgType.Text,
                text= '微信笔记 :\n  - - - - - - - - - - - - - - - \n' +desc + '\n' + datadesc,
                vendor_specific={ "is_mp": True }
            )
        elif type == 35:
            efb_msg = Message(
                type=MsgType.Text,
                text= '系统消息 : 消息同步',
                vendor_specific={ "is_mp": False }
            )
        elif type == 40: # 转发的转发消息
            title = xml.xpath('/msg/appmsg/title/text()')[0]
            desc = xml.xpath('/msg/appmsg/des/text()')[0]
            efb_msg = Message(
                type=MsgType.Text,
                text= f"{title}\n\n{desc}" ,
                vendor_specific={ "is_forwarded": True }
            )
        elif type == 57: # 引用（回复）消息
            msg = xml.xpath('/msg/appmsg/title/text()')[0]
            refer_msgType = int(xml.xpath('/msg/appmsg/refermsg/type/text()')[0]) # 被引用消息类型
            e = xml.xpath('/msg/appmsg/refermsg/svrid/text()') # 被引用消息 id
            refer_svrid = len(e) > 0 and e[0] or None
            e = xml.xpath('/msg/appmsg/refermsg/fromusr/text()') # 被引用消息所在房间
            refer_fromusr = len(e) > 0 and e[0] or None
            e = xml.xpath('/msg/appmsg/refermsg/chatusr/text()') # 被引用消息发送人微信号
            refer_chatusr = len(e) > 0 and e[0] or None
            e = xml.xpath('/msg/appmsg/refermsg/displayname/text()') # 被引用消息发送人微信名称
            refer_displayname = len(e) > 0 and e[0] or refer_chatusr
            efb_msg = Message(
                type=MsgType.Text,
                text=msg,
                vendor_specific={ "is_refer": True }
            )
            prefix = ""
            sent_by_master = True
            if refer_svrid is not None:
                try:
                    # 从 master channel 中根据微信 id 查找，如果找到说明是由 comwechat self_msg 发送过去的
                    master_message = coordinator.master.get_message_by_id(chat=chat, msg_id=refer_svrid)
                    if master_message is not None:
                        sent_by_master = False
                except:
                    pass
            if refer_displayname is not None:
                prefix = f"{refer_displayname}:"
            # 被引用的消息是自己从 TG 发出去的(fromusr 是自己):强制文本拼接,
            # 不走原生回复(ETM 侧映射常缺失,原生回复会静默丢失引用关系)
            _quoted_self = refer_fromusr is not None and refer_fromusr == message["self"]
            if _quoted_self or refer_svrid is None or (refer_chatusr == message["self"] and sent_by_master):
                if refer_msgType == 1: # 被引用的消息是文本
                    refer_content = xml.xpath('/msg/appmsg/refermsg/content/text()')[0] # 被引用消息内容
                    result_text = qutoed_text(refer_content, msg, prefix)
                elif refer_msgType == 49: # 被引用的消息也是引用消息
                    try:
                        refer_msg_content = xml.xpath('/msg/appmsg/refermsg/content/text()')[0] # 被引用消息引用的消息
                        refer_msg_xml = etree.fromstring(refer_msg_content)
                        type = int(refer_msg_xml.xpath('/msg/appmsg/type/text()')[0])
                        if type == 57:
                            refer_msg_text = refer_msg_xml.xpath('/msg/appmsg/title/text()')[0]
                            result_text = qutoed_text(refer_msg_text, msg, prefix)
                        else:
                            result_text = msg
                    except Exception as e:
                        print_exc()
                else: # 被引用的消息非文本，提示不支持
                    result_text = qutoed_text(" 系统消息: 被引用的消息不是文本,暂不支持展示", msg, prefix)
                efb_msg.text = result_text
            else:
                efb_msg.target = Message(
                    uid=MessageID(refer_svrid),
                    chat=chat,
                )
        elif type == 87: # 群公告
            return
        #     title = xml.xpath('/msg/appmsg/textannouncement/text()')[0]
        #     efb_msg = Message(
        #         type=MsgType.Text,
        #         text= f"[群公告]:\n{title}" ,
        #         vendor_specific={ "is_mp": False }
        #     )
        elif type == 2000:
            subtype = xml.xpath("/msg/appmsg/wcpayinfo/paysubtype/text()")[0]
            money =  xml.xpath("/msg/appmsg/wcpayinfo/feedesc/text()")[0].strip("<![CDATA[").strip("]]>")
            if subtype == "1":
                efb_msg = Message(
                    type=MsgType.Text,
                    text= f"收到微信转账 {money} 元",
                    vendor_specific={ "is_mp": False }
                )
            elif subtype == "3":
                efb_msg = Message(
                    type=MsgType.Text,
                    text= f"接收微信转账 {money} 元",
                    vendor_specific={ "is_mp": False }
                )
            elif subtype == "4":
                efb_msg = Message(
                    type=MsgType.Text,
                    text= f"退还微信转账 {money} 元",
                    vendor_specific={ "is_mp": False }
                )
    except Exception:
        logging.getLogger("comwechat").warning(
            "分享消息解析失败: type=%s", appmsg_type, exc_info=True
        )

    if 'efb_msg' in locals():
        return efb_msg
    try:
        return _build_share_message(
            xml,
            message,
            appmsg_type,
            thumbnail_resolver=thumbnail_resolver,
        )
    except Exception:
        logging.getLogger("comwechat").warning(
            "未知分享消息无法规范化: type=%s", appmsg_type, exc_info=True
        )
        return efb_text_simple_wrapper("微信分享消息无法解析，请在微信查看原文")

def efb_location_wrapper(msg: str) -> Message:
    efb_msg = Message()
    text = re.search('''poiname="(.*?)"''', msg).group(1)
    x = re.search('''x="(.*?)"''', msg).group(1)
    y = re.search('''y="(.*?)"''', msg).group(1)
    efb_msg.text = text
    efb_msg.attributes = LocationAttribute(latitude=float(x),
                                           longitude=float(y))
    efb_msg.type = MsgType.Location
    return efb_msg

def efb_qqmail_wrapper(text: str) -> Message:
    xml = etree.fromstring(text)
    result_text = ""
    sender = xml.xpath('/msg/pushmail/content/sender/text()')[0].strip("<![CDATA[").strip("]]>")
    subjectwithCDATA = xml.xpath('/msg/pushmail/content/subject/text()')
    if len(subjectwithCDATA) != 0:
        subject = subjectwithCDATA[0].strip("<![CDATA[").strip("]]>")
    digest = xml.xpath('/msg/pushmail/content/digest/text()')[0].strip("<![CDATA[").strip("]]>")
    addr = xml.xpath('/msg/pushmail/content/fromlist/item/addr/text()')[0]
    datereceive = xml.xpath('/msg/pushmail/content/date/text()')[0].strip("<![CDATA[").strip("]]>")
    result_text = f"主题：{subject}\nfrom: {sender}\n收信时间: {datereceive}\n内容: {digest}"
    attribute = LinkAttribute(
        title= f'地址: {addr}',
        description= result_text,
        url= f"mailto:{addr}",
        image= None
    )
    efb_msg = Message(
        attributes=attribute,
        type=MsgType.Link,
        text=None,
        vendor_specific={ "is_mp": False }
    )
    return efb_msg

def efb_miniprogram_wrapper(text: str) -> Message:
    xml = etree.fromstring(text)
    result_text = ""
    title = xml.xpath('/msg/appmsg/title/text()')[0]
    programname = xml.xpath('/msg/appmsg/sourcedisplayname/text()')[0]
    imgurl = xml.xpath('/msg/appmsg/weappinfo/weappiconurl/text()')[0].strip("<![CDATA[").strip("]]>")
    url = xml.xpath('/msg/appmsg/url/text()')[0]
    result_text = f"from: {programname}\n  - - - - - - - - - - - - - - - \n微信小程序信息"
    attribute = LinkAttribute(
        title= f'{title}',
        description= result_text,
        url= url,
        image= imgurl
    )
    efb_msg = Message(
        attributes=attribute,
        type=MsgType.Link,
        text=None,
        vendor_specific={ "is_mp": False }
    )
    return efb_msg

def efb_unsupported_wrapper( text : str) -> Message:
    """
    A simple EFB message wrapper for unsupported message
    :param text: The content of the message
    :return: EFB Message
    """
    efb_msg = Message(
        type=MsgType.Unsupported,
        text=text
    )
    return efb_msg

def efb_voice_wrapper(file: IO, filename: str = None, text: str = None) -> Message:
    """
    A EFB message wrapper for voices.
    :param file: The file handle
    :param filename: The actual filename
    :param text: The attached text
    :return: EFB Message
    """
    efb_msg = Message()
    efb_msg.type = MsgType.Audio
    efb_msg.file = file
    mime = magic.from_file(efb_msg.file.name, mime=True)
    if isinstance(mime, bytes):
        mime = mime.decode()
    if filename:
        efb_msg.filename = filename
    else:
        efb_msg.filename = file.name
        efb_msg.filename += '.' + str(mime).split('/')[1]  # Add extension suffix
    efb_msg.path = efb_msg.file.name
    efb_msg.mime = mime
    if text:
        efb_msg.text = text
    return efb_msg

def efb_other_wrapper(text: str, chat) -> Union[Message, None]:
    """
    A simple EFB message wrapper for other message
    :param text: The content of the message
    :return: EFB Message or None
    """

    xml = etree.fromstring(text)
    efb_msg = None
    try:
        msg_type = xml.xpath('/sysmsg/@type')[0]
    except:
        return None

    if msg_type == "NewXmlAddForcePush":
        userIcon = xml.xpath('/sysmsg/userIcon/text()')[0]
        desc = xml.xpath('/sysmsg/description/text()')[0]

        extinfo = json.loads(xml.xpath('/sysmsg/extInfo/text()')[0])
        auth_icon_url = extinfo["auth_icon_url"]
        nickname = extinfo["nickname"]

        attribute = LinkAttribute(
            title = nickname,
            description = desc,
            url = auth_icon_url ,
            image = userIcon
        )
        efb_msg = Message(
            attributes=attribute,
            type=MsgType.Link,
            text= None,
            vendor_specific={ "is_mp": False }
        )
    elif msg_type == "voipmt" or msg_type == "multivoip":
        if "banner" in text:
            return None
        efb_msg = efb_text_simple_wrapper("[收到/取消 语音邀请]")
    elif msg_type == "delchatroommember":
        content = xml.xpath('//plain/text()')[0]
        efb_msg = efb_text_simple_wrapper(content)
    elif msg_type == "roomtoolstips":
        if str(xml.xpath('/sysmsg/todo/op/text()')[0]) == "0":
            efb_msg = efb_text_simple_wrapper("[发布了群待办]")
        elif str(xml.xpath('/sysmsg/todo/op/text()')[0]) == "1":
            efb_msg = efb_text_simple_wrapper("[撤回了群待办]")
    elif msg_type == "paymsg":
        try:
            paymsg_type = str(xml.xpath('/sysmsg/paymsg/PayMsgType/text()')[0])
        except:
            paymsg_type = ""
        if paymsg_type == "9":
            status = xml.xpath('/sysmsg/paymsg/status/text()')[0]
            displayname = xml.xpath('/sysmsg/paymsg/displayname/text()')[0]
            if str(status) == "0":
                efb_msg = efb_text_simple_wrapper(f"[{displayname} 进入扫码支付]")
            if str(status) == "1":
                efb_msg = efb_text_simple_wrapper(f"[{displayname} 完成了扫码支付]")
            elif str(status) == "2":
                efb_msg = efb_text_simple_wrapper(f"[{displayname} 取消了扫码支付]")
        if "待接收" in text and "转账" in text:
            efb_msg = efb_text_simple_wrapper("[你有一笔待接收的转账]")

    elif msg_type == "carditemmsg":
        msg_type = xml.xpath('/sysmsg/carditemmsg/msg_type/text()')[0]
        if str(msg_type) == "15":
            title = xml.xpath('/sysmsg/carditemmsg/title/text()')[0]
            desc = xml.xpath('/sysmsg/carditemmsg/description/text()')[0]
            url = xml.xpath('/sysmsg/carditemmsg/logo_url/text()')[0]
            attribute = LinkAttribute(
            title = title,
            description = desc,
            url = url,
            image = None
            )
            efb_msg = Message(
            attributes=attribute,
            type=MsgType.Link,
            text= None,
            vendor_specific={ "is_mp": False }
            )

    elif msg_type == "mmchatroombarannouncememt":
        title = xml.xpath('/sysmsg/mmchatroombarannouncememt/content/text()')[0]
        at_list = {}
        at_list[(1, 4)] = chat.self
        efb_msg = Message(
            type=MsgType.Text,
            text= f"[群公告]:\n{title}" ,
            vendor_specific={ "is_mp": False },
            substitutions = Substitutions(at_list)
        )

    if efb_msg:
        return efb_msg
    else:
        return None
