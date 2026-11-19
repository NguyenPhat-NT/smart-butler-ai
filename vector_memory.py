# -*- coding: utf-8 -*-
import re
import json
import math
import time
import struct
import asyncio
import calendar
import datetime
import logging
import aiohttp
from typing import List, Optional, Dict, Any, Tuple
import config
import database
import persona

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 512
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

FORBIDDEN_FACT_KEYWORDS: List[str] = []

TRIVIAL_EXACT_SET = {
    "ok", "oke", "okie", "okii", "oki", "ừ", "ừm", "um", "uh", "uhm",
    "à", "ờ", "kkk", "kkkk", "haha", "hahaha", "hihi", "hehe", "lol",
    "ủa", "hả", "vâng", "dạ", "nha", "nhé", "zạ", "dạ vâng", "ok bạn",
    "cảm ơn", "thanks", "thank you", "đúng rồi", "chuẩn rồi"
}

# Chi phí chấm điểm cosine là O(số_ký_ức × số_query × 512) bằng Python thuần.
# 91 ký ức ~ 0,23s; 3.000 ký ức ~ 7,8s (đo trên Snapdragon 410) nên phải cắt bớt episode cũ.
RETRIEVAL_MAX_EPISODES = 300
EPISODE_PRUNE_AFTER_DAYS = 21
EPISODE_KEEP_MAX = 400
CORE_FACT_CATEGORIES = (
    "general",
    "user_fact",
    "preference",
    "important_note",
    "shared_moment",
    "inside_joke",
    "promise",
)
VN_TZ = datetime.timezone(datetime.timedelta(hours=getattr(config, "TIMEZONE_OFFSET_HOURS", 7)))


def pack_vector(vec: List[float]) -> bytes:
    """Đóng gói mảng số thực thành nhị phân BLOB để lưu vào SQLite (~2KB/vector)."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> List[float]:
    """Giải nén BLOB nhị phân từ SQLite thành mảng số thực float."""
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Tính độ tương đồng Cosine cực nhanh bằng Python thuần (0MB RAM phụ)."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    return dot / (norm1 * norm2) if (norm1 and norm2) else 0.0


def format_vn_timestamp(ts: Optional[float] = None) -> str:
    """Định dạng timestamp sang giờ Việt Nam (UTC+7): dd/mm HH:MM."""
    base_ts = ts if ts and ts > 0 else time.time()
    vn_dt = datetime.datetime.fromtimestamp(base_ts, tz=VN_TZ)
    return vn_dt.strftime("%d/%m %H:%M")


def is_trivial_message(text: str) -> bool:
    """True nếu tin nhắn chỉ là câu xã giao quá ngắn không cần gọi model lục trí nhớ."""
    if not text:
        return True
    cleaned = re.sub(r"[^\w\s]", "", text.strip().lower()).strip()
    if len(cleaned) < 4:
        return True
    return cleaned in TRIVIAL_EXACT_SET


async def get_text_embedding(text: str, key_role: str = "memory") -> Optional[List[float]]:
    """
    Gọi Gemini Embedding API v1beta dùng chung ClientSession và tự xoay vòng đa API Key khi 429/5xx.
    """
    if not text or not text.strip():
        return None

    import gemini_client
    api_keys = config.get_api_keys(role=key_role)
    if not api_keys and config.GEMINI_API_KEY:
        api_keys = [config.GEMINI_API_KEY]
    if not api_keys:
        logger.warning("Chưa cấu hình Gemini API Key cho Embedding.")
        return None

    payload = {
        "model": f"models/{EMBEDDING_MODEL}",
        "content": {"parts": [{"text": text.strip()}]},
        "outputDimensionality": EMBEDDING_DIM,
    }
    headers = {"Content-Type": "application/json"}
    session = gemini_client.get_session()

    for idx, api_key in enumerate(api_keys):
        url = f"{GEMINI_BASE_URL}/{EMBEDDING_MODEL}:embedContent?key={api_key}"
        try:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=12, connect=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("embedding", {}).get("values")
                err = (await resp.text())[:150]
                logger.warning(f"Embedding API [key#{idx + 1}] HTTP {resp.status}: {err}")
                if resp.status in (429, 500, 502, 503, 504):
                    continue
                return None
        except Exception as e:
            logger.warning(f"Embedding API [key#{idx + 1}] lỗi: {type(e).__name__}: {e}")
    return None


def _clamp_importance(value: Any, default: int = 2) -> int:
    try:
        imp = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(3, imp))


def compute_time_adjusted_score(raw_sim: float, importance: int, created_at: float, now: float) -> float:
    """
    Tính điểm truy xuất có trọng số thời gian dựa trên mức độ quan trọng (1/2/3):
    - imp=3 (cốt lõi): không suy giảm theo thời gian (decay = 0).
    - imp=2 (thông tin trung bình): suy giảm rất nhẹ (tối đa -0.08, tau=45 ngày).
    - imp=1 (chuyện vụn vặt hằng ngày): suy giảm rõ theo thời gian (tối đa -0.22, tau=14 ngày)
      nên query vu vơ sẽ không còn tự nổi lên, nhưng nhắc đúng từ khóa trực tiếp (raw_sim cao) vẫn kéo lên được.
    """
    age_days = max(0.0, (now - (created_at or now)) / 86400.0)
    if importance >= 3:
        decay_penalty = 0.0
    elif importance == 2:
        decay_penalty = 0.08 * (1.0 - math.exp(-age_days / 45.0))
    else:
        decay_penalty = 0.22 * (1.0 - math.exp(-age_days / 14.0))

    recency_bonus = 0.04 * math.exp(-age_days / 7.0)
    return raw_sim + recency_bonus - decay_penalty


async def save_memory_with_embedding(
    user_id: int,
    fact: str,
    category: str = "general",
    expires_at: Optional[float] = None,
    created_at: Optional[float] = None,
    importance: Optional[int] = None,
):
    """Lưu ký ức mới vào SQLite đồng thời tự động tạo vector embedding nhị phân."""
    if not fact or len(fact.strip()) < 3:
        return
    fact = fact.strip()
    embedding_blob = None
    try:
        vec = await get_text_embedding(fact, key_role="memory")
        if vec:
            embedding_blob = pack_vector(vec)
    except Exception as e:
        logger.error(f"Error generating embedding for fact '{fact}': {e}")

    imp_val = _clamp_importance(importance, default=1 if category == "episode" else 2) if importance is not None else None
    database.add_memory(
        user_id,
        fact,
        category=category,
        embedding=embedding_blob,
        expires_at=expires_at,
        created_at=created_at,
        importance=imp_val,
    )


async def backfill_missing_embeddings():
    """Tự động bổ sung vector embedding cho các ký ức cũ chưa có embedding."""
    unembedded = database.get_unembedded_memories()
    if not unembedded:
        return
    logger.info(f"Đang tạo vector embedding cho {len(unembedded)} ký ức cũ trong database...")
    throttle = len(unembedded) > 3
    for idx, item in enumerate(unembedded):
        if throttle and idx > 0:
            await asyncio.sleep(0.4)
        mem_id = item["id"]
        fact = item["fact"]
        try:
            vec = await get_text_embedding(fact)
            if vec:
                blob = pack_vector(vec)
                database.update_memory_embedding(mem_id, blob)
                logger.info(f"Đã cập nhật vector cho ký ức ID {mem_id}: '{fact[:40]}...'")
        except Exception as e:
            logger.error(f"Lỗi backfill vector cho ký ức ID {mem_id}: {e}")


def _select_core_facts(all_memories: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    """Lấy các Fact Book cốt lõi (ưu tiên user_fact/general, sau đó shared_moment/inside_joke)."""
    candidates = [
        m for m in (all_memories or [])
        if (m.get("category") or "general") in CORE_FACT_CATEGORIES and m.get("fact")
    ]
    cat_priority = {"user_fact": 2, "general": 2, "shared_moment": 1, "inside_joke": 1, "promise": 0}
    candidates.sort(
        key=lambda x: (
            cat_priority.get(x.get("category") or "general", 0),
            _clamp_importance(x.get("importance"), default=2),
            float(x.get("updated_at") or x.get("created_at") or 0.0),
        ),
        reverse=True,
    )
    out: List[str] = []
    seen = set()
    for item in candidates:
        f = str(item["fact"]).strip()
        if f and f not in seen:
            seen.add(f)
            out.append(f)
            if len(out) >= limit:
                break
    return out


def _select_today_highlight_episodes(all_memories: List[Dict[str, Any]], limit: int = 4) -> List[str]:
    """Lấy tối đa `limit` episode quan trọng (imp>=2) trong ngày hôm nay theo thứ tự thời gian."""
    _, start_ts, _ = _vn_day_bounds()
    today_eps = [
        m for m in (all_memories or [])
        if (m.get("category") or "") == "episode"
        and m.get("fact")
        and float(m.get("created_at") or 0.0) >= start_ts
        and _clamp_importance(m.get("importance"), default=1) >= 2
    ]
    today_eps.sort(key=lambda x: (float(x.get("created_at") or 0.0), int(x.get("id") or 0)))
    return [str(m["fact"]).strip() for m in today_eps[-limit:] if str(m["fact"]).strip()]


def _merge_baseline_and_retrieved(baseline: List[str], retrieved: List[str], max_items: int = 10) -> List[str]:
    out: List[str] = []
    seen = set()
    for src in (baseline or []) + (retrieved or []):
        s = str(src or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= max_items:
                break
    return out


def get_core_facts_for_prompt(user_id: int, limit: int = 6) -> List[str]:
    """Truy vấn nhanh Fact Book nền (user_fact/general/shared_moment) để tiêm vào Pass 2 / Proactive."""
    rows = database.get_memories_by_categories(user_id, exclude_categories=["episode"], limit=30)
    return _select_core_facts(rows, limit=limit)


def _format_recent_turns(recent_history: Optional[List[Dict[str, Any]]], max_turns: int = 8) -> str:
    if not recent_history:
        return "(Chưa có lịch sử gần đây)"
    import diary_helper
    p = persona.get_persona_config()
    now_v = time.time()
    lines = []
    for msg in recent_history[-max_turns:]:
        speaker = p["owner_name"] if msg["role"] == "user" else p["butler_name"]
        ts_val = msg.get("timestamp") if msg.get("timestamp") is not None else msg.get("created_at")
        rel = diary_helper.format_relative_vn_ts(ts_val, now_ts=now_v) if ts_val else ""
        ts_prefix = f"[{rel}] " if rel else ""
        lines.append(f"- {ts_prefix}{speaker}: {msg['content']}")
    return "\n".join(lines)


def _sanitize_memory_pronoun_inversion(text: str) -> str:
    """Giữ sạch văn bản ký ức trước khi lưu vào database."""
    if not text or not isinstance(text, str):
        return text
    return " ".join(text.strip().split())


async def _rewrite_search_queries(
    query_text: str,
    recent_history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, List[str]]:
    """
    Bước 1 truy xuất: Dùng MEMORY_MODEL giải mã đại từ/ngữ cảnh ('hôm qua', 'vụ đó')
    thành 1-3 truy vấn rõ nghĩa để tìm Vector Search chính xác.
    """
    import gemini_client

    p = persona.get_persona_config()
    owner_name = p["owner_name"]
    butler_name = p["butler_name"]
    recent_block = _format_recent_turns(recent_history, max_turns=10)
    prompt = f"""
