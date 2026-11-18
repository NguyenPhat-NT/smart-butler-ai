# -*- coding: utf-8 -*-
import os
import asyncio
import random
import time
import datetime
import logging
from typing import Any, Dict, List, Optional
from aiogram import Bot
import config
import database
import gemini_client
import trend_helper
import task_helper
import lan_events
import lan_presence
import backup_helper
import proactive_engine
import esp32_audio
import persona
from typo_helper import maybe_inject_typo

logger = logging.getLogger(__name__)
VN_TZ = datetime.timezone(datetime.timedelta(hours=getattr(config, "TIMEZONE_OFFSET_HOURS", 7)))

# Lưu vết các khung giờ đã chủ động nhắn trong ngày để tránh trùng lặp
PROACTIVE_SLOTS_SENT = set()
_LAST_ARRIVAL_WAKE_AT = 0.0

async def send_natural_multi_bubble(
    bot: Bot,
    chat_id: int,
    messages: List[str],
    base_delay: int = 8,
    photo_path: Optional[str] = None,
    voice_text: Optional[str] = None,
    voice_mood: Optional[str] = None,
    voice_time_of_day: Optional[str] = None,
    voice_intensity: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Gửi tin nhắn (và ảnh/voice nếu có) với độ trễ tự nhiên, gõ nhầm tự sửa (*đang) và hiệu ứng 'đang soạn tin...'.
    Thứ tự: ảnh -> các bong bóng chữ -> voice note (voice gửi sau để máy PC bật TTS chậm không làm trễ tin nhắn chữ).
    Trả về báo cáo những phần thực sự gửi thành công ra Telegram.
    """
    report: Dict[str, Any] = {
        "delivered": False,
        "sent_texts": [],
        "sent_photo": False,
        "sent_voice": False,
        "voice_text": None,
    }
    if not messages and not photo_path and not voice_text:
        return report

    # Chờ độ trễ tự nhiên ban đầu (đọc tin nhắn + suy nghĩ)
    await asyncio.sleep(base_delay)

    # Nếu có ảnh kèm theo trong lượt phản hồi, gửi ảnh ngay trước các bong bóng chữ để liền mạch
    if photo_path and os.path.exists(photo_path):
        from aiogram.types import FSInputFile
        try:
            await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
            await asyncio.sleep(1.2)
            await bot.send_photo(chat_id=chat_id, photo=FSInputFile(photo_path))
            report["sent_photo"] = True
            logger.info(f"Đã gửi ảnh album chuẩn đời thực đồng bộ cùng tin nhắn: {photo_path}")
        except Exception as e:
            logger.error(f"Lỗi gửi ảnh album: {e}")

    # Khởi tạo tác vụ tổng hợp voice song song (nhưng CHỈ gửi sau khi xong tin nhắn chữ)
    clean_voice = voice_text.strip() if isinstance(voice_text, str) else ""
    voice_audio = None
    voice_fmt = "ogg"
    clean_messages = [m.strip() for m in (messages or []) if isinstance(m, str) and m.strip()]
    tts_task = None
    deferred_fallback_check = False
    fallback_plain = ""
    voice_mem_text = ""
    norm_voice = " ".join(clean_voice.lower().split()) if clean_voice else ""

    if clean_voice:
        import tts_client
        fallback_plain = tts_client.strip_speech_markup(clean_voice, keep_cues=False) or clean_voice
        voice_mem_text = tts_client.strip_speech_markup(clean_voice, keep_cues=True) or fallback_plain
        norm_voice = " ".join(fallback_plain.lower().split())
        tts_task = asyncio.create_task(
            tts_client.synthesize_voice(
                clean_voice,
                timeout_sec=60.0,
                mood=voice_mood,
                time_of_day=voice_time_of_day,
                intensity=voice_intensity,
            )
        )
        has_exact_dup = any(" ".join(m.lower().split()) == norm_voice for m in clean_messages)
        if has_exact_dup:
            # Nếu bong bóng chữ trùng nguyên văn câu voice -> chờ TTS trước để biết nên bỏ câu trùng hay giữ làm fallback
            try:
                tts_res = await tts_task
            except Exception as e:
                logger.warning(f"TTS task error: {e}")
                tts_res = None
            tts_task = None
            if tts_res:
                voice_audio, voice_fmt = tts_res
                clean_messages = [
                    m for m in clean_messages
                    if " ".join(m.lower().split()) != norm_voice
                ]
            else:
                joined_norm = " ".join(" ".join(m.split()) for m in clean_messages).lower()
                if norm_voice not in joined_norm:
                    clean_messages = [fallback_plain] + clean_messages
        else:
            deferred_fallback_check = True

    # Mô phỏng phản xạ gõ nhầm tự nhiên (15% cơ hội) cho tin nhắn chữ mà không làm bẩn bản sạch lưu lịch sử
    if clean_messages:
        send_messages, correction = maybe_inject_typo(clean_messages, chance=0.15)
    else:
        send_messages, correction = [], None

    waited_record_voice = False
    try:
        for i, msg in enumerate(send_messages):
            msg = msg.strip()
            if not msg:
                continue

            # Gửi typing action giả lập người thật gõ phím
            typing_duration = min(5, max(2, len(msg) // 18))
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logger.warning(f"send_chat_action error: {e}")

            await asyncio.sleep(typing_duration)

            try:
                await bot.send_message(chat_id=chat_id, text=msg)
                orig_msg = clean_messages[i] if i < len(clean_messages) else msg
                report["sent_texts"].append(orig_msg)
            except Exception as e:
                logger.error(f"Failed to send message bubble: {e}")

            # Nghỉ ngắn giữa các câu bong bóng chat
            if i < len(send_messages) - 1:
                await asyncio.sleep(random.uniform(1.5, 2.8))

        # Nếu có câu đính chính từ gõ nhầm (ví dụ: *đang), gửi ngay sau đó 1.2 - 2.0s
        if correction and report["sent_texts"]:
            await asyncio.sleep(random.uniform(1.2, 2.0))
            try:
                await bot.send_message(chat_id=chat_id, text=correction)
            except Exception as e:
                logger.error(f"Failed to send typo correction: {e}")

        # Chờ phần còn lại của TTS (nếu chạy nền song song với tin nhắn chữ)
        if tts_task is not None:
            record_wait_start = time.monotonic()
            while not tts_task.done():
                waited_record_voice = True
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="record_voice")
                except Exception:
                    pass
                await asyncio.wait({tts_task}, timeout=4.2)

            try:
                tts_res = await tts_task
            except Exception as e:
                logger.warning(f"TTS task error: {e}")
                tts_res = None
            tts_task = None

            if tts_res:
                voice_audio, voice_fmt = tts_res
                if waited_record_voice and (time.monotonic() - record_wait_start) < 0.8:
                    waited_record_voice = False
            elif deferred_fallback_check and clean_voice:
                joined_norm = " ".join(" ".join(m.split()) for m in (report["sent_texts"] or [])).lower()
                if norm_voice not in joined_norm:
                    fb_msg = fallback_plain or clean_voice
                    try:
                        await bot.send_message(chat_id=chat_id, text=fb_msg)
                        report["sent_texts"].append(fb_msg)
                    except Exception as send_err:
                        logger.error(f"Fallback voice->text thất bại: {send_err}")
    finally:
        if tts_task is not None and not tts_task.done():
            tts_task.cancel()
            try:
                await tts_task
            except (asyncio.CancelledError, Exception):
                pass

    # Gửi voice note SAU CÙNG để không chặn tin nhắn chữ
    if voice_audio:
        try:
            from aiogram.types import BufferedInputFile
            if not waited_record_voice:
                await bot.send_chat_action(chat_id=chat_id, action="record_voice")
                await asyncio.sleep(random.uniform(0.8, 1.5))
            if voice_fmt == "ogg":
                await bot.send_voice(chat_id=chat_id, voice=BufferedInputFile(voice_audio, filename="butler_voice.ogg"))
            else:
                await bot.send_audio(chat_id=chat_id, audio=BufferedInputFile(voice_audio, filename=f"butler_voice.{voice_fmt}"))
            database.set_metadata(f"last_voice_ts:{chat_id}", str(time.time()))
            report["sent_voice"] = True
            report["voice_text"] = voice_mem_text or clean_voice
            logger.info(f"🎙️ Đã gửi voice note ({len(voice_audio)} bytes, {voice_fmt})")
            mgr = esp32_audio.get_manager()
            if mgr.is_voice_autoplay_enabled() and mgr.is_esp32_online() and not lan_presence.is_quiet_hours_now():
                task_helper.spawn(
                    mgr.enqueue_voice_overlay(voice_audio, voice_fmt or "ogg")
                )
        except Exception as e:
            logger.warning(f"Gửi Telegram voice thất bại: {e}")
            # Voice lỗi thì ít nhất gửi nội dung đó dưới dạng chữ (đã lột markup thoại)
            try:
                fb_msg = fallback_plain or clean_voice
                await bot.send_message(chat_id=chat_id, text=fb_msg)
                report["sent_texts"].append(fb_msg)
            except Exception as send_err:
                logger.error(f"Fallback voice->text thất bại: {send_err}")

    report["delivered"] = bool(report["sent_photo"] or report["sent_texts"] or report["sent_voice"])

    # Chỉ cập nhật timestamp hoàn tất gửi tin của bot nếu gửi thành công ít nhất 1 nội dung
    if report["delivered"]:
        try:
            database.update_user_state(chat_id, is_bot_msg=True)
        except Exception:
            pass
    return report

async def _maybe_catchup_yesterday_audit(target_user_id: int):
    """Nếu hôm qua bot tắt trước 23:30 (chưa kịp Audit trí nhớ), tự chạy bù 1 lần."""
    if target_user_id <= 0:
        return
    try:
        vn_now = datetime.datetime.now(VN_TZ)
        yesterday_str = (vn_now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        last_done = database.get_metadata(f"nightly_audit_last_date:{target_user_id}")
        if last_done is None or last_done < yesterday_str:
            import vector_memory
            logger.info(f"Phát hiện chưa Audit trí nhớ ngày {yesterday_str} (last_done={last_done}) -> chạy bù...")
            task_helper.spawn(vector_memory.consolidate_nightly_memories(target_user_id, target_date_str=yesterday_str))
    except Exception as e:
        logger.warning(f"Lỗi kiểm tra catch-up Nightly Audit: {e}")


async def _resolve_photo_file_for_send(
    user_id: int,
    photo_category: str = None,
    photo_key: str = None,
):
    """Smart Butler không dùng kho ảnh cá nhân dựng sẵn."""
    return None, None


async def _dispatch_proactive_messages_locked(
    bot: Bot,
    target_user_id: int,
    messages: List[str],
    photo_category: str = None,
    new_mood: str = None,
    count_as_unanswered: bool = False,
    base_delay: int = 5,
    voice_text: str = None,
    notify_title: Optional[str] = None,
    speak_notify_tts: bool = False,
    photo_key: str = None,
) -> bool:
    """Thực thi gửi tin nhắn chủ động khi caller ĐÃ giữ `get_user_lock(target_user_id)`."""
    if not messages and not voice_text:
        return False

    butler_name = persona.get_persona_config()["butler_name"]
    effective_notify_title = notify_title or f"{butler_name} vừa nhắn"

    photo_path, chosen_key = await _resolve_photo_file_for_send(
        target_user_id, photo_category=photo_category, photo_key=photo_key
    )
    import tts_client
    voice_tod = tts_client.get_time_of_day_bucket() if voice_text else None
    voice_intensity = (
        tts_client.compute_contextual_voice_intensity(
            mood=new_mood,
            time_of_day=voice_tod,
            text=voice_text,
        )
        if voice_text
        else None
    )
    delivery = await send_natural_multi_bubble(
        bot,
        target_user_id,
        messages,
        base_delay=base_delay,
        photo_path=photo_path,
        voice_text=voice_text,
        voice_mood=new_mood,
        voice_time_of_day=voice_tod,
        voice_intensity=voice_intensity,
    )
    if not delivery.get("delivered"):
        logger.warning(f"Tin nhắn chủ động cho user {target_user_id} không gửi được ra Telegram -> bỏ qua lưu DB.")
        return False

    if delivery.get("sent_photo") and chosen_key:
        database.record_sent_photo(target_user_id, chosen_key)

    sent_texts = delivery.get("sent_texts") or []
    combined_text = " ".join(sent_texts).strip()
    sent_vt = delivery.get("voice_text")
    if sent_vt and sent_vt.lower() not in combined_text.lower():
        combined_text = f"{combined_text} | [Voice note]: {sent_vt}" if combined_text else f"[Voice note]: {sent_vt}"
    elif not combined_text and delivery.get("sent_photo"):
        combined_text = "[Đã gửi 1 tấm ảnh]"

    if combined_text:
        database.add_chat_message(target_user_id, "model", combined_text, 0)
    database.update_user_state(target_user_id, new_mood=new_mood, is_bot_msg=True)
    if count_as_unanswered:
        database.increment_unanswered_proactive(target_user_id)

    lan_events.emit_notify(
        title=effective_notify_title,
        body=(sent_texts[0] if sent_texts else (sent_vt or f"{butler_name} vừa gửi thông báo")),
        level="info",
        source="scheduler",
        speak_tts=speak_notify_tts,
    )
    return True


async def _dispatch_proactive_messages(
    bot: Bot,
    target_user_id: int,
    messages: List[str],
    photo_category: str = None,
    new_mood: str = None,
    count_as_unanswered: bool = False,
    base_delay: int = 5,
    voice_text: str = None,
    notify_title: Optional[str] = None,
    speak_notify_tts: bool = False,
    photo_key: str = None,
) -> bool:
    """Gửi tin nhắn/ảnh/voice chủ động ra Telegram rồi mới lưu lịch sử khi giao thành công."""
    if not messages and not voice_text:
        return False

    async with task_helper.get_user_lock(target_user_id):
        return await _dispatch_proactive_messages_locked(
            bot=bot,
            target_user_id=target_user_id,
            messages=messages,
            photo_category=photo_category,
            new_mood=new_mood,
            count_as_unanswered=count_as_unanswered,
            base_delay=base_delay,
            voice_text=voice_text,
            notify_title=notify_title,
            speak_notify_tts=speak_notify_tts,
            photo_key=photo_key,
        )


def _mood_for_speaker_event(kind: str) -> tuple[str, Optional[str]]:
    """Chọn mood & khung giờ phù hợp cho lời nhắc / tổng kết trên loa ESP32."""
    k = str(kind or "focus_done").strip().lower()
    vn_now = datetime.datetime.now(VN_TZ)
    quiet_now = lan_presence.is_quiet_hours_now()
    # Nếu user đặt quiet_end_hour muộn (vd 09:00) nhưng chủ động đặt báo thức / nhạc gọi dậy từ 05:30 sáng -> nói giọng sáng
    is_morning_wake_override = (
        k in ("alarm_wake", "scheduled_music")
        and ((vn_now.hour == 5 and vn_now.minute >= 30) or (6 <= vn_now.hour < 12))
    )
    use_night_whisper = quiet_now and not is_morning_wake_override
    tod: Optional[str] = "late_night" if use_night_whisper else ("morning" if is_morning_wake_override else None)
    if k in ("nap_wake", "alarm_wake", "user_reminder", "scheduled_music"):
        return ("thì thầm khuya" if use_night_whisper else "ngọt ngào"), tod
    if k == "chill_done":
        return ("thì thầm khuya" if use_night_whisper else "bình yên"), tod
    return ("bình yên" if use_night_whisper else "hào hứng"), tod


async def _maybe_speak_reminder_on_esp32(spoken_text: str):
    """Đọc nhắc hẹn qua loa ESP32-C3 (ducking nhạc nền) khi chưa phải giờ yên lặng."""
    clean = str(spoken_text or "").strip()
    if not clean:
        return
    try:
        import tts_client
        mood_hint, tod_hint = _mood_for_speaker_event("user_reminder")
        inten = tts_client.compute_contextual_voice_intensity(mood=mood_hint, time_of_day=tod_hint, text=clean)
        res = await tts_client.synthesize_voice(
            clean, timeout_sec=24.0, mood=mood_hint, time_of_day=tod_hint, intensity=inten
        )
        if res:
            audio_b, fmt = res
            await esp32_audio.get_manager().enqueue_voice_overlay(audio_b, fmt or "ogg")
    except Exception as e:
        logger.debug(f"Đọc reminder trên loa ESP32 bỏ qua: {e}")


async def _build_speaker_event_text(event: Dict[str, Any]) -> tuple[str, str, str]:
    """Tạo câu tổng kết / đánh thức ngắn gọn và xác định loại chuông cho sự kiện loa ESP32."""
    kind = str(event.get("kind") or "focus_done")
    label = str(event.get("label") or "").strip()
    duration_min = int(event.get("duration_min") or 0)

    p_cfg = persona.get_persona_config()
    butler_name = p_cfg["butler_name"]
    owner_name = p_cfg["owner_name"]
    self_ref = p_cfg["self_ref"]
    if kind == "nap_wake":
        chime_kind = "wake_alarm"
        notify_title = "⏰ Hết giờ chợp mắt"
        fallback = (
            f"Đã hết {duration_min} phút chợp mắt rồi {owner_name} ơi! Uống ngụm nước cho tỉnh táo nhé."
            if duration_min > 0
            else f"Tới giờ rồi {owner_name} ơi, hết giờ chợp mắt rồi nhé!"
        )
        prompt_goal = f"gọi {owner_name} dậy nhẹ nhàng sau giấc chợp mắt {duration_min or 20} phút"
    elif kind == "alarm_wake":
        chime_kind = "wake_alarm"
        notify_title = f"⏰ {label or 'Báo thức loa ESP32'}"
        fallback = f"Chào buổi sáng {owner_name} ({label or 'đến giờ hẹn'})! Vươn vai một chút cho tỉnh táo nhé."
        prompt_goal = f"đánh thức {owner_name} dậy vào buổi sáng / giờ hẹn ({label or 'báo thức'}) thật dễ chịu"
    elif kind == "scheduled_music":
        chime_kind = "ping"
        time_vn = str(event.get("time_vn") or "").strip()
        track_desc = str(event.get("track_title") or event.get("query") or label or "nhạc hợp gu").strip()
        notify_title = f"🎵 Nhạc hẹn giờ {time_vn}".strip()
        dur_suffix = f" phát trong {duration_min} phút" if duration_min > 0 else ""
        fallback = (
            f"Đã tới {time_vn or 'giờ hẹn'}, {self_ref} bật bài \"{track_desc}\"{dur_suffix} cho {owner_name} nhé."
        )
        prompt_goal = (
            f"báo {owner_name} là đã tới {time_vn or 'giờ hẹn'} và đã bật bài '{track_desc}'"
            f"{dur_suffix}"
        )
    elif kind == "chill_done":
        chime_kind = "focus_done"
        notify_title = "🎧 Kết thúc phiên thư giãn"
        fallback = (
            f"Đã xong {duration_min} phút nghe nhạc thư giãn, cần {butler_name} mở gì tiếp cứ báo nhé."
            if duration_min > 0
            else "Đã xong phiên nghe nhạc thư giãn rồi nhé!"
        )
        prompt_goal = f"báo nhẹ là phiên nghe nhạc thư giãn {duration_min or 15} phút vừa kết thúc"
    else:
        chime_kind = "focus_done"
        notify_title = "✅ Hoàn thành phiên tập trung"
        fallback = (
            f"Hết {duration_min} phút tập trung rồi {owner_name}! Nghỉ tay uống ngụm nước với duỗi lưng một chút nhé."
            if duration_min > 0
            else f"Hết phiên tập trung rồi {owner_name}, nghỉ ngơi một chút nhé!"
        )
        prompt_goal = f"nhắc {owner_name} nghỉ tay uống nước sau {duration_min or 15} phút tập trung xong"

    text = fallback
    try:
        import config
        if getattr(config, "GEMINI_API_KEYS", None):
            prompt = (
                f"Viết đúng 1-2 câu tiếng Việt thật ngắn (dưới 28 từ) để trợ lý {butler_name} "
                f"(xưng '{self_ref}', gọi {owner_name}) {prompt_goal}. Phong cách {p_cfg['tone_desc']}, không emoji."
            )
            ai_res = await asyncio.wait_for(
                gemini_client.call_gemini_api(
                    model=config.CHAT_MODEL,
                    system_instruction=f"Bạn là trợ lý {butler_name}, nhắn câu ngắn gọn qua loa thông minh.",
                    contents=[{"role": "user", "parts": [{"text": prompt}]}],
                    response_json=False,
                ),
                timeout=6.0,
            )
            clean_ai = str(ai_res or "").strip().strip('"').strip()
            if clean_ai and len(clean_ai) >= 8:
                text = clean_ai
    except Exception:
        pass
    return text, chime_kind, notify_title


async def handle_speaker_session_event(bot: Optional[Bot], target_user_id: int, event: Dict[str, Any]) -> None:
    """Phát chuông + giọng Smart Butler tổng kết/báo thức ra loa ESP32 và gửi tin nhắn Telegram."""
    try:
        spoken_text, chime_kind, notify_title = await _build_speaker_event_text(event)
        mgr = esp32_audio.get_manager()
        speaker_ready = (
            mgr.volume > 0
            and (mgr.is_esp32_online() or mgr.active_ws_clients > 0 or mgr.active_stream_clients > 0)
        )
        if speaker_ready:
            spoke_voice = False
            try:
                import tts_client
                spk_mood, spk_tod = _mood_for_speaker_event(str(event.get("kind") or "focus_done"))
                spk_intensity = tts_client.compute_contextual_voice_intensity(
                    mood=spk_mood, time_of_day=spk_tod, text=spoken_text
                )
                tts_res = await tts_client.synthesize_voice(
                    spoken_text,
                    timeout_sec=22.0,
                    mood=spk_mood,
                    time_of_day=spk_tod,
                    intensity=spk_intensity,
                )
                if tts_res:
                    audio_b, fmt = tts_res
                    spoke_voice = await mgr.enqueue_voice_overlay(
                        audio_b,
                        fmt or "ogg",
                        prepend_chime=chime_kind,
                    )
            except Exception as tts_err:
                logger.debug(f"TTS sự kiện loa ESP32 lỗi: {tts_err}")
            if not spoke_voice:
                await mgr.play_chime(chime_kind)

        if bot is not None and int(target_user_id or 0) > 0:
            await _dispatch_proactive_messages(
                bot=bot,
                target_user_id=int(target_user_id),
                messages=[spoken_text],
                count_as_unanswered=False,
                base_delay=0,
                voice_text=None,
                notify_title=notify_title,
                speak_notify_tts=False,
            )
        else:
            lan_events.emit_notify(
                title=notify_title,
                body=spoken_text,
                level="info",
                source="esp32",
                speak_tts=False,
            )
    except Exception as e:
        logger.error(f"Lỗi xử lý sự kiện loa ESP32 ({event}): {e}", exc_info=True)


async def _fire_due_reminder(bot: Bot, target_user_id: int, rem: dict, now_ts: float):
    """Thực thi một lịch nhắc hẹn ('user_reminder') hoặc tự hỏi thăm ('ai_followup') tuần tự theo user_lock."""
    rid = int(rem["id"])
    kind = rem.get("kind", "user_reminder")
    title = rem.get("title", "")
    note = rem.get("context_note", "")

    async with task_helper.get_user_lock(target_user_id):
        live_rem = database.get_reminder_by_id(target_user_id, rid)
        if live_rem is not None and live_rem.get("status") != "pending":
            return

        user_state = database.get_or_create_user(target_user_id)
        last_user_msg = float(user_state.get("last_user_message_time") or 0.0)
        last_bot_msg = float(user_state.get("last_bot_message_time") or 0.0)
        fresh_now = time.time()

        session_state = database.get_session_state(target_user_id)
        if kind == "ai_followup" and database.reminder_conflicts_with_session_state(rem, session_state):
            database.update_reminder(target_user_id, rid, status="cancelled")
            logger.info(f"⏰ [Reminder #{rid} CANCELLED]: xung đột với Trạng thái phiên (đã xong / đang call) -> bỏ qua ai_followup")
            return

        created_at = float(rem.get("created_at") or 0.0)
        due_at = float(rem.get("due_at") or fresh_now)

        if kind == "ai_promise":
            import commitment_helper
            import diary_helper

            anchor_due = float(rem.get("orig_due_at") or due_at)
            if (fresh_now - anchor_due) > 6 * 3600:
                database.update_reminder(target_user_id, rid, status="cancelled")
                logger.info(f"⏰ [Promise #{rid} EXPIRED]: quá hạn >6 tiếng kể từ giờ hẹn gốc -> hủy tự động")
                return
            if session_state.get("call_status") == "in_call":
                database.update_reminder(target_user_id, rid, status="cancelled")
                logger.info(f"⏰ [Promise #{rid} CANCELLED]: đang trong cuộc gọi -> bỏ qua tin nhắn text")
                return

            # Nếu Đan đã tự báo xong việc này trong đoạn chat sau thời điểm hứa -> tự đóng tránh lặp
            recent_hist = database.get_recent_history(target_user_id, limit=8)
            for msg in recent_hist:
                if msg.get("role") == "user":
                    continue
                m_ts = diary_helper.parse_ts_float(msg.get("created_at"))
                if m_ts and m_ts > (created_at + 15.0) and commitment_helper.is_promise_fulfilled_by_text(
                    title, note, str(msg.get("content") or "")
                ):
                    database.update_reminder(target_user_id, rid, status="cancelled")
                    database.resolve_open_loops_for_promise(target_user_id, title, note, now_ts=fresh_now)
                    logger.info(f"⏰ [Promise #{rid} CANCELLED]: Đan đã báo hoàn tất trong chat trước đó")
                    return

            # Nếu hai đứa đang nhắn qua lại liên tục ngay giây phút này (< 3 phút) -> lùi nhẹ 5 phút để không ngắt nhịp
            active_gap = fresh_now - max(last_user_msg, last_bot_msg) if max(last_user_msg, last_bot_msg) > 0 else 9999.0
            if active_gap < 180.0 and (fresh_now - created_at) < 3 * 3600:
                database.update_reminder(target_user_id, rid, due_at=fresh_now + 300.0)
                logger.info(f"⏰ [Promise #{rid} DEFER 5m]: đang nhắn qua lại liên tục -> chờ dứt nhịp rồi báo")
                return

        # Nếu anh đã chủ động nhắn tin lại trước khi ai_followup thường tới hạn -> hủy lịch hỏi thăm thừa
        if kind == "ai_followup" and (
            (last_user_msg > 0 and last_user_msg > created_at)
            or (last_user_msg > 0 and (fresh_now - last_user_msg) < 1800)
        ):
            database.update_reminder(target_user_id, rid, status="cancelled")
            logger.info(f"⏰ [Reminder #{rid} CANCELLED]: user vừa nhắn gần đây -> bỏ qua ai_followup thừa")
            return

        has_promise_in_state = (kind == "ai_promise") or any(
            p.get("kind") == "promise" and p.get("status", "planned") == "planned"
            for p in (session_state.get("today_plans") or [])
        )
        safe_title = gemini_client._sanitize_reminder_promise_phrase(title, allow_promise=has_promise_in_state) or title
        safe_note = gemini_client._sanitize_reminder_promise_phrase(note, allow_promise=has_promise_in_state)

        logger.info(f"⏰ [Trigger Reminder #{rid} | {kind}]: {safe_title}")

        recent_gap = fresh_now - max(last_user_msg, last_bot_msg) if max(last_user_msg, last_bot_msg) > 0 else 9999.0
        anti_repeat_clause = ""
        if recent_gap < 180:
            anti_repeat_clause = (
                " LƯU Ý BẮT BUỘC: Vừa trao đổi ngay cách đây vài giây/phút — "
                "TUYỆT ĐỐI KHÔNG chào lại từ đầu hay nhắc lại chuyện vừa nói; "
                "chỉ chêm 1-2 câu ngắn gọn đúng trọng tâm việc nhắc hẹn."
            )

        state_guard_clause = (
            " BÁM SÁT Trạng thái phiên hiện tại; nếu việc chỉ là thông báo/dự định thường thì "
            "TUYỆT ĐỐI KHÔNG dùng cụm 'như đã hứa/giữ lời hứa'."
        )

        p_cfg = persona.get_persona_config()
        butler_name = p_cfg["butler_name"]
        owner_name = p_cfg["owner_name"]

        if kind == "user_reminder":
            prompt_ctx = (
                f"[HỆ THỐNG: ĐÃ ĐẾN GIỜ NHẮC HẸN {owner_name.upper()} ĐÃ ĐẶT — Nội dung: '{safe_title}'"
                + (f" | Ghi chú: {safe_note}" if safe_note else "")
                + f". Hãy nhắc ngay thật rõ ràng và tự nhiên đúng giọng {butler_name}.{anti_repeat_clause}{state_guard_clause}]"
            )
        elif kind == "ai_promise":
            promised_hm = datetime.datetime.fromtimestamp(created_at or fresh_now, tz=VN_TZ).strftime("%H:%M")
            now_hm = datetime.datetime.fromtimestamp(fresh_now, tz=VN_TZ).strftime("%H:%M")
            overdue_mins = max(0, int((fresh_now - due_at) // 60))
            delay_note = (
                f" (Hiện tại là {now_hm}, đã qua ~{overdue_mins} phút so với mốc hẹn — báo lại ngắn gọn và tự nhiên.)"
                if overdue_mins >= 20
                else ""
            )
            prompt_ctx = (
                f"[HỆ THỐNG: ĐÃ ĐẾN LÚC {butler_name.upper()} BÁO LẠI CHO {owner_name.upper()} THEO HẸN — Nội dung: '{safe_title}'"
                + (f" | Ghi chú: {safe_note}" if safe_note else "")
                + f" (hẹn từ lúc {promised_hm}).{delay_note} "
                f"Hãy chủ động nhắn cập nhật ngay thật tự nhiên đúng giọng {butler_name}. "
                f"Nếu trong tin nhắn trước đó vừa xử lý xong việc này rồi thì chuyển sang xác nhận nhẹ hoặc hỏi cần hỗ trợ gì thêm.{anti_repeat_clause}]"
            )
        else:
            prompt_ctx = (
                f"[HỆ THỐNG: ĐÃ TỚI LÚC {butler_name.upper()} CHỦ ĐỘNG HỎI THĂM LẠI {owner_name.upper()} — Lý do: '{safe_title}'"
                + (f" | Ghi chú: {safe_note}" if safe_note else "")
                + f". Hãy hỏi thăm gọn gàng nối tiếp ngữ cảnh trước.{anti_repeat_clause}{state_guard_clause}]"
            )

        # Không đánh dấu 'sent' trước khi gửi: nếu gọi AI lỗi thì dời lại 10 phút để không mất lời nhắc.
        # Chặn AI tự sửa/huỷ reminder trong chính lượt fire để tránh vòng lặp UPDATE #rid.
        try:
            response_data = await gemini_client.chat_with_butler(
                user_id=target_user_id,
                user_message=prompt_ctx,
                current_message_saved=False,
                exclude_reminder_id=rid,
                allow_reminder_mutations=False,
                turn_kind=kind,
                defer_commit=True,
            )
        except Exception as e:
            retry_at = fresh_now + 600
            database.update_reminder(target_user_id, rid, due_at=retry_at)
            logger.warning(f"⏰ [Reminder #{rid}] gọi AI lỗi -> dời lại 10 phút: {type(e).__name__}: {e}")
            return

        if response_data.get("logic_cancelled") and kind != "user_reminder":
            database.update_reminder(target_user_id, rid, status="cancelled")
            if kind == "ai_promise":
                database.resolve_open_loops_for_promise(target_user_id, title, note, now_ts=time.time())
            logger.info(f"🛑 [Reminder #{rid} CANCELLED by Pass 3 | {kind}]: {title}")
            return

        messages = response_data.get("messages") or [f"Nhắc lịch cho {owner_name}: {title}"]
        voice_text = response_data.get("voice_text") if response_data.get("voice_decision") else None
        notify_title = (
            f"⏰ Nhắc hẹn: {title}"
            if kind == "user_reminder"
            else (f"💬 {butler_name} báo hẹn" if kind == "ai_promise" else f"🔔 {butler_name} hỏi thăm")
        )
        sent_ok = await _dispatch_proactive_messages_locked(
            bot=bot,
            target_user_id=target_user_id,
            messages=messages,
            photo_category=response_data.get("photo_decision"),
            new_mood=response_data.get("new_mood"),
            count_as_unanswered=(kind != "user_reminder"),
            base_delay=random.randint(2, 6),
            voice_text=voice_text,
            notify_title=notify_title,
            speak_notify_tts=(kind == "user_reminder"),
            photo_key=response_data.get("chosen_photo_key"),
        )
        if sent_ok:
            sent_ts = time.time()
            gemini_client.commit_deferred_side_effects(response_data)
            database.update_reminder(target_user_id, rid, status="sent", sent_at=sent_ts)
            if kind == "ai_promise":
                database.resolve_open_loops_for_promise(target_user_id, title, note, now_ts=sent_ts)
                ss_update = response_data.get("session_state_update")
                if isinstance(ss_update, dict) and ss_update:
                    database.apply_session_state_update(target_user_id, ss_update, now_ts=sent_ts)
            logger.info(f"⏰ [Reminder #{rid} SENT | {kind}] {title}")
            if (
                kind == "user_reminder"
                and not voice_text
                and esp32_audio.get_manager().is_esp32_online()
                and not lan_presence.is_quiet_hours_now()
            ):
                spoken = (messages[0] if messages else f"Anh ơi, nhớ {safe_title} nha!").strip()
                task_helper.spawn(_maybe_speak_reminder_on_esp32(spoken))
        else:
            database.update_reminder(target_user_id, rid, due_at=fresh_now + 300)
            logger.warning(f"⏰ [Reminder #{rid}] gửi Telegram lỗi -> dời lại 5 phút")


async def proactive_scheduler_loop(bot: Bot, target_user_id: int = 0):
    """
    Vòng lặp chạy nền 24/7 cho Snapdragon 410:
    1) Kiểm tra bảng reminders mỗi 60 giây (0 tốn API khi chưa tới hạn).
    2) Khi rảnh, đánh thức Đan tự đánh giá Send / Defer / Wait_for_user tiết kiệm quota.
    """
    global PROACTIVE_SLOTS_SENT
    import config

    _configured_uid = int(target_user_id or 0)

    def _resolve_active_uid() -> int:
        if _configured_uid > 0:
            return _configured_uid
        if config.ALLOWED_USER_ID and int(config.ALLOWED_USER_ID) > 0:
            return int(config.ALLOWED_USER_ID)
        try:
            return int(database.get_primary_user_id() or 0)
        except Exception:
            return 0

    active_uid = _resolve_active_uid()
    logger.info(f"Khởi động Smart & Autonomous Proactive Scheduler cho User ID: {active_uid}")
    _mgr = esp32_audio.get_manager()

    async def _on_speaker_ev(ev):
        uid_ev = _resolve_active_uid()
        if uid_ev <= 0:
            return
        await handle_speaker_session_event(bot, uid_ev, ev)

    _mgr.set_session_event_handler(_on_speaker_ev)
    _mgr.ensure_background_loops()

    # Đặt last_day_checked bằng ngày VN hiện tại sau startup để tránh force=True lần nữa sau 60s
    last_day_checked = datetime.datetime.now(VN_TZ).day
    startup_initialized = False

    async def _run_first_time_init(uid_init: int) -> None:
        # Khởi tạo hoặc cập nhật trends & diary ngày hôm nay khi bật bot / có chủ bot đầu tiên
        try:
            await trend_helper.refresh_daily_trends()
            import diary_helper
            await diary_helper.get_or_create_today_diary()
        except Exception as e:
            logger.warning(f"Lỗi khởi tạo daily trends / diary: {e}")
        await _maybe_catchup_yesterday_audit(uid_init)

    if active_uid > 0:
        startup_initialized = True
        await _run_first_time_init(active_uid)

    while True:
        try:
            await asyncio.sleep(60)  # Kiểm tra DB cục bộ mỗi 1 phút

            target_user_id = _resolve_active_uid()
            if target_user_id <= 0:
                continue

            if not startup_initialized:
                startup_initialized = True
                last_day_checked = datetime.datetime.now(VN_TZ).day
                logger.info(f"Phát hiện chủ bot đầu tiên (User ID: {target_user_id}) -> hoàn tất khởi tạo Scheduler")
                await _run_first_time_init(target_user_id)

            # Múi giờ Việt Nam (UTC+7)
            vn_time = datetime.datetime.now(VN_TZ)
            current_day = vn_time.day
            hour = vn_time.hour
            minute = vn_time.minute
            now_ts = time.time()

            # Reset danh sách slot & cập nhật trend mới mỗi khi sang ngày mới
            if current_day != last_day_checked:
                PROACTIVE_SLOTS_SENT.clear()
                last_day_checked = current_day
                try:
                    await trend_helper.refresh_daily_trends(force=True)
                    import diary_helper
                    await diary_helper.generate_daily_diary(force=True)
                except Exception as e:
                    logger.warning(f"Lỗi refresh daily trends / diary ngày mới: {e}")
                await _maybe_catchup_yesterday_audit(target_user_id)

            # Tiến trình Audit & kết tinh ký ức cuối ngày (23:30)
            if hour == 23 and minute >= 30 and "nightly_reflection" not in PROACTIVE_SLOTS_SENT:
                PROACTIVE_SLOTS_SENT.add("nightly_reflection")
                today_str = vn_time.strftime("%Y-%m-%d")
                import vector_memory
                logger.info(f"Bắt đầu Nightly Memory Audit cuối ngày ({today_str})...")
                task_helper.spawn(vector_memory.consolidate_nightly_memories(target_user_id, target_date_str=today_str))

            # Sao lưu Database định kỳ hàng ngày (mặc định 03:00 VN, không phụ thuộc PC)
            bkp_cfg = backup_helper.get_backup_settings()
            if hour == int(bkp_cfg.get("daily_hour", 3)) and "daily_backup" not in PROACTIVE_SLOTS_SENT:
                PROACTIVE_SLOTS_SENT.add("daily_backup")
                if bkp_cfg.get("daily_telegram_enabled", True):
                    task_helper.spawn(
                        backup_helper.send_backup_to_telegram(bot, target_user_id, reason="Tự động hằng ngày")
                    )
                else:
                    task_helper.spawn(backup_helper.create_backup(tag="daily"))

            # === ƯU TIÊN 1 & 2: NHẮC HẸN ANH NHỜ & FOLLOW-UP ĐẾN HẠN ===
            due_reminders = database.get_due_reminders(target_user_id, now_ts=now_ts)
            if due_reminders:
                # Xử lý từng hẹn tới giờ (bất chấp cooldown hay giờ giấc)
                await _fire_due_reminder(bot, target_user_id, due_reminders[0], now_ts)
                continue

            user_state = database.get_or_create_user(target_user_id)
            last_user_msg = float(user_state.get("last_user_message_time") or 0.0)
            last_bot_msg = float(user_state.get("last_bot_message_time") or 0.0)
            next_check_at = float(user_state.get("next_proactive_check_at") or 0.0)
            idle_since_last = now_ts - max(last_user_msg, last_bot_msg)
            quiet_session_active = esp32_audio.get_manager().is_quiet_session_active(now_ts)

            # === ƯU TIÊN 3: TỰ CHỦ THỜI ĐIỂM NHẮN TIN (AUTONOMOUS PROACTIVE TIMING) ===
            unanswered_cnt = int(user_state.get("unanswered_proactive_count") or 0)
            if unanswered_cnt >= 2:
                continue

            global _LAST_ARRIVAL_WAKE_AT
            pres_snap = lan_presence.get_presence_snapshot()
            arrived_at = float(pres_snap.get("just_arrived_home_at") or 0.0)
            arrival_window = (
                arrived_at > _LAST_ARRIVAL_WAKE_AT
                and 0 < (now_ts - arrived_at) <= 600
                and 7 <= hour <= 23
                and idle_since_last >= 1800
            )

            should_wake = False
            if next_check_at <= 0.0:
                should_wake = idle_since_last >= 3600
            else:
                should_wake = ((now_ts >= next_check_at) or arrival_window) and (idle_since_last >= 1800)

            if not should_wake:
                continue

            if quiet_session_active:
                logger.debug("🤫 [Quiet Session]: tạm hoãn proactive chat trong lúc đang Focus/Chợp mắt")
                continue

            allow_proactive, allow_reason = lan_presence.should_allow_autonomous_proactive(user_state, now_ts)
            if not allow_proactive:
                logger.debug(f"⏸️ [Proactive Pacing]: tạm hoãn nhịp tự chủ ({allow_reason})")
                continue

            if arrival_window:
                _LAST_ARRIVAL_WAKE_AT = arrived_at

            gate = proactive_engine.evaluate_wakeup_gate(
                target_user_id,
                user_state=user_state,
                just_arrived_home=arrival_window,
                now_ts=now_ts,
            )
            if not gate["allow"]:
                database.set_proactive_schedule(target_user_id, now_ts + gate["recheck_mins"] * 60.0)
                logger.debug(
                    f"⏸️ [ProactiveArbiter]: hoãn ({gate['reason']}, "
                    f"drive={gate['drive_score']:.2f}, recheck={gate['recheck_mins']}m)"
                )
                continue

            try:
                coupon = proactive_engine.peek_wake_lottery_coupon(target_user_id, now_ts=now_ts)
                eval_started_at = time.time()
                if coupon and coupon.get("hint"):
                    try:
                        eval_data = await gemini_client.evaluate_proactive_turn(
                            target_user_id, wake_ritual_hint=coupon["hint"]
                        )
                    except TypeError:
                        eval_data = await gemini_client.evaluate_proactive_turn(target_user_id)
                else:
                    eval_data = await gemini_client.evaluate_proactive_turn(target_user_id)

                decision = eval_data.get("decision", "defer")
                recheck_mins = int(eval_data.get("recheck_after_mins") or 120)
                reason = eval_data.get("proactive_reason", "")
                database.set_proactive_schedule(target_user_id, now_ts + recheck_mins * 60.0)
                logger.info(
                    f"🤖 [Autonomous Proactive]: decision={decision}, recheck={recheck_mins}m, "
                    f"unanswered={unanswered_cnt}, drive={gate['drive_score']:.2f}, reason='{reason}'"
                )

                latest_state = database.get_or_create_user(target_user_id)
                latest_user_ts = float(latest_state.get("last_user_message_time") or 0.0)
                latest_bot_ts = float(latest_state.get("last_bot_message_time") or 0.0)
                sent_ok = False
                if (
                    decision == "send"
                    and eval_data.get("messages")
                    and latest_user_ts <= eval_started_at
                    and latest_bot_ts <= eval_started_at
                ):
                    sent_ok = await _dispatch_proactive_messages(
                        bot=bot,
                        target_user_id=target_user_id,
                        messages=eval_data["messages"],
                        photo_category=eval_data.get("photo_decision"),
                        new_mood=eval_data.get("new_mood"),
                        count_as_unanswered=True,
                        base_delay=random.randint(4, 14),
                        photo_key=eval_data.get("chosen_photo_key"),
                    )

                if sent_ok:
                    try:
                        gemini_client.close_fulfilled_promises_by_text(
                            target_user_id,
                            " ".join(eval_data.get("messages") or []),
                            now_ts=time.time(),
                            min_age_sec=0.0,
                        )
                    except Exception:
                        pass
                    proactive_engine.on_bot_proactive_sent(
                        target_user_id, unanswered_count=unanswered_cnt + 1
                    )
                    proactive_engine.consume_wake_lottery_coupon(target_user_id, coupon)
                    proactive_engine.record_eval_outcome(
                        target_user_id,
                        decision="sent",
                        reason=reason,
                        category=(coupon["category"] if coupon else gate["circadian"]["phase"]),
                        score=gate["drive_score"],
                        details={"photo": bool(eval_data.get("photo_decision")), "recheck_mins": recheck_mins},
                    )
                else:
                    proactive_engine.record_eval_outcome(
                        target_user_id,
                        decision=("skipped_race" if decision == "send" else decision),
                        reason=reason,
                        category=gate["circadian"]["phase"],
                        score=gate["drive_score"],
                        details={"recheck_mins": recheck_mins},
                    )
            except Exception as e:
                logger.warning(f"Autonomous proactive check lỗi: {e}")
                database.set_proactive_schedule(target_user_id, now_ts + 1800)

        except Exception as e:
            logger.error(f"Lỗi trong proactive_scheduler_loop: {e}")
