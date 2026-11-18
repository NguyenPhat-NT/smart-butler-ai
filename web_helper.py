# -*- coding: utf-8 -*-
"""
Hỗ trợ tìm kiếm thông tin thực tế (Gemini Google Search Grounding)
và đọc tóm tắt link web / YouTube bằng thư viện nhẹ (0 dependency ngoài aiohttp).
"""
import re
import json
import time
import asyncio
import socket
import logging
import ipaddress
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlparse, quote, urljoin
from typing import Optional, Dict, List
import aiohttp

logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
YOUTUBE_HOST_RE = re.compile(r"(?:^|\.)(?:youtube\.com|youtu\.be)$", re.IGNORECASE)
TIKTOK_HOST_RE = re.compile(r"(?:^|\.)tiktok\.com$", re.IGNORECASE)

MAX_HTML_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 1400
_GROUNDING_BACKOFF_UNTIL = 0.0


def extract_web_urls(text: str, limit: int = 2) -> List[str]:
    """Trích xuất các link web / YouTube (bỏ qua TikTok vì media_helper xử lý riêng)."""
    if not text:
        return []
    urls: List[str] = []
    seen = set()
    for m in URL_REGEX.finditer(text):
        url = m.group(0).rstrip(".,!?:;")
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            continue
        if not host or TIKTOK_HOST_RE.search(host):
            continue
        if url not in seen and is_safe_public_url(url):
            seen.add(url)
            urls.append(url)
            if len(urls) >= limit:
                break
    return urls


def _is_public_ip_obj(ip_obj) -> bool:
    return not (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
    )


def is_safe_public_url(url: str) -> bool:
    """Chặn các URL nội bộ / loopback để bảo vệ mạng LAN và tránh đọc nhầm dịch vụ local."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").strip().lower()
        if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan")):
            return False
        try:
            ip_obj = ipaddress.ip_address(host)
            return _is_public_ip_obj(ip_obj)
        except ValueError:
            pass
        return True
    except Exception:
        return False


async def _resolves_to_public_ips(url: str) -> bool:
    """Phân giải DNS bất đồng bộ và từ chối nếu tên miền trỏ về IP nội bộ/loopback."""
    if not is_safe_public_url(url):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        ip_obj = ipaddress.ip_address(host.lower())
        return _is_public_ip_obj(ip_obj)
    except ValueError:
        pass
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=3.0,
        )
        if not infos:
            return False
        for info in infos:
            addr_ip = str(info[4][0]).split("%", 1)[0]
            if not _is_public_ip_obj(ipaddress.ip_address(addr_ip)):
                return False
        return True
    except Exception:
        return False


class _ReadableHTMLParser(HTMLParser):
    """Trích xuất tiêu đề, meta description và văn bản chính từ HTML bằng stdlib."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header"}
    TEXT_TAGS = {"p", "h1", "h2", "h3", "li", "article", "blockquote"}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self._in_text_tag = 0
        self.title_parts: List[str] = []
        self.meta_desc: str = ""
        self.paragraphs: List[str] = []
        self._current_para: List[str] = []

    def handle_starttag(self, tag: str, attrs):
        t = tag.lower()
        if t in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if t == "title" and self._skip_depth == 0:
            self._in_title = True
        elif t == "meta":
            attr_map = {k.lower(): (v or "") for k, v in attrs if k}
            name = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content", "").strip()
            if content and name in ("description", "og:description", "twitter:description") and not self.meta_desc:
                self.meta_desc = content
            elif content and name in ("og:title", "twitter:title") and not self.title_parts:
                self.title_parts.append(content)
        elif t in self.TEXT_TAGS and self._skip_depth == 0:
            self._in_text_tag += 1

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if t == "title":
            self._in_title = False
        elif t in self.TEXT_TAGS and self._in_text_tag > 0:
            self._in_text_tag -= 1
            if self._in_text_tag == 0 and self._current_para:
                para = re.sub(r"\s+", " ", "".join(self._current_para)).strip()
                if len(para) >= 20:
                    self.paragraphs.append(para)
                self._current_para.clear()

    def handle_data(self, data: str):
        if self._skip_depth > 0 or not data:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_text_tag > 0:
            self._current_para.append(data)


