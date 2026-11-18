# -*- coding: utf-8 -*-
import random
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

COOLDOWN_PENALTY_FACTOR = 0.7

TELEGRAM_ALLOWED_REACTIONS = {
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉",
    "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥",
    "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾",
    "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇",
    "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒",
    "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷‍♀", "🤷",
}

REACTION_NORMALIZE = {
    "😂": "🤣", "🙄": "🤨", "❤️": "❤", "❤\ufe0f": "❤", "🥹": "🥰", "😊": "😁",
    "😄": "😁", "😃": "😁", "😉": "😍", "💕": "❤", "💖": "❤", "💗": "❤",
    "😅": "🤣", "😆": "🤣", "😋": "🥴", "😏": "🤨", "👏🏻": "👏", "✅": "👍",
}


def normalize_reaction(emoji: Optional[str]) -> Optional[str]:
    if not emoji:
        return None
    cleaned = str(emoji).strip()
    if not cleaned or cleaned.lower() in ("null", "none"):
        return None
    cleaned = REACTION_NORMALIZE.get(cleaned, cleaned)
    return cleaned if cleaned in TELEGRAM_ALLOWED_REACTIONS else None


def weighted_choice(
    candidates: List[Dict[str, Any]],
    value_key: str = "type",
    apply_penalty: bool = False,
    penalty_factor: float = COOLDOWN_PENALTY_FACTOR,
) -> Optional[Any]:
    if not candidates or not isinstance(candidates, list):
        return None
    values = []
    weights = []
    null_weight = 0.0
    non_null_candidates = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        val = item.get(value_key)
        if val is None and "value" in item:
            val = item.get("value")
        try:
            w = max(0.0, float(item.get("weight", 0.0)))
        except (ValueError, TypeError):
            w = 0.0
        if val is None or str(val).lower() in ("null", "none", ""):
            null_weight += w
        else:
            non_null_candidates.append((val, w))
    if not non_null_candidates:
        return None

    total_reduced = 0.0
    for val, w in non_null_candidates:
        if apply_penalty:
            nw = w * penalty_factor
            total_reduced += (w - nw)
            values.append(val)
            weights.append(nw)
        else:
            values.append(val)
            weights.append(w)

    adj_null = null_weight + total_reduced
    if adj_null > 0:
        values.append(None)
        weights.append(adj_null)
    if sum(weights) <= 0:
        return None
    return random.choices(values, weights=weights, k=1)[0]


def resolve_reaction_choice(response_data: Dict[str, Any], reacted_recently: bool) -> Optional[str]:
    candidates = response_data.get("reaction_candidates")
    if candidates and isinstance(candidates, list):
        valid = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            norm = normalize_reaction(item.get("emoji"))
            if norm:
                valid.append({**item, "emoji": norm})
            elif item.get("emoji") is None:
                valid.append(item)
        if valid:
            return weighted_choice(valid, value_key="emoji", apply_penalty=reacted_recently)

    legacy = normalize_reaction(response_data.get("reaction"))
    if legacy:
        return weighted_choice([{"emoji": legacy, "weight": 0.45}, {"emoji": None, "weight": 0.55}], value_key="emoji", apply_penalty=reacted_recently)
    return None


def resolve_latency_choice(
    last_bot_msg_time: float = 0.0,
    last_user_msg_time: float = 0.0,
    affinity_score: int = 80,
    latency_hint: Optional[str] = None,
    api_elapsed_sec: float = 0.0,
) -> int:
    """Độ trễ phản hồi gọn nhẹ chuẩn AI Quản gia (nhanh ~1–3s, không làm chủ nhà chờ lâu)."""
    if latency_hint == "rush":
        return 1
    if latency_hint == "busy":
        return max(1, int(round(3.0 - api_elapsed_sec)))
    return max(1, int(round(2.0 - min(1.0, api_elapsed_sec))))
