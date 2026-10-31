"""Parser for WeChat merged-forward chat history payloads."""

import re
from typing import Any, Dict, List, Optional, Union

from lxml import etree


MAX_FORWARD_DEPTH = 8
MAX_FORWARD_ITEMS = 200

TYPE_NAMES = {
    "1": "文本",
    "2": "图片",
    "3": "语音",
    "4": "视频",
    "5": "链接",
    "6": "位置",
    "8": "文件",
    "17": "聊天记录",
    "19": "小程序",
}

_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_EMPTY_MSG_IDS = {"", "0", "None", "null"}
_MEDIA_ELEMENT_NAMES = (
    "cdndataurl",
    "cdn_dataurl",
    "cdnbigimgurl",
    "cdnthumburl",
)


class ChatHistoryParseError(ValueError):
    pass


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        recover=False,
        huge_tree=False,
    )


def _fromstring(value: Union[str, bytes]) -> etree._Element:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    value = value.lstrip("\ufeff").strip()
    if not value:
        raise ChatHistoryParseError("chat history XML is empty")
    try:
        return etree.fromstring(value.encode("utf-8"), parser=_xml_parser())
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise ChatHistoryParseError("invalid chat history XML: {}".format(exc))


def _direct_text(node: etree._Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text is not None:
            return child.text.strip()
    return ""


def _text(node: etree._Element, *names: str) -> str:
    for name in names:
        child = node.find(".//{}".format(name))
        if child is not None and child.text is not None:
            return child.text.strip()
    return ""


def _recordinfo_from_element(node: etree._Element) -> Optional[etree._Element]:
    if node.tag == "recordinfo":
        return node
    direct = node.find(".//recordinfo")
    if direct is not None:
        return direct
    if node.tag == "recordxml" or node.tag == "recorditem":
        text = node.text or ""
        if text.strip():
            parsed = _fromstring(text)
            if parsed.tag == "recordinfo":
                return parsed
            nested = parsed.find(".//recordinfo")
            if nested is not None:
                return nested
    return None


def parse_recordinfo(
    value: Union[str, bytes, etree._Element],
    level: int = 1,
) -> etree._Element:
    """Return the ``recordinfo`` element from an XML string or container."""
    if level > MAX_FORWARD_DEPTH:
        raise ChatHistoryParseError("chat history nesting is too deep")
    if isinstance(value, etree._Element):
        root = value
    else:
        root = _fromstring(value)
    if root.tag == "recordinfo":
        return root
    if root.tag == "msg":
        appmsg = root.find("appmsg")
        if appmsg is not None:
            recorditem = appmsg.find("recorditem")
            if recorditem is not None:
                recordinfo = _recordinfo_from_element(recorditem)
                if recordinfo is not None:
                    return recordinfo
    recordinfo = _recordinfo_from_element(root)
    if recordinfo is None:
        raise ChatHistoryParseError("recordinfo was not found")
    return recordinfo


def _msgids(node: etree._Element) -> List[str]:
    candidates = [
        _direct_text(node, "fromnewmsgid"),
        _text(node, "dataitemsource/msgid"),
        _text(node, "dataitemsource/svrid"),
        _direct_text(node, "msgid"),
        _direct_text(node, "svrid"),
    ]
    result = []
    for value in candidates:
        if value not in _EMPTY_MSG_IDS and value not in result:
            result.append(value)
    return result


def _local_paths(node: etree._Element) -> List[str]:
    result = []
    for name in ("filepath", "datapath", "sourcepath"):
        value = _direct_text(node, name)
        if value and value not in result:
            result.append(value)
    datasourceid = _direct_text(node, "datasourceid")
    if datasourceid and (
        _WINDOWS_PATH_RE.match(datasourceid)
        or datasourceid.startswith("/")
    ):
        result.append(datasourceid)
    return result


def _cdn_values(node: etree._Element) -> Dict[str, Any]:
    urls = []
    for name in _MEDIA_ELEMENT_NAMES:
        value = _direct_text(node, name)
        if value and value not in urls:
            urls.append(value)
    emoji_urls = []
    for name in ("cdnurlstring", "cdnurl", "encrypturlstring"):
        value = _text(node, "emojiitem/{}".format(name))
        if value and value not in emoji_urls:
            emoji_urls.append(value.replace("&amp;", "&"))
    http_urls = [
        value
        for value in urls + emoji_urls
        if _HTTP_URL_RE.match(value)
    ]
    return {
        "cdn_urls": urls,
        "emoji_urls": emoji_urls,
        "http_urls": list(dict.fromkeys(http_urls)),
        "cdn_data_key": _direct_text(node, "cdndatakey", "cdn_data_key"),
        "full_md5": _direct_text(node, "fullmd5"),
    }


def _nested_recordinfo(node: etree._Element) -> Optional[etree._Element]:
    recordxml = node.find("recordxml")
    if recordxml is None:
        return None
    nested = _recordinfo_from_element(recordxml)
    if nested is not None:
        return nested
    text = recordxml.text or ""
    if text.strip():
        root = _fromstring(text)
        return root if root.tag == "recordinfo" else root.find(".//recordinfo")
    return None


def parse_chat_history(
    value: Union[str, bytes, etree._Element],
    level: int = 1,
) -> List[Dict[str, Any]]:
    """Parse record items while preserving order, sender, time and media IDs."""
    root = parse_recordinfo(value, level=level)
    datalist = root.find("datalist")
    if datalist is None:
        datalist = root.find(".//datalist")
    if datalist is None:
        return []

    result = []
    for node in datalist.findall("dataitem")[:MAX_FORWARD_ITEMS]:
        datatype = str(node.get("datatype") or "")
        item = {
            "datatype": datatype,
            "type_name": TYPE_NAMES.get(datatype, "未知类型"),
            "dataid": str(node.get("dataid") or ""),
            "sourcename": _direct_text(node, "sourcename", "srcname"),
            "sourcetime": _direct_text(node, "sourcetime"),
            "src_create_time": _direct_text(node, "srcMsgCreateTime"),
            "datatitle": _direct_text(node, "datatitle"),
            "datadesc": _direct_text(node, "datadesc", "datatext"),
            "datafmt": _direct_text(node, "datafmt"),
            "duration": _direct_text(node, "duration"),
            "msgids": _msgids(node),
            "local_paths": _local_paths(node),
            "level": level,
            "children": [],
        }
        item.update(_cdn_values(node))

        if datatype == "17":
            nested = _nested_recordinfo(node)
            if nested is not None:
                item["title"] = _direct_text(nested, "title") or item["datatitle"]
                item["children"] = parse_chat_history(nested, level=level + 1)
            else:
                item["title"] = item["datatitle"]
        return_item = item
        result.append(return_item)
    return result


def parse_forwarded_message(
    value: Union[str, bytes, etree._Element],
) -> Dict[str, Any]:
    """Parse a type-19 app message and its embedded recordinfo."""
    root = value if isinstance(value, etree._Element) else _fromstring(value)
    appmsg = root.find("appmsg") if root.tag == "msg" else root
    if appmsg is None:
        raise ChatHistoryParseError("appmsg was not found")
    title = _direct_text(appmsg, "title")
    description = _direct_text(appmsg, "des")
    recordinfo = parse_recordinfo(root)
    title = title or _direct_text(recordinfo, "title")
    description = description or _direct_text(recordinfo, "desc", "info")
    return {
        "title": title or "转发的聊天记录",
        "description": description,
        "items": parse_chat_history(recordinfo),
    }


def item_meta(item: Dict[str, Any]) -> str:
    sender = item.get("sourcename") or "未知发送者"
    sent_at = item.get("sourcetime") or ""
    return "{} · {}".format(sender, sent_at) if sent_at else sender


def item_text(item: Dict[str, Any]) -> str:
    """Create a readable text fallback and caption for one record item."""
    meta = item_meta(item)
    datatype = item.get("datatype") or ""
    title = item.get("datatitle") or ""
    description = item.get("datadesc") or ""
    type_name = item.get("type_name") or "未知类型"

    if datatype == "1":
        body = description or title or "[空文本]"
        if item.get("emoji_urls"):
            body = description or "[动画表情]"
    elif datatype == "5":
        body = title or "[链接]"
        if description:
            body += "\n{}".format(description)
        url = next(
            (value for value in item.get("http_urls", []) if _HTTP_URL_RE.match(value)),
            "",
        )
        if url:
            body += "\n{}".format(url)
    elif datatype == "17":
        count = len(item.get("children") or [])
        body = "{}（{} 条）".format(title or "[聊天记录]", count)
    elif datatype in ("2", "3", "4", "8"):
        body = title or "[{}]".format(type_name)
    else:
        body = description or title or "[{}]".format(type_name)
    return "{}\n{}".format(meta, body)


def iter_history_items(items: List[Dict[str, Any]]):
    for item in items:
        yield item
        for child in item.get("children") or []:
            for nested in iter_history_items([child]):
                yield nested
