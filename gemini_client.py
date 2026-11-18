# -*- coding: utf-8 -*-
import re
import json
import time
import base64
import difflib
import socket
import asyncio
import datetime
import logging
import aiohttp
from typing import Dict, Any, List, Optional, Tuple
import config
from config import GEMINI_API_KEY, CHAT_MODEL, MEMORY_MODEL, REVIEW_MODEL, get_api_keys
import database
import character_bible
import mcp_tools
import persona

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
VN_TZ = datetime.timezone(datetime.timedelta(hours=getattr(config, "TIMEZONE_OFFSET_HOURS", 7)))

# Ngân sách thời gian cho 1 lần gọi và cho toàn bộ chuỗi fallback.
# Đặt 12s/lần trên tổng 42s để kịp thử đủ >=3 tổ hợp model/key dự phòng khi model chính treo.
REQUEST_TIMEOUT_SEC = 12.0
TOTAL_DEADLINE_SEC = 42.0

# Ghi nhớ API key đã upload từng file_uri (Files API gắn URI theo GCP Project của key upload)
_UPLOAD_FILE_KEY_OWNER: Dict[str, str] = {}

_INTERNAL_JSON_KEYS = {
    "messages", "delta_affinity", "new_mood", "reason", "reaction",
    "send_photo", "send_voice", "voice_text", "generate_photo_prompt",
    "new_fact_learned", "image_memory_summary", "web_search_query",
    "reminder_actions", "decision", "recheck_after_mins",
    "user_intent", "fact_check", "direct_answer_strategy",
    "inner_monologue", "emotional_strategy", "raw_ideas",
    "reaction_candidates", "photo_candidates", "latency_hint",
    "proactive_reason", "weight", "emoji", "type", "action",
    "kind", "delay_minutes", "due_iso_vn", "title", "context_note",
    "session_state_update", "user_activity", "dan_activity",
    "call_status", "call_note", "plan_updates", "topic",
    "status", "time_hint", "note", "durable_user_fact",
    "music_action", "duration_minutes", "alarm_time_vn", "eq_preset",
    "fan_action", "speed_pct", "step",
    "plug_action", "target", "targets",
    "habit_action", "habit_key", "device_action", "trigger_time", "temp_min", "duration_mins", "pattern",
    "fetch_tools", "query",
}
_JSON_KEY_PREFIX_RE = re.compile(r'^\s*"?([a-zA-Z0-9_]{2,40})"?\s*:')
_META_RAW_IDEA_RE = re.compile(
    r"^(?:[-*•]\s*|"
    r"(?:trêu|hỏi|khen|chê|nhắc|nũng|bật|giải thích|phản hồi|tung hứng|nêu|đặt câu|"
    r"nhận xét|bày tỏ|tạo|mở lời|khuyến khích|chia sẻ|tâm sự|chốt|xác nhận|dặn|giữ|"
    r"cà khịa|chọc|thể hiện|đáp|gợi|tỏ ra|bắt chuyện)\b)",
    re.IGNORECASE,
)

# Hạn mức thật của Gemini Free Tier: 15 request/phút cho MỖI project MỖI model
# (quotaId=GenerateRequestsPerMinutePerProjectPerModel-FreeTier). Hai key khác project
# thì độc lập nhau, nên khi 1 key bị 429 vẫn có thể dùng key còn lại ngay.
QUOTA_COOLDOWN_DEFAULT_SEC = 30.0
# (model, key_tail, scope) -> thời điểm monotonic được phép gọi lại
# - Tách riêng scope ('grounding' vs 'gen') để lỗi 429 của Google Search Grounding
#   không khóa nhầm các lượt chat / tổng hợp văn bản thông thường.
# - Dùng key_tail (8 ký tự cuối của key) để cooldown gắn đúng với project của từng key
#   dù key_role='chat' và key_role='memory' sắp xếp thứ tự key khác nhau.
_KEY_COOLDOWN: Dict[Tuple[str, str, str], float] = {}
_CHAT_RR_COUNTER = 0

# Gemini giới hạn request ~20MB; ảnh nhúng base64 phình 4/3 nên chặn tổng nhị phân ở 12MB.
MAX_INLINE_MEDIA_BYTES = 12 * 1024 * 1024

_session: Optional[aiohttp.ClientSession] = None


def get_session() -> aiohttp.ClientSession:
    """Dùng chung 1 ClientSession (keep-alive + cache DNS) thay vì mở session mới mỗi lần gọi."""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300, enable_cleanup_closed=True),
            timeout=aiohttp.ClientTimeout(
                total=REQUEST_TIMEOUT_SEC, connect=8, sock_read=REQUEST_TIMEOUT_SEC
            ),
        )
    return _session


