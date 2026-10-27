import logging
import unittest

from efb_wechat_comwechat_slave.ComWechat import ComWeChatChannel


def _channel():
    ch = ComWeChatChannel.__new__(ComWeChatChannel)
    ch.logger = logging.getLogger("test_extract_alias")
    ch.get_nickname_by_wxid = lambda wxid: "原名"
    merged = {}
    ch.merge_group_members = lambda sender, mapping: merged.update(mapping)
    ch._merged = merged
    return ch


class ExtractAliasTest(unittest.TestCase):
    def test_at_without_prefix_is_silent_not_noisy(self):
        ch = _channel()
        msg = {"sender": "room@chatroom",
               "message": "这是一条没有 at 前缀的普通消息",
               "extrainfo": "<msgsource><atuserlist>wxid_someone</atuserlist></msgsource>"}
        ch.extract_alias(msg)  # must not raise, must not merge
        self.assertEqual(ch._merged, {})

    def test_at_with_prefix_merges_alias(self):
        ch = _channel()
        msg = {"sender": "room@chatroom",
               "message": "@别名\u2005大家好",
               "extrainfo": "<msgsource><atuserlist>wxid_someone</atuserlist></msgsource>"}
        ch.extract_alias(msg)
        self.assertEqual(ch._merged, {"wxid_someone": "别名"})

    def test_refermsg_still_extracts(self):
        ch = _channel()
        msg = {"sender": "room@chatroom",
               "message": "<msg><appmsg><refermsg><chatusr>wxid_x</chatusr>"
                          "<displayname>引用者</displayname></refermsg>"
                          "</appmsg></msg>",
               "extrainfo": ""}
        ch.extract_alias(msg)
        self.assertEqual(ch._merged, {"wxid_x": "引用者"})

    def test_malformed_refermsg_does_not_break_delivery(self):
        ch = _channel()
        msg = {"sender": "room@chatroom",
               "message": "<msg><appmsg><refermsg><chatusr>未闭合",
               "extrainfo": ""}
        ch.extract_alias(msg)  # must not raise
        self.assertEqual(ch._merged, {})


if __name__ == "__main__":
    unittest.main()
