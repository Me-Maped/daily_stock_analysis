# -*- coding: utf-8 -*-
"""Tests for the link crawler news service (offline, mocked requests)."""

from __future__ import annotations

import socket
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import requests

from src.config import Config
from src.services.link_crawler_service import LinkCrawlerService

# 通用 HTML 页面：含有效新闻链接、相对路径、重复链接与导航/功能垃圾链接
HTML_FIXTURE = """<html><body>
<a href="https://news.example.com/a">央行宣布人工智能板块持续走强</a>
<a href="https://news.example.com/b">港股科技板块今日集体走强</a>
<a href="https://news.example.com/a">央行宣布人工智能板块持续走强</a>
<a href="/relative">相对路径新闻链接测试</a>
<a href="https://news.example.com/c">首页</a>
<a href="https://news.example.com/d">登录</a>
<a href="javascript:void(0)">坏链接应被过滤</a>
<a href="mailto:x@example.com">邮件链接应被过滤</a>
</body></html>"""

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>政策支持提振人工智能产业链</title><link>https://news.example.com/rss-a</link><description>摘要一。</description><pubDate>{date}</pubDate></item>
<item><title>第二则市场快讯</title><link>https://news.example.com/rss-b</link><description>摘要二。</description></item>
</channel></rss>"""


class _FakeResponse:
    """最小 requests.Response 替身：url / status_code / iter_content / content / close。"""

    def __init__(self, url: str, content: bytes):
        self.url = url
        self.status_code = 200
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 8192):
        yield self._content

    @property
    def content(self) -> bytes:
        return self._content

    def close(self) -> None:
        return None


class _FakeSession:
    """requests.Session 替身：按 URL 返回内容，或对指定 URL 抛异常。"""

    def __init__(self, routes, raise_urls=None):
        self.routes = routes
        self.raise_urls = set(raise_urls or [])
        self.max_redirects = 5

    def get(self, url, **kwargs):
        if url in self.raise_urls:
            raise requests.RequestException("boom")
        return _FakeResponse(url, self.routes[url])

    def close(self) -> None:
        return None


class LinkCrawlerServiceTestCase(unittest.TestCase):
    """共享工具：离线 DNS、构造服务与 fake session。"""

    def setUp(self) -> None:
        # 让 _validate_url 的 DNS 解析离线化：任意 hostname 解析为公网 IP
        self._dns_patcher = patch(
            "src.services.link_crawler_service.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            ],
        )
        self._dns_patcher.start()
        self.addCleanup(self._dns_patcher.stop)

    def _make_service(self, **overrides) -> LinkCrawlerService:
        defaults = {
            "link_crawl_sources": ["测试源|cn|https://news.example.com/a"],
            "link_crawl_timeout_sec": 5.0,
            "link_crawl_max_items_per_source": 20,
            "link_crawl_max_age_hours": 48,
            "link_crawl_enabled": True,
        }
        defaults.update(overrides)
        return LinkCrawlerService(config=Config(**defaults), db=None)

    def _patch(self, routes, raise_urls=None):
        fake = _FakeSession(routes, raise_urls)
        return patch(
            "src.services.link_crawler_service.requests.Session",
            return_value=fake,
        )


class ParseSourceTest(LinkCrawlerServiceTestCase):
    def test_parse_source_three_part(self) -> None:
        service = self._make_service()
        source = service._parse_source("财联社|cn|https://www.cls.cn/telegraph")
        self.assertIsNotNone(source)
        self.assertEqual(source.label, "财联社")
        self.assertEqual(source.market, "cn")
        self.assertEqual(source.url, "https://www.cls.cn/telegraph")

    def test_parse_source_two_part_default_global(self) -> None:
        service = self._make_service()
        source = service._parse_source("快讯|https://news.example.com/live")
        self.assertIsNotNone(source)
        self.assertEqual(source.label, "快讯")
        self.assertEqual(source.market, "global")
        self.assertEqual(source.url, "https://news.example.com/live")

    def test_parse_source_one_part_hostname_label(self) -> None:
        service = self._make_service()
        source = service._parse_source("https://news.example.com/feed")
        self.assertIsNotNone(source)
        self.assertEqual(source.label, "news.example.com")
        self.assertEqual(source.market, "global")
        self.assertEqual(source.url, "https://news.example.com/feed")

    def test_parse_source_malformed_skipped(self) -> None:
        service = self._make_service()
        self.assertIsNone(service._parse_source(""))
        self.assertIsNone(service._parse_source("https://news.example.com|"))
        self.assertIsNone(service._parse_source("a|b|c|d"))
        self.assertIsNone(service._parse_source("|cn|https://news.example.com"))

    def test_parse_source_invalid_market_defaults_global(self) -> None:
        service = self._make_service()
        source = service._parse_source("标签|xx|https://news.example.com/x")
        self.assertIsNotNone(source)
        self.assertEqual(source.market, "global")

    def test_parse_source_ssrf_invalid_skipped(self) -> None:
        service = self._make_service()
        self.assertIsNone(service._parse_source("标签|cn|http://127.0.0.1:8080/x"))


class ApplicableSourcesTest(LinkCrawlerServiceTestCase):
    def test_global_matches_any_market(self) -> None:
        service = self._make_service(
            link_crawl_sources=[
                "全球源|global|https://news.example.com/g",
                "A股源|cn|https://news.example.com/cn",
            ]
        )
        cn_sources = service._applicable_sources("cn")
        self.assertEqual({s.label for s in cn_sources}, {"全球源", "A股源"})
        us_sources = service._applicable_sources("us")
        self.assertEqual({s.label for s in us_sources}, {"全球源"})

    def test_cn_only_matches_cn(self) -> None:
        service = self._make_service(
            link_crawl_sources=["A股源|cn|https://news.example.com/cn"]
        )
        self.assertEqual(len(service._applicable_sources("cn")), 1)
        self.assertEqual(len(service._applicable_sources("us")), 0)


class ExtractArticlesTest(LinkCrawlerServiceTestCase):
    def test_extract_articles_filters_nav_and_returns_valid_links(self) -> None:
        service = self._make_service()
        items = service._extract_articles(HTML_FIXTURE, base_url="https://news.example.com/")
        urls = [i["url"] for i in items]
        titles = [i["title"] for i in items]
        # 有效链接（含相对路径归一化）
        self.assertIn("https://news.example.com/a", urls)
        self.assertIn("https://news.example.com/b", urls)
        self.assertIn("https://news.example.com/relative", urls)
        # 导航/功能垃圾被过滤
        self.assertNotIn("首页", titles)
        self.assertNotIn("登录", titles)
        # 非法链接被过滤
        for u in urls:
            self.assertTrue(u.startswith("http"))
        self.assertNotIn("javascript:", urls)
        self.assertNotIn("mailto:", urls)


class ParseFeedTest(LinkCrawlerServiceTestCase):
    def test_parse_feed_rss(self) -> None:
        service = self._make_service()
        content = RSS_FIXTURE.format(date=format_datetime(datetime.now(timezone.utc)))
        entries = service._parse_feed(content.encode("utf-8"))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["title"], "政策支持提振人工智能产业链")
        self.assertEqual(entries[0]["url"], "https://news.example.com/rss-a")
        self.assertIsNotNone(entries[0]["date"])


class ValidateUrlTest(LinkCrawlerServiceTestCase):
    def test_validate_url_ssrf_raises(self) -> None:
        service = self._make_service()
        for bad in [
            "file:///etc/passwd",
            "http://127.0.0.1:8080/x",
            "http://localhost/x",
            "http://192.168.1.1/x",
            "http://10.0.0.1/x",
        ]:
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    service._validate_url(bad)

    def test_validate_url_accepts_public_https(self) -> None:
        service = self._make_service()
        service._validate_url("https://news.example.com/a")  # 不应抛异常


class FetchContextTest(LinkCrawlerServiceTestCase):
    def test_fetch_context_end_to_end_formatted(self) -> None:
        service = self._make_service(
            link_crawl_sources=["测试源|cn|https://news.example.com/a"]
        )
        routes = {"https://news.example.com/a": HTML_FIXTURE.encode("utf-8")}
        with self._patch(routes):
            text = service.fetch_context(
                code="600519", stock_name="贵州茅台", market="cn"
            )
        self.assertIsNotNone(text)
        self.assertIn("## 🔗 爬虫新闻源（测试源）", text)
        self.assertIn("央行宣布人工智能板块持续走强", text)
        self.assertIn("https://news.example.com/a", text)

    def test_fetch_context_respects_max_items_per_source(self) -> None:
        service = self._make_service(
            link_crawl_sources=["测试源|cn|https://news.example.com/a"],
            link_crawl_max_items_per_source=2,
        )
        routes = {"https://news.example.com/a": HTML_FIXTURE.encode("utf-8")}
        with self._patch(routes):
            text = service.fetch_context(
                code="600519", stock_name="贵州茅台", market="cn"
            )
        self.assertIsNotNone(text)
        # 每行一条 "- title url"，最多 2 条
        self.assertEqual(text.count("\n- "), 2)

    def test_fetch_context_respects_max_age_hours(self) -> None:
        service = self._make_service(
            link_crawl_sources=["测试源|cn|https://news.example.com/a"],
            link_crawl_max_age_hours=1,
        )
        old_date = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))
        content = RSS_FIXTURE.format(date=old_date).encode("utf-8")
        routes = {"https://news.example.com/a": content}
        with self._patch(routes):
            text = service.fetch_context(
                code="600519", stock_name="贵州茅台", market="cn"
            )
        # 第一条超龄被过滤；第二条无日期不拦截 → 仍返回第二条
        self.assertIsNotNone(text)
        self.assertIn("第二则市场快讯", text)
        self.assertNotIn("政策支持提振人工智能产业链", text)

    def test_fetch_context_returns_none_when_no_items(self) -> None:
        service = self._make_service(
            link_crawl_sources=["测试源|cn|https://news.example.com/a"]
        )
        empty_html = "<html><body><a href='https://news.example.com/x'>首页</a></body></html>"
        routes = {"https://news.example.com/a": empty_html.encode("utf-8")}
        with self._patch(routes):
            text = service.fetch_context(
                code="600519", stock_name="贵州茅台", market="cn"
            )
        self.assertIsNone(text)

    def test_fetch_context_dedupes_same_url(self) -> None:
        service = self._make_service(
            link_crawl_sources=["测试源|cn|https://news.example.com/a"]
        )
        routes = {"https://news.example.com/a": HTML_FIXTURE.encode("utf-8")}
        with self._patch(routes):
            text = service.fetch_context(
                code="600519", stock_name="贵州茅台", market="cn"
            )
        self.assertIsNotNone(text)
        # 重复 URL 只出现一次
        self.assertEqual(text.count("https://news.example.com/a"), 1)

    def test_fetch_context_fail_open_one_source_raises(self) -> None:
        service = self._make_service(
            link_crawl_sources=[
                "坏源|cn|https://news.example.com/bad",
                "好源|cn|https://news.example.com/good",
            ]
        )
        routes = {"https://news.example.com/good": HTML_FIXTURE.encode("utf-8")}
        with self._patch(routes, raise_urls=["https://news.example.com/bad"]):
            text = service.fetch_context(
                code="600519", stock_name="贵州茅台", market="cn"
            )
        # 坏源失败不阻断，好源仍贡献
        self.assertIsNotNone(text)
        self.assertIn("## 🔗 爬虫新闻源（好源）", text)
        self.assertNotIn("坏源", text)


if __name__ == "__main__":
    unittest.main()
