"""type 1/2 分享消息 des 缺失回归测试(2026-10-28:好名字分享在 TG 成原 XML)。

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


def _wrap(xml: str):
    return MsgDeco.efb_share_link_wrapper({"message": xml}, chat=None)


TYPE1_TITLE_ONLY = """<?xml version="1.0"?>
<msg><appmsg><title>适配快还是降价快？iPhone Duo、华为Pura X Max - https://www.bilibili.com/video/BV1ebxM61EkQ/</title><type>1</type><appinfo><appname>哔哩哔哩</appname></appinfo></appmsg></msg>"""

TYPE1_TITLE_DES = """<?xml version="1.0"?>
<msg><appmsg><title>标题A</title><des>描述B</des><type>1</type></appmsg></msg>"""

TYPE1_TITLE_EQ_DES = """<?xml version="1.0"?>
<msg><appmsg><title>同款</title><des>同款</des><type>1</type></appmsg></msg>"""

TYPE2_TITLE_ONLY = """<?xml version="1.0"?>
<msg><appmsg><title>仅标题</title><type>2</type></appmsg></msg>"""

TYPE1_NEITHER = """<?xml version="1.0"?>
<msg><appmsg><type>1</type></appmsg></msg>"""


def test_type1_title_only_returns_title_not_raw_xml():
    msg = _wrap(TYPE1_TITLE_ONLY)
    assert "适配快还是降价快" in msg.text
    assert "<?xml" not in msg.text
    assert "bilibili.com/video/BV1ebxM61EkQ" in msg.text


def test_type1_title_des_joined():
    msg = _wrap(TYPE1_TITLE_DES)
    assert msg.text == "标题A :\n描述B"


def test_type1_title_eq_des_single():
    msg = _wrap(TYPE1_TITLE_EQ_DES)
    assert msg.text == "同款"


def test_type2_title_only():
    msg = _wrap(TYPE2_TITLE_ONLY)
    assert msg.text == "仅标题"


def test_type1_neither_gives_notice_not_empty_not_xml():
    msg = _wrap(TYPE1_NEITHER)
    assert msg.text
    assert "<?xml" not in msg.text


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"{len(tests)} passed")
