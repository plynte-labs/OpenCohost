"""Default composition: real readers behind the injectable providers."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from opencohost.core.self_knowledge.providers import (
    CapabilitiesProvider,
    ClockProvider,
    CommandsProvider,
    CreatorProvider,
    GpuInfo,
    GpuProvider,
    IdentityCard,
    IdentityProvider,
    InterlocutorProvider,
    ModelProvider,
    RuntimeProvider,
)
from opencohost.core.self_knowledge.service import SelfKnowledgeService


def read_gpu_info() -> Optional[GpuInfo]:
    """Name, total and free VRAM of GPU 0 via pynvml; None when pynvml is
    absent or any call fails (the provider then yields no fact)."""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        name = raw_name.decode("utf-8") if isinstance(raw_name, bytes) else str(raw_name)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        mb = 1024 * 1024
        return GpuInfo(name=name, total_mb=info.total / mb, free_mb=info.free / mb)
    except Exception:
        return None


def read_memory_enabled() -> bool:
    """Live state of the persistent memory layers (facts or episodes on).
    Read from the settings module attributes the API toggles at runtime."""
    from opencohost.config import settings

    return bool(settings.MEMORY_FACTS_ENABLED or settings.MEMORY_EPISODES_ENABLED)


def read_owner_nickname() -> str:
    """The owner's personalization nickname, sanitized like every other prompt
    label; "" when personalization is off or the nickname is empty or
    sanitizes to nothing (K5: the owner fact then uses its neutral wording,
    never a placeholder name like "Usuario")."""
    from opencohost.core.context.injection_markers import clean_user_label
    from opencohost.core.profiles import personalization

    try:
        data = personalization.load_personalization(locale=_active_lang())
    except TypeError:
        data = personalization.load_personalization()
    if not data.get("enabled", True):
        return ""
    return clean_user_label((data.get("nickname") or "").strip())


def read_active_provider() -> str:
    """The persisted active LLM provider id ("local" by default)."""
    from opencohost.config.llm_provider import load_provider_config

    return str(load_provider_config().get("active_provider") or "local")


def _active_lang() -> str:
    from opencohost.i18n import active as i18n_active

    return i18n_active.get_active_bundle().code


def build_default_service(
    *,
    vision_enabled: Callable[[], bool],
    tts_label: Callable[[], str],
    settings_reader: Optional[Callable[[], object]] = None,
    gpu_reader: Callable[[], Optional[GpuInfo]] = read_gpu_info,
    memory_enabled: Callable[[], bool] = read_memory_enabled,
    now: Callable[[], datetime] = datetime.now,
    provider_reader: Callable[[], str] = read_active_provider,
) -> SelfKnowledgeService:
    """The production service. ``settings_reader`` defaults to the persisted
    settings (read live per turn)."""
    from opencohost.config import self_knowledge_settings as sks

    reader = settings_reader or sks.load_settings

    def card() -> IdentityCard:
        s = reader()
        return IdentityCard(
            creator=getattr(s, "creator", ""),
            repository=getattr(s, "repository", ""),
            description=getattr(s, "description", ""),
        )

    return SelfKnowledgeService(
        providers=[
            ClockProvider(now=now),
            ModelProvider(),
            GpuProvider(reader=gpu_reader),
            RuntimeProvider(provider_reader=provider_reader),
            IdentityProvider(card_reader=card),
            CapabilitiesProvider(
                vision_enabled=vision_enabled,
                tts_label=tts_label,
                memory_enabled=memory_enabled,
            ),
            CommandsProvider(),
            CreatorProvider(card_reader=card),
            InterlocutorProvider(owner_name=read_owner_nickname),
        ],
        settings_reader=reader,
        lang_reader=_active_lang,
    )
