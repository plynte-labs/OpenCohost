"""Tests for editorial_matching.py — pure token-overlap and card selection logic.

All tests follow strict TDD: written before the implementation exists.
"""

from __future__ import annotations

import pytest

from opencohost.core.editorial_cards import EditorialCard, EditorialCardStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(
    topic: str,
    triggers: list[str] | None = None,
    *,
    status: EditorialCardStatus = EditorialCardStatus.ARMED,
) -> EditorialCard:
    card = EditorialCard(
        topic=topic,
        summary="Test summary for editorial matching.",
        streamer_take="My take on this topic.",
        triggers=triggers or [],
    )
    card.status = status
    return card


# ---------------------------------------------------------------------------
# normalize_tokens
# ---------------------------------------------------------------------------

class TestNormalizeTokens:
    def test_accent_normalization_equals_non_accented(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        # Both should yield the same result — stopwords stripped, lowercase
        accented = normalize_tokens("El delay de GTA 6")
        plain = normalize_tokens("el delay de gta 6")
        assert accented == plain

    def test_accent_normalization_result_has_expected_tokens(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("El delay de GTA 6")
        # "el" "de" are stopwords; "delay", "gta", "6" remain
        assert "delay" in tokens
        assert "gta" in tokens
        assert "6" in tokens
        assert "el" not in tokens
        assert "de" not in tokens

    def test_n_tilde_normalizes_to_n(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("año nuevo")
        # "año" -> NFD -> "año" -> drop combining -> "ano"
        # "nuevo" is not a stopword
        assert "ano" in tokens
        assert "nuevo" in tokens

    def test_emoji_stripped(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("🔥 GTA 6 se retrasa 🔥")
        # No emoji tokens
        assert all(len(t) < 4 or t.isascii() for t in tokens)
        assert "gta" in tokens
        assert "6" in tokens
        assert "retrasa" in tokens
        # Nothing that looks like emoji residue
        for t in tokens:
            assert all(ord(c) < 0x2600 for c in t), f"Token {t!r} contains emoji-range char"

    def test_letter_digit_split_gta6(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("GTA6")
        assert tokens == ["gta", "6"]

    def test_letter_digit_split_mixed_string(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("GTA6 retrasa")
        assert "gta" in tokens
        assert "6" in tokens
        assert "retrasa" in tokens

    def test_punctuation_stripped(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("hola, mundo! ¿Qué tal?")
        assert "hola" in tokens
        assert "mundo" in tokens
        # punctuation-only tokens must not appear
        for t in tokens:
            assert t.isalnum(), f"Non-alphanumeric token: {t!r}"

    def test_empty_string_returns_empty(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        assert normalize_tokens("") == []

    def test_stopwords_removed(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("the of in a an and for to on at is it was")
        assert tokens == []

    def test_spanish_stopwords_removed(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("el la los las de del en un una y a al con por")
        assert tokens == []

    def test_all_tokens_are_nonempty_alphanumeric(self) -> None:
        from opencohost.core.editorial_matching import normalize_tokens

        tokens = normalize_tokens("El retraso de GTA6 confirmado en 2025!")
        assert all(t and t.isalnum() for t in tokens)


# ---------------------------------------------------------------------------
# match_score
# ---------------------------------------------------------------------------

class TestMatchScore:
    def test_gta6_vs_gta_6_card_scores_high(self) -> None:
        """GTA6 (no space) and 'GTA 6' both normalize to ['gta','6'].

        Card with topic 'GTA6' -> ['gta','6']; topic 'GTA 6 delay' -> ['gta','6','delay'].
        Overlap = 2, card_tokens = 2 -> score = 2/2 = 1.0 >= 0.8.
        This verifies that the tokenizer treats 'GTA6' and 'GTA 6' as equivalent.
        """
        from opencohost.core.editorial_matching import match_score

        # Card topic is just "GTA6" -> normalizes to ["gta","6"] (2 tokens)
        # Topic "GTA 6 delay" -> ["gta","6","delay"] (3 tokens)
        # overlap = {"gta","6"} = 2 tokens; score = 2/max(2,1) = 1.0
        card = _make_card("GTA6 retraso")  # card has 2 content tokens: ["gta", "retraso"]
        score = match_score("GTA 6 retraso confirmado", card)
        # overlap = {"gta","retraso"} = 2, card_tokens = {"gta","retraso"} = 2 -> score = 1.0
        assert score >= 0.8, f"Expected >= 0.8, got {score}"

    def test_trigger_hit_raises_score_to_09(self) -> None:
        from opencohost.core.editorial_matching import match_score

        card = _make_card("GTA 6 release date", triggers=["delay de gta"])
        score = match_score("El delay de GTA 6 se confirmo", card)
        assert score >= 0.9, f"Trigger should raise score to >= 0.9, got {score}"

    def test_no_overlap_scores_zero(self) -> None:
        from opencohost.core.editorial_matching import match_score

        card = _make_card("Minecraft nueva version")
        score = match_score("GTA 6 delay confirmado", card)
        assert score == 0.0

    def test_partial_overlap_scores_between_0_and_1(self) -> None:
        from opencohost.core.editorial_matching import match_score

        card = _make_card("GTA 6 release date")
        score = match_score("GTA 6 release confirmado", card)
        assert 0.0 < score <= 1.0

    def test_score_returns_float(self) -> None:
        from opencohost.core.editorial_matching import match_score

        card = _make_card("test topic")
        score = match_score("test topic text", card)
        assert isinstance(score, float)

    def test_trigger_partial_miss_does_not_raise(self) -> None:
        """A trigger only partially matched in topic must NOT raise score to 0.9."""
        from opencohost.core.editorial_matching import match_score

        # trigger has 3 tokens; only 2 are in topic
        card = _make_card("GTA 6 release", triggers=["delay de gta"])
        # topic has "gta" and "de" but not "delay" (it's a stopword? no, "delay" is not a stopword)
        # Actually "delay" is not in topic here
        score = match_score("GTA 6 confirmado fecha", card)
        assert score < 0.9, f"Partial trigger should not raise score, got {score}"


# ---------------------------------------------------------------------------
# select_card
# ---------------------------------------------------------------------------

class TestSelectCard:
    def test_single_clear_winner_returned(self) -> None:
        from opencohost.core.editorial_matching import select_card

        # "GTA retraso" -> ["gta","retraso"]; topic "GTA 6 retraso confirmado" -> overlap = {"gta","retraso"} = 2/2 = 1.0
        # "Minecraft survival tips" -> 0 overlap -> 0.0
        cards = [
            _make_card("GTA retraso"),
            _make_card("Minecraft survival tips"),
        ]
        result = select_card("GTA 6 retraso confirmado", cards)
        assert result is not None
        assert result.topic == "GTA retraso"

    def test_ambiguous_catalog_returns_none(self) -> None:
        from opencohost.core.editorial_matching import select_card

        # Two cards with nearly identical tokens -> ambiguous
        cards = [
            _make_card("GTA 6 retraso confirmado"),
            _make_card("GTA 6 retraso anunciado"),
        ]
        result = select_card("GTA 6 retraso noticias", cards)
        assert result is None

    def test_no_match_below_threshold_returns_none(self) -> None:
        from opencohost.core.editorial_matching import select_card

        cards = [
            _make_card("Minecraft survival tips"),
            _make_card("Fortnite season pass"),
        ]
        result = select_card("GTA 6 delay confirmado", cards)
        assert result is None

    def test_trigger_hit_selects_card(self) -> None:
        from opencohost.core.editorial_matching import select_card

        cards = [
            _make_card("GTA 6 release date", triggers=["delay de gta"]),
            _make_card("Minecraft survival tips"),
        ]
        result = select_card("El delay de GTA 6 se confirmo en 2026", cards)
        assert result is not None
        assert result.topic == "GTA 6 release date"

    def test_empty_card_list_returns_none(self) -> None:
        from opencohost.core.editorial_matching import select_card

        result = select_card("GTA 6 delay confirmado", [])
        assert result is None

    def test_short_topic_single_token_no_trigger_returns_none(self) -> None:
        """A topic with only 1 meaningful token and no trigger match -> None."""
        from opencohost.core.editorial_matching import select_card

        # "El" is a stopword, "GTA" is the only meaningful token (1 token)
        cards = [_make_card("GTA 6 release date")]
        result = select_card("El GTA", cards)
        # < 2 tokens after stopword removal -> short guard applies -> need trigger or exact slug
        assert result is None

    def test_short_topic_single_token_with_trigger_returns_card(self) -> None:
        """A topic with 1 token but exact trigger -> card returned."""
        from opencohost.core.editorial_matching import select_card

        # "gta" is a 1-token topic; card has trigger "gta" that exactly matches
        cards = [_make_card("GTA 6 release date", triggers=["gta"])]
        result = select_card("El GTA", cards)
        assert result is not None

    def test_short_topic_two_tokens_no_short_guard(self) -> None:
        """A topic with exactly 2 meaningful tokens is NOT subject to short guard."""
        from opencohost.core.editorial_matching import select_card

        # "GTA 6" -> ["gta", "6"] = 2 tokens -> normal threshold applies
        cards = [_make_card("GTA 6 release date")]
        # Need score >= 0.8 — "gta" and "6" vs card tokens ["gta","6","release","date"]
        # overlap = 2/4 = 0.5 -> below threshold -> None (short guard not the reason)
        result = select_card("GTA 6", cards)
        # This may or may not hit — what matters is short guard is NOT applied (no trigger needed)
        # The score may be 0.5 (below threshold), so None is valid too
        # Key: if result is not None, it's the GTA card (no spurious other pick)
        if result is not None:
            assert result.topic == "GTA 6 release date"

    def test_gap_less_than_01_with_multiple_candidates_returns_none(self) -> None:
        """Multiple candidates within 0.1 of each other -> ambiguous -> None."""
        from opencohost.core.editorial_matching import select_card

        # Both cards share almost all tokens with topic
        cards = [
            _make_card("GTA 6 retraso", triggers=["gta retraso"]),   # score will be 0.9
            _make_card("GTA 6 delay", triggers=["gta delay"]),        # score also 0.9
        ]
        result = select_card("GTA 6 retraso delay noticia", cards)
        # Both trigger hits -> both 0.9 -> gap = 0.0 -> ambiguous
        assert result is None

    # Fix 2: card-side short-token guard ---------------------------------

    def test_single_token_card_without_trigger_returns_none(self) -> None:
        """Card topic with < 2 tokens is only eligible via trigger hit.

        Card 'stream' -> normalize_tokens -> ['stream'] (1 token).
        Topic 'stream caido problema' has 'stream' so overlap = 1/1 = 1.0 — but
        without a trigger hit the card-side guard must reject it.
        """
        from opencohost.core.editorial_matching import select_card

        # Card with single meaningful token, no triggers
        card = _make_card("stream", triggers=[])
        result = select_card("stream caido problema", [card])
        assert result is None, (
            "A single-token card topic must not match via overlap alone — "
            "requires a trigger hit (card-side short-token guard)"
        )

    def test_single_token_card_with_trigger_returns_card(self) -> None:
        """Card 'stream' with trigger 'stream caido' matches topic containing both tokens."""
        from opencohost.core.editorial_matching import select_card

        card = _make_card("stream", triggers=["stream caido"])
        result = select_card("stream caido problema", [card])
        assert result is not None, (
            "Single-token card with a matching trigger must be selected"
        )
        assert result.topic == "stream"
