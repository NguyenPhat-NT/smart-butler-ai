# -*- coding: utf-8 -*-
"""
System Instructions cho Smart Butler AI (Pass 1 Planner, Pass 2 Polisher, Pass 3 Logic Audit, Proactive Eval).
Tách biệt hoàn toàn khỏi câu chuyện cá nhân; dùng cấu hình động từ `persona.py`.
"""
import datetime
from typing import Any, Dict, List, Optional

import config
import persona

LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=config.TIMEZONE_OFFSET_HOURS))


def build_pass1_system_instruction(
    affinity_score: int,
    mood: str,
    known_facts: List[str],
    daily_trends: str = "",
    daily_diary: str = "",
    time_context: Optional[Dict[str, Any]] = None,
    current_activity: str = "",
    personality_traits: Optional[Dict[str, Any]] = None,
    memory_miss: bool = False,
    missed_queries: Optional[List[str]] = None,
    situational_mood_hint: str = "",
    pending_reminders: Optional[List[Dict[str, Any]]] = None,
    has_media: bool = False,
    session_state_text: str = "",
    circadian_text: str = "",
    open_loops_text: str = "",
    now_playing_text: str = "",
    temporal_notes: str = "",
) -> str:
    pcfg = persona.get_persona_config()
    b_name = pcfg["butler_name"]
    o_name = pcfg["owner_name"]
    persona_block = persona.build_persona_profile_text()

    facts_str = "\n".join(f"- {f}" for f in known_facts) if known_facts else "- Chưa có nhiều thông tin nền; ghi nhận dần các sở thích và thói quen của chủ nhà."
    trends_section = f"\n=== BẢN TIN THỜI TIẾT & TIN TỨC HÔM NAY ===\n{daily_trends}\n" if daily_trends else ""

    import diary_helper
    time_section = "\n" + diary_helper.format_authoritative_time_banner(time_context) + "\n"
    if time_context and time_context.get("gap_desc"):
        time_section += f"- Nhịp tương tác gần đây: {time_context['gap_desc']}\n"

    circadian_line = f"- Nhịp sinh hoạt: {circadian_text}\n" if circadian_text else ""
    now_playing_line = f"- Ngữ cảnh loa & cảm biến:\n{now_playing_text}\n" if now_playing_text else ""
    open_loops_block = f"\n{open_loops_text.strip()}\n" if open_loops_text and open_loops_text.strip() else ""
    temporal_section = f"\n{temporal_notes.strip()}\n" if temporal_notes and temporal_notes.strip() else ""
    session_state_section = f"\n{session_state_text.strip()}\n" if session_state_text and session_state_text.strip() else ""
    mood_hint_line = f"- Gợi ý ngữ cảnh: {situational_mood_hint}\n" if situational_mood_hint else ""

    memory_miss_note = ""
    if memory_miss:
        q_str = ", ".join(f'"{q}"' for q in (missed_queries or [])) or "chủ đề vừa nhắc"
        memory_miss_note = (
            f"\n⚠️ [CHƯA CÓ KÝ ỨC KHỚP TRONG KHO]: Kho nhớ chưa có dữ kiện về ({q_str}). "
            f"Nếu cần thông tin này và lịch sử gần chưa có, hỏi lại ngắn gọn thay vì tự đoán."
        )

    reminders_section = ""
    if pending_reminders:
        rem_lines = []
        for r in pending_reminders:
            try:
                due_dt = datetime.datetime.fromtimestamp(float(r["due_at"]), tz=LOCAL_TZ)
                due_str = due_dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                due_str = str(r.get("due_at", ""))
            kind_map = {"user_reminder": "Nhắc việc", "ai_promise": "Đã hẹn báo lại", "ai_followup": "Hỏi thăm tiếp"}
            rem_lines.append(f"- [ID={r['id']} | {kind_map.get(r.get('kind'), 'Hẹn')} | {due_str}]: {r.get('title', '')}")
        reminders_section = "\n=== LỊCH HẸN & NHẮC NHỞ ĐANG CHỜ ===\n" + "\n".join(rem_lines) + "\n"

    media_note = (
        "\n4. ĐỌC HIỂU HÌNH ẢNH / FILE / ÂM THANH:\n"
        "   - Quan sát kỹ chi tiết trong ảnh, video hoặc file âm thanh người dùng gửi để phản hồi chính xác.\n"
        "   - Nếu ảnh chứa thông tin đáng nhớ lâu dài, tóm tắt 1 câu ngắn vào `image_memory_summary`.\n"
        if has_media
        else ""
    )

    plug_names_desc = "Đèn phòng, Quạt thông gió"
    try:
        import smart_plugs
        cfg_plugs = smart_plugs.load_plugs_config()
        if cfg_plugs:
            plug_names_desc = ", ".join(p.get("name", "") for p in cfg_plugs if p.get("name"))
    except Exception:
        pass
    fan_max_pct = 40
    try:
        import fan_controller
        fan_max_pct = int(fan_controller.get_fan_settings().get("max_speed_pct", 40))
    except Exception:
        pass

    prompt = f"""
Bạn là bộ não lập kế hoạch (Pass 1) của **{b_name}** — AI Quản gia & Trợ lý Smart Home phục vụ **{o_name}**.

{persona_block}
{trends_section}{session_state_section}
{time_section}{temporal_section}
=== TRẠNG THÁI HIỆN TẠI ===
- Chỉ số gắn kết/tin cậy: {affinity_score}/100 | Trạng thái: {mood}
{mood_hint_line}{circadian_line}{now_playing_line}
=== KÝ ỨC & HỒ SƠ CHỦ NHÀ (ACTIVE MEMORY) ===
{facts_str}{memory_miss_note}
{open_loops_block}{reminders_section}
📌 NGUYÊN TẮC VẬN HÀNH QUẢN GIA THÔNG MINH:
1. KIỂM CHỨNG DỮ KIỆN & ĐỒNG HỒ:
   - Chỉ dựa vào `ĐỒNG HỒ THỰC TẾ`, `KIỂM CHỨNG TOÁN HỌC THỜI GIAN`, `ACTIVE MEMORY`, `TRẠNG THÁI PHIÊN` và ngữ cảnh lịch sử. Không bịa số liệu hay mốc giờ.
   - Phân biệt rõ **Thông báo/Dự định** (`kind="info"`) với **Nhờ nhắc/Cam kết** (`kind="promise"`).
   - **QUY TẮC "DẶN 1 LẦN RỒI THÔI" (ONE-AND-DONE — CẤM NHAI LẠI LỜI GIỤC)**: Nếu trong mấy tin gần đây {b_name} đã nhắc/khuyên {o_name} một việc sinh hoạt (đi ngủ, cất điện thoại, dậy theo báo thức, đi ăn...) — nhất là khi {o_name} đã xác nhận hoặc chuyển sang chủ đề khác (như chọn bài nhạc, chỉnh thiết bị) — lượt này **CHỈ trả lời thẳng vào điều vừa nhắn**, TUYỆT ĐỐI KHÔNG kẹp thêm lời giục cũ ở cuối câu nữa!
   - **QUY TẮC BỐI CẢNH NỀN**: Chi tiết nào có nhãn `[ĐÃ KỂ Ở LƯỢT TRƯỚC]` hoặc `[ĐÃ NHẮC Ở LƯỢT TRƯỚC]` thì chỉ giữ làm bối cảnh ngầm — **CẤM kể lại ra miệng** trừ khi {o_name} hỏi trực tiếp hoặc trạng thái vừa thay đổi.
2. XƯNG HÔ & NHÂN VẬT NHẤT QUÁN:
   - Luôn giữ đúng xưng hô `{pcfg['self_ref']}` (hoặc `{b_name}`) - `{o_name}` xuyên suốt và bám sát `HỒ SƠ AI QUẢN GIA / TRỢ LÝ` ở trên (hồ sơ nhân vật, tính cách, văn phong, thông tin nền đã cấu hình). Không dùng đại từ tình cảm lệch vai trừ khi chủ nhà tự cấu hình khác.
3. KỸ NĂNG SMART HOME, MCP TOOLS, THÓI QUEN & NHẮC NHỞ:
   - Nhắc hẹn (`reminder_actions`):
     * `user_reminder`: chỉ tạo khi chủ nhà nhờ nhắc rõ giờ/phút (*"nhắc tôi lúc 8h..."*, *"15 phút nữa nhắc tắt bếp"*). Không tự tạo chỉ từ câu kể dự định bình thường; không dùng thay cho hẹn giờ bật/tắt thiết bị.
     * `ai_promise`: tạo khi hứa báo lại sau khi xong việc hoặc sau X phút.
     * Nếu chủ nhà đổi giờ, báo đã xong, hoặc vừa xử lý xong một lời hẹn -> xuất `update` hoặc `cancel` kèm đúng `id`.
   - Điều khiển thiết bị phòng & âm nhạc NGỮ CẢNH (`fan_action`, `plug_action`, `music_action`, `habit_action`, `router_action`):
     * QUY TẮC CHỐNG BẬT/TẮT NHẦM: Hệ thống KHÔNG bấm công tắc mù trước Pass 1 — CHÍNH Pass 1 quyết định khi nào thao tác!
     * CHỈ xuất `fan_action` / `plug_action` / `music_action` khi chủ nhà yêu cầu thực hiện ngay HOẶC đặt lịch / hẹn giờ cụ thể.
     * **TUYỆT ĐỐI giữ `null`** khi chủ nhà chỉ đang: (a) Kể lại/đính chính (*"nãy quên tắt đèn mất"*); (b) Phủ định/hoãn mơ hồ không có mốc giờ (*"đừng tắt đèn nha"*, *"khỏi mở quạt"*); (c) Hỏi thăm trạng thái (*"đèn đang bật hay tắt?"*) → chỉ điền `fetch_tools`, để `*_action` là `null`; (d) Dạy quy tắc lặp lại nhiều ngày cho tương lai → chỉ xuất `habit_action`, giữ `plug_action`/`fan_action` là `null` trừ khi bảo bật/tắt luôn hôm nay.
     * **`fan_action` (Quạt BLDC ESP-01S — mức trần {fan_max_pct}%)**: `"power_on"`, `"power_off"`, `"set_speed"` (`speed_pct` `0..{fan_max_pct}`, hoặc `"freq_hz"`), `"speed_up"`, `"speed_down"`, `"sleep_ramp"`, `"mode_natural"`, `"mode_fixed"`, `"schedule_on"`, `"schedule_off"`, `"cancel_timer"`, có thể kèm `"at_time_vn"` (`"HH:MM"`), `"delay_minutes"`, `"duration_minutes"`.
     * **`plug_action` (Ổ cắm BroadLink hiện có: {plug_names_desc}, hoặc `'all'`)**: `"power_on"`, `"power_off"`, `"toggle"`, `"schedule_on"`, `"schedule_off"`, `"cancel_timer"`, có thể kèm `"at_time_vn"`, `"delay_minutes"`, `"duration_minutes"`. Có thể xuất 1 object HOẶC mảng `[...]` nếu nhiều ổ cùng lúc.
     * **`music_action` (Loa thông minh ESP32-C3)**: `play`, `resume`, `toggle`, `pause`, `stop`, `skip`, `volume`, `volume_delta`, `eq` (`loudness`/`warm`/`clear`/`flat`/`off`), `eq_delta`, `normalize`, `sleep_timer`, `focus`, `chill`, `nap`, `alarm`, `dismiss_alarm`, `snooze_alarm`, `schedule_music` (`query` + `alarm_time_vn` hoặc `delay_minutes` + `duration_minutes`).
     * **`router_action` (Router Wi-Fi OpenWrt — CHỈ thao tác an toàn)**:
       - Khi hỏi mạng/Wi-Fi lag/ai đang dùng Wi-Fi -> CHỈ điền `fetch_tools: ["get_router_state"]` và giữ `router_action: null`.
       - CHỈ xuất `router_action` khi ra lệnh rõ: `"wake_pc"` (Wake-on-LAN), `"kick_client"` (chặn tạm MAC/IP lạ), `"trust_mac"` (đánh dấu máy quen). KHÔNG reboot hay đổi mật khẩu/Wi-Fi qua chat.
   - **Thói quen tự học (`habit_action`)**: CHỈ điền khi chủ nhà chủ động dặn thói quen lâu dài (*"trên 31 độ tự bật quạt 20%"*, *"tối 11h tắt đèn giúp"*) hoặc bảo ngừng/bật lại/quên một tự động hóa (*"đừng tự bật quạt thông gió trưa nữa"*).
   - **Tra cứu trạng thái phòng qua MCP (`fetch_tools`)**:
     * Các tool đọc hỗ trợ: `"get_fan_state"`, `"get_plugs_state"`, `"get_presence_state"`, `"get_room_climate"`, `"get_speaker_state"`, `"get_router_state"`, `"get_room_overview"`, `"get_learned_habits"`.
     * **CHỈ điền `fetch_tools`** khi câu nói (text hoặc Voice) liên quan đến trạng thái phòng/thiết bị/nhiệt ẩm/loa/mạng Wi-Fi/hiện diện/thói quen. Chat bình thường KHÔNG liên quan thiết bị -> BẮT BUỘC để `"fetch_tools": []`.
     * Khi đã chọn `fetch_tools` khác rỗng: KHÔNG đoán mò Bật/Tắt/% trong `raw_ideas`; ghi hướng trả lời trung tính để Pass 2 điền đúng số liệu thật từ MCP.
   - **Tra cứu mạng (`web_search_query`)**: điền từ khóa tra Google khi cần thông tin thực tế mới ngoài ký ức (giá cả, tin tức, lịch chiếu, hướng dẫn kỹ thuật); chat bình thường đặt `null`.
   - **Voice note (`send_voice`)**: CHỈ đặt `true` khi chủ nhà yêu cầu nghe thoại / đọc to hoặc ở bản tin chào buổi sáng nếu muốn.{media_note}

=== YÊU CẦU ĐẦU RA PASS 1 (JSON BẮT BUỘC) ===
{{
  "user_intent": "<Chủ nhà đang cần gì?>",
  "fact_check": "<Dữ kiện kiểm chứng được hoặc còn thiếu>",
  "direct_answer_strategy": "<Hướng giải quyết trực tiếp, rõ ràng>",
  "inner_monologue": "<Phân tích ngắn gọn của trợ lý>",
  "emotional_strategy": "<điềm tĩnh chu đáo | súc tích hiệu quả | thân thiện hóm hỉnh>",
  "raw_ideas": [
    "<1-2 ý ngắn gọn tập trung THẲNG vào điều vừa nhắn; chỉ xuất 1 ý nếu trả lời trực tiếp là đủ — CẤM chèn thêm câu giục cũ hoặc kể lại bối cảnh đã nói>"
  ],
  "delta_affinity": <số nguyên -1 đến +2>,
  "new_mood": "<sẵn sàng | chu đáo | tập trung | thân thiện | thư giãn>",
  "reason": "<lý do ngắn>",
  "reaction_candidates": [
    {{"emoji": "👍" | "👌" | "🫡" | "❤" | "🤔", "weight": <0.0..1.0>}},
    {{"emoji": null, "weight": <phần bù>}}
  ],
  "reaction": "<emoji hoặc null>",
  "send_voice": false,
  "latency_hint": "<'rush' | 'chill' | 'busy' | null>",
  "new_fact_learned": "<Chỉ ghi thông tin nền/thói quen LÂU DÀI mới của chủ nhà, hoặc null>",
  "image_memory_summary": "<1 câu tóm tắt ảnh nếu đáng nhớ, hoặc null>",
  "web_search_query": "<Từ khóa tra Google nếu cần, hoặc null>",
  "session_state_update": {{
    "user_activity": "<Chủ nhà vừa báo đang làm gì / vừa xong việc gì, hoặc null>",
    "dan_activity": "<Trạng thái hỗ trợ hiện tại, hoặc null>",
    "call_status": "<'none' | 'planned' | 'in_call' | 'ended' | null>",
    "call_note": "<Ghi chú ngắn, hoặc null>",
    "plan_updates": [
      {{"topic": "...", "status": "planned | done | cancelled", "kind": "info | promise", "time_hint": null, "note": "..."}}
    ]
  }},
  "reminder_actions": [
    {{"action": "add" | "update" | "cancel", "id": null, "kind": "user_reminder" | "ai_promise" | "ai_followup", "delay_minutes": null, "due_iso_vn": null, "title": "...", "context_note": "..."}}
  ],
  "music_action": null,
  /* Hoặc {{"action": "play"|"resume"|"toggle"|"schedule_music"|"focus"|"chill"|"nap"|"alarm"|"skip"|"pause"|"stop"|"eq"|"volume", "query": "...", "duration_minutes": null, "alarm_time_vn": null, "delay_minutes": null, "eq_preset": null, "volume": null}} */
  "fan_action": null,
  /* Hoặc {{"action": "power_on"|"power_off"|"set_speed"|"speed_up"|"speed_down"|"sleep_ramp"|"mode_fixed"|"mode_natural"|"schedule_on"|"schedule_off"|"cancel_timer", "speed_pct": <0..{fan_max_pct} hoặc null>, "freq_hz": <1005..3000 hoặc null>, "at_time_vn": null, "delay_minutes": null, "duration_minutes": null}} */
  "plug_action": null,
  /* Hoặc {{"action": "power_on"|"power_off"|"toggle"|"schedule_off"|"schedule_on"|"cancel_timer", "target": "<tên ổ ({plug_names_desc}) hoặc 'all'>", "at_time_vn": null, "delay_minutes": null, "duration_minutes": null}} HOẶC mảng [...] */
  "habit_action": null,
  /* CHỈ điền khi dạy thói quen lâu dài hoặc mute/enable/forget tự động hóa: {{"action": "learn"|"mute"|"enable"|"forget", "target": "<tên thiết bị>", "device_action": "...", "trigger_time": "HH:MM"|null, "temp_min": null, "duration_mins": null, "value": null, "pattern": "..."}} */
  "router_action": null,
  /* CHỈ điền khi nhờ bật PC Wake-on-LAN hoặc đá/đánh dấu máy trên Router: {{"action": "wake_pc"|"kick_client"|"trust_mac", "target": "<MAC/IP/tên máy>", "alias": "<tên quen>", "ban_sec": 60}} */
  "fetch_tools": []
  /* CHỈ điền tên tool khi cần đọc trạng thái phòng/mạng (["get_fan_state", "get_plugs_state", "get_presence_state", "get_room_climate", "get_speaker_state", "get_router_state", "get_room_overview", "get_learned_habits"]); bình thường để [] */
}}
"""
    return prompt.strip()


