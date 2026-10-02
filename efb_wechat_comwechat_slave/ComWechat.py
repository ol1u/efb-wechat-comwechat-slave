import logging, tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from lxml import etree
from traceback import print_exc
from pydub import AudioSegment
import os
import base64
from pathlib import Path
from xml.sax.saxutils import escape

import re
import json
from ehforwarderbot.chat import SystemChat, PrivateChat , SystemChatMember, ChatMember, SelfChatMember
import hashlib
from typing import Tuple, Optional, Collection, BinaryIO, Dict, Any , Union , List
from datetime import datetime
from cachetools import TTLCache

from ehforwarderbot import MsgType, Chat, Message, Status, coordinator
from wechatrobot import WeChatRobot

from . import __version__ as version

from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.types import MessageID, ChatID, InstanceID
from ehforwarderbot import utils as efb_utils
from ehforwarderbot.exceptions import EFBException, EFBChatNotFound, EFBMessageError, EFBOperationNotSupported
from ehforwarderbot.message import MessageCommand, MessageCommands
from ehforwarderbot.status import MessageRemoval, ChatUpdates

from .ChatMgr import ChatMgr
from .CustomTypes import EFBGroupChat, EFBPrivateChat, EFBGroupMember, EFBSystemUser
from .MsgDeco import qutoed_text
from .MsgProcess import MsgProcess, MsgWrapper
from .Utils import download_file , load_config , load_temp_file_to_local , WC_EMOTICON_CONVERSION , is_emoticon_share , emoticon_cdn_url , emoticon_full_urls , dump_message_ids , load_message_ids , is_message_reference
from .db import DatabaseManager
from .Constant import QUOTE_MESSAGE

from rich.console import Console
from rich import print as rprint
from io import BytesIO
from PIL import Image

# 语音数据库分片名(兜底用,实际以 hook 上报的句柄列表为准;移植自上游 b278d27)
VOICE_DATABASE_NAMES = ("MediaMSG0.db", "MediaMSG1.db", "MediaMSG2.db")

