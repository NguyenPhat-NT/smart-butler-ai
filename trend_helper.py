# -*- coding: utf-8 -*-
import re
import datetime
import logging
import xml.etree.ElementTree as ET
from typing import List, Tuple
import aiohttp

import config
import database
import persona

logger = logging.getLogger(__name__)
LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=config.TIMEZONE_OFFSET_HOURS))


def get_today_date_str() -> str:
    """Trả về ngày hiện tại theo múi giờ cấu hình: YYYY-MM-DD."""
    return datetime.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def _extract_rss_titles(xml_text: str, exclude_substrings: Tuple[str, ...], limit: int) -> List[str]:
    raw_titles: List[str] = []
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for el in root.iter():
                tag = el.tag.split("}", 1)[-1].lower()
                if tag == "title" and el.text:
                    raw_titles.append(el.text.strip())
        except Exception:
            for cdata_m, plain_m in re.findall(
                r"<title[^>]*>\s*(?:<!\[CDATA\[(.*?)\]\]>|([^<]+))\s*</title>",
                xml_text,
                flags=re.DOTALL | re.IGNORECASE,
            ):
                val = (cdata_m or plain_m or "").strip()
                if val:
                    raw_titles.append(val)

    cleaned: List[str] = []
    for t in raw_titles:
        if any(sub in t for sub in exclude_substrings):
            continue
        cleaned.append(t)
        if len(cleaned) >= limit:
            break
    return cleaned


async def fetch_realtime_feeds() -> Tuple[str, str, List[str], List[str]]:
    """Tải nhanh thời tiết tại WEATHER_CITY, Google Trends VN và tin tức mới."""
    city = persona.get_persona_config().get("weather_city") or config.WEATHER_CITY
    city_weather = f"Thời tiết tại {city.replace('_', ' ')} ổn định"
    trends: List[str] = []
    news_items: List[str] = []

    headers = {"User-Agent": "Mozilla/5.0 (compatible; SmartButlerBot/1.0)"}
    import gemini_client
    session = gemini_client.get_session()

    try:
        async with session.get(
            f"https://wttr.in/{city}?format=%C,+khoang+%t",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status == 200:
                txt = (await resp.text()).strip()
                if txt:
                    city_weather = txt
    except Exception as e:
        logger.warning(f"Lỗi lấy thời tiết ({city}): {e}")

    try:
        async with session.get(
            "https://trends.google.com/trending/rss?geo=VN",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status == 200:
                xml = await resp.text()
                trends = _extract_rss_titles(xml, ("Daily Search",), 6)
    except Exception as e:
        logger.warning(f"Lỗi lấy Google Trends: {e}")

    try:
        async with session.get(
            "https://vnexpress.net/rss/tin-moi-nhat.rss",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status == 200:
                xml = await resp.text()
                news_items = _extract_rss_titles(xml, ("Tin mới", "VnExpress"), 4)
    except Exception as e:
        logger.warning(f"Lỗi lấy VnExpress RSS: {e}")

    return city_weather, city, trends, news_items


async def refresh_daily_trends(force: bool = False) -> str:
    """Cập nhật bản tin thời tiết & xu hướng hằng ngày vào SQLite."""
    today_str = get_today_date_str()
    existing = database.get_today_trends(today_str)
    if existing and not force:
        return existing

    logger.info("Đang cập nhật bản tin thời tiết & tin tức hôm nay...")
    city_weather, city, trends, news_items = await fetch_realtime_feeds()
    trends_joined = ", ".join(trends) if trends else "chưa có trend nổi bật"
    news_joined = "; ".join(news_items) if news_items else "chưa có tin mới"
    pcfg = persona.get_persona_config()

    prompt = f"""Bạn là trợ lý quản gia {pcfg['butler_name']}.
Dưới đây là thông tin thời tiết & tin tức thực tế ngày {today_str}:
- Thời tiết ({city.replace('_', ' ')}): {city_weather}
- Chủ đề tìm kiếm nổi bật hôm nay: {trends_joined}
- Tin tức đáng chú ý: {news_joined}

Hãy tóm tắt gọn thành 3-4 gạch đầu dòng súc tích để làm 'Bản tin sáng / Ngữ cảnh hôm nay' cho {pcfg['owner_name']}.
Chỉ trả về danh sách gạch đầu dòng, không dài dòng ngoài lề."""

    try:
        import gemini_client
        raw_res = await gemini_client.call_gemini_api(
            model=config.CHAT_MODEL,
            system_instruction=f"Bạn là trợ lý quản gia {pcfg['butler_name']}.",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            response_json=False,
        )
        synth_text = raw_res.strip()
        if synth_text:
            database.save_daily_trends(today_str, synth_text)
            return synth_text
    except Exception as e:
        logger.warning(f"Không tổng hợp được bản tin bằng AI, dùng tóm tắt thô: {e}")

    default_trend = (
        f"- Thời tiết ({city.replace('_', ' ')}): {city_weather}.\n"
        f"- Tin nổi bật: {news_joined}."
    )
    database.save_daily_trends(today_str, default_trend)
    return default_trend
