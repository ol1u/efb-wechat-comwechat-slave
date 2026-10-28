"""type 1/2 分享消息标注式输出测试(2026-10-28:好名字分享在 TG 成原 XML)。

第一版修 des 缺失判空;第二版(本文件)改为标注式输出:首行说明是分享消息
及 type,标题/描述/链接/来源各字段有无都明说,不再只发裸标题。

MsgDeco 依赖 ehforwarderbot;无该依赖时用轻量桩模块代替,
只测 efb_share_link_wrapper 的纯解析分支,不触通道逻辑。
pytest 风格的 test_* 函数;无 pytest 时也可直接跑本文件(__main__)。
"""
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "efb_wechat_comwechat_slave"

try:
    import ehforwarderbot  # noqa: F401
except ImportError:
    ehforwarderbot = types.ModuleType("ehforwarderbot")
    chat_mod = types.ModuleType("ehforwarderbot.chat")
    msg_mod = types.ModuleType("ehforwarderbot.message")
    types_mod = types.ModuleType("ehforwarderbot.types")

    class _MsgType:
        Text = "Text"
        Link = "Link"
        Unsupported = "Unsupported"
        Location = "Location"

    class _Message:
        def __init__(self, type=None, text=None, attributes=None, vendor_specific=None, **kw):
            self.type = type
            self.text = text
            self.attributes = attributes
            self.vendor_specific = vendor_specific

    class _LinkAttribute:
        def __init__(self, title=None, description=None, url=None, image=None):
            self.title, self.description, self.url, self.image = title, description, url, image

    class _LocationAttribute:
        def __init__(self, latitude=None, longitude=None):
            self.latitude, self.longitude = latitude, longitude

    ehforwarderbot.MsgType = _MsgType
    ehforwarderbot.Chat = object
    ehforwarderbot.coordinator = types.SimpleNamespace(master=None)
    chat_mod.ChatMember = object
    msg_mod.Substitutions = object
    msg_mod.Message = _Message
    msg_mod.LinkAttribute = _LinkAttribute
    msg_mod.LocationAttribute = _LocationAttribute
    types_mod.MessageID = str
    for _name, _mod in [("ehforwarderbot", ehforwarderbot),
                        ("ehforwarderbot.chat", chat_mod),
                        ("ehforwarderbot.message", msg_mod),
                        ("ehforwarderbot.types", types_mod)]:
        sys.modules[_name] = _mod

try:
    import magic  # noqa: F401
except ImportError:
    sys.modules.setdefault("magic", types.ModuleType("magic"))

# 不执行包 __init__:以命名空间包姿态加载 MsgDeco.py 本体。
pkg = types.ModuleType("efb_wechat_comwechat_slave")
pkg.__path__ = [str(SRC)]
sys.modules["efb_wechat_comwechat_slave"] = pkg
for _sub, _attrs in [("ChatMgr", {"ChatMgr": object}),
                     ("CustomTypes", {"EFBGroupChat": object, "EFBPrivateChat": object})]:
    _mod = types.ModuleType(f"efb_wechat_comwechat_slave.{_sub}")
    for _k, _v in _attrs.items():
        setattr(_mod, _k, _v)
    sys.modules[f"efb_wechat_comwechat_slave.{_sub}"] = _mod

import importlib
MsgDeco = importlib.import_module("efb_wechat_comwechat_slave.MsgDeco")
from ehforwarderbot import MsgType


def _wrap(xml: str):
    return MsgDeco.efb_share_link_wrapper({"message": xml}, chat=None)


# 好名字 20:26 那条的形状:有 title 与来源,无 des、无 url。
TYPE1_TITLE_ONLY = """<?xml version="1.0"?>
<msg><appmsg><title>适配快还是降价快？iPhone Duo、华为Pura X Max - https://www.bilibili.com/video/BV1ebxM61EkQ/</title><type>1</type></appmsg><appinfo><appname>哔哩哔哩</appname></appinfo></msg>"""

TYPE1_FULL = """<?xml version="1.0"?>
<msg><appmsg><title>标题A</title><des>描述B</des><url>https://example.com/x</url><type>1</type></appmsg><appinfo><appname>某应用</appname></appinfo></msg>"""

TYPE1_TITLE_EQ_DES = """<?xml version="1.0"?>
<msg><appmsg><title>同款</title><des>同款</des><type>1</type></appmsg></msg>"""

TYPE2_TITLE_ONLY = """<?xml version="1.0"?>
<msg><appmsg><title>仅标题</title><type>2</type></appmsg></msg>"""

TYPE1_NEITHER = """<?xml version="1.0"?>
<msg><appmsg><type>1</type></appmsg></msg>"""