Đoạn hội thoại gần đây giữa {owner_name} và {butler_name}:
{recent_block}

Tin nhắn mới nhất của {owner_name}:
"{query_text}"

Nhiệm vụ: Xác định xem để trả lời chính xác và đúng ngữ cảnh, {butler_name} có cần lục lại ký ức cũ (sở thích, thói quen, thiết bị, công việc, lời dặn, sự kiện đã trao đổi trước đây) không.
- Nếu chỉ là câu chào xã giao đơn thuần, lệnh bật/tắt thiết bị trực tiếp hoặc phản hồi hoàn toàn nằm trong 6 tin gần nhất không liên quan quá khứ, đặt "need_memory": false.
- Nếu có nhắc ẩn ý ("vụ đó", "hôm bữa", "lần trước", "thói quen kia"...) hoặc hỏi về sở thích/lịch trình/thông tin nền cá nhân, đặt "need_memory": true và viết 1-3 câu truy vấn tìm kiếm rõ nghĩa (đã thay thế đại từ bằng chủ thể thật từ ngữ cảnh).

Trả về duy nhất JSON:
{{
  "need_memory": true,
  "queries": ["truy vấn 1", "truy vấn 2"]
}}
"""
    try:
        raw = await gemini_client.call_gemini_api(
            model=config.MEMORY_MODEL,
            system_instruction="Bạn là module trích xuất từ khóa tìm kiếm ký ức siêu nhanh. Chỉ trả JSON hợp lệ.",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            response_json=True,
            key_role="memory",
            timeout_sec=10.0,
        )
        data = gemini_client.parse_ai_json(raw)
        need_mem = bool(data.get("need_memory", True))
        queries = [q.strip() for q in (data.get("queries") or []) if isinstance(q, str) and q.strip()]
        if need_mem and not queries:
            queries = [query_text.strip()]
        return need_mem, queries[:3]
    except gemini_client.GeminiQuotaError:
        # Hết hạn mức thì đừng đốt thêm request: bỏ qua truy xuất sâu, dùng fact cơ bản
        raise
    except Exception as e:
        logger.warning(f"Query Rewriter fallback do lỗi: {e}")
        return True, [query_text.strip()]


def _search_dual_tier_candidates(
    all_memories: List[Dict[str, Any]],
    query_vecs: List[List[float]],
    fact_threshold: float = 0.52,
    episode_threshold: float = 0.53,
    top_facts: int = 8,
    top_episodes: int = 8,
    recall_penalties: Optional[Dict[int, float]] = None,
) -> Tuple[List[Tuple[float, Dict[str, Any]]], List[Tuple[float, Dict[str, Any]]]]:
    """
    Chấm điểm Cosine Similarity song song cho cả 2 tầng (Fact Book & Episodic Turns),
    kết hợp trọng số thời gian (Time Decay & Recency Bonus) và Recall Ledger penalty chống lặp ký ức.
    """
    now = time.time()
    penalties = recall_penalties or {}
    fact_scored: List[Tuple[float, Dict[str, Any]]] = []
    episode_scored: List[Tuple[float, Dict[str, Any]]] = []

    for item in all_memories:
        blob = item.get("embedding")
        if not blob:
            continue
        try:
            vec = unpack_vector(blob)
        except Exception:
            continue

        best_sim = max(cosine_similarity(qv, vec) for qv in query_vecs)
        cat = item.get("category") or "general"
        default_imp = 1 if cat == "episode" else 2
        importance = _clamp_importance(item.get("importance"), default=default_imp)
        mid = int(item.get("id") or 0)
        penalty = float(penalties.get(mid, 0.0)) if (mid and cat not in CORE_FACT_CATEGORIES) else 0.0

        if cat == "episode":
            created = item.get("created_at") or now
            effective_score = compute_time_adjusted_score(best_sim, importance, created, now) - penalty
            if effective_score >= episode_threshold:
                episode_scored.append((effective_score, item))
        else:
            ref_ts = item.get("updated_at") or item.get("created_at") or now
            effective_score = compute_time_adjusted_score(best_sim, importance, ref_ts, now) - penalty
            if effective_score >= fact_threshold:
                fact_scored.append((effective_score, item))

    fact_scored.sort(key=lambda x: x[0], reverse=True)
    episode_scored.sort(key=lambda x: x[0], reverse=True)
    return fact_scored[:top_facts], episode_scored[:top_episodes]


async def _synthesize_memories(
    query_text: str,
    recent_history: Optional[List[Dict[str, Any]]],
    fact_candidates: List[Tuple[float, Dict[str, Any]]],
    episode_candidates: List[Tuple[float, Dict[str, Any]]],
    core_facts: List[str],
) -> Tuple[List[str], bool]:
    """
    Bước 3 truy xuất: Dùng MEMORY_MODEL lọc bỏ nhiễu và tổng hợp đúng những ký ức
    hữu ích nhất (ngôi 'em - anh') kèm cờ is_hit cho biết có tìm thấy thông tin khớp thật sự hay không.
    """
    import gemini_client

    cand_lines = []
    for score, item in fact_candidates:
        ts_str = format_vn_timestamp(item.get("updated_at") or item.get("created_at"))
        imp = _clamp_importance(item.get("importance"), default=2)
        cand_lines.append(f"- [Fact #{item['id']} | {item.get('category')} | imp={imp} | {ts_str} | score={score:.2f}] {item['fact']}")
    for score, item in episode_candidates:
        ts_str = format_vn_timestamp(item.get("created_at"))
        imp = _clamp_importance(item.get("importance"), default=1)
        cand_lines.append(f"- [Sự kiện #{item['id']} | imp={imp} | {ts_str} | score={score:.2f}] {item['fact']}")

    if not cand_lines:
        return core_facts[:6], False

    p = persona.get_persona_config()
    owner_name = p["owner_name"]
    butler_name = p["butler_name"]
    recent_block = _format_recent_turns(recent_history, max_turns=8)
    prompt = f"""
