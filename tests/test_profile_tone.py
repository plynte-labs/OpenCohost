"""Profile Tone Test Set — validates that calibrated profiles contain guardrails.

Tests verify prompt STRUCTURE, not LLM behavior.
"""

import json
import os
import re

import pytest

from opencohost.config.settings import BASE_DIR, PACKAGE_CONFIG_DIR

PERFILES_FILE = os.path.join(BASE_DIR, "perfiles.json")
DEFAULT_PROFILES_FILE = os.path.join(PACKAGE_CONFIG_DIR, "default_profiles.json")


def _load_profiles() -> dict:
    """Load the user's seeded profiles, or the seed source when absent.

    perfiles.json is gitignored user state; on clean environments (CI) the
    canonical contract lives in default_profiles.json, which seeds it.
    """
    path = PERFILES_FILE if os.path.isfile(PERFILES_FILE) else DEFAULT_PROFILES_FILE
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Prohibited as PERMISSION (must NOT appear as a positive instruction) ──
# These are checked with context: the phrase must not appear as something
# Kira is told to DO, only as something she's told NOT to do.
PROHIBITED_AS_PERMISSION = [
    "dilo sin filtros",
    "provocar reacciones",
    "Si el contexto es aburrido, dilo sin filtros",
    "generar debate y provocar reacciones",
]


class TestProfileGuardrails:
    """Every new profile must include anti-hostility guardrails."""

    @pytest.mark.parametrize("profile_name,required", [
        ("Comunidad", ["NUNCA insultes", "NUNCA digas que el chat", "NUNCA te quejes"]),
        ("Calmado", ["NUNCA insultes", "NUNCA digas que el chat", "NUNCA te quejes"]),
        ("Técnico", ["NUNCA insultes", "NUNCA inventes", "NUNCA digas frases como"]),
        ("Show", ["NUNCA insultes", "NUNCA digas que el chat", "no te quej"]),
    ])
    def test_required_guardrails_present(self, profile_name, required):
        profiles = _load_profiles()
        prompt = profiles[profile_name]["prompt"].lower()
        for guardrail in required:
            assert guardrail.lower() in prompt, (
                f"Profile '{profile_name}' missing guardrail: {guardrail}"
            )

    @pytest.mark.parametrize("profile_name", ["Comunidad", "Calmado", "Técnico", "Show"])
    def test_prohibited_as_permission_absent(self, profile_name):
        """Toxic patterns must not appear as things Kira is told TO DO."""
        profiles = _load_profiles()
        prompt = profiles[profile_name]["prompt"]
        for pattern in PROHIBITED_AS_PERMISSION:
            # Check if pattern appears OUTSIDE a negation context
            # Simple heuristic: if it appears, check nearby words for "NUNCA", "no"
            idx = prompt.lower().find(pattern.lower())
            if idx == -1:
                continue  # not present at all — good
            # Look at surrounding context (100 chars before)
            context_start = max(0, idx - 120)
            context = prompt[context_start:idx + len(pattern) + 60].lower()
            # If "NUNCA" or "prohibido" or "no digas" appears nearby, it's a guardrail example — OK
            if any(word in context for word in ["nunca", "prohibido", "no digas", "no uses"]):
                continue  # appears in a prohibition context — OK
            pytest.fail(
                f"Profile '{profile_name}' contains '{pattern}' as a permission, "
                f"not as a prohibition. Context: ...{context}..."
            )


