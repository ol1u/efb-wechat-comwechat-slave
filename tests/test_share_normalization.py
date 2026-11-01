import logging
import tempfile
import unittest
from unittest.mock import patch

from ehforwarderbot import MsgType
from lxml import etree

from efb_wechat_comwechat_slave.ComWechat import (
    SHARE_THUMBNAIL_MAX_BYTES,
    SHARE_THUMBNAIL_TIMEOUT,
    ComWeChatChannel,
)
from efb_wechat_comwechat_slave.MsgDeco import (
    ShareCard,
    _build_share_message,
    efb_share_link_wrapper,
)
from efb_wechat_comwechat_slave.MsgProcess import MsgProcess, MsgWrapper
from efb_wechat_comwechat_slave.Utils import download_file


def _wrap(xml, resolver=None):
    return efb_share_link_wrapper(
        {"message": xml},
        None,
        thumbnail_resolver=resolver,
    )


def _share_xml(
    appmsg_type,
    *,
    title="标题",
    description="描述",
    url="https://example.com/item",
    source="来源",
    extra="",
):
    return (
        "<msg><appmsg>"
        f"<title>{title}</title><des>{description}</des>"
        f"<url>{url}</url><type>{appmsg_type}</type>{extra}"
        "</appmsg>"
        f"<appinfo><appname>{source}</appname></appinfo></msg>"
    )


TYPE1_TITLE_ONLY = """<?xml version="1.0"?>
<msg><appmsg><title>标题</title><type>1</type></appmsg>
<appinfo><appname>哔哩哔哩</appname></appinfo></msg>"""

TYPE1_WITH_THUMB = """<msg><appmsg>
<title>A &amp; B</title><des>描述</des><type>1</type>
<url>https://example.com/a?x=1&amp;y=2</url>
<thumburl>https://example.com/thumb?x=1&amp;y=2</thumburl>
</appmsg><appinfo><appname>来源</appname></appinfo></msg>"""

TYPE51_NO_URL = """<msg><appmsg><type>51</type><title>视频</title>
<finderFeed><nickname>作者</nickname><desc>文案</desc>
<avatar>https://example.com/avatar.jpg</avatar></finderFeed></appmsg></msg>"""

TYPE51_NESTED_COVER = """<msg><appmsg><type>51</type><title>视频</title>
<url>https://example.com/video</url><finderFeed><nickname>作者</nickname>
<desc>文案</desc><mediaList><media>
<thumbUrl>https://example.com/nested-thumb.jpg</thumbUrl>
</media></mediaList></finderFeed></appmsg></msg>"""

TYPE63_WITH_URL = """<msg><appmsg><type>63</type><title>直播</title>
<url>https://example.com/live</url><finderLive><desc>直播文案</desc>
<headUrl>https://example.com/head.jpg</headUrl></finderLive></appmsg></msg>"""

UNKNOWN_SHARE = """<msg><appmsg><type>999</type><title>未知分享</title>
<des>描述</des><url>https://example.com/unknown</url>
<thumburl>https://example.com/thumb.jpg</thumburl></appmsg>
<appinfo><appname>应用</appname></appinfo></msg>"""

EXPECTED_TYPE_NAMES = {
    1: "网页/应用分享",
    2: "其他分享",
    3: "音乐分享",
    4: "小红书等内容分享",
    5: "公众号文章",
    33: "小程序/服务分享",
    36: "应用活动分享",
    51: "视频号视频",
    63: "视频号直播",
    999: "其他分享",
}