async def close_session():
    """Đóng session chung khi bot dừng (gọi trong lúc shutdown)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def _extract_text(data: Dict[str, Any]) -> str:
    """Lấy toàn bộ phần text trong phản hồi Gemini; trả về '' nếu bị chặn / hết token."""
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        logger.warning(f"Gemini không trả về candidate nào (blockReason={feedback.get('blockReason')})")
        return ""
    first = candidates[0]
    parts_text = []
    for part in (first.get("content") or {}).get("parts") or []:
        text = part.get("text")
        if text:
            parts_text.append(text)
    if parts_text:
        return "".join(parts_text)
    logger.warning(f"Gemini trả về candidate rỗng (finishReason={first.get('finishReason')})")
    return ""


def resolve_pronoun_referent_hint(user_text: str) -> str:
    """Giữ tương thích giao diện cho Smart Butler (không ép ngôi xưng cặp đôi cố định)."""
    return ""


def user_asks_for_photo(text: str) -> bool:
    """Smart Butler không dùng kho ảnh cá nhân dựng sẵn."""
    return False

class GeminiFatalError(Exception):
    """Lỗi không thể cứu bằng retry/fallback (sai key, sai request, 4xx...)."""


class GeminiQuotaError(Exception):
    """Chạm hạn mức request/phút của toàn bộ key + model đang có (429)."""


def _parse_retry_delay(err_text: str) -> Optional[float]:
    """Đọc retryDelay ('35s') trong body lỗi 429 của Google để nghỉ đúng thời gian."""
    m = re.search(r'"retryDelay"\s*:\s*"([0-9.]+)s"', err_text or "")
    if not m:
        return None
    try:
        return max(5.0, min(120.0, float(m.group(1))))
    except ValueError:
        return None


async def call_gemini_api(model: str, system_instruction: str, contents: List[Dict[str, Any]],
                          response_json: bool = True,
                          key_role: str = "chat",
                          fallback_models: Optional[List[str]] = None,
                          timeout_sec: Optional[float] = None,
                          tools: Optional[List[Dict[str, Any]]] = None,
                          allowed_keys: Optional[List[str]] = None,
                          temperature: float = 0.70,
                          max_output_tokens: int = 2048,
                          total_deadline_sec: Optional[float] = None,
                          allow_cooldown_wait: bool = True) -> str:
    """
    Gọi trực tiếp Gemini REST API qua aiohttp - Tối ưu cực đại cho RAM 400MB.
    Có deadline tổng 42s, timeout 12s/lần, fail-fast khi mất DNS/mạng cục bộ,
    xoay vòng đa API Key, cân bằng tải luân phiên 3.1/3.5 và cooldown theo (model, key, scope).
    """
    global _CHAT_RR_COUNTER

    if fallback_models is not None:
        raw_candidates = [model] + list(fallback_models)
    elif key_role == "memory":
        raw_candidates = [model, MEMORY_MODEL, CHAT_MODEL, "gemini-flash-lite-latest"]
    else:
        # Cân bằng tải (round-robin) cho các lượt chat giữa CHAT_MODEL (3.1) và MEMORY_MODEL (3.5)
        # để tận dụng trọn vẹn hạn mức Free Tier độc lập của cả hai model (15 RPM/project/model).
        if not tools and model == CHAT_MODEL and MEMORY_MODEL and MEMORY_MODEL != CHAT_MODEL:
            use_alt_first = (_CHAT_RR_COUNTER % 2) == 1
            _CHAT_RR_COUNTER += 1
            raw_candidates = (
                [MEMORY_MODEL, CHAT_MODEL, "gemini-flash-lite-latest"]
                if use_alt_first
                else [CHAT_MODEL, MEMORY_MODEL, "gemini-flash-lite-latest"]
            )
        else:
            raw_candidates = [model, CHAT_MODEL, MEMORY_MODEL, "gemini-flash-lite-latest"]

    candidate_models: List[str] = []
    for candidate in raw_candidates:
        if candidate and candidate not in candidate_models:
            candidate_models.append(candidate)

    api_keys = get_api_keys(role=key_role)
    if not api_keys and GEMINI_API_KEY:
        api_keys = [GEMINI_API_KEY]
    if allowed_keys:
        filtered_keys = [k for k in api_keys if k in allowed_keys]
        api_keys = filtered_keys or list(allowed_keys)
    if not api_keys:
        raise GeminiFatalError("Chưa cấu hình GEMINI_API_KEY / GEMINI_API_KEYS")

    payload = {
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(max_output_tokens),
        }
    }
    if tools:
        payload["tools"] = tools
    if response_json and not tools:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    headers = {"Content-Type": "application/json"}
    per_req_timeout = timeout_sec or REQUEST_TIMEOUT_SEC
    cooldown_scope = "grounding" if tools else "gen"
    deadline_budget = total_deadline_sec if total_deadline_sec is not None else TOTAL_DEADLINE_SEC
    deadline = time.monotonic() + max(1.0, float(deadline_budget))
    started_at = time.monotonic()
    last_err: Optional[Exception] = None
    quota_hits = 0
    calls_made = 0
    min_cooldown_wait = 0.0
    connector_errors = 0
    abort_network = False

    for attempt_round in range(2):
        calls_made = 0
        quota_hits = 0
        pending_cooldowns: List[float] = []

        for cur_model in candidate_models:
            for key_idx, api_key in enumerate(api_keys):
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    break
                key_tail = api_key[-8:]
                now_mono = time.monotonic()
                cooldown_until = _KEY_COOLDOWN.get((cur_model, key_tail, cooldown_scope), 0.0)
                if cooldown_until > now_mono:
                    pending_cooldowns.append(cooldown_until - now_mono)
                    logger.debug(
                        f"Bỏ qua Gemini [{cur_model} | key#{key_idx + 1} | {cooldown_scope}] — đang cooldown "
                        f"{cooldown_until - now_mono:.0f}s sau lỗi 429"
                    )
                    continue
                url = f"{GEMINI_BASE_URL}/{cur_model}:generateContent?key={api_key}"
                attempt_started = time.monotonic()
                calls_made += 1
                try:
                    async with get_session().post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=min(per_req_timeout, remaining), connect=6),
                    ) as resp:
                        elapsed = time.monotonic() - attempt_started
                        if resp.status == 200:
                            data = await resp.json()
                            text = _extract_text(data)
                            if text:
                                logger.info(
                                    f"🤖 Gemini [{cur_model} | role={key_role} | key#{key_idx + 1}] OK ({elapsed:.1f}s)"
                                )
                                return text
                            last_err = RuntimeError("Gemini trả về nội dung rỗng (bị chặn hoặc hết token)")
                            continue
                        err_text = (await resp.text())[:300]
                        logger.warning(
                            f"Gemini [{cur_model} | key#{key_idx + 1}] HTTP {resp.status} "
                            f"sau {elapsed:.1f}s: {err_text[:200]}"
                        )
                        if resp.status == 429:
                            # Hạn mức: 15 RPM / project / model. Ghi nhớ cooldown theo đúng
                            # bộ (model, key_tail, scope) để không bắn lại vào bucket vừa bị chặn.
                            quota_hits += 1
                            delay = _parse_retry_delay(err_text) or QUOTA_COOLDOWN_DEFAULT_SEC
                            _KEY_COOLDOWN[(cur_model, key_tail, cooldown_scope)] = time.monotonic() + delay
                            pending_cooldowns.append(delay)
                            last_err = GeminiQuotaError(
                                f"Hết hạn mức {cur_model} key#{key_idx + 1} ({cooldown_scope}, nghỉ {delay:.0f}s)"
                            )
                            continue
                        if resp.status in (500, 502, 503, 504):
                            last_err = RuntimeError(f"HTTP {resp.status}: {err_text}")
                            continue  # nghẽn tạm thời -> thử key/model khác
                        if resp.status in (403, 404) or (
                            resp.status == 400 and ("api_key_invalid" in err_text.lower() or "api key" in err_text.lower())
                        ):
                            _KEY_COOLDOWN[(cur_model, key_tail, cooldown_scope)] = time.monotonic() + 300.0
                            last_err = GeminiFatalError(f"Gemini API error code: {resp.status} - {err_text}")
                            continue  # key lỗi hoặc model alias không hợp lệ -> thử key/model dự phòng
                        raise GeminiFatalError(f"Gemini API error code: {resp.status} - {err_text}")
                except asyncio.TimeoutError as e:
                    last_err = e
                    logger.warning(
                        f"Gemini [{cur_model} | key#{key_idx + 1}] timeout sau "
                        f"{time.monotonic() - attempt_started:.1f}s"
                    )
                except GeminiFatalError:
                    raise
                except (aiohttp.ClientConnectorError, socket.gaierror) as e:
                    last_err = e
                    connector_errors += 1
                    logger.warning(
                        f"Gemini [{cur_model} | key#{key_idx + 1}] lỗi mạng/DNS sau "
                        f"{time.monotonic() - attempt_started:.1f}s: {type(e).__name__}: {e}"
                    )
                    if connector_errors >= 2:
                        abort_network = True
                        break
                    if deadline - time.monotonic() > 3.0:
                        await asyncio.sleep(0.8)
                except Exception as e:
                    last_err = e
                    logger.warning(
                        f"Gemini [{cur_model} | key#{key_idx + 1}] lỗi sau "
                        f"{time.monotonic() - attempt_started:.1f}s: {type(e).__name__}: {e}"
                    )

            if abort_network or (deadline - time.monotonic() <= 1):
                break

        if abort_network:
            break

        # Không gọi được lần nào vì mọi tổ hợp đang cooldown -> chờ cái sớm nhất (tối đa 12s) rồi thử lại
        # (Bỏ qua ngủ chờ nếu đang gọi grounding tools để rơi ngay về fallback RSS)
        if calls_made == 0 and pending_cooldowns:
            wait_s = min(pending_cooldowns)
            min_cooldown_wait = wait_s
            if allow_cooldown_wait and not tools and attempt_round == 0 and wait_s <= 12.0 and (deadline - time.monotonic()) > wait_s + 3:
                logger.info(f"Tất cả key đang cooldown sau 429 — chờ {wait_s:.0f}s rồi thử lại.")
                await asyncio.sleep(wait_s + 0.5)
                continue
        break

    total = time.monotonic() - started_at
    if quota_hits and quota_hits >= calls_made:
        logger.error(
            f"Tất cả API key đều đã chạm hạn mức (15 RPM/key/model). "
            f"Đã thử {calls_made} tổ hợp trong {total:.1f}s."
        )
        raise GeminiQuotaError(
            "Tất cả key đang chạm hạn mức Gemini (15 request/phút/key). Thử lại sau ít phút."
        )
    if calls_made == 0 and min_cooldown_wait > 0:
        logger.error(f"Toàn bộ key/model đang cooldown thêm ~{min_cooldown_wait:.0f}s (429).")
        raise GeminiQuotaError(
            f"Đang bị giới hạn hạn mức Gemini, cần nghỉ thêm ~{min_cooldown_wait:.0f}s."
        )
    logger.error(
        f"Gọi Gemini thất bại sau {total:.1f}s (models={candidate_models}, keys={len(api_keys)}, "
        f"calls={calls_made}): {type(last_err).__name__ if last_err else 'unknown'}: {last_err}"
    )
    raise last_err or Exception("All Gemini model attempts failed")

async def upload_file_to_gemini(file_bytes: bytes, mime_type: str, display_name: str = "media",
                                 api_key: Optional[str] = None) -> str:
    """
    Upload file (video TikTok / GIF) lên Gemini Files API để Gemini phân tích video.
    Ghi nhớ API key đã upload (do File URI gắn theo project) để lượt gọi chat dùng đúng key đó.
    """
    candidate_keys = [api_key] if api_key else (get_api_keys(role="chat") or ([GEMINI_API_KEY] if GEMINI_API_KEY else []))
    if not candidate_keys:
        raise GeminiFatalError("Chưa cấu hình GEMINI_API_KEY / GEMINI_API_KEYS để upload media")

    session = get_session()
    init_payload = {"file": {"display_name": display_name}}
    last_err: Optional[Exception] = None

    for key_idx, chosen_key in enumerate(candidate_keys):
        url = f"{GEMINI_UPLOAD_URL}?key={chosen_key}"
        headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(file_bytes)),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        }
        try:
            async with session.post(url, headers=headers, json=init_payload,
                                    timeout=aiohttp.ClientTimeout(total=20, connect=6)) as resp:
                if resp.status != 200:
                    err_body = (await resp.text())[:200]
                    last_err = RuntimeError(f"Init upload HTTP {resp.status}: {err_body}")
                    logger.warning(f"Upload init key#{key_idx + 1} thất bại: {last_err}")
                    continue
                upload_url = resp.headers.get("X-Goog-Upload-URL")
                if not upload_url:
                    last_err = RuntimeError("Missing X-Goog-Upload-URL header")
                    continue

            upload_headers = {
                "Content-Length": str(len(file_bytes)),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            }
            async with session.post(upload_url, headers=upload_headers, data=file_bytes,
                                    timeout=aiohttp.ClientTimeout(total=60, connect=6)) as resp:
                if resp.status != 200:
                    err_body = (await resp.text())[:200]
                    last_err = RuntimeError(f"Finalize upload HTTP {resp.status}: {err_body}")
                    logger.warning(f"Upload finalize key#{key_idx + 1} thất bại: {last_err}")
                    continue
                result = await resp.json()
                file_uri = result["file"]["uri"]
                if len(_UPLOAD_FILE_KEY_OWNER) >= 64:
                    oldest_uri = next(iter(_UPLOAD_FILE_KEY_OWNER))
                    _UPLOAD_FILE_KEY_OWNER.pop(oldest_uri, None)
                _UPLOAD_FILE_KEY_OWNER[file_uri] = chosen_key
                return file_uri
        except Exception as e:
            last_err = e
            logger.warning(f"Upload media key#{key_idx + 1} lỗi: {type(e).__name__}: {e}")

    raise last_err or Exception("Upload file to Gemini failed")

def _clean_message_list(raw_msgs: Any) -> List[str]:
    cleaned: List[str] = []
    if not isinstance(raw_msgs, list):
        return cleaned
    for msg in raw_msgs:
        if not isinstance(msg, str):
            continue
        c = _sanitize_visible_bubble_text(msg)
        if c and not _is_internal_json_line(c):
            cleaned.append(c)
    return cleaned


def _finalize_parsed_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "messages" in payload:
        payload["messages"] = _clean_message_list(payload.get("messages"))
    if isinstance(payload.get("voice_text"), str):
        vt = _sanitize_voice_text(payload["voice_text"])
        payload["voice_text"] = vt or None
    if "fetch_tools" in payload:
        payload["fetch_tools"] = mcp_tools.normalize_ai_fetch_tools(payload.get("fetch_tools"))
    return payload


def parse_ai_json(raw_text: str, default_mood: str = "vui vẻ") -> Dict[str, Any]:
    """
    Phân tích chuỗi JSON từ AI một cách an toàn tuyệt đối.
    Nếu AI dùng ngoặc kép lồng ("ông tướng"), hàm vẫn trích xuất chính xác
    các tin nhắn và chỉ số, KHÔNG BAO GIỜ làm lọt raw JSON hay tag nội tâm ra ngoài.
    """
    clean_text = raw_text.strip()
    if clean_text.startswith("```"):
        clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text, flags=re.MULTILINE)
        clean_text = re.sub(r"\s*```$", "", clean_text, flags=re.MULTILINE).strip()
        
    def _try_parse_json_dict(raw_json: str) -> Optional[Dict[str, Any]]:
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            return _finalize_parsed_payload(parsed)
        if isinstance(parsed, list):
            if parsed and isinstance(parsed[0], dict):
                return _finalize_parsed_payload(parsed[0])
            if all(isinstance(x, str) for x in parsed):
                return _finalize_parsed_payload({
                    "messages": [x.strip() for x in parsed if x.strip()],
                    "delta_affinity": 0,
                    "new_mood": default_mood
                })
        return None

    last_json_err: Optional[Exception] = None
    no_trailing_commas = re.sub(r",\s*([}\]])", r"\1", clean_text)
    candidates = [clean_text] if no_trailing_commas == clean_text else [clean_text, no_trailing_commas]
    for candidate in candidates:
        try:
            parsed_payload = _try_parse_json_dict(candidate)
            if parsed_payload is not None:
                return parsed_payload
        except Exception as e:
            last_json_err = e
    if last_json_err is not None:
        logger.warning(f"json.loads failed, triggering robust regex extractor: {last_json_err}")

    result = {
        "messages": [],
        "delta_affinity": 0,
        "new_mood": default_mood,
        "reason": "",
        "user_intent": "",
        "fact_check": "",
        "direct_answer_strategy": "",
        "inner_monologue": "",
        "emotional_strategy": "",
        "proactive_reason": "",
        "raw_ideas": [],
        "reaction": None,
        "send_photo": None,
        "send_voice": False,
        "voice_text": None,
        "generate_photo_prompt": None,
        "new_fact_learned": None,
        "image_memory_summary": None,
        "web_search_query": None,
        "reminder_actions": [],
        "music_action": None,
        "fan_action": None,
        "plug_action": None,
        "habit_action": None,
        "fetch_tools": [],
        "decision": None,
        "recheck_after_mins": None,
    }

    def _extract_json_string_field(field_name: str) -> Optional[str]:
        m = re.search(
            rf'"{re.escape(field_name)}"\s*:\s*"(.+?)(?<!\\)"(?=\s*(?:,\s*"[A-Za-z_][A-Za-z0-9_]*"\s*:|\}}))',
            clean_text,
            re.DOTALL,
        )
        if not m:
            m = re.search(rf'"{re.escape(field_name)}"\s*:\s*"((?:\\.|[^"\\])+)"', clean_text)
        if not m:
            return None
        raw_val = m.group(1)
        try:
            raw_val = json.loads(f'"{raw_val}"')
        except Exception:
            raw_val = raw_val.replace('\\"', '"').replace("\\n", "\n")
        if isinstance(raw_val, str) and raw_val.strip() and raw_val.strip().lower() != "null":
            return raw_val.strip()
        return None

    def _extract_json_string_array(field_name: str, allow_truncated: bool = False, filter_internal: bool = False) -> List[str]:
        m_arr = re.search(rf'"{re.escape(field_name)}"\s*:\s*\[(.*?)(?:\]\s*,\s*"|\]\s*\}}|\])', clean_text, re.DOTALL)
        if not m_arr and allow_truncated and f'"{field_name}"' in clean_text:
            m_arr = re.search(rf'"{re.escape(field_name)}"\s*:\s*\[(.*)$', clean_text, re.DOTALL)
        if not m_arr:
            return []
        block = m_arr.group(1).strip()
        block_clean = re.sub(r",\s*$", "", block)
        try:
            parsed_arr = json.loads(f"[{block_clean}]")
            if isinstance(parsed_arr, list):
                items: List[str] = []
                for item in parsed_arr:
                    if isinstance(item, str):
                        s = item.strip()
                        if s and (not filter_internal or not _is_internal_json_line(s)):
                            items.append(s)
                return items
        except Exception:
            pass

        items = []
        for line in block.splitlines():
            line = line.strip().rstrip(',')
            if line.startswith('"') and line.endswith('"') and len(line) >= 2:
                try:
                    decoded = json.loads(line)
                    line = decoded if isinstance(decoded, str) else line[1:-1]
                except Exception:
                    line = line[1:-1]
            elif line.startswith('"') and not line.endswith('"'):
                # Phần tử cuối bị cắt giữa chừng -> bỏ qua câu dở dang thay vì gửi text cụt
                continue
            line = line.strip()
            if line and (not filter_internal or not _is_internal_json_line(line)):
                items.append(line)
        return items

    # Trích xuất delta_affinity
    m_delta = re.search(r'"delta_affinity"\s*:\s*(-?\d+)', clean_text)
    if m_delta:
        result["delta_affinity"] = int(m_delta.group(1))

    # Trích xuất send_voice
    m_sv = re.search(r'"send_voice"\s*:\s*(true|false)', clean_text, re.IGNORECASE)
    if m_sv:
        result["send_voice"] = m_sv.group(1).lower() == "true"

    for str_field in (
        "new_mood",
        "reason",
        "user_intent",
        "fact_check",
        "direct_answer_strategy",
        "inner_monologue",
        "emotional_strategy",
        "proactive_reason",
        "reaction",
        "send_photo",
        "voice_text",
        "generate_photo_prompt",
        "new_fact_learned",
        "image_memory_summary",
        "web_search_query",
        "decision",
    ):
        extracted = _extract_json_string_field(str_field)
        if extracted is not None:
            result[str_field] = extracted

    m_recheck = re.search(r'"recheck_after_mins"\s*:\s*(\d+)', clean_text)
    if m_recheck:
        result["recheck_after_mins"] = int(m_recheck.group(1))

    for act_key in ("music_action", "fan_action", "plug_action", "habit_action"):
        m_act = re.search(rf'"{act_key}"\s*:\s*(\{{[^{{}}]*\}})', clean_text, re.DOTALL)
        if m_act:
            try:
                act_obj = json.loads(m_act.group(1))
                if isinstance(act_obj, dict):
                    result[act_key] = act_obj
            except Exception:
                pass

    result["fetch_tools"] = _extract_json_string_array("fetch_tools", allow_truncated=False, filter_internal=False)
    result["raw_ideas"] = _extract_json_string_array("raw_ideas", allow_truncated=False, filter_internal=False)
    # Trích xuất khối "messages": [ ... ] (kể cả khi JSON bị cắt cụt cuối câu)
    result["messages"] = _extract_json_string_array("messages", allow_truncated=True, filter_internal=True)

    # Biện pháp phòng vệ cuối cùng: chỉ gom plain text nếu output KHÔNG phải là JSON cấu trúc Pass 1/Pass 2 bị vỡ
    looks_like_json = clean_text.lstrip().startswith("{") or any(
        f'"{k}"' in clean_text for k in _INTERNAL_JSON_KEYS
    )
    if not result["messages"] and not looks_like_json:
        clean_lines = []
        for line in clean_text.splitlines():
            line = line.strip()
            if not line or _is_internal_json_line(line):
                continue
            line = line.strip('",')
            if line and not _is_internal_json_line(line):
                clean_lines.append(line)
        result["messages"] = clean_lines

    # Đảm bảo không câu nào chứa cấu trúc raw JSON hoặc key nội bộ hoặc tag độc thoại rò rỉ
    return _finalize_parsed_payload(result)


_STAGE_BRACKET_TAG_RE = re.compile(
    r"\[(?:"
    r"internal|suy\s*nghĩ|thinking|mood|tâm\s*trạng|trạng\s*thái|strategy|"
    r"note|ghi\s*chú|system|hệ\s*thống|emotion|pass\s*[12]|json|meta"
    r")\s*:[^\]]*\]",
    re.IGNORECASE,
)
_STAGE_PAREN_PREFIX_RE = re.compile(
    r"^\s*\((?:"
    r"suy\s*nghĩ|thầm\s*nghĩ|nội\s*tâm|tâm\s*trạng|cười\s*thầm|ghi\s*chú|"
    r"thinking|internal|note|mood|trạng\s*thái"
    r")(?:\s*:|\s+)[^)]*\)\s*",
    re.IGNORECASE,
)
_THOUGHT_TAG_RE = re.compile(r"<thought>.*?</thought>", re.IGNORECASE | re.DOTALL)
_PROMPT_LABEL_LEAK_RE = re.compile(
    r"^(?:Recent_turns|assistant_response|user_intent|inner_monologue|emotional_strategy|fact_check)\s*:.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _sanitize_visible_bubble_text(text: str, preserve_voice_cues: bool = False) -> str:
    """Lọc bỏ tag độc thoại nội tâm / stage-note / nhãn prompt rò rỉ khỏi bong bóng chat gửi người dùng."""
    s = str(text or "").strip()
    if not s:
        return ""
    s = _THOUGHT_TAG_RE.sub("", s)
    s = _STAGE_BRACKET_TAG_RE.sub("", s)
    s = _PROMPT_LABEL_LEAK_RE.sub("", s)
    s = _STAGE_PAREN_PREFIX_RE.sub("", s)
    if not preserve_voice_cues:
        import tts_client
        s = tts_client._ANY_CUE_RE.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _sanitize_voice_text(text: str) -> str:
    """Làm sạch voice_text: giữ 8 cue biểu cảm v2.2 & *nhấn* hợp lệ, bỏ thẻ lạ và dấu * lẻ."""
    s = _sanitize_visible_bubble_text(text, preserve_voice_cues=True)
    if not s:
        return ""
    import tts_client
    s = tts_client._STRAY_BRACKET_RE.sub("", s)
    s = tts_client.strip_balanced_stray_asterisks(s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def _is_internal_json_line(line: str) -> bool:
    """True nếu dòng trông giống mảnh JSON hoặc trường nội bộ Pass 1/Pass 2."""
    s = (line or "").strip()
    if not s or s in {"{", "}", "[", "]", "],", "},", ","}:
        return True
    if s.startswith("{") or s.startswith("}") or s.startswith("]"):
        return True
    if s.startswith("[") and (
        s.startswith("[\"")
        or s.startswith("[{")
        or s.rstrip(",").endswith("]")
    ):
        return True
    m_key = _JSON_KEY_PREFIX_RE.match(s)
    if m_key and (m_key.group(1).lower() in _INTERNAL_JSON_KEYS or "_" in m_key.group(1)):
        return True
    return any(f'"{k}"' in s for k in _INTERNAL_JSON_KEYS)


def _resolve_due_timestamp(
    delay_minutes: Any = None,
    due_iso_vn: Any = None,
    now_ts: Optional[float] = None,
) -> Optional[float]:
    """Quy đổi delay_minutes hoặc chuỗi giờ VN ('YYYY-MM-DD HH:MM' / 'HH:MM' / '11h30') sang Unix timestamp UTC."""
    base_now = float(now_ts if now_ts is not None else time.time())
    if delay_minutes is not None and str(delay_minutes).lower() not in ("null", "none", ""):
        try:
            mins = float(delay_minutes)
            if 0.1 <= mins <= 43200:
                return base_now + mins * 60.0
        except (TypeError, ValueError):
            pass

    if isinstance(due_iso_vn, str):
        raw = due_iso_vn.strip()
        if raw and raw.lower() not in ("null", "none"):
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt_vn = datetime.datetime.strptime(raw[:19], fmt).replace(tzinfo=VN_TZ)
                    ts = dt_vn.timestamp()
                    if ts >= base_now - 90:
                        return max(base_now + 5.0, ts)
                    return None
                except ValueError:
                    continue
            import diary_helper
            parsed_ts = diary_helper.parse_vn_clock_to_ts(raw, now_ts=base_now, roll_forward_if_past=True)
            if parsed_ts is not None:
                return max(base_now + 5.0, parsed_ts)
    return None


_EXPLICIT_REMINDER_ASK_RE = re.compile(
    r"(?:"
    r"(?:nhớ\s+)?nhắc\s+(?:giùm\s+|hộ\s+|cho\s+)?(?:anh|mình)"
    r"|đặt\s+(?:lịch|nhắc|hẹn|báo\s*thức)"
    r"|lên\s+lịch\s+(?:nhắc|hẹn)"
    r"|báo\s+thức\s+(?:cho\s+|giúp\s+)?(?:anh|mình)"
    r"|(?:nhớ\s+)?(?:kêu|gọi)\s+(?:anh|mình)\s+(?:dậy|làm|học|uống|ăn|đi|với|nha|nhé)"
    r"|(?:gọi|kêu)\s+dậy\s+lúc"
    r"|(?:tới|đến)\s+giờ\s+(?:nhớ\s+)?(?:nhắc|kêu|gọi)"
    r"|hẹn\s+(?:lịch|giờ|nhắc)"
    r"|remind\s+me"
    r"|set\s+(?:a\s+)?reminder"
    r")",
    re.IGNORECASE,
)

_EXPLICIT_PROMISE_RE = re.compile(
    r"(?:"
    r"\b(?:tôi|mình|anh|em)\s+(?:xin\s+)?hứa\b"
    r"|\bhứa\s+(?:chắc|luôn|nha|nhé|đó|nè)\b"
    r"|\bchốt\s+(?:lịch|nha|nhé|luôn|nhé)\b"
    r"|\bchắc\s+chắn\s+(?:sẽ|nha|nhé)\b"
    r"|\bnhất\s+định\s+(?:sẽ|làm|về)\b"
    r"|\bđảm\s+bảo\b"
    r")",
    re.IGNORECASE,
)


def _has_explicit_user_reminder_intent(user_text: str) -> bool:
    """True chỉ khi user thực sự nhờ bot nhắc/đặt lịch/báo thức (tránh câu chào 'hẹn gặp lại')."""
    if not user_text or not isinstance(user_text, str):
        return False
    return bool(_EXPLICIT_REMINDER_ASK_RE.search(user_text))


def _has_explicit_promise_signal(
    user_text: str,
    session_state: Optional[Dict[str, Any]] = None,
) -> bool:
    """True khi user thực sự cam kết/chốt lịch trong câu hoặc đang có promise active trong session."""
    if user_text and isinstance(user_text, str) and _EXPLICIT_PROMISE_RE.search(user_text):
        return True
    if session_state and isinstance(session_state, dict):
        for plan in (session_state.get("today_plans") or []):
            if plan.get("status", "planned") == "planned" and plan.get("kind") == "promise":
                return True
    return False


def _sanitize_reminder_promise_phrase(text: str, allow_promise: bool) -> str:
    """Loại bỏ chữ 'như đã hứa/chốt kèo' bịa đặt nếu user chỉ thông báo bình thường."""
    clean = str(text or "").strip()
    if not clean or allow_promise:
        return clean
    clean = re.sub(r",?\s*(?:như\s+)?(?:đã\s+)?hứa", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r",?\s*giữ\s+lời\s+hứa(?:\s+(?:đó|nha|nhé))?", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r",?\s*(?:theo\s+|đúng\s+)?chốt\s+kèo", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s{2,}", " ", clean).strip(" ,;-.")
    return clean


def _format_turn_content(msg: Dict[str, Any], now_ts: Optional[float] = None) -> str:
    """Gắn mốc thời gian VN tương đối ([Hôm nay HH:MM] / [Hôm qua HH:MM] / [dd/mm HH:MM]) vào lịch sử."""
    content = str(msg.get("content") or "")
    if not content or re.match(r"^\[(?:Hôm nay|Hôm qua|\d{2}/\d{2})\s+\d{2}:\d{2}\]", content):
        return content
    raw_ts = msg.get("timestamp") if msg.get("timestamp") is not None else msg.get("created_at")
    if not raw_ts:
        return content
    import diary_helper
    label = diary_helper.format_relative_vn_ts(raw_ts, now_ts=now_ts)
    if label:
        return f"[{label}] {content}"
    return content


def _build_history_contents(messages: List[Dict[str, Any]], now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """Chuyển lịch sử chat sang Gemini contents kèm dải phân cách khi có khoảng lặng >= 2 giờ."""
    import diary_helper
    base_now = float(now_ts if now_ts is not None else time.time())
    contents: List[Dict[str, Any]] = []
    prev_ts: Optional[float] = None
    for msg in messages or []:
        role = "user" if msg.get("role") == "user" else "model"
        raw_ts = msg.get("timestamp") if msg.get("timestamp") is not None else msg.get("created_at")
        cur_ts = diary_helper.parse_ts_float(raw_ts)
        formatted = _format_turn_content(msg, now_ts=base_now)
        if prev_ts is not None and cur_ts is not None and (cur_ts - prev_ts) >= 7200.0:
            gap_label = diary_helper.format_vn_duration(cur_ts - prev_ts)
            cur_label = diary_helper.format_relative_vn_ts(cur_ts, now_ts=base_now)
            formatted = f"--- (Sau khoảng lặng {gap_label}, tới {cur_label}) ---\n{formatted}"
        contents.append({"role": role, "parts": [{"text": formatted}]})
        if cur_ts is not None:
            prev_ts = cur_ts
    return contents


def _format_current_user_turn_text(
    text_content: str,
    recent: Optional[List[Dict[str, Any]]] = None,
    now_ts: Optional[float] = None,
) -> str:
    """Gắn mốc [Hôm nay HH:MM] và dải phân cách khoảng lặng (nếu >= 2h) cho tin nhắn hiện tại của Phát."""
    import diary_helper
    base_now = float(now_ts if now_ts is not None else time.time())
    text_val = str(text_content or "")
    cur_label = diary_helper.format_relative_vn_ts(base_now, now_ts=base_now)
    if cur_label and text_val and not re.match(r"^\[(?:Hôm nay|Hôm qua|\d{2}/\d{2})\s+\d{2}:\d{2}\]", text_val):
        tagged = f"[{cur_label}] {text_val}"
    else:
        tagged = text_val

    if recent:
        for msg in reversed(recent):
            raw_ts = msg.get("timestamp") if msg.get("timestamp") is not None else msg.get("created_at")
            prev_ts = diary_helper.parse_ts_float(raw_ts)
            if prev_ts is not None:
                if (base_now - prev_ts) >= 7200.0 and cur_label:
                    gap_label = diary_helper.format_vn_duration(base_now - prev_ts)
                    tagged = f"--- (Sau khoảng lặng {gap_label}, tới {cur_label}) ---\n{tagged}"
                break
    return tagged


def _sanitize_session_state_update(
    update: Any,
    user_text: str,
    prev_state: Optional[Dict[str, Any]] = None,
) -> Any:
    """Ngăn Pass 1 tự ý nâng cấp thông báo thường thành 'promise' khi user không hứa/chốt kèo."""
    if not isinstance(update, dict):
        return update
    plan_updates = update.get("plan_updates")
    if not isinstance(plan_updates, list) or not plan_updates:
        return update

    has_text_promise = bool(user_text and _EXPLICIT_PROMISE_RE.search(user_text))
    if has_text_promise:
        return update

    prev_promises: List[str] = [
        str(p.get("topic") or "").strip().lower()
        for p in ((prev_state or {}).get("today_plans") or [])
        if p.get("kind") == "promise" and p.get("topic")
    ]
    cleaned_plans = []
    for pu in plan_updates:
        if not isinstance(pu, dict):
            continue
        pu_copy = dict(pu)
        if str(pu_copy.get("kind") or "").strip().lower() == "promise":
            topic_low = str(pu_copy.get("topic") or "").strip().lower()
            was_existing_promise = any(
                (topic_low in pp or pp in topic_low) for pp in prev_promises if topic_low
            )
            if not was_existing_promise:
                pu_copy["kind"] = "info"
        cleaned_plans.append(pu_copy)

    out = dict(update)
    out["plan_updates"] = cleaned_plans
    return out


def _apply_reminder_actions(
    user_id: int,
    actions: Any,
    exclude_id: Optional[int] = None,
    source_user_text: str = "",
    allow_promise: bool = False,
    out_added: Optional[List[Tuple[int, str]]] = None,
) -> int:
    """Thực thi danh sách reminder_actions do Pass 1 đề xuất vào SQLite."""
    if not actions or not isinstance(actions, list):
        return 0
    import commitment_helper
    applied = 0
    explicit_ask = _has_explicit_user_reminder_intent(source_user_text) if source_user_text.strip() else True
    has_deferred_promise = commitment_helper.has_deferred_promise_intent(source_user_text, "") if source_user_text.strip() else False
    for item in actions:
        if not isinstance(item, dict):
            continue
        act = str(item.get("action") or "").strip().lower()
        if act == "add":
            due_ts = _resolve_due_timestamp(item.get("delay_minutes"), item.get("due_iso_vn"))
            raw_title = str(item.get("title") or "").strip()
            raw_kind = str(item.get("kind") or "user_reminder").strip().lower()
            kind = raw_kind if raw_kind in ("user_reminder", "ai_promise", "ai_followup") else "user_reminder"
            if kind == "user_reminder" and source_user_text.strip() and not explicit_ask:
                kind = "ai_promise" if has_deferred_promise else "ai_followup"
                logger.info(
                    f"⏰ [Reminder DOWNGRADE -> {kind}]: user không nhờ nhắc trong '{source_user_text[:40]}'"
                )
            allow_p = allow_promise or (kind == "ai_promise")
            title = _sanitize_reminder_promise_phrase(raw_title, allow_promise=allow_p) or raw_title
            if due_ts and title:
                note = _sanitize_reminder_promise_phrase(
                    str(item.get("context_note") or "").strip(),
                    allow_promise=allow_p,
                )
                rid = database.create_reminder(user_id, kind, title, due_ts, context_note=note)
                due_vn = datetime.datetime.fromtimestamp(due_ts, tz=VN_TZ).strftime("%Y-%m-%d %H:%M")
                logger.info(f"⏰ [Reminder ADD #{rid} | {kind}]: '{title}' lúc {due_vn}")
                if out_added is not None:
                    out_added.append((rid, kind))
                applied += 1
        elif act == "update":
            rid = item.get("id")
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                continue
            if exclude_id is not None and rid_int == int(exclude_id):
                continue
            raw_delay = item.get("delay_minutes")
            raw_due = item.get("due_iso_vn")
            has_time_input = (
                (raw_delay is not None and str(raw_delay).strip().lower() not in ("", "null", "none"))
                or (isinstance(raw_due, str) and raw_due.strip().lower() not in ("", "null", "none"))
            )
            due_ts = _resolve_due_timestamp(raw_delay, raw_due)
            if has_time_input and due_ts is None:
                logger.warning(
                    f"⏰ [Reminder UPDATE #{rid_int}]: không parse được mốc giờ ({raw_delay=}, {raw_due=}) -> giữ nguyên giờ cũ"
                )
            title = item.get("title") if isinstance(item.get("title"), str) else None
            note = item.get("context_note") if isinstance(item.get("context_note"), str) else None
            if (due_ts is not None or title is not None or note is not None) and database.update_reminder(
                user_id, rid_int, due_at=due_ts, title=title, context_note=note, preserve_orig_due=False
            ):
                due_desc = (
                    datetime.datetime.fromtimestamp(due_ts, tz=VN_TZ).strftime("%Y-%m-%d %H:%M")
                    if due_ts is not None
                    else "unchanged"
                )
                logger.info(f"⏰ [Reminder UPDATE #{rid_int}]: title={title}, due={due_desc}")
                applied += 1
        elif act == "cancel":
            rid = item.get("id")
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                continue
            if exclude_id is not None and rid_int == int(exclude_id):
                continue
            if database.update_reminder(user_id, rid_int, status="cancelled"):
                logger.info(f"⏰ [Reminder CANCEL #{rid_int}]")
                applied += 1
    return applied


_LINK_CONTEXT_WRAPPER_RE = re.compile(
    r"\n*\[(?:Nội dung tóm tắt từ link|Người dùng vừa gửi cho bạn xem 1 video TikTok)[\s\S]*$",
    re.IGNORECASE,
)


def close_fulfilled_promises_by_text(
    user_id: int,
    bot_text: str,
    now_ts: Optional[float] = None,
    min_age_sec: float = 5.0,
) -> int:
    """
    Kiểm tra nếu tin nhắn Đan vừa gửi thực sự đã báo hoàn tất một `ai_promise` hoặc `open_loop`
    đang mở (vd 'em ăn xong rồi nè') thì tự đóng để không lặp lại ở lượt sau.
    """
    import commitment_helper

    full_bot = str(bot_text or "").strip()
    if not full_bot:
        return 0
    now = float(now_ts if now_ts is not None else time.time())
    closed = 0

    for rem in database.get_pending_reminders(user_id, kind="ai_promise", limit=8):
        if float(rem.get("created_at") or 0.0) >= (now - min_age_sec):
            continue
        if commitment_helper.is_promise_fulfilled_by_text(
            str(rem.get("title") or ""),
            str(rem.get("context_note") or ""),
            full_bot,
        ):
            rid = int(rem["id"])
            if database.update_reminder(user_id, rid, status="cancelled"):
                database.resolve_open_loops_for_promise(
                    user_id, str(rem.get("title") or ""), str(rem.get("context_note") or ""), now_ts=now
                )
                logger.info(f"⏰ [Promise Auto-Close #{rid}]: Đan vừa báo xong -> đóng '{rem.get('title')}'")
                closed += 1

    for loop in database.get_open_loops(user_id, include_resolved=False, now_ts=now):
        if float(loop.get("created_at") or 0.0) >= (now - min_age_sec):
            continue
        if commitment_helper.is_promise_fulfilled_by_text(
            str(loop.get("topic") or ""),
            str(loop.get("note") or ""),
            full_bot,
        ):
            if database.resolve_open_loop_by_id(user_id, str(loop.get("id") or ""), now_ts=now):
                logger.info(f"🧵 [Open Loop Auto-Close]: Đan vừa báo xong -> đóng '{loop.get('topic')}'")
                closed += 1
    return closed


def _sync_turn_deferred_promises(
    user_id: int,
    user_text: str,
    final_messages: List[str],
    voice_text: Optional[str],
    p1_added: List[Tuple[int, str]],
    now_ts: float,
) -> None:
    """
    Hậu kiểm sau Pass 2 để bảo đảm mọi lời hẹn/dặn 'X xong nhắn anh' đều có `ai_promise`,
    và tự đóng các `ai_promise` cũ nếu Đan vừa báo đã làm xong trong lượt này.
    """
    import commitment_helper

    clean_user = _LINK_CONTEXT_WRAPPER_RE.sub("", str(user_text or "")).strip()
    bot_parts = [str(m).strip() for m in (final_messages or []) if isinstance(m, str) and m.strip()]
    if isinstance(voice_text, str) and voice_text.strip():
        bot_parts.append(voice_text.strip())
    full_bot = " ".join(bot_parts).strip()

    close_fulfilled_promises_by_text(user_id, full_bot, now_ts=now_ts, min_age_sec=5.0)

    added_kinds = {k for _, k in p1_added}
    if p1_added:
        if "ai_followup" in added_kinds and commitment_helper.has_deferred_promise_intent(clean_user, full_bot):
            conn = database.get_connection()
            try:
                for rid, k in p1_added:
                    if k == "ai_followup":
                        conn.execute(
                            "UPDATE reminders SET kind = 'ai_promise' WHERE id = ? AND user_id = ? AND status = 'pending'",
                            (int(rid), user_id),
                        )
                        logger.info(f"⏰ [Promise PROMOTE #{rid}]: chuyển ai_followup -> ai_promise để không bị hủy oan")
                conn.commit()
            finally:
                conn.close()
        return

    extracted = commitment_helper.extract_deferred_promise(
        user_text=clean_user,
        bot_text=full_bot,
        now_ts=now_ts,
    )
    if not extracted:
        return

    # Tránh ghi đè lời hứa cụ thể vừa đặt (vd "sau khi ăn xong") chỉ vì lượt chào tiếp theo nói "xíu xong em nhắn"
    existing_promises = database.get_pending_reminders(user_id, kind="ai_promise", limit=5)
    for ex in existing_promises:
        age_sec = max(0.0, now_ts - float(ex.get("created_at") or 0.0))
        if age_sec <= 2700.0:
            ex_title = str(ex.get("title") or "").lower()
            if extracted["activity_topic"] in ex_title or extracted["activity_topic"] in ("sau vài phút nữa", "khi xong việc"):
                return

    rid = database.create_reminder(
        user_id,
        "ai_promise",
        extracted["title"],
        extracted["due_at"],
        context_note=extracted["context_note"],
    )
    due_vn = datetime.datetime.fromtimestamp(extracted["due_at"], tz=VN_TZ).strftime("%Y-%m-%d %H:%M")
    logger.info(
        f"⏰ [Promise Auto-Add #{rid} | {extracted['source']}]: "
        f"'{extracted['title']}' lúc {due_vn} (~{extracted['delay_mins']:.0f}p)"
    )


def _extract_recent_bot_texts(recent: List[Dict[str, Any]], limit: int = 4) -> List[str]:
    """Lấy danh sách các tin nhắn gần nhất của Đan (cả chữ lẫn voice) để chống lặp câu chữ / mở đầu / ý cũ."""
    texts: List[str] = []
    for msg in reversed(recent or []):
        if msg.get("role") == "user":
            continue
        raw = str(msg.get("content") or "").strip()
        if not raw:
            continue
        cleaned = re.sub(r"\[Voice note\]:\s*", "", raw)
        texts.append(cleaned)
        if len(texts) >= limit:
            break
    return texts


def _normalize_text_for_dup(text: str) -> str:
    """Chuẩn hóa văn bản tiếng Việt viết thường, bỏ dấu câu thừa và tag biểu cảm để so sánh độ trùng lặp."""
    import tts_client
    s = re.sub(r"https?://\S+", " ", str(text or "").lower())
    s = re.sub(r"\[voice note\]\s*:?", " ", s, flags=re.IGNORECASE)
    s = tts_client._ANY_CUE_RE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return " ".join(s.split())


def _opening_5words(text: str) -> str:
    """Trích cụm 5 từ mở đầu (nếu câu dài >= 8 từ) để phát hiện mở bài rập khuôn giữa các lượt."""
    words = _normalize_text_for_dup(text).split()
    if len(words) < 8:
        return ""
    return " ".join(words[:5])


def _is_pair_similar(a: str, b: str, ratio_thresh: float = 0.72, jaccard_thresh: float = 0.68) -> bool:
    """Kiểm tra hai đoạn văn bản đủ dài có trùng lặp đáng kể không."""
    na = _normalize_text_for_dup(a)
    nb = _normalize_text_for_dup(b)
    if len(na) < 35 or len(nb) < 35:
        return False
    if na == nb or na in nb or nb in na:
        return True
    if difflib.SequenceMatcher(None, na, nb).ratio() >= ratio_thresh:
        return True
    wa, wb = set(na.split()), set(nb.split())
    if min(len(wa), len(wb)) >= 10:
        jaccard = len(wa & wb) / max(1, len(wa | wb))
        if jaccard >= jaccard_thresh:
            return True
    return False


def _find_repetitive_previous_msg(
    candidate_messages: List[str],
    recent_bot_texts: List[str],
    candidate_voice: Optional[str] = None,
) -> Optional[str]:
    """
    Trả về nội dung tin nhắn cũ bị lặp nếu câu trả lời mới (chữ hoặc voice) giống >~72% tin Đan vừa gửi
    hoặc dùng lại đúng cụm 5 từ mở đầu rập khuôn gần đây.
    """
    if not recent_bot_texts:
        return None
    clean_cands = [str(m).strip() for m in (candidate_messages or []) if isinstance(m, str) and str(m).strip()]
    if isinstance(candidate_voice, str) and candidate_voice.strip():
        clean_cands.append(candidate_voice.strip())
    if not clean_cands:
        return None
    full_cand = " ".join(clean_cands)

    for prev in recent_bot_texts:
        prev_segments = [s.strip() for s in re.split(r"[.!?\n|]+", prev) if len(s.strip()) >= 28]
        prev_openings = {op for op in (_opening_5words(s) for s in [prev] + prev_segments) if op}
        if _is_pair_similar(full_cand, prev):
            return prev
        for bubble in clean_cands:
            if _is_pair_similar(bubble, prev, ratio_thresh=0.75):
                return prev
            for seg in prev_segments:
                if _is_pair_similar(bubble, seg, ratio_thresh=0.78, jaccard_thresh=0.72):
                    return prev
            for cand_seg in [bubble] + [s.strip() for s in re.split(r"[.!?\n]+", bubble) if len(s.strip()) >= 24]:
                op = _opening_5words(cand_seg)
                if op and op in prev_openings:
                    return prev
        # So riêng câu mở đầu của 2 lượt chat để tránh mở bài lặp khuôn giữa các lượt liên tiếp
        first_cand_seg = next((s.strip() for s in re.split(r"[.!?\n|]+", clean_cands[0]) if len(s.strip()) >= 28), "")
        if first_cand_seg and prev_segments:
            if _is_pair_similar(first_cand_seg, prev_segments[0], ratio_thresh=0.78, jaccard_thresh=0.72):
                return prev
    return None


_PLAN_PHOTO_PROMISE_RE = re.compile(
    r"(?:gửi|khoe|thưởng|tặng|đính\s*kèm)\s*(?:cho\s*anh\s*|anh\s*)?(?:thêm\s*)?(?:tấm|bức|1|một)?\s*(?:ảnh|hình|tấm\s*này|tấm\s*ảnh|tấm\s*hình|selfie)",
    re.IGNORECASE,
)
_PLAN_PHOTO_DEFER_RE = re.compile(
    r"(?:chưa\s*gửi|không\s*gửi|hong\s*gửi|hẹn\s*anh|lần\s*sau|lúc\s*khác|không\s*kèm\s*ảnh)",
    re.IGNORECASE,
)


def _has_photo_promise_in_plan(eval_or_p1_data: Dict[str, Any]) -> bool:
    """True nếu chiến lược/ý tưởng Pass 1 hứa gửi hoặc thưởng thêm ảnh ngay trong lượt này."""
    if not isinstance(eval_or_p1_data, dict):
        return False
    candidates: List[str] = []
    direct = eval_or_p1_data.get("direct_answer_strategy")
    if isinstance(direct, str) and direct.strip():
        candidates.append(direct.strip())
    raw_ideas = eval_or_p1_data.get("raw_ideas")
    if isinstance(raw_ideas, list):
        candidates.extend(str(item).strip() for item in raw_ideas if isinstance(item, str) and item.strip())
    for text in candidates:
        if _PLAN_PHOTO_PROMISE_RE.search(text) and not _PLAN_PHOTO_DEFER_RE.search(text):
            return True
    return False


def _resolve_photo_selection(
    user_id: int,
    eval_or_p1_data: Dict[str, Any],
    text_content: str = "",
    allow_explicit_fallback: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Smart Butler không dùng kho ảnh cá nhân dựng sẵn."""
    return None, None