class TestAkiraControlled:
    """Akira must retain sarcasm identity but remove hostility triggers."""

    def test_akira_keeps_sarcasm_identity(self):
        profiles = _load_profiles()
        prompt = profiles["Akira"]["prompt"]
        assert "Sarcástica" in prompt or "sarcasmo" in prompt.lower(), (
            "Akira must retain sarcasm as core identity"
        )

    def test_akira_removed_hostility_triggers(self):
        profiles = _load_profiles()
        prompt = profiles["Akira"]["prompt"]
        removed = [
            "dilo sin filtros",
            "Si el contexto es aburrido",
        ]
        for phrase in removed:
            assert phrase.lower() not in prompt.lower(), (
                f"Akira still contains hostile trigger: {phrase}"
            )

    def test_akira_guardrails_added(self):
        profiles = _load_profiles()
        prompt = profiles["Akira"]["prompt"]
        guardrails = [
            "NUNCA insultes al chat",
            "mejor sé neutral",
        ]
        for g in guardrails:
            assert g.lower() in prompt.lower(), (
                f"Akira missing guardrail: {g}"
            )

    def test_akira_default_seed_narration_has_no_voseo_forms(self):
        """default_profiles.json (the seed, independent of any live perfiles.json)
        must not narrate the Akira profile in voseo (neutral_spanish_20260809).
        The ESTILO line's quoted banned-word examples ("vos", "tenés", ...) are
        documentation of what NOT to say, not narration, so quoted spans are
        excluded from the scan.
        """
        with open(DEFAULT_PROFILES_FILE, "r", encoding="utf-8") as f:
            profiles = json.load(f)
        prompt = profiles["Akira"]["prompt"]
        narration = re.sub(r'"[^"]*"', "", prompt)
        markers = re.compile(
            r"\b(sos|tenés|querés|hacés|mirá|acordate|reaccioná|usás|podés)\b",
            re.IGNORECASE,
        )
        match = markers.search(narration)
        assert not match, f"Akira seed prompt still narrates in voseo: {match.group()!r}"


class TestProfileExistence:
    """All expected profiles must exist."""

    def test_new_profiles_exist(self):
        profiles = _load_profiles()
        for name in ["Comunidad", "Calmado", "Técnico", "Show"]:
            assert name in profiles, f"Missing profile: {name}"

    # There is deliberately no legacy-profile assertion here. One existed and
    # listed Hagg and Akira (Uncensored) — real tracked profiles until 924ea5a
    # untracked perfiles.json as runtime user state. The owner has since renamed
    # and removed profiles, which is now a supported thing to do, so pinning any
    # list is asserting that the user has not used the feature. The shipped set
    # is covered by test_new_profiles_exist against default_profiles.json.

    def test_all_profiles_have_prompt(self):
        profiles = _load_profiles()
        for name, data in profiles.items():
            if name == "Vacio":
                continue
            prompt = data.get("prompt", "")
            assert len(prompt) > 50, (
                f"Profile '{name}' prompt too short ({len(prompt)} chars)"
            )


class TestProfileUseCases:
    """Profiles must cover key streaming scenarios."""

    @pytest.mark.parametrize("profile_name,expected_tone", [
        ("Comunidad", ["cálida", "cercana", "participación"]),
        ("Calmado", ["tranquilo", "suavidad", "pausa"]),
        ("Técnico", ["precisión", "utilidad", "conceptos"]),
        ("Show", ["energía", "humor", "entreten"]),
    ])
    def test_profile_matches_intended_tone(self, profile_name, expected_tone):
        profiles = _load_profiles()
        prompt = profiles[profile_name]["prompt"].lower()
        found = sum(1 for t in expected_tone if t.lower() in prompt)
        assert found >= 2, (
            f"Profile '{profile_name}' missing tone markers. "
            f"Expected >=2 of {expected_tone}, found {found}"
        )

    def test_comunidad_handles_small_chat(self):
        """Comunidad should support intimate, small-stream scenarios."""
        profiles = _load_profiles()
        prompt = profiles["Comunidad"]["prompt"].lower()
        markers = ["nombre", "leída", "bienvenida", "pequeña"]
        assert any(m in prompt for m in markers), (
            "Comunidad must support small-chat intimacy"
        )

    def test_calmado_handles_cohost_absence(self):
        """Calmado should handle streamer-away cohost scenarios."""
        profiles = _load_profiles()
        prompt = profiles["Calmado"]["prompt"].lower()
        assert "cohost" in prompt or "streamer" in prompt, (
            "Calmado must address cohost/streamer-away scenarios"
        )
