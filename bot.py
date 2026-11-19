# -*- coding: utf-8 -*-
import os
import io
import re
import time
import random
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

import config
import database
import gemini_client
import scheduler
import media_helper
import web_helper
import task_helper
import esp32_audio
import fan_controller
import smart_plugs
import persona

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# Lưu vết thời điểm thả reaction gần nhất cho từng user để áp dụng Cooldown Penalty (0.7)
LAST_REACTION_TIME = {}
# Bộ đệm gom album nhiều ảnh gửi liên tiếp (key: (user_id, media_group_id))
MEDIA_GROUP_BUFFER = {}
MAX_ALBUM_IMAGES = 6
# Giữ dưới 12MB nhị phân vì base64 làm request phình 4/3, Gemini giới hạn ~20MB/request
MAX_ALBUM_BYTES = 12 * 1024 * 1024

def _resolve_bound_owner_id() -> int:
    """Ưu tiên ALLOWED_USER_ID từ env; nếu bằng 0 thì fallback về user đầu tiên đã /start trong DB."""
    if config.ALLOWED_USER_ID and int(config.ALLOWED_USER_ID) > 0:
        return int(config.ALLOWED_USER_ID)
    try:
        return int(database.get_primary_user_id() or 0)
    except Exception:
        return 0


def check_permission(user_id: int) -> bool:
    """Khóa quyền riêng tư (fail-closed): Chỉ cho phép chủ bot đã gắn (`/start` hoặc ALLOWED_USER_ID)."""
    owner_id = _resolve_bound_owner_id()
    if owner_id <= 0:
        return False
    return int(user_id) == owner_id


def _escape_md_legacy(value) -> str:
    """Escape các ký tự đặc biệt của Telegram legacy Markdown trong dữ liệu động."""
    s = str(value or "")
    for ch in ("\\", "_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


async def answer_markdown_safe(message: Message, text: str):
    """Gửi tin nhắn Markdown an toàn: tự chia đoạn >4000 ký tự và fallback plain text nếu lỗi parse."""
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] if len(text) > 4000 else [text]
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest as parse_err:
            logger.warning(f"Markdown send failed ({parse_err}), falling back to plain text")
            await message.answer(chunk, parse_mode=None)
        except Exception as parse_err:
            logger.warning(f"Markdown send failed ({parse_err}), falling back to plain text")
            await message.answer(chunk, parse_mode=None)

@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    owner_id = _resolve_bound_owner_id()
    if owner_id > 0 and int(user_id) != owner_id:
        return
    if int(config.ALLOWED_USER_ID or 0) == 0:
        # Khóa chủ bot đầu tiên vào ALLOWED_USER_ID trong phiên hiện tại (và primary_user_id trong DB)
        config.ALLOWED_USER_ID = user_id
        logger.warning(
            f"ALLOWED_USER_ID đang là 0 -> khóa bot cho user {user_id}. "
            f"Nên điền ALLOWED_USER_ID={user_id} vào .env để giữ nguyên cấu hình."
        )

    database.init_db()
    database.get_or_create_user(user_id)
    p = persona.get_persona_config()

    welcome_msg = (
        f"🤖 **CHÀO MỪNG ĐẾN VỚI {p['butler_name'].upper()} — SMART BUTLER & HOME HUB**\n\n"
        f"Xin chào **{p['owner_name']}**, {p['self_ref']} là **{p['butler_name']}** "
        f"(phong cách: `{p['style_label']}` • khu vực: `{p['weather_city']}`).\n"
        f"Bạn có thể chat tự nhiên bằng tiếng Việt để điều khiển nhà thông minh, nghe nhạc và quản lý công việc.\n\n"
        "⚡ **Khả năng chính:**\n"
        "1. **Smart Home & Thiết bị:** Điều khiển quạt BLDC (`ESP-01S`), ổ cắm (`BroadLink SPmini`), loa thông minh & cảm biến nhiệt/ẩm (`ESP32-C3`), tìm thiết bị theo tên hoặc biệt danh.\n"
        "2. **MCP Tools & Tự học Thói quen:** AI tự truy vấn trạng thái phòng theo nhu cầu (`fetch_tools` + `/mcp` HTTP cho PC) và tự học thói quen bật/tắt an toàn.\n"
        "3. **Trí nhớ Dài hạn & Nhắc việc:** Nhớ sở thích, lịch hẹn, tự nhắc việc khi gần đến giờ và nhận diện ảnh/voice/link.\n\n"
        "📌 **Các lệnh nhanh:**\n"
        "• `/status` - Xem trạng thái thiết bị, thói quen, việc đang mở & trí nhớ\n"
        "• `/habits` - Xem các thói quen thiết bị AI đã học\n"
        "• `/music` - Xem & phát nhạc trên loa ESP32 (`/skip`, `/stop`, `/vol`, `/focus`, `/nap`, `/alarm`, `/eq`)\n"
        "• `/trend` - Xem thời tiết & bản tin xu hướng hôm nay\n"
        "• `/review` - Báo cáo tổng kết ngữ cảnh & thói quen gần đây\n"
        "• `/dashboard` - Xem link Web Dashboard nội bộ, endpoint `/mcp` & mã PIN\n"
        "• `/backup` - Sao lưu SQLite DB và gửi lên Telegram\n"
        "• `/reset` - Làm mới lịch sử chat gần đây (giữ Fact Book cốt lõi)\n"
        "• `/reset_all` - Xóa trắng lịch sử chat & ký ức để bắt đầu lại\n\n"
        f"👉 *Hãy nhắn cho {p['butler_name']} yêu cầu đầu tiên của bạn ngay nhé!*"
    )
    await answer_markdown_safe(message, welcome_msg)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    import circadian
    import proactive_engine
    import habit_engine

    p = persona.get_persona_config()
    user_state = database.get_or_create_user(user_id)
    fact_rows = database.get_memories_by_categories(user_id, exclude_categories=["episode"], limit=10)
    episode_rows = database.get_memories_by_categories(user_id, categories=["episode"], limit=5)
    session_state = database.get_session_state(user_id)

    fact_text = "\n".join([f"• {r['fact']}" for r in fact_rows]) if fact_rows else "• Chưa ghi nhận hồ sơ nền nào."
    ep_text = "\n".join([f"• {r['fact']}" for r in episode_rows]) if episode_rows else "• Chưa có sự kiện gần đây."

    session_lines = []
    if session_state.get("user_activity"):
        session_lines.append(f"• {p['owner_name']}: {session_state['user_activity']}")
    if session_state.get("dan_activity"):
        session_lines.append(f"• {p['butler_name']}: {session_state['dan_activity']}")
    for pl in (session_state.get("today_plans") or [])[:5]:
        st = pl.get("status", "planned")
        tag = "ĐÃ XONG" if st == "done" else ("ĐÃ HỦY" if st == "cancelled" else "Kế hoạch")
        t_hint = f" ({pl['time_hint']})" if pl.get("time_hint") else ""
        session_lines.append(f"• [{tag}] {pl.get('topic', '')}{t_hint}")
    session_block = "\n".join(session_lines) if session_lines else "• Chưa có trạng thái phiên riêng hôm nay."

    circ = circadian.get_circadian_state()
    open_loops = database.get_open_loops(user_id, include_resolved=False)
    loops_line = " | ".join(l["topic"] for l in open_loops[:3]) if open_loops else "Không có việc đang mở"
    eng = proactive_engine.get_engine_snapshot(user_id)
    habits_line = habit_engine.format_habits_for_ai(limit=5)

    fan_snap = fan_controller.get_controller().get_status_snapshot()
    fan_st = fan_snap.get("state") or {}
    plug_snaps = smart_plugs.get_controller().get_status_snapshot()
    spk_snap = esp32_audio.get_manager().get_status_snapshot()
    on_plugs = [pl["name"] for pl in plug_snaps if pl.get("is_on") is True]
    fan_mode = fan_st.get("mode_label") or ("Bật" if fan_st.get("is_on") else "Tắt")
    fan_line = f"{fan_st.get('speed_pct', 0)}% ({fan_mode})"
    if not fan_snap.get("configured"):
        fan_line = "Chưa cấu hình"
    elif not fan_snap.get("online"):
        fan_line += " [offline]"
    spk_line = f"{spk_snap.get('status', 'idle')} • Vol {spk_snap.get('volume', 0)}%"
    dht = spk_snap.get("dht11")
    room_env = f"{dht['temperature_c']}°C / {dht['humidity_pct']}%" if dht else "Chưa có cảm biến"

    status_msg = (
        f"🏠 **TRẠNG THÁI {p['butler_name'].upper()} & NHÀ THÔNG MINH**\n\n"
        f"🎭 **Persona:** `{p['style_label']}` • Khu vực: `{p['weather_city']}`\n"
        f"🕰️ **Nhịp thời gian:** `{circ['label_vi']}` _(chủ động {eng['sent_today']}/{eng['daily_cap']} lượt hôm nay)_\n"
        f"🌡️ **Phòng (DHT11):** `{room_env}`\n"
        f"🌀 **Quạt BLDC:** `{fan_line}`\n"
        f"🔌 **Ổ cắm đang bật ({len(on_plugs)}/{len(plug_snaps)}):** `{', '.join(on_plugs) if on_plugs else 'Không'}`\n"
        f"🔊 **Loa ESP32-C3:** `{spk_line}`\n\n"
        f"🕒 **Trạng thái phiên hôm nay:**\n{session_block}\n"
        f"🧵 **Đầu việc đang theo dõi:** `{loops_line}`\n\n"
        f"🔄 **Thói quen nổi bật:**\n{habits_line}\n\n"
        f"🧠 **Hồ sơ nền đã nhớ (Fact Book):**\n{fact_text}\n\n"
        f"🎞️ **Sự kiện gần nhất:**\n{ep_text}\n\n"
        f"💡 *Gõ `/dashboard` để cấu hình thiết bị, persona và xem `/mcp` endpoint.*"
    )
    await answer_markdown_safe(message, status_msg)