class ShareNormalizationTest(unittest.TestCase):
    def test_type1_missing_fields_are_explicit(self):
        message = _wrap(TYPE1_TITLE_ONLY)

        self.assertEqual(message.type, MsgType.Text)
        self.assertEqual(
            message.text,
            "微信分享消息（type=1 · 网页/应用分享）\n"
            "标题：标题\n"
            "描述：无\n"
            "来源：哔哩哔哩\n"
            "缩略图：无",
        )

    def test_type1_keeps_link_and_thumbnail(self):
        message = _wrap(TYPE1_WITH_THUMB)

        self.assertEqual(message.type, MsgType.Link)
        self.assertEqual(message.attributes.url, "https://example.com/a?x=1&y=2")
        self.assertEqual(message.attributes.title, "打开原链接")
        self.assertEqual(
            message.attributes.image,
            "https://example.com/thumb?x=1&y=2",
        )
        self.assertIn("标题：A & B", message.text)
        self.assertNotIn("https://example.com/a", message.text)
        self.assertIn("缩略图：有", message.text)

    def test_common_share_types_share_one_output_shape(self):
        for appmsg_type in (1, 2, 3, 4, 33, 36, 51, 63, 999):
            with self.subTest(appmsg_type=appmsg_type):
                message = _wrap(_share_xml(appmsg_type))

                self.assertEqual(message.type, MsgType.Link)
                self.assertTrue(
                    message.text.startswith(
                        "微信分享消息（type={} · {}）".format(
                            appmsg_type,
                            EXPECTED_TYPE_NAMES[appmsg_type],
                        )
                    )
                )
                self.assertNotIn("<msg", message.text)
                self.assertNotIn("链接：https://", message.text)
                self.assertIn("标题：标题", message.text)
                self.assertIn("描述：描述", message.text)
                self.assertIn("来源：来源", message.text)

    def test_type5_showtype_zero_is_normalized(self):
        message = _wrap(
            _share_xml(5, extra="<showtype>0</showtype>")
        )

        self.assertEqual(message.type, MsgType.Link)
        self.assertTrue(
            message.text.startswith(
                "微信分享消息（type=5 · 公众号文章）"
            )
        )
        self.assertEqual(message.vendor_specific["is_mp"], True)

    def test_finder_feed_without_url_keeps_author(self):
        message = _wrap(TYPE51_NO_URL)

        self.assertEqual(message.type, MsgType.Text)
        self.assertIn("作者：作者", message.text)
        self.assertIn("缩略图：有", message.text)

    def test_nested_finder_cover_is_detected_case_insensitively(self):
        message = _wrap(TYPE51_NESTED_COVER)

        self.assertEqual(message.type, MsgType.Link)
        self.assertEqual(
            message.attributes.image,
            "https://example.com/nested-thumb.jpg",
        )
        self.assertIn("缩略图：有", message.text)

    def test_finder_live_with_url_keeps_cover(self):
        message = _wrap(TYPE63_WITH_URL)

        self.assertEqual(message.type, MsgType.Link)
        self.assertEqual(message.attributes.url, "https://example.com/live")
        self.assertEqual(message.attributes.image, "https://example.com/head.jpg")
        self.assertIn(
            "微信分享消息（type=63 · 视频号直播）",
            message.text,
        )

    def test_unknown_type_uses_normalized_fallback(self):
        message = _wrap(UNKNOWN_SHARE)

        self.assertEqual(message.type, MsgType.Link)
        self.assertEqual(message.attributes.url, "https://example.com/unknown")
        self.assertTrue(
            message.text.startswith(
                "微信分享消息（type=999 · 其他分享）"
            )
        )

    def test_malformed_xml_returns_safe_text(self):
        with self.assertLogs("comwechat", level="WARNING"):
            message = _wrap("<msg><appmsg><title>broken")

        self.assertEqual(message.type, MsgType.Text)
        self.assertIn("分享消息解析失败", message.text)

    def test_type8_keeps_existing_unsupported_behavior(self):
        message = _wrap("<msg><appmsg><type>8</type></appmsg></msg>")

        self.assertEqual(message.type, MsgType.Unsupported)
        self.assertIn("表情", message.text)

    def test_msgprocess_passes_share_thumbnail_resolver(self):
        resolver = object()
        with patch(
            "efb_wechat_comwechat_slave.MsgProcess.efb_share_link_wrapper"
        ) as wrapper:
            MsgProcess(
                {
                    "type": "share",
                    "message": TYPE51_NO_URL,
                    "filepath": "",
                },
                None,
                share_thumbnail_resolver=resolver,
            )

        self.assertIs(
            wrapper.call_args.kwargs["thumbnail_resolver"],
            resolver,
        )

    def test_thumbnail_resolver_returns_image_with_caption(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as thumbnail:
            thumbnail.write(b"\x89PNG\r\n\x1a\nfake")
            thumbnail.flush()
            card_xml = etree.fromstring(TYPE51_NO_URL)
            captured = []

            def resolver(_message, card):
                captured.append(card)
                thumbnail.seek(0)
                return thumbnail

            with patch(
                "efb_wechat_comwechat_slave.MsgDeco.magic.from_file",
                return_value="image/png",
            ):
                message = _build_share_message(
                    card_xml,
                    {"message": TYPE51_NO_URL},
                    51,
                    thumbnail_resolver=resolver,
                )

        self.assertEqual(message.type, MsgType.Image)
        self.assertIn(
            "微信分享消息（type=51 · 视频号视频）",
            message.text,
        )
        self.assertIn("作者：作者", message.text)
        self.assertIn("缩略图：有", message.text)
        self.assertTrue(message.path)
        self.assertEqual(captured[0].author, "作者")

    def test_link_with_thumbnail_uses_one_image_with_clickable_caption(self):
        xml = _share_xml(1)
        with tempfile.NamedTemporaryFile(suffix=".png") as thumbnail:
            thumbnail.write(b"\x89PNG\r\n\x1a\nfake")
            thumbnail.flush()

            with patch(
                "efb_wechat_comwechat_slave.MsgDeco.magic.from_file",
                return_value="image/png",
            ):
                messages = _build_share_message(
                    etree.fromstring(xml),
                    {"message": xml},
                    1,
                    thumbnail_resolver=lambda _message, _card: thumbnail,
                )

        self.assertEqual(messages.type, MsgType.Image)
        self.assertIn("🔗 打开原链接", messages.text)
        self.assertNotIn("https://example.com/item", messages.text)
        self.assertEqual(
            messages.vendor_specific["html_link"],
            {
                "text": "打开原链接",
                "url": "https://example.com/item",
            },
        )
        self.assertIn("缩略图：有", messages.text)
        self.assertTrue(messages.path)
        wrapped = MsgWrapper({"message": xml}, messages)[0]
        self.assertEqual(
            wrapped.vendor_specific["html_link"],
            {
                "text": "打开原链接",
                "url": "https://example.com/item",
            },
        )

    def test_long_text_with_thumbnail_still_splits_messages(self):
        xml = """<msg><appmsg><type>51</type>
<title>{}</title><finderFeed><nickname>{}</nickname><desc>{}</desc>
<avatar>https://example.com/avatar.jpg</avatar></finderFeed></appmsg></msg>""".format(
            "T" * 300,
            "A" * 100,
            "D" * 600,
        )
        with tempfile.NamedTemporaryFile(suffix=".png") as thumbnail:
            thumbnail.write(b"\x89PNG\r\n\x1a\nfake")
            thumbnail.flush()

            with patch(
                "efb_wechat_comwechat_slave.MsgDeco.magic.from_file",
                return_value="image/png",
            ), patch(
                "efb_wechat_comwechat_slave.MsgDeco._SHARE_CAPTION_LIMIT",
                100,
            ):
                messages = _build_share_message(
                    etree.fromstring(xml),
                    {"message": xml},
                    51,
                    thumbnail_resolver=lambda _message, _card: thumbnail,
                )

        self.assertIsInstance(messages, list)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].type, MsgType.Text)
        self.assertEqual(messages[1].type, MsgType.Image)

    def test_long_link_caption_stays_single_image(self):
        xml = _share_xml(1, description="D" * 600)
        with tempfile.NamedTemporaryFile(suffix=".png") as thumbnail:
            thumbnail.write(b"\x89PNG\r\n\x1a\nfake")
            thumbnail.flush()

            with patch(
                "efb_wechat_comwechat_slave.MsgDeco.magic.from_file",
                return_value="image/png",
            ), patch(
                "efb_wechat_comwechat_slave.MsgDeco._SHARE_CAPTION_LIMIT",
                120,
            ):
                message = _build_share_message(
                    etree.fromstring(xml),
                    {"message": xml},
                    1,
                    thumbnail_resolver=lambda _message, _card: thumbnail,
                )

        self.assertEqual(message.type, MsgType.Image)
        self.assertLessEqual(len(message.text), 120)
        self.assertIn("🔗 打开原链接", message.text)
        self.assertTrue(message.text.endswith("..."))
        self.assertEqual(
            message.vendor_specific["html_link"]["url"],
            "https://example.com/item",
        )