async def fetch_youtube_oembed(url: str) -> Optional[Dict[str, str]]:
    """Lấy tiêu đề và tên kênh YouTube nhanh qua endpoint oEmbed chính thức."""
    oembed_url = f"https://www.youtube.com/oembed?url={quote(url, safe='')}&format=json"
    import gemini_client
    try:
        session = gemini_client.get_session()
        async with session.get(oembed_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "kind": "youtube",
                    "url": url,
                    "title": str(data.get("title", "")).strip(),
                    "author": str(data.get("author_name", "")).strip(),
                    "summary": f"Video YouTube '{data.get('title', '')}' của kênh {data.get('author_name', '')}".strip(),
                }
    except Exception as e:
        logger.debug(f"YouTube oEmbed failed for {url}: {e}")
    return None


async def fetch_url_context(url: str) -> Optional[Dict[str, str]]:
    """Đọc tóm tắt an toàn từ link web / bài báo / YouTube để AI nắm nội dung."""
    if not is_safe_public_url(url):
        return None

    host = (urlparse(url).hostname or "").lower()
    if YOUTUBE_HOST_RE.search(host):
        yt = await fetch_youtube_oembed(url)
        if yt and yt.get("title"):
            return yt

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }
    import gemini_client
    try:
        session = gemini_client.get_session()
        # Tự đi theo redirect nhưng KIỂM TRA LẠI từng chặng, tránh bị redirect về IP nội bộ
        resp = None
        current_url = url
        for _ in range(4):
            if not await _resolves_to_public_ips(current_url):
                logger.warning(f"Chặn URL/redirect tới địa chỉ không an toàn: {current_url}")
                return None
            r = await session.get(
                current_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
                allow_redirects=False,
            )
            if r.status in (301, 302, 303, 307, 308):
                location = r.headers.get("Location")
                r.release()
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue
            resp = r
            break

        if resp is None or resp.status != 200:
            if resp is not None:
                resp.release()
            return None
        try:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text/" not in ctype:
                return None
            chunks: List[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(16384):
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_HTML_BYTES:
                    break
            raw_html = b"".join(chunks).decode(resp.charset or "utf-8", errors="replace")
        finally:
            resp.release()
    except Exception as e:
        logger.warning(f"Fetch URL failed ({url}): {e}")
        return None

    parser = _ReadableHTMLParser()
    try:
        parser.feed(raw_html)
    except Exception:
        pass

    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()[:200]
    desc = re.sub(r"\s+", " ", parser.meta_desc).strip()[:350]
    body_text = " ".join(parser.paragraphs)
    summary_parts = []
    if title:
        summary_parts.append(f"Tiêu đề: {title}")
    if desc:
        summary_parts.append(f"Mô tả: {desc}")
    if body_text:
        remaining = max(250, MAX_SUMMARY_CHARS - sum(len(p) for p in summary_parts))
        summary_parts.append(f"Nội dung chính: {body_text[:remaining]}")

    if not summary_parts:
        return None

    return {
        "kind": "web",
        "url": url,
        "title": title,
        "summary": "\n".join(summary_parts),
    }


async def _fetch_bing_and_gnews_snippets(query: str) -> str:
    """Lấy tiêu đề & trích đoạn tìm kiếm thời gian thực từ Bing RSS và Google News VN RSS (miễn phí, nhẹ RAM)."""
    q_enc = quote(query.strip())
    bing_url = f"https://www.bing.com/search?q={q_enc}&format=rss"
    gnews_url = f"https://news.google.com/rss/search?q={q_enc}&hl=vi&gl=VN&ceid=VN:vi"
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36"}

    async def _get_rss(session: aiohttp.ClientSession, url: str) -> str:
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    raw = await resp.content.read(200 * 1024)
                    return raw.decode("utf-8", errors="replace")
        except Exception as e:
            logger.debug(f"RSS fetch failed ({url}): {e}")
        return ""

    import asyncio
    import gemini_client
    session = gemini_client.get_session()
    bing_xml, gnews_xml = await asyncio.gather(_get_rss(session, bing_url), _get_rss(session, gnews_url))

    lines: List[str] = []
    if gnews_xml:
        try:
            root = ET.fromstring(gnews_xml)
            for it in root.findall("./channel/item")[:5]:
                title = (it.findtext("title") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                if title:
                    lines.append(f"- [Tin mới | {pub}] {title}")
        except Exception:
            pass

    if bing_xml:
        try:
            root = ET.fromstring(bing_xml)
            for it in root.findall("./channel/item")[:5]:
                title = (it.findtext("title") or "").strip()
                desc = re.sub(r"\s+", " ", (it.findtext("description") or "")).strip()
                if title or desc:
                    lines.append(f"- [Web] {title}: {desc[:240]}")
        except Exception:
            pass

    return "\n".join(lines)


async def search_with_gemini_grounding(query: str, user_message: str = "") -> Optional[str]:
    """
    Tra cứu thông tin thực tế:
    1) Ưu tiên Gemini Google Search Grounding (tự tạm nghỉ 6h nếu Free Tier trả 429 quota=0).
    2) Dự phòng tức thì bằng Bing Web RSS + Google News VN RSS rồi tóm tắt bằng CHAT_MODEL.
    """
    global _GROUNDING_BACKOFF_UNTIL
    clean_query = (query or "").strip()
    if len(clean_query) < 2:
        return None

    import gemini_client
    import config
    import diary_helper

    vn_ctx = diary_helper.get_vietnam_time_context()
    today_line = f"{vn_ctx.get('weekday_str', '')}, ngày {vn_ctx.get('date_str', '')} lúc {vn_ctx.get('time_str', '')} (giờ Việt Nam)"

    prompt = (
        f"Thời điểm hiện tại: {today_line}.\n"
        f"Câu người dùng vừa hỏi: {user_message[:300]}\n"
        f"Truy vấn cần tìm hiểu: {clean_query}\n\n"
        "Hãy tìm kiếm thông tin chính xác mới nhất trên Google và tổng hợp thành 3 đến 5 gạch đầu dòng ngắn gọn "
        "(nêu rõ con số, giá cả, ngày giờ, tên địa điểm hoặc diễn biến chính xác, tối đa 180 từ)."
    )

    if time.time() >= _GROUNDING_BACKOFF_UNTIL:
        try:
            result = await gemini_client.call_gemini_api(
                model=config.CHAT_MODEL,
                system_instruction="Bạn là trợ lý tra cứu dữ kiện thực tế nhanh, chính xác và ngắn gọn bằng tiếng Việt.",
                contents=[{"role": "user", "parts": [{"text": prompt}]}],
                response_json=False,
                key_role="chat",
                tools=[{"google_search": {}}],
                timeout_sec=12.0,
            )
            cleaned = (result or "").strip()
            if cleaned:
                logger.info(f"🔎 [Web Search Grounding]: query='{clean_query}' -> {len(cleaned)} chars")
                return cleaned[:1200]
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                _GROUNDING_BACKOFF_UNTIL = time.time() + 21600
                logger.info("Gemini Google Search Grounding đang giới hạn quota Free Tier -> chuyển sang RSS Live Search + tổng hợp LLM trong 6h")
            else:
                logger.warning(f"Web Search Grounding lỗi với query='{clean_query}': {e}")

    # Dự phòng miễn phí hoàn toàn: Bing RSS + Google News VN RSS + CHAT_MODEL tổng hợp
    snippets = await _fetch_bing_and_gnews_snippets(clean_query)
    if not snippets:
        return None

    synth_prompt = (
        f"Thời điểm hiện tại: {today_line}.\n"
        f"Câu người dùng vừa hỏi: {user_message[:300]}\n"
        f"Truy vấn: {clean_query}\n\n"
        f"Kết quả tìm kiếm trực tiếp từ Internet:\n{snippets}\n\n"
        "Dựa vào các kết quả thực tế ở trên, hãy chắt lọc thành 3 đến 5 gạch đầu dòng dữ kiện chính xác, "
        "rõ con số/thời gian/địa điểm để dùng trả lời tự nhiên (tối đa 160 từ)."
    )
    try:
        synthesized = await gemini_client.call_gemini_api(
            model=config.CHAT_MODEL,
            system_instruction="Bạn là bộ tổng hợp dữ kiện tìm kiếm thực tế ngắn gọn, trung thực và chính xác.",
            contents=[{"role": "user", "parts": [{"text": synth_prompt}]}],
            response_json=False,
            key_role="chat",
            timeout_sec=15.0,
        )
        cleaned = (synthesized or "").strip()
        if cleaned:
            logger.info(f"🔎 [RSS Live Web Search]: query='{clean_query}' -> {len(cleaned)} chars")
            return cleaned[:1200]
    except Exception as e:
        logger.warning(f"RSS Live Search synthesis failed ({clean_query}): {e}")
    return snippets[:1200]
