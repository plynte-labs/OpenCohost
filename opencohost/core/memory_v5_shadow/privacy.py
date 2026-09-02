from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime


class ShadowPrivacyController:
    def __init__(self, runtime: MemoryRuntime) -> None:
        self._rt = runtime

    def purge_profile(self, profile_id: str) -> dict:
        return self._rt._dispatch_barrier("purge", profile_id, timeout=2.0)

    def forget_all(self) -> dict:
        return self._rt._dispatch_barrier("forget_all", None, timeout=2.0)
