"""High-volume deterministic property-based fuzzing suite for Contextual Safety & Deck Parity.

Covers the 5 core invariants across 5,000+ generated combinations:
1. No Silent Veto Failure: genuine serious_amplifier + self-harm stem never becomes
   BENIGN_HYPERBOLE, GAMING_FICTION, NONE, or TROLL_ABUSE_BAIT regardless of laughter/gaming.
2. Negation & Gaming Safety: pure gaming mortality and explicit negations without
   serious amplifiers never trigger CRISIS_INTERVENTION.
3. Determinism & Throughput / Anti-ReDoS: 5,000 classifications are 100% deterministic
   in < 2.0s, and pathological 10,000-char inputs show zero catastrophic backtracking.
4. Privacy & Bounded Memory: ViewerCrisisTracker with 2,000 random viewers/messages
   enforces max_viewers LRU cap, leaks zero raw text/usernames/plain hashes, and
   escalates trajectories deterministically.
5. Deck + Safety prefetch == live Parity: 2,100 multi-turn agenda sessions across
   dark/existential/vulnerable/standard topics, safety states, and variants v1..v5 + auto
   select the exact same safe move and facet with 0 mismatches.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time

import pytest

from opencohost.config import settings
from opencohost.core.safety.contextual import (
    CARE_RECENCY_OVERRIDE,
    CARE_RECENCY_OVERRIDE_ES,
    ContextualCrisisArbiter,
    SafetySeverity,
    StreamContextMetadata,
    TopicSafetyTier,
    TurnSource,
    ViewerCrisisTracker,
    classify_topic_safety,
)
from opencohost.i18n import active as i18n_active
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaState,
    KiraAgendaController,
    TopicStatus,
)

# ---------------------------------------------------------------------------
# Combinatorial Fuzzing Vocabulary
# ---------------------------------------------------------------------------

_FUZZ_SEEDS = (20261114, 20261115, 777001)

_CRISIS_STEMS_ES = (
    "quiero morir",
    "me quiero matar",
    "me voy a matar",
    "quiero suicidarme",
    "ya no quiero vivir más",
    "quitarme la vida",
    "quiero dejar de existir",
    "ojalá me muriera",
    "desearía estar muerto",
    "todos estarían mejor sin mí",
)

_CRISIS_STEMS_EN = (
    "i want to die",
    "i wanna die",
    "i want to kill myself",
    "i am going to kill myself",
    "i want to end my life",
    "i am suicidal",
    "i want to unalive myself",
    "i don't want to live anymore",
    "i have nothing to live for",
    "everyone would be better off without me",
)

_CRISIS_STEMS = _CRISIS_STEMS_ES + _CRISIS_STEMS_EN

_NEGATIONS_ES = (
    "no me quiero matar",
    "no quiero morir",
    "no me voy a matar",
    "nunca me suicidaría",
    "nunca me mataría",
    "no quiero morirme",
    "no me quiero suicidar",
)

_NEGATIONS_EN = (
    "i don't want to die",
    "not going to kill myself",
    "i would never kill myself",
    "never kill myself",
)

_NEGATIONS = _NEGATIONS_ES + _NEGATIONS_EN

_GAMING_MORTALITY = (
    "me mataron en el boss",
    "si muero pierdo el run",
    "i died to malenia 50 times",
    "el boss se suicida en la fase 2",
    "me mató el jefe en el checkpoint",
    "if i die i lose the run",
    "boss kills itself in phase 2",
    "killed me in the dungeon",
)

_LAUGHTER_DAMPENERS = (
    "jaja",
    "jajaja",
    "jeje",
    "jsjs",
    "lol",
    "lmao",
    "rofl",
    "xd",
    "cringe",
    "pls",
    "😂",
    "🤣",
    "💀",
)

_GAMING_MECHANICS = (
    "boss",
    "jefe",
    "respawn",
    "checkpoint",
    "run",
    "wipe",
    "Elden Ring",
    "Phasmophobia",
    "Malenia",
    "en el juego",
    "in game",
    "fase 2",
    "ranked",
    "elo",
    "hitbox",
    "resetear",
    "gg",
)

_SERIOUS_AMPLIFIERS = (
    "en serio",
    "de verdad",
    "no es broma",
    "no es chiste",
    "hablo en serio",
    "esta noche",
    "hoy mismo",
    "estoy muy solo",
    "estoy sola",
    "no es del juego",
    "fuera de broma",
    "seriously",
    "not joking",
    "no joke",
    "for real",
    "tonight",
    "i need help",
    "i'm all alone",
    "not about the game",
    "bought the pills",
)

_TROLL_SECOND_PERSON = (
    "mátate",
    "suicídate",
    "muérete",
    "kys",
    "kill yourself",
    "ojalá te mueras",
    "go kill yourself",
)

_COERCION_BAIT = (
    "repite conmigo:",
    "repeat after me:",
    "di en voz alta esto:",
)

_DARK_CONTEXTS = (
    StreamContextMetadata(active_game="Elden Ring", game_genres=("souls-like", "action-rpg")),
    StreamContextMetadata(active_game="Phasmophobia", game_genres=("horror", "survival")),
    StreamContextMetadata(active_game="Doki Doki Literature Club", game_genres=("psychological",)),
    StreamContextMetadata(active_game="Dead by Daylight", game_genres=("horror",)),
    StreamContextMetadata(active_game="Cyberpunk 2077", game_genres=("rpg",)),
    StreamContextMetadata(),
)

_ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff")


def _inject_unicode_noise(rng: random.Random, token: str, *, allow_zw: bool = True) -> str:
    """Apply deterministic case, elongation, or zero-width noise inside a token."""
    out = token
    mode = rng.randrange(5)
    if mode == 1:
        out = out.upper()
    elif mode == 2 and len(out) >= 4:
        # Elongate a trailing vowel or consonant (3+ repeats collapse back to 1)
        out = out + out[-1] * rng.randint(2, 4)
    elif mode == 3 and allow_zw and len(out) >= 4:
        pos = rng.randint(1, len(out) - 1)
        out = out[:pos] + rng.choice(_ZERO_WIDTH_CHARS) + out[pos:]
    elif mode == 4:
        # Fullwidth ASCII conversion for short Latin words (NFKC normalizes back)
        out = "".join(
            chr(0xFF00 + ord(ch) - 0x20) if ("a" <= ch <= "z" or "A" <= ch <= "Z") else ch
            for ch in out
        )
    return out


def _random_chunks(rng: random.Random, pool: tuple[str, ...], count: int) -> list[str]:
    return [rng.choice(pool) for _ in range(count)]


# ---------------------------------------------------------------------------
# Invariant 1: No Silent Veto Failure
# ---------------------------------------------------------------------------


def test_invariant_1_no_silent_veto_failure() -> None:
    """Any genuine serious_amplifier + self-harm stem must NEVER be classified as
    BENIGN_HYPERBOLE, GAMING_FICTION, NONE, or TROLL_ABUSE_BAIT, regardless of
    how many laughter/slang dampeners, gaming mechanics, or 2nd-person tokens appear.
    """
    arbiter = ContextualCrisisArbiter()
    total_cases = 0

    for seed in _FUZZ_SEEDS:
        rng = random.Random(seed)
        for idx in range(700):  # 3 * 700 = 2,100 combinations
            stem = rng.choice(_CRISIS_STEMS)
            amp = rng.choice(_SERIOUS_AMPLIFIER_SLICE := _SERIOUS_AMPLIFIERS)
            laughs = _random_chunks(rng, _LAUGHTER_DAMPENERS, rng.randint(1, 5))
            games = _random_chunks(rng, _GAMING_MECHANICS, rng.randint(1, 4))
            extras: list[str] = []
            if idx % 4 == 0:
                extras.append(rng.choice(_TROLL_SECOND_PERSON))
            if idx % 5 == 0:
                extras.append(rng.choice(_COERCION_BAIT))

            stem_part = _inject_unicode_noise(rng, stem, allow_zw=True) if idx % 3 == 0 else stem
            amp_part = _inject_unicode_noise(rng, amp, allow_zw=True) if idx % 7 == 0 else amp

            tokens = [stem_part, amp_part, *laughs, *games, *extras]
            rng.shuffle(tokens)
            utterance = " ".join(tokens)

            src = rng.choice((TurnSource.DIRECT.value, TurnSource.PTT.value, TurnSource.CHAT.value))
            ctx = rng.choice(_DARK_CONTEXTS)
            result = arbiter.assess(utterance, source=src, stream_ctx=ctx)
            total_cases += 1

            assert result.severity not in {
                SafetySeverity.NONE,
                SafetySeverity.BENIGN_HYPERBOLE,
                SafetySeverity.GAMING_FICTION,
                SafetySeverity.TROLL_ABUSE_BAIT,
            }, f"Silent veto failure (seed={seed}, idx={idx}): {utterance!r} -> {result}"
            assert result.severity == SafetySeverity.CRISIS_INTERVENTION, (
                f"Expected CRISIS_INTERVENTION (seed={seed}, idx={idx}): {utterance!r} -> {result}"
            )
            assert result.signals.dampeners_vetoed is True, (
                f"Dampeners not vetoed (seed={seed}, idx={idx}): {utterance!r} -> {result}"
            )
            assert result.signals.laughter_dampener == 0.0
            assert result.signals.gaming_dampener == 0.0

            if src in {TurnSource.DIRECT.value, TurnSource.PTT.value}:
                assert result.should_trigger_crisis_turn is True
                assert result.bypass_llm_with_canned_reply is True
            else:
                assert result.should_trigger_crisis_turn is False
                assert result.should_withhold_chat_msg is True

    assert total_cases >= 2000


# ---------------------------------------------------------------------------
# Invariant 2: Negation & Gaming Safety
# ---------------------------------------------------------------------------


def test_invariant_2_negation_and_gaming_safety() -> None:
    """Pure gaming mortality and explicit negations without serious amplifiers
    must NEVER trigger CRISIS_INTERVENTION.
    """
    arbiter = ContextualCrisisArbiter()
    total_cases = 0

    for seed in _FUZZ_SEEDS:
        rng = random.Random(seed + 11)
        for idx in range(550):  # 3 * 550 = 1,650 combinations
            use_negation = idx % 2 == 0
            core = rng.choice(_NEGATIONS if use_negation else _GAMING_MORTALITY)
            if idx % 3 == 0:
                core = _inject_unicode_noise(rng, core, allow_zw=True)

            parts = [core]
            if rng.random() < 0.7:
                parts.extend(_random_chunks(rng, _GAMING_MECHANICS, rng.randint(1, 3)))
            if rng.random() < 0.6:
                parts.extend(_random_chunks(rng, _LAUGHTER_DAMPENERS, rng.randint(1, 3)))
            rng.shuffle(parts)
            utterance = " ".join(parts)

            src = rng.choice((TurnSource.DIRECT.value, TurnSource.PTT.value, TurnSource.CHAT.value))
            ctx = rng.choice(_DARK_CONTEXTS)
            result = arbiter.assess(utterance, source=src, stream_ctx=ctx)
            total_cases += 1

            assert result.severity != SafetySeverity.CRISIS_INTERVENTION, (
                f"False CRISIS_INTERVENTION (seed={seed}, idx={idx}, neg={use_negation}): "
                f"{utterance!r} -> {result}"
            )
            assert result.should_trigger_crisis_turn is False
            assert result.bypass_llm_with_canned_reply is False

    assert total_cases >= 1500


# ---------------------------------------------------------------------------
# Invariant 3: Determinism, Throughput (< 2s for 5,000), & Anti-ReDoS (10k chars)
# ---------------------------------------------------------------------------


def _build_mixed_5000_corpus() -> list[tuple[str, str, StreamContextMetadata]]:
    corpus: list[tuple[str, str, StreamContextMetadata]] = []
    sources = (TurnSource.DIRECT.value, TurnSource.PTT.value, TurnSource.CHAT.value)
    for seed in _FUZZ_SEEDS:
        rng = random.Random(seed + 99)
        for idx in range(1700):  # 3 * 1700 = 5,100 combinations
            bucket = idx % 6
            if bucket == 0:
                text = " ".join([
                    rng.choice(_CRISIS_STEMS),
                    rng.choice(_SERIOUS_AMPLIFIERS),
                    *_random_chunks(rng, _LAUGHTER_DAMPENERS, 2),
                ])
            elif bucket == 1:
                text = " ".join([
                    rng.choice(_GAMING_MORTALITY),
                    *_random_chunks(rng, _GAMING_MECHANICS, 2),
                    rng.choice(_LAUGHTER_DAMPENERS),
                ])
            elif bucket == 2:
                text = " ".join([
                    rng.choice(_NEGATIONS),
                    rng.choice(_GAMING_MECHANICS),
                ])
            elif bucket == 3:
                text = " ".join([
                    rng.choice(_TROLL_SECOND_PERSON + _COERCION_BAIT),
                    rng.choice(_LAUGHTER_DAMPENERS),
                ])
            elif bucket == 4:
                text = " ".join([
                    _inject_unicode_noise(rng, rng.choice(_CRISIS_STEMS)),
                    *_random_chunks(rng, _LAUGHTER_DAMPENERS, 2),
                    rng.choice(_GAMING_MECHANICS),
                ])
            else:
                text = "" if idx % 12 == 5 else "hola kira que buen stream hoy con el chat"
            corpus.append((text, rng.choice(sources), rng.choice(_DARK_CONTEXTS)))
    return corpus


def test_invariant_3_determinism_throughput_and_redos() -> None:
    """5,000+ classifications must be 100% deterministic and finish in < 2.0s;
    pathological 10,000-char inputs must exhibit linear time (no ReDoS).
    """
    arbiter = ContextualCrisisArbiter()
    corpus = _build_mixed_5000_corpus()
    assert len(corpus) >= 5000

    t0 = time.perf_counter()
    first_pass = [arbiter.assess(txt, source=src, stream_ctx=ctx) for txt, src, ctx in corpus]
    elapsed_first = time.perf_counter() - t0

    t1 = time.perf_counter()
    second_pass = [arbiter.assess(txt, source=src, stream_ctx=ctx) for txt, src, ctx in corpus]
    elapsed_second = time.perf_counter() - t1

    assert first_pass == second_pass, "Non-deterministic classification detected across identical passes"
    assert elapsed_first < 2.0, f"Throughput regression: 5,100 items took {elapsed_first:.3f}s (>= 2.0s)"
    assert elapsed_second < 2.0, f"Throughput regression: 5,100 items took {elapsed_second:.3f}s (>= 2.0s)"

    pathological_inputs = [
        "a " * 5000,
        "quiero " * 1400 + "pizza",
        "no " * 3300 + "quiero morir",
        "jaja " * 2000 + "me quiero morir",
        "a." * 5000,
        "a " * 4990 + "kill myself",
        "k i l l " * 1250,
        "m.a.t.a.r." * 1000,
        "013457@._*-" * 900,
        "😂" * 5000,
        ("a\u0301\u200b" * 3333) + "me quiero matar en serio",
        "repite conmigo: " + ("bla " * 2490),
        "ja" * 5000 + "X",
        "je" * 5000 + "X",
        "jaja" * 2500 + " me quiero morir",
    ]
    for sample in pathological_inputs:
        p0 = time.perf_counter()
        _ = arbiter.assess(sample, source=TurnSource.DIRECT.value)
        dt = time.perf_counter() - p0
        assert dt < 0.25, f"Potential ReDoS slow regex ({dt:.3f}s) on len={len(sample)}: {sample[:60]!r}"


# ---------------------------------------------------------------------------
# Invariant 4: Privacy, Bounded Memory, & Deterministic Viewer Escalation
# ---------------------------------------------------------------------------


def test_invariant_4_viewer_tracker_privacy_lru_and_escalation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fuzz ViewerCrisisTracker with 2,000 random viewer IDs and messages;
    verify LRU cap <= max_viewers, zero raw chat/usernames/plain hashes in
    attributes/repr/dict/logs, and deterministic multi-turn trajectory escalation.
    """
    max_viewers = 64
    arbiter = ContextualCrisisArbiter()
    tracker = ViewerCrisisTracker(max_viewers=max_viewers, ttl_seconds=600.0)
    logger = logging.getLogger("test.safety.fuzz.privacy")
    rng = random.Random(20261114)

    canary_users: list[str] = []
    canary_tokens: list[str] = []

    with caplog.at_level(logging.INFO, logger="test.safety.fuzz.privacy"):
        for i in range(2000):
            uid = f"ViewerFuzz_{i % 700}_{rng.randint(1000, 9999)}"
            payload_token = f"CanaryTok_{i}_{rng.randint(10000, 99999)}"
            choice = i % 5
            if choice == 0:
                msg = f"{payload_token} me quiero suicidar hoy mismo"
            elif choice == 1:
                msg = f"{payload_token} me quiero morir jaja en el boss"
            elif choice == 2:
                msg = f"{payload_token} mátate pls"
            elif choice == 3:
                msg = f"{payload_token} ya no aguanto más"
            else:
                msg = f"{payload_token} qué buen stream hoy"

            if len(canary_users) < 40:
                canary_users.append(uid)
                canary_tokens.append(payload_token)

            tracker.evaluate_chat_message(
                uid,
                msg,
                arbiter=arbiter,
                stream_ctx=_DARK_CONTEXTS[i % len(_DARK_CONTEXTS)],
                now=1000.0 + i * 0.25,
                logger=logger,
            )
            assert len(tracker._buckets) <= max_viewers

    assert tracker.counters["seen"] == 2000
    assert len(tracker._buckets) == max_viewers

    # Zero raw usernames, zero raw canary message tokens, zero unkeyed SHA-256 hashes
    state_blob = "\n".join([
        repr(tracker),
        repr(tracker.__dict__),
        repr(tracker._buckets),
        repr(tracker.counters),
    ])
    log_blob = "\n".join(r.getMessage() for r in caplog.records)
    bucket_keys = set(tracker._buckets.keys())

    for user in canary_users:
        assert user not in state_blob
        assert user not in log_blob
        plain_user_sha = hashlib.sha256(user.strip().lower().encode("utf-8")).digest()
        assert plain_user_sha not in bucket_keys

    for tok in canary_tokens:
        assert tok not in state_blob
        assert tok not in log_blob
        plain_msg_sha = hashlib.sha256(tok.encode("utf-8")).digest()
        assert plain_msg_sha not in bucket_keys

    # Deterministic trajectory escalation across fresh trackers & TTL expiry reset
    for idx in range(200):
        viewer = f"EscalationViewer_{idx}"
        t_base = 5000.0 + idx * 10.0
        t_a = ViewerCrisisTracker(max_viewers=32, ttl_seconds=600.0)
        t_b = ViewerCrisisTracker(max_viewers=32, ttl_seconds=600.0)

        seq = ("estoy agotado", "nada tiene sentido", "ya no aguanto más")
        out_a = [
            t_a.evaluate_chat_message(viewer, m, arbiter=arbiter, now=t_base + step * 15.0)
            for step, m in enumerate(seq)
        ]
        out_b = [
            t_b.evaluate_chat_message(viewer, m, arbiter=arbiter, now=t_base + step * 15.0)
            for step, m in enumerate(seq)
        ]
        assert out_a == out_b
        assert out_a[0].should_withhold_chat_msg is False
        assert out_a[1].should_withhold_chat_msg is True
        assert out_a[2].severity == SafetySeverity.CRISIS_INTERVENTION
        assert out_a[2].should_withhold_chat_msg is True

        # After TTL expires (> 600s), subtle turn does not carry stale escalation
        after_ttl = t_a.evaluate_chat_message(
            viewer, "estoy agotado", arbiter=arbiter, now=t_base + 700.0
        )
        assert after_ttl.signals.trajectory_delta == 0.0
        assert after_ttl.should_withhold_chat_msg is False

    prev_key = tracker._ephemeral_key
    tracker.reset_ephemeral_key()
    assert tracker._ephemeral_key != prev_key
    assert len(tracker._buckets) == 0