@dp.message(Command("review"))
async def cmd_review(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    await bot.send_chat_action(chat_id=user_id, action="typing")
    status_loading = await message.answer("🔍 *Đang tổng kết hội thoại, thói quen thiết bị và lịch nhắc gần đây...*", parse_mode=ParseMode.MARKDOWN)
    
    try:
        coach_advice = await gemini_client.generate_coach_review(user_id)
        try:
            await status_loading.delete()
        except Exception:
            pass
            
        # Gửi nhận xét an toàn (xử lý lỗi parse markdown và giới hạn 4096 ký tự của Telegram)
        await answer_markdown_safe(message, coach_advice)
    except Exception as e:
        logger.error(f"Review error: {e}")
        try:
            await status_loading.edit_text(f"⚠️ Có lỗi khi gọi Huấn luyện viên: {e}")
        except Exception:
            await message.answer(f"⚠️ Có lỗi khi gọi Huấn luyện viên: {e}")

@dp.message(Command("trend"))
async def cmd_trend(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return
        
    import trend_helper
    today_str = trend_helper.get_today_date_str()
    trends = database.get_today_trends(today_str)
    if not trends:
        await bot.send_chat_action(chat_id=user_id, action="typing")
        trends = await trend_helper.refresh_daily_trends()
        
    trend_msg = (
        f"🌤️ **TỔNG HỢP BỐI CẢNH & XU HƯỚNG HÔM NAY ({today_str})**\n\n"
        f"{trends}\n\n"
        f"💬 *Bạn có thể hỏi trực tiếp trợ lý về thời tiết hoặc tin tức nổi bật bất cứ lúc nào.*"
    )
    await answer_markdown_safe(message, trend_msg)


@dp.message(Command("habits"))
async def cmd_habits(message: Message):
    """Lệnh xem nhanh danh sách thói quen bật/tắt thiết bị tự học."""
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    import habit_engine
    habits_txt = habit_engine.format_habits_for_ai(limit=20)
    await answer_markdown_safe(
        message,
        f"🧠 **THÓI QUEN THIẾT BỊ TỰ HỌC**\n\n{habits_txt}\n\n"
        f"💡 *Bạn có thể chat tự nhiên để dạy hoặc tắt thói quen (vd: 'từ giờ 23h nhớ tắt đèn giúp mình' hoặc 'đừng tự tắt quạt nữa'), hoặc chỉnh chi tiết trên Web Dashboard (`/dashboard`).*",
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    async with task_helper.get_user_lock(user_id):
        database.reset_user(user_id, wipe_all=False)
    p = persona.get_persona_config()
    await answer_markdown_safe(
        message,
        "🔄 **ĐÃ LÀM MỚI LỊCH SỬ TRÒ CHUYỆN!**\n\n"
        f"Lịch sử chat gần đây đã được dọn sạch, vẫn giữ nguyên Fact Book, cấu hình thiết bị và thói quen đã học.\n"
        f"Nếu muốn xóa trắng 100% cả ký ức, hãy dùng `/reset_all`. Hãy nhắn cho {p['butler_name']} ngay nhé!",
    )


@dp.message(Command("reset_all"))
async def cmd_reset_all(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    async with task_helper.get_user_lock(user_id):
        database.reset_user(user_id, wipe_all=True)
    p = persona.get_persona_config()
    await answer_markdown_safe(
        message,
        "🧹 **ĐÃ XÓA TRẮNG TOÀN BỘ LỊCH SỬ & KÝ ỨC!**\n\n"
        f"Mọi tin nhắn cũ, ký ức cũ, nhật ký và lịch nhắc hẹn đều đã được làm sạch.\n"
        f"{p['butler_name']} sẵn sàng phục vụ lại từ đầu!",
    )


@dp.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    import lan_presence
    import lan_watchdog
    import web_dashboard

    if not config.DASHBOARD_ENABLED:
        await message.answer("Web Dashboard hiện đang tắt (DASHBOARD_ENABLED=false).")
        return

    snap = lan_presence.get_presence_snapshot()
    local_ip = snap.get("local_ip") or (await lan_presence.get_local_network_info()).get("local_ip") or "192.168.1.100"
    pin = web_dashboard.get_effective_pin()
    wd = lan_watchdog.get_watchdog_snapshot()
    tts_state = "Đang bật" if snap.get("tts_online") else "Đang tắt / PC nghỉ"

    text = (
        f"🌐 **WEB DASHBOARD & MCP HUB (NỘI BỘ LAN)**\n\n"
        f"🔗 Dashboard: http://{local_ip}:{config.DASHBOARD_PORT}\n"
        f"🔌 MCP Endpoint: `http://{local_ip}:{config.DASHBOARD_PORT}/mcp`\n"
        f"🔐 Mã PIN / Bearer: `{pin}`\n\n"
        f"🏠 Presence: `{snap.get('state', 'unconfigured')}`\n"
        f"🎙️ TTS Server: {tts_state}\n"
        f"🛡️ Watchdog mạng: `{wd.get('status', 'starting')}` (tự phục hồi {wd.get('heals_count', 0)} lần)\n\n"
        f"💡 Mở link Dashboard bằng trình duyệt cùng Wi-Fi để đổi Persona (Tên/Phong cách Quản gia), gán MAC thiết bị, xem thói quen tự học và điều khiển loa/quạt/ổ cắm."
    )
    await answer_markdown_safe(message, text)


@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    import backup_helper

    await message.answer("⏳ Đang nén bản sao lưu SQLite an toàn và gửi lên Telegram...")
    try:
        await backup_helper.send_backup_to_telegram(bot, user_id, reason="Thủ công (/backup)", create_new=True)
    except Exception as e:
        logger.error(f"Lỗi /backup: {e}")
        await message.answer(f"❌ Sao lưu thất bại: {type(e).__name__}: {e}")


@dp.message(Command("music", "nhac"))
async def cmd_music(message: Message):
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    parts = (message.text or "").split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    mgr = esp32_audio.get_manager()

    if not query:
        snap = mgr.get_status_snapshot()
        esp = snap.get("esp32") or {}
        dht = snap.get("dht11")
        cache = snap.get("cache") or {}
        tracks = cache.get("tracks") or []
        used_mb = (cache.get("total_bytes") or 0) / (1024 * 1024)
        max_mb = (cache.get("max_bytes") or esp32_audio.get_effective_max_cache_bytes()) / (1024 * 1024)
        cur = snap.get("current_track")

        lines = [
            "🔊 *LOA THÔNG MINH ESP32-C3 & JUKEBOX*",
            f"• Kết nối loa: `{'Online' if esp.get('online') else 'Chưa kết nối'}`"
            + (f" ({_escape_md_legacy(esp.get('ip'))})" if esp.get("ip") else ""),
        ]
        if dht:
            lines.append(f"• Nhiệt độ / Độ ẩm phòng (DHT11): `{dht['temperature_c']}°C` • Độ ẩm `{dht['humidity_pct']}%`")
        lines.append(
            f"• Trạng thái: `{_escape_md_legacy(snap.get('status'))}` • Âm lượng: `{snap.get('volume')}%` • Auto-DJ: `{'Bật' if snap.get('auto_dj') else 'Tắt'}`"
        )
        if cur:
            lines.append(f"• Đang phát: *{_escape_md_legacy(cur.get('title'))}*")
        alarms = snap.get("alarms") or []
        if alarms:
            sched_parts = []
            for a in alarms[:4]:
                icon = "🎵" if a.get("kind") == "music" else "⏰"
                dur = int(a.get("duration_min") or 0)
                dur_txt = f" ({dur}p)" if dur > 0 else ""
                sched_parts.append(
                    f"{icon} {_escape_md_legacy(a.get('time_vn', ''))}: "
                    f"{_escape_md_legacy(a.get('query') or a.get('label', ''))}{dur_txt}"
                )
            lines.append("• Lịch hẹn loa: " + " | ".join(sched_parts))
        loc_label = "ổ USB ngoài" if cache.get("storage_mode") == "usb" else "bộ nhớ trong (eMMC)"
        lines.append(f"\n💿 *Kho nhạc trên {loc_label} ({len(tracks)} bài • {used_mb:.1f}/{max_mb:.0f} MB):*")
        if tracks:
            for idx, tr in enumerate(tracks[:12], start=1):
                lines.append(f"{idx}. {_escape_md_legacy(tr.get('title'))}")
        else:
            butler_name = persona.get_persona_config()["butler_name"]
            lines.append(f"_Kho nhạc đang trống — gõ `/music tên bài` hoặc gửi link YouTube, hoặc nhắn '{butler_name} ơi mở bài ...' nhé!_")
        await answer_markdown_safe(message, "\n".join(lines))
        return

    cached = esp32_audio.find_cached_track(query)
    if cached:
        await mgr.play_track(cached)
        await message.answer(f"🎵 Đang phát từ kho USB: {cached.get('title')}")
        return

    mgr.spawn_play_or_search(query, play_now=True)
    await message.answer(f"⏳ Đang tìm & tải \"{query}\" trên YouTube về USB, xong sẽ tự phát ra loa ESP32-C3 ngay!")


@dp.message(Command("skip"))
async def cmd_skip(message: Message):
    if not check_permission(message.from_user.id):
        return
    snap = await esp32_audio.get_manager().skip()
    cur = snap.get("current_track")
    if cur:
        await message.answer(f"⏭️ Đã chuyển sang bài: {cur.get('title')}")
    else:
        await message.answer("⏭️ Hết bài trong kho nhạc/hàng đợi nên đã dừng.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if not check_permission(message.from_user.id):
        return
    await esp32_audio.get_manager().stop()
    await message.answer("⏹️ Đã dừng phát nhạc trên loa ESP32-C3.")


@dp.message(Command("vol"))
async def cmd_vol(message: Message):
    if not check_permission(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    mgr = esp32_audio.get_manager()
    if len(parts) < 2 or not parts[1].strip():
        await answer_markdown_safe(
            message,
            f"🔊 Âm lượng loa ESP32-C3 hiện tại: *{mgr.volume}%* (gõ `/vol 0..100` để chỉnh).",
        )
        return
    try:
        val = int(parts[1].strip().rstrip("%"))
    except ValueError:
        await message.answer("Cú pháp: /vol 60 (từ 0 đến 100).")
        return
    new_v = mgr.set_volume(val)
    await message.answer(f"🔊 Đã đặt âm lượng loa ESP32-C3: {new_v}%.")

async def reply_ai_failure(message: Message, user_id: int, text: str):
    """
    Gửi câu xin lỗi khi gọi AI thất bại; chỉ lưu DB nếu Telegram gửi thành công.
    """
    try:
        await message.answer(text)
    except Exception as send_err:
        logger.error(f"Không gửi được câu xin lỗi: {send_err}")
        return
    try:
        database.add_chat_message(user_id, "model", text, 0)
        database.update_user_state(user_id, is_bot_msg=True)
    except Exception as db_err:
        logger.error(f"Không lưu được câu xin lỗi vào DB: {db_err}")

async def _execute_media_turn(
    user_id: int,
    message: Message,
    media_items: list,
    caption: str,
    prev_last_user_time: float,
    t_start: float,
    history_label: str = "ảnh",
    video_uri: str = None,
    custom_prompt_text: str = None,
):
    """Lưu lịch sử lượt gửi media và gọi Gemini tuần tự theo user_id lock (ảnh đơn, album, sticker, GIF)."""
    count = len(media_items)
    if custom_prompt_text:
        user_msg_for_mem = custom_prompt_text
        prompt_for_ai = custom_prompt_text
    elif count > 1:
        user_msg_for_mem = f"[Gửi album {count} ảnh kèm lời nhắn: {caption}]" if caption else f"[Gửi album {count} ảnh]"
        prompt_for_ai = caption or f"[Người dùng vừa gửi album gồm {count} bức ảnh này]"
    else:
        user_msg_for_mem = f"[Gửi {history_label} kèm lời nhắn: {caption}]" if caption else f"[Gửi một {history_label}]"
        prompt_for_ai = caption

    async with task_helper.get_user_lock(user_id):
        turn_start = time.time()
        prev_state = database.get_or_create_user(user_id)
        latest_prev_user_time = prev_state.get("last_user_message_time", prev_last_user_time)
        database.add_chat_message(user_id, "user", user_msg_for_mem)
        database.update_user_state(user_id, is_user_msg=True)

        try:
            await bot.send_chat_action(chat_id=user_id, action="typing")
        except Exception:
            pass

        try:
            response_data = await gemini_client.chat_with_butler(
                user_id=user_id,
                user_message=prompt_for_ai,
                media_items=media_items,
                video_file_uri=video_uri,
                prev_last_user_time=latest_prev_user_time,
                defer_commit=True,
            )
            api_elapsed = time.time() - turn_start
            await process_ai_response(
                user_id,
                response_data,
                user_message=message,
                api_elapsed_sec=api_elapsed,
                user_text=user_msg_for_mem,
            )
        except Exception as e:
            logger.error(f"Media handle error ({history_label}): {e}", exc_info=True)
            await reply_ai_failure(
                message,
                user_id,
                "Kết nối xử lý ảnh vừa tạm gián đoạn, bạn vui lòng gửi lại giúp mình nhé!",
            )


async def _flush_media_group(group_key: tuple):
    """Chờ hết nhịp gửi album Telegram (1.4s) rồi gộp toàn bộ ảnh vào 1 lượt phản hồi."""
    await asyncio.sleep(1.4)
    entry = MEDIA_GROUP_BUFFER.pop(group_key, None)
    if not entry or not entry.get("items"):
        return
    user_id = group_key[0]
    caption = " ".join(c for c in entry.get("captions", []) if c).strip()
    await _execute_media_turn(
        user_id=user_id,
        message=entry["last_message"],
        media_items=entry["items"],
        caption=caption,
        prev_last_user_time=entry["prev_last_user_time"],
        t_start=entry["t_start"],
        history_label=entry.get("label", "ảnh"),
    )


async def _enqueue_image_message(
    message: Message,
    file_id: str,
    file_size: int = None,
    default_mime: str = "image/jpeg",
    history_label: str = "ảnh",
):
    """Nhận ảnh nén hoặc ảnh gốc dạng file; tự động gom nhóm nếu thuộc 1 album (media_group_id)."""
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    t_start = time.time()
    prev_state = database.get_or_create_user(user_id)
    prev_last_user_time = prev_state.get("last_user_message_time", 0.0)
    caption = (message.caption or "").strip()

    raw_bytes = await media_helper.download_telegram_bytes(bot, file_id, file_size=file_size)
    if not raw_bytes:
        await reply_ai_failure(
            message,
            user_id,
            "Ảnh hơi nặng hoặc kết nối vừa chậm nên chưa tải xuống được, bạn thử gửi ảnh nén nhẹ hơn nhé!",
        )
        return

    mime = media_helper._guess_image_mime(raw_bytes, default_mime=default_mime)
    mgid = getattr(message, "media_group_id", None)
    if not mgid:
        await _execute_media_turn(
            user_id=user_id,
            message=message,
            media_items=[(raw_bytes, mime)],
            caption=caption,
            prev_last_user_time=prev_last_user_time,
            t_start=t_start,
            history_label=history_label,
        )
        return

    group_key = (user_id, str(mgid))
    entry = MEDIA_GROUP_BUFFER.get(group_key)
    if entry is None:
        entry = {
            "items": [],
            "captions": [],
            "last_message": message,
            "prev_last_user_time": prev_last_user_time,
            "t_start": t_start,
            "total_bytes": 0,
            "timer": None,
            "label": history_label,
        }
        MEDIA_GROUP_BUFFER[group_key] = entry

    if len(entry["items"]) < MAX_ALBUM_IMAGES and (entry["total_bytes"] + len(raw_bytes)) <= MAX_ALBUM_BYTES:
        entry["items"].append((raw_bytes, mime))
        entry["total_bytes"] += len(raw_bytes)
    if caption and caption not in entry["captions"]:
        entry["captions"].append(caption)
    entry["last_message"] = message

    old_timer = entry.get("timer")
    if old_timer and not old_timer.done():
        old_timer.cancel()
    entry["timer"] = task_helper.spawn(_flush_media_group(group_key))


@dp.message(F.photo)
async def handle_photo(message: Message):
    """Xử lý khi người dùng gửi ảnh nén (hỗ trợ ảnh đơn và album nhiều ảnh)."""
    photo = message.photo[-1]
    await _enqueue_image_message(
        message,
        file_id=photo.file_id,
        file_size=getattr(photo, "file_size", None),
        default_mime="image/jpeg",
        history_label="ảnh",
    )


@dp.message(F.document, F.document.mime_type.startswith("image/"))
async def handle_image_document(message: Message):
    """Xử lý ảnh gốc không nén gửi dạng File (F.document) và album ảnh gửi dạng File."""
    doc = message.document
    await _enqueue_image_message(
        message,
        file_id=doc.file_id,
        file_size=getattr(doc, "file_size", None),
        default_mime=doc.mime_type or "image/jpeg",
        history_label="ảnh gốc",
    )


@dp.message(F.sticker)
async def handle_sticker(message: Message):
    """Xử lý Sticker Telegram (đọc ảnh .webp hoặc thumbnail sticker động/video cho Gemini hiểu cảm xúc)."""
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    t_start = time.time()
    prev_state = database.get_or_create_user(user_id)
    prev_last_user_time = prev_state.get("last_user_message_time", 0.0)

    media_pair, sticker_desc = await media_helper.sticker_to_media_payload(bot, message.sticker)
    emoji = getattr(message.sticker, "emoji", "") or ""
    history_text = f"[Gửi sticker {emoji}]".strip() if emoji else "[Gửi một sticker]"

    media_list = [media_pair] if media_pair else []
    await _execute_media_turn(
        user_id=user_id,
        message=message,
        media_items=media_list,
        caption="",
        prev_last_user_time=prev_last_user_time,
        t_start=t_start,
        history_label="sticker",
        custom_prompt_text=f"{history_text}\n{sticker_desc}",
    )


@dp.message(F.animation)
async def handle_animation(message: Message):
    """Xử lý ảnh động GIF Telegram (upload clip ngắn <=8MB hoặc đọc thumbnail nhẹ RAM)."""
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    t_start = time.time()
    prev_state = database.get_or_create_user(user_id)
    prev_last_user_time = prev_state.get("last_user_message_time", 0.0)
    caption = (message.caption or "").strip()
    anim = message.animation

    video_uri = None
    media_list = []
    anim_size = getattr(anim, "file_size", None)
    if anim_size is None or anim_size <= 8 * 1024 * 1024:
        raw_mp4 = await media_helper.download_telegram_bytes(bot, anim.file_id, file_size=anim_size, max_bytes=8 * 1024 * 1024)
        if raw_mp4:
            try:
                video_uri = await gemini_client.upload_file_to_gemini(raw_mp4, "video/mp4", "tg_gif")
            except Exception as e:
                logger.warning(f"Upload GIF to Gemini failed, fallback thumbnail: {e}")

    if not video_uri and getattr(anim, "thumbnail", None):
        thumb = anim.thumbnail
        t_bytes = await media_helper.download_telegram_bytes(bot, thumb.file_id, file_size=getattr(thumb, "file_size", None), max_bytes=4 * 1024 * 1024)
        if t_bytes:
            media_list.append((t_bytes, media_helper._guess_image_mime(t_bytes)))

    prompt = f"{caption}\n[Anh vừa gửi một ảnh động GIF]".strip() if caption else "[Anh vừa gửi một ảnh động GIF cho em xem]"
    await _execute_media_turn(
        user_id=user_id,
        message=message,
        media_items=media_list,
        caption=caption,
        prev_last_user_time=prev_last_user_time,
        t_start=t_start,
        history_label="GIF",
        video_uri=video_uri,
        custom_prompt_text=prompt,
    )

@dp.message(F.voice)
async def handle_voice_message(message: Message):
    """Xử lý tin nhắn thoại Telegram (gửi trực tiếp audio .ogg qua Gemini Pass 1 không tốn thêm API)."""
    user_id = message.from_user.id
    if not check_permission(user_id):
        return
    voice = message.voice
    if not voice:
        return

    t_start = time.time()
    prev_state = database.get_or_create_user(user_id)
    prev_last_user_time = prev_state.get("last_user_message_time", 0.0)
    raw_audio = await media_helper.download_telegram_bytes(
        bot,
        voice.file_id,
        file_size=getattr(voice, "file_size", None),
        max_bytes=8 * 1024 * 1024,
    )
    if not raw_audio:
        await reply_ai_failure(
            message,
            user_id,
            "Kết nối vừa chậm nên chưa tải được tin nhắn thoại của bạn, vui lòng gửi lại nhé!",
        )
        return

    dur = int(getattr(voice, "duration", 0) or 0)
    mime = getattr(voice, "mime_type", None) or "audio/ogg"
    butler_name = persona.get_persona_config()["butler_name"]
    voice_prompt = (
        f"[Người dùng vừa gửi tin nhắn thoại dài {dur} giây — hãy nghe trực tiếp đoạn audio đính kèm "
        f"và trả lời chính xác, tự nhiên theo vai {butler_name}.]"
    )
    await _execute_media_turn(
        user_id=user_id,
        message=message,
        media_items=[(raw_audio, mime)],
        caption="",
        prev_last_user_time=prev_last_user_time,
        t_start=t_start,
        history_label=f"tin nhắn thoại ({dur}s)",
        custom_prompt_text=voice_prompt,
    )


@dp.message(Command("focus", "nap", "alarm", "eq"))
async def cmd_speaker_modes(message: Message):
    """Lệnh nhanh điều khiển Phiên tập trung, Chợp mắt, Báo thức và EQ loa 4cm."""
    if not check_permission(message.from_user.id):
        return
    parts = (message.text or "").strip().split()
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg_str = " ".join(parts[1:]).strip()
    mgr = esp32_audio.get_manager()
    mgr.ensure_background_loops()

    if cmd == "focus":
        if arg_str.lower() in ("off", "stop", "huy", "tắt", "dừng"):
            mgr.cancel_timed_session()
            await mgr.stop()
            await message.answer("🛑 Đã dừng phiên Tập trung và tắt nhạc trên loa.")
            return
        tokens = arg_str.split(None, 1) if arg_str else []
        mins = 15
        topic = ""
        if tokens and re.fullmatch(r"\d{1,3}", tokens[0]):
            mins = max(1, min(240, int(tokens[0])))
            topic = tokens[1].strip() if len(tokens) > 1 else ""
        else:
            topic = arg_str
        res = await mgr.start_timed_session("focus", mins, query=topic)
        tr = res.get("track")
        tr_line = (
            f"\n🎵 Đang phát: *{_escape_md_legacy(tr['title'])}*"
            if tr
            else "\n⏳ Đang tìm & tải nhạc tập trung phù hợp..."
        )
        await answer_markdown_safe(
            message,
            f"🎯 Đã bật chế độ *Tập trung {mins} phút* (tạm hoãn tin rủ rỉ để bạn tập trung).{tr_line}",
        )
        return

    if cmd == "nap":
        if arg_str.lower() in ("off", "stop", "huy", "tắt"):
            mgr.cancel_timed_session()
            await mgr.dismiss_alarm()
            await message.answer("🛑 Đã hủy hẹn chợp mắt.")
            return
        mins = int(arg_str) if re.fullmatch(r"\d{1,3}", arg_str) else 20
        mins = max(1, min(240, mins))
        await mgr.start_timed_session("nap", mins, play_music=False)
        butler_name = persona.get_persona_config()["butler_name"]
        await answer_markdown_safe(
            message,
            f"😴 Đã hẹn *chợp mắt {mins} phút* — hết giờ loa sẽ fade-in nhẹ và {butler_name} gọi bạn dậy!",
        )
        return

    if cmd == "alarm":
        low = arg_str.lower()
        if low in ("off", "stop", "tắt"):
            await mgr.dismiss_alarm()
            for a in list(mgr.alarms):
                mgr.cancel_alarm(a["id"])
            await message.answer("⏰ Đã tắt báo thức & xóa lịch báo thức trên loa.")
            return
        if not arg_str:
            alarms = mgr.get_status_snapshot().get("alarms") or []
            if not alarms:
                butler_name = persona.get_persona_config()["butler_name"]
                await answer_markdown_safe(
                    message,
                    f"⏰ Chưa có báo thức hoặc nhạc hẹn giờ nào. Dùng `/alarm 06:30` hoặc nhắn {butler_name} 'mở bài ... lúc 8h sáng'.",
                )
            else:
                lines = "\n".join(
                    f"• {'🎵' if a.get('kind') == 'music' else '⏰'} {_escape_md_legacy(a['time_vn'])} — {_escape_md_legacy(a['label'])}"
                    + (f" ({int(a['duration_min'])}p)" if int(a.get("duration_min") or 0) > 0 else "")
                    for a in alarms
                )
                await answer_markdown_safe(message, f"⏰ *Báo thức & Nhạc hẹn giờ:*\n{lines}")
            return
        try:
            alm = mgr.add_alarm_clock(time_str=arg_str)
            await answer_markdown_safe(
                message,
                f"⏰ Đã đặt báo thức gọi dậy nhẹ nhàng lúc *{_escape_md_legacy(alm['time_vn'])}*!",
            )
        except ValueError:
            await answer_markdown_safe(message, "⚠️ Nhập giờ theo dạng `/alarm 06:30` hoặc `/alarm 7h15` nha.")
        return

    if cmd == "eq":
        if not arg_str:
            eq = mgr.get_eq_settings()
            presets = ", ".join(eq.get("presets", {}).keys())
            await answer_markdown_safe(
                message,
                f"🎛️ *EQ Loa toàn dãy 4cm:* `{_escape_md_legacy(eq['label'])}`\n"
                f"• Cắt High-Pass: `{eq['highpass_hz']} Hz` | Bass: `{eq['bass_db']:+.1f} dB` | Treble: `{eq['treble_db']:+.1f} dB`\n"
                f"• Chọn preset: `/eq loudness` | `/eq warm` | `/eq clear` | `/eq flat` | `/eq off`\n"
                f"({presets})",
            )
            return
        preset = arg_str.split()[0].lower()
        if preset not in ("loudness", "warm", "clear", "flat", "off"):
            await answer_markdown_safe(message, "⚠️ Preset hỗ trợ: `loudness`, `warm`, `clear`, `flat`, `off`.")
            return
        eq = mgr.set_eq_settings(preset=preset)
        await answer_markdown_safe(
            message,
            f"🎛️ Đã chuyển EQ sang *{_escape_md_legacy(eq['label'])}* (HP `{eq['highpass_hz']}Hz`, Bass `{eq['bass_db']:+.1f}dB`, Treble `{eq['treble_db']:+.1f}dB`).",
        )


@dp.message(F.text)
async def handle_text(message: Message):
    """Xử lý tin nhắn văn bản và link TikTok (tuần tự theo lock từng user_id)."""
    user_id = message.from_user.id
    if not check_permission(user_id):
        return

    text = (message.text or "").strip()
    if not text:
        return

    async with task_helper.get_user_lock(user_id):
        t_start = time.time()
        prev_state = database.get_or_create_user(user_id)
        prev_last_user_time = prev_state.get("last_user_message_time", 0.0)
        database.add_chat_message(user_id, "user", text)
        database.update_user_state(user_id, is_user_msg=True)

        # Kiểm tra xem có chứa link TikTok không
        tiktok_url = media_helper.extract_tiktok_url(text)
        video_uri = None
        user_prompt_text = text

        if tiktok_url:
            logger.info(f"Phát hiện link TikTok: {tiktok_url}")
            try:
                await bot.send_chat_action(chat_id=user_id, action="typing")
            except Exception:
                pass

            tiktok_data = await media_helper.fetch_tiktok_data(tiktok_url)
            if tiktok_data:
                title = tiktok_data.get("title", "")
                author = tiktok_data.get("author", "")
                music = tiktok_data.get("music", "")
                logger.info(f"TikTok fetched: Title={title}, Author={author}")

                user_prompt_text = (
                    f"{text}\n\n"
                    f"[Người dùng vừa gửi cho bạn xem 1 video TikTok:\n"
                    f"- Kênh tác giả: '{author}'\n"
                    f"- Nhạc nền: '{music}'\n"
                    f"- Tiêu đề/Nội dung caption: '{title}']\n"
                    f"Hãy xem nội dung và đưa ra bình luận/cảm nhận thật tự nhiên theo tính cách của bạn!"
                )

                play_url = tiktok_data.get("play_url")
                if play_url:
                    v_bytes = await media_helper.download_video_bytes(play_url)
                    if v_bytes:
                        try:
                            video_uri = await gemini_client.upload_file_to_gemini(v_bytes, "video/mp4", "tiktok_clip")
                            logger.info(f"Upload video lên Gemini thành công: {video_uri}")
                        except Exception as e:
                            logger.warning(f"Upload clip to Gemini failed: {e}")

        # Kiểm tra các link web / bài báo / YouTube ngoài TikTok
        web_urls = web_helper.extract_web_urls(text)
        if web_urls:
            try:
                await bot.send_chat_action(chat_id=user_id, action="typing")
            except Exception:
                pass
            fetched = await asyncio.gather(*(web_helper.fetch_url_context(u) for u in web_urls), return_exceptions=True)
            url_notes = []
            for item in fetched:
                if isinstance(item, dict) and item.get("summary"):
                    url_notes.append(f"- Link ({item.get('url')}):\n{item['summary']}")
            if url_notes:
                user_prompt_text = (
                    f"{user_prompt_text}\n\n"
                    f"[Nội dung tóm tắt từ link người dùng vừa gửi để trợ lý đọc hiểu và phản hồi:\n"
                    + "\n".join(url_notes)
                    + "]"
                )

        # AI-First Device Control: KHÔNG dùng Regex tự bật/tắt thiết bị trước LLM để tránh hiểu sai ngữ cảnh
        # (vd câu kể lại, phủ định, hỏi thăm hay dạy thói quen). Pass 1 sẽ đọc trọn ngữ cảnh và quyết định
        # `fan_action` / `plug_action` / `music_action` / `habit_action`, sau đó thực thi ngay trước Pass 2 & Pass 3.
        sleep_prompt_note = ""
        try:
            sleep_prompt_note = fan_controller.get_controller().build_sleep_prompt_context_note()
        except Exception as fan_err:
            logger.warning(f"Lỗi đọc ngữ cảnh giờ ngủ Quạt BLDC: {fan_err}")

        extra_turn_note = sleep_prompt_note or None

        try:
            response_data = await gemini_client.chat_with_butler(
                user_id=user_id,
                user_message=user_prompt_text,
                video_file_uri=video_uri,
                prev_last_user_time=prev_last_user_time,
                extra_system_note=extra_turn_note,
                defer_commit=True,
            )
            api_elapsed = time.time() - t_start
            await process_ai_response(
                user_id, response_data, user_message=message,
                api_elapsed_sec=api_elapsed, user_text=text,
            )
        except Exception as e:
            logger.error(f"Text handle error: {e}", exc_info=True)
            await reply_ai_failure(message, user_id, "Kết nối AI vừa tạm gián đoạn một chút, bạn nhắn lại giúp mình nhé!")

async def process_ai_response(user_id: int, response_data: dict, user_message: Message = None,
                              api_elapsed_sec: float = 0.0, user_text: str = "",
                              jukebox_handled: bool = False,
                              fan_handled: bool = False,
                              plug_handled: bool = False):
    """Xử lý phản hồi từ Gemini: Cập nhật điểm, trí nhớ, thả reaction, gửi ảnh và tin trễ tự nhiên."""
    import sampling_helper
    now_ts = time.time()

    # Lưu vết timestamp trước khi cập nhật trạng thái bot để tính Conversational Momentum chính xác
    prev_state = database.get_or_create_user(user_id)
    prev_last_bot_ts = prev_state.get("last_bot_message_time", 0.0)
    prev_last_user_ts = prev_state.get("last_user_message_time", 0.0)

    messages = response_data.get("messages", [])
    delta = response_data.get("delta_affinity", 0)
    new_mood = response_data.get("new_mood", "bình thường")
    new_fact = response_data.get("new_fact_learned")
    
    # 1. Thả emoji reaction qua Weighted Random Sampling (Áp dụng phạt hệ số 0.7 nếu vừa react trong 3 phút)
    reacted_recently = (now_ts - LAST_REACTION_TIME.get(user_id, 0)) < 180
    chosen_reaction = sampling_helper.resolve_reaction_choice(response_data, reacted_recently=reacted_recently)
    
    if chosen_reaction and user_message:
        try:
            from aiogram.types import ReactionTypeEmoji
            await bot.set_message_reaction(
                chat_id=user_id,
                message_id=user_message.message_id,
                reaction=[ReactionTypeEmoji(emoji=chosen_reaction)]
            )
            LAST_REACTION_TIME[user_id] = now_ts
            logger.info(f"Thả reaction '{chosen_reaction}' vào tin nhắn user {user_id}")
        except Exception as e:
            logger.warning(f"Reaction failed: {e}")

    # 2. Tính điểm dự kiến để cân nhịp phản hồi; chỉ ghi DB chính thức nếu gửi Telegram thành công
    projected_score = max(0, min(100, int(prev_state.get("affinity_score", 0)) + int(delta or 0)))

    # 2.5. AI DJ ngữ cảnh: fallback thực thi music_action nếu chưa pre-executed sau Pass 1
    if not jukebox_handled and not response_data.get("_pre_executed_music") and isinstance(response_data.get("music_action"), dict):
        try:
            ai_ma_note = await esp32_audio.execute_ai_music_action(response_data["music_action"])
            if ai_ma_note:
                logger.info(f"🎧 [AI DJ]: {response_data['music_action']} -> executed")
        except Exception as ma_err:
            logger.warning(f"Lỗi thực thi AI music_action: {ma_err}")

    # 2.6. AI Smart Fan: fallback thực thi fan_action nếu chưa pre-executed sau Pass 1
    if not fan_handled and not response_data.get("_pre_executed_fan") and isinstance(response_data.get("fan_action"), dict):
        try:
            ai_fa_note = await fan_controller.get_controller().execute_ai_fan_action(response_data["fan_action"])
            if ai_fa_note:
                logger.info(f"🌬️ [AI Fan]: {response_data['fan_action']} -> executed")
        except Exception as fa_err:
            logger.warning(f"Lỗi thực thi AI fan_action: {fa_err}")

    # 2.7. AI Smart Plug: fallback thực thi plug_action nếu chưa pre-executed sau Pass 1
    plug_act_fallback = response_data.get("plug_action")
    if not plug_handled and not response_data.get("_pre_executed_plug") and (
        isinstance(plug_act_fallback, dict)
        or (isinstance(plug_act_fallback, list) and any(isinstance(x, dict) for x in plug_act_fallback))
    ):
        try:
            ai_pa_note = await smart_plugs.get_controller().execute_ai_plug_action(plug_act_fallback)
            if ai_pa_note:
                logger.info(f"🔌 [AI Plug]: {plug_act_fallback} -> executed")
        except Exception as pa_err:
            logger.warning(f"Lỗi thực thi AI plug_action: {pa_err}")

    # 2.8. Smart Habits: fallback nếu chưa pre-executed sau Pass 1
    if not response_data.get("_pre_executed_habit") and isinstance(response_data.get("habit_action"), dict):
        try:
            import habit_engine
            ai_ha_note = habit_engine.execute_ai_habit_action(response_data["habit_action"])
            if ai_ha_note:
                logger.info(f"🧠 [AI Habit]: {response_data['habit_action']} -> {ai_ha_note}")
        except Exception as ha_err:
            logger.warning(f"Lỗi thực thi AI habit_action: {ha_err}")

    # 2.9. Router Wi-Fi OpenWrt: fallback nếu chưa pre-executed sau Pass 1
    if not response_data.get("_pre_executed_router") and isinstance(response_data.get("router_action"), dict):
        try:
            import router_controller
            ai_ra_note = await router_controller.get_controller().execute_ai_router_action(response_data["router_action"])
            if ai_ra_note:
                logger.info(f"🌐 [AI Router]: {response_data['router_action']} -> {ai_ra_note}")
        except Exception as ra_err:
            logger.warning(f"Lỗi thực thi AI router_action: {ra_err}")

    # 3. Smart Butler không dùng kho ảnh cá nhân dựng sẵn
    photo_path_to_send = None
    chosen_photo_key = None

    # Lấy nội dung voice riêng (đoạn thoại tâm sự 2-4 câu, giữ nguyên các bong bóng chữ bổ trợ)
    voice_text_to_send = None
    if response_data.get("voice_decision"):
        raw_vt = response_data.get("voice_text")
        if isinstance(raw_vt, str) and raw_vt.strip():
            voice_text_to_send = raw_vt.strip()
        elif messages:
            voice_text_to_send = messages[0].strip()

    # 4. Tính toán độ trễ phản hồi qua Weighted Random Sampling & Conversational Momentum (có khấu trừ thời gian API)
    latency_hint = response_data.get("latency_hint")
    base_delay = sampling_helper.resolve_latency_choice(
        last_bot_msg_time=prev_last_bot_ts,
        last_user_msg_time=prev_last_user_ts,
        affinity_score=projected_score,
        latency_hint=latency_hint,
        api_elapsed_sec=api_elapsed_sec
    )

    # Gửi đồng bộ trong lượt lock trước khi ghi lịch sử DB + ký ức để bảo toàn thứ tự & tránh lưu tin chưa gửi được
    import tts_client
    voice_tod = tts_client.get_time_of_day_bucket() if voice_text_to_send else None
    voice_intensity = (
        tts_client.compute_contextual_voice_intensity(
            mood=new_mood,
            time_of_day=voice_tod,
            text=voice_text_to_send,
            affinity_score=projected_score,
        )
        if voice_text_to_send
        else None
    )
    delivery = await scheduler.send_natural_multi_bubble(
        bot,
        user_id,
        messages,
        base_delay=base_delay,
        photo_path=photo_path_to_send,
        voice_text=voice_text_to_send,
        voice_mood=new_mood,
        voice_time_of_day=voice_tod,
        voice_intensity=voice_intensity,
    )
    if not delivery.get("delivered"):
        logger.warning(f"Không gửi được phản hồi cho user {user_id} ra Telegram -> bỏ qua lưu lịch sử & ký ức.")
        return

    gemini_client.commit_deferred_side_effects(response_data)
    database.update_user_state(
        user_id,
        affinity_delta=delta,
        new_mood=new_mood,
        is_bot_msg=True,
    )
    if delivery.get("sent_photo") and chosen_photo_key:
        database.record_sent_photo(user_id, chosen_photo_key)

    sent_texts = delivery.get("sent_texts") or []
    combined_msg = " ".join(sent_texts).strip()
    sent_vt = delivery.get("voice_text")
    memory_bot_msg = combined_msg
    if sent_vt and sent_vt.lower() not in combined_msg.lower():
        memory_bot_msg = f"{combined_msg} | [Voice note]: {sent_vt}" if combined_msg else f"[Voice note]: {sent_vt}"
    elif not memory_bot_msg and delivery.get("sent_photo"):
        memory_bot_msg = "[Đã gửi 1 tấm ảnh]"

    if memory_bot_msg:
        database.add_chat_message(user_id, "model", memory_bot_msg, delta)

    # Xử lý & tiến hóa Trí nhớ Vector ngay sau lượt nhắn (Episodic Turn + Tóm tắt ảnh anh gửi)
    raw_user_text = user_text or (user_message.text.strip() if user_message and user_message.text else "")

    import proactive_engine
    try:
        proactive_engine.on_user_turn(
            user_id=user_id,
            delta_affinity=delta,
            mood=new_mood,
            user_text=raw_user_text,
            bot_text=memory_bot_msg or combined_msg,
            now_ts=now_ts,
        )
    except Exception as e:
        logger.debug(f"Jiwen on_user_turn bỏ qua: {e}")

    img_summary = response_data.get("image_memory_summary")
    hint_parts = []
    if isinstance(img_summary, str) and len(img_summary.strip()) >= 5 and img_summary.strip().lower() != "null":
        hint_parts.append(img_summary.strip())
    if isinstance(new_fact, str) and len(new_fact.strip()) >= 5 and new_fact.strip().lower() != "null":
        if not hint_parts or new_fact.strip() != hint_parts[0]:
            hint_parts.append(new_fact.strip())
    hint_fact = " | ".join(hint_parts) if hint_parts else None

    if memory_bot_msg or raw_user_text:
        import vector_memory
        task_helper.spawn(
            vector_memory.process_turn_memory(
                user_id=user_id,
                user_msg=raw_user_text,
                bot_msg=memory_bot_msg,
                hint_fact=hint_fact,
                timestamp=now_ts,
            )
        )

async def main():
    database.init_db()
    logger.info("Khởi chạy Smart Butler AI & Smart Home Hub...")

    # Nạp bù vector embedding cho các ký ức cũ (nếu có)
    import vector_memory
    import lan_presence
    import lan_watchdog
    import backup_helper
    import web_dashboard
    import habit_engine
    import router_controller

    task_helper.spawn(vector_memory.backfill_missing_embeddings())

    # Lấy thông tin user ID cho scheduler chạy nền (tự động fallback lấy từ DB nếu ALLOWED_USER_ID=0)
    target_user = _resolve_bound_owner_id()

    # Backup khi khởi động: tạo nếu chưa có bản nào HOẶC bản mới nhất đã cũ hơn 24h
    # (tránh trường hợp bot restart sau 03:00 làm mất slot backup hằng ngày trong ngày đó)
    backups = backup_helper.list_backups()
    latest_backup_ts = backups[0]["created_at"] if backups else 0.0
    if not backups or (time.time() - float(latest_backup_ts)) > 24 * 3600:
        task_helper.spawn(backup_helper.create_backup(tag="boot"))

    task_helper.spawn(lan_presence.presence_loop(target_user_id=target_user), daemon=True)
    task_helper.spawn(lan_watchdog.watchdog_loop(bot=bot, target_user_id=target_user), daemon=True)
    task_helper.spawn(fan_controller.fan_monitor_loop(bot=bot, target_user_id=target_user), daemon=True)
    task_helper.spawn(habit_engine.habit_monitor_loop(bot=bot, target_user_id=target_user), daemon=True)
    task_helper.spawn(router_controller.router_monitor_loop(bot=bot, target_user_id=target_user), daemon=True)
    task_helper.spawn(scheduler.proactive_scheduler_loop(bot, target_user), daemon=True)

    dashboard_runner = await web_dashboard.start_dashboard(bot=bot, target_user_id=target_user)

    try:
        await dp.start_polling(bot)
    finally:
        await web_dashboard.stop_dashboard(dashboard_runner)
        await router_controller.get_controller().close()
        await esp32_audio.get_manager().shutdown()
        await task_helper.shutdown(timeout_sec=4.0)
        await gemini_client.close_session()

if __name__ == "__main__":
    asyncio.run(main())