def test_type1_title_only_labeled_not_raw_xml():
    msg = _wrap(TYPE1_TITLE_ONLY)
    assert msg.text == (
        "微信分享消息（type=1）\n"
        "标题：适配快还是降价快？iPhone Duo、华为Pura X Max - https://www.bilibili.com/video/BV1ebxM61EkQ/\n"
        "描述：无\n"
        "链接：无\n"
        "来源：哔哩哔哩"
    )
    assert "<?xml" not in msg.text


def test_type1_full_all_fields_shown():
    msg = _wrap(TYPE1_FULL)
    assert msg.text == (
        "微信分享消息（type=1）\n"
        "标题：标题A\n"
        "描述：描述B\n"
        "链接：https://example.com/x\n"
        "来源：某应用"
    )


def test_type1_title_eq_des_both_lines_shown():
    msg = _wrap(TYPE1_TITLE_EQ_DES)
    assert msg.text == (
        "微信分享消息（type=1）\n"
        "标题：同款\n"
        "描述：同款\n"
        "链接：无\n"
        "来源：无"
    )


def test_type2_header_shows_type_2():
    msg = _wrap(TYPE2_TITLE_ONLY)
    assert msg.text == (
        "微信分享消息（type=2）\n"
        "标题：仅标题\n"
        "描述：无\n"
        "链接：无\n"
        "来源：无"
    )


def test_type1_neither_all_none_plus_hint():
    msg = _wrap(TYPE1_NEITHER)
    assert msg.text == (
        "微信分享消息（type=1）\n"
        "标题：无\n"
        "描述：无\n"
        "链接：无\n"
        "来源：无\n"
        "（无可用内容，请在微信查看原文）"
    )
    assert "<?xml" not in msg.text




# 视频号 51/63:与 type 1/2 同款标注式输出;有 url 时仍为链接卡片,标注文本随卡片一起发。
CH51_NO_URL = """<msg><appmsg><type>51</type><title>测试视频标题</title><finderFeed><nickname>作者名</nickname><desc>视频文案</desc><avatar>http://example.com/a.jpg</avatar></finderFeed></appmsg></msg>"""

CH51_WITH_URL = """<msg><appmsg><type>51</type><title>标题</title><url>http://example.com/v</url><finderFeed><nickname>作者</nickname><desc>文案</desc></finderFeed></appmsg></msg>"""

CH51_URL_NO_DESC = """<msg><appmsg><type>51</type><title>标题</title><url>http://example.com/v</url><finderFeed></finderFeed></appmsg></msg>"""

CH63_NO_URL = """<msg><appmsg><type>63</type><title>直播标题</title><finderLive><desc>直播文案</desc><headUrl>http://example.com/h.jpg</headUrl></finderLive></appmsg></msg>"""

CH63_WITH_URL = """<msg><appmsg><type>63</type><title>直播标题</title><url>http://example.com/live</url><finderLive><desc>文案</desc><headUrl>http://example.com/h.jpg</headUrl></finderLive></appmsg></msg>"""


def test_type51_no_url_labeled_text():
    msg = _wrap(CH51_NO_URL)
    assert msg.type == MsgType.Text
    assert msg.text == (
        "微信分享消息（type=51）\n"
        "标题：测试视频标题\n"
        "描述：视频文案\n"
        "链接：无\n"
        "作者：作者名\n"
        "头像：有"
    )


def test_type51_with_url_still_link_with_labeled_text():
    msg = _wrap(CH51_WITH_URL)
    assert msg.type == MsgType.Link
    assert msg.attributes.url == "http://example.com/v"
    assert msg.text == (
        "微信分享消息（type=51）\n"
        "标题：标题\n"
        "描述：文案\n"
        "链接：http://example.com/v\n"
        "作者：作者\n"
        "头像：无"
    )


def test_type51_url_no_desc_missing_fields_shown_as_none():
    msg = _wrap(CH51_URL_NO_DESC)
    assert msg.type == MsgType.Link
    assert msg.text == (
        "微信分享消息（type=51）\n"
        "标题：标题\n"
        "描述：无\n"
        "链接：http://example.com/v\n"
        "作者：无\n"
        "头像：无"
    )


def test_type63_no_url_labeled_text_no_author_line():
    msg = _wrap(CH63_NO_URL)
    assert msg.type == MsgType.Text
    assert msg.text == (
        "微信分享消息（type=63）\n"
        "标题：直播标题\n"
        "描述：直播文案\n"
        "链接：无\n"
        "头像：有"
    )


def test_type63_with_url_still_link_with_labeled_text():
    msg = _wrap(CH63_WITH_URL)
    assert msg.type == MsgType.Link
    assert msg.attributes.url == "http://example.com/live"
    assert msg.text == (
        "微信分享消息（type=63）\n"
        "标题：直播标题\n"
        "描述：文案\n"
        "链接：http://example.com/live\n"
        "头像：有"
    )

if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"{len(tests)} passed")