Đoạn chat vừa diễn ra:
{recent_block}
Tin nhắn mới của {owner_name}: "{query_text}"

Các ứng viên ký ức tìm thấy trong kho vector:
{chr(10).join(cand_lines)}

Nhiệm vụ: Chọn lọc những mẩu ký ức thực sự liên quan và trả lời đúng trọng tâm tin nhắn trên, viết lại thành 1 đến 6 gạch đầu dòng ngắn gọn, rõ chủ thể ({owner_name} và trợ lý {butler_name}).
- Giữ rõ mốc ngày/giờ nếu có sự kiện cũ và mới liên quan nhau.
- Chỉ tổng hợp đúng dữ kiện có trong danh sách ứng viên.
- Nếu danh sách ứng viên chỉ là chuyện khác chủ đề và chưa chứa dữ kiện liên quan, trả về mảng rỗng [].

Trả về duy nhất JSON:
{{
  "relevant_memories": [
    "Gạch đầu dòng ký ức 1"
  ]
}}
"""
    try:
        raw = await gemini_client.call_gemini_api(
            model=config.MEMORY_MODEL,
            system_instruction=f"Bạn là bộ lọc trí nhớ chính xác cho trợ lý AI {butler_name}. Chỉ trả JSON hợp lệ.",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            response_json=True,
            key_role="memory",
            timeout_sec=12.0,
        )
        data = gemini_client.parse_ai_json(raw)
        synthesized = [
            m.strip() for m in (data.get("relevant_memories") or [])
            if isinstance(m, str) and len(m.strip()) >= 4
        ]
        if synthesized:
            return synthesized[:6], True
        return [], False
    except Exception as e:
        logger.warning(f"Memory Synthesizer fallback do lỗi: {e}")

    # Fallback an toàn nếu Synthesizer lỗi
    fallback = [item["fact"] for score, item in (fact_candidates[:3] + episode_candidates[:3]) if score >= 0.50]
    if fallback:
        return fallback, True
    return core_facts[:3], False


async def retrieve_memory_context_with_status(
    user_id: int,
    query_text: str,
    recent_history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Pipeline Truy xuất Trí nhớ Đa Model kèm trạng thái memory_miss:
    - memory_miss=True khi người dùng nhắc/hỏi thông tin ngoài hội thoại gần đây
      nhưng kho nhớ chưa có dữ kiện khớp, giúp Pass 1 hỏi lại tự nhiên thay vì đoán mò.
    """
    all_memories = database.get_memories_for_retrieval(user_id, max_episodes=RETRIEVAL_MAX_EPISODES)
    if not all_memories:
        return {"memories": [], "memory_miss": False, "queries": []}

    core_facts = _select_core_facts(all_memories, limit=6)
    today_highlights = _select_today_highlight_episodes(all_memories, limit=4)
    baseline_memories = _merge_baseline_and_retrieved(core_facts, today_highlights, max_items=8)
    clean_query = (query_text or "").strip()

    # Chưa có episode nào (bot mới chạy / vừa backfill) -> dùng thẳng fact cơ bản, khỏi tốn 3 request
    has_episodes = any((m.get("category") == "episode") for m in all_memories)
    if not has_episodes:
        return {"memories": baseline_memories, "memory_miss": False, "queries": []}

    # Với tin nhắn hệ thống chủ động hoặc câu xã giao quá ngắn -> dùng ngay baseline facts + diễn biến nổi bật hôm nay
    if clean_query.startswith("[HỆ THỐNG:") or is_trivial_message(clean_query):
        return {"memories": baseline_memories, "memory_miss": False, "queries": []}

    # Câu ngắn (< 25 ký tự) thường chỉ là tán gẫu: bỏ qua 3 request (rewrite + embed + synth)
    # nhưng VẪN giữ đủ Fact Book nền + sự kiện nổi bật hôm nay để không mất ngữ cảnh.
    if len(clean_query) < 25:
        logger.debug(f"🧠 [Memory Short-skip]: câu ngắn '{clean_query}' -> dùng fact cơ bản + mốc hôm nay.")
        return {"memories": baseline_memories, "memory_miss": False, "queries": []}

    # Bước 1: Query Rewriter
    try:
        need_memory, queries = await _rewrite_search_queries(clean_query, recent_history=recent_history)
    except Exception as e:
        # Hết hạn mức / lỗi mạng: dùng fact cơ bản thay vì gọi tiếp 2 request nữa chắc chắn cũng lỗi
        logger.warning(f"🧠 [Memory Fallback]: bỏ qua truy xuất sâu do {type(e).__name__}: {e}")
        return {"memories": baseline_memories, "memory_miss": False, "queries": []}
    if not need_memory:
        logger.debug(f"🧠 [Memory Skip]: Câu không cần lục trí nhớ cũ ('{clean_query[:30]}...')")
        return {"memories": baseline_memories, "memory_miss": False, "queries": []}

    # Bước 2: Embed đồng thời các câu truy vấn
    embed_tasks = [get_text_embedding(q, key_role="memory") for q in queries]
    vecs_raw = await asyncio.gather(*embed_tasks, return_exceptions=True)
    query_vecs = [v for v in vecs_raw if isinstance(v, list) and v]

    if not query_vecs:
        return {"memories": baseline_memories, "memory_miss": True, "queries": queries}

    recall_penalties = database.get_recall_penalties(user_id)
    fact_cands, ep_cands = _search_dual_tier_candidates(
        all_memories,
        query_vecs,
        recall_penalties=recall_penalties,
    )
    if not fact_cands and not ep_cands:
        logger.info(f"🧠 [Memory Miss]: queries={queries} -> 0 ứng viên đạt ngưỡng.")
        return {"memories": baseline_memories, "memory_miss": True, "queries": queries}

    # Ít ứng viên (<= 2) thì dùng luôn, bỏ qua 1 request gọi Memory Synthesizer
    if len(fact_cands) + len(ep_cands) <= 2:
        quick_items = fact_cands + ep_cands
        quick = [item["fact"] for _, item in quick_items]
        recalled_ids = [
            int(item["id"]) for _, item in quick_items
            if item.get("id") and (item.get("category") or "general") not in CORE_FACT_CATEGORIES
        ]
        database.record_recalled_memories(user_id, recalled_ids)
        logger.info(f"🧠 [Memory Quick-hit]: {len(quick)} ứng viên -> dùng trực tiếp (tiết kiệm 1 request).")
        merged = _merge_baseline_and_retrieved(core_facts[:5], quick + today_highlights[-2:], max_items=10)
        return {"memories": merged or baseline_memories, "memory_miss": False, "queries": queries}

    # Bước 3: Memory Synthesizer
    result, is_hit = await _synthesize_memories(clean_query, recent_history, fact_cands, ep_cands, core_facts)
    if not is_hit:
        logger.info(f"🧠 [Memory Miss sau lọc]: queries={queries} -> ứng viên không chứa đáp án khớp.")
        return {"memories": baseline_memories, "memory_miss": True, "queries": queries}

    non_core_cands = [
        item for _, item in (fact_cands + ep_cands)
        if item.get("id") and (item.get("category") or "general") not in CORE_FACT_CATEGORIES
    ]
    recalled_ids = [int(item["id"]) for item in non_core_cands[:6]]
    database.record_recalled_memories(user_id, recalled_ids)

    logger.info(
        f"🧠 [Memory Retrieved]: queries={queries} -> facts={len(fact_cands)}, "
        f"episodes={len(ep_cands)} => synthesized={len(result)} items"
    )
    merged = _merge_baseline_and_retrieved(core_facts[:5], result + today_highlights[-2:], max_items=10)
    return {"memories": merged, "memory_miss": False, "queries": queries}


