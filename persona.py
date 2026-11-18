# -*- coding: utf-8 -*-
"""
Quản lý Persona cho Smart Butler AI (tách biệt hoàn toàn dữ liệu cá nhân khỏi mã nguồn).
Hỗ trợ 3 phong cách:
  - "butler": Quản gia thông minh, lịch thiệp, chu đáo.
  - "assistant": Trợ lý cá nhân súc tích, tập trung hiệu suất.
  - "companion": Bạn đồng hành thân thiện, tự nhiên.
"""
from typing import Any, Dict

import config
import database

_META_PERSONA = "butler_persona_config"

STYLE_PRESETS: Dict[str, Dict[str, str]] = {
    "butler": {
        "label": "Quản gia thông minh (Lịch thiệp & Chu đáo)",
        "self_ref": "mình",
        "tone_desc": (
            "Điềm tĩnh, tinh tế, chu đáo như một người quản gia công nghệ đáng tin cậy; "
            "chủ động theo dõi phòng ốc, lịch nhắc và sức khỏe sinh hoạt của chủ nhà mà không rườm rà."
        ),
        "voice_style": "Ấm áp, điềm đạm, tự nhiên như người thật nói chuyện qua thoại.",
    },
    "assistant": {
        "label": "Trợ lý chuyên nghiệp (Súc tích & Rõ ràng)",
        "self_ref": "trợ lý",
        "tone_desc": (
            "Rõ ràng, ngắn gọn, đi thẳng vào trọng tâm, ưu tiên số liệu chính xác, hành động nhanh và báo cáo minh bạch."
        ),
        "voice_style": "Gọn gàng, mạch lạc, dễ nghe, không dài dòng.",
    },
    "companion": {
        "label": "Bạn đồng hành (Thân thiện & Tự nhiên)",
        "self_ref": "mình",
        "tone_desc": (
            "Gần gũi, thoải mái, có chút hóm hỉnh nhẹ nhàng như một người bạn cùng nhà thông minh, lắng nghe tốt."
        ),
        "voice_style": "Thân mật, tươi tắn, biểu cảm tự nhiên.",
    },
}


def get_persona_config() -> Dict[str, str]:
    raw = database._get_json_meta(_META_PERSONA)
    style = config.BUTLER_STYLE if config.BUTLER_STYLE in STYLE_PRESETS else "butler"
    out = {
        "butler_name": config.BUTLER_NAME,
        "owner_name": config.OWNER_NAME,
        "style": style,
        "weather_city": config.WEATHER_CITY,
        "custom_notes": "",
    }
    if isinstance(raw, dict):
        if str(raw.get("butler_name") or "").strip():
            out["butler_name"] = str(raw["butler_name"]).strip()[:40]
        if str(raw.get("owner_name") or "").strip():
            out["owner_name"] = str(raw["owner_name"]).strip()[:40]
        if str(raw.get("style") or "").strip().lower() in STYLE_PRESETS:
            out["style"] = str(raw["style"]).strip().lower()
        if str(raw.get("weather_city") or "").strip():
            out["weather_city"] = str(raw["weather_city"]).strip()[:60]
        if isinstance(raw.get("custom_notes"), str):
            out["custom_notes"] = raw["custom_notes"].strip()[:400]
    preset = STYLE_PRESETS.get(out["style"], STYLE_PRESETS["butler"])
    out["style_label"] = preset["label"]
    out["self_ref"] = preset["self_ref"]
    out["tone_desc"] = preset["tone_desc"]
    out["voice_style"] = preset["voice_style"]
    return out


def save_persona_config(patch: Dict[str, Any]) -> Dict[str, str]:
    cur = get_persona_config()
    updated = {
        "butler_name": cur["butler_name"],
        "owner_name": cur["owner_name"],
        "style": cur["style"],
        "weather_city": cur["weather_city"],
        "custom_notes": cur["custom_notes"],
    }
    if isinstance(patch, dict):
        if "butler_name" in patch and str(patch["butler_name"]).strip():
            updated["butler_name"] = str(patch["butler_name"]).strip()[:40]
        if "owner_name" in patch and str(patch["owner_name"]).strip():
            updated["owner_name"] = str(patch["owner_name"]).strip()[:40]
        if "style" in patch and str(patch["style"]).strip().lower() in STYLE_PRESETS:
            updated["style"] = str(patch["style"]).strip().lower()
        if "weather_city" in patch and str(patch["weather_city"]).strip():
            updated["weather_city"] = str(patch["weather_city"]).strip()[:60]
        if "custom_notes" in patch:
            updated["custom_notes"] = str(patch["custom_notes"] or "").strip()[:400]
    database._set_json_meta(_META_PERSONA, updated)
    return get_persona_config()


def build_persona_profile_text() -> str:
    cfg = get_persona_config()
    b_name = cfg["butler_name"]
    o_name = cfg["owner_name"]
    notes = f"\n- Ghi chú thêm từ chủ nhà: {cfg['custom_notes']}" if cfg["custom_notes"] else ""
    return f"""=== HỒ SƠ AI QUẢN GIA / TRỢ LÝ: {b_name} ===
- Vai trò: Trợ lý & Quản gia Smart Home cá nhân của {o_name}, vận hành 24/7 trên thiết bị biên tại nhà.
- Xưng hô chuẩn: Tự xưng "{cfg['self_ref']}" (hoặc "{b_name}"), gọi người dùng là "{o_name}".
- Phong cách giao tiếp ({cfg['style']}): {cfg['tone_desc']}
- Năng lực cốt lõi:
  1. Theo dõi & điều khiển phòng thông minh qua LAN/MCP (Quạt BLDC, ổ cắm thông minh BroadLink SPmini, Loa Wi-Fi ESP32 kèm nhạc & cảm biến nhiệt ẩm DHT11, phát hiện hiện diện Wi-Fi).
  2. Tự động học thói quen sinh hoạt (`habit_engine`) và thực thi an toàn theo hiện diện / giờ ngủ.
  3. Ghi nhớ thông tin lâu dài, quản lý nhắc hẹn (`reminders`) và tóm tắt tin tức / thời tiết khu vực {cfg['weather_city']}.{notes}
"""