class ComWeChatChannel(SlaveChannel):
    channel_name : str = "ComWechatChannel"
    channel_emoji : str = "💻"
    channel_id : str = "honus.comwechat"

    bot : WeChatRobot = None
    config : Dict = {}

    friends : EFBPrivateChat = []
    groups : EFBGroupChat    = []

    contacts : Dict = {}            # {wxid : {alias : str , remark : str, nickname : str , type : int}} -> {wxid : name(after handle)}
    nicknames : Dict = {}
    group_members : Dict = {}       # {"group_id" : { "wxID" : "displayName"}}

    time_out : int = 120
    cache =  TTLCache(maxsize=200, ttl= time_out)  # 缓存发送过的消息ID
    file_msg : Dict = {}                           # 存储待修改的文件类消息 {path : msg}
    delete_file : Dict = {}                        # 存储待删除的消息 {path : time}
    forward_pattern = r"ehforwarderbot:\/\/([^/]+)\/forward\/(\d+)"

    __version__ = version.__version__
    logger: logging.Logger = logging.getLogger("comwechat")
    logger.setLevel(logging.DEBUG)

    #MsgType.Voice
    supported_message_types = {MsgType.Text, MsgType.Sticker, MsgType.Image , MsgType.Link , MsgType.File , MsgType.Video , MsgType.Animation, MsgType.Voice}
    self_update_lock = threading.Lock()
    contact_update_lock = threading.Lock()
    group_update_lock = threading.Lock()

    def __init__(self, instance_id: InstanceID = None):
        super().__init__(instance_id=instance_id)
        self.logger.info("ComWeChat Slave Channel initialized.")
        self.logger.info("Version: %s" % self.__version__)
        self.config = load_config(efb_utils.get_config_path(self.channel_id))
        self.db: DatabaseManager = DatabaseManager(self)

        # 配置API连接参数
        self.api_host = self.config.get("api_host", "127.0.0.1")
        self.api_port = self.config.get("api_port", 18888)
        self.api_base_url = f"http://{self.api_host}:{self.api_port}"

        # 配置WeChatRobot实例使用自定义host和port
        self.bot = WeChatRobot(ip="0.0.0.0", port=23456, api_host=self.api_host, api_port=self.api_port)

        self.wxid = None
        # 入站投递线程池:Hook 回调只做轻量投递,不在回调线程里阻塞。
        # 避免某条消息处理中 hanging 时占住 Hook 线程,导致后续所有消息(包括文字)进不来。
        self._inbound_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cw-inbound")
        # 发往 master(最终调 Telegram 接口)的投递线程池,配合超时使用,超时即放弃不拖死流水线
        self._deliver_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cw-deliver")
        self._deliver_timeout = 60  # 单条消息投递超时(秒)
        self._file_probe: Dict[str, Tuple[int, float]] = {}  # {path: (size, 首次观测时间)} 文件就绪探测
        self._voice_db_names: Optional[List[str]] = None  # 语音库分片名缓存,移植自上游 b278d27
        # 撤回/编辑支持(移植自上游 a847ad3/aca50dc)
        self.sent_msgs: Dict[Any, threading.Event] = {}  # {(wxid, text, seq): Event} 等待 hook 回传 msgid
        self.sent_msg_results: Dict[Any, MessageID] = {}
        self.pending_lock = threading.Lock()
        self._send_seq = 0  # 发送序号,保证每个 _wait key 唯一,避免快速连发时后者覆盖前者
        self.revoke_message_ids = TTLCache(maxsize=200, ttl=max(self.time_out, 1))  # 防撤回回声
        self.send_timeout = self.config.get("send_timeout", 5)  # 等待 hook 回传 msgid 的超时(秒)
        self.base_path = self.config["base_path"] if "base_path" in self.config else self.bot.get_base_path()
        self.load()
        self.dir = self.config["dir"]
        if not self.dir.endswith(os.path.sep):
            self.dir += os.path.sep
        
        try:
            import subprocess
            import json
            
            url = f'{self.api_base_url}/api/?type=35'
            payload = {'version': '3.9.12.55'}
            payload_str = json.dumps(payload)
            
            self.logger.info(f"向Hook发送微信版本号: {payload['version']}")
            cmd = ["curl", "-X", "POST", url, "-d", payload_str]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            
            if result.returncode != 0:
                self.logger.error(f"设置微信版本号的curl命令执行失败. Curl stderr: {result.stderr.strip()}")
            else:
                try:
                    response = json.loads(result.stdout)
                    # Assuming a response with 'result' == 'OK' indicates success.
                    if response.get('result') == 'OK':
                        self.logger.info("成功设置微信版本号.")
                    else:
                        self.logger.error(f"设置微信版本号失败，Hook返回: {result.stdout.strip()}")
                except json.JSONDecodeError:
                    self.logger.error(f"解析Hook返回的JSON失败. Response: {result.stdout.strip()}")
                    
        except Exception as e:
            self.logger.error(f"设置微信版本号失败: {e}")

        # WSL环境检测和路径转换配置
        self.is_wsl = self._detect_wsl()
        if self.is_wsl:
            self.logger.info("检测到WSL环境，启用WSL到Windows路径转换")
            try:
                import subprocess
                import json

                # 移除末尾的路径分隔符
                clean_dir = self.dir.rstrip(os.path.sep)
                win_path = self._wsl_to_windows_path(clean_dir)

                payload = {"save_path": win_path}
                payload_str = json.dumps(payload)

                # 设置图片保存路径 (type=13)
                url13 = f'{self.api_base_url}/api/?type=13'
                self.logger.info(f"向Hook发送图片保存路径: {win_path}")
                cmd13 = ["curl", "-X", "POST", url13, "-d", payload_str]
                result13 = subprocess.run(cmd13, capture_output=True, text=True, timeout=5)
                if result13.returncode != 0:
                    self.logger.error(f"设置图片保存路径的curl命令执行失败. Curl stderr: {result13.stderr.strip()}")
                else:
                    try:
                        response = json.loads(result13.stdout)
                        if response.get('msg') == 1 and response.get('result') == 'OK':
                            self.logger.info("成功设置Hook图片保存路径.")
                        else:
                            self.logger.error(f"设置Hook图片保存路径失败，Hook返回: {result13.stdout.strip()}")
                    except json.JSONDecodeError:
                        self.logger.error(f"解析Hook返回的JSON失败. Response: {result13.stdout.strip()}")

                # 设置语音保存路径 (type=11)
                url11 = f'{self.api_base_url}/api/?type=11'
                self.logger.info(f"向Hook发送语音保存路径: {win_path}")
                cmd11 = ["curl", "-X", "POST", url11, "-d", payload_str]
                result11 = subprocess.run(cmd11, capture_output=True, text=True, timeout=5)
                if result11.returncode != 0:
                    self.logger.error(f"设置语音保存路径的curl命令执行失败. Curl stderr: {result11.stderr.strip()}")
                else:
                    try:
                        response = json.loads(result11.stdout)
                        if response.get('msg') == 1 and response.get('result') == 'OK':
                            self.logger.info("成功设置Hook语音保存路径.")
                        else:
                            self.logger.error(f"设置Hook语音保存路径失败，Hook返回: {result11.stdout.strip()}")
                    except json.JSONDecodeError:
                        self.logger.error(f"解析Hook返回的JSON失败. Response: {result11.stdout.strip()}")

            except Exception as e:
                self.logger.error(f"设置Windows Hook路径失败: {e}")
        
        ChatMgr.slave_channel = self
        self.user_auth_chat = ChatMgr.build_efb_chat_as_system_user(EFBSystemUser(
            uid = self.channel_name,
            name = self.channel_name,
        ))

        def update_contacts_wrapper(func):
            def wrapper(msg):
                if self.wxid is None:
                    self.confirm_login()
                if self.wxid is None:
                    # 仍未登录,丢弃并记日志。
                    # 之前这里是 if self.confirm_login(): 结构,但 confirm_login()
                    # 没有 return 语句永远返回 None,导致重启后第一条消息必被无声丢弃。
                    self.logger.warning("登录确认失败,丢弃消息: type=%s", msg.get("type"))
                    return
                return func(msg)
            return wrapper

        @self.bot.on("sent_msg")
        def on_sent_msg(msg: Dict):
            """hook 回传已发送消息的微信 msgid,唤醒 _wait(移植自上游,用于撤回/编辑)。"""
            self.logger.debug(f"on_sent_msg received: {msg}")
            sender: str = msg.get("sender")
            msgid = msg.get("msgid")
            message_content = msg.get("message")
            filepath = msg.get("filepath")

            if not sender or not msgid:
                self.logger.warning("on_sent_msg missing sender or msgid.")
                return

            if msgid in self.cache:
                self.logger.warning("self msg due to bug from upstream.")
                return

            key = None
            with self.pending_lock:
                # 按注册顺序(FIFO)匹配最早的等待项。每个发送 key 都带唯一序号,
                # 相同文本快速连发/多文件连发时不再互相覆盖,按 hook 回传顺序依次匹配。
                # key 格式:(sender, content_or_None, seq),文件类发送的 content 为 None。
                for k in list(self.sent_msgs.keys()):
                    if not isinstance(k, tuple) or len(k) != 3:
                        continue
                    k_sender, k_content, _ = k
                    if k_sender != sender:
                        continue
                    if filepath:
                        if k_content is None:
                            key = k
                            break
                    elif message_content:
                        if k_content == message_content:
                            key = k
                            break

                if key is not None:
                    event = self.sent_msgs[key]
                    self.sent_msg_results[key] = MessageID(str(msgid))
                    event.set()
                    self.logger.debug(f"Matched sent message {key} with msgid {msgid}. Signaled event.")
                else:
                    self.logger.debug(f"No pending message found matching sender {sender}.")

        @self.bot.on("self_msg")
        @update_contacts_wrapper
        def on_self_msg(msg : Dict):
            self.logger.debug(f"self_msg:{msg}")
            sender = msg["sender"]

            name = self.get_name_by_wxid(sender)

            if "@chatroom" in sender:
                chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                    uid = sender,
                    name = name,
                ))
                author = chat.self
                self.extract_alias(msg)
            else:
                chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                    uid = sender,
                    name = name,
                ))
                if sender.startswith('gh_'):
                    chat.vendor_specific = {'is_mp' : True}
                author = chat.self

            self._dispatch_inbound(msg , author , chat)

        @self.bot.on("friend_msg")
        @update_contacts_wrapper
        def on_friend_msg(msg : Dict):
            self.logger.debug(f"friend_msg:{msg}")

            sender = msg['sender']

            if msg["type"] == "eventnotify":
                return

            name = self.get_name_by_wxid(sender)

            chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                    uid= sender,
                    name= name,
            ))
            if sender.startswith('gh_'):
                chat.vendor_specific = {'is_mp' : True}
                self.logger.debug(f'modified_chat:{chat}')
            author = chat.other
            self._dispatch_inbound(msg, author, chat)

        @self.bot.on("group_msg")
        @update_contacts_wrapper
        def on_group_msg(msg : Dict):
            self.logger.debug(f"group_msg:{msg}")
            sender = msg["sender"]
            wxid  =  msg["wxid"]

            chatname = self.get_name_by_wxid(sender)

            chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                uid = sender,
                name = chatname,
            ))

            try:
                name = self.contacts[wxid]
            except:
                name = wxid
            self.extract_alias(msg)
            alias = self.group_members.get(sender,{}).get(wxid , None)
            if alias == self.nicknames.get(wxid, None):
                alias = None

            author = ChatMgr.build_efb_chat_as_member(chat, EFBGroupMember(
                uid = wxid,
                name = name,
                alias = alias
            ))
            self._dispatch_inbound(msg, author, chat)

        @self.bot.on("revoke_msg")
        @update_contacts_wrapper
        def on_revoked_msg(msg : Dict):
            self.logger.debug(f"revoke_msg:{msg}")
            sender = msg["sender"]
            if "@chatroom" in sender:
                wxid  =  msg["wxid"]

            name = self.get_name_by_wxid(sender)

            if "@chatroom" in sender:
                chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                    uid = sender,
                    name = name,
                ))
                xml = etree.fromstring(msg["message"])
                text = xml.xpath('string(/sysmsg/revokemsg/replacemsg)')
                alias = re.search(r'^"(.*?)" (撤回了一条消息|recalled a message)$', text)
                if alias and alias.group(1) != self.get_nickname_by_wxid(wxid):
                    self.merge_group_members(sender, {
                        wxid: alias.group(1)
                    })
            else:
                chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                    uid = sender,
                    name = name,
                ))

            newmsgid = MessageID(re.search("<newmsgid>(.*?)<\/newmsgid>", msg["message"]).group(1))

            # 防回声:自己从 TG 发起撤回后,微信会回一个撤回通知,直接忽略(移植自上游 a847ad3)
            if self.revoke_message_ids.get(newmsgid):
                self.logger.debug("Ignoring revoke feedback for server msgid %s", newmsgid)
                return

            efb_msg = Message(chat = chat , uid = newmsgid)
            coordinator.send_status(
                MessageRemoval(source_channel=self, destination_channel=coordinator.master, message=efb_msg)
            )

        @self.bot.on("transfer_msg")
        @update_contacts_wrapper
        def on_transfer_msg(msg : Dict):
            self.logger.debug(f"transfer_msg:{msg}")
            sender = msg["sender"]
            name = self.get_name_by_wxid(sender)

            if msg["isSendMsg"]:
                if msg["isSendByPhone"]:
                    chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                            uid= sender,
                            name= name,
                    ))
                    author = chat.other
                    self._dispatch_inbound(msg, author, chat)
                    return

            content = {}

            money = re.search("收到转账(.*)元", msg["message"]).group(1)
            transcationid = re.search("<transcationid><!\[CDATA\[(.*)\]\]><\/transcationid>", msg["message"]).group(1)
            transferid = re.search("<transferid><!\[CDATA\[(.*)\]\]><\/transferid>", msg["message"]).group(1)
            text = (
                f"收到 {name} 转账:\n"
                f"金额为 {money} 元\n"
            )

            commands = [
                MessageCommand(
                    name=("Accept"),
                    callable_name="process_transfer",
                    kwargs={"transcationid" : transcationid , "transferid" : transferid , "wxid" : sender},
                )
            ]

            content["sender"] = sender
            content["message"] = text
            content["commands"] = commands
            content["name"] = name
            self.system_msg(content)

        @self.bot.on("frdver_msg")
        @update_contacts_wrapper
        def on_frdver_msg(msg : Dict):
            self.logger.debug(f"frdver_msg:{msg}")
            content = {}
            sender = msg["sender"]
            fromnickname = re.search('fromnickname="(.*?)"', msg["message"]).group(1)
            apply_content = re.search('content="(.*?)"', msg["message"]).group(1)
            url = re.search('bigheadimgurl="(.*?)"', msg["message"]).group(1)
            v3 = re.search('encryptusername="(v3.*?)"', msg["message"]).group(1)
            v4 = re.search('ticket="(v4.*?)"', msg["message"]).group(1)
            text = (
                "好友申请:\n"
                f"名字: {fromnickname}\n"
                f"验证内容: {apply_content}\n"
                f"头像: {url}"
            )

            commands = [
                MessageCommand(
                    name=("Accept"),
                    callable_name="process_friend_request",
                    kwargs={"v3" : v3 , "v4" : v4},
                )
            ]

            content["sender"] = sender
            content["message"] = text
            content["commands"] = commands
            self.system_msg(content)

        @self.bot.on("card_msg")
        @update_contacts_wrapper
        def on_card_msg(msg : Dict):
            self.logger.debug(f"card_msg:{msg}")
            sender = msg["sender"]
            wxid = msg["wxid"]
            content = {}
            name = self.get_name_by_wxid(sender)

            bigheadimgurl = re.search('bigheadimgurl="(.*?)"', msg["message"]).group(1)
            nickname = re.search('nickname="(.*?)"', msg["message"]).group(1)
            province = re.search('province="(.*?)"', msg["message"]).group(1)
            city = re.search('city="(.*?)"', msg["message"]).group(1)
            sex = re.search('sex="(.*?)"', msg["message"]).group(1)
            username = re.search('username="(.*?)"', msg["message"]).group(1)

            text = "名片信息:\n"
            if nickname:
                text += f"昵称: {nickname}\n"
            if city:
                text += f"城市: {city}\n"
            if province:
                text += f"省份: {province}\n"
            if sex:
                if sex == "0":
                    text += "性别: 未知\n"
                elif sex == "1":
                    text += "性别: 男\n"
                elif sex == "2":
                    text += "性别: 女\n"
            if bigheadimgurl:
                text += f"头像: {bigheadimgurl}\n"

            commands = [
                MessageCommand(
                    name=("Add To Friend"),
                    callable_name="add_friend",
                    kwargs={"v3" : username},
                )
            ]

            if "@chatroom" in sender:
                chat = ChatMgr.build_efb_chat_as_group(EFBGroupChat(
                    uid = sender,
                    name = self.get_name_by_wxid(sender)
                ))
                if sender == wxid:
                    author = chat.self
                else:
                    alias = self.group_members.get(sender,{}).get(wxid , None),
                    alias = None if alias == name else alias
                    author = ChatMgr.build_efb_chat_as_member(chat, EFBGroupMember(
                        uid = wxid,
                        name = name,
                        alias = alias
                    ))
            else:
                chat = ChatMgr.build_efb_chat_as_private(EFBPrivateChat(
                    uid = sender,
                    name = name,
                ))
                author = chat.self if sender == self.wxid else chat.other
                if sender.startswith('gh_'):
                    chat.vendor_specific = {'is_mp' : True}

            # if "v3" in username:
            #     content["commands"] = commands
            # 暂时屏蔽
            m = Message(
                type=MsgType.Text,
                text=text
            )
            self.send_efb_msgs(MsgWrapper(msg, m), author=author, chat=chat, uid=MessageID(str(msg['msgid'])))

    def is_login(self) -> bool:
        try:
            response = self.bot.IsLoginIn()
            return response.get("is_login", 0) == 1
        except:
            return False

    def get_qrcode(self):
        result = self.bot.GetQrcodeImage()
        
        # 检查是否返回了 JSON 数据（已登录）
        try:
            json_result = json.loads(result)
            return None
        except Exception:
            return self.save_qr_code(result)

    @staticmethod
    def save_qr_code(qr_code):
        # 创建临时文件保存二维码图片
        tmp_file = tempfile.NamedTemporaryFile(suffix='.png')
        try:
            tmp_file.write(qr_code)
            tmp_file.flush()
        except:
            print("[red]获取二维码失败[/red]")
            tmp_file.close()
            return None
        return tmp_file

    def confirm_login(self):
        chat = self.user_auth_chat
        author = self.user_auth_chat.other
        msg = Message(
            type=MsgType.Text,
            uid=MessageID(str(int(time.time()))),
        )
        if self.is_login():
            self.after_login()
            msg.text = "登录成功"
        else:
            msg.text = "登录失败，请重新登录"
        self.send_efb_msgs(msg, chat=chat, author=author)

    def after_login(self):
        self.get_me()
        self.GetContactListBySql()
        self.GetGroupListBySql()

    @efb_utils.extra(name="Get QR Code",
           desc="重新扫码登录")
    def reauth(self, _: str = "") -> str:
        file = self.get_qrcode()
        chat = self.user_auth_chat
        author = self.user_auth_chat.other
        msg = Message(
            type=MsgType.Text,
            uid=MessageID(str(int(time.time()))),
        )

        if not file:
            if self.is_login():
                self.after_login()
                return "登录成功"
            else:
                return "获取二维码失败，请稍后再试"
        else:
            msg.type = MsgType.Image
            msg.path = Path(file.name)
            msg.file = file
            msg.mime = 'image/png'
            self.send_efb_msgs(msg, chat=chat, author=author)
        return "请扫描二维码登录"

    @efb_utils.extra(name="Force Logout",
           desc="强制退出")
    def force_logout(self, _: str = "") -> str:
        res = self.bot.post(44, params=EmptyJsonResponse())
        if self.is_login():
            return "退出失败，原因: %s" % res
        else:
            self.wxid = None
            return "退出成功"

    def send_efb_msgs(self, efb_msgs: Union[Message, List[Message]], **kwargs):
        if not efb_msgs:
            return
        efb_msgs = [efb_msgs] if isinstance(efb_msgs, Message) else efb_msgs
        if 'deliver_to' not in kwargs:
            kwargs['deliver_to'] = coordinator.master
        for efb_msg in efb_msgs:
            for k, v in kwargs.items():
                setattr(efb_msg, k, v)
            # 投递到 master(最终调 Telegram 接口)可能因网络问题 hanging,
            # 用超时隔离:超时即放弃该条并记 ERROR,不让它卡住整条流水线。
            # 注意:极端情况下超时后后台线程仍可能投递成功,会产生一条重复消息,
            # 这是为保住流水线而接受的代价,概率极低。
            def _deliver(m):
                try:
                    coordinator.send_message(m)
                finally:
                    try:
                        if m.file:
                            m.file.close()
                    except Exception:
                        pass
            future = self._deliver_executor.submit(_deliver, efb_msg)
            try:
                future.result(timeout=self._deliver_timeout)
            except FuturesTimeoutError:
                self.logger.error(
                    "投递消息到 Telegram 超时(%ss),已跳过以保住流水线: type=%s uid=%s",
                    self._deliver_timeout, getattr(efb_msg, 'type', '?'), kwargs.get('uid'))
                # 尽力而为:补一条超时提示,避免用户侧无声丢失。
                # 提示本身也走同样的超时投递,若网络已断则发不出,仅记日志。
                # _is_timeout_notice 防止提示的投递再超时时无限递归。
                if not kwargs.get('_is_timeout_notice'):
                    try:
                        notice = Message()
                        notice.text = "[消息投递超时,请在手机端查看]"
                        self.send_efb_msgs(
                            notice,
                            uid=f"{kwargs.get('uid')}-timeout",
                            chat=kwargs.get('chat'),
                            author=kwargs.get('author'),
                            type=MsgType.Text,
                            _is_timeout_notice=True,
                        )
                    except Exception:
                        self.logger.exception("发送投递超时提示失败")
            except Exception:
                self.logger.exception("投递消息到 Telegram 异常: uid=%s", kwargs.get('uid'))

    def system_msg(self, content : Dict):
        self.logger.debug(f"system_msg:{content}")
        msg = Message()
        sender = content["sender"]
        if "name" in content:
            name = content["name"]
        else:
            name  = '\u2139 System'

        chat = ChatMgr.build_efb_chat_as_system_user(EFBSystemUser(
            uid = sender,
            name = name
        ))

        try:
            author = chat.get_member(SystemChatMember.SYSTEM_ID)
        except KeyError:
            author = chat.add_system_member()

        if "commands" in content:
            msg.commands = MessageCommands(content["commands"])
        if "message" in content:
            msg.text = content['message']
        if "target" in content:
            msg.target = content['target']

        self.send_efb_msgs(msg, uid=int(time.time()), chat=chat, author=author, type=MsgType.Text)

    def _dispatch_inbound(self, msg : Dict[str, Any] , author : 'ChatMember' , chat : 'Chat'):
        """Hook 回调的轻量投递入口:把耗时处理丢进线程池,回调线程立即返回。

        之前回调里直接同步处理,某条消息 hanging 会占住 Hook 线程,
        后续所有消息(包括文字消息)都进不来,表现为偶发漏消息。
        """
        try:
            self._inbound_executor.submit(self._handle_msg_guarded, msg, author, chat)
        except Exception:
            self.logger.exception("入站消息投递到线程池失败: type=%s", msg.get("type"))

    def _handle_msg_guarded(self, msg : Dict[str, Any] , author : 'ChatMember' , chat : 'Chat'):
        try:
            self.handle_msg(msg, author, chat)
        except Exception:
            # 毒消息(缺字段、结构异常等)不能无声丢失,记 ERROR 便于定位
            self.logger.exception("处理入站消息失败,已丢弃: type=%s msgid=%s",
                                  msg.get("type"), msg.get("msgid"))

    def handle_msg(self , msg : Dict[str, Any] , author : 'ChatMember' , chat : 'Chat'):
        emojiList = re.findall('\[[\w|！|!| ]+\]' , msg["message"])
        for emoji in emojiList:
            try:
                msg["message"] = msg["message"].replace(emoji, WC_EMOTICON_CONVERSION[emoji])
            except:
                pass

        if msg["msgid"] not in self.cache:
            self.cache[msg["msgid"]] = msg["type"]
        else:
            if self.cache[msg["msgid"]] == msg["type"]:
                return

        try:
            if ("FileStorage" in msg["filepath"]) and ("Cache" not in msg["filepath"]):
                # 表情包(share/appmsg type=8):有 CDN 地址就跳过延迟队列直接处理。
                # 大表情包以加密文件形式传输,本地文件永不出现,进延迟队列必超时,
                # 故有 cdnurl 或 emojiinfo 地址时直接走 CDN 通道(不行则快速失败)。
                if is_emoticon_share(msg) and (emoticon_cdn_url(msg) or emoticon_full_urls(msg)):
                    pass
                else:
                    msg["timestamp"] = int(time.time())
                    msg["filepath"] = msg["filepath"].replace("\\","/")
                    msg["filepath"] = f'''{self.dir}{msg["filepath"]}'''
                    self.file_msg[msg["filepath"]] = ( msg , author , chat )
                    return
            if msg["type"] == "video":
                msg["timestamp"] = int(time.time())
                msg["filepath"] = msg["thumb_path"].replace("\\","/").replace(".jpg", ".mp4")
                msg["filepath"] = f'''{self.dir}{msg["filepath"]}'''
                self.file_msg[msg["filepath"]] = ( msg , author , chat )
                return
        except:
            ...

        if msg["type"] == "voice":
            file_path = re.search("clientmsgid=\"(.*?)\"", msg["message"]).group(1) + ".amr"
            msg["timestamp"] = int(time.time())
            msg["filepath"] = f'''{self.dir}{msg["self"]}/{file_path}'''
            self.file_msg[msg["filepath"]] = ( msg , author , chat )
            return

        try:
            efb_msgs = MsgProcess(msg, chat)
        except Exception:
            # 单条消息处理失败时降级为文本提示,避免异常上浮拖死接收线程导致后续消息丢失
            self.logger.exception("MsgProcess 处理消息失败,降级为文本提示: type=%s", msg.get("type"))
            msg['message'] = f"[{msg.get('type')} 接收失败,请在手机端查看]"
            msg["type"] = "text"
            efb_msgs = MsgProcess(msg, chat)
        self.send_efb_msgs(MsgWrapper(msg, efb_msgs), author=author, chat=chat, uid=MessageID(str(msg['msgid'])))

    def _drop_pending_file(self, path: str):
        """从延迟队列移除一条文件消息,同时清理就绪探测状态。"""
        self.file_msg.pop(path, None)
        self._file_probe.pop(path, None)

    def _file_is_stable(self, path: str, stable_secs: float = 2.0) -> bool:
        """文件存在且大小连续 stable_secs 秒不再变化(下载完成),才算就绪。

        用跨轮询的探测代替 time.sleep,避免单线程处理大量延迟文件时
        被 sleep 串行拖慢,导致就绪的文件也要排长队。
        """
        try:
            size = os.path.getsize(path)
        except OSError:
            self._file_probe.pop(path, None)
            return False
        if size <= 0:
            self._file_probe.pop(path, None)
            return False
        now = time.time()
        prev = self._file_probe.get(path)
        if prev is None or prev[0] != size:
            self._file_probe[path] = (size, now)
            return False
        if now - prev[1] >= stable_secs:
            self._file_probe.pop(path, None)
            return True
        return False

    def _voice_database_names(self, refresh=False):
        """发现微信语音数据库分片名(移植自上游 b278d27,适配本仓库结构)。

        本仓库未启用 DbKeyManager,文件扫描一级不可用;直接用 hook 上报的
        数据库句柄列表做发现,拿不到则回退到默认三个分片。结果缓存,失败时清缓存重试。
        """
        cached = getattr(self, "_voice_db_names", None)
        if cached is not None and not refresh:
            return list(cached)

        names = []
        try:
            handles = self.bot.GetDatabaseHandles().get("data") or []
            names = [
                item.get("db_name")
                for item in handles
                if isinstance(item, dict)
                and isinstance(item.get("db_name"), str)
                and item["db_name"].startswith("MediaMSG")
            ]
        except Exception:
            self.logger.debug("获取微信数据库句柄列表失败", exc_info=True)

        if not names:
            names = list(VOICE_DATABASE_NAMES)
        return sorted(set(names), key=lambda name: (len(name), name))

    def _next_send_seq(self) -> int:
        """分配发送序号,保证每个 _wait key 唯一。"""
        with self.pending_lock:
            self._send_seq += 1
            return self._send_seq

    def _wait(self, key: Any, timeout: int) -> Optional[MessageID]:
        """等待 hook 回传指定 key 的微信 msgid(移植自上游,用于撤回/编辑)。"""
        event = self.sent_msgs.get(key)
        if not event:
            self.logger.error(f"No event found for key {key} before waiting.")
            return None

        self.logger.debug(f"Waiting for event for key: {key} with timeout {timeout}s")
        event_set = event.wait(timeout=timeout)

        with self.pending_lock:
            self.sent_msgs.pop(key, None)
            received_msgid = self.sent_msg_results.pop(key, None)

        if not event_set or not received_msgid:
            # 拿不到 msgid:这条消息的撤回/编辑将不可用,直接告警方便排查 hook 事件
            self.logger.warning(
                "[msgid-missing] 未收到 hook 的 sent_msg 回传(key=%s),"
                "本条消息无法撤回/编辑。请检查 hook 是否正常推送 sent_msg 事件。",
                key,
            )
            return None
        return received_msgid

    def _deliver_pending_file(self, path: str):
        """处理一条延迟等待的文件消息,成功或降级后从队列移除。"""
        msg = self.file_msg[path][0]
        author = self.file_msg[path][1]
        chat = self.file_msg[path][2]

        # 下载超时:降级为文本提示,避免无限等待
        if (int(time.time()) - msg["timestamp"]) > self.time_out:
            self.logger.warning("文件 %s 下载超时,降级为文本提示", path)
            msg['message'] = f"[{msg['type']} 下载超时,请在手机端查看]"
            msg["type"] = "text"
            self._drop_pending_file(path)
            self.send_efb_msgs(MsgWrapper(msg, MsgProcess(msg, chat)), author=author, chat=chat, uid=MessageID(str(msg['msgid'])))
            return

        # 语音:文件未出现时尝试从数据库提取音频数据,轮询全部 MediaMSG 分片
        # (移植自上游 b278d27:之前写死只查 MediaMSG0.db,语音在别的分片就取不到)
        if msg["type"] == "voice" and not os.path.exists(path):
            sql = f'SELECT Buf FROM Media WHERE Reserved0 = {msg["msgid"]}'
            database_names = self._voice_database_names()
            filebuffer = None
            for attempt in range(2):
                for db_name in database_names:
                    try:
                        dbresult = self.bot.QueryDatabase(
                            db_handle=self.bot.GetDBHandle(db_name), sql=sql)["data"]
                    except Exception:
                        continue  # 该分片不存在或查询失败,换下一个分片
                    if len(dbresult) == 2:
                        filebuffer = dbresult[1][0]
                        break
                if filebuffer is not None:
                    break
                if attempt == 0:
                    # 第一轮都没查到:清分片缓存,刷新列表再试一轮
                    self._voice_db_names = None
                    try:
                        self.bot.invalidate_db_handles()
                    except Exception:
                        pass  # hook 无此接口时忽略
                    database_names = self._voice_database_names(refresh=True)
            if filebuffer is None:
                self.logger.debug("语音数据在 %s 均未查到,继续等待文件: %s",
                                  ",".join(database_names), path)
                return
            try:
                decoded = bytes(base64.b64decode(filebuffer))
                with open(msg["filepath"], 'wb') as f:
                    f.write(decoded)
            except Exception:
                self.logger.exception("语音数据解码/写入失败,继续等待文件: %s", path)
                return

        # 文件就绪且稳定才处理,避免读到下载中的半截文件
        if not (os.path.exists(path) and self._file_is_stable(path)):
            return

        self._drop_pending_file(path)
        try:
            efb_msgs = MsgProcess(msg, chat)
        except Exception:
            self.logger.exception("处理文件消息失败,降级为文本提示: %s", path)
            msg['message'] = f"[{msg['type']} 接收失败,请在手机端查看]"
            msg["type"] = "text"
            efb_msgs = MsgProcess(msg, chat)
        self.send_efb_msgs(MsgWrapper(msg, efb_msgs), author=author, chat=chat, uid=MessageID(str(msg['msgid'])))

    def handle_file_msg(self):
        while True:
            try:
                for path in list(self.file_msg.keys()):
                    try:
                        self._deliver_pending_file(path)
                    except Exception:
                        # 单条消息异常不能拖死整个处理线程,否则后续所有延迟消息都收不到
                        self.logger.exception("处理延迟文件消息异常,已跳过: %s", path)
                        self._drop_pending_file(path)
                if len(self.delete_file):
                    for k in list(self.delete_file.keys()):
                        file_path = k
                        begin_time = self.delete_file[k]
                        if  (int(time.time()) - begin_time) > self.time_out:
                            try:
                                os.remove(file_path)
                            except:
                                pass
                            del self.delete_file[file_path]
            except Exception:
                self.logger.exception("handle_file_msg 主循环异常,稍后继续")
            time.sleep(1)

    def process_friend_request(self , v3 , v4):
        self.logger.debug(f"process_friend_request:{v3} {v4}")
        res = self.bot.VerifyApply(v3 = v3 , v4 = v4)
        if str(res['msg']) != "0":
            return "Success"
        else:
            return "Failed"

    def process_transfer(self, transcationid , transferid , wxid):
        res = self.bot.GetTransfer(transcationid = transcationid , transferid = transferid , wxid = wxid)
        if str(res["msg"]) != "0":
            return "Success"
        else:
            return "Failed"

    def add_friend(self , v3):
        res = self.bot.AddContactByV3(v3 = v3 , msg = "")
        if str(res['msg']) != "0":
            return "Success"
        else:
            return "Failed"

    # 定时任务
    def scheduled_job(self):
        count = 0
        content = {
            "name": self.channel_name,
            "sender": self.channel_name,
            "message": "检测到未登录状态，请发送 /extra 重新扫码登录",
        }
        while True:
            time.sleep(1)
            count += 1
            if count % 1800 == 0:
                if self.wxid is not None:
                    self.GetGroupListBySql()
                    self.GetContactListBySql()
            if count % 1800 == 3:
                if getattr(coordinator, 'master', None) is not None and not self.is_login():
                    self.wxid = None
                    self.system_msg(content)

    #获取全部联系人
    def get_chats(self) -> Collection['Chat']:
        return []

    #获取联系人
    def get_chat(self, chat_uid: ChatID) -> 'Chat':
        if "@chatroom" in chat_uid:
            for group in self.groups:
                if group.uid == chat_uid:
                    return group
        else:
            for friend in self.friends:
                if friend.uid == chat_uid:
                    return friend
        raise EFBChatNotFound

    #发送消息
    def send_message(self, msg : Message) -> Message:
        chat_uid = msg.chat.uid
        msg_ids: List[MessageID] = []  # 收集微信 msgid,写回 msg.uid 供撤回/编辑用(移植自上游)

        # TG 编辑消息 -> 微信:无原生编辑,用撤回+重发模拟(移植自上游 aca50dc)
        if msg.edit:
            if (msg.text or "").startswith("/"):
                raise EFBMessageError("不支持编辑命令消息")

            references = list(dict.fromkeys(load_message_ids(msg.uid))) if msg.uid else []
            if not references:
                raise EFBMessageError("编辑消息缺少有效的消息 ID")
            invalid_reference = next(
                (reference for reference in references if not is_message_reference(reference)),
                None,
            )
            if invalid_reference:
                raise EFBMessageError(f"无效的消息 ID: {invalid_reference}")

            if not msg.edit_media and msg.type in (
                MsgType.Voice,
                MsgType.Image,
                MsgType.File,
                MsgType.Video,
                MsgType.Animation,
                MsgType.Sticker,
            ):
                # 只改了配文、媒体没换:撤回旧配文 -> 重发文字 -> 更新 uid
                media_reference = references[0]
                caption_references = references[1:]
                if caption_references:
                    caption = Message(
                        chat=msg.chat,
                        uid=dump_message_ids(caption_references),
                    )
                    self.send_status(MessageRemoval(self, self, caption))
                if msg.text:
                    caption_reference = self.send_text(chat_uid, msg)
                    if caption_reference is None:
                        raise EFBMessageError("发送失败，请在手机端确认")
                    msg.uid = dump_message_ids([media_reference, caption_reference])
                else:
                    msg.uid = media_reference
                return msg

            # 其他情况:先撤回原微信消息,再走正常流程重发
            self.send_status(MessageRemoval(self, self, msg))
            msg.edit = False

        if self.wxid is None:
            if self.is_login():
                self.after_login()
            else:
                content = {
                    "name": self.user_auth_chat.name,
                    "sender": self.user_auth_chat.uid,
                    "message": "尚未登录，请发送 /extra 扫码登录"
                }
                self.system_msg(content)
                return msg

        if msg.text:
            match = re.search(self.forward_pattern, msg.text)
            if match:
                if match.group(1) == hashlib.md5(self.channel_id.encode('utf-8')).hexdigest():
                    msgid = match.group(2)
                    self.logger.debug(f"提取到的消息 ID: {msgid}")
                    self.bot.ForwardMessage(wxid = chat_uid, msgid = msgid)
                else:
                    self.logger.debug(f"非本 slave 消息: {match.group(1)}/{match.group(2)}")
                return msg

        if msg.type == MsgType.Voice:
            try:
                f = tempfile.NamedTemporaryFile(prefix='voice_message_', suffix=".mp3")
                AudioSegment.from_ogg(msg.file.name).export(f, format="mp3")
            except Exception:
                # ogg 损坏/格式异常时之前直接抛异常,整条发送失败且提示含糊。
                # 改为明确报错,让用户在 TG 看到原因。
                self.logger.exception("TG 语音转码 mp3 失败")
                raise EFBMessageError("语音转码失败,请在手机端确认")
            msg.file = f
            msg.file.name = "语音留言.mp3"
            msg.type = MsgType.Video
            msg.filename = os.path.basename(f.name)

        if msg.type in [MsgType.Text]:
            if msg.text.startswith('/changename'):
                newname = msg.text.strip('/changename ')
                res = self.bot.SetChatroomName(chatroom_id = chat_uid , chatroom_name = newname)
            elif msg.text.startswith('/getmemberlist'):
                memberlist = self.bot.GetChatroomMemberList(chatroom_id = chat_uid)
                message = '群组成员包括：'
                for wxid in memberlist['members'].split('^G'):
                    try:
                        name = self.contacts[wxid]
                    except:
                        try:
                            name = self.bot.GetChatroomMemberNickname(chatroom_id = chat_uid, wxid = wxid)['nickname'] or wxid
                        except:
                            name = wxid
                    message += '\n' + wxid + ' : ' + name
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/getstaticinfo'):
                info = msg.text[15::]
                if info == 'friends':
                    message = str(self.friends)
                elif info == 'groups':
                    message = str(self.groups)
                elif info == 'group_members':
                    message = json.dumps(self.group_members)
                elif info == 'contacts':
                    message = json.dumps(self.contacts)
                else:
                    message = '当前仅支持查询friends, groups, group_members, contacts'
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/helpcomwechat'):
                message = '''/search - 按关键字匹配好友昵称搜索联系人

/addtogroup - 按wxid添加好友到群组

/getmemberlist - 查看群组用户wxid

/at - 后面跟wxid，多个用英文,隔开，最后可用空格隔开，带内容。

/sendcard - 后面格式'wxid nickname'

/changename - 修改群组名称

/addfriend - 后面格式'wxid message'

/getstaticinfo - 可获取friends, groups, contacts信息'''
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/search'):
                keyword = msg.text[8::]
                message = 'result:'
                for key, value in self.contacts.items():
                    if keyword in value:
                        message += '\n' + str(key) + " : " + str(value)
                self.system_msg({'sender':chat_uid, 'message':message})
            elif msg.text.startswith('/addtogroup'):
                users = msg.text[12::]
                res = self.bot.AddChatroomMember(chatroom_id = chat_uid, wxids = users)
            elif msg.text.startswith('/forward'):
                if isinstance(msg.target, Message):
                    msgid = msg.target.uid
                    if msgid.isdecimal():
                        url = f"ehforwarderbot://{hashlib.md5(self.channel_id.encode('utf-8')).hexdigest()}/forward/{msgid}"
                        prompt = "请将这条信息转发到目标聊天中"
                        text = f"{url}\n{prompt}"
                        if msg.target.text:
                            match = re.search(self.forward_pattern, msg.target.text)
                            if match:
                                msg.target.text = f"{msg.target.text[0:match.start()]}{text}"
                            else:
                                msg.target.text = f"{msg.target.text}\n\n---\n{text}"
                        else:
                            msg.target.text = text
                        self.send_efb_msgs(msg.target, edit=True)
                    else:
                        text = f"无法转发{msgid},不是有效的微信消息"
                        self.system_msg({'sender': chat_uid, 'message': text, 'target': msg.target})
                    return msg
            elif msg.text.startswith('/at'):
                users_message = msg.text[4::].split(' ', 1)
                if isinstance(msg.target, Message):
                    users = msg.target.author.uid
                    message = msg.text[4::]
                elif len(users_message) == 2:
                    users, message = users_message
                else:
                    users, message = users_message[0], ''
                if users != '':
                    res = self.bot.SendAt(chatroom_id = chat_uid, wxids = users, msg = message)
                else:
                    self.bot.SendText(wxid = chat_uid , msg = msg.text)
            elif msg.text.startswith('/sendcard'):
                user_nickname = msg.text[10::].split(' ', 1)
                if len(user_nickname) == 2:
                    user, nickname = user_nickname
                else:
                    user, nickname = user_nickname[0], ''
                if user != '':
                    res = self.bot.SendCard(receiver = chat_uid, share_wxid = user, nickname = nickname)
                else:
                    self.bot.SendText(wxid = chat_uid , msg = msg.text)
            elif msg.text.startswith('/addfriend'):
                user_invite = msg.text[11::].split(' ', 1)
                if len(user_invite) == 2:
                    user, invite = user_invite
                else:
                    user, invite = user_invite[0], ''
                if user != '':
                    res = self.bot.AddContactByWxid(wxid = user, msg = invite)
                else:
                    self.bot.SendText(wxid = chat_uid , msg = msg.text)
            else:
                text_msgid = self.send_text(wxid = chat_uid , msg = msg)
                if text_msgid:
                    msg_ids.append(text_msgid)
                res = {"msg": "1" if text_msgid else "0"}
        elif msg.type in [MsgType.Link]:
            link_msgid = self.send_text(wxid = chat_uid , msg = msg)
            if link_msgid:
                msg_ids.append(link_msgid)
        elif msg.type in [MsgType.Image , MsgType.Sticker]:
            name = os.path.basename(msg.file.name)
            local_path = f"{self.dir}{self.wxid}/{name}"
            load_temp_file_to_local(msg.file, local_path)
            
            # WSL环境下需要将路径转换为Windows格式
            if self.is_wsl:
                img_path = self._wsl_to_windows_path(local_path)
                self.logger.debug(f"WSL路径转换: {local_path} -> {img_path}")
            else:
                img_path = os.path.join(self.base_path, self.wxid, name)
            
            self.logger.debug(f"发送图片路径: {img_path}")
            file_key = (chat_uid, None, self._next_send_seq())
            with self.pending_lock:
                self.sent_msgs[file_key] = threading.Event()
            res = self.bot.SendImage(receiver = chat_uid , img_path = img_path)
            media_msgid = self._wait(file_key, self.send_timeout)
            if media_msgid:
                msg_ids.append(media_msgid)
            self.delete_file[local_path] = int(time.time())
            if msg.text:
                self.send_text(wxid = chat_uid , msg = msg)
        elif msg.type in [MsgType.File , MsgType.Video]:
            name = os.path.basename(msg.file.name)
            local_path = f"{self.dir}{self.wxid}/{name}"
            load_temp_file_to_local(msg.file, local_path)
            
            if msg.filename:
                try:
                    os.rename(local_path , f"{self.dir}{self.wxid}/{msg.filename}")
                except:
                    os.replace(local_path , f"{self.dir}{self.wxid}/{msg.filename}")
                local_path = f"{self.dir}{self.wxid}/{msg.filename}"
            
            # WSL环境下需要将路径转换为Windows格式
            if self.is_wsl:
                file_path = self._wsl_to_windows_path(local_path)
                self.logger.debug(f"WSL路径转换: {local_path} -> {file_path}")
            else:
                filename = msg.filename if msg.filename else name
                file_path = os.path.join(self.base_path, self.wxid, filename)
            
            self.logger.debug(f"发送文件路径: {file_path}")
            file_key = (chat_uid, None, self._next_send_seq())
            with self.pending_lock:
                self.sent_msgs[file_key] = threading.Event()
            res = self.bot.SendFile(receiver = chat_uid , file_path = file_path)
            media_msgid = self._wait(file_key, self.send_timeout)
            if media_msgid:
                msg_ids.append(media_msgid)
            self.delete_file[local_path] = int(time.time())
            if msg.text:
                self.send_text(wxid = chat_uid , msg = msg)
            if msg.type == MsgType.Video:
                res["msg"] = 1
        elif msg.type in [MsgType.Animation]:
            name = os.path.basename(msg.file.name)
            local_path = f"{self.dir}{self.wxid}/{name}"
            load_temp_file_to_local(msg.file, local_path)
            
            # WSL环境下需要将路径转换为Windows格式
            if self.is_wsl:
                file_path = self._wsl_to_windows_path(local_path)
                self.logger.debug(f"WSL路径转换: {local_path} -> {file_path}")
            else:
                file_path = os.path.join(self.base_path, self.wxid, name)
            
            self.logger.debug(f"发送动画表情路径: {file_path}")
            file_key = (chat_uid, None, self._next_send_seq())
            with self.pending_lock:
                self.sent_msgs[file_key] = threading.Event()
            res = self.bot.SendEmotion(wxid = chat_uid , img_path = file_path)
            media_msgid = self._wait(file_key, self.send_timeout)
            if media_msgid:
                msg_ids.append(media_msgid)
            self.delete_file[local_path] = int(time.time())
            if msg.text:
                self.send_text(wxid = chat_uid , msg = msg)

        # 发送失败必须抛给 ETM,让用户在 TG 看到提示。
        # 注意:之前写成 try 里 raise、except 里吞掉,失败时用户毫无感知。
        try:
            send_failed = str(res["msg"]) == "0"
        except Exception:
            send_failed = False  # res 结构异常时不误判,保持原有容错行为
        if send_failed:
            raise EFBMessageError("发送失败，请在手机端确认")
        # 保存微信 msgid 供撤回/编辑用(移植自上游)
        if msg_ids:
            msg.uid = dump_message_ids(msg_ids)
        return msg

    def send_text(self, wxid: ChatID, msg: Message) -> Optional[MessageID]:
        """发送文本并等待 hook 回传微信 msgid(移植自上游,用于撤回/编辑)。
        返回微信服务器 msgid,超时/失败返回 None。"""
        text = msg.text
        if isinstance(msg.target, Message):
                if isinstance(msg.target.author, SelfChatMember) and isinstance(msg.target.deliver_to, SlaveChannel):
                    qt_txt = msg.target.text or msg.target.type.name
                    text = qutoed_text(qt_txt, msg.text)
                else:
                    # 取第一个有效的微信 msgid;没有则降级为文本引用,避免拼出坏 XML
                    # (移植自上游 c1c7bae 思想:引用目标无有效 msgid 时不硬拼)
                    target_msgid = next(
                        (item for item in load_message_ids(msg.target.uid or "") if item.isdecimal()),
                        None,
                    )
                    if target_msgid is None:
                        self.logger.debug(
                            "引用目标无有效微信 msgid,降级为文本引用: uid=%s", msg.target.uid)
                        qt_txt = msg.target.text or msg.target.type.name
                        text = qutoed_text(qt_txt, msg.text)
                    else:
                        msgid = target_msgid
                        sender = msg.target.author.uid
                        displayname = self.group_members.get(wxid,{}).get(sender, self.get_nickname_by_wxid(sender))
                        content = escape(msg.target.vendor_specific.get("wx_xml", ""), {
                            "\n": "&#x0A;",
                            "\t": "&#x09;",
                            '"': "&quot;",
                        }) or msg.target.text
                        comwechat_info = msg.target.vendor_specific.get("comwechat_info", {})
                        if comwechat_info.get("type", None) == "animatedsticker":
                            refer_type = 47
                        elif msg.target.type == MsgType.Image:
                            refer_type = 3
                        elif msg.target.type == MsgType.Voice:
                            refer_type = 34
                        elif msg.target.type == MsgType.Video:
                            refer_type = 43
                        elif msg.target.type == MsgType.Sticker:
                            refer_type = 47
                        elif msg.target.type == MsgType.Location:
                            refer_type = 48
                        elif msg.target.type == MsgType.File:
                            refer_type = 49
                        elif comwechat_info.get("type", None) == "share":
                            refer_type = 49
                        else:
                            refer_type = 1
                        if content:
                            content = "<content>%s</content>" % content
                        else:
                            content = "<content />"
                        xml = QUOTE_MESSAGE % (self.wxid, text, refer_type, msgid, sender, sender, displayname, content)
                        key = (wxid, xml, self._next_send_seq())
                        with self.pending_lock:
                            self.sent_msgs[key] = threading.Event()
                        self.bot.SendXml(wxid = wxid , xml = xml, img_path = "")
                        return self._wait(key, self.send_timeout)
        key = (wxid, text, self._next_send_seq())
        with self.pending_lock:
            self.sent_msgs[key] = threading.Event()
        self.bot.SendText(wxid = wxid , msg = text)
        return self._wait(key, self.send_timeout)

    def get_chat_picture(self, chat: 'Chat') -> BinaryIO:
        wxid = chat.uid
        result = self.bot.GetPictureBySql(wxid = wxid)
        if result:
            return download_file(result)
        else:
            return None

    def get_chat_member_picture(self, chat_member: 'ChatMember') -> BinaryIO:
        wxid = chat_member.uid
        result = self.bot.GetPictureBySql(wxid = wxid)
        if result:
            return download_file(result)
        else:
            return None

    def poll(self):
        timer = threading.Thread(target = self.scheduled_job)
        timer.daemon = True
        timer.start()

        while True:
            time.sleep(1)
            try:
                #防止偶尔 comwechat 启动落后
                if self.bot.run(main_thread = False) is not None:
                    break
            except Exception as e:
                self.logger.error("Start failed. Reason: %s" % e)

        t = threading.Thread(target = self.handle_file_msg)
        t.daemon = True
        t.start()

    def send_status(self, status: 'Status'):
        # TG 删消息 -> 撤回微信消息(移植自上游 a847ad3)
        if not isinstance(status, MessageRemoval):
            raise EFBOperationNotSupported()

        message = status.message
        references = list(dict.fromkeys(load_message_ids(message.uid)))
        chat_uid = str(message.chat.uid)
        if not references:
            raise EFBMessageError("撤回消息缺少有效的消息 ID")

        failures = []
        for server_msgid in references:
            if not server_msgid.isdecimal():
                raise EFBMessageError(f"无效的消息 ID: {server_msgid}")

            self.revoke_message_ids[server_msgid] = True
            try:
                response = self.bot.RevokeMessage(
                    wxid=chat_uid,
                    msgid=server_msgid,
                )
            except Exception as exc:
                self.revoke_message_ids.pop(server_msgid, None)
                failures.append(str(exc))
                continue

            reason = self._revoke_failure_reason(response)
            if reason is not None:
                self.revoke_message_ids.pop(server_msgid, None)
                failures.append(reason)

        if failures:
            reason = "; ".join(failures)
            if len(failures) == len(references):
                raise EFBMessageError(f"消息撤回失败：{reason}")
            raise EFBMessageError(f"部分消息撤回失败：{reason}")

    @staticmethod
    def _revoke_failure_reason(response: Any) -> Optional[str]:
        if isinstance(response, dict) and response.get("result") == "OK" and "msg" not in response:
            return "上游不支持撤回消息"
        if not isinstance(response, dict) or str(response.get("msg")) != "1":
            return response.get("err_msg") if isinstance(response, dict) else response
        return None
    def stop_polling(self):
        self.db.stop_worker()
        for pool in (getattr(self, "_inbound_executor", None), getattr(self, "_deliver_executor", None)):
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass

    def get_message_by_id(self, chat: 'Chat', msg_id: MessageID) -> Optional['Message']:
        ...

    def get_name_by_wxid(self, wxid):
        try:
            name = self.contacts[wxid]
            if name == "":
                name = wxid
        except:
            data = self.bot.GetContactBySql(wxid = wxid)
            if data:
                name = data[3]
                if name == "":
                    name = wxid
                else:
                    self.contacts[wxid] = name
            else:
                name = wxid
        return name

    @staticmethod
    def non_blocking_lock_wrapper(lock: threading.Lock) :
        def wrapper(func):
            def inner(*args, **kwargs):
                if not lock.acquire(False):
                    return
                try:
                    return func(*args, **kwargs)
                finally:
                    lock.release()
            return inner
        return wrapper

    @non_blocking_lock_wrapper(contact_update_lock)
    def get_me(self):
        self.me = self.bot.GetSelfInfo()["data"]
        self.wxid = self.me["wxId"]

    def get_nickname_by_wxid(self, wxid):
        try:
            nickname = self.nicknames[wxid]
            if nickname == "":
                nickname = wxid
        except:
            data = self.bot.GetContactBySql(wxid = wxid)
            if data:
                nickname = data[3]
                if nickname == "":
                    nickname = wxid
                else:
                    self.nicknames[wxid] = nickname
            else:
                nickname = wxid
        return nickname

    #定时更新 Start
    @non_blocking_lock_wrapper(contact_update_lock)
    def GetContactListBySql(self):
        new_chats = []
        modified_chats = []
        contacts = self.bot.GetContactListBySql()
        for contact in contacts:
            data = contacts[contact]
            name = (f"{data['remark']}({data['nickname']})") if data["remark"] else data["nickname"]

            self.contacts[contact] = name
            self.nicknames[contact] = data["nickname"]
            if data["type"] == 0 or data["type"] == 4:
                continue

            if "@chatroom" in contact:
                new_entity = EFBGroupChat(
                    uid=contact,
                    name=name
                )
                try:
                    self.get_chat(contact)
                    modified_chats.append(contact)
                except EFBChatNotFound:
                    self.groups.append(ChatMgr.build_efb_chat_as_group(new_entity))
                    new_chats.append(contact)
            else:
                new_entity = EFBPrivateChat(
                    uid=contact,
                    name=name
                )
                try:
                    self.get_chat(contact)
                    modified_chats.append(contact)
                except EFBChatNotFound:
                    self.friends.append(ChatMgr.build_efb_chat_as_private(new_entity))
                    new_chats.append(contact)

        if new_chats or modified_chats:
            coordinator.send_status(ChatUpdates(channel=self, new_chats=new_chats, modified_chats=modified_chats))

    def load(self):
        rows = self.db.get_all_group_aliases()
        for r in rows:
            self.group_members[r.group_uid] = self.group_members.get(r.group_uid, {})
            self.group_members[r.group_uid][r.wxid] = r.group_alias

    def merge_group_members(self, group, new_members):
        self.group_members[group] = self.group_members.get(group, {})
        for wxid, alias in new_members.items():
            if self.group_members[group].get(wxid, None) != alias:
                self.group_members[group][wxid] = alias
                self.db.update_group_alias(group, wxid, alias)

    @non_blocking_lock_wrapper(group_update_lock)
    def GetGroupListBySql(self):
        groups = self.bot.GetAllGroupMembersBySql()
        for group, members in groups.items():
            self.merge_group_members(group, members)

    def extract_alias(self, msg):
        sender = msg["sender"]
        extracted = False
        if "<refermsg>" in msg["message"]:
            xml = etree.fromstring(msg["message"])
            id = xml.xpath('string(/msg/appmsg/refermsg/chatusr)')
            alias = xml.xpath('string(/msg/appmsg/refermsg/displayname)')
            name = self.get_nickname_by_wxid(id)
            if alias and alias != name:
                extracted = True
                self.merge_group_members(sender, {
                    id: alias
                })

        if not extracted and "<atuserlist>" in msg["extrainfo"]:
            xml = etree.fromstring(msg["extrainfo"])
            at_user = xml.xpath('string(/msgsource/atuserlist)')
            user_list = [user for user in at_user.split(",") if user]
            if len(user_list) == 1:
                try:
                    name = self.get_nickname_by_wxid(user_list[0])
                    alias = re.search("^@(.*)\u2005", msg["message"]).group(1)
                    if alias != name:
                        self.merge_group_members(sender, {
                            user_list[0]: alias
                        })
                except:
                    print_exc()
    #定时更新 End
    
    def _detect_wsl(self) -> bool:
        """检测是否在WSL环境中运行"""
        try:
            # 检查/proc/version文件是否包含WSL标识
            if os.path.exists('/proc/version'):
                with open('/proc/version', 'r') as f:
                    version_info = f.read().lower()
                    return 'microsoft' in version_info or 'wsl' in version_info
            return False
        except:
            return False
    
    def _wsl_to_windows_path(self, wsl_path: str) -> str:
        """将WSL路径转换为Windows路径"""
        if not self.is_wsl:
            return wsl_path
            
        try:
            # 处理 /mnt/c/ 格式的路径
            if wsl_path.startswith('/mnt/'):
                # /mnt/c/Users/... -> C:\Users\...
                parts = wsl_path.split('/', 3)
                if len(parts) >= 3:
                    drive_letter = parts[2].upper()
                    if len(parts) > 3:
                        path_part = parts[3].replace('/', '\\')
                        return f"{drive_letter}:\\{path_part}"
                    else:
                        return f"{drive_letter}:\\"
            
            # 如果不是/mnt/格式，尝试使用wslpath命令转换
            import subprocess
            result = subprocess.run(['wslpath', '-w', wsl_path], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            self.logger.warning(f"WSL路径转换失败: {wsl_path}, 错误: {e}")
        
        # 转换失败时返回原路径
        return wsl_path

class EmptyJsonResponse:
    def json(self):
        return {}
