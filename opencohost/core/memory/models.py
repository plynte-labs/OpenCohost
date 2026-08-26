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
    "",
]


class MemoryDecision(BaseModel):
    """One strictly validated decision from the memory judge."""

    model_config = ConfigDict(extra="forbid", strict=True)

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