async def chat_with_linh_dan(user_id: int, user_message: str, 
                            photo_bytes: Optional[bytes] = None, 
                            media_items: Optional[List[Tuple[bytes, str]]] = None,
                            video_file_uri: Optional[str] = None,
                            prev_last_user_time: Optional[float] = None,
                            current_message_saved: bool = True,
                            exclude_reminder_id: Optional[int] = None,
                            allow_reminder_mutations: bool = True,
                            extra_system_note: Optional[str] = None,
                            turn_kind: str = "reply",
                            defer_commit: bool = False) -> Dict[str, Any]:
    """
    Tương tác nâng cao qua kiến trúc Dual-Pass (2 bước AI độc lập):
    - Pass 1: Độc thoại nội tâm, tâm lý, chiến thuật cảm xúc & đánh giá điểm số (30 tin lịch sử + Nhật ký + Ký ức).
    - Pass 2: Bản năng Gen Z chuốt 1-3 bong bóng chat tự nhiên 100%.

    current_message_saved: True nếu lượt nhắn hiện tại ĐÃ được lưu vào chat_history trước khi gọi
    (bot.py luôn lưu trước) -> bỏ dòng cuối để không gửi lặp 2 lần. Scheduler chủ động nhắn thì để False.
    """
    user_state = database.get_or_create_user(user_id)
    affinity = user_state["affinity_score"]
    mood = user_state["mood"]

    # 1. Lấy lịch sử 40 tin gần nhất trước để cung cấp ngữ cảnh đầy đủ mốc thời gian cho bộ truy xuất trí nhớ
    recent = database.get_recent_history(user_id, limit=40)
    if recent and recent[-1]["role"] == "user" and current_message_saved:
        recent = recent[:-1]

    text_content = user_message.strip() if user_message else "Gửi ảnh/video cho bạn xem nè."

    # 2. Chạy song song: Truy xuất Trí nhớ Đa Model (Query Rewriter -> Vector Search -> Synthesizer) & Nhật ký hôm nay
    import vector_memory
    import trend_helper
    import diary_helper

    today_str = trend_helper.get_today_date_str()
    daily_trends = database.get_today_trends(today_str) or ""

    mem_res, daily_diary = await asyncio.gather(
        vector_memory.retrieve_memory_context_with_status(user_id, text_content, recent_history=recent),
        diary_helper.get_or_create_today_diary(),
    )
    memories = mem_res.get("memories") or []
    memory_miss = bool(mem_res.get("memory_miss"))
    missed_queries = mem_res.get("queries") or []

    now_ts = time.time()
    last_user_time = prev_last_user_time if prev_last_user_time is not None else user_state.get("last_user_message_time", 0.0)
    time_ctx = diary_helper.get_vietnam_time_context(last_user_time, now_ts=now_ts)
    current_act = diary_helper.extract_current_session_activity(daily_diary, time_ctx["session_key"])
    mood_hint = diary_helper.build_situational_mood_context(
        current_mood=mood,
        mood_updated_at=float(user_state.get("mood_updated_at") or 0.0),
        last_user_time=last_user_time,
        today_trends=daily_trends,
        now_ts=now_ts,
    )
    personality_traits = {
        "tease_level": user_state.get("tease_level", database.PERSONALITY_DEFAULTS["tease_level"]),
        "stubborn_level": user_state.get("stubborn_level", database.PERSONALITY_DEFAULTS["stubborn_level"]),
        "clingy_level": user_state.get("clingy_level", database.PERSONALITY_DEFAULTS["clingy_level"]),
    }

    all_media: List[Tuple[bytes, str]] = list(media_items or [])
    if photo_bytes:
        all_media.insert(0, (photo_bytes, "image/jpeg"))

    # Chặn tổng dung lượng ảnh nhúng: base64 phình 4/3 nên phải giữ dưới ~15MB request của Gemini
    capped_media: List[Tuple[bytes, str]] = []
    total_media_bytes = 0
    for m_bytes, m_mime in all_media:
        size = len(m_bytes or b"")
        if capped_media and (total_media_bytes + size) > MAX_INLINE_MEDIA_BYTES:
            logger.warning(
                f"Bỏ bớt 1 ảnh {size / 1024 / 1024:.1f}MB vì tổng vượt giới hạn "
                f"{MAX_INLINE_MEDIA_BYTES / 1024 / 1024:.0f}MB của request Gemini"
            )
            continue
        capped_media.append((m_bytes, m_mime))
        total_media_bytes += size
    all_media = capped_media

    pending_reminders = database.get_pending_reminders(user_id, limit=12)
    if exclude_reminder_id is not None:
        pending_reminders = [r for r in pending_reminders if int(r["id"]) != int(exclude_reminder_id)]

    if current_message_saved:
        session_state = database.clear_stale_user_sleep_state_on_message(
            user_id, text_content, now_ts=now_ts
        )
    else:
        session_state = database.get_session_state(user_id)
    session_state_text = database.format_session_state_for_prompt(session_state, now_ts=now_ts)

    # Pass 1 System Instruction (Chiến lược & suy nghĩ ngữ cảnh thời gian thực)
    import circadian
    import tts_client
    explicit_voice_req = tts_client.user_asks_for_voice(text_content, recent)
    voice_cooldown_active = False
    try:
        raw_lv = database.get_metadata(f"last_voice_ts:{user_id}")
        last_voice_ts = float(raw_lv) if raw_lv else 0.0
    except Exception:
        last_voice_ts = 0.0
    if not explicit_voice_req:
        recent_bot_turns = [m for m in recent if m.get("role") != "user"][-2:]
        recent_had_voice = any("[Voice note]:" in str(m.get("content") or "") for m in recent_bot_turns)
        if (last_voice_ts > 0.0 and (now_ts - last_voice_ts) < 240.0) or recent_had_voice:
            voice_cooldown_active = True

    pass1_memories = list(memories)
    if extra_system_note:
        pass1_memories.append(extra_system_note)
    if voice_cooldown_active:
        pass1_memories.append(
            "Ghi chú nhịp thoại: Vừa gửi voice note cách đây ít phút — lượt này hãy để send_voice=false và trả lời bằng tin nhắn chữ rõ ràng."
        )
    circadian_text = circadian.format_circadian_for_prompt()
    open_loops_text = database.format_open_loops_for_prompt(
        user_id, limit=2, cooldown_sec=7200.0, mark_surfaced=allow_reminder_mutations, now_ts=now_ts
    )
    # Tránh nhồi thông tin thiết bị phòng cố định theo từ khóa vào Pass 1 (Pass 1 tự chọn `fetch_tools` MCP khi cần)
    now_playing_text = database.format_now_playing_for_prompt()
    temporal_notes = diary_helper.build_temporal_comparison_hints(
        text_content,
        now_ts=now_ts,
        pending_reminders=pending_reminders,
        open_loops=database.get_open_loops(user_id, now_ts=now_ts),
    )

    pass1_system_instruction = character_bible.build_pass1_system_instruction(
        affinity, mood, pass1_memories, daily_trends, daily_diary,
        time_context=time_ctx, current_activity=current_act,
        personality_traits=personality_traits,
        memory_miss=memory_miss,
        missed_queries=missed_queries,
        situational_mood_hint=mood_hint,
        pending_reminders=pending_reminders,
        has_media=bool(all_media or video_file_uri),
        session_state_text=session_state_text,
        circadian_text=circadian_text,
        open_loops_text=open_loops_text,
        now_playing_text=now_playing_text,
        temporal_notes=temporal_notes,
    )

    contents = _build_history_contents(recent, now_ts=now_ts)

    # Tạo nội dung lượt nhắn hiện tại của user (hỗ trợ nhiều ảnh, file ảnh, sticker)
    current_parts = []
    for m_bytes, m_mime in all_media:
        b64_img = base64.b64encode(m_bytes).decode("utf-8")
        current_parts.append({
            "inline_data": {
                "mime_type": m_mime or "image/jpeg",
                "data": b64_img
            }
        })
    if video_file_uri:
        current_parts.append({
            "file_data": {
                "mime_type": "video/mp4",
                "file_uri": video_file_uri
            }
        })

    current_user_text = _format_current_user_turn_text(text_content, recent=recent, now_ts=now_ts)
    current_parts.append({"text": current_user_text})
    contents.append({"role": "user", "parts": current_parts})
    
    # === BƯỚC 1: PASS 1 - CHIẾN LƯỢC TÂM LÝ & ĐỘC THOẠI NỘI TÂM ===
    pinned_keys = None
    if video_file_uri and video_file_uri in _UPLOAD_FILE_KEY_OWNER:
        pinned_keys = [_UPLOAD_FILE_KEY_OWNER[video_file_uri]]

    raw_p1 = await call_gemini_api(
        model=CHAT_MODEL,
        system_instruction=pass1_system_instruction,
        contents=contents,
        response_json=True,
        allowed_keys=pinned_keys,
    )
    p1_data = parse_ai_json(raw_p1, default_mood=mood)
    
    inner_monologue = p1_data.get("inner_monologue", "")
    emotional_strategy = p1_data.get("emotional_strategy", "")
    raw_ideas = p1_data.get("raw_ideas", [])
    if inner_monologue:
        logger.info(f"💭 [Butler Reasoning]: {inner_monologue}")
    if emotional_strategy:
        logger.info(f"🎯 [Emotional Strategy]: {emotional_strategy}")

    # Cập nhật Live Session State trước khi Pass 2 chạy (khi defer_commit=True chỉ dựng preview trong RAM, ghi DB sau khi gửi Telegram OK)
    p1_added_reminders: List[Tuple[int, str]] = []
    deferred_payload: Dict[str, Any] = {}
    if allow_reminder_mutations:
        sanitized_ss_update = _sanitize_session_state_update(
            p1_data.get("session_state_update"),
            text_content,
            prev_state=session_state,
        )
        if defer_commit:
            session_state, _ = database.merge_session_state_update(session_state, sanitized_ss_update, now_ts=now_ts)
            session_state_text = database.format_session_state_for_prompt(session_state, now_ts=now_ts)
            deferred_payload = {
                "user_id": user_id,
                "now_ts": now_ts,
                "allow_reminder_mutations": True,
                "exclude_reminder_id": exclude_reminder_id,
                "source_user_text": text_content,
                "sanitized_ss_update": sanitized_ss_update,
                "reminder_actions": p1_data.get("reminder_actions"),
                "allow_promise": _has_explicit_promise_signal(text_content, session_state),
            }
        else:
            session_state = database.apply_session_state_update(user_id, sanitized_ss_update)
            session_state_text = database.format_session_state_for_prompt(session_state, now_ts=now_ts)
            allow_promise = _has_explicit_promise_signal(text_content, session_state)
            _apply_reminder_actions(
                user_id,
                p1_data.get("reminder_actions"),
                exclude_id=exclude_reminder_id,
                source_user_text=text_content,
                allow_promise=allow_promise,
                out_added=p1_added_reminders,
            )
            database.reconcile_reminders_with_session_state(user_id, session_state)

    # Nếu Pass 1 tự quyết định cần kiểm tra thiết bị / không khí phòng -> gọi MCP tools song song cho Pass 2 & Pass 3
    raw_fetch_tools = p1_data.get("fetch_tools")
    if raw_fetch_tools:
        try:
            mcp_facts_block = await mcp_tools.execute_fetch_tools_for_ai(raw_fetch_tools)
        except Exception as mcp_err:
            logger.warning(f"⚠️ [MCP Tools Fetch]: {mcp_err}")
            mcp_facts_block = ""
        if mcp_facts_block:
            logger.info(f"🔌 [MCP Tools Fetch]: {mcp_tools.normalize_ai_fetch_tools(raw_fetch_tools)}")
            now_playing_text = (
                f"{now_playing_text}\n{mcp_facts_block}".strip()
                if now_playing_text
                else mcp_facts_block
            )

    # Nếu Pass 1 xác định cần tra cứu thực tế mới -> gọi Google Search Grounding (tách bước khỏi JSON mode)
    web_facts_block = ""
    if extra_system_note:
        web_facts_block += f"\n{extra_system_note.strip()}\n"
    web_query = p1_data.get("web_search_query")
    if isinstance(web_query, str) and web_query.strip() and web_query.strip().lower() != "null":
        import web_helper
        grounded_facts = await web_helper.search_with_gemini_grounding(web_query.strip(), text_content)
        if grounded_facts:
            p_cfg = persona.get_persona_config()
            web_facts_block = (
                f"\nTHÔNG TIN THỰC TẾ {p_cfg['butler_name'].upper()} VỪA TRA CỨU ĐƯỢC (trả lời chính xác, tự nhiên):\n"
                f"{grounded_facts}\n"
            )

    photo_decision, chosen_photo_key = None, None
    p1_data["photo_decision"] = None
    p1_data["chosen_photo_key"] = None

    # === QUYẾT ĐỊNH GỬI VOICE NOTE: KIỂM TRA TTS HEALTH + COOLDOWN TRƯỚC KHI CHO PHÉP PASS 2 HỨA VOICE ===
    raw_send_voice = p1_data.get("send_voice")
    ai_wants_voice = (
        raw_send_voice is True
        or (isinstance(raw_send_voice, str) and raw_send_voice.strip().lower() == "true")
    )
    if ai_wants_voice and voice_cooldown_active and not explicit_voice_req:
        logger.info("🎙️ [Voice Cooldown]: vừa gửi voice gần đây -> tạm tắt send_voice lượt này để giữ nhịp chữ tự nhiên")
        ai_wants_voice = False
    tts_ready = await tts_client.is_tts_available()
    voice_decision = bool(tts_ready and ai_wants_voice)
    p1_data["voice_decision"] = voice_decision
    voice_holder: Optional[Dict[str, Optional[str]]] = {"voice_text": None} if voice_decision else None
    if voice_decision:
        p_cfg = persona.get_persona_config()
        web_facts_block = (
            web_facts_block
            + f"\nCHẾ ĐỘ TIN NHẮN THOẠI THỐNG NHẤT (1 MẠCH CHỮ DẪN + VOICE):\n"
              f"Lượt này {p_cfg['butler_name']} gửi 1 voice note rõ ràng (~10 đến 18 giây nói) làm câu trả lời CHÍNH cho {p_cfg['owner_name']} — chữ và voice phải là MỘT MẠCH DUY NHẤT.\n"
              f"1) Trong \"voice_text\": truyền tải trọn vẹn câu trả lời hoặc thông báo chính (2-3 câu mạch lạc, ~40-65 chữ tiếng Việt), ngữ điệu phù hợp phong cách ({p_cfg['voice_style']}), ngắt nhịp tự nhiên bằng dấu phẩy (,), ba chấm (...), (?) hoặc (!).\n"
              f"2) Trong mảng \"messages\": CHỈ viết 0 hoặc ĐÚNG 1 câu dẫn thật ngắn (4-14 chữ, hoặc [] nếu muốn gửi thẳng voice) nối trực tiếp vào \"voice_text\".\n"
              f"   - TUYỆT ĐỐI CẤM trả lời đầy đủ một đằng trong \"messages\" rồi nói chuyện khác trong \"voice_text\".\n"
        )
    else:
        unavailable_voice_note = ""
        if not tts_ready and (ai_wants_voice or explicit_voice_req):
            unavailable_voice_note = (
                " ĐẶC BIỆT LƯU Ý: Chức năng gửi tin nhắn thoại lượt này tạm thời không khả dụng — "
                "tuyệt đối KHÔNG hứa hay nói kiểu 'nghe voice nhé', hãy nhắn chữ trực tiếp."
            )
        web_facts_block = (
            web_facts_block
            + "\nCHẾ ĐỘ TIN NHẮN CHỮ THUẦN TÚY: Lượt này giao tiếp hoàn toàn bằng bong bóng chữ trên Telegram, "
              "truyền tải trọn vẹn câu trả lời ngay trong mảng messages."
              + unavailable_voice_note
              + "\n"
        )

    # === BƯỚC 2: PASS 2 + BƯỚC 3: KIỂM TOÁN LOGIC TIỀN GỬI ===
    baseline_facts = vector_memory.get_core_facts_for_prompt(user_id, limit=6)
    pass2_temporal_notes = (
        diary_helper.build_temporal_comparison_hints(
            text_content,
            now_ts=now_ts,
            pending_reminders=[
                r for r in database.get_pending_reminders(user_id, limit=12)
                if exclude_reminder_id is None or int(r["id"]) != int(exclude_reminder_id)
            ],
            open_loops=database.get_open_loops(user_id, now_ts=now_ts),
        )
        if allow_reminder_mutations
        else temporal_notes
    )
    final_messages, logic_audit, logic_cancelled = await _polish_and_audit_response(
        turn_kind=turn_kind or "reply",
        recent=recent,
        text_content=text_content,
        p1_data=p1_data,
        photo_decision=photo_decision,
        default_mood=mood,
        extra_context=web_facts_block,
        out_voice_holder=voice_holder,
        session_state_text=session_state_text,
        baseline_facts=baseline_facts,
        time_context=time_ctx,
        temporal_notes=pass2_temporal_notes,
        now_playing_text=now_playing_text,
        now_ts=now_ts,
    )
    p1_data["logic_audit"] = logic_audit
    p1_data["logic_cancelled"] = bool(logic_cancelled)

    if logic_cancelled:
        p1_data["messages"] = []
        p1_data["voice_decision"] = False
        p1_data["voice_text"] = None
        p1_data["photo_decision"] = None
        p1_data["chosen_photo_key"] = None
        return p1_data

    p1_data["messages"] = final_messages
    if voice_decision:
        vtext = (voice_holder or {}).get("voice_text")
        if not vtext and final_messages:
            vtext = " ".join(final_messages).strip()
        p1_data["voice_text"] = vtext
        logger.info(f"🎙️ [Voice Decision]: enabled | text_bubbles={len(final_messages)} | voice_text={repr(vtext)}")
    else:
        p1_data["voice_text"] = None
        logger.info(f"🎙️ [Voice Decision]: off (p1_send_voice={p1_data.get('send_voice')})")

    if allow_reminder_mutations:
        if defer_commit:
            deferred_payload["final_messages"] = list(final_messages)
            deferred_payload["voice_text"] = p1_data.get("voice_text")
        else:
            try:
                _sync_turn_deferred_promises(
                    user_id=user_id,
                    user_text=text_content,
                    final_messages=final_messages,
                    voice_text=p1_data.get("voice_text"),
                    p1_added=p1_added_reminders,
                    now_ts=now_ts,
                )
            except Exception as sync_err:
                logger.warning(f"⏰ Lỗi hậu kiểm deferred promise: {sync_err}")
    if defer_commit and deferred_payload:
        p1_data["_deferred_commit"] = deferred_payload
    return p1_data


