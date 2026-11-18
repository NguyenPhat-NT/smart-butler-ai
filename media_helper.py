# -*- coding: utf-8 -*-
import os
import io
import re
import asyncio
import logging
import aiohttp
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# Regex nhận diện mọi định dạng link TikTok (chuẩn, rút gọn, nhúng player)
TIKTOK_REGEX = re.compile(r"https?://(?:www\.|vt\.|vm\.|v\.)?tiktok\.com/[^\s<>\"]+")

def extract_tiktok_url(text: str) -> Optional[str]:
    """Tìm và chuẩn hóa link TikTok trong tin nhắn."""
    match = TIKTOK_REGEX.search(text)
    if not match:
        return None
    url = match.group(0)
    
    # Chuẩn hóa nếu là link nhúng iframe player
    player_match = re.search(r"tiktok\.com/player/v1/(\d+)", url)
    if player_match:
        url = f"https://www.tiktok.com/@user/video/{player_match.group(1)}"
        
    return url

async def fetch_tiktok_data(tiktok_url: str) -> Optional[Dict[str, Any]]:
    """
    Lấy thông tin tiêu đề, tác giả, nhạc nền và link tải MP4 trực tiếp qua TikWM API.
    Nhanh, nhẹ, không bị TikTok chặn như yt-dlp.
    """
    api_url = f"https://www.tikwm.com/api/?url={tiktok_url}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    import gemini_client
    try:
        session = gemini_client.get_session()
        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                result = await resp.json()
                if result.get("code") == 0 and "data" in result:
                    d = result["data"]
                    return {
                        "title": d.get("title", ""),
                        "author": d.get("author", {}).get("nickname", "ai đó"),
                        "music": d.get("music_info", {}).get("title", ""),
                        "play_url": d.get("play", ""),
                        "cover": d.get("cover", "")
                    }
                else:
                    logger.warning(f"TikWM returned non-zero code: {result.get('msg')}")
    except Exception as e:
        logger.error(f"Error calling TikWM API: {e}")
        
    return None

async def download_video_bytes(play_url: str) -> Optional[bytes]:
    """Tải trực tiếp video MP4 vào bộ nhớ RAM (thường chỉ 1-3MB)."""
    if not play_url:
        return None
        
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    import gemini_client
    try:
        session = gemini_client.get_session()
        async with session.get(play_url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            if resp.status == 200:
                video_bytes = await resp.read()
                # Giới hạn file <= 20MB để bảo vệ RAM Snap 410
                if len(video_bytes) <= 20 * 1024 * 1024:
                    return video_bytes
                else:
                    logger.warning(f"Video quá lớn ({len(video_bytes)/1024/1024:.1f}MB), bỏ qua tải video.")
    except Exception as e:
        logger.error(f"Error downloading TikTok video: {e}")
        
    return None

def clean_temp_file(file_path: str):
    """Xóa file tạm nếu có lưu ra đĩa."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logger.error(f"Failed to delete temp file {file_path}: {e}")


async def download_telegram_bytes(
    bot,
    file_id: str,
    file_size: Optional[int] = None,
    max_bytes: int = 15 * 1024 * 1024,
) -> Optional[bytes]:
    """Tải file từ Telegram Bot API vào RAM với giới hạn dung lượng để bảo vệ thiết bị."""
    if not file_id:
        return None
    if file_size and file_size > max_bytes:
        logger.warning(f"Bỏ qua file Telegram dung lượng lớn ({file_size / 1024 / 1024:.1f}MB > {max_bytes / 1024 / 1024:.1f}MB)")
        return None
    try:
        file_info = await bot.get_file(file_id)
        if getattr(file_info, "file_size", None) and file_info.file_size > max_bytes:
            return None
        stream = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=stream)
        raw = stream.getvalue()
        if len(raw) > max_bytes:
            return None
        return raw
    except Exception as e:
        logger.warning(f"Lỗi tải file Telegram ({file_id}): {e}")
        return None


def _guess_image_mime(raw: bytes, default_mime: str = "image/jpeg") -> str:
    """Nhận diện nhanh định dạng ảnh từ magic header để gửi đúng mime_type cho Gemini."""
    if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        return "image/webp"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"GIF8"):
        return "image/gif"
    return default_mime


async def sticker_to_media_payload(bot, sticker) -> Tuple[Optional[Tuple[bytes, str]], str]:
    """
    Chuyển Sticker Telegram (tĩnh .webp hoặc thumbnail của sticker động .tgs / video .webm)
    thành (bytes, mime_type) kèm mô tả ngữ cảnh để Gemini đọc hiểu biểu cảm sticker.
    """
    emoji = getattr(sticker, "emoji", "") or ""
    set_name = getattr(sticker, "set_name", "") or ""
    is_animated = bool(getattr(sticker, "is_animated", False))
    is_video = bool(getattr(sticker, "is_video", False))

    target_file_id = None
    target_size = None
    default_mime = "image/webp"

    if not is_animated and not is_video:
        target_file_id = getattr(sticker, "file_id", None)
        target_size = getattr(sticker, "file_size", None)
    else:
        thumb = getattr(sticker, "thumbnail", None)
        if thumb:
            target_file_id = getattr(thumb, "file_id", None)
            target_size = getattr(thumb, "file_size", None)

    media_pair: Optional[Tuple[bytes, str]] = None
    if target_file_id:
        raw = await download_telegram_bytes(bot, target_file_id, file_size=target_size, max_bytes=4 * 1024 * 1024)
        if raw:
            mime = _guess_image_mime(raw, default_mime=default_mime)
            if mime in ("image/webp", "image/jpeg", "image/png"):
                media_pair = (raw, mime)

    desc_parts = ["Anh vừa gửi 1 sticker Telegram"]
    if emoji:
        desc_parts.append(f"biểu tượng cảm xúc gợi ý '{emoji}'")
    if set_name:
        desc_parts.append(f"bộ '{set_name}'")
    desc = " (".join([desc_parts[0], ", ".join(desc_parts[1:]) + ")"]) if len(desc_parts) > 1 else desc_parts[0]
    desc += ". Hãy nhìn biểu cảm/nội dung sticker để trêu, tung hứng hoặc phản hồi đúng cảm xúc tự nhiên."
    return media_pair, desc