class ShareThumbnailResolverTest(unittest.TestCase):
    def setUp(self):
        self.channel = object.__new__(ComWeChatChannel)
        self.channel.base_path = None
        self.channel.dir = None
        self.channel.is_wsl = False
        self.channel.logger = logging.getLogger("test-share-thumbnail")

    def test_local_thumbnail_wins_over_remote_candidate(self):
        with tempfile.NamedTemporaryFile() as local_source:
            local_source.write(b"local thumbnail")
            local_source.flush()
            with tempfile.NamedTemporaryFile() as decoded:
                with patch(
                    "efb_wechat_comwechat_slave.ComWechat.wechatimagedecode",
                    return_value=decoded,
                ), patch(
                    "efb_wechat_comwechat_slave.ComWechat."
                    "is_supported_share_thumbnail",
                    return_value=True,
                ), patch(
                    "efb_wechat_comwechat_slave.ComWechat.download_file"
                ) as download:
                    result = self.channel._resolve_share_thumbnail(
                        {"thumb_path": local_source.name},
                        ShareCard(
                            appmsg_type=1,
                            thumbnail_urls=("https://example.com/remote.jpg",),
                        ),
                    )

        self.assertIs(result, decoded)
        download.assert_not_called()

    def test_remote_thumbnail_uses_size_and_timeout_limits(self):
        with tempfile.NamedTemporaryFile() as downloaded:
            card = ShareCard(
                appmsg_type=1,
                thumbnail_urls=("https://example.com/thumb.jpg",),
            )
            with patch(
                "efb_wechat_comwechat_slave.ComWechat.download_file",
                return_value=downloaded,
            ) as download, patch(
                "efb_wechat_comwechat_slave.ComWechat."
                "is_supported_share_thumbnail",
                return_value=True,
            ):
                result = self.channel._resolve_share_thumbnail({}, card)

        self.assertIs(result, downloaded)
        download.assert_called_once_with(
            "https://example.com/thumb.jpg",
            retry=1,
            retry_interval=0,
            timeout=SHARE_THUMBNAIL_TIMEOUT,
            max_bytes=SHARE_THUMBNAIL_MAX_BYTES,
        )

    def test_oversized_local_candidate_is_skipped(self):
        with tempfile.NamedTemporaryFile() as local_source:
            local_source.truncate(SHARE_THUMBNAIL_MAX_BYTES + 1)
            with patch(
                "efb_wechat_comwechat_slave.ComWechat.wechatimagedecode"
            ) as decode:
                result = self.channel._resolve_share_thumbnail(
                    {"thumb_path": local_source.name},
                    ShareCard(appmsg_type=1),
                )

        self.assertIsNone(result)
        decode.assert_not_called()

    def test_remote_attempts_are_bounded(self):
        card = ShareCard(
            appmsg_type=1,
            thumbnail_urls=(
                "https://example.com/1.jpg",
                "https://example.com/2.jpg",
                "https://example.com/3.jpg",
                "https://example.com/4.jpg",
            ),
        )
        with patch(
            "efb_wechat_comwechat_slave.ComWechat.download_file",
            return_value=None,
        ) as download:
            result = self.channel._resolve_share_thumbnail({}, card)

        self.assertIsNone(result)
        self.assertEqual(download.call_count, 3)


class _FakeResponse:
    def __init__(self, chunks, *, content_length=None):
        self.chunks = chunks
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        yield from self.chunks


class ShareThumbnailDownloadTest(unittest.TestCase):
    def test_max_bytes_allows_download_within_limit(self):
        response = _FakeResponse([b"abc", b"de"], content_length=5)
        with patch(
            "efb_wechat_comwechat_slave.Utils.requests.get",
            return_value=response,
        ):
            downloaded = download_file(
                "https://example.com/thumb.jpg",
                retry=1,
                max_bytes=5,
            )

        try:
            self.assertEqual(downloaded.read(), b"abcde")
        finally:
            downloaded.close()
        self.assertTrue(response.closed)

    def test_max_bytes_stops_streaming_download(self):
        response = _FakeResponse([b"abc", b"def"])
        with patch(
            "efb_wechat_comwechat_slave.Utils.requests.get",
            return_value=response,
        ), self.assertLogs(
            "efb_wechat_comwechat_slave.Utils",
            level="WARNING",
        ):
            with self.assertRaisesRegex(ValueError, "exceeds 5 bytes"):
                download_file(
                    "https://example.com/thumb.jpg",
                    retry=1,
                    max_bytes=5,
                )

        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
