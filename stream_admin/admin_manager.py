import os
import json
import time
from typing import Callable, Optional

# Auto-revoke write mode after inactivity (seconds)
WRITE_TIMEOUT_SECONDS = 900  # 15 minutes

import yaml

from .analytics import AnalyticsTracker
from .moderation import ModerationEngine
from .oauth_store import OAuthStore, redact_token_text
from .providers import ProviderAuthError, ProviderError, ProviderUnsupportedError, StreamMetadata
from .twitch_provider import TwitchProvider
from .youtube_provider import YouTubeProvider


class AdminManager:
    def __init__(self, config_path: str = "config/stream_admin.yaml", llm_interface: Optional[Callable] = None):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isabs(config_path):
            config_path = os.path.join(base_dir, config_path)
        self.base_dir = base_dir
        self.config_path = config_path
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}

        token_path = self.config.get("oauth", {}).get("token_path", "data/stream_admin/oauth_tokens.json")
        if not os.path.isabs(token_path):
            token_path = os.path.join(base_dir, token_path)
        self.oauth_store = OAuthStore(token_path)
        self.llm_interface = llm_interface
        self.oauth_client_path = self._resolve_local_path(
            self.config.get("oauth", {}).get("client_config_path", "data/stream_admin/oauth_client.json")
        )
        self._merge_oauth_client_config()

        self.on_log: Optional[Callable[[str], None]] = None
        self.on_state: Optional[Callable[[dict], None]] = None
        self.on_metadata: Optional[Callable[[dict], None]] = None
        self.on_pending_action: Optional[Callable[[dict], None]] = None
        self.on_analytics: Optional[Callable[[dict], None]] = None

        self.providers = {
            "youtube": YouTubeProvider(self.config.get("youtube", {}), self.oauth_store, self._log),
            "twitch": TwitchProvider(self.config.get("twitch", {}), self.oauth_store, self._log),
        }
        self.active_provider_name = self.config.get("provider", {}).get("default", "youtube")
        self.metadata: StreamMetadata = StreamMetadata(provider=self.active_provider_name, status="not_loaded")
        self.pending_action: Optional[dict] = None
        self.analytics = AnalyticsTracker(self.config.get("analytics", {}))
        self.moderation = ModerationEngine(self.config.get("moderation", {}))
        self._last_vibe = None
        self._last_activity = None
        self._write_activated_at: float = 0.0

    @property
    def provider(self):
        return self.providers[self.active_provider_name]

    def set_llm_interface(self, llm_interface: Optional[Callable]):
        self.llm_interface = llm_interface

    def get_oauth_client_config(self) -> dict:
        client_id = self.config.get("youtube", {}).get("client_id", "")
        client_secret = self.config.get("youtube", {}).get("client_secret", "")
        return {
            "client_id": client_id if self._is_real_oauth_value(client_id) else "",
            "has_client_secret": self._is_real_oauth_value(client_secret),
            "client_config_path": self.oauth_client_path,
        }

    def save_oauth_client_config(self, client_id: str, client_secret: str):
        client_id = (client_id or "").strip()
        client_secret = (client_secret or "").strip()
        if not client_id or not client_secret:
            raise ProviderAuthError("Client ID y Client Secret son requeridos")
        os.makedirs(os.path.dirname(self.oauth_client_path), exist_ok=True)
        with open(self.oauth_client_path, "w", encoding="utf-8") as f:
            json.dump({"client_id": client_id, "client_secret": client_secret}, f, indent=2)
        try:
            os.chmod(self.oauth_client_path, 0o600)
        except Exception:
            pass
        self.config.setdefault("youtube", {})["client_id"] = client_id
        self.config.setdefault("youtube", {})["client_secret"] = client_secret
        youtube = self.providers.get("youtube")
        if youtube:
            youtube.client_id = client_id
            youtube.client_secret = client_secret
        self._log("Credenciales OAuth YouTube guardadas localmente")
        self._emit_state()

    def status(self) -> dict:
        status = self.provider.status()
        token = self.oauth_store.load(self.active_provider_name) or {}
        scopes = set(token.get("scopes", [])) if isinstance(token, dict) else set()
        write_scope_active = "https://www.googleapis.com/auth/youtube.force-ssl" in scopes
        status.update({
            "active_provider": self.active_provider_name,
            "mode": self.config.get("provider", {}).get("mode", "read_only"),
            "write_scope_active": write_scope_active,
            "write_enabled": self._write_enabled(),
            "metadata_status": self.metadata.status,
            "pending_action": self.pending_action,
            "twitch_placeholder": True,
            "oauth_client_configured": (
                self._is_real_oauth_value(self.config.get("youtube", {}).get("client_id"))
                and self._is_real_oauth_value(self.config.get("youtube", {}).get("client_secret"))
            ),
        })
        return status

    def authenticate(self, provider_name: str = "youtube", request_write_scopes: bool = False) -> dict:
        self.active_provider_name = provider_name
        if provider_name == "twitch":
            raise ProviderUnsupportedError("Twitch es placeholder en el MVP")
        status = self.provider.authenticate(request_write_scopes=request_write_scopes)
        self.config.setdefault("provider", {})["mode"] = "write" if request_write_scopes else "read_only"
        if request_write_scopes:
            self._write_activated_at = time.time()
        self._emit_state()
        self._log(f"{provider_name} conectado. Escritura={'si' if request_write_scopes else 'no'}")
        return status

    def disconnect(self):
        self.provider.disconnect()
        self._log(f"{self.active_provider_name} desconectado")
        self._emit_state()

    def refresh_metadata(self) -> dict:
        self.metadata = self.provider.read_metadata()
        data = self.metadata.to_dict()
        self._log(f"Metadata leida: {data.get('title', '') or data.get('status', '')}")
        if self.on_metadata:
            self.on_metadata(data)
        return data

    def suggest_metadata(self, context: str = "") -> dict:
        presets = self.config.get("metadata", {}).get("presets", [])
        categories = self.config.get("metadata", {}).get("categories", [])
        suggestion = self._fallback_suggestion(presets, categories)
        if self.llm_interface:
            try:
                prompt = self._build_suggestion_prompt(context, presets, categories)
                raw = self.llm_interface(prompt)
                parsed = self._parse_llm_suggestion(raw)
                if parsed:
                    suggestion.update(parsed)
            except Exception as exc:
                self._log(f"Sugerencia Kira uso fallback: {exc}")

        require_approval = self.config.get("metadata", {}).get("require_approval", True)
        auto_apply = self.config.get("metadata", {}).get("auto_apply_metadata", False)
        self.pending_action = {
            "type": "metadata_update",
            "provider": self.active_provider_name,
            "payload": suggestion,
            "requires_confirmation": require_approval and not auto_apply,
            "created_at": time.time(),
            "status": "pending",
        }
        self._log(f"Kira sugirio metadata: {suggestion.get('title', '')}")
        self._emit_pending()
        if auto_apply:
            self.apply_pending_action(force=True)
        return self.pending_action

    def apply_metadata(self, payload: dict) -> dict:
        self._require_write_enabled("aplicar metadata")
        result = self.provider.update_metadata(payload).to_dict()
        self.metadata = StreamMetadata(**{k: v for k, v in result.items() if k in StreamMetadata.__dataclass_fields__})
        self._log(f"Metadata aplicada manualmente: {payload.get('title', '')}")
        if self.on_metadata:
            self.on_metadata(result)
        return result

    def apply_pending_action(self, edited_payload: Optional[dict] = None, force: bool = False) -> dict:
        if not self.pending_action:
            raise ProviderError("No hay accion pendiente")
        action = self.pending_action
        if action.get("requires_confirmation") and not force:
            raise ProviderError("La accion requiere confirmacion explicita")
        payload = edited_payload or action.get("payload", {})
        if action.get("type") == "metadata_update":
            self._require_write_enabled("aplicar metadata")
            result = self.provider.update_metadata(payload).to_dict()
            self._log(f"Metadata aplicada: {payload.get('title', '')}")
        elif action.get("type") == "moderation":
            self._require_write_enabled("aplicar moderacion")
            result = self._apply_moderation_payload(payload)
        else:
            raise ProviderError(f"Tipo de accion no soportado: {action.get('type')}")
        action["status"] = "applied"
        self.pending_action = None
        self._emit_pending()
        return result

    def reject_pending_action(self):
        if self.pending_action:
            self._log(f"Accion rechazada: {self.pending_action.get('type')}")
        self.pending_action = None
        self._emit_pending()

    def send_chat_message(self, message: str) -> dict:
        if not self.config.get("chat", {}).get("allow_kira_chat_messages", False):
            raise ProviderError("Mensajes de Kira al chat desactivados en configuracion")
        self._require_write_enabled("enviar mensajes al chat")
        result = self.provider.post_chat_message(message)
        self._log(f"Kira envio mensaje al chat: {message}")
        return result

    def propose_high_risk_moderation(self, action: str, user_channel_id: str, reason: str, duration_seconds: int = 300) -> dict:
        proposal = self.moderation.high_risk_action(action, user_channel_id, reason, duration_seconds)
        self.pending_action = {
            "type": "moderation",
            "provider": self.active_provider_name,
            "payload": proposal,
            "requires_confirmation": proposal.get("requires_confirmation", True),
            "created_at": time.time(),
            "status": "pending",
        }
        self._log(f"Moderacion de alto riesgo propuesta: {action} {user_channel_id}")
        self._emit_pending()
        return self.pending_action

    def apply_high_risk_moderation(self, action: str, user_channel_id: str, reason: str, duration_seconds: int = 300) -> dict:
        self._require_write_enabled("aplicar moderacion")
        payload = self.moderation.high_risk_action(action, user_channel_id, reason, duration_seconds)
        result = self._apply_moderation_payload(payload)
        self._log(f"Moderacion confirmada aplicada: {action} {user_channel_id}")
        return result

    def ingest_rf3_event(self, event_type: str, payload: dict):
        self.analytics.ingest_rf3_event(event_type, payload)
        if event_type == "vibe":
            self._last_vibe = payload
        elif event_type == "activity":
            self._last_activity = payload
        if self.on_analytics:
            self.on_analytics(self.analytics.snapshot())
        self._evaluate_moderation()

    def analytics_context_if_due(self) -> Optional[str]:
        if self.analytics.should_inject_context():
            return self.analytics.context_text()
        return None

    def _evaluate_moderation(self):
        proposal = self.moderation.evaluate(self._last_vibe, self._last_activity)
        action = proposal.get("action")
        if action in ("none", "cooldown_skip"):
            return
        if action == "alert":
            self._log(f"Alerta moderacion: {proposal.get('reason')}")
            return
        payload = proposal
        pending = {
            "type": "moderation",
            "provider": self.active_provider_name,
            "payload": payload,
            "requires_confirmation": payload.get("requires_confirmation", True),
            "created_at": time.time(),
            "status": "pending",
        }
        if not pending["requires_confirmation"]:
            try:
                self._apply_moderation_payload(payload)
                self.moderation.mark_action()
                self._log(f"Moderacion automatica aplicada: {action}")
            except ProviderUnsupportedError as exc:
                self._log(f"Moderacion sugerida no soportada por proveedor: {exc}")
            except (ProviderError, ProviderAuthError) as exc:
                self._log(f"Moderacion automatica fallida: {exc}")
            return
        self.pending_action = pending
        self._log(f"Moderacion pendiente: {action} ({payload.get('reason')})")
        self._emit_pending()

    def _apply_moderation_payload(self, payload: dict) -> dict:
        self._require_write_enabled("aplicar moderacion")
        action = payload.get("action")
        if action == "slow_mode":
            result = self.provider.set_slow_mode(
                True,
                delay_seconds=payload.get("delay_seconds", 10),
                duration_minutes=payload.get("duration_minutes", 5),
            )
        elif action in ("timeout", "ban"):
            result = self.provider.moderate_user(
                action,
                user_channel_id=payload.get("user", ""),
                reason=payload.get("reason", ""),
                duration_seconds=payload.get("duration_seconds", 300),
            )
        else:
            raise ProviderUnsupportedError(f"Accion de moderacion no soportada: {action}")
        self.moderation.mark_action()
        if self.config.get("moderation", {}).get("announce_actions_to_chat", False):
            try:
                self.send_chat_message(self._moderation_announcement(payload))
            except Exception as exc:
                self._log(f"Anuncio al chat omitido: {exc}")
        return result

    def _moderation_announcement(self, payload: dict) -> str:
        action = payload.get("action")
        if action == "slow_mode":
            return "Vamos a bajar un poco el ritmo del chat para que todos puedan participar."
        if action == "timeout":
            return "Pausa breve para mantener el chat jugable."
        if action == "ban":
            return "Se aplico moderacion para cuidar el chat."
        return "Moderacion aplicada para cuidar el stream."

    def _fallback_suggestion(self, presets, categories) -> dict:
        preset = presets[0] if presets else {}
        category = categories[0] if categories else {}
        return {
            "title": preset.get("title", self.metadata.title or "Kira Live - Jugando con el chat"),
            "description": preset.get("description", self.metadata.description or "Stream con Kira como co-host virtual."),
            "category_id": str(category.get("id", self.metadata.category_id or "22")),
            "category_name": category.get("name", self.metadata.category_name or "People & Blogs"),
            "tags": preset.get("tags", self.metadata.tags or ["kira", "voiceai", "live"]),
        }

    def _build_suggestion_prompt(self, context, presets, categories) -> str:
        return (
            "Sugiere metadata para un stream de YouTube. Responde SOLO JSON con keys "
            "title, description, category_id, category_name, tags (array). "
            f"Metadata actual: {self.metadata.to_dict()}. "
            f"Presets del streamer: {presets}. Categorias permitidas/sugeridas: {categories}. "
            f"Contexto reciente: {context}"
        )

    def _parse_llm_suggestion(self, raw: str) -> Optional[dict]:
        import json
        if not raw:
            return None
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        allowed = {"title", "description", "category_id", "category_name", "tags"}
        return {k: v for k, v in data.items() if k in allowed}

    def _resolve_local_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(self.base_dir, path)

    def _merge_oauth_client_config(self):
        try:
            if not os.path.exists(self.oauth_client_path):
                return
            with open(self.oauth_client_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            youtube_cfg = self.config.setdefault("youtube", {})
            if data.get("client_id"):
                youtube_cfg["client_id"] = data["client_id"]
            if data.get("client_secret"):
                youtube_cfg["client_secret"] = data["client_secret"]
        except Exception as exc:
            self._log(f"No se pudo cargar OAuth client local: {exc}")

    def _is_real_oauth_value(self, value) -> bool:
        if not value or not isinstance(value, str):
            return False
        value = value.strip()
        return bool(value) and not (value.startswith("${") and value.endswith("}"))

    def _write_enabled(self) -> bool:
        if self.config.get("provider", {}).get("mode", "read_only") != "write":
            return False
        # Auto-revoke write mode after timeout
        if self._write_activated_at and (time.time() - self._write_activated_at) > WRITE_TIMEOUT_SECONDS:
            self._log(f"Modo escritura expirado por inactividad ({WRITE_TIMEOUT_SECONDS // 60} min). Revocado automaticamente.")
            self.revoke_write_mode()
            return False
        # Verify actual token scopes
        token = self.oauth_store.load(self.active_provider_name) or {}
        write_scope = "https://www.googleapis.com/auth/youtube.force-ssl"
        return write_scope in set(token.get("scopes", []))

    def revoke_write_mode(self):
        """Downgrade to read_only without disconnecting OAuth."""
        self.config.setdefault("provider", {})["mode"] = "read_only"
        self._write_activated_at = 0.0
        self._emit_state()
        self._log("Modo escritura revocado. Solo lectura activo.")

    def refresh_write_timeout(self):
        """Reset the write mode timeout after a successful write action."""
        if self._write_enabled():
            self._write_activated_at = time.time()

    def _require_write_enabled(self, action: str):
        if not self._write_enabled():
            raise ProviderAuthError(
                f"Modo solo lectura activo: no se puede {action}. Reconecta escritura y habilita modo write."
            )
        self.refresh_write_timeout()

    def _emit_state(self):
        if self.on_state:
            self.on_state(self.status())

    def _emit_pending(self):
        if self.on_pending_action:
            self.on_pending_action(self.pending_action or {})

    def _log(self, message: str):
        safe = redact_token_text(message)
        if self.on_log:
            self.on_log(f"[StreamAdmin] {safe}")