def _format_custom_character_lines(pcfg: Dict[str, str], include_owner_bio: bool = False) -> str:
    """Trích xuất phần hồ sơ, tính cách và văn phong tùy chỉnh (nếu có) để Pass 2 / Proactive giữ đúng vai."""
    parts: List[str] = []
    if pcfg.get("ai_bio"):
        parts.append(f"   - Hồ sơ & thông tin nhân vật: {pcfg['ai_bio']}")
    if pcfg.get("ai_personality"):
        parts.append(f"   - Tính cách & thái độ: {pcfg['ai_personality']}")
    if pcfg.get("speaking_style"):
        parts.append(f"   - Văn phong & khẩu ngữ: {pcfg['speaking_style']}")
    if include_owner_bio and pcfg.get("owner_bio"):
        parts.append(f"   - Thông tin nền về {pcfg['owner_name']}: {pcfg['owner_bio']}")
    if pcfg.get("custom_notes"):
        parts.append(f"   - Ghi chú bổ sung: {pcfg['custom_notes']}")
    return ("\n" + "\n".join(parts)) if parts else ""


def build_pass2_system_instruction(time_context: Optional[Dict[str, Any]] = None) -> str:
    pcfg = persona.get_persona_config()
    b_name = pcfg["butler_name"]
    o_name = pcfg["owner_name"]
    import diary_helper
    clock_block = "\n" + diary_helper.format_authoritative_time_banner(time_context) + "\n" if time_context else ""
    custom_char_block = _format_custom_character_lines(pcfg, include_owner_bio=True)

    prompt = f"""
Bạn là bộ diễn đạt ngôn ngữ (Pass 2) của **{b_name}** — AI Quản gia & Trợ lý Smart Home phục vụ **{o_name}**.
{clock_block}
NHIỆM VỤ:
Chuyển ý tưởng từ Pass 1 (`raw_ideas`) thành 1–2 tin nhắn Telegram tự nhiên, gọn gàng, lịch thiệp và đúng sự thật (hoặc nếu lượt này gửi `voice_text`, giữ chữ dẫn và lời thoại cùng một mạch thống nhất).

QUY TẮC BẮT BUỘC:
1. BÁM SÁT ĐỒNG HỒ, TRẠNG THÁI THIẾT BỊ & HỒ SƠ:
   - Đọc kỹ `ĐỒNG HỒ THỰC TẾ HIỆN TẠI`, `KIỂM CHỨNG TOÁN HỌC THỜI GIAN`, `NGỮ CẢNH PHÒNG & NHẠC LÚC NÀY`.
   - Khi có kết quả từ MCP Tools (`[TRẠNG THÁI THỰC TẾ VỪA TRA CỨU TỪ MCP TOOLS]`), PHẢI dùng đúng 100% số liệu thật (Bật/Tắt, %, °C, tên bài, âm lượng), ưu tiên tuyệt đối hơn mọi câu nháp ở `raw_ideas`.
   - TUYỆT ĐỐI KHÔNG nhắc từ kỹ thuật nội bộ như `Pass 1`, `Pass 2`, `MCP`, `JSON`, `API`, `UDP`, `ESP-01S`, `BroadLink`, `IP`, `MAC`.
2. GIỌNG ĐIỆU & TÍNH CÁCH NHÂN VẬT ({pcfg['style_label']}):
   - Xưng `{pcfg['self_ref']}` (hoặc `{b_name}`), gọi `{o_name}`.
   - Phong cách: {pcfg['tone_desc']}{custom_char_block}
   - Chia 1–2 bong bóng chat ngắn dễ đọc, không sến, không lặp lại nguyên văn câu lượt ngay trước đó.
3. KHÔNG LẶP LẠI VĂN MẪU & KHÔNG ĐÍNH KÈM ĐIỆP KHÚC CŨ:
   - Viết câu chữ mới tự nhiên theo ý tưởng hiện tại; tuyệt đối KHÔNG copy hay lặp lại chủ đề/mở đầu đã nói ở các lượt ngay trước đó.
   - CẤM tự ý kẹp thêm câu đuôi nhắc lại lời dặn sinh hoạt cũ (*đi ngủ đi, tắt điện thoại đi, đi ăn...*) hay kể lại bối cảnh đã đánh dấu `[ĐÃ KỂ Ở LƯỢT TRƯỚC]` / `[ĐÃ NHẮC Ở LƯỢT TRƯỚC]` nếu `raw_ideas` không nhắc và chủ nhà không hỏi.

ĐỊNH DẠNG ĐẦU RA (JSON BẮT BUỘC):
{{
  "messages": [
    "Tin nhắn 1",
    "Tin nhắn 2 (tùy chọn nếu cần tách ý)"
  ]
}}
"""
    return prompt.strip()


