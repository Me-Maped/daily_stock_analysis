# -*- coding: utf-8 -*-
"""链接爬虫新闻服务：用户配置 URL 列表，每次个股分析时实时抓取并入 LLM 新闻上下文。

设计原则：
- 纯增量、fail-open：任何单源失败或整体失败都不影响主分析流程。
- 用户配置来源（config.link_crawl_sources），格式: 标签|市场|URL。
- 每次抓取后按 URL 去重并持久化到 crawl_news_items 表。
- 对配置 URL 做最小 SSRF 防护（仅 http/https，禁止内网/回环/链路本地地址）。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests

from src.config import Config, get_config
from src.storage import CrawlNewsItem, save_crawl_news_item

logger = logging.getLogger(__name__)

_ALLOWED_MARKETS = {"cn", "hk", "us", "global"}
_PRIVATE_HOSTNAMES = {"localhost", "localhost.localdomain"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 单页最大 2MB，超出拒绝
_MAX_REDIRECTS = 5
_DISABLE_REQUEST_PROXIES = {"http": None, "https": None}
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 通用导航/功能锚文本（小写规范化后精确匹配），避免把页脚/导航抓成新闻
_GENERIC_ANCHOR_TEXTS = {
    "首页", "下一页", "上一页", "上一页|下一页", "更多", "更多>>", "更多 »", "登录",
    "注册", "关于", "关于我们", "联系我们", "返回", "返回顶部", "全部", "详情",
    "查看详情", "阅读全文", "免责声明", "隐私政策", "使用条款", "网站地图",
    "home", "about", "about us", "contact", "contact us", "more", "next",
    "prev", "previous", "back", "top", "read more", "full story", "terms",
    "privacy", "privacy policy", "disclaimer", "sitemap", "footer", "menu",
}
# 用于在锚点父块文本中识别日期
_DATE_PATTERNS = [
    re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?"),
    re.compile(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})"),
]


@dataclass(frozen=True)
class CrawlSource:
    """解析后的单个爬虫新闻源。"""

    label: str
    market: str
    url: str


class LinkCrawlerService:
    """用户配置 URL 列表的实时新闻爬虫服务。"""

    def __init__(self, config: Optional[Config] = None, db=None):
        self.config = config or get_config()
        self.db = db  # 持久化用；可为 None（不持久化，仅返回上下文）
        self.enabled = bool(getattr(self.config, "link_crawl_enabled", True))
        self.sources_raw = list(getattr(self.config, "link_crawl_sources", None) or [])
        self.timeout_sec = float(getattr(self.config, "link_crawl_timeout_sec", 10.0) or 10.0)
        self.max_items_per_source = int(getattr(self.config, "link_crawl_max_items_per_source", 20) or 20)
        self.max_age_hours = int(getattr(self.config, "link_crawl_max_age_hours", 48) or 48)
        self.parsed_sources: List[CrawlSource] = self._parse_sources()

    @property
    def is_available(self) -> bool:
        return bool(self.enabled and self.parsed_sources)

    # ---------- 源解析 ----------

    def _parse_sources(self) -> List[CrawlSource]:
        """解析配置的源列表，跳过畸形条目（记录告警）。"""
        sources: List[CrawlSource] = []
        for raw in self.sources_raw:
            source = self._parse_source(raw)
            if source is None:
                logger.warning("链接爬虫源配置无效，已跳过: %r", raw)
                continue
            sources.append(source)
        return sources

    def _parse_source(self, raw: str) -> Optional[CrawlSource]:
        """单条解析: 标签|市场|URL / 标签|URL / URL。

        - 3 段: label|market|url
        - 2 段: label|url，market 视为 global
        - 1 段: 视为裸 URL，label 取 hostname，market 视为 global
        """
        parts = [p.strip() for p in (raw or "").split("|")]
        if len(parts) == 3:
            label, market, url = parts
        elif len(parts) == 2:
            label, url = parts
            market = "global"
        elif len(parts) == 1 and parts[0]:
            url = parts[0]
            market = "global"
            try:
                label = (urlparse(url).hostname or url).strip()
            except ValueError:
                label = url
        else:
            return None

        if not url:
            return None
        if not label:
            return None
        market = (market or "global").strip().lower()
        if market not in _ALLOWED_MARKETS:
            logger.warning("链接爬虫源市场取值无效（视为 global）: %r", market)
            market = "global"
        try:
            self._validate_url(url)
        except ValueError as exc:
            logger.warning("链接爬虫源 URL 校验未通过，已跳过: %s (%s)", url, exc)
            return None
        return CrawlSource(label=label, market=market, url=url)

    # ---------- SSRF 防护 ----------

    def _validate_url(self, raw_url: str) -> None:
        """最小 SSRF 防护：仅 http/https，拒绝内网/回环/链路本地地址。"""
        parsed = urlparse(raw_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("链接爬虫源必须是完整的 http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("链接爬虫源 URL 不得包含账号密码")
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if not hostname:
            raise ValueError("链接爬虫源缺少主机名")
        if hostname in _PRIVATE_HOSTNAMES or hostname.endswith(".local"):
            raise ValueError("链接爬虫源主机不允许（本地地址）")
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None
        if ip is not None:
            if self._is_blocked_ip(ip):
                raise ValueError("链接爬虫源不允许指向内网或本地网络地址")
            return
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except OSError as exc:
            raise ValueError(f"链接爬虫源主机 DNS 解析失败: {hostname}") from exc
        if not addr_infos:
            raise ValueError(f"链接爬虫源主机 DNS 解析失败: {hostname}")
        for info in addr_infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except (IndexError, ValueError):
                continue
            if self._is_blocked_ip(ip):
                raise ValueError("链接爬虫源不允许指向内网或本地网络地址")

    @staticmethod
    def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
        return (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )

    # ---------- 市场过滤 ----------

    def _applicable_sources(self, market: Optional[str]) -> List[CrawlSource]:
        """global 源匹配所有市场；否则仅匹配相同市场。"""
        market = (market or "global").strip().lower()
        return [
            s for s in self.parsed_sources
            if s.market == "global" or s.market == market
        ]

    # ---------- 主入口 ----------

    def fetch_context(
        self,
        code: str,
        stock_name: str,
        market: str,
        query_id: Optional[str] = None,
        db=None,
    ) -> Optional[str]:
        """为指定股票抓取适用的爬虫新闻源并格式化上下文。

        任何异常都不会向上抛出（fail-open）；零条目或整体失败返回 None。
        """
        if not self.is_available:
            return None
        try:
            db = db if db is not None else self.db
            sources = self._applicable_sources(market)
            if not sources:
                return None
            seen_urls = set(self._existing_urls(db, code))
            sections: List[str] = []
            for source in sources:
                try:
                    items = self._fetch_source(
                        source=source,
                        code=code,
                        stock_name=stock_name,
                        query_id=query_id,
                        db=db,
                        seen_urls=seen_urls,
                    )
                except Exception as exc:
                    logger.warning("链接爬虫源 %s 抓取失败（fail-open）: %s", source.label, exc)
                    continue
                if items:
                    sections.append(self._format_context(source.label, items))
            if not sections:
                return None
            return "\n\n".join(sections)
        except Exception as exc:
            logger.warning("链接爬虫新闻获取失败（fail-open）: %s", exc)
            return None

    def _fetch_source(
        self,
        source: CrawlSource,
        code: str,
        stock_name: str,
        query_id: Optional[str],
        db,
        seen_urls: set,
    ) -> List[Dict[str, Any]]:
        """抓取单个源并返回新增条目（已过滤时效、去重并尝试持久化）。"""
        self._validate_url(source.url)
        headers = {"User-Agent": _DEFAULT_USER_AGENT}
        session = requests.Session()
        response = None
        current_url = source.url
        redirect_count = 0
        try:
            while True:
                response = session.get(
                    current_url,
                    timeout=self.timeout_sec,
                    headers=headers,
                    proxies=_DISABLE_REQUEST_PROXIES,
                    stream=True,
                    allow_redirects=False,
                )
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break

                try:
                    location = (response.headers.get("Location") or "").strip()
                    if not location:
                        raise ValueError("重定向响应缺少 Location")
                    redirect_url = urljoin(current_url, location)
                    self._validate_url(redirect_url)
                    redirect_count += 1
                    if redirect_count > _MAX_REDIRECTS:
                        raise ValueError("链接爬虫重定向次数超过限制")
                    current_url = redirect_url
                finally:
                    response.close()
                    response = None

            response.raise_for_status()
            content = self._read_limited_response(response)
            if self._looks_like_feed(content):
                raw_items = self._parse_feed(content)
            else:
                raw_items = self._extract_articles(content, base_url=current_url)
            return self._select_items(
                raw_items=raw_items,
                source=source,
                code=code,
                stock_name=stock_name,
                query_id=query_id,
                db=db,
                seen_urls=seen_urls,
            )
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                session.close()

    def _select_items(
        self,
        raw_items: List[Dict[str, Any]],
        source: CrawlSource,
        code: str,
        stock_name: str,
        query_id: Optional[str],
        db,
        seen_urls: set,
    ) -> List[Dict[str, Any]]:
        """过滤时效、截断条数、按 URL 去重，并尝试持久化。"""
        now = datetime.now()
        collected: List[Dict[str, Any]] = []
        for item in raw_items:
            if len(collected) >= self.max_items_per_source:
                break
            title = self._clean_text(item.get("title") or "")
            url = (item.get("url") or "").strip()
            published_date = item.get("date")
            if not title or not url:
                continue
            if not self._is_http_url(url):
                continue
            if url in seen_urls:
                continue
            if published_date is not None:
                if now - published_date > timedelta(hours=self.max_age_hours):
                    continue
            seen_urls.add(url)
            saved = False
            if db is not None:
                saved = save_crawl_news_item(
                    db,
                    query_id=query_id,
                    code=code,
                    name=stock_name,
                    source_label=source.label,
                    market=source.market,
                    title=title,
                    snippet=item.get("snippet"),
                    url=url,
                    published_date=published_date,
                )
            # db 未提供时仍返回上下文；db 提供但持久化失败也不阻断上下文
            if saved or db is None:
                collected.append(
                    {
                        "title": title,
                        "url": url,
                        "date": published_date,
                        "snippet": item.get("snippet"),
                    }
                )
        return collected

    def _existing_urls(self, db, code: str) -> set:
        """该股票已入库的爬虫新闻 URL（用于跨批次去重）。"""
        if db is None or not code:
            return set()
        try:
            if hasattr(db, "get_session"):
                from sqlalchemy import select

                with db.get_session() as session:
                    rows = session.execute(
                        select(CrawlNewsItem.url).where(CrawlNewsItem.code == code)
                    ).scalars().all()
                return {u for u in rows if u}
            if hasattr(db, "query"):  # 直接传入 Session 的兼容路径
                rows = db.query(CrawlNewsItem).filter_by(code=code).all()
                return {u for u in (getattr(r, "url", None) for r in rows) if u}
        except Exception as exc:
            logger.debug("查询已有爬虫新闻 URL 失败（fail-open）: %s", exc)
        return set()

    # ---------- 页面 / Feed 解析 ----------

    def _looks_like_feed(self, content: bytes) -> bool:
        head = content[:4096].lower().lstrip()
        if head.startswith(b"<rss") or head.startswith(b"<feed"):
            return True
        return b"<item>" in head or b"<entry>" in head

    def _read_limited_response(self, response: requests.Response) -> bytes:
        """读取响应体并限制大小（超过 _MAX_RESPONSE_BYTES 拒绝）。"""
        if hasattr(response, "iter_content") and callable(response.iter_content):
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise ValueError("爬虫页面响应过大（超过 2MB）")
                chunks.append(chunk)
            return b"".join(chunks)
        content = response.content[:_MAX_RESPONSE_BYTES + 1]
        if len(content) > _MAX_RESPONSE_BYTES:
            raise ValueError("爬虫页面响应过大（超过 2MB）")
        return content

    def _parse_feed(self, content: bytes) -> List[Dict[str, Any]]:
        """解析 RSS/Atom feed（复用 intelligence_service 的解析思路）。"""
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return []
        tag = self._strip_ns(root.tag).lower()
        entries: List[Dict[str, Any]] = []
        if tag == "rss":
            for node in root.findall("./channel/item"):
                entries.append(
                    {
                        "title": self._clean_text(self._text(node, "title")),
                        "url": self._text(node, "link").strip(),
                        "date": self._parse_datetime(
                            self._text(node, "pubDate") or self._text(node, "published")
                        ),
                        "snippet": self._clean_text(
                            self._text(node, "description") or self._text(node, "summary")
                        ),
                    }
                )
        elif tag == "feed":
            for node in root.findall("./{*}entry") or root.findall("./entry"):
                url = ""
                for link in node.findall("./{*}link") or node.findall("./link"):
                    if (link.attrib.get("rel") or "alternate").lower() == "alternate" and link.attrib.get("href"):
                        url = link.attrib["href"].strip()
                        break
                entries.append(
                    {
                        "title": self._clean_text(self._text(node, "title")),
                        "url": url,
                        "date": self._parse_datetime(
                            self._text(node, "published") or self._text(node, "updated")
                        ),
                        "snippet": self._clean_text(
                            self._text(node, "summary") or self._text(node, "content")
                        ),
                    }
                )
        else:
            return []
        return [e for e in entries if e.get("title") or e.get("url")]

    def _extract_articles(self, html: str, base_url: str) -> List[Dict[str, Any]]:
        """从 HTML 页面提取新闻链接。

        优先使用 bs4（newspaper3k 的依赖）；导入失败时回退到正则实现。
        返回条目列表，每个条目为 {title, url, date, snippet}。
        """
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except ImportError:
            return self._extract_articles_regex(html, base_url)

        try:
            soup = BeautifulSoup(html or "", "html.parser")
        except Exception:
            return self._extract_articles_regex(html, base_url)

        results: List[Dict[str, Any]] = []
        for anchor in soup.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            title = anchor.get_text(" ", strip=True) or (anchor.get("title") or "").strip()
            if not self._is_valid_anchor_text(title):
                continue
            url = self._normalize_anchor_url(href, base_url)
            if url is None:
                continue
            results.append(
                {
                    "title": title,
                    "url": url,
                    "date": self._find_date_near_anchor(anchor),
                    "snippet": "",
                }
            )
        return results

    def _extract_articles_regex(self, html: str, base_url: str) -> List[Dict[str, Any]]:
        """bs4 不可用时的正则回退实现。"""
        results: List[Dict[str, Any]] = []
        pattern = re.compile(
            r'<a\s+[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        for match in pattern.finditer(html or ""):
            href = match.group(1).strip()
            inner = re.sub(r"<[^>]+>", " ", match.group(2))
            title = re.sub(r"\s+", " ", inner).strip()
            if not self._is_valid_anchor_text(title):
                continue
            url = self._normalize_anchor_url(href, base_url)
            if url is None:
                continue
            results.append({"title": title, "url": url, "date": None, "snippet": ""})
        return results

    def _is_valid_anchor_text(self, text: str) -> bool:
        """过滤导航/页脚/功能链接的短文本与通用词。"""
        cleaned = self._clean_text(text)
        if not cleaned:
            return False
        if len(cleaned) < 8:
            return False
        if len(cleaned) > 300:
            return False
        if cleaned.lower() in _GENERIC_ANCHOR_TEXTS:
            return False
        return True

    @staticmethod
    def _normalize_anchor_url(href: str, base_url: str) -> Optional[str]:
        """把锚点 href 规范化为绝对 http(s) URL，非法/空链接返回 None。"""
        href = (href or "").strip()
        if not href:
            return None
        low = href.lower()
        if any(token in low for token in ("#", "javascript:", "mailto:", "tel:")):
            return None
        if href.startswith("//"):
            parsed = urlparse(base_url)
            href = f"{parsed.scheme}:{href}"
        try:
            url = urljoin(base_url, href)
        except ValueError:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return url

    def _find_date_near_anchor(self, anchor) -> Optional[datetime]:
        """尽力从锚点附近提取发布日期；找不到返回 None。"""
        candidates = []
        try:
            candidates.extend(anchor.find_all("time"))
        except Exception:
            pass
        parent = getattr(anchor, "parent", None)
        if parent is not None:
            try:
                candidates.extend(parent.find_all("time"))
            except Exception:
                pass
        for node in candidates:
            dt = node.get("datetime") or node.get_text(" ", strip=True)
            parsed = self._parse_datetime(dt)
            if parsed is not None:
                return parsed
        for container in (anchor, parent):
            if container is None:
                continue
            try:
                text = container.get_text(" ", strip=True)
            except Exception:
                continue
            parsed = self._parse_date_from_text(text)
            if parsed is not None:
                return parsed
        return None

    def _parse_date_from_text(self, text: str) -> Optional[datetime]:
        """从自由文本中识别日期（ISO / 中文 / 月-日 时:分）。"""
        if not text:
            return None
        for pattern in _DATE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            groups = match.groups()
            try:
                if len(groups) == 3:
                    year, month, day = (int(g) for g in groups)
                    return datetime(year, month, day)
                month, day, hour, minute = (int(g) for g in groups)
                now = datetime.now()
                year = now.year
                candidate = datetime(year, month, day, hour, minute)
                if candidate > now + timedelta(days=1):
                    candidate = datetime(year - 1, month, day, hour, minute)
                return candidate
            except (ValueError, TypeError):
                continue
        return None

    # ---------- 输出格式化 ----------

    @staticmethod
    def _format_context(label: str, items: List[Dict[str, Any]]) -> str:
        lines = [f"## 🔗 爬虫新闻源（{label}）"]
        for item in items:
            date_part = ""
            date = item.get("date")
            if isinstance(date, datetime):
                date_part = f"（{date.strftime('%Y-%m-%d')}）"
            lines.append(f"- {item['title']}{date_part}{item['url']}")
        return "\n".join(lines)

    # ---------- 工具 ----------

    @staticmethod
    def _strip_ns(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    @classmethod
    def _text(cls, node: ET.Element, name: str) -> str:
        found = node.find(f"./{{*}}{name}")
        if found is None:
            found = node.find(f"./{name}")
        return "" if found is None or found.text is None else found.text.strip()

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()

    @staticmethod
    def _parse_datetime(value: str) -> Optional[datetime]:
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _is_http_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