def commit_deferred_side_effects(response_data: Optional[Dict[str, Any]]) -> None:
    """Thực thi các cập nhật DB (session_state, reminders, promises) sau khi gửi Telegram thành công."""
    if not isinstance(response_data, dict):
        return
    payload = response_data.pop("_deferred_commit", None)
    if not isinstance(payload, dict) or not payload:
        return
    uid = int(payload.get("user_id") or 0)
    if uid <= 0:
        return
    now_ts = float(payload.get("now_ts") or time.time())

    if payload.get("allow_reminder_mutations"):
        added: List[Tuple[int, str]] = []
        session_state = database.apply_session_state_update(
            uid, payload.get("sanitized_ss_update"), now_ts=now_ts
        )
        _apply_reminder_actions(
            uid,
            payload.get("reminder_actions"),
            exclude_id=payload.get("exclude_reminder_id"),
            source_user_text=str(payload.get("source_user_text") or ""),
            allow_promise=bool(payload.get("allow_promise")),
            out_added=added,
        )
        database.reconcile_reminders_with_session_state(uid, session_state)
        try:
            _sync_turn_deferred_promises(
                user_id=uid,
                user_text=str(payload.get("source_user_text") or ""),
                final_messages=list(payload.get("final_messages") or []),
                voice_text=payload.get("voice_text"),
                p1_added=added,
                now_ts=now_ts,
            )
        except Exception as sync_err:
            logger.warning(f"⏰ Lỗi hậu kiểm deferred promise: {sync_err}")