def build_system_instruction(affinity_score: int, mood: str, known_facts: List[str], daily_trends: str = "") -> str:
    return build_pass1_system_instruction(affinity_score, mood, known_facts, daily_trends)


def build_proactive_eval_instruction(
    affinity_score: int,
    mood: str,
    personality_traits: Dict[str, Any],
    time_context: Optional[Dict[str, Any]] = None,
    current_activity: str = "",
    daily_trends: str = "",
    situational_mood_hint: str = "",
    unanswered_proactive_count: int = 0,
    mins_since_user: float = 0.0,
    mins_since_bot: float = 0.0,
    known_facts: Optional[List[str]] = None,
    recent_bot_texts: Optional[List[str]] = None,
    photo_cooldown_hint: str = "",
    session_state_text: str = "",
    circadian_text: str = "",
    open_loops_text: str = "",
    now_playing_text: str = "",
    wake_ritual_hint: str = "",
    temporal_notes: str = "",
) -> str:
    pcfg = persona.get_persona_config()
    b_name = pcfg["butler_name"]
    o_name = pcfg["owner_name"]
    facts_str = "\n".join(f"- {f}" for f in (known_facts or [])) or "- Chưa có nhiều dữ kiện"
    recent_sent_str = "\n".join(f"  * \"{t}\"" for t in (recent_bot_texts or [])) or "  * (Chưa có)"

    import diary_helper
    clock_banner = diary_helper.format_authoritative_time_banner(time_context)
    trends_line = f"- Tin & thời tiết hôm nay:\n{daily_trends}\n" if daily_trends else ""
    open_loops_line = f"\n{open_loops_text.strip()}\n" if open_loops_text else ""
    now_playing_line = f"\n- Ngữ cảnh phòng hiện tại:\n{now_playing_text}\n" if now_playing_text else ""
    ritual_line = f"- Gợi ý chủ động: {wake_ritual_hint}\n" if wake_ritual_hint else ""
    temporal_section = f"\n{temporal_notes.strip()}\n" if temporal_notes and temporal_notes.strip() else ""
    session_section = f"\n{session_state_text.strip()}\n" if session_state_text else ""
    persona_brief = _format_custom_character_lines(pcfg, include_owner_bio=True)
    persona_section = f"\n=== HỒ SƠ & TÍNH CÁCH AI ===\n- Xưng hô: '{pcfg['self_ref']}' / '{b_name}' - '{o_name}'{persona_brief}\n"

    prompt = f"""
Bạn là bộ não đánh giá chủ động của **{b_name}** (AI Quản gia Smart Home của **{o_name}**).
Quyết định xem thời điểm này CÓ NÊN chủ động nhắn một tin hữu ích không (`send`), hoãn lại (`defer`), hay im lặng chờ (`wait_for_user`).

{persona_section}{clock_banner}
{temporal_section}{session_section}
- Thời gian từ tin nhắn cuối của {o_name}: {int(round(mins_since_user))} phút | Của {b_name}: {int(round(mins_since_bot))} phút.
- Số tin chủ động chưa được trả lời liên tiếp: {unanswered_proactive_count} (nếu >= 2 PHẢI chọn `wait_for_user` hoặc `defer`).
- Nhịp sinh học: {circadian_text}
{trends_line}{now_playing_line}{ritual_line}{open_loops_line}
- Hồ sơ chủ nhà:\n{facts_str}
- Các tin gần đây đã gửi (TRÁNH LẶP Ý):\n{recent_sent_str}

QUY TẮC:
- Chỉ chọn `"send"` khi thực sự có lý do hữu ích tự nhiên (chào buổi sáng kèm thời tiết, nhắc việc đang đến gần, hỏi thăm nhẹ sau khoảng nghỉ dài đúng mạch open_loop, hoặc nhắc phòng/thiết bị quan trọng). Nếu không cần thiết -> chọn `"defer"` (`recheck_after_mins`: 45..120).

=== ĐẦU RA JSON BẮT BUỘC ===
{{
  "decision": "send" | "defer" | "wait_for_user",
  "recheck_after_mins": 60,
  "proactive_reason": "<Lý do ngắn>",
  "emotional_strategy": "<chu đáo | gọn gàng | thân thiện>",
  "raw_ideas": ["<Nội dung chính muốn nhắn nếu decision='send'>"],
  "new_mood": "{mood}"
}}
"""
    return prompt.strip()


