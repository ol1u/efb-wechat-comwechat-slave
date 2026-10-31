import os
import tempfile
import unittest

from ehforwarderbot import MsgType

from efb_wechat_comwechat_slave.MsgDeco import (
    _decode_forwarded_media,
    efb_forwarded_chat_wrapper,
    efb_share_link_wrapper,
)


def _xml(appmsg_inner):
    return "<msg><appmsg>" + appmsg_inner + "</appmsg></msg>"


class ChannelsShareTest(unittest.TestCase):
    def test_type51_without_url_falls_back_to_text(self):
        xml = _xml("<type>51</type><title>测试视频标题</title>"
                   "<finderFeed><nickname>作者名</nickname>"
                   "<desc>视频文案</desc>"
                   "<avatar>http://example.com/a.jpg</avatar></finderFeed>")
        msg = efb_share_link_wrapper({"message": xml}, None)
        self.assertEqual(msg.type, MsgType.Text)
        self.assertIn("测试视频标题", msg.text)
        self.assertIn("视频文案", msg.text)
        self.assertIn("作者名", msg.text)

    def test_type51_with_url_keeps_link(self):
        xml = _xml("<type>51</type><title>标题</title>"
                   "<url>http://example.com/v</url>"
                   "<finderFeed><nickname>作者</nickname>"
                   "<desc>文案</desc></finderFeed>")
        msg = efb_share_link_wrapper({"message": xml}, None)
        self.assertEqual(msg.type, MsgType.Link)
        self.assertEqual(msg.attributes.url, "http://example.com/v")

    def test_type51_with_url_but_no_desc_does_not_crash(self):
        xml = _xml("<type>51</type><title>标题</title>"
                   "<url>http://example.com/v</url><finderFeed></finderFeed>")
        msg = efb_share_link_wrapper({"message": xml}, None)
        self.assertEqual(msg.type, MsgType.Link)

    def test_type63_without_url_falls_back_to_text(self):
        xml = _xml("<type>63</type><title>直播标题</title>"
                   "<finderLive><desc>直播文案</desc>"
                   "<headUrl>http://example.com/h.jpg</headUrl></finderLive>")
        msg = efb_share_link_wrapper({"message": xml}, None)
        self.assertEqual(msg.type, MsgType.Text)
        self.assertIn("直播标题", msg.text)
        self.assertIn("直播文案", msg.text)

    def test_type63_with_url_keeps_link(self):
        xml = _xml("<type>63</type><title>直播标题</title>"
                   "<url>http://example.com/live</url>"
                   "<finderLive><desc>文案</desc>"
                   "<headUrl>http://example.com/h.jpg</headUrl></finderLive>")
        msg = efb_share_link_wrapper({"message": xml}, None)
        self.assertEqual(msg.type, MsgType.Link)
        self.assertEqual(msg.attributes.url, "http://example.com/live")


class ForwardedMediaDecodeTest(unittest.TestCase):
    def test_xor_wrapped_dat_is_decoded_and_renamed(self):
        plain = b"\xff\xd8\xff\xe0" + b"jpeg payload"
        wrapped = bytes(byte ^ 0x47 for byte in plain)
        with tempfile.NamedTemporaryFile(suffix=".dat") as source:
            source.write(wrapped)
            source.seek(0)

            decoded, filename = _decode_forwarded_media(
                source,
                "cached-file.dat",
                fallback_name="forwarded-2.jpg",
            )
            try:
                self.assertEqual(decoded.read(), plain)
                self.assertEqual(filename, "forwarded-2.jpg")
            finally:
                decoded.close()

    def test_plaintext_media_is_not_rewritten(self):
        plain = b"\x00\x00\x00\x18ftypisom" + b"video payload"
        with tempfile.NamedTemporaryFile(suffix=".dat") as source:
            source.write(plain)
            source.seek(0)

            decoded, filename = _decode_forwarded_media(
                source,
                os.path.basename(source.name),
                fallback_name="forwarded-4.mp4",
            )

            self.assertIs(decoded, source)
            self.assertEqual(decoded.read(), plain)
            self.assertEqual(filename, "forwarded-4.mp4")


class ForwardedChatHierarchyTest(unittest.TestCase):
    def test_only_nested_headers_reply_to_their_parent_header(self):
        xml = """<msg><appmsg><title>多层聊天</title><type>19</type>
        <recorditem><![CDATA[
        <recordinfo><datalist count="2">
          <dataitem datatype="1"><datadesc>顶层内容</datadesc></dataitem>
          <dataitem datatype="17" dataid="nested-1">
            <datatitle>第二层</datatitle>
            <recordxml><recordinfo><datalist count="2">
              <dataitem datatype="1"><datadesc>二层内容</datadesc></dataitem>
              <dataitem datatype="17" dataid="nested-2">
                <datatitle>第三层</datatitle>
                <recordxml><recordinfo><datalist count="1">
                  <dataitem datatype="1"><datadesc>三层内容</datadesc></dataitem>
                </datalist></recordinfo></recordxml>
              </dataitem>
            </datalist></recordinfo></recordxml>
          </dataitem>
        </datalist></recordinfo>
        ]]></recorditem></appmsg></msg>"""

        messages = efb_forwarded_chat_wrapper(xml)

        self.assertEqual(len(messages), 6)
        self.assertIsNone(messages[0].target)
        self.assertIsNone(messages[1].target)
        self.assertIs(messages[2].target, messages[0])
        self.assertIsNone(messages[3].target)
        self.assertIs(messages[4].target, messages[2])
        self.assertIsNone(messages[5].target)


if __name__ == "__main__":
    unittest.main()
