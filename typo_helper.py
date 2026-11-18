# -*- coding: utf-8 -*-
import random
import re
from typing import List, Tuple, Optional

# Danh sách các từ tiếng Việt dễ gõ nhầm khi gõ nhanh trên điện thoại
COMMON_TYPOS = {
    "đang": ["đnag", "đag"],
    "không": ["khôgn", "k"],
    "được": ["đươcj", "đc"],
    "nhiều": ["nheìu", "nhìeu"],
    "trời": ["trừoi", "troi"],
    "thật": ["thtaj", "thât"],
    "ngủ": ["nhủ", "ngur"],
    "vậy": ["vạy", "vay"],
    "mình": ["mìng", "mifnh"]
}

def maybe_inject_typo(messages: List[str], chance: float = 0.12) -> Tuple[List[str], Optional[str]]:
    """
    Mô phỏng thói quen gõ nhầm tự nhiên của người thật trên điện thoại:
    - Khoảng 10-15% xác suất gõ nhầm 1 từ trong tin nhắn.
    - Không biến đổi trực tiếp list đầu vào, giữ nguyên viết hoa đầu từ và dấu câu đi kèm.
    - Trả về danh sách tin nhắn (đã có từ gõ nhầm) kèm câu đính chính gửi ngay sau đó (ví dụ: '*đang').
    """
    if random.random() > chance or not messages:
        return messages, None

    out_messages = list(messages)
    # Chọn ngẫu nhiên 1 câu để gõ nhầm
    target_idx = random.randint(0, len(out_messages) - 1)
    target_msg = out_messages[target_idx]

    words = target_msg.split()
    for i, word in enumerate(words):
        match = re.match(r"^([^\w]*)([\w]+)([^\w]*)$", word)
        if not match:
            continue
        prefix, core, suffix = match.groups()
        clean_word = core.lower()
        if clean_word in COMMON_TYPOS:
            typo_variant = random.choice(COMMON_TYPOS[clean_word])
            if core[0].isupper() and typo_variant:
                typo_variant = typo_variant[0].upper() + typo_variant[1:]

            words[i] = f"{prefix}{typo_variant}{suffix}"
            out_messages[target_idx] = " ".join(words)

            # Tạo câu đính chính tự nhiên
            correction_style = random.choice([
                f"*{clean_word}",
                f"*{clean_word} kkk",
                f"*{clean_word} á, gõ vội quá :v"
            ])
            return out_messages, correction_style

    return messages, None
