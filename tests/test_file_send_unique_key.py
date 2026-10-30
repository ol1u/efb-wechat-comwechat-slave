"""文件发送误报 + file_key 抢占回归测试(2026-10-30:4 次连发文件已收到仍报失败)。

测两点:
1. File/Video 分支:SendFile 成功回 res["msg"]="0" 时不再抛 EFBMessageError
   (误报根因);SendFile 真抛异常时异常透传,且已注册的等待被丢弃(不留 stale key)。
2. file_key 唯一化:同一会话连发时各条等待 key 不同,回传按 FIFO 匹配,
   _wait 各取各的 msgid,互不顶替;超时仍返回 None 并清理 key。

ComWechat 依赖很重,无依赖时用轻量桩模块代替,只测 send_message/_wait/
_register_file_wait/_find_pending_file_key 的逻辑,不触通道与 hook。
pytest 风格的 test_* 函数;无 pytest 时也可直接跑本文件(__main__)。
"""
import logging
import sys
import threading
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "efb_wechat_comwechat_slave"


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


# ---- ehforwarderbot 桩 ----
class _MsgType:
    Text = "Text"
    Sticker = "Sticker"
    Image = "Image"
    Link = "Link"
    File = "File"
    Video = "Video"
    Animation = "Animation"
    Voice = "Voice"


class _EFBError(Exception):
    pass


class _EFBMessageError(_EFBError):
    pass


efb = _stub("ehforwarderbot", MsgType=_MsgType, Chat=object, Message=object,
            Status=object, coordinator=types.SimpleNamespace(master=None))
_stub("ehforwarderbot.chat", SystemChat=object, PrivateChat=object,
      SystemChatMember=object, ChatMember=object, SelfChatMember=object)
_stub("ehforwarderbot.message", MessageCommand=object, MessageCommands=object)
_stub("ehforwarderbot.types", MessageID=str, ChatID=str, InstanceID=str)
_stub("ehforwarderbot.exceptions", EFBException=_EFBError,
      EFBChatNotFound=_EFBError, EFBMessageError=_EFBMessageError,
      EFBOperationNotSupported=_EFBError)
_stub("ehforwarderbot.channel", SlaveChannel=object)
_stub("ehforwarderbot.status", MessageRemoval=object, ChatUpdates=object)
_stub("ehforwarderbot.utils", get_config_path=lambda *a, **k: "",
      extra=lambda *a, **k: (lambda f: f))
efb.chat = sys.modules["ehforwarderbot.chat"]
efb.message = sys.modules["ehforwarderbot.message"]
efb.types = sys.modules["ehforwarderbot.types"]
efb.exceptions = sys.modules["ehforwarderbot.exceptions"]
efb.channel = sys.modules["ehforwarderbot.channel"]
efb.status = sys.modules["ehforwarderbot.status"]
efb.utils = sys.modules["ehforwarderbot.utils"]

# ---- wechatrobot 桩 ----
_stub("wechatrobot", WeChatRobot=object)

# ---- 可选重依赖:有则真用,无则桩 ----
class _TTLCache(dict):
    def __init__(self, *a, **k):
        super().__init__()


try:
    import cachetools  # noqa: F401
except ImportError:
    _stub("cachetools", TTLCache=_TTLCache)
for _name, _attrs in [("pydub", {"AudioSegment": object}),
                      ("PIL", {"Image": object})]:
    try:
        __import__(_name)
    except ImportError:
        _stub(_name, **_attrs)
try:
    import rich  # noqa: F401
    import rich.console  # noqa: F401
except ImportError:
    _stub("rich", print=print)
    _stub("rich.console", Console=object)

# ---- 包内相对模块桩 ----
pkg = types.ModuleType("efb_wechat_comwechat_slave")
pkg.__path__ = [str(SRC)]
pkg.__version__ = "0.0.0-test"
sys.modules["efb_wechat_comwechat_slave"] = pkg
_ver = types.ModuleType("efb_wechat_comwechat_slave.__version__")
_ver.__version__ = "0.0.0-test"
sys.modules["efb_wechat_comwechat_slave.__version__"] = _ver
pkg.__version__ = _ver


