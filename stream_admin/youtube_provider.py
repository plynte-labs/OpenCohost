import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.parse
import uuid
import webbrowser
from typing import Optional

import requests

from .providers import BaseStreamProvider, ProviderAuthError, ProviderError, ProviderUnsupportedError, StreamMetadata


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/youtube/v3"
READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


class YouTubeProvider(BaseStreamProvider):
    name = "youtube"
    supports_write = True
    supports_chat_write = True
    supports_timeout_ban = True
    supports_slow_mode = False

    def __init__(self, config: dict, oauth_store, log_callback=None):
        super().__init__(config, oauth_store, log_callback)
        self.client_id = os.getenv("YOUTUBE_OAUTH_CLIENT_ID") or self.config.get("client_id", "")
        self.client_secret = os.getenv("YOUTUBE_OAUTH_CLIENT_SECRET") or self.config.get("client_secret", "")
        if isinstance(self.client_id, str) and self.client_id.startswith("${"):
            self.client_id = ""
        if isinstance(self.client_secret, str) and self.client_secret.startswith("${"):
            self.client_secret = ""
        self.timeout_seconds = int(self.config.get("oauth_timeout_seconds", 120))
        self._last_metadata: Optional[StreamMetadata] = None

    def authenticate(self, request_write_scopes: bool = False) -> dict:
        if not self.client_id or not self.client_secret:
            raise ProviderAuthError(
                "Configura YOUTUBE_OAUTH_CLIENT_ID/YOUTUBE_OAUTH_CLIENT_SECRET o config/stream_admin.yaml antes de conectar YouTube."
            )

        scopes = [READ_SCOPE]
        if request_write_scopes:
            scopes.append(WRITE_SCOPE)

        code_data = {}
        state = uuid.uuid4().hex
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")

        class OAuthHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler_self):
                parsed = urllib.parse.urlparse(handler_self.path)
                params = urllib.parse.parse_qs(parsed.query)
                code_data["code"] = params.get("code", [""])[0]
                code_data["state"] = params.get("state", [""])[0]
                code_data["error"] = params.get("error", [""])[0]
                body = b"VoiceAI RF4 OAuth completado. Puedes cerrar esta ventana."
                handler_self.send_response(200)
                handler_self.send_header("Content-Type", "text/plain; charset=utf-8")
                handler_self.send_header("Content-Length", str(len(body)))
                handler_self.end_headers()
                handler_self.wfile.write(body)

            def log_message(self, format, *args):
                return

        with socketserver.TCPServer(("127.0.0.1", 0), OAuthHandler) as server:
            port = server.server_address[1]
            redirect_uri = f"http://127.0.0.1:{port}/oauth2callback"
            params = {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
            auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
            self._log("Abriendo navegador para OAuth YouTube...")
            webbrowser.open(auth_url)

            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            thread.join(timeout=self.timeout_seconds)

        if code_data.get("error"):
            raise ProviderAuthError(f"OAuth YouTube rechazado: {code_data['error']}")
        if not code_data.get("code") or code_data.get("state") != state:
            raise ProviderAuthError("OAuth YouTube no completado o estado invalido")

        token = self._exchange_code(code_data["code"], redirect_uri, code_verifier)
        token["scopes"] = scopes
        self.oauth_store.save(self.name, token)
        self.connected = True
        self.account = self._fetch_account()
        return self.status()

    def read_metadata(self) -> StreamMetadata:
        live = self._find_owned_live_broadcast()
        if live:
            metadata = self._metadata_from_video(live.get("id", ""), broadcast=live)
        else:
            metadata = self._find_owned_live_video_from_search()

        if metadata is None:
            metadata = StreamMetadata(provider=self.name, status="no_active_live", title="No hay live activo detectado")

        self._last_metadata = metadata
        self.connected = True
        return metadata

    def _find_owned_live_broadcast(self) -> Optional[dict]:
        try:
            broadcast = self._request(
                "GET",
                f"{API_URL}/liveBroadcasts",
                params={"part": "snippet,status,contentDetails", "mine": "true", "maxResults": 10},
            )
        except ProviderError as exc:
            self._log(f"liveBroadcasts no disponible; usando fallback search.list ({exc})")
            return None

        items = broadcast.get("items", [])
        if not items:
            return None

        for item in items:
            status = item.get("status", {}).get("lifeCycleStatus", "").lower()
            if status == "live":
                return item
        for item in items:
            status = item.get("status", {}).get("lifeCycleStatus", "").lower()
            if status in ("ready", "testing", "created"):
                return item
        return items[0]

    def _find_owned_live_video_from_search(self) -> Optional[StreamMetadata]:
        try:
            search = self._request(
                "GET",
                f"{API_URL}/search",
                params={"part": "snippet", "forMine": "true", "type": "video", "eventType": "live", "maxResults": 1},
            )
        except ProviderError as exc:
            self._log(f"search.list fallback no encontro live o fallo: {exc}")
            return None

        items = search.get("items", [])
        if not items:
            return None
        video_id = items[0].get("id", {}).get("videoId", "")
        if not video_id:
            return None
        return self._metadata_from_video(video_id, broadcast=None, fallback_snippet=items[0].get("snippet", {}))

    def _metadata_from_video(self, video_id: str, broadcast: Optional[dict] = None, fallback_snippet: Optional[dict] = None) -> StreamMetadata:
        if not video_id:
            return StreamMetadata(provider=self.name, status="no_video_id", title="No se pudo identificar el video del live")

        video = self._request(
            "GET",
            f"{API_URL}/videos",
            params={"part": "snippet,liveStreamingDetails,status", "id": video_id},
        )
        video_items = video.get("items", [])
        video_item = video_items[0] if video_items else {}
        video_snippet = video_item.get("snippet", {}) or fallback_snippet or {}
        live_details = video_item.get("liveStreamingDetails", {})
        broadcast_snippet = (broadcast or {}).get("snippet", {})
        live_chat_id = broadcast_snippet.get("liveChatId", "") or live_details.get("activeLiveChatId", "")
        status = (broadcast or {}).get("status", {}).get("lifeCycleStatus")
        if not status:
            status = "active" if live_details.get("actualStartTime") and not live_details.get("actualEndTime") else "video_found"

        return StreamMetadata(
            provider=self.name,
            status=status,
            title=video_snippet.get("title") or broadcast_snippet.get("title", ""),
            category_id=video_snippet.get("categoryId", ""),
            description=video_snippet.get("description") or broadcast_snippet.get("description", ""),
            tags=video_snippet.get("tags", []),
            video_id=video_id,
            live_chat_id=live_chat_id,
            raw={"broadcast": broadcast or {}, "video": video_item},
        )

    def update_metadata(self, metadata: dict) -> StreamMetadata:
        token = self._valid_token()
        scopes = set(token.get("scopes", []))
        if WRITE_SCOPE not in scopes:
            raise ProviderAuthError("Falta scope de escritura YouTube. Reconecta habilitando escritura.")

        current = self._last_metadata or self.read_metadata()
        if not current.video_id:
            raise ProviderError("No hay video/live activo para actualizar metadata")

        snippet = dict(current.raw.get("video", {}).get("snippet", {}))
        snippet.update({
            "title": metadata.get("title") or current.title,
            "description": metadata.get("description") if metadata.get("description") is not None else current.description,
            "categoryId": str(metadata.get("category_id") or current.category_id or "22"),
        })
        tags = metadata.get("tags")
        if tags is not None:
            snippet["tags"] = tags if isinstance(tags, list) else [t.strip() for t in str(tags).split(",") if t.strip()]

        payload = {"id": current.video_id, "snippet": snippet}
        self._request("PUT", f"{API_URL}/videos", params={"part": "snippet"}, json_body=payload)
        return self.read_metadata()

    def post_chat_message(self, message: str, live_chat_id: Optional[str] = None) -> dict:
        token = self._valid_token()
        scopes = set(token.get("scopes", []))
        if WRITE_SCOPE not in scopes:
            raise ProviderAuthError("Falta scope para escribir en chat. Reconecta habilitando escritura.")
        live_chat_id = live_chat_id or (self._last_metadata.live_chat_id if self._last_metadata else "")
        if not live_chat_id:
            raise ProviderError("No hay liveChatId activo para enviar mensaje")
        payload = {
            "snippet": {
                "liveChatId": live_chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": message},
            }
        }
        return self._request("POST", f"{API_URL}/liveChat/messages", params={"part": "snippet"}, json_body=payload)

    def list_live_chat_messages(self, live_chat_id: str, page_token: Optional[str] = None, max_results: int = 200) -> dict:
        if not live_chat_id:
            raise ProviderError("liveChatId es requerido para leer chat autenticado")
        params = {
            "part": "snippet,authorDetails",
            "liveChatId": live_chat_id,
            "maxResults": max(1, min(int(max_results), 2000)),
        }
        if page_token:
            params["pageToken"] = page_token
        data = self._request("GET", f"{API_URL}/liveChat/messages", params=params)
        messages = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            author = item.get("authorDetails", {})
            text = snippet.get("displayMessage") or snippet.get("textMessageDetails", {}).get("messageText", "")
            if not text:
                continue
            messages.append({
                "id": item.get("id", ""),
                "user": author.get("displayName", "YouTube"),
                "text": text,
                "timestamp": time.time(),
                "author_channel_id": author.get("channelId", ""),
                "is_moderator": author.get("isChatModerator", False),
                "is_owner": author.get("isChatOwner", False),
                "is_member": author.get("isChatSponsor", False),
                "source": "youtube_api",
            })
        return {
            "messages": messages,
            "next_page_token": data.get("nextPageToken", ""),
            "polling_interval_millis": data.get("pollingIntervalMillis", 5000),
        }

    def moderate_user(self, action: str, user_channel_id: str, reason: str = "", duration_seconds: int = 300, live_chat_id: Optional[str] = None) -> dict:
        token = self._valid_token()
        scopes = set(token.get("scopes", []))
        if WRITE_SCOPE not in scopes:
            raise ProviderAuthError("Falta scope para moderacion. Reconecta habilitando escritura.")
        live_chat_id = live_chat_id or (self._last_metadata.live_chat_id if self._last_metadata else "")
        if not live_chat_id:
            raise ProviderError("No hay liveChatId activo para moderar")
        # YouTube Data API liveChatBans.insert spec:
        # snippet.liveChatId, snippet.type, snippet.bannedUserDetails.channelId
        # snippet.banDurationSeconds only when type=temporary
        ban_type = "permanent" if action == "ban" else "temporary"
        snippet = {
            "liveChatId": live_chat_id,
            "type": ban_type,
            "bannedUserDetails": {"channelId": user_channel_id},
        }
        if ban_type == "temporary":
            snippet["banDurationSeconds"] = max(1, int(duration_seconds))
        payload = {"snippet": snippet}
        result = self._request("POST", f"{API_URL}/liveChat/bans", params={"part": "snippet"}, json_body=payload)
        if reason:
            self._log(f"Moderacion YouTube aplicada ({action}): {reason}")
        return result

    def set_slow_mode(self, enabled: bool, delay_seconds: int = 10, duration_minutes: int = 5) -> dict:
        raise ProviderUnsupportedError("YouTube Data API no expone Slow Mode para Live Chat en este MVP")

    def _exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> dict:
        resp = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            raise ProviderAuthError(f"No se pudo obtener token YouTube: HTTP {resp.status_code}")
        return resp.json()

    def _fetch_account(self) -> dict:
        data = self._request("GET", f"{API_URL}/channels", params={"part": "snippet", "mine": "true", "maxResults": 1})
        items = data.get("items", [])
        if not items:
            return {}
        item = items[0]
        snippet = item.get("snippet", {})
        return {"channel_id": item.get("id", ""), "title": snippet.get("title", "")}

    def _valid_token(self) -> dict:
        token = self.oauth_store.load(self.name)
        if not token:
            raise ProviderAuthError("YouTube no conectado")
        expires_at = float(token.get("expires_at", 0) or 0)
        if expires_at and expires_at <= time.time():
            token = self._refresh_token(token)
            self.oauth_store.save(self.name, token)
        return token

    def _refresh_token(self, token: dict) -> dict:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise ProviderAuthError("Token expirado sin refresh_token. Reconecta YouTube.")
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            raise ProviderAuthError(f"No se pudo refrescar token YouTube: HTTP {resp.status_code}")
        new_token = resp.json()
        new_token["refresh_token"] = refresh_token
        new_token["scopes"] = token.get("scopes", [READ_SCOPE])
        return new_token

    def _request(self, method: str, url: str, params=None, json_body=None) -> dict:
        token = self._valid_token()
        headers = {"Authorization": f"Bearer {token.get('access_token', '')}"}
        try:
            resp = requests.request(method, url, params=params, json=json_body, headers=headers, timeout=20)
        except requests.RequestException as exc:
            raise ProviderError(f"Error de red YouTube: {exc}") from exc
        if resp.status_code == 401:
            self.oauth_store.delete(self.name)
            raise ProviderAuthError("Token YouTube invalido o revocado. Reconecta la cuenta.")
        if resp.status_code == 403:
            raise ProviderAuthError("Permisos insuficientes o cuota YouTube agotada")
        if resp.status_code == 429:
            raise ProviderError("Rate-limit YouTube. Espera antes de reintentar.")
        if resp.status_code >= 400:
            raise ProviderError(f"YouTube API HTTP {resp.status_code}: {resp.text[:200]}")
        if not resp.text:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {}
