"""Composition facade for the staged ``MotorVocalIA`` decomposition."""

from __future__ import annotations

from typing import Any, Generic, TypeVar


RuntimeT = TypeVar("RuntimeT")


class CohostEngine(Generic[RuntimeT]):
    """Expose one engine runtime without duplicating any of its state.

    Phase 2 inserts this seam before scheduler and inference extraction. The
    current motor surface stays available during that migration, while every
    write still lands on the sole runtime that owns the behavior.
    """

    __slots__ = ("_runtime",)

    def __init__(self, runtime: RuntimeT) -> None:
        object.__setattr__(self, "_runtime", runtime)

    @property
    def runtime(self) -> RuntimeT:
        """Return the wrapped runtime for composition and migration tests."""
        return object.__getattribute__(self, "_runtime")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "runtime":
            raise AttributeError("runtime is read-only")
        if name == "_runtime":
            object.__setattr__(self, name, value)
            return
        setattr(self.runtime, name, value)

    def __delattr__(self, name: str) -> None:
        if name in {"_runtime", "runtime"}:
            raise AttributeError("runtime is read-only")
        delattr(self.runtime, name)

    def __dir__(self) -> list[str]:
        return sorted(set(object.__dir__(self)) | set(dir(self.runtime)))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(runtime={self.runtime!r})"