async def retrieve_memory_context(
    user_id: int,
    query_text: str,
    recent_history: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Wrapper tương thích ngược trả về danh sách chuỗi ký ức."""
    res = await retrieve_memory_context_with_status(user_id, query_text, recent_history=recent_history)
    return res["memories"]


async def search_relevant_memories(
    user_id: int,
    query_text: str,
    top_k: int = 3,
    threshold: float = 0.62,
) -> List[str]:
    """Wrapper tương thích ngược cho các script cũ, ủy quyền cho retrieve_memory_context."""
    return await retrieve_memory_context(user_id, query_text, recent_history=None)


async def _decide_fact_evolution(new_fact: str, similar_facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Dùng MEMORY_MODEL đối chiếu Fact mới với Top 5 Fact Book tương đồng nhất
    để quyết định ADD / UPDATE / DELETE / NONE.
    """
    import gemini_client

    existing_lines = "\n".join(
        f"- ID {item['id']} [{item.get('category', 'general')} | imp={_clamp_importance(item.get('importance'), default=2)}]: {item['fact']}"
        for item in similar_facts
    )
    p = persona.get_persona_config()
    owner_name = p["owner_name"]
    butler_name = p["butler_name"]
    prompt = f"""
Thông tin mới vừa ghi nhận:
"{new_fact}"

Các ký ức lâu dài (Fact Book) tương đồng hiện có trong database:
{existing_lines}

Nhiệm vụ: Quyết định hành động tối ưu nhất để giữ kho Fact Book sạch, chính xác, không trùng lặp và không mâu thuẫn cho trợ lý {butler_name}.
LƯU Ý GÓC NHÌN: Mọi câu trong "content" viết ngắn gọn, rõ chủ thể ({owner_name} là người dùng, {butler_name} tự xưng '{p['self_ref']}').

Các lựa chọn "action":
- "NONE": Thông tin mới đã hoàn toàn nằm trong ký ức cũ, không có gì mới thêm.
- "ADD": Thông tin mới bổ sung một khía cạnh khác biệt, giữ cả cũ lẫn mới.
- "UPDATE": Thông tin mới cập nhật/thay đổi trạng thái/mở rộng cho một ký ức cũ cụ thể -> cung cấp "target_id" và viết lại "content" hoàn chỉnh gộp thông tin mới nhất.
- "DELETE": Ký ức cũ ở "target_id" đã hoàn toàn sai hoặc hết hiệu lực (tùy chọn thêm "content" nếu muốn thay bằng câu mới).

Trả về duy nhất JSON:
{{
  "action": "NONE" | "ADD" | "UPDATE" | "DELETE",
  "target_id": null,
  "content": "..."
}}
"""
    try:
        raw = await gemini_client.call_gemini_api(
            model=config.MEMORY_MODEL,
            system_instruction="Bạn là quản trị viên Fact Book chống trùng lặp & tiến hóa ký ức. Chỉ trả JSON.",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            response_json=True,
            key_role="memory",
            timeout_sec=12.0,
        )
        return gemini_client.parse_ai_json(raw)
    except Exception as e:
        logger.warning(f"Fact evolution model lỗi ({e}), fallback ADD")
        return {"action": "ADD", "content": new_fact}


async def evolve_fact_book_item(
    user_id: int,
    new_fact: str,
    category: str = "user_fact",
    created_at: Optional[float] = None,
    importance: int = 2,
):
    """
    Đưa một Fact mới qua bộ đối chiếu Top 5 Vector tương đồng và tiến hóa (ADD / UPDATE / DELETE).
    """
    if not new_fact or len(new_fact.strip()) < 5:
        return
    clean_fact = new_fact.strip()
    imp = _clamp_importance(importance, default=2)
    lowered = clean_fact.lower()
    if any(k in lowered for k in FORBIDDEN_FACT_KEYWORDS):
        logger.debug(f"Bỏ qua fact đùa tạm thời: '{clean_fact}'")
        return False
    if database.is_memory_rejected(user_id, clean_fact):
        logger.info(f"🚫 [Rejection Ledger]: Bỏ qua ký ức người dùng đã từ chối: '{clean_fact}'")
        return False

    new_vec = await get_text_embedding(clean_fact, key_role="memory")
    if not new_vec:
        return

    fact_book_mems = database.get_memories_with_embeddings(
        user_id,
        include_expired=False,
        exclude_categories=["episode"],
        require_embedding=True,
    )

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for m in fact_book_mems:
        try:
            sim = cosine_similarity(new_vec, unpack_vector(m["embedding"]))
            if sim >= 0.52:
                scored.append((sim, m))
        except Exception:
            continue

    scored.sort(key=lambda x: x[0], reverse=True)
    top_similar = [m for _, m in scored[:5]]

    if not top_similar:
        database.add_memory(
            user_id,
            clean_fact,
            category=category,
            embedding=pack_vector(new_vec),
            created_at=created_at,
            importance=imp,
        )
        logger.info(f"🧠 [Fact ADD | imp={imp}]: '{clean_fact}'")
        return

    decision = await _decide_fact_evolution(clean_fact, top_similar)
    action = str(decision.get("action", "NONE")).upper()
    valid_ids = {int(m["id"]): m for m in top_similar}
    raw_target = decision.get("target_id")
    target_id = None
    if raw_target is not None:
        m_id = re.search(r"\d+", str(raw_target))
        if m_id and int(m_id.group(0)) in valid_ids:
            target_id = int(m_id.group(0))
    updated_content = (decision.get("content") or clean_fact).strip()

    if action == "NONE":
        logger.debug(f"🧠 [Fact NONE - trùng]: '{clean_fact}'")
        return
    elif action == "UPDATE" and target_id:
        old_imp = _clamp_importance(valid_ids[target_id].get("importance"), default=2)
        merged_imp = max(old_imp, imp)
        upd_vec = await get_text_embedding(updated_content, key_role="memory")
        upd_blob = pack_vector(upd_vec) if upd_vec else pack_vector(new_vec)
        database.update_memory_fact(int(target_id), updated_content, embedding=upd_blob, importance=merged_imp)
        logger.info(f"🧠 [Fact UPDATE #{target_id} | imp={merged_imp}]: '{updated_content}'")
    elif action == "DELETE" and target_id:
        database.delete_memory(int(target_id))
        logger.info(f"🧠 [Fact DELETE #{target_id}]")
        if decision.get("content") and len(updated_content) >= 5:
            add_vec = await get_text_embedding(updated_content, key_role="memory")
            add_blob = pack_vector(add_vec) if add_vec else pack_vector(new_vec)
            database.add_memory(user_id, updated_content, category=category, embedding=add_blob, created_at=created_at, importance=imp)
            logger.info(f"🧠 [Fact ADD sau DELETE | imp={imp}]: '{updated_content}'")
    else:
        add_vec = await get_text_embedding(updated_content, key_role="memory")
        add_blob = pack_vector(add_vec) if add_vec else pack_vector(new_vec)
        database.add_memory(user_id, updated_content, category=category, embedding=add_blob, created_at=created_at, importance=imp)
        logger.info(f"🧠 [Fact ADD | imp={imp}]: '{updated_content}'")


def _resolve_open_loop_target_ts(
    target_iso_vn: Any,
    ol_topic: str,
    ol_note: str,
    user_msg: str,
    now_ts: float,
) -> Optional[float]:
    """Chuẩn hóa mốc thời gian hẹn của open_loop (xử lý đúng câu 'sáng mai' lúc 0h-5h sáng)."""
    import diary_helper

    now_dt = datetime.datetime.fromtimestamp(now_ts, tz=VN_TZ)
    target_ts = None
    if isinstance(target_iso_vn, str) and target_iso_vn.strip() and target_iso_vn.strip().lower() not in ("null", "none"):
        target_ts = diary_helper.parse_vn_clock_to_ts(target_iso_vn, now_ts=now_ts, roll_forward_if_past=True)

    if target_ts is None:
        cands = diary_helper.extract_vn_clock_candidates(
            f"{ol_topic} {ol_note}",
            now_dt,
            assume_same_day=(now_dt.hour < 5),
        )
        if len(cands) == 1:
            target_ts = cands[0][0].timestamp()

    if target_ts is not None and now_dt.hour < 5:
        target_dt = datetime.datetime.fromtimestamp(target_ts, tz=VN_TZ)
        joined = f"{user_msg} {ol_topic} {ol_note}".lower()
        if (
            (target_dt.date() - now_dt.date()).days == 1
            and 5 <= target_dt.hour <= 14
            and "ngày mốt" not in joined
            and "hôm sau nữa" not in joined
        ):
            target_dt = target_dt - datetime.timedelta(days=1)
            target_ts = target_dt.timestamp()
    return target_ts


async def process_turn_memory(
    user_id: int,
    user_msg: str,
    bot_msg: str,
    hint_fact: Optional[str] = None,
    timestamp: Optional[float] = None,
):
    """
    Xử lý ký ức chạy ngầm NGAY SAU mỗi lượt nhắn (Ban ngày = Episode-only):
    1) Bỏ qua nếu lượt chỉ gồm xã giao ngắn.
    2) Gọi MEMORY_MODEL tóm tắt lượt thành 1 Episodic Turn Memory (ngôi 'em - anh', kèm importance 1/2/3).
    3) Không đẩy thẳng vào user_fact ban ngày; việc thăng cấp lên Fact Book dài hạn được Audit cuối ngày (23:30).
    """
    u_clean = (user_msg or "").strip()
    b_clean = (bot_msg or "").strip()
    clean_hint = hint_fact.strip() if isinstance(hint_fact, str) and len(hint_fact.strip()) >= 5 else None

    if is_trivial_message(u_clean) and len(b_clean) < 40 and not clean_hint:
        return

    import gemini_client

    p = persona.get_persona_config()
    owner_name = p["owner_name"]
    butler_name = p["butler_name"]
    ts = timestamp or time.time()
    vn_dt_now = datetime.datetime.fromtimestamp(ts, tz=VN_TZ)
    vn_ts_str = format_vn_timestamp(ts)
    vn_iso_date = vn_dt_now.strftime("%Y-%m-%d")
    hint_line = f"\nGợi ý chi tiết đáng chú ý từ lượt chat: {clean_hint}" if clean_hint else ""
    pronoun_hint = gemini_client.resolve_pronoun_referent_hint(u_clean)
    referent_note = f"\n{pronoun_hint}" if pronoun_hint else ""

    prompt = f"""
Thời điểm lượt trò chuyện: {vn_ts_str} (ngày {vn_iso_date}, Giờ địa phương)
{owner_name} nhắn: "{u_clean}"
{butler_name} trả lời: "{b_clean}"{hint_line}

Nhiệm vụ: Ghi chép 1 dòng nhật ký sự kiện (Episode) ngắn gọn, rõ chủ thể ({owner_name} là người dùng, {butler_name} là trợ lý AI).
LƯU Ý PHÂN BIỆT: Nếu {owner_name} chỉ dự định/báo lịch trình ("chiều định đi...", "4h về"), ghi đúng là dự định — KHÔNG tự bịa thành lời cam kết.{referent_note}

1. "episode_summary": Tóm tắt ngắn gọn 1 câu (dưới 30 từ) nội dung cốt lõi vừa trao đổi (yêu cầu chính, bối cảnh, thao tác thiết bị đáng chú ý hoặc thông tin mới). Nếu chỉ là chào/ok rỗng hoặc lệnh bật tắt đơn thuần thì có thể đặt null.
2. "episode_importance": Chấm độ quan trọng (1, 2 hoặc 3):
   - 1 (Vụn vặt hằng ngày): hỏi đáp nhanh, than nóng/mệt, bật tắt thiết bị bình thường -> tự mờ dần theo thời gian.
   - 2 (Đáng nhớ vừa): lịch làm việc/học tập tuần này, kế hoạch cụ thể, sở thích mới, vấn đề kỹ thuật đang xử lý.
   - 3 (Cốt lõi / dài hạn): thông tin nền quan trọng, thói quen sinh hoạt cố định, ghi chú dài hạn của {owner_name}.
3. "durable_user_fact": CHỈ điền 1 câu ngắn nếu lượt này vừa lộ ra THÔNG TIN NỀN LÂU DÀI MỚI về {owner_name} (công việc, lịch học/làm việc cố định, sở thích âm nhạc/nhiệt độ, thói quen lặp lại, tên phòng/thiết bị, thú cưng/gia đình) để đưa ngay vào Fact Book; nếu chỉ là tin nhắn nhất thời thì BẮT BUỘC đặt null.
4. "open_loop": Đầu việc ĐANG THEO DÕI chưa khép lại ({owner_name} sắp có cuộc họp/thi/sự kiện, đang đợi kết quả, hoặc hẹn giờ nhắc tiếp).
   - **BẮT BUỘC đặt `action="none"`** đối với các lời dặn sinh hoạt thường ngày (đi ngủ, dậy sớm, đi ăn, uống nước, nghỉ ngơi) hoặc lệnh hẹn giờ thiết bị/báo thức (bật/tắt quạt, đèn, loa) vì hệ thống đã tự theo dõi riêng.
   - Nếu mở mới/cập nhật thì action="add", nếu đã xong/có kết quả ở lượt này thì action="resolve", bình thường action="none".
   - Nếu có mốc giờ hẹn rõ ràng (vd "11h dậy", "tối 9h họp"), điền "target_iso_vn": "YYYY-MM-DD HH:MM".

Trả về duy nhất JSON:
{{
  "episode_summary": "..." | null,
  "episode_importance": 1,
  "durable_user_fact": "..." | null,
  "open_loop": {{"action": "add|resolve|none", "topic": "..." | null, "note": "..." | null, "target_iso_vn": "YYYY-MM-DD HH:MM" | null}}
}}
"""
    try:
        raw = await gemini_client.call_gemini_api(
            model=config.MEMORY_MODEL,
            system_instruction=f"Bạn là bộ ghi nhật ký sự kiện (Episode) và lọc hồ sơ nền cho trợ lý AI {butler_name}. Chỉ trả JSON hợp lệ.",
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            response_json=True,
            key_role="memory",
            timeout_sec=15.0,
        )
        data = gemini_client.parse_ai_json(raw)
    except Exception as e:
        logger.warning(f"Post-turn Memory Extractor lỗi: {e}")
        if clean_hint and not database.is_memory_rejected(user_id, clean_hint):
            safe_hint = _sanitize_memory_pronoun_inversion(clean_hint)
            fallback_ep = safe_hint if safe_hint.startswith("[") else f"[{vn_ts_str}] {safe_hint}"
            await save_memory_with_embedding(user_id, fallback_ep, category="episode", created_at=ts, importance=2)
            logger.info(f"📝 [Episodic Fallback từ hint]: {fallback_ep}")
        return

    ep_summary = data.get("episode_summary")
    ep_imp = _clamp_importance(data.get("episode_importance"), default=1)
    if isinstance(ep_summary, str) and len(ep_summary.strip()) >= 8 and ep_summary.strip().lower() != "null":
        ep_text = ep_summary.strip()
    elif clean_hint:
        ep_text = clean_hint
        ep_imp = max(ep_imp, 2)
    else:
        ep_text = ""

    if ep_text:
        ep_text = _sanitize_memory_pronoun_inversion(ep_text)
        if not ep_text.startswith("["):
            ep_text = f"[{vn_ts_str}] {ep_text}"
        if not database.is_memory_rejected(user_id, ep_text):
            await save_memory_with_embedding(user_id, ep_text, category="episode", created_at=ts, importance=ep_imp)
            logger.info(f"📝 [Episodic Saved | imp={ep_imp}]: {ep_text}")

    durable_fact = data.get("durable_user_fact")
    if isinstance(durable_fact, str):
        clean_df = _sanitize_memory_pronoun_inversion(durable_fact.strip())
        if len(clean_df) >= 8 and clean_df.lower() != "null":
            await evolve_fact_book_item(
                user_id,
                clean_df,
                category="user_fact",
                created_at=ts,
                importance=2,
            )

    ol_raw = data.get("open_loop")
    if isinstance(ol_raw, dict):
        ol_act = str(ol_raw.get("action") or "none").strip().lower()
        ol_topic = _sanitize_memory_pronoun_inversion(str(ol_raw.get("topic") or "").strip())
        ol_note = _sanitize_memory_pronoun_inversion(str(ol_raw.get("note") or "").strip())
        if ol_topic and ol_topic.lower() != "null":
            if (
                ol_act == "add"
                and not database.is_memory_rejected(user_id, ol_topic)
                and not database.is_trivial_open_loop_topic(ol_topic, ol_note)
            ):
                target_ts = _resolve_open_loop_target_ts(
                    ol_raw.get("target_iso_vn"),
                    ol_topic,
                    ol_note,
                    u_clean,
                    ts,
                )
                if target_ts is None:
                    for rem in database.get_pending_reminders(user_id, kind="ai_promise", limit=5):
                        if abs(float(rem.get("created_at") or 0.0) - ts) <= 120.0 or database._topics_match(
                            str(rem.get("title") or ""), ol_topic
                        ):
                            target_ts = float(rem["due_at"])
                            break
                database.upsert_open_loop(user_id, ol_topic, ol_note, now_ts=ts, target_ts=target_ts)
                logger.info(f"🧵 [Open Loop ADD]: '{ol_topic}'" + (f" (target={format_vn_timestamp(target_ts)})" if target_ts else ""))
            elif ol_act == "resolve":
                if database.resolve_open_loop(user_id, ol_topic, now_ts=ts):
                    logger.info(f"🧵 [Open Loop RESOLVED]: '{ol_topic}'")


def _vn_day_bounds(date_str: Optional[str] = None) -> Tuple[str, float, float]:
    """Trả về (YYYY-MM-DD, start_utc_ts, end_utc_ts) theo múi giờ VN (UTC+7)."""
    if date_str:
        vn_day = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=VN_TZ)
    else:
        vn_now = datetime.datetime.now(VN_TZ)
        vn_day = vn_now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_str = vn_day.strftime("%Y-%m-%d")

    start_ts = vn_day.timestamp()
    end_ts = (vn_day + datetime.timedelta(days=1)).timestamp() - 1.0
    return date_str, start_ts, end_ts


