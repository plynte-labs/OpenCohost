"""Global push-to-talk bridge for the OpenCohost API (Windows).

Polls a physical key system-wide (GetAsyncKeyState, the same technique as
CTK's ptt_manager) and drives the same /api/ptt/* endpoints as the Tauri
PTT card — so hold-to-talk works while gaming or focused on any other app.
The server-side keepalive watchdog still backstops this script dying mid-hold.

Usage:
    python ptt_f10_bridge.py           # F10 (default)
    python ptt_f10_bridge.py 0x78      # F9, or any virtual-key code

Optional env:
    OPENCOHOST_API_URL    explicit API base URL (default: probe 8770 then 8765)
    OPENCOHOST_API_TOKEN  operator token, sent as Authorization: Bearer
"""

import ctypes
import json
import os
import sys
import time
import urllib.error
import urllib.request

VK = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x79  # VK_F10
POLL_S = 0.03
KEEPALIVE_S = 1.0
GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState


def _post(base, path, body):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = os.environ.get("OPENCOHOST_API_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def find_api():
    explicit = os.environ.get("OPENCOHOST_API_URL")
    candidates = [explicit] if explicit else [
        "http://127.0.0.1:8770",  # normal spot when LiveAudio owns 8765
        "http://127.0.0.1:8765",
    ]
    for base in candidates:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2):
                return base
        except OSError:
            continue
    sys.exit("OpenCohost API not reachable on 8770/8765 — is the app running?")


def main():
    base = find_api()
    print(f"PTT bridge on {base} — hold VK 0x{VK:X} to talk (Ctrl+C to quit)")
    session_id = None
    last_keepalive = 0.0
    try:
        while True:
            held = bool(GetAsyncKeyState(VK) & 0x8000)
            now = time.monotonic()
            if held and session_id is None:
                status, body = _post(base, "/api/ptt/start", {})
                if status == 200:
                    session_id = body["session_id"]
                    last_keepalive = now
                    print("listening...")
                elif status == 503:
                    print("STT (LiveAudio) not available — is it running on 8765?")
                elif status == 409:
                    print("PTT slot busy (Tauri button holding?)")
                else:
                    print(f"start failed: {status} {body}")
                if status != 200:
                    while GetAsyncKeyState(VK) & 0x8000:  # wait for release
                        time.sleep(POLL_S)
            elif held and session_id and now - last_keepalive >= KEEPALIVE_S:
                status, _ = _post(base, "/api/ptt/keepalive", {"session_id": session_id})
                last_keepalive = now
                if status == 409:  # server guillotined the session
                    print("server cut the session — released")
                    session_id = None
            elif not held and session_id:
                _post(base, "/api/ptt/stop", {"session_id": session_id})
                session_id = None
                print("sent to Kira")
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        if session_id:
            _post(base, "/api/ptt/stop", {"session_id": session_id})
        print("\nbye")


if __name__ == "__main__":
    main()
