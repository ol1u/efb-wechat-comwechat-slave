import unittest

from ehforwarderbot import MsgType

from efb_wechat_comwechat_slave.MsgDeco import efb_share_link_wrapper


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


if __name__ == "__main__":
    unittest.main()