async def _apply_fact_book_cleanup(
    valid_facts_map: Dict[int, Dict[str, Any]],
    cleanup_actions: List[Dict[str, Any]],
) -> Tuple[int, int]:
    """Thực thi xóa/gộp các dòng Fact Book dư thừa hoặc sự kiện nhất thời lọt nhầm."""
    deleted = 0
    updated = 0
    removed_ids = set()

    for act in cleanup_actions:
        if not isinstance(act, dict):
            continue
        action = str(act.get("action") or "").upper().strip()
        try:
            target_id = int(act.get("target_id"))
        except (TypeError, ValueError):
            continue
        if target_id not in valid_facts_map or target_id in removed_ids:
            continue

        orig = valid_facts_map[target_id]
        if action == "DELETE":
            database.delete_memory(target_id)
            removed_ids.add(target_id)
            deleted += 1
            logger.info(f"🧹 [Audit DELETE #{target_id}] '{orig['fact']}' (lý do: {act.get('reason', '')})")
        elif action == "UPDATE":
            new_text = str(act.get("content") or "").strip()
            new_imp = _clamp_importance(act.get("importance"), default=_clamp_importance(orig.get("importance"), 2))
            if len(new_text) < 5:
                continue
            if new_text != orig["fact"]:
                vec = await get_text_embedding(new_text, key_role="memory")
                blob = pack_vector(vec) if vec else None
                database.update_memory_fact(target_id, new_text, embedding=blob, importance=new_imp)
            else:
                database.update_memory_fact(target_id, orig["fact"], importance=new_imp)
            orig["fact"] = new_text
            orig["importance"] = new_imp
            updated += 1
            logger.info(f"🧹 [Audit UPDATE #{target_id} | imp={new_imp}] -> '{new_text}'")

    return deleted, updated


