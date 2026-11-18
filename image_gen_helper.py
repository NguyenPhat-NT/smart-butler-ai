# -*- coding: utf-8 -*-
import os
import random
import logging
import urllib.parse
import aiohttp

logger = logging.getLogger(__name__)

async def generate_candid_photo(prompt: str, output_path: str) -> bool:
    """
    Tạo ảnh đời thực mới toanh theo thời gian thực bằng engine Flux (Pollinations AI).
    Hoàn toàn miễn phí, không tốn quota API Gemini, chất lượng ảnh chụp smartphone rất tự nhiên.
    """
    if not prompt or len(prompt.strip()) < 5:
        return False
        
    dir_name = os.path.dirname(output_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    
    # Tinh chỉnh prompt để đảm bảo phong cách ảnh chụp đời thực (candid snapshot)
    refined_prompt = f"candid realistic smartphone snapshot, {prompt.strip()}, natural lighting, authentic everyday photo, 35mm lens, not 3D, not cartoon"
    encoded_prompt = urllib.parse.quote(refined_prompt)
    seed = random.randint(1000, 999999)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?nologo=true&width=768&height=768&seed={seed}&model=flux"
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=35) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    if len(image_bytes) > 5000: # Đảm bảo file ảnh hợp lệ (>5KB)
                        with open(output_path, "wb") as f:
                            f.write(image_bytes)
                        return True
                    else:
                        logger.warning("Ảnh tải về dung lượng quá nhỏ, có thể bị lỗi.")
                else:
                    logger.warning(f"Pollinations trả về HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Lỗi tạo ảnh thời gian thực: {e}")
        
    return False
