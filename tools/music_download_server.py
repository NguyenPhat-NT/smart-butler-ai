# -*- coding: utf-8 -*-
"""
PC LAN Music Download Bridge cho Smart Butler Hub.
Chạy trên PC/Server mạnh cùng mạng LAN để nhận yêu cầu tải nhạc từ OpenStick,
dùng `yt-dlp` + Node.js/QuickJS trên PC xử lý YouTube/SoundCloud trong vài giây,
và phục vụ file `.m4a`/`.mp3` để OpenStick stream chunk nhỏ về USB (< 2 MB RAM).

Cách chạy:
  python tools/music_download_server.py --port 8091 --api-key <KEY_GIONG_TTS_API_KEY>
"""
import argparse
import array
import asyncio
import glob
import hmac
import json
import logging
import math
import operator
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("music_dl_bridge")

_YT_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s<>\"'`]+)",
    re.IGNORECASE,
)
_SC_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.|on\.)?soundcloud\.com/[^\s<>\"'`]+)",
    re.IGNORECASE,
)
_YT_ID_RE = re.compile(r"(?:v=|/shorts/|/live/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_TRACK_NORM_VERSION = 2
_NORM_SAMPLE_RATE = 24000
_NORM_BLOCK_BYTES = 19200  # 0.4s mono 24kHz PCM16, khop voi esp32_dsp.LoudnessAnalyzer
_NORM_MAX_ANALYZE_SEC = 420

_DEFAULT_FILE_TTL_SEC = 900.0
_DEFAULT_MAX_CONCURRENT = 2


def extract_youtube_video_id(text: str) -> Optional[str]:
    m = _YT_ID_RE.search(str(text or ""))
    return m.group(1) if m else None


def normalize_youtube_single_url(url_or_text: str) -> Optional[str]:
    m = _YT_URL_RE.search(str(url_or_text or ""))
    if not m:
        return None
    raw_url = m.group(1).rstrip(").,;!?\"'")
    vid = extract_youtube_video_id(raw_url)
    return f"https://www.youtube.com/watch?v={vid}" if vid else raw_url


def extract_soundcloud_url(text: str) -> Optional[str]:
    m = _SC_URL_RE.search(str(text or ""))
    return m.group(1).rstrip(").,;!?\"'") if m else None


def detect_js_runtime() -> Optional[str]:
    """Tự phát hiện JS runtime ưu tiên JIT nhanh trên PC (node/deno/bun/quickjs)."""
    candidates = (
        (
            "node",
            ("node", "nodejs"),
            (
                r"C:\Program Files\nodejs\node.exe",
                r"C:\Program Files (x86)\nodejs\node.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
                "/usr/local/bin/node",
                "/usr/bin/node",
                "/usr/bin/nodejs",
            ),
        ),
        (
            "deno",
            ("deno",),
            (
                os.path.expandvars(r"%USERPROFILE%\.deno\bin\deno.exe"),
                "/usr/local/bin/deno",
                "/usr/bin/deno",
            ),
        ),
        (
            "bun",
            ("bun",),
            (
                os.path.expandvars(r"%USERPROFILE%\.bun\bin\bun.exe"),
                "/usr/local/bin/bun",
                "/usr/bin/bun",
            ),
        ),
        ("quickjs", ("qjs", "quickjs"), ("/usr/local/bin/qjs", "/usr/bin/qjs", "/usr/local/bin/quickjs")),
    )
    for rt_name, names, fixed_paths in candidates:
        for nm in names:
            found = shutil.which(nm)
            if found:
                return f"{rt_name}:{found}"
        for fp in fixed_paths:
            if os.path.isfile(fp) and os.access(fp, os.X_OK):
                return f"{rt_name}:{fp}"
    return None


def find_yt_dlp_cmd() -> List[str]:
    venv_yt = os.path.join(os.path.dirname(sys.executable), "yt-dlp" + (".exe" if os.name == "nt" else ""))
    if os.path.isfile(venv_yt):
        return [venv_yt]
    for cand in ("yt-dlp", "yt-dlp.exe"):
        found = shutil.which(cand)
        if found:
            return [found]
    return [sys.executable, "-m", "yt_dlp"]


def classify_yt_dlp_error(stdout_s: str, stderr_s: str, rc: int) -> Tuple[str, str]:
    merged = "\n".join(x for x in (stderr_s, stdout_s) if x).strip()
    low = merged.lower()
    err_lines = [l.strip() for l in merged.splitlines() if "error:" in l.lower() or "warning:" in l.lower()]
    detail = (err_lines[-1] if err_lines else (merged.splitlines()[-1].strip() if merged.splitlines() else "")) or "Không rõ lỗi"
    if "sign in to confirm" in low or "not a bot" in low or "cookies-from-browser" in low:
        return "bot_check", "YouTube yêu cầu xác minh tài khoản/cookies."
    if "larger than max-filesize" in low or "file is larger than" in low:
        return "too_large", "File nhạc vượt quá giới hạn dung lượng cho phép."
    if (
        "signature solving failed" in low
        or "challenge solver" in low
        or "supported javascript runtime" in low
        or "n challenge solving failed" in low
        or "error solving n challenge" in low
    ):
        return "js_challenge", "yt-dlp trên PC thiếu JS runtime hoặc EJS challenge solver."
    if "video unavailable" in low or "private video" in low or "requested format is not available" in low:
        return "unavailable", f"Video/định dạng không khả dụng: {detail[:160]}"
    if rc != 0:
        return "yt_dlp_error", f"yt-dlp lỗi ({rc}): {detail[:180]}"
    return "empty_file", "Không thấy file âm thanh sau khi tải xong."


def clean_oembed_search_title(title: str) -> str:
    raw = str(title or "").strip()
    if not raw:
        return ""
    s = re.sub(
        r"[\[\(][^\]\)]{1,40}(?:official|mv|lyric|lyrics|audio|live|hd|4k|video|visualizer)[^\]\)]{0,40}[\]\)]",
        " ",
        raw,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"[\U0001F1E0-\U0001FAFF\u2600-\u27BF\uFE0F\u200D|#♪♫★☆•·~]+", " ", s)
    if re.search(r"\b(?:playlist|tuyển tập|quả list|list|mix|tổng hợp)\b", s, flags=re.IGNORECASE):
        m_quote = re.search(r"['\"“‘]([^'\"”’]{3,50})['\"”’]", s)
        if m_quote:
            quoted = re.sub(r"\s+", " ", m_quote.group(1)).strip()
            if quoted:
                return quoted[:90]
    s = re.sub(r"['\"“”‘’]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:90] or raw[:90]


def build_soundcloud_fallback_queries(title: str) -> List[str]:
    clean = clean_oembed_search_title(title)
    if not clean:
        return []
    candidates = [clean]
    first_seg = re.split(r"\s[-–—:/|]\s", clean, maxsplit=1)[0].strip()
    if first_seg and len(first_seg) >= 4 and first_seg.lower() != clean.lower():
        candidates.append(first_seg)
    words = clean.split()
    if len(words) >= 5:
        short = " ".join(words[:4]).strip()
        if len(short) >= 4 and short.lower() not in {c.lower() for c in candidates}:
            candidates.append(short)
    return candidates


def _fetch_oembed_sync(yt_url: str, timeout_sec: float = 4.0) -> Optional[Dict[str, str]]:
    endpoint = f"https://www.youtube.com/oembed?url={urllib.parse.quote(yt_url, safe='')}&format=json"
    req = urllib.request.Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict) and data.get("title"):
                return {
                    "title": str(data.get("title") or "").strip(),
                    "author": str(data.get("author_name") or "").strip(),
                }
    except Exception:
        return None
    return None


class MusicDownloadWorker:
    """Thực thi tải bài hát bằng yt-dlp trên PC và quản lý file tạm theo token."""

    def __init__(
        self,
        cache_dir: str,
        cookies_path: str = "",
        file_ttl_sec: float = _DEFAULT_FILE_TTL_SEC,
        max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
    ):
        self.cache_dir = os.path.abspath(cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cookies_path = cookies_path.strip()
        self.file_ttl_sec = max(60.0, float(file_ttl_sec))
        self._max_concurrent = max(1, int(max_concurrent))
        self._sem: Optional[asyncio.Semaphore] = None
        self.tokens: Dict[str, Dict[str, Any]] = {}
        self.active_jobs = 0

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._max_concurrent)
        return self._sem

    def _effective_cookies_path(self) -> Optional[str]:
        if self.cookies_path and os.path.isfile(self.cookies_path) and os.path.getsize(self.cookies_path) > 0:
            return self.cookies_path
        default_cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt")
        if os.path.isfile(default_cand) and os.path.getsize(default_cand) > 0:
            return default_cand
        return None

    async def _remux_mp4_to_adts(self, filepath: str) -> bool:
        """Giải bọc AAC-in-MP4 (isom/dash) sang ADTS thuần bằng ffmpeg (copy stream, 0 re-encode) để faad đọc mượt."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin or not filepath or not os.path.isfile(filepath):
            return False
        ext = os.path.splitext(filepath)[1].lower()
        if ext not in (".m4a", ".mp4", ".aac"):
            return False
        try:
            with open(filepath, "rb") as fp:
                head = fp.read(16)
        except OSError:
            return False
        if len(head) < 12 or head[:2] in (b"\xff\xf1", b"\xff\xf9") or head[4:8] != b"ftyp":
            return False

        tmp_adts = filepath + ".adts.aac"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-i",
                filepath,
                "-vn",
                "-c:a",
                "copy",
                "-f",
                "adts",
                tmp_adts,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode == 0 and os.path.isfile(tmp_adts) and os.path.getsize(tmp_adts) > 128:
                with open(tmp_adts, "rb") as chk:
                    magic = chk.read(2)
                if magic in (b"\xff\xf1", b"\xff\xf9"):
                    os.replace(tmp_adts, filepath)
                    return True
        except Exception as e:
            logger.debug(f"[PC Music Bridge] Bỏ qua remux ADTS ({e})")
            if proc is not None and getattr(proc, "returncode", None) is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
        if os.path.isfile(tmp_adts):
            try:
                os.remove(tmp_adts)
            except OSError:
                pass
        return False

    async def _analyze_loudness(self, filepath: str) -> Optional[Dict[str, float]]:
        """Đo mức âm lượng chuẩn unweighted 24kHz (60Hz HP, 2-stage gate) khớp 100% với OpenStick DSP."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin or not filepath or not os.path.isfile(filepath):
            return None
        proc = None
        block_ms: List[float] = []
        max_peak = 0.0
        total_read = 0
        buf = bytearray()
        inv_scale = 1.0 / 32768.0
        inv_sq_scale = inv_scale * inv_scale
        mul = operator.mul
        min_tail_bytes = max(960, _NORM_BLOCK_BYTES // 10)
        hard_cap_bytes = (_NORM_MAX_ANALYZE_SEC + 5) * _NORM_SAMPLE_RATE * 2

        def _consume_block(raw_blk: bytes) -> None:
            nonlocal max_peak
            usable = len(raw_blk) - (len(raw_blk) % 2)
            if usable < 2:
                return
            arr = array.array("h")
            arr.frombytes(raw_blk[:usable])
            if sys.byteorder == "big":
                arr.byteswap()
            pk_val = max(abs(min(arr)), abs(max(arr))) * inv_scale
            if pk_val > max_peak:
                max_peak = pk_val
            sum_sq = sum(map(mul, arr, arr))
            block_ms.append((sum_sq * inv_sq_scale) / float(len(arr)))

        async def _stream_pcm() -> None:
            nonlocal total_read
            assert proc is not None and proc.stdout is not None
            while total_read < hard_cap_bytes:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                total_read += len(chunk)
                buf.extend(chunk)
                while len(buf) >= _NORM_BLOCK_BYTES:
                    blk = bytes(buf[:_NORM_BLOCK_BYTES])
                    del buf[:_NORM_BLOCK_BYTES]
                    _consume_block(blk)
            if total_read >= hard_cap_bytes and getattr(proc, "returncode", None) is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            await proc.wait()

        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin,
                "-hide_banner",
                "-nostats",
                "-v",
                "error",
                "-i",
                filepath,
                "-t",
                str(_NORM_MAX_ANALYZE_SEC),
                "-vn",
                "-af",
                "highpass=f=60",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(_NORM_SAMPLE_RATE),
                "-ac",
                "1",
                "pipe:1",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(_stream_pcm(), timeout=25.0)
            rem = len(buf) - (len(buf) % 2)
            if rem >= min_tail_bytes:
                _consume_block(bytes(buf[:rem]))
            buf.clear()

            if total_read < (_NORM_SAMPLE_RATE * 2) // 2 or not block_ms:
                return None

            abs_gate_ms = 10.0 ** (-45.0 / 10.0)
            gated1 = [m for m in block_ms if m >= abs_gate_ms]
            if not gated1:
                gated1 = [m for m in block_ms if m > 1e-8]
            if not gated1:
                return None
            avg1 = sum(gated1) / float(len(gated1))
            rel_gate = avg1 * 0.1
            gated2 = [m for m in gated1 if m >= rel_gate]
            if not gated2:
                gated2 = gated1
            avg2 = sum(gated2) / float(len(gated2))
            rms_db = round(10.0 * math.log10(max(1e-12, avg2)), 2)
            peak_db = round(20.0 * math.log10(max(1e-6, max_peak)), 2)
            if not (-60.0 <= rms_db <= 6.0):
                return None
            return {"rms_db": rms_db, "peak_db": peak_db}
        except Exception as e:
            logger.debug(f"[PC Music Bridge] Bỏ qua đo loudness ({e})")
            if proc is not None and getattr(proc, "returncode", None) is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return None

    async def _run_yt_dlp(
        self,
        *,
        target: str,
        source_kind: str,
        max_filesize_mb: int,
        timeout_sec: float,
        use_cookies: Optional[bool] = None,
    ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        job_dir = os.path.join(self.cache_dir, f"job_{secrets.token_hex(6)}")
        os.makedirs(job_dir, exist_ok=True)
        out_tmpl = os.path.join(job_dir, "%(id)s.%(ext)s")
        is_sc = (source_kind == "soundcloud")

        cmd = find_yt_dlp_cmd() + ["--no-playlist", "--no-update"]
        if not is_sc:
            js_rt = detect_js_runtime()
            if js_rt:
                cmd.extend(["--js-runtimes", js_rt, "--remote-components", "ejs:github"])
            cfile = self._effective_cookies_path() if (use_cookies is None or use_cookies) else None
            if cfile:
                cmd.extend(["--cookies", cfile])
            else:
                cmd.extend(["--extractor-args", "youtube:player_client=visionos,default"])
            fmt_sel = (
                f"140[filesize<={max_filesize_mb}M]/140[filesize_approx<={max_filesize_mb}M]"
                f"/139/ba[ext=m4a][filesize<={max_filesize_mb}M]/ba[acodec^=mp4a][filesize<={max_filesize_mb}M]"
                f"/ba[filesize<={max_filesize_mb}M]/140/ba"
            )
        else:
            fmt_sel = "ba[ext=mp3]/hls_aac_160k/hls_aac_96k/ba[ext=m4a]/ba"

        cmd.extend([
            "-f", fmt_sel,
            "--max-filesize", f"{max_filesize_mb}M",
            "--print-json",
            "-o", out_tmpl,
            target,
        ])

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            if proc is not None and getattr(proc, "returncode", None) is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            shutil.rmtree(job_dir, ignore_errors=True)
            return None, "timeout", f"Tải {source_kind} quá thời gian ({int(timeout_sec)}s)."
        except Exception as e:
            shutil.rmtree(job_dir, ignore_errors=True)
            return None, "exec_error", f"Lỗi chạy yt-dlp: {e}"

        stdout_s = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr_s = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            code, msg = classify_yt_dlp_error(stdout_s, stderr_s, proc.returncode)
            shutil.rmtree(job_dir, ignore_errors=True)
            return None, code, msg

        info: Dict[str, Any] = {}
        for line in reversed(stdout_s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict) and parsed.get("id"):
                        info = parsed
                        break
                except Exception:
                    continue

        vid = re.sub(r"[^A-Za-z0-9._-]", "_", str(info.get("id") or "").strip())
        if not vid:
            code, msg = classify_yt_dlp_error(stdout_s, stderr_s, 0)
            shutil.rmtree(job_dir, ignore_errors=True)
            return None, code, msg

        chosen_path = None
        for req in (info.get("requested_downloads") or []):
            fp = req.get("filepath") if isinstance(req, dict) else None
            if fp and os.path.isfile(fp):
                chosen_path = fp
                break
        if not chosen_path:
            matches = [
                p for p in glob.glob(os.path.join(job_dir, "*"))
                if os.path.isfile(p) and not p.endswith(".part") and not p.endswith(".json")
            ]
            if matches:
                chosen_path = matches[0]

        if not chosen_path or not os.path.isfile(chosen_path) or os.path.getsize(chosen_path) <= 0:
            code, msg = classify_yt_dlp_error(stdout_s, stderr_s, 0)
            if code == "empty_file" and int(info.get("filesize") or info.get("filesize_approx") or 0) > max_filesize_mb * 1024 * 1024:
                code = "too_large"
                msg = f"Bài '{info.get('title') or vid}' vượt quá giới hạn {max_filesize_mb} MB."
            shutil.rmtree(job_dir, ignore_errors=True)
            return None, code, msg

        if await self._remux_mp4_to_adts(chosen_path):
            logger.info(f"[PC Music Bridge] Đã giải bọc MP4 -> ADTS AAC trước khi truyền: {os.path.basename(chosen_path)}")

        loudness = await self._analyze_loudness(chosen_path)

        ext = os.path.splitext(chosen_path)[1].lstrip(".").lower() or str(info.get("ext") or "m4a").lower()
        size_bytes = os.path.getsize(chosen_path)
        token = secrets.token_urlsafe(18)
        self.tokens[token] = {
            "filepath": chosen_path,
            "job_dir": job_dir,
            "created_at": time.time(),
            "filename": f"{vid}.{ext}",
        }
        track = {
            "video_id": vid,
            "source": source_kind,
            "title": str(info.get("title") or vid)[:180],
            "uploader": str(info.get("uploader") or info.get("channel") or "")[:120],
            "duration": int(info.get("duration") or 0),
            "ext": ext,
            "size_bytes": size_bytes,
        }
        if loudness:
            track["norm_rms_db"] = loudness["rms_db"]
            if "peak_db" in loudness:
                track["norm_peak_db"] = loudness["peak_db"]
            track["norm_ver"] = _TRACK_NORM_VERSION
        return {"token": token, "track": track}, "", ""

    async def _download_yt_with_cookieless_retry(
        self,
        *,
        target: str,
        max_filesize_mb: int,
        timeout_sec: float,
    ) -> Tuple[Optional[Dict[str, Any]], str, str]:
        has_cookies = bool(self._effective_cookies_path())
        res, code, msg = await self._run_yt_dlp(
            target=target,
            source_kind="youtube",
            max_filesize_mb=max_filesize_mb,
            timeout_sec=timeout_sec,
            use_cookies=True if has_cookies else False,
        )
        if res:
            return res, "", ""
        if has_cookies and code in ("js_challenge", "unavailable", "yt_dlp_error", "bot_check"):
            retry_res, retry_code, retry_msg = await self._run_yt_dlp(
                target=target,
                source_kind="youtube",
                max_filesize_mb=max_filesize_mb,
                timeout_sec=timeout_sec,
                use_cookies=False,
            )
            if retry_res:
                return retry_res, "", ""
            if code != "js_challenge" or retry_code in ("too_large",):
                code, msg = retry_code, retry_msg
        return None, code, msg

    async def process_request(
        self,
        query_or_url: str,
        max_filesize_mb: int = 150,
        timeout_sec: float = 180.0,
    ) -> Tuple[Optional[Dict[str, Any]], str, str, str]:
        raw = str(query_or_url or "").strip()
        if not raw:
            return None, "invalid_query", "Thiếu tên bài hát hoặc link.", ""

        limit_mb = max(10, min(300, int(max_filesize_mb or 150)))
        t_limit = max(30.0, min(300.0, float(timeout_sec or 180.0)))

        async with self._get_semaphore():
            self.active_jobs += 1
            try:
                sc_url = extract_soundcloud_url(raw)
                if sc_url:
                    res, code, msg = await self._run_yt_dlp(
                        target=sc_url,
                        source_kind="soundcloud",
                        max_filesize_mb=limit_mb,
                        timeout_sec=t_limit,
                    )
                    return res, code, msg, ""

                yt_url = normalize_youtube_single_url(raw)
                if yt_url:
                    res, code, msg = await self._download_yt_with_cookieless_retry(
                        target=yt_url,
                        max_filesize_mb=limit_mb,
                        timeout_sec=t_limit,
                    )
                    if res:
                        return res, "", "", ""
                    if code in ("bot_check", "unavailable", "js_challenge"):
                        loop = asyncio.get_running_loop()
                        meta = await loop.run_in_executor(None, _fetch_oembed_sync, yt_url)
                        if meta and meta.get("title"):
                            for sc_q in build_soundcloud_fallback_queries(meta["title"]):
                                sc_res, sc_code, _sc_msg = await self._run_yt_dlp(
                                    target=f"scsearch1:{sc_q}",
                                    source_kind="soundcloud",
                                    max_filesize_mb=limit_mb,
                                    timeout_sec=t_limit,
                                )
                                if sc_res:
                                    note = f"YouTube fallback -> đã tải bản '{sc_q}' từ SoundCloud qua PC."
                                    return sc_res, "", "", note
                                if sc_code == "timeout":
                                    break
                    return None, code, msg, ""

                res, code, msg = await self._download_yt_with_cookieless_retry(
                    target=f"ytsearch1:{raw}",
                    max_filesize_mb=limit_mb,
                    timeout_sec=t_limit,
                )
                if res:
                    return res, "", "", ""
                if code != "too_large":
                    sc_res, _sc_code, _sc_msg = await self._run_yt_dlp(
                        target=f"scsearch1:{raw}",
                        source_kind="soundcloud",
                        max_filesize_mb=limit_mb,
                        timeout_sec=t_limit,
                    )
                    if sc_res:
                        return sc_res, "", "", f"Đã tự động lấy từ SoundCloud cho '{raw}' qua PC."
                return None, code, msg, ""
            finally:
                self.active_jobs = max(0, self.active_jobs - 1)

    def remove_token(self, token: str) -> bool:
        entry = self.tokens.pop(token, None)
        if not entry:
            return False
        job_dir = entry.get("job_dir")
        if job_dir and os.path.isdir(job_dir):
            shutil.rmtree(job_dir, ignore_errors=True)
        return True

    def cleanup_expired(self) -> int:
        now = time.time()
        expired = [
            tok for tok, meta in list(self.tokens.items())
            if (now - float(meta.get("created_at") or 0.0)) >= self.file_ttl_sec
        ]
        for tok in expired:
            self.remove_token(tok)
        active_dirs = {
            os.path.abspath(str(m.get("job_dir") or ""))
            for m in self.tokens.values()
            if m.get("job_dir")
        }
        try:
            for name in os.listdir(self.cache_dir):
                if not name.startswith("job_"):
                    continue
                full = os.path.abspath(os.path.join(self.cache_dir, name))
                if full in active_dirs or not os.path.isdir(full):
                    continue
                try:
                    if (now - os.path.getmtime(full)) >= max(600.0, self.file_ttl_sec):
                        shutil.rmtree(full, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            pass
        return len(expired)


def _is_authorized(request: web.Request, api_key: str) -> bool:
    if not api_key:
        return True
    supplied = str(request.headers.get("X-API-Key") or "").strip()
    if not supplied:
        auth = str(request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, api_key)


_WASAPI_LOOPBACK_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

public class DsWasapiLoopback {
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    private class MMDeviceEnumeratorComObject { }

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator {
        int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IntPtr ppDevices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
    }

    [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice {
        int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    }

    [Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioClient {
        int Initialize(int ShareMode, int StreamFlags, long hnsBufferDuration, long hnsPeriodicity, IntPtr pFormat, IntPtr AudioSessionGuid);
        int GetBufferSize(out uint pNumBufferFrames);
        int GetStreamLatency(out long phnsLatency);
        int GetCurrentPadding(out uint pNumPaddingFrames);
        int IsFormatSupported(int ShareMode, IntPtr pFormat, out IntPtr ppClosestMatch);
        int GetMixFormat(out IntPtr ppDeviceFormat);
        int GetDevicePeriod(out long phnsDefaultDevicePeriod, out long phnsMinimumDevicePeriod);
        int Start();
        int Stop();
        int Reset();
        int SetEventHandle(IntPtr eventHandle);
        int GetService(ref Guid riid, [MarshalAs(UnmanagedType.IUnknown)] out object ppv);
    }

    [Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioCaptureClient {
        int GetBuffer(out IntPtr ppData, out uint pNumFramesToRead, out uint pdwFlags, out ulong pu64DevicePosition, out ulong pu64QPCPosition);
        int ReleaseBuffer(uint NumFramesRead);
        int GetNextPacketSize(out uint pNumFramesInNextPacket);
    }

    public static void StreamLoopback24kMono() {
        var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
        IMMDevice dev;
        Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 0, out dev));
        Guid iidClient = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
        object objClient;
        Marshal.ThrowExceptionForHR(dev.Activate(ref iidClient, 23, IntPtr.Zero, out objClient));
        var client = (IAudioClient)objClient;

        IntPtr fmtPtr;
        Marshal.ThrowExceptionForHR(client.GetMixFormat(out fmtPtr));
        ushort tag = (ushort)Marshal.ReadInt16(fmtPtr, 0);
        int channels = Math.Max(1, (int)Marshal.ReadInt16(fmtPtr, 2));
        int sr = Math.Max(8000, Marshal.ReadInt32(fmtPtr, 4));
        int blockAlign = Math.Max(2, (int)Marshal.ReadInt16(fmtPtr, 12));
        int bits = (int)Marshal.ReadInt16(fmtPtr, 14);
        int subTag = (tag == 0xFFFE) ? Marshal.ReadInt32(fmtPtr, 24) : (int)tag;
        bool isFloat = (subTag == 3 && bits == 32);

        Marshal.ThrowExceptionForHR(client.Initialize(0, 0x00020000, 1000000, 0, fmtPtr, IntPtr.Zero));
        Guid iidCap = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
        object objCap;
        Marshal.ThrowExceptionForHR(client.GetService(ref iidCap, out objCap));
        var cap = (IAudioCaptureClient)objCap;

        Marshal.ThrowExceptionForHR(client.Start());
        Stream stdout = Console.OpenStandardOutput();
        byte[] raw = new byte[65536];
        byte[] outFrame = new byte[960];
        byte[] zeroFrame = new byte[960];
        int outPos = 0;
        double step = (double)sr / 24000.0;
        double phase = 0.0;
        float prevSample = 0f;
        long lastEmitMs = Environment.TickCount;

        try {
            while (true) {
                uint nextPkt = 0;
                Marshal.ThrowExceptionForHR(cap.GetNextPacketSize(out nextPkt));
                bool gotAudio = false;
                while (nextPkt > 0) {
                    IntPtr dataPtr;
                    uint frames = 0;
                    uint flags = 0;
                    ulong devPos, qpcPos;
                    Marshal.ThrowExceptionForHR(cap.GetBuffer(out dataPtr, out frames, out flags, out devPos, out qpcPos));
                    try {
                        int nFrames = (int)frames;
                        if (nFrames > 0) {
                            gotAudio = true;
                            int byteCount = nFrames * blockAlign;
                            if (byteCount > raw.Length) raw = new byte[byteCount];
                            bool silentFlag = (flags & 0x2U) != 0U;
                            if (!silentFlag) {
                                Marshal.Copy(dataPtr, raw, 0, byteCount);
                            } else {
                                Array.Clear(raw, 0, byteCount);
                            }

                            for (int f = 0; f < nFrames; f++) {
                                float mono = 0f;
                                if (!silentFlag) {
                                    int baseOff = f * blockAlign;
                                    if (isFloat) {
                                        for (int c = 0; c < channels; c++) {
                                            mono += BitConverter.ToSingle(raw, baseOff + c * 4);
                                        }
                                        mono /= (float)channels;
                                    } else if (bits == 16) {
                                        for (int c = 0; c < channels; c++) {
                                            mono += (float)BitConverter.ToInt16(raw, baseOff + c * 2) / 32768f;
                                        }
                                        mono /= (float)channels;
                                    }
                                }
                                phase += 1.0;
                                while (phase >= step) {
                                    phase -= step;
                                    float frac = (float)Math.Max(0.0, Math.Min(1.0, 1.0 - phase));
                                    float interp = prevSample + (mono - prevSample) * frac;
                                    int s16 = (int)Math.Round(interp * 32767.0f);
                                    if (s16 > 32767) s16 = 32767;
                                    else if (s16 < -32768) s16 = -32768;
                                    outFrame[outPos++] = (byte)(s16 & 0xFF);
                                    outFrame[outPos++] = (byte)((s16 >> 8) & 0xFF);
                                    if (outPos >= 960) {
                                        stdout.Write(outFrame, 0, 960);
                                        stdout.Flush();
                                        outPos = 0;
                                        lastEmitMs = Environment.TickCount;
                                    }
                                }
                                prevSample = mono;
                            }
                        }
                    } finally {
                        cap.ReleaseBuffer(frames);
                    }
                    Marshal.ThrowExceptionForHR(cap.GetNextPacketSize(out nextPkt));
                }
                if (!gotAudio) {
                    int nowMs = Environment.TickCount;
                    if (unchecked(nowMs - (int)lastEmitMs) >= 25) {
                        if (outPos > 0) {
                            Array.Clear(outFrame, outPos, 960 - outPos);
                            stdout.Write(outFrame, 0, 960);
                            outPos = 0;
                        } else {
                            stdout.Write(zeroFrame, 0, 960);
                        }
                        stdout.Flush();
                        lastEmitMs = nowMs;
                    }
                    Thread.Sleep(6);
                }
            }
        } catch (IOException) {
        } finally {
            try { client.Stop(); } catch { }
            Marshal.FreeCoTaskMem(fmtPtr);
        }
    }
}
"@
[DsWasapiLoopback]::StreamLoopback24kMono()
""".strip()


def create_app(worker: MusicDownloadWorker, api_key: str = "") -> web.Application:
    app = web.Application()

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if not _is_authorized(request, api_key):
            return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "service": "music_dl_bridge",
            "js_runtime": detect_js_runtime() or "",
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "active_jobs": worker.active_jobs,
            "cached_tokens": len(worker.tokens),
        })

    async def handle_download(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error_code": "bad_json", "error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error_code": "bad_json", "error": "JSON object required"}, status=400)

        query = str(body.get("query_or_url") or "").strip()
        if not query:
            return web.json_response({"ok": False, "error_code": "invalid_query", "error": "Missing query_or_url"}, status=400)
        try:
            raw_limit = body.get("max_filesize_mb")
            raw_timeout = body.get("timeout_sec")
            limit_mb = int(150 if raw_limit is None or raw_limit == "" else raw_limit)
            timeout_sec = float(180.0 if raw_timeout is None or raw_timeout == "" else raw_timeout)
        except (TypeError, ValueError):
            return web.json_response(
                {"ok": False, "error_code": "invalid_params", "error": "max_filesize_mb and timeout_sec must be numeric"},
                status=400,
            )

        logger.info(f"[PC Music Bridge] Nhận yêu cầu tải: {query} (limit={limit_mb}MB)")
        t0 = time.time()
        res, code, msg, note = await worker.process_request(query, max_filesize_mb=limit_mb, timeout_sec=timeout_sec)
        dt = time.time() - t0
        if not res:
            logger.warning(f"[PC Music Bridge] Thất bại ({dt:.1f}s) [{code}]: {msg}")
            return web.json_response({"ok": False, "error_code": code, "error": msg}, status=422)

        tok = res["token"]
        tr = res["track"]
        logger.info(f"[PC Music Bridge] Xong ({dt:.1f}s): {tr['title']} ({tr['size_bytes']/1024/1024:.2f} MB)")
        return web.json_response({
            "ok": True,
            "token": tok,
            "download_path": f"/files/{tok}",
            "track": tr,
            "fallback_note": note,
        })

    async def handle_file_get(request: web.Request) -> web.StreamResponse:
        token = str(request.match_info.get("token") or "").strip()
        if not _TOKEN_RE.match(token):
            return web.json_response({"ok": False, "error": "Invalid token"}, status=400)
        entry = worker.tokens.get(token)
        if not entry or not os.path.isfile(entry.get("filepath", "")):
            return web.json_response({"ok": False, "error": "File expired or not found"}, status=404)
        return web.FileResponse(entry["filepath"])

    async def handle_file_delete(request: web.Request) -> web.Response:
        token = str(request.match_info.get("token") or "").strip()
        if not _TOKEN_RE.match(token):
            return web.json_response({"ok": False, "error": "Invalid token"}, status=400)
        removed = worker.remove_token(token)
        return web.json_response({"ok": True, "removed": removed})

    async def handle_live_audio_ws(request: web.Request) -> web.StreamResponse:
        if os.name != "nt":
            return web.json_response({"ok": False, "error": "WASAPI Loopback chi ho tro tren Windows."}, status=501)
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        proc = await _spawn_wasapi_loopback_proc()

        async def _pump_stdout() -> None:
            assert proc.stdout is not None
            while not ws.closed:
                try:
                    chunk = await proc.stdout.readexactly(1920)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial and not ws.closed:
                        await ws.send_bytes(exc.partial)
                    break
                if not chunk or ws.closed:
                    break
                await ws.send_bytes(chunk)

        async def _read_ws() -> None:
            async for msg in ws:
                if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break

        pump_task = asyncio.create_task(_pump_stdout())
        ws_task = asyncio.create_task(_read_ws())
        try:
            await asyncio.wait({pump_task, ws_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (pump_task, ws_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(pump_task, ws_task, return_exceptions=True)
            await _stop_proc(proc)
            if not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    pass
        return ws

    cleanup_task = None

    async def start_cleanup(app_obj: web.Application):
        nonlocal cleanup_task

        async def _cleanup_loop():
            while True:
                await asyncio.sleep(60.0)
                try:
                    n = worker.cleanup_expired()
                    if n:
                        logger.debug(f"[PC Music Bridge] Dọn {n} file tạm hết hạn.")
                except Exception:
                    pass

        cleanup_task = asyncio.create_task(_cleanup_loop())

    async def stop_cleanup(app_obj: web.Application):
        nonlocal cleanup_task
        task = cleanup_task
        cleanup_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for tok in list(worker.tokens.keys()):
            worker.remove_token(tok)

    app.on_startup.append(start_cleanup)
    app.on_cleanup.append(stop_cleanup)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/download", handle_download)
    app.router.add_get("/files/{token}", handle_file_get)
    app.router.add_delete("/files/{token}", handle_file_delete)
    app.router.add_get("/live_audio/ws", handle_live_audio_ws)
    return app


async def _spawn_wasapi_loopback_proc() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _WASAPI_LOOPBACK_PS_SCRIPT,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def _stop_proc(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass


def _load_env_fallback() -> Dict[str, str]:
    vals: Dict[str, str] = {}
    here = os.path.dirname(os.path.abspath(__file__))
    for env_file in (os.path.join(here, ".env"), os.path.join(os.path.dirname(here), ".env")):
        if not os.path.isfile(env_file):
            continue
        try:
            with open(env_file, "r", encoding="utf-8") as ef:
                for raw_ln in ef:
                    ln = raw_ln.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    ek, ev = ln.split("=", 1)
                    k = ek.strip()
                    v = ev.strip().strip("\"'")
                    if k and v and k not in vals:
                        vals[k] = v
        except OSError:
            pass
    return vals


async def run_push_live_speaker(openstick_url: str, pin: str = "", label: str = "") -> None:
    """Chế độ đẩy trực tiếp WASAPI Loopback từ PC hiện tại lên OpenStick (không cần mở cổng firewall inbound)."""
    if os.name != "nt":
        raise RuntimeError("--push-live chỉ hỗ trợ trên Windows.")
    import aiohttp
    import platform

    base = str(openstick_url or "http://192.168.1.100:8080").strip().rstrip("/")
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = "ws://" + base
    lbl = label.strip() or f"💻 PC {platform.node() or 'Windows'}"
    qs = urllib.parse.urlencode({"source": "pc_loopback", "label": lbl})
    ws_url = f"{ws_base}/api/jukebox/live_ws?{qs}"
    headers = {"X-Dashboard-Pin": pin} if pin else {}

    proc: Optional[asyncio.subprocess.Process] = None
    timeout = aiohttp.ClientTimeout(total=None, connect=5.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(ws_url, headers=headers, heartbeat=15.0) as ws:
            logger.info(f"🔊 [Push Live Speaker] Đã kết nối {ws_url} ({lbl}) — nhấn Ctrl+C để dừng.")
            proc = await _spawn_wasapi_loopback_proc()

            async def _pump() -> None:
                assert proc is not None and proc.stdout is not None
                while not ws.closed:
                    try:
                        chunk = await proc.stdout.readexactly(1920)
                    except asyncio.IncompleteReadError as exc:
                        if exc.partial and not ws.closed:
                            await ws.send_bytes(exc.partial)
                        break
                    if not chunk or ws.closed:
                        break
                    await ws.send_bytes(chunk)

            async def _listen() -> None:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            obj = json.loads(msg.data)
                        except Exception:
                            obj = {}
                        if obj.get("type") == "live_stopped":
                            logger.info(f"⏹️ [Push Live Speaker] Đã dừng từ OpenStick ({obj.get('reason')}).")
                            break
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            pump_task = asyncio.create_task(_pump())
            listen_task = asyncio.create_task(_listen())
            try:
                await asyncio.wait({pump_task, listen_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (pump_task, listen_task):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(pump_task, listen_task, return_exceptions=True)
                await _stop_proc(proc)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description="PC LAN Music Download Bridge cho OpenStick")
    parser.add_argument("--host", default=os.getenv("MUSIC_DL_HOST", "0.0.0.0"), help="Bind host (mặc định 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("MUSIC_DL_PORT", "8091")), help="Bind port (mặc định 8091)")
    parser.add_argument(
        "--api-key",
        default="",
        help="API key xác thực header X-API-Key (mặc định đọc MUSIC_DL_API_KEY / TTS_API_KEY hoặc .env)",
    )
    parser.add_argument(
        "--cache-dir",
        default=os.path.join(tempfile.gettempdir(), "smart_butler_music_dl_bridge"),
        help="Thư mục tạm chứa file trước khi OpenStick kéo về",
    )
    parser.add_argument("--cookies", default="", help="Đường dẫn youtube_cookies.txt tùy chọn trên PC")
    parser.add_argument("--ttl", type=float, default=_DEFAULT_FILE_TTL_SEC, help="Thời gian giữ file tạm (giây)")
    parser.add_argument("--push-live", action="store_true", help="Đẩy trực tiếp âm thanh PC lên loa OpenStick qua WebSocket")
    parser.add_argument("--openstick-url", default="", help="URL OpenStick khi dùng --push-live")
    parser.add_argument("--pin", default="", help="PIN Dashboard khi dùng --push-live")
    parser.add_argument("--label", default="", help="Tên hiển thị thiết bị khi dùng --push-live")
    args = parser.parse_args()

    env_fallback = _load_env_fallback()
    if args.push_live:
        openstick_url = (args.openstick_url or os.getenv("OPENSTICK_URL") or env_fallback.get("OPENSTICK_URL") or "http://192.168.1.100:8080").strip()
        pin = (args.pin or os.getenv("DASHBOARD_PIN") or env_fallback.get("DASHBOARD_PIN") or "").strip()
        try:
            asyncio.run(run_push_live_speaker(openstick_url, pin=pin, label=args.label))
        except KeyboardInterrupt:
            pass
        return

    effective_key = (
        args.api_key
        or os.getenv("MUSIC_DL_API_KEY")
        or os.getenv("TTS_API_KEY")
        or env_fallback.get("MUSIC_DL_API_KEY")
        or env_fallback.get("TTS_API_KEY")
        or ""
    ).strip()

    worker = MusicDownloadWorker(
        cache_dir=args.cache_dir,
        cookies_path=args.cookies,
        file_ttl_sec=args.ttl,
    )
    app = create_app(worker, api_key=effective_key)
    logger.info(
        f"🎵 [PC Music Bridge] Lắng nghe http://{args.host}:{args.port} | "
        f"JS={detect_js_runtime() or 'none'} | auth={'on' if effective_key else 'off'}"
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