async def audit_and_consolidate_memories(
    user_id: int,
    target_date_str: Optional[str] = None,
    force: bool = False,
    force_full_factbook: bool = False,
):
    """
    Tiến trình Nightly Memory Auditor (chạy lúc 23:30 hoặc chạy bù hôm sau):
    1) Đọc toàn bộ Episode phát sinh trong ngày (+ đoạn chat ngày đó) và toàn bộ Fact Book hiện tại.
    2) Chắt lọc những thông tin THỰC SỰ BỀN VỮNG để thăng cấp vào user_fact / shared_moment / inside_joke / promise.
    3) Dọn/gộp Fact Book cũ nếu phát hiện dòng trùng hoặc sự kiện nhất thời lọt nhầm.
    4) Ghi dấu ngày đã audit vào app_metadata để chống trùng và hỗ trợ chạy bù.
    """
    import gemini_client

    audit_date, start_ts, end_ts = _vn_day_bounds(target_date_str)
    meta_key = f"nightly_audit_last_date:{user_id}"
    last_done = database.get_metadata(meta_key)
    if not force and not force_full_factbook and last_done == audit_date:
        logger.debug(f"Nightly Audit ngày {audit_date} cho user {user_id} đã chạy trước đó -> bỏ qua.")
        return

    if force_full_factbook:
        episodes = database.get_memories_by_categories(user_id, categories=["episode"], limit=120)
        episodes.reverse()
        day_history = database.get_recent_history(user_id, limit=40)
    else:
        episodes = database.get_memories_in_time_range(user_id, start_ts, end_ts, categories=["episode"], limit=120)
        day_history = database.get_chat_history_since(user_id, start_ts, end_ts=end_ts, limit=80)

    if not episodes and not day_history and not force_full_factbook:
        database.set_metadata(meta_key, audit_date)
        logger.info(f"Nightly Audit ngày {audit_date}: không có hội thoại/episode mới -> đánh dấu hoàn tất.")
        return

    fact_book = database.get_memories_by_categories(
        user_id,
        exclude_categories=["episode", "promise"],
        limit=200,
    )
    valid_facts_map = {int(m["id"]): m for m in fact_book}

    ep_lines = "\n".join(
        f"- [Ep #{m['id']} | imp={_clamp_importance(m.get('importance'), 1)}] {m['fact']}"
        for m in episodes
    ) or "(Không có episode mới)"

    fb_lines = "\n".join(
        f"- [ID {m['id']} | {m.get('category', 'general')} | imp={_clamp_importance(m.get('importance'), 2)}] {m['fact']}"
        for m in sorted(fact_book, key=lambda x: int(x["id"]))
    ) or "(Chưa có Fact Book)"

    p = persona.get_persona_config()
    owner_name = p["owner_name"]
    butler_name = p["butler_name"]

    history_str = "\n".join(
        f"[{format_vn_timestamp(msg.get('created_at'))}] {owner_name if msg['role'] == 'user' else butler_name}: {msg['content']}"
        for msg in day_history[-50:]
    ) or "(Không có tin nhắn)"

    audit_prompt = f"""
Bạn là trợ lý AI {butler_name}, đang tổng kết lại trí nhớ ngày {audit_date} phục vụ {owner_name}.

=== 1. CÁC SỰ KIỆN TRONG NGÀY (EPISODES) ===
{ep_lines}

=== 2. ĐOẠN CHAT GẦN ĐÂY ĐỂ ĐỐI CHIẾU NGỮ CẢNH ===
{history_str}

=== 3. KHO FACT BOOK DÀI HẠN HIỆN CÓ ===
{fb_lines}

=== NHIỆM VỤ AUDIT CUỐI NGÀY ===
Thực hiện 2 việc gọn gàng, viết rõ chủ thể:

A. "promotions": Rút ra tối đa 3 điều MỚI thực sự bền vững mà Fact Book hiện tại chưa có (trả [] nếu các chuyện hôm nay chỉ mang tính nhất thời):
   - "user_fact": Thông tin nền bền vững của {owner_name} (công việc, lịch trình cố định, nơi ở, sở thích lâu dài, thói quen, thiết bị phòng, gia đình/thú cưng).
   - "preference": Gu âm nhạc, mức quạt/đèn ưa thích, phong cách giao tiếp hoặc giờ giấc mong muốn.
   - "important_note": Lưu ý quan trọng cần nhớ lâu dài khi hỗ trợ {owner_name}.
   - "promise": Cam kết/lịch hẹn cụ thể đã chốt rõ ràng (tự mờ sau 4 ngày).

B. "fact_book_cleanup": Rà lại danh sách KHO FACT BOOK (mục 3) để giữ kho tinh gọn:
   - "DELETE": Xóa dòng thực chất là sự kiện nhất thời, hoặc dòng trùng ý đã nằm gọn trong một ID đầy đủ hơn.
   - "UPDATE": Gộp 2 dòng giao ý vào 1 `target_id` (viết lại "content" hoàn chỉnh, kèm "importance" 2 hoặc 3) rồi dùng "DELETE" cho ID dư còn lại.

Trả về duy nhất JSON:
{{
  "promotions": [
    {{"category": "user_fact|preference|important_note|promise", "content": "...", "importance": 2}}
  ],
  "fact_book_cleanup": [
    {{"action": "DELETE", "target_id": 123, "reason": "..."}},
    {{"action": "UPDATE", "target_id": 456, "content": "...", "importance": 3, "reason": "..."}}
  ]
}}
"""
    try:
        raw_res = await gemini_client.call_gemini_api(
            model=config.MEMORY_MODEL,
            system_instruction=f"Bạn là bộ đánh giá và kết tinh ký ức cuối ngày cho trợ lý AI {butler_name}. Chỉ trả JSON hợp lệ.",
            contents=[{"role": "user", "parts": [{"text": audit_prompt}]}],
            response_json=True,
            key_role="memory",
            timeout_sec=30.0,
        )
        audit_data = gemini_client.parse_ai_json(raw_res)
    except Exception as e:
        logger.warning(f"Lỗi khi chạy Nightly Memory Audit ngày {audit_date}: {e}")
        return

    cleanup_actions = audit_data.get("fact_book_cleanup") or []
    deleted, updated = await _apply_fact_book_cleanup(valid_facts_map, cleanup_actions)

    promotions = audit_data.get("promotions") or []
    promoted = 0
    promo_ts = min(end_ts, time.time()) if target_date_str and not force_full_factbook else time.time()

    for item in promotions[:4]:
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category") or "user_fact").strip()
        if cat not in ("user_fact", "preference", "important_note", "shared_moment", "inside_joke", "promise"):
            cat = "user_fact"
        content = str(item.get("content") or "").strip()
        if len(content) < 5:
            continue
        if database.is_memory_rejected(user_id, content):
            logger.info(f"🚫 [Rejection Ledger]: Bỏ qua promotion bị user từ chối: '{content}'")
            continue

        if cat == "promise":
            expires_at = time.time() + 4 * 86400
            vec = await get_text_embedding(content, key_role="memory")
            if vec:
                database.add_memory(user_id, content, category="promise", embedding=pack_vector(vec), expires_at=expires_at, importance=2)
                promoted += 1
                logger.info(f"🌟 [Audit PROMOTE promise]: '{content}'")
        else:
            default_imp = 3 if cat in ("shared_moment", "important_note") else 2
            imp = _clamp_importance(item.get("importance"), default=default_imp)
            added = await evolve_fact_book_item(user_id, content, category=cat, created_at=promo_ts, importance=imp)
            if added is not False:
                promoted += 1

    # Dọn episode cũ để giữ tốc độ truy xuất ổn định (chi phí cosine tăng tuyến tính theo số ký ức)
    pruned = database.prune_episodes(
        user_id,
        older_than_days=EPISODE_PRUNE_AFTER_DAYS,
        keep_max=EPISODE_KEEP_MAX,
    )
    database.set_metadata(meta_key, audit_date)
    logger.info(
        f"✅ Nightly Audit [{audit_date}] hoàn tất: deleted={deleted}, updated={updated}, promoted={promoted}, "
        f"pruned_episodes={pruned}"
    )


async def consolidate_nightly_memories(
    user_id: int,
    target_date_str: Optional[str] = None,
    force: bool = False,
):
    """Alias tương thích cho lịch 23:30 và cơ chế chạy bù trong scheduler."""
    await audit_and_consolidate_memories(user_id, target_date_str=target_date_str, force=force)


if __name__ == "__main__":
    import sys

    async def _cli():
        database.init_db()
        uid = config.ALLOWED_USER_ID or database.get_primary_user_id()
        for arg in sys.argv:
            if arg.startswith("--user="):
                uid = int(arg.split("=", 1)[1])
        if uid <= 0:
            logger.error("Không tìm thấy user_id hợp lệ.")
            return
        import gemini_client
        try:
            if "--audit-now" in sys.argv:
                await audit_and_consolidate_memories(uid, force=True, force_full_factbook=True)
        finally:
            await gemini_client.close_session()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(_cli())
