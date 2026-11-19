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


_TEXT_FIELD_LIMITS: Dict[str, int] = {
    "self_pronoun": 30,
    "ai_bio": 1500,
    "ai_personality": 1500,
    "speaking_style": 1000,
    "owner_bio": 1000,
    "custom_notes": 1500,
}


def _build_persona_profile_from_cfg(cfg: Dict[str, str]) -> str:
    b_name = cfg["butler_name"]
    o_name = cfg["owner_name"]
    extra_lines = []
    if cfg.get("ai_bio"):
        extra_lines.append(f"- Hồ sơ & Thông tin nhân vật AI:\n  {cfg['ai_bio']}")
    if cfg.get("ai_personality"):
        extra_lines.append(f"- Tính cách & Thái độ ứng xử:\n  {cfg['ai_personality']}")
    if cfg.get("speaking_style"):
        extra_lines.append(f"- Văn phong & Khẩu ngữ riêng:\n  {cfg['speaking_style']}")
    if cfg.get("owner_bio"):
        extra_lines.append(f"- Thông tin nền về {o_name}:\n  {cfg['owner_bio']}")
    if cfg.get("custom_notes"):
        extra_lines.append(f"- Ghi chú / Quy tắc bổ sung:\n  {cfg['custom_notes']}")
    extra_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
    return f"""=== HỒ SƠ AI QUẢN GIA / TRỢ LÝ: {b_name} ===
- Vai trò: Trợ lý & Quản gia Smart Home cá nhân của {o_name}, vận hành 24/7 trên thiết bị biên tại nhà.
- Xưng hô chuẩn: Tự xưng "{cfg['self_ref']}" (hoặc "{b_name}"), gọi người dùng là "{o_name}".
- Phong cách giao tiếp ({cfg['style']}): {cfg['tone_desc']}
- Năng lực cốt lõi:
  1. Theo dõi & điều khiển phòng thông minh qua LAN/MCP (Quạt BLDC, ổ cắm thông minh BroadLink SPmini, Loa Wi-Fi ESP32 kèm nhạc & cảm biến nhiệt ẩm DHT11, phát hiện hiện diện Wi-Fi).
  2. Tự động học thói quen sinh hoạt (`habit_engine`) và thực thi an toàn theo hiện diện / giờ ngủ.
  3. Ghi nhớ thông tin lâu dài, quản lý nhắc hẹn (`reminders`) và tóm tắt tin tức / thời tiết khu vực {cfg['weather_city']}.{extra_block}
"""


def get_persona_config() -> Dict[str, str]:
    raw = database._get_json_meta(_META_PERSONA)
    style = config.BUTLER_STYLE if config.BUTLER_STYLE in STYLE_PRESETS else "butler"
    out: Dict[str, str] = {
        "butler_name": config.BUTLER_NAME,
        "owner_name": config.OWNER_NAME,
        "self_pronoun": "",
        "style": style,
        "weather_city": config.WEATHER_CITY,
        "ai_bio": "",
        "ai_personality": "",
        "speaking_style": "",
        "owner_bio": "",
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
        for field, max_len in _TEXT_FIELD_LIMITS.items():
            if isinstance(raw.get(field), str):
                out[field] = raw[field].strip()[:max_len]
    preset = STYLE_PRESETS.get(out["style"], STYLE_PRESETS["butler"])
    out["style_label"] = preset["label"]
    out["self_ref"] = out["self_pronoun"] or preset["self_ref"]
    out["tone_desc"] = preset["tone_desc"]
    out["voice_style"] = preset["voice_style"]
    out["prompt_preview"] = _build_persona_profile_from_cfg(out).strip()
    return out


def save_persona_config(patch: Dict[str, Any]) -> Dict[str, str]:
    cur = get_persona_config()
    updated: Dict[str, str] = {
        "butler_name": cur["butler_name"],
        "owner_name": cur["owner_name"],
        "self_pronoun": cur.get("self_pronoun", ""),
        "style": cur["style"],
        "weather_city": cur["weather_city"],
        "ai_bio": cur.get("ai_bio", ""),
        "ai_personality": cur.get("ai_personality", ""),
        "speaking_style": cur.get("speaking_style", ""),
        "owner_bio": cur.get("owner_bio", ""),
        "custom_notes": cur.get("custom_notes", ""),
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
        for field, max_len in _TEXT_FIELD_LIMITS.items():
            if field in patch and patch[field] is not None:
                updated[field] = str(patch[field]).strip()[:max_len]
    database._set_json_meta(_META_PERSONA, updated)
    return get_persona_config()


def build_persona_profile_text() -> str:
    return _build_persona_profile_from_cfg(get_persona_config())