# ---------------------------------------------------------------------------
# Invariant 5: Deck + Safety prefetch == live Parity (2,000+ Sessions)
# ---------------------------------------------------------------------------


@pytest.fixture
def official_bundles():
    return discover_bundles(official_locales_dir(), "official")


@pytest.fixture(autouse=True)
def _restore_i18n_and_settings():
    yield
    i18n_active.reset_active_bundle()


def _activate_variant(monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", variant, raising=False)


def test_invariant_5_deck_and_safety_prefetch_live_parity(
    official_bundles, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fuzz 2,100 multi-turn agenda sessions across dark/existential/vulnerable/standard
    topics, safety states (NONE, BENIGN_HYPERBOLE, SENSITIVE_CARE, CRISIS_INTERVENTION),
    both locales (es, en), and all variants (v1..v5, auto).

    Proves prefetch and live ALWAYS select the exact same safe move and facet with 0 mismatches.
    """
    topics_pool = (
        "Teoría del internet muerto",
        "Si el universo nos controla",
        "Simulación y libre albedrío",
        "Burnout y soledad en creadores",
        "Duelo y ansiedad en directos",
        "Café de especialidad y rutina",
        "Dead internet theory",
        "Are we living in a simulation",
    )
    severities = (
        SafetySeverity.NONE,
        SafetySeverity.BENIGN_HYPERBOLE,
        SafetySeverity.SENSITIVE_CARE,
        SafetySeverity.CRISIS_INTERVENTION,
    )
    variants = ("v1", "v2", "v3", "v4", "v5", "auto")

    rng = random.Random(20261114)
    total_sessions = 0

    for locale in ("es", "en"):
        i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=official_bundles))
        flat_moves = i18n_active.agenda_progression_moves()
        fam_map = i18n_active.agenda_progression_family_moves()
        fam_moves = tuple(m for cards in fam_map.values() for m in cards)
        facets = i18n_active.agenda_progression_facets()

        for variant in variants:
            _activate_variant(monkeypatch, variant)
            move_pool = flat_moves if variant in ("v1", "v2") else fam_moves

            for i in range(175):  # 2 locales * 6 variants * 175 = 2,100 sessions
                title = topics_pool[(i + rng.randrange(len(topics_pool))) % len(topics_pool)]
                sev = severities[i % len(severities)]
                max_turns = 2 + (i % 19)  # 2..20
                turns_spoken = rng.randint(0, max_turns - 2)

                c = KiraAgendaController(
                    max_turns_per_topic=max_turns,
                    progression_variant=variant,
                )
                c.set_owner_safety_severity(sev)
                topic = c.add_topic(title, angle="enfoque práctico", approved=True)
                topic.status = TopicStatus.ACTIVE
                topic.turns_spoken = turns_spoken
                c.active_topic = topic

                c.state = AgendaState.SPEAKING
                preview = c.prefetch_action_after_current_speech()
                assert preview.kind == "enqueue"

                topic.turns_spoken = turns_spoken + 1
                c.state = AgendaState.WAITING_SIGNAL
                live = c.next_action()
                assert live.kind == "enqueue"

                p_moves = [m for m in move_pool if m in preview.prompt]
                l_moves = [m for m in move_pool if m in live.prompt]
                assert len(p_moves) == 1 and p_moves == l_moves, (
                    f"Move mismatch ({locale}, {variant}, {sev.value}, {title!r}): "
                    f"preview={p_moves} vs live={l_moves}"
                )

                if variant in ("v5", "auto"):
                    p_facets = [f for f in facets if f in preview.prompt]
                    l_facets = [f for f in facets if f in live.prompt]
                    assert len(p_facets) == 1 and p_facets == l_facets, (
                        f"Facet mismatch ({locale}, {variant}, {sev.value}, {title!r}): "
                        f"preview={p_facets} vs live={l_facets}"
                    )

                # Verify unsafe card families are never selected on dark/vulnerable topics
                profile = classify_topic_safety(title, last_owner_severity=sev)
                if variant in ("v3", "v4", "v5", "auto") and profile.blocked_families:
                    selected_move = l_moves[0]
                    for blocked_fam in profile.blocked_families:
                        assert selected_move not in fam_map.get(blocked_fam, ()), (
                            f"Blocked family {blocked_fam!r} leaked for {title!r} / {sev.value}"
                        )

                # Verify care mode strips sarcasm & injects recency override identically
                if sev in {SafetySeverity.SENSITIVE_CARE, SafetySeverity.CRISIS_INTERVENTION}:
                    override = (
                        CARE_RECENCY_OVERRIDE if locale == "en" else CARE_RECENCY_OVERRIDE_ES
                    )
                    assert override in preview.prompt
                    assert override in live.prompt
                elif profile.tier == TopicSafetyTier.STANDARD:
                    assert CARE_RECENCY_OVERRIDE not in live.prompt
                    assert CARE_RECENCY_OVERRIDE_ES not in live.prompt

                total_sessions += 1

    assert total_sessions == 2100
