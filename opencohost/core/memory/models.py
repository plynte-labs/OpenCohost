"""Strict Pydantic contracts for memory-promotion judge output."""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)


PromotionRejectReason = Literal[
    "vague",
    "speculative",
    "trivial",
    "transient",
    "not_attributable",
    # memory-noise-amplification-20260928 T1: the judge itself may emit these.
    "assistant_sourced",
    "general_knowledge",
    # T2: applied by the store's post-judge dedup check, never by the judge.
    "duplicate",
    "",
]

# ADR-062 (T6 a): whose fact a keeper is. Kept in sync with
# memoria_store.MEMORIA_ORIGINS (the DB CHECK constraint).
MemoryOrigin = Literal["self_stated", "shared_content"]
_MEMORY_ORIGINS = frozenset({"self_stated", "shared_content"})

# F2 (memory-paraphrase-contradiction-20260928): what a keeper does to the
# EXISTING facts shown to the judge. "new" is the backward-compatible default.
MemoryAction = Literal["new", "duplicate", "update"]
_MEMORY_ACTIONS = frozenset({"new", "duplicate", "update"})


def _require_origin_in_schema(schema: dict) -> None:
    """Constrained decoding (Ollama ``format=``) must always emit ``origin``;
    the parser still tolerates its absence (see MemoryDecision.origin)."""
    required = schema.setdefault("required", [])
    if "origin" not in required:
        required.append("origin")


class MemoryDecision(BaseModel):
    """One strictly validated decision from the memory judge."""

    model_config = ConfigDict(
        extra="forbid", strict=True, json_schema_extra=_require_origin_in_schema,
    )

    i: StrictInt = Field(
        description="1-based candidate index matching the candidate list",
        ge=1,
    )
    keep: StrictBool = Field(
        description=(
            "True if the fact is explicit, self-contained, specific, reusable, "
            "durable, and attributable"
        ),
    )
    text: StrictStr | None = Field(
        default=None,
        description=(
            "Self-contained third-person factual summary, required when keep "
            "is true"
        ),
        min_length=1,
        max_length=220,
    )
    uncertain: StrictBool = Field(
        default=False,
        description=(
            "True only when keep is true and a proper noun transcription is "
            "uncertain"
        ),
    )
    reason: PromotionRejectReason = Field(
        default="",
        description="Bounded rejection reason, used only when keep is false",
    )
    origin: MemoryOrigin | None = Field(
        default=None,
        description=(
            "self_stated when the operator said it about themselves; "
            "shared_content when they relayed someone or something else"
        ),
    )

    action: MemoryAction = Field(
        default="new",
        description=(
            "new (default), duplicate (same fact as target, adds nothing) or "
            "update (same subject, changed or contradicting value of target)"
        ),
    )
    target: StrictStr | None = Field(
        default=None,
        description="EXISTING fact label such as E1; required for duplicate/update",
    )

    @field_validator("action", mode="before")
    @classmethod
    def _tolerate_unknown_action(cls, value: object) -> object:
        """A missing or invalid action never drops the decision: it becomes
        "new" (F2 backward-compatible default)."""
        if isinstance(value, str):
            value = value.strip().lower()
            return value if value in _MEMORY_ACTIONS else "new"
        return "new"

    @field_validator("target", mode="before")
    @classmethod
    def _tolerate_bad_target(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else None

    @field_validator("origin", mode="before")
    @classmethod
    def _tolerate_unknown_origin(cls, value: object) -> object:
        """A missing or invalid origin never drops the decision: it becomes
        None, and the store applies its fail-safe default (ADR-062)."""
        if isinstance(value, str):
            value = value.strip().lower()
            return value if value in _MEMORY_ORIGINS else None
        return None

    @field_validator("text", mode="before")
    @classmethod
    def _collapse_text_whitespace(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split())
        return value

    @model_validator(mode="after")
    def _validate_decision_shape(self) -> MemoryDecision:
        if self.keep:
            if self.text is None:
                raise ValueError("text is required when keep is true")
            if self.reason:
                raise ValueError("reason is only valid when keep is false")
        else:
            if self.text is not None:
                raise ValueError("text is only valid when keep is true")
            if self.uncertain:
                raise ValueError("uncertain is only valid when keep is true")
        return self


class MemoryJudgeResult(BaseModel):
    """Strict top-level result with entry-local fail-open validation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decisions: list[MemoryDecision] = Field(
        description="List of memory promotion decisions",
    )

    @field_validator("decisions", mode="before")
    @classmethod
    def _validate_entries_independently(cls, value: object) -> object:
        if not isinstance(value, list):
            return value

        decisions: list[MemoryDecision] = []
        for entry in value:
            try:
                decisions.append(MemoryDecision.model_validate(entry))
            except ValidationError:
                continue
        return decisions
