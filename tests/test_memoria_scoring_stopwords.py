"""W3 (memoria_recall_20260718) — scoring-only stopset for select_top_k.

Kira's captured signatures/titles are polluted with high-frequency Spanish
function words ({que, mi, se, esa, este, como, para}) that survive
normalize_tokens and the domain stopset, then inflate the shared-token score
in select_top_k — letting an incidental function-word overlap cross the
>=2-shared-token gate and false-match unrelated memories.

W3 subtracts a SCORING-ONLY stopset from both the topic and the signature side
inside select_top_k. It is deliberately NOT added to _MEMORIA_DOMAIN_STOPWORDS
(design: that would shift derive_stable_key / is_capturable and re-capture
duplicates). These tests pin BOTH halves: scoring drops the function words,
capture stays byte-identical.
"""

from __future__ import annotations

import opencohost.core.memoria_store as ms
from opencohost.core.memoria_store import select_top_k


def _row(row_id, *, signature="", title="x"):
    """Minimal row shape select_top_k reads: row["signature"] or row["title"]."""
    return {"id": row_id, "signature": signature, "title": title}


# ---------------------------------------------------------------------------
# Scoring side — function words must not carry shared-token weight
# ---------------------------------------------------------------------------

def test_pure_scoring_stopword_overlap_never_matches():
    # topic and row share ONLY scoring stopwords ({que, mi, se}) and no real
    # topic token. Pre-W3 shared=3 crossed the >=2 gate and false-matched;
    # the scoring stopset drops them so shared=0 -> no match.
    rows = [_row("r1", signature="que mi se javascript")]
    assert select_top_k("que mi se python", rows) == []


def test_second_half_of_scoring_stopset_also_neutralized():
    # Locks the rest of the frozenset ({como, para, este}) the same way — three
    # shared function words alone must not match.
    rows = [_row("r1", signature="como para este javascript")]
    assert select_top_k("como para este python", rows) == []


def test_single_real_token_not_rescued_by_shared_stopwords():
    # One genuine shared token (musica) plus two shared scoring stopwords
    # (que, se). Pre-W3 shared=3 matched; post-W3 the stopwords drop out
    # leaving shared=1 (< _MIN_SHARED_TOKENS) so the incidental single-topic
    # overlap is correctly rejected (precision over recall, design W3).
    rows = [_row("r1", signature="que se musica gaming")]
    assert select_top_k("que se musica", rows) == []


def test_real_shared_tokens_still_match_after_stopset():
    # Guard against over-filtering: two genuine shared tokens (python, rust)
    # still cross the >=2 gate and select the row — before AND after W3.
    rows = [_row("r1", signature="python rust gaming")]
    assert [row["id"] for row in select_top_k("hablamos de python y rust ayer", rows)] == ["r1"]


def test_stopwords_stripped_from_topic_and_signature_both_sides():
    # Both sides carry the stopwords; only the shared REAL tokens (python,
    # rust) must count. Two real shared tokens -> still matches, proving the
    # subtraction happens symmetrically without eating the real overlap.
    rows = [_row("r1", signature="que mi python rust como")]
    assert [row["id"] for row in select_top_k("se este python rust", rows)] == ["r1"]


# ---------------------------------------------------------------------------
# Capture side — must stay BYTE-IDENTICAL (scoring stopset must not leak in)
# ---------------------------------------------------------------------------

def test_scoring_stopset_does_not_touch_capture_gates():
    text = "que mi se python programar rust"
    # 6 significant tokens (scoring stopwords still count) -> capturable.
    assert ms.is_capturable(text) is True
    # stable_key = first 6 distinct significant tokens, sorted, prefixed —
    # scoring stopwords remain part of the key (dedup unchanged).
    assert ms.derive_stable_key("prof", text) == "prof|mi-programar-python-que-rust-se"


def test_scoring_stopwords_still_count_as_significant_for_capture():
    # A pair made of ONLY scoring stopwords is still >=3 significant tokens for
    # capture — capture must NOT treat them as stopwords (design rejected
    # folding them into _MEMORIA_DOMAIN_STOPWORDS).
    assert ms.significant_token_count("que mi se") == 3
    assert ms.is_capturable("que mi se") is True
