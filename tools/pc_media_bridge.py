# -*- coding: utf-8 -*-
"""
Windows PC Listening Bridge cho Smart Butler LAN.
Doc trang thai nhac/video dang phat tren Windows qua WinRT (PowerShell built-in, 0 pip deps)
va chi day len OpenStick POST /api/lan/now_playing khi bai hat/trang thai doi hoac heartbeat 120s.

Cach dung:
  python tools/pc_media_bridge.py --host http://192.168.1.100:8080 --pin YOUR_PIN
  python tools/pc_media_bridge.py --once --dry-run
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

_PS_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
    $null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
    $null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
    $asTaskMethod = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod
    })[0]
    function AwaitWinRt($WinRtOp, $ResType) {
        $m = $asTaskMethod.MakeGenericMethod($ResType)
        $t = $m.Invoke($null, @($WinRtOp))
        $t.Wait(-1) | Out-Null
        return $t.Result
    }
    $mgr = AwaitWinRt ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    $sess = $mgr.GetCurrentSession()
    if ($null -eq $sess) {
        Write-Output '{}'
        exit 0
    }
    $pb = $sess.GetPlaybackInfo()
    $props = AwaitWinRt ($sess.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $out = [PSCustomObject]@{
        title = [string]$props.Title
        artist = [string]$props.Artist
        album = [string]$props.AlbumTitle
        app_name = [string]$sess.SourceAppUserModelId
        playback_status = [string]$pb.PlaybackStatus
    }
    Write-Output ($out | ConvertTo-Json -Compress)
} catch {
    Write-Output '{}'
}
""".strip()


def prettify_app_name(raw_app: str) -> str:
    low = str(raw_app or "").strip().lower()
    if not low:
        return ""
    if "spotify" in low:
        return "Spotify"
    if "chrome" in low:
        return "Chrome"
    if "msedge" in low or "edge" in low:
        return "Edge"
    if "applemusic" in low or "itunes" in low:
        return "Apple Music"
    if "vlc" in low:
        return "VLC"
    if "foobar" in low:
        return "foobar2000"
    if "firefox" in low:
        return "Firefox"
    if "coccoc" in low:
        return "Cốc Cốc"
    if "!" in low:
        return raw_app.split("!")[-1].strip()[:40]
    return raw_app.replace(".exe", "").replace(".EXE", "").strip()[:40]


def parse_winrt_media_json(raw: str) -> Dict[str, Any]:
    """Chuan hoa JSON tra ve tu PowerShell WinRT SessionManager."""
    s = str(raw or "").strip().lstrip("\ufeff")
    if not s:
        return {"title": "", "artist": "", "album": "", "app_name": "", "playback_status": "stopped"}
    try:
        obj = json.loads(s)
    except Exception:
        return {"title": "", "artist": "", "album": "", "app_name": "", "playback_status": "stopped"}
    if not isinstance(obj, dict):
        return {"title": "", "artist": "", "album": "", "app_name": "", "playback_status": "stopped"}

    title = str(obj.get("title") or "").strip()[:140]
    artist = str(obj.get("artist") or "").strip()[:100]
    album = str(obj.get("album") or "").strip()[:100]
    app_name = prettify_app_name(str(obj.get("app_name") or ""))
    status_raw = str(obj.get("playback_status") or "").strip().lower()
    if status_raw in ("playing", "4"):
        status = "playing"
    elif status_raw in ("paused", "5"):
        status = "paused"
    else:
        status = "stopped" if not title else status_raw
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "app_name": app_name,
        "playback_status": status,
    }


def query_windows_media(timeout_sec: float = 6.0) -> Dict[str, Any]:
    """Goi PowerShell doc trang thai media Windows hien tai."""
    if os.name != "nt":
        return {"title": "", "artist": "", "album": "", "app_name": "", "playback_status": "stopped"}
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return parse_winrt_media_json(proc.stdout)
    except Exception:
        return {"title": "", "artist": "", "album": "", "app_name": "", "playback_status": "stopped"}


def payload_signature(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(payload.get("title") or ""),
        str(payload.get("artist") or ""),
        str(payload.get("playback_status") or ""),
    )


def should_push_update(
    prev_sig: Optional[Tuple[str, str, str]],
    curr_payload: Dict[str, Any],
    last_push_ts: float,
    now_ts: float,
    heartbeat_sec: float = 120.0,
) -> Tuple[bool, str]:
    sig = payload_signature(curr_payload)
    if prev_sig is None or sig != prev_sig:
        return True, "changed"
    if curr_payload.get("playback_status") == "playing" and curr_payload.get("title"):
        if (now_ts - last_push_ts) >= heartbeat_sec:
            return True, "heartbeat"
    return False, "unchanged"


def push_now_playing(host_url: str, pin: str, payload: Dict[str, Any], timeout_sec: float = 4.0) -> Tuple[bool, str]:
    endpoint = host_url.rstrip("/") + "/api/lan/now_playing"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Dashboard-Pin": pin,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if 200 <= resp.status < 300:
                return True, f"HTTP {resp.status}"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main():
    parser = argparse.ArgumentParser(description="OpenStick LAN PC Media Bridge")
    parser.add_argument("--host", default=os.getenv("OPENSTICK_URL", "http://192.168.1.100:8080"), help="URL OpenStick Dashboard")
    parser.add_argument("--pin", default=os.getenv("DASHBOARD_PIN", ""), help="Ma PIN Dashboard")
    parser.add_argument("--interval", type=float, default=8.0, help="Chu ky kiem tra (giay)")
    parser.add_argument("--once", action="store_true", help="Chay 1 lan roi thoat")
    parser.add_argument("--dry-run", action="store_true", help="Chi in trang thai media, khong gui HTTP")
    args = parser.parse_args()

    if not args.dry_run and not args.pin:
        print("Thieu --pin (hoac bien moi truong DASHBOARD_PIN).", file=sys.stderr)
        sys.exit(2)

    prev_sig: Optional[Tuple[str, str, str]] = None
    last_push_ts = 0.0

    while True:
        now = time.time()
        payload = query_windows_media()
        if args.dry_run or args.once:
            print(json.dumps(payload, ensure_ascii=False))

        if not args.dry_run:
            do_push, reason = should_push_update(prev_sig, payload, last_push_ts, now)
            if do_push:
                ok, info = push_now_playing(args.host, args.pin, payload)
                if ok:
                    prev_sig = payload_signature(payload)
                    last_push_ts = now
                    if not args.once:
                        print(f"[{time.strftime('%H:%M:%S')}] Pushed ({reason}): {payload.get('playback_status')} - {payload.get('title')}")
                else:
                    print(f"[{time.strftime('%H:%M:%S')}] Push failed ({info})", file=sys.stderr)

        if args.once:
            break
        time.sleep(max(2.0, args.interval))


if __name__ == "__main__":
    main()