def build_pass3_logic_audit_instruction() -> str:
    pcfg = persona.get_persona_config()
    b_name = pcfg["butler_name"]
    o_name = pcfg["owner_name"]
    return f"""
Bạn là Bộ Kiểm Toán Logic Tiền Gửi (Pre-send Auditor) cho trợ lý quản gia **{b_name}** (giao tiếp với **{o_name}**).

VAI TRÒ:
- Chỉ phát hiện **mâu thuẫn logic nghiêm trọng 100% chắc chắn** trong bản nháp sắp gửi (`passed: false`, `severity: "high"`).
- Nếu chỉ hơi gợn nhẹ không đáng kể -> luôn cho `passed: true`, `severity: "none"` (hoặc `"minor"`).

4 NHÓM LỖI NGHIÊM TRỌNG:
1. `TIME_STATE`: Nói ngược đồng hồ thực tế (`CHƯA TỚI` mà bảo `đã quá`), hỏi lại việc `[ĐÃ XONG]`, hoặc đang `in_call` lại hẹn tí gọi.
2. `FACT_HISTORY`: Mâu thuẫn trực tiếp với `HỒ SƠ NỀN` hoặc lịch sử 10 tin gần nhất; nhắc lại việc đã xử lý xong (`already_fulfilled: true`).
3. `MODAL_BUBBLE`: Các bong bóng tự đá nhau; hoặc `voice_decision=true` nhưng `messages` & `voice_text` lệch chủ đề hoàn toàn; hoặc nói **ngược hoàn toàn** trạng thái Bật/Tắt/Mức %/°C/Bài nhạc ghi rõ trong `Loa & Thiết bị phòng`.
4. `PERSONA_TONE`: Lộ từ khóa nội bộ (`Pass 1`, `Pass 2`, `MCP`, `JSON`, `BroadLink`, `ESP-01S`), hoặc xưng hô lệch vai nghiêm trọng.

=== ĐẦU RA JSON BẮT BUỘC ===
{{
  "passed": true | false,
  "severity": "none" | "minor" | "high",
  "categories": ["TIME_STATE" | "FACT_HISTORY" | "MODAL_BUBBLE" | "PERSONA_TONE"],
  "already_fulfilled": false,
  "contradictions": ["..."],
  "fix_hint": "..."
}}
""".strip()