def _write_dummy(file, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake")


for _sub, _attrs in [
    ("ChatMgr", {"ChatMgr": object}),
    ("CustomTypes", {"EFBGroupChat": object, "EFBPrivateChat": object,
                     "EFBGroupMember": object, "EFBSystemUser": object}),
    ("MsgDeco", {"qutoed_text": lambda *a, **k: ""}),
    ("MsgProcess", {"MsgProcess": object, "MsgWrapper": object}),
    ("Utils", {"download_file": lambda *a, **k: None,
               "load_config": lambda *a, **k: {},
               "load_temp_file_to_local": _write_dummy,
               "WC_EMOTICON_CONVERSION": {},
               "is_emoticon_share": lambda *a, **k: False,
               "emoticon_cdn_url": lambda *a, **k: "",
               "emoticon_full_urls": lambda *a, **k: [],
               "dump_message_ids": lambda *a, **k: "",
               "load_message_ids": lambda *a, **k: [],
               "is_message_reference": lambda *a, **k: True}),
    ("db", {"DatabaseManager": object}),
    ("delivery_retry", {"DeliveryRetryQueue": object}),
    ("Constant", {"QUOTE_MESSAGE": "quote:%s"}),
]:
    _stub(f"efb_wechat_comwechat_slave.{_sub}", **_attrs)

import importlib
comwechat = importlib.import_module("efb_wechat_comwechat_slave.ComWechat")
ComWeChatChannel = comwechat.ComWeChatChannel
from ehforwarderbot import MsgType  # noqa: E402
from ehforwarderbot.exceptions import EFBMessageError  # noqa: E402


class FakeBot:
    def __init__(self, res=None, exc=None):
        self.res = res if res is not None else {"msg": "0"}
        self.exc = exc
        self.sent = []

    def SendFile(self, receiver, file_path):
        self.sent.append((receiver, file_path))
        if self.exc is not None:
            raise self.exc
        return dict(self.res)


def _channel(bot, tmpdir, send_timeout=0.2):
    ch = ComWeChatChannel.__new__(ComWeChatChannel)
    ch.sent_msgs = {}
    ch.sent_msg_results = {}
    ch.pending_lock = threading.Lock()
    ch._file_op_seq = 0
    ch.cache = set()
    ch.logger = logging.getLogger("test-fileop")
    ch.bot = bot
    ch.dir = str(tmpdir) + "/"
    ch.wxid = "wxid_self"
    ch.is_wsl = False
    ch.base_path = str(tmpdir)
    ch.delete_file = {}
    ch.send_timeout = send_timeout
    return ch


def _file_msg(chat_uid, mtype, filename="doc.pdf", text=""):
    chat = types.SimpleNamespace(uid=chat_uid)
    f = types.SimpleNamespace(name="tmp123")
    return types.SimpleNamespace(chat=chat, edit=False, text=text, type=mtype,
                                 file=f, filename=filename, uid=None)


def test_file_res0_no_false_report(tmp_path):
    bot = FakeBot(res={"msg": "0"})
    ch = _channel(bot, tmp_path)
    msg = _file_msg("wxid_peer", MsgType.File)
    assert ch.send_message(msg) is msg
    assert len(bot.sent) == 1
    assert ch.sent_msgs == {}


def test_video_res0_no_false_report(tmp_path):
    bot = FakeBot(res={"msg": "0"})
    ch = _channel(bot, tmp_path)
    msg = _file_msg("wxid_peer", MsgType.Video, filename="v.mp4")
    assert ch.send_message(msg) is msg
    assert len(bot.sent) == 1


def test_sendfile_exception_propagates_and_cleans(tmp_path):
    bot = FakeBot(exc=RuntimeError("hook down"))
    ch = _channel(bot, tmp_path)
    try:
        ch.send_message(_file_msg("wxid_peer", MsgType.File))
    except RuntimeError:
        pass
    else:
        raise AssertionError("SendFile exception was swallowed")
    assert ch.sent_msgs == {}
    assert ch.sent_msg_results == {}


def test_file_keys_unique_and_fifo_matched(tmp_path):
    ch = _channel(FakeBot(), tmp_path)
    keys = [ch._register_file_wait("wxid_peer") for _ in range(3)]
    assert len(set(keys)) == 3
    for i, k in enumerate(keys):
        with ch.pending_lock:
            found = ch._find_pending_file_key("wxid_peer")
            assert found == k
            ch.sent_msg_results[found] = str(1000 + i)
            ch.sent_msgs[found].set()
        assert ch._wait(k, timeout=1) == str(1000 + i)
    assert ch._find_pending_file_key("wxid_peer") is None


def test_wait_timeout_returns_none_and_pops(tmp_path):
    ch = _channel(FakeBot(), tmp_path)
    key = ch._register_file_wait("wxid_peer")
    assert ch._wait(key, timeout=0.05) is None
    assert key not in ch.sent_msgs
    assert key not in ch.sent_msg_results


if __name__ == "__main__":
    import tempfile
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print("PASS", name)
    print(f"{len(tests)} passed")