def _resolve_photo_desc(chosen_photo_key: Optional[str]) -> str:
    return ""


_AUDIT_SYSTEM_LEAK_RE = re.compile(
    r"\b(?:pass\s*[12]|raw_ideas|inner_monologue|session_state_update|direct_answer_strategy|người\s+dùng)\b",
    re.IGNORECASE,
)


def _pass_audit_result(status: str = "pass", severity: str = "none") -> Dict[str, Any]:
    return {
        "passed": True,
        "block": False,
        "status": status,
        "severity": severity,
        "categories": [],
        "already_fulfilled": False,
        "contradictions": [],
        "fix_hint": "",
    }


def _parse_logic_audit_json(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text or not isinstance(raw_text, str):
        return None
    cleaned = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("passed"), bool):
        return None

    passed = bool(data["passed"])
    raw_sev = str(data.get("severity") or ("none" if passed else "high")).strip().lower()
    if raw_sev in ("critical", "major", "severe", "high"):
        severity = "high"
    elif raw_sev in ("minor", "low"):
        severity = "minor"
    elif raw_sev in ("none", ""):
        severity = "none" if passed else "high"
    else:
        severity = "none" if passed else "high"

    cats = [str(c).strip().upper() for c in (data.get("categories") or []) if str(c).strip()]
    contradictions = [str(c).strip() for c in (data.get("contradictions") or []) if str(c).strip()]
    already_fulfilled = bool(data.get("already_fulfilled"))
    fix_hint = str(data.get("fix_hint") or "").strip()

    if passed:
        block = False
        status = "pass"
    elif severity == "minor" and not already_fulfilled:
        block = False
        status = "minor"
    else:
        block = True
        status = "block"

    return {
        "passed": not block,
        "block": block,
        "status": status,
        "severity": severity,
        "categories": cats,
        "already_fulfilled": already_fulfilled,
        "contradictions": contradictions,
        "fix_hint": fix_hint,
    }


def _detect_deterministic_logic_issues(
    draft_messages: List[str],
    draft_voice_text: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    texts = [m for m in (draft_messages or []) if isinstance(m, str) and m.strip()]
    if draft_voice_text and isinstance(draft_voice_text, str) and draft_voice_text.strip():
        texts.append(draft_voice_text.strip())
    for txt in texts:
        m = _AUDIT_SYSTEM_LEAK_RE.search(txt)
        if m:
            return {
                "passed": False,
                "block": True,
                "status": "block",
                "severity": "high",
                "categories": ["PERSONA_TONE"],
                "already_fulfilled": False,
                "contradictions": [f"Lộ từ khóa nội bộ hệ thống ('{m.group(0)}') trong bản nháp sắp gửi."],
                "fix_hint": "Xóa bỏ mọi từ ngữ meta hệ thống và viết lại hoàn toàn tự nhiên theo đúng giọng trợ lý.",
            }
    return None


def _format_recent_audit_history(
    recent: List[Dict[str, Any]],
    now_ts: float,
    limit: int = 10,
) -> str:
    import diary_helper
    p_cfg = persona.get_persona_config()
    tail = (recent or [])[-limit:]
    if not tail:
        return "(Chưa có lịch sử gần đây)"
    lines = []
    for item in tail:
        role = p_cfg["owner_name"] if item.get("role") == "user" else p_cfg["butler_name"]
        raw_c = str(item.get("content") or "").strip().replace("\n", " ")
        if not raw_c:
            continue
        if len(raw_c) > 260:
            raw_c = raw_c[:257] + "..."
        ts_val = diary_helper.parse_ts_float(item.get("timestamp") or item.get("created_at"))
        ts_label = diary_helper.format_relative_vn_ts(ts_val, now_ts=now_ts) if ts_val else ""
        prefix = f"[{ts_label}] " if ts_label else ""
        lines.append(f"- {prefix}{role}: {raw_c}")
    return "\n".join(lines) if lines else "(Chưa có lịch sử gần đây)"


async def audit_draft_logic(
    *,
    turn_kind: str,
    recent: List[Dict[str, Any]],
    text_content: str,
    draft_messages: List[str],
    draft_voice_text: Optional[str] = None,
    photo_decision: Optional[str] = None,
    chosen_photo_key: Optional[str] = None,
    voice_decision: bool = False,
    session_state_text: str = "",
    baseline_facts: Optional[List[str]] = None,
    time_context: Optional[Dict[str, Any]] = None,
    temporal_notes: str = "",
    now_playing_text: str = "",
    mood: str = "vui vẻ",
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Pass 3: Kiểm toán logic tiền gửi (không tự viết lại câu chữ).
    Fail-open tuyệt đối khi tắt cờ, lỗi mạng, 429 hoặc quá timeout ngắn.
    """
    if not getattr(config, "LOGIC_AUDIT_ENABLED", True):
        return _pass_audit_result("disabled")

    has_draft = bool(
        any(isinstance(m, str) and m.strip() for m in (draft_messages or []))
        or (isinstance(draft_voice_text, str) and draft_voice_text.strip())
    )
    if not has_draft:
        return _pass_audit_result("empty")

    det_issue = _detect_deterministic_logic_issues(draft_messages, draft_voice_text)
    if det_issue is not None:
        logger.warning(
            f"🔍 [Pass 3 Logic Audit | {turn_kind} | BLOCK_DETERMINISTIC]: {det_issue['contradictions']}"
        )
        return det_issue

    import diary_helper
    now_v = float(now_ts if now_ts is not None else (time_context or {}).get("now_ts") or time.time())
    effective_time_ctx = time_context or diary_helper.get_vietnam_time_context(now_ts=now_v)

    photo_desc = _resolve_photo_desc(chosen_photo_key) if photo_decision else ""
    if photo_decision:
        attach_summary = f"CÓ ({photo_decision}" + (f" - {photo_desc}" if photo_desc else "") + ")"
    else:
        attach_summary = "KHÔNG"

    facts_block = "\n".join(f"• {f}" for f in (baseline_facts or [])[:6] if str(f).strip()) or "(Chưa có)"
    history_block = _format_recent_audit_history(recent, now_ts=now_v, limit=10)
    draft_payload: Dict[str, Any] = {"messages": draft_messages or []}
    if voice_decision:
        draft_payload["voice_text"] = draft_voice_text or ""

    p_cfg = persona.get_persona_config()
    owner_name = p_cfg["owner_name"]
    butler_name = p_cfg["butler_name"]
    audit_prompt = f"""LOẠI LƯỢT: {turn_kind}
{diary_helper.format_authoritative_time_banner(effective_time_ctx, now_ts=now_v)}
{temporal_notes.strip() if temporal_notes and temporal_notes.strip() else ""}

TRẠNG THÁI PHIÊN HIỆN TẠI:
{session_state_text.strip() if session_state_text and session_state_text.strip() else "(Không có)"}

ĐÍNH KÈM & NGỮ CẢNH THỰC TẾ:
- Đính kèm ảnh: {attach_summary}
- Đính kèm voice: {"CÓ" if voice_decision else "KHÔNG"}
- Trạng thái hiện tại của {butler_name}: {mood}
- Loa & Thiết bị của {owner_name}: {now_playing_text.strip() if now_playing_text and now_playing_text.strip() else "Không phát nhạc / Không rõ"}

HỒ SƠ NỀN {owner_name.upper()}:
{facts_block}

LỊCH SỬ HỘI THOẠI GẦN NHẤT (tối đa 10 tin):
{history_block}

TIN NHẮN / TÍN HIỆU KÍCH HOẠT LƯỢT NÀY:
"{text_content}"

BẢN NHÁP {butler_name.upper()} SẮP GỬI (CHỈ ĐÁNH GIÁ LOGIC, KHÔNG TỰ SỬA VĂN PHONG):
{json.dumps(draft_payload, ensure_ascii=False)}"""

    budget_sec = float(getattr(config, "LOGIC_AUDIT_TIMEOUT_SEC", 6.0) or 6.0)
    audit_model = getattr(config, "LOGIC_AUDIT_MODEL", MEMORY_MODEL) or MEMORY_MODEL
    started = time.monotonic()
    try:
        raw = await call_gemini_api(
            model=audit_model,
            system_instruction=character_bible.build_pass3_logic_audit_instruction(),
            contents=[{"role": "user", "parts": [{"text": audit_prompt}]}],
            response_json=True,
            key_role="memory",
            fallback_models=[MEMORY_MODEL, CHAT_MODEL, "gemini-flash-lite-latest"],
            timeout_sec=min(4.5, budget_sec),
            total_deadline_sec=budget_sec,
            allow_cooldown_wait=False,
            temperature=0.1,
            max_output_tokens=512,
        )
    except (Exception, StopIteration) as e:
        elapsed = time.monotonic() - started
        logger.warning(
            f"⏭️ [Pass 3 Logic Audit | {turn_kind} | FAIL_OPEN ({elapsed:.2f}s)]: "
            f"{type(e).__name__}: {e}"
        )
        return _pass_audit_result("fail_open")

    elapsed = time.monotonic() - started
    parsed = _parse_logic_audit_json(raw)
    if parsed is None:
        logger.warning(
            f"⏭️ [Pass 3 Logic Audit | {turn_kind} | FAIL_OPEN_PARSE ({elapsed:.2f}s)]"
        )
        return _pass_audit_result("fail_open_parse")

    if parsed["block"]:
        logger.warning(
            f"🔍 [Pass 3 Logic Audit | {turn_kind} | HIGH_CONFLICT ({elapsed:.2f}s)]: "
            f"cats={parsed['categories']} | fulfilled={parsed['already_fulfilled']} | "
            f"issues={parsed['contradictions']}"
        )
    elif parsed["status"] == "minor":
        logger.info(
            f"ℹ️ [Pass 3 Logic Audit | {turn_kind} | MINOR ({elapsed:.2f}s)]: "
            f"issues={parsed['contradictions']} -> giữ nguyên bản nháp"
        )
    else:
        logger.info(f"✅ [Pass 3 Logic Audit | {turn_kind} | PASS ({elapsed:.2f}s)]")
    return parsed


async def _polish_and_audit_response(
    *,
    turn_kind: str,
    recent: List[Dict[str, Any]],
    text_content: str,
    p1_data: Dict[str, Any],
    photo_decision: Optional[str],
    default_mood: str,
    extra_context: str = "",
    out_voice_holder: Optional[Dict[str, Optional[str]]] = None,
    session_state_text: str = "",
    baseline_facts: Optional[List[str]] = None,
    time_context: Optional[Dict[str, Any]] = None,
    temporal_notes: str = "",
    now_playing_text: str = "",
    now_ts: Optional[float] = None,
    recent_bot_texts_for_proactive: Optional[List[str]] = None,
) -> Tuple[List[str], Dict[str, Any], bool]:
    """
    Chạy Pass 2 chuốt lời thoại rồi đưa qua Pass 3 Kiểm toán Logic (Hybrid Gate):
    - Nếu ổn / minor / fail-open: giữ nguyên bản Pass 2.
    - Nếu lỗi nặng ở `proactive` hoặc `ai_followup`, hoặc `ai_promise` đã được thực hiện xong: hủy gửi.
    - Nếu lỗi nặng ở `reply` / `user_reminder` / `ai_promise` (sai câu chữ): trả chỉ dẫn cho Pass 2 viết lại tối đa 1 lần.
    """
    draft_messages = await _run_pass2_polisher(
        recent=recent,
        text_content=text_content,
        p1_data=p1_data,
        photo_decision=photo_decision,
        default_mood=default_mood,
        extra_context=extra_context,
        out_voice_holder=out_voice_holder,
        session_state_text=session_state_text,
        baseline_facts=baseline_facts,
        time_context=time_context,
        temporal_notes=temporal_notes,
        now_playing_text=now_playing_text,
        now_ts=now_ts,
    )
    draft_vt = (out_voice_holder or {}).get("voice_text") if out_voice_holder is not None else None

    if turn_kind == "proactive":
        fallback_marker = "kết nối ai vừa tạm gián đoạn"
        if (
            not draft_messages
            or any(fallback_marker in m.lower() for m in draft_messages)
            or _find_repetitive_previous_msg(draft_messages, recent_bot_texts_for_proactive or [])
        ):
            logger.warning("🛑 [Autonomous Proactive]: nội dung chủ động bị lặp hoặc fallback -> tự động hoãn để tránh spam")
            return [], _pass_audit_result("skipped_repeat"), True

    audit = await audit_draft_logic(
        turn_kind=turn_kind,
        recent=recent,
        text_content=text_content,
        draft_messages=draft_messages,
        draft_voice_text=draft_vt,
        photo_decision=photo_decision,
        chosen_photo_key=p1_data.get("chosen_photo_key"),
        voice_decision=(out_voice_holder is not None),
        session_state_text=session_state_text,
        baseline_facts=baseline_facts,
        time_context=time_context,
        temporal_notes=temporal_notes,
        now_playing_text=now_playing_text,
        mood=p1_data.get("new_mood", default_mood),
        now_ts=now_ts,
    )

    if not audit.get("block"):
        return draft_messages, audit, False

    if turn_kind in ("proactive", "ai_followup"):
        logger.warning(
            f"🛑 [Pass 3 Logic Audit | {turn_kind} | CANCEL]: "
            f"cats={audit['categories']} | issues={audit['contradictions']}"
        )
        return [], {**audit, "action": "cancelled"}, True

    if turn_kind == "ai_promise" and audit.get("already_fulfilled"):
        logger.warning(
            f"🛑 [Pass 3 Logic Audit | ai_promise | CANCEL_FULFILLED]: "
            f"issues={audit['contradictions']}"
        )
        return [], {**audit, "action": "cancelled_fulfilled"}, True

    p_cfg = persona.get_persona_config()
    issues_txt = "; ".join(audit.get("contradictions") or []) or "Phát hiện mâu thuẫn logic nghiêm trọng."
    hint_txt = audit.get("fix_hint") or "Sửa lại các chi tiết mâu thuẫn cho khớp hoàn toàn với đồng hồ, trạng thái thiết bị và lịch sử."
    critique = (
        f"- Các lỗi logic bắt buộc phải sửa:\n  {issues_txt}\n"
        f"- Chỉ dẫn sửa: {hint_txt}\n"
        f"- Bản nháp cũ bị từ chối: {json.dumps({'messages': draft_messages, 'voice_text': draft_vt} if draft_vt else {'messages': draft_messages}, ensure_ascii=False)}\n"
        f"Hãy viết lại lời thoại mới giữ đúng phong cách tự nhiên của {p_cfg['butler_name']} ({p_cfg['tone_desc']}) nhưng KHẮC PHỤC TRIỆT ĐỂ các lỗi trên."
    )
    retry_voice_holder = {"voice_text": None} if out_voice_holder is not None else None
    rewritten_messages = await _run_pass2_polisher(
        recent=recent,
        text_content=text_content,
        p1_data=p1_data,
        photo_decision=photo_decision,
        default_mood=default_mood,
        extra_context=extra_context,
        out_voice_holder=retry_voice_holder,
        session_state_text=session_state_text,
        baseline_facts=baseline_facts,
        time_context=time_context,
        temporal_notes=temporal_notes,
        now_playing_text=now_playing_text,
        now_ts=now_ts,
        audit_critique=critique,
    )
    rewritten_vt = (retry_voice_holder or {}).get("voice_text") if retry_voice_holder is not None else None
    fallback_marker = "kết nối ai vừa tạm gián đoạn"
    retry_failed = (
        not rewritten_messages and not rewritten_vt
    ) or (
        draft_messages
        and rewritten_messages
        and any(fallback_marker in m.lower() for m in rewritten_messages)
        and not any(fallback_marker in m.lower() for m in draft_messages)
    )
    if retry_failed:
        if _detect_deterministic_logic_issues(draft_messages, draft_vt):
            logger.warning(
                f"🛡️ [Pass 3 Logic Audit | {turn_kind} | BLOCKED_LEAK_FALLBACK]: bản nháp chứa leak hệ thống và rewrite lỗi -> thay bằng câu an toàn"
            )
            if out_voice_holder is not None:
                out_voice_holder["voice_text"] = None
            return ["Kết nối AI vừa tạm gián đoạn một chút, bạn nhắn lại giúp mình nhé!"], {
                **audit,
                "action": "blocked_leak_fallback",
            }, False
        logger.warning(
            f"⏭️ [Pass 3 Logic Audit | {turn_kind} | REWRITE_FAIL_OPEN]: Pass 2 sửa lại lỗi -> giữ bản nháp ban đầu"
        )
        return draft_messages, {**audit, "action": "rewrite_fail_open"}, False

    if out_voice_holder is not None and retry_voice_holder is not None:
        if rewritten_vt:
            out_voice_holder["voice_text"] = rewritten_vt
        elif rewritten_messages:
            # Không giữ draft_vt cũ đã bị Pass 3 gắn cờ mâu thuẫn khi text mới đã được sửa
            out_voice_holder["voice_text"] = " ".join(rewritten_messages).strip()

    logger.info(
        f"✏️ [Pass 3 Logic Audit | {turn_kind} | REWRITTEN]: "
        f"cats={audit['categories']} | old={draft_messages} -> new={rewritten_messages}"
    )
    return rewritten_messages, {**audit, "action": "rewritten"}, False


async def _run_pass2_polisher(
    recent: List[Dict[str, Any]],
    text_content: str,
    p1_data: Dict[str, Any],
    photo_decision: Optional[str],
    default_mood: str,
    extra_context: str = "",
    out_voice_holder: Optional[Dict[str, Optional[str]]] = None,
    session_state_text: str = "",
    baseline_facts: Optional[List[str]] = None,
    time_context: Optional[Dict[str, Any]] = None,
    temporal_notes: str = "",
    now_playing_text: str = "",
    now_ts: Optional[float] = None,
    audit_critique: str = "",
) -> List[str]:
    """Chuốt ý tưởng Pass 1 / Proactive Eval thành 1-3 tin nhắn ngắn theo persona hiện hành."""
    import diary_helper
    p_cfg = persona.get_persona_config()
    butler_name = p_cfg["butler_name"]
    owner_name = p_cfg["owner_name"]
    now_v = float(now_ts if now_ts is not None else (time_context or {}).get("now_ts") or time.time())
    effective_time_ctx = time_context or diary_helper.get_vietnam_time_context(now_ts=now_v)
    pass2_system = character_bible.build_pass2_system_instruction(time_context=effective_time_ctx)
    pass2_contents = _build_history_contents(recent[-16:], now_ts=now_v)

    user_intent = p1_data.get("user_intent", "")
    direct_ans = p1_data.get("direct_answer_strategy", "")
    inner_monologue = p1_data.get("inner_monologue") or p1_data.get("proactive_reason", "")
    emotional_strategy = p1_data.get("emotional_strategy", "")
    raw_ideas = p1_data.get("raw_ideas", [])
    fact_check_note = p1_data.get("fact_check", "")

    attach_note = (
        "TRẠNG THÁI ĐÍNH KÈM: Lượt này chỉ trả lời bằng văn bản/thoại (không đính kèm ảnh). "
        "Tập trung giải quyết đúng yêu cầu hoặc giao tiếp rõ ràng, tự nhiên."
    )

    if out_voice_holder is not None:
        output_schema_hint = (
            'JSON trả về gồm cả hai khóa: {"messages": ["0 hoặc 1 câu dẫn thật ngắn (<= 15 chữ) nối thẳng vào voice note, hoặc để [] nếu vào thẳng voice"], '
            f'"voice_text": "1 đoạn thoại tự nhiên (~40-65 chữ, 2-3 câu mạch lạc) phù hợp phong cách {butler_name} ({p_cfg["voice_style"]}): '
            'đồng nhất cách xưng hô với câu chữ dẫn, ngắt nhịp rõ ràng bằng phẩy (,), ba chấm (...), (?) hoặc (!), tránh emoji và ngoặc kép"}'
        )
        turn_direction_hint = (
            "ĐỊNH HƯỚNG VOICE TURN: Phản hồi PHẢI là MỘT lượt nói thống nhất về đúng 1 chủ đề: "
            "câu dẫn ở \"messages\" (tối đa 1 câu ngắn <= 15 chữ hoặc rỗng []) chỉ mở đường vào đúng nội dung trong \"voice_text\"; "
            "CẤM trả lời riêng 1 chuyện trong chữ rồi nói chuyện khác trong voice."
        )
    else:
        output_schema_hint = 'JSON trả về: {"messages": ["bong bóng chữ 1", "bong bóng chữ 2 (nếu cần)"]}'
        turn_direction_hint = (
            f"ĐỊNH HƯỚNG: Chuốt lại thành 1 đến 2 tin nhắn gọn gàng, chính xác và tự nhiên đúng chất {butler_name} "
            f"(xưng '{p_cfg['self_ref']}', gọi {owner_name}, giọng điệu: {p_cfg['tone_desc']})."
        )

    recent_bot_texts = _extract_recent_bot_texts(recent, limit=4)

    context_sections: List[str] = [
        diary_helper.format_authoritative_time_banner(effective_time_ctx, now_ts=now_v),
    ]
    if temporal_notes and temporal_notes.strip():
        context_sections.append(temporal_notes.strip())
    if now_playing_text and now_playing_text.strip():
        context_sections.append(
            "=== NGỮ CẢNH THIẾT BỊ, LOA & PHÒNG LÚC NÀY (CHỈ BÁO ĐÚNG SỰ THẬT; ƯU TIÊN TUYỆT ĐỐI HƠN raw_ideas NẾU CHÊNH LỆCH) ===\n"
            + now_playing_text.strip()
        )
    if session_state_text and session_state_text.strip():
        context_sections.append(
            session_state_text.strip()
            + "\n(NGUYÊN TẮC BẮT BUỘC PASS 2: việc [ĐÃ XONG] không hỏi lại như sắp làm; "
            "việc chỉ là [THÔNG BÁO / DỰ ĐỊNH] không tự bịa thành 'đã hứa'.)"
        )
    if baseline_facts:
        facts_lines = "\n".join(f"• {f}" for f in baseline_facts[:6] if str(f).strip())
        if facts_lines:
            context_sections.append(f"=== HỒ SƠ NỀN {owner_name.upper()} (PHẢI NHỚ ĐÚNG) ===\n" + facts_lines)

    grounding_block = ("\n\n".join(context_sections) + "\n\n") if context_sections else ""
    if text_content and not text_content.lstrip().startswith("[Chủ động"):
        display_user_msg = _format_current_user_turn_text(text_content, recent=None, now_ts=now_v)
    else:
        display_user_msg = text_content

    audit_block = (
        f"\n=== SỬA MÂU THUẪN LOGIC BẮT BUỘC (TỪ BỘ KIỂM TOÁN TIỀN GỬI) ===\n{audit_critique.strip()}\n"
        if audit_critique and audit_critique.strip()
        else ""
    )
    pass2_user_prompt = f"""{grounding_block}
BỐI CẢNH / CÂU {owner_name.upper()} VỪA NHẮN:
"{display_user_msg}"

{attach_note}
{extra_context}
ĐỊNH HƯỚNG TRẢ LỜI:
- Mục đích của {owner_name}: {user_intent}
- Kiểm chứng dữ kiện: {fact_check_note}
- Định hướng trả lời trực tiếp: {direct_ans}
- Ghi chú nội tâm: {inner_monologue}
- Chiến thuật giao tiếp: {emotional_strategy}
- Trạng thái: {p1_data.get('new_mood', default_mood)}
- Các ý muốn nói: {json.dumps(raw_ideas, ensure_ascii=False) if raw_ideas else json.dumps(p1_data.get('messages', []), ensure_ascii=False)}
{audit_block}
ĐỊNH DẠNG ĐẦU RA:
{output_schema_hint}
{turn_direction_hint}
"""
    pass2_contents.append({"role": "user", "parts": [{"text": pass2_user_prompt}]})

    final_messages: List[str] = []
    anti_repeat_note = ""
    for attempt in range(2):
        attempt_contents = list(pass2_contents[:-1])
        retry_user_prompt = (
            pass2_user_prompt
            if not anti_repeat_note
            else f"{pass2_user_prompt}\n{anti_repeat_note}"
        )
        attempt_contents.append({"role": "user", "parts": [{"text": retry_user_prompt}]})
        attempt_msgs: List[str] = []
        attempt_vt: Optional[str] = None
        try:
            raw_p2 = await call_gemini_api(
                model=CHAT_MODEL,
                system_instruction=pass2_system,
                contents=attempt_contents,
                response_json=True,
            )
            p2_data = parse_ai_json(raw_p2, default_mood=p1_data.get("new_mood", default_mood))
            attempt_msgs = p2_data.get("messages", [])
            if out_voice_holder is not None:
                raw_vt = p2_data.get("voice_text")
                if isinstance(raw_vt, str) and raw_vt.strip() and raw_vt.strip().lower() != "null":
                    clean_vt = _sanitize_voice_text(raw_vt)
                    if clean_vt:
                        attempt_vt = clean_vt
                        out_voice_holder["voice_text"] = clean_vt
        except Exception as e:
            logger.warning(f"Pass 2 polishing error (attempt {attempt + 1}): {type(e).__name__}: {e}, falling back cleanly")
            break

        has_voice_payload = bool(out_voice_holder is not None and attempt_vt)
        if not attempt_msgs and not has_voice_payload:
            break
        final_messages = attempt_msgs
        dup_prev = _find_repetitive_previous_msg(
            attempt_msgs, recent_bot_texts, candidate_voice=attempt_vt
        )
        if not dup_prev or attempt == 1:
            break

        logger.warning("⚠️ [Pass 2 Anti-Repeat]: Bản nháp giống tin bot vừa gửi gần đây -> viết lại câu mới hoàn toàn")
        anti_repeat_note = (
            f"\n⚠️ CHỐNG LẶP BẮT BUỘC: Bản nháp vừa rồi quá giống câu hoặc cách mở đầu đã nói gần đây "
            f"('{dup_prev[:140]}...'). Hãy viết lại lời thoại MỚI HOÀN TOÀN, đổi cách mở đầu và bỏ ý cũ đã xong!"
        )

    has_final_voice = bool(out_voice_holder is not None and out_voice_holder.get("voice_text"))
    if not final_messages and not has_final_voice:
        p1_msgs = [
            _sanitize_visible_bubble_text(m) for m in (p1_data.get("messages") or [])
            if isinstance(m, str) and m.strip() and not _is_internal_json_line(m)
        ]
        p1_msgs = [m for m in p1_msgs if m and not _is_internal_json_line(m)]
        safe_ideas = [
            _sanitize_visible_bubble_text(m) for m in (raw_ideas or [])
            if isinstance(m, str)
            and m.strip()
            and not _is_internal_json_line(m)
            and not _META_RAW_IDEA_RE.match(m.strip())
            and "người dùng" not in m.lower()
            and "pass 1" not in m.lower()
        ]
        safe_ideas = [m for m in safe_ideas if m and not _is_internal_json_line(m)]
        final_messages = p1_msgs or safe_ideas or ["Kết nối AI vừa tạm gián đoạn một chút, bạn nhắn lại giúp mình nhé!"]
    return [m for m in (_sanitize_visible_bubble_text(x) for x in final_messages) if m and not _is_internal_json_line(m)]


async def evaluate_proactive_turn(user_id: int, wake_ritual_hint: str = "") -> Dict[str, Any]:
    """
    Đánh thức Smart Butler tự đánh giá xem lúc này nên:
    - 'send': Nhắn chủ động (hỏi thăm / nhắc việc / báo bối cảnh hữu ích)
    - 'defer': Hoãn kiểm tra lại sau recheck_after_mins phút
    - 'wait_for_user': Giữ im lặng chờ chủ nhà nhắn trước
    """
    import diary_helper
    import trend_helper
    import circadian

    p_cfg = persona.get_persona_config()
    user_state = database.get_or_create_user(user_id)
    affinity = int(user_state["affinity_score"])
    mood = user_state["mood"]
    last_user_time = float(user_state.get("last_user_message_time") or 0.0)
    last_bot_time = float(user_state.get("last_bot_message_time") or 0.0)
    unanswered_cnt = int(user_state.get("unanswered_proactive_count") or 0)

    if unanswered_cnt >= 2:
        logger.info(f"🛑 [Autonomous Proactive]: unanswered={unanswered_cnt} >= 2 -> ép 'wait_for_user' để không spam")
        return {
            "decision": "wait_for_user",
            "recheck_after_mins": 360,
            "proactive_reason": f"Đã nhắn từ 2 tin chủ động liên tiếp mà {p_cfg['owner_name']} chưa phản hồi, tạm giữ im lặng tránh làm phiền.",
            "new_mood": mood,
            "photo_decision": None,
            "chosen_photo_key": None,
            "messages": [],
        }

    now_ts = time.time()
    mins_since_user = max(0.0, (now_ts - last_user_time) / 60.0) if last_user_time > 0 else 999.0
    mins_since_bot = max(0.0, (now_ts - last_bot_time) / 60.0) if last_bot_time > 0 else 999.0

    has_due_loop = bool(
        database.get_eligible_open_loops(
            user_id, limit=1, cooldown_sec=3600.0, now_ts=now_ts, respect_target_time=True
        )
    )
    min_send_gap = 120 if unanswered_cnt >= 1 else (30 if has_due_loop else 55)
    if mins_since_bot < min_send_gap:
        recheck_early = max(min_send_gap, int(min_send_gap + 15 - mins_since_bot))
        logger.info(
            f"⏸️ [Autonomous Proactive]: vừa nhắn cách đây {mins_since_bot:.1f}p (<{min_send_gap}p) -> defer trước khi gọi LLM"
        )
        return {
            "decision": "defer",
            "recheck_after_mins": recheck_early,
            "proactive_reason": f"Chưa đủ khoảng nghỉ tối thiểu kể từ tin nhắn gần nhất của {p_cfg['butler_name']}.",
            "new_mood": mood,
            "photo_decision": None,
            "chosen_photo_key": None,
            "messages": [],
        }

    today_str = trend_helper.get_today_date_str()
    daily_trends = database.get_today_trends(today_str) or ""
    daily_diary = await diary_helper.get_or_create_today_diary()
    time_ctx = diary_helper.get_vietnam_time_context(last_user_time, now_ts=now_ts)
    current_act = diary_helper.extract_current_session_activity(daily_diary, time_ctx["session_key"])
    mood_hint = diary_helper.build_situational_mood_context(
        current_mood=mood,
        mood_updated_at=float(user_state.get("mood_updated_at") or 0.0),
        last_user_time=last_user_time,
        today_trends=daily_trends,
        now_ts=now_ts,
    )
    personality_traits = {
        "tease_level": user_state.get("tease_level", database.PERSONALITY_DEFAULTS["tease_level"]),
        "stubborn_level": user_state.get("stubborn_level", database.PERSONALITY_DEFAULTS["stubborn_level"]),
        "clingy_level": user_state.get("clingy_level", database.PERSONALITY_DEFAULTS["clingy_level"]),
    }

    core_fact_rows = database.get_memories_by_categories(
        user_id,
        categories=["user_fact", "general", "preference", "important_note", "shared_moment", "inside_joke"],
        limit=8,
    )
    other_fact_rows = database.get_memories_by_categories(
        user_id,
        exclude_categories=["episode", "user_fact", "general", "preference", "important_note", "shared_moment", "inside_joke"],
        limit=4,
    )
    fact_rows = core_fact_rows + other_fact_rows
    recent_ep_rows = database.get_memories_by_categories(user_id, categories=["episode"], limit=5)
    proactive_facts = [r["fact"] for r in fact_rows + recent_ep_rows if r.get("fact")]

    session_state = database.get_session_state(user_id)
    session_state_text = database.format_session_state_for_prompt(session_state, now_ts=now_ts)

    recent = database.get_recent_history(user_id, limit=20)
    recent_bot_texts = _extract_recent_bot_texts(recent, limit=3)
    circadian_text = circadian.format_circadian_for_prompt()
    open_loops_text = database.format_open_loops_for_prompt(
        user_id, limit=2, cooldown_sec=7200.0, mark_surfaced=False, now_ts=now_ts
    )
    now_playing_text = database.format_now_playing_for_prompt()
    proactive_room_ctx = database.format_compact_proactive_room_context(now_ts=now_ts)
    if proactive_room_ctx:
        now_playing_text = (
            f"{now_playing_text}\n{proactive_room_ctx}".strip()
            if now_playing_text
            else proactive_room_ctx
        )
    temporal_notes = diary_helper.build_temporal_comparison_hints(
        "",
        now_ts=now_ts,
        pending_reminders=database.get_pending_reminders(user_id, limit=8),
        open_loops=database.get_open_loops(user_id, now_ts=now_ts),
    )

    eval_sys = character_bible.build_proactive_eval_instruction(
        affinity_score=affinity,
        mood=mood,
        personality_traits=personality_traits,
        time_context=time_ctx,
        current_activity=current_act,
        daily_trends=daily_trends,
        situational_mood_hint=mood_hint,
        unanswered_proactive_count=unanswered_cnt,
        mins_since_user=mins_since_user,
        mins_since_bot=mins_since_bot,
        known_facts=proactive_facts,
        recent_bot_texts=recent_bot_texts,
        photo_cooldown_hint="",
        session_state_text=session_state_text,
        circadian_text=circadian_text,
        open_loops_text=open_loops_text,
        now_playing_text=now_playing_text,
        wake_ritual_hint=wake_ritual_hint,
        temporal_notes=temporal_notes,
    )

    contents = _build_history_contents(recent, now_ts=now_ts)
    cur_clock = diary_helper.format_relative_vn_ts(now_ts, now_ts=now_ts) or (time_ctx or {}).get("time_str", "")
    contents.append({
        "role": "user",
        "parts": [{
            "text": f"[HỆ THỐNG (Lúc {cur_clock}): Đánh giá bối cảnh hiện tại và quyết định: 'send', 'defer', hoặc 'wait_for_user' dưới dạng JSON. Nếu chọn 'send', hãy chọn thông điệp MỚI KHÁC với các tin đã gửi gần đây!]"
        }],
    })

    raw = await call_gemini_api(
        model=CHAT_MODEL,
        system_instruction=eval_sys,
        contents=contents,
        response_json=True,
    )
    eval_data = parse_ai_json(raw, default_mood=mood)
    decision = str(eval_data.get("decision") or "defer").strip().lower()
    if decision not in ("send", "defer", "wait_for_user"):
        decision = "defer"

    try:
        recheck_mins = int(eval_data.get("recheck_after_mins") or (150 if decision == "send" else 90))
    except (TypeError, ValueError):
        recheck_mins = 150

    if decision == "wait_for_user":
        recheck_mins = max(240, min(720, recheck_mins))
    elif decision == "send":
        next_unanswered = unanswered_cnt + 1
        min_post_send = 240 if next_unanswered >= 2 else 120
        recheck_mins = max(min_post_send, min(480, recheck_mins))
    else:
        recheck_mins = max(45, min(360, recheck_mins))

    eval_data["decision"] = decision
    eval_data["recheck_after_mins"] = recheck_mins

    if decision == "send":
        eval_data["photo_decision"] = None
        eval_data["chosen_photo_key"] = None
        import vector_memory
        baseline_facts = vector_memory.get_core_facts_for_prompt(user_id, limit=6)
        msgs, logic_audit, logic_cancelled = await _polish_and_audit_response(
            turn_kind="proactive",
            recent=recent,
            text_content=f"[Chủ động mở lời mới (KHÔNG lặp tin cũ): {eval_data.get('proactive_reason', '')}]",
            p1_data=eval_data,
            photo_decision=None,
            default_mood=mood,
            extra_context=(
                "\nYÊU CẦU ĐỔI MỚI CHỦ ĐỀ: Đây là lượt chủ động sau khoảng nghỉ; "
                "viết lời mở đầu và thông điệp KHÁC hoàn toàn các tin đã gửi trước đó.\n"
            ),
            session_state_text=session_state_text,
            baseline_facts=baseline_facts,
            time_context=time_ctx,
            temporal_notes=temporal_notes,
            now_playing_text=now_playing_text,
            now_ts=now_ts,
            recent_bot_texts_for_proactive=recent_bot_texts,
        )
        eval_data["logic_audit"] = logic_audit
        if logic_cancelled or not msgs:
            eval_data["decision"] = "defer"
            eval_data["recheck_after_mins"] = max(120, recheck_mins)
            eval_data["photo_decision"] = None
            eval_data["chosen_photo_key"] = None
            eval_data["messages"] = []
        else:
            eval_data["messages"] = msgs
    else:
        eval_data["photo_decision"] = None
        eval_data["chosen_photo_key"] = None
        eval_data["messages"] = []

    return eval_data


async def generate_coach_review(user_id: int) -> str:
    """
    Lệnh /review: Tóm tắt nhanh trạng thái Smart Butler, thiết bị, thói quen đã học & lịch hẹn.
    """
    import habit_engine

    p_cfg = persona.get_persona_config()
    butler_name = p_cfg["butler_name"]
    owner_name = p_cfg["owner_name"]
    user_state = database.get_or_create_user(user_id)
    mood = user_state["mood"]
    history = database.get_recent_history(user_id, limit=12)

    if not history:
        return f"Chưa có đủ dữ liệu trò chuyện để tổng kết. Hãy nhắn cho {butler_name} vài câu trước nhé!"

    history_str = ""
    for h in history:
        speaker = owner_name if h["role"] == "user" else butler_name
        history_str += f"- {speaker}: {h['content']}\n"

    pending = database.get_pending_reminders(user_id, limit=6)
    rem_lines = "\n".join(f"- {r.get('title')} (status={r.get('status')})" for r in pending) or "(Không có lịch nhắc chờ)"
    habits_text = habit_engine.format_habits_for_ai(limit=8)

    review_prompt = f"""
Bạn là module Tổng quan Trợ lý Thông minh ({butler_name}) hỗ trợ {owner_name}.
Hãy đọc đoạn trao đổi gần đây, lịch nhắc và danh sách thói quen thiết bị để viết một báo cáo tổng kết ngắn gọn, hữu ích bằng tiếng Việt (Markdown):

=== HỘI THOẠI GẦN ĐÂY ===
{history_str}

=== LỊCH NHẮC ĐANG MỞ ===
{rem_lines}

=== THÓI QUEN THIẾT BỊ TỰ HỌC ===
{habits_text}

=== TRẠNG THÁI HIỆN TẠI ===
- Persona: {butler_name} ({p_cfg['style_label']}) | Thành phố: {p_cfg['weather_city']}
- Trạng thái: {mood}

YÊU CẦU ĐẦU RA (3 mục ngắn gọn, thực tế):
### 📋 1. Tóm tắt ngữ cảnh & yêu cầu gần đây
### 🏠 2. Thói quen & Lịch hẹn đang hoạt động
### 💡 3. Gợi ý tối ưu hóa cho {owner_name} (tối đa 3 ý)
"""

    contents = [{"role": "user", "parts": [{"text": review_prompt}]}]
    fallbacks = [m for m in (CHAT_MODEL, MEMORY_MODEL, "gemini-flash-lite-latest") if m != REVIEW_MODEL]
    return await call_gemini_api(
        model=REVIEW_MODEL,
        system_instruction="Bạn là trợ lý tổng kết vận hành Smart Butler gọn gàng, chính xác.",
        contents=contents,
        response_json=False,
        key_role="chat",
        fallback_models=fallbacks,
    )


# Alias rõ nghĩa cho Smart Butler (giữ tên cũ để tương thích ngược module gọi)
chat_with_butler = chat_with_linh_dan


def get_api_quota_status() -> Dict[str, Any]:
    """Trả về trạng thái pool API key (đã ẩn key) và thời gian cooldown 429 cho Dashboard."""
    now_mono = time.monotonic()
    keys = get_api_keys("chat")
    cooldowns: List[Dict[str, Any]] = []
    for (model, key_tail, scope), until in list(_KEY_COOLDOWN.items()):
        rem = max(0.0, until - now_mono)
        if rem > 0.5:
            cooldowns.append({
                "model": model,
                "key_tail": f"...{key_tail}",
                "scope": scope,
                "remaining_sec": round(rem, 1),
            })
        else:
            _KEY_COOLDOWN.pop((model, key_tail, scope), None)

    return {
        "chat_model": CHAT_MODEL,
        "memory_model": MEMORY_MODEL,
        "review_model": REVIEW_MODEL,
        "key_count": len(keys),
        "keys_masked": [f"...{k[-6:]}" for k in keys],
        "active_cooldowns": cooldowns,
    }
