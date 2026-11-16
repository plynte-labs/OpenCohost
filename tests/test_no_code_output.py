"""no_code_output guard (spoken-output-guard-and-measurement-20261109 T1).

Kira speaks every reply aloud, so she must never answer with code, JSON,
config files or unfilled templates, even when the user insists. Detection is
structural (fences, code-shaped lines, mapping shape, placeholders, symbol
density), never a vocabulary list: talking ABOUT JSON or Python stays allowed.
"""

import queue
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from opencohost.config.validation import output_guard
from opencohost.core import llm_engine
from opencohost.i18n import active as i18n_active

RULE = "no_code_output"
LOCALES = Path(llm_engine.__file__).resolve().parents[1] / "locales"


def _manifest(lang):
    return yaml.safe_load((LOCALES / lang / "manifest.yaml").read_text(encoding="utf-8"))


BLOCKED = [
    # fences
    "Sure! Here it is:\n```python\nprint('hi')\n```",
    "Claro, aquí tienes:\n```python\nprint('hola')\n```",
    "Here you go ```json {\"a\": 1} ```",
    "~~~\nls -la\n~~~",
    # code-shaped lines
    "def greet(name):\n    return name",
    "def saludar(nombre):\n    print(nombre)",
    "class Greeter:\n    pass",
    "import os\nimport sys",
    "from math import sqrt",
    "#include <stdio.h>",
    "function add(a, b) { return a + b; }",
    "const total = items.length;",
    "let count = 0;",
    "var x = 10",
    "SELECT name FROM users WHERE id = 1",
    "$ pip install requests",
    "pip install requests",
    "git clone https://example.com/repo.git",
    '<div class="box">hello</div>',
    "<script>alert(1)</script>",
    # JSON / mapping shape
    '{"name": "Kira", "role": "cohost"}',
    'config: {"modo": "estricto", "nivel": 3}',
    "name: Kira\nrole: cohost\nmode: warm",
    "servidor: local\npuerto: 8080\nmodo: debug",
    "[core]\nname = kira\nmode = warm",
    # unfilled placeholders
    "Hello [Your name], welcome to the stream.",
    "Hola [Tu nombre], bienvenido al stream.",
    "Hello {name}, welcome.",
    "Hola {nombre}, bienvenido.",
    "Hi {{ user.name }}, nice to see you.",
    "[INSERT DATE HERE]",
    "[Inserta aquí tu fecha]",
    "Dear <your name>,",
    "Querido <tu nombre>,",
    # symbol density (two weak signals together)
    "if a == b then c => d and e != f",
    "x != y && z == w",
    "Dime a -> b -> c y luego x => y",
    # two weak signals together (two symbols + braces)
    "{ a => b } and then x == y",
]

ALLOWED = [
    "The JSON file is huge.",
    "I love Python, it is such a friendly language.",
    "El archivo JSON es enorme.",
    "Me encanta Python.",
    "Primero abre el archivo, luego guarda.",
    "First open the file, then save it.",
    "Step one, two, three, done.",
    "Paso uno, dos, tres, listo.",
    "It is 3:45 and the price is $5.99.",
    "Son las 3:45 y cuesta $5.99.",
    "Ha ha :) that was great <3",
    "xD jaja qué risa.",
    "Visit example dot com slash about.",
    "Nota: mañana llueve.",
    "Note: tomorrow it rains.",
    "Let me explain how a loop works: it repeats something several times.",
    "Te explico cómo funciona un bucle: repite algo varias veces.",
    "Use the arrow keys, then press enter.",
    "Two plus two equals four.",
    "A function takes an input and returns an output, simple as that.",
    "HTML tags like div and span are the bones of a page.",
    "Well... (laughs) that was fun!",
    "The meeting is at 10:30; bring snacks.",
    "Eso es -> bueno, creo.",
    "Use a -> b and c => d, it is just arrows.",
    "She said \"no\": and left the room.",
    "Hello, I am Kira, nice to meet you!",
    "Hola, soy Kira, ¿cómo estás?",
]


@pytest.mark.parametrize("text", BLOCKED)
def test_code_shaped_text_is_blocked(text):
    allowed, reason = output_guard(text, source="direct")
    assert not allowed, text
    assert f"[{RULE}]" in reason, (text, reason)


@pytest.mark.parametrize("text", ALLOWED)
def test_plain_speech_is_not_blocked(text):
    allowed, reason = output_guard(text, source="direct")
    assert allowed, (text, reason)


@pytest.mark.parametrize("source", ["direct", "ptt", "chat", "accumulated"])
def test_rule_applies_to_every_source(source):
    allowed, reason = output_guard("Hello [Your name], welcome.", source=source)
    assert not allowed and f"[{RULE}]" in reason


def test_block_log_never_carries_the_reply_text(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        output_guard("def secret_function_name(x):\n    return x", source="direct")
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert RULE in logged
    assert "secret_function_name" not in logged


# ----------------------------------------------------------------- locales


@pytest.mark.parametrize("lang", ["en", "es"])
def test_nudge_exists_and_is_dedicated(lang):
    nudges = _manifest(lang)["llm"]["guardrail_retry_nudges"]
    assert RULE in nudges and nudges[RULE].strip()
    assert nudges[RULE] != nudges["generic"]
    allowed, reason = output_guard(nudges[RULE], source="direct")
    assert allowed, reason


@pytest.mark.parametrize("lang", ["en", "es"])
def test_fallback_lines_for_the_rule_exist_and_pass_every_guard(lang):
    lines = _manifest(lang)["llm"]["guardrail_fallback_lines"]
    # 0-3 generic, 4 memory flavoured (R9), 5-6 dedicated to no_code_output
    assert len(lines) == 7
    for line in lines:
        allowed, reason = output_guard(line, source="direct")
        assert allowed, (line, reason)
        allowed, reason = output_guard(line, source="chat")
        assert allowed, (line, reason)


def test_legacy_es_lines_stay_in_step_with_the_es_manifest():
    assert tuple(_manifest("es")["llm"]["guardrail_fallback_lines"]) == (
        i18n_active.LEGACY_GUARDRAIL_FALLBACK_LINES
    )


@pytest.mark.parametrize("lang", ["en", "es"])
def test_prompt_forbids_code_even_if_asked(lang):
    body = _manifest(lang)["llm"]["core_rules"]["body"].lower()
    assert "json" in body and "python" in body
    assert ("even if" in body) if lang == "en" else ("aunque" in body)


def test_builtin_core_rules_body_matches_the_en_manifest():
    assert i18n_active._CORE_RULES_BUILTIN["body"] == _manifest("en")["llm"]["core_rules"]["body"].rstrip("\n")


# ------------------------------------------------------- buffered path flow


@pytest.fixture
def motor():
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    return m


CODE_REPLY = "Claro:\n```python\nprint('hola')\n```"


def test_fallback_picker_uses_the_dedicated_lines_for_this_rule(motor):
    lines = i18n_active.guardrail_fallback_lines()
    reason = f"Non-negotiable violation [{RULE}]: x"
    picked = {motor._guardrail_fallback_line("direct", reason) for _ in range(6)}
    assert picked == set(lines[5:7])
    # other rules keep their pools
    generic = {motor._guardrail_fallback_line("direct", "") for _ in range(8)}
    assert generic == set(lines[:4])


def test_fallback_picker_is_silent_for_agenda_sources(motor):
    assert motor._guardrail_fallback_line("kira-agenda", f"[{RULE}]") == ""


def test_nudge_picked_for_the_rule_is_not_the_generic_one():
    nudge = llm_engine._guardrail_retry_nudge_for_reason(
        f"Non-negotiable violation [{RULE}]: response contains code"
    )
    assert nudge == i18n_active.guardrail_retry_nudge(RULE)
    assert nudge != i18n_active.guardrail_retry_nudge("generic")


def test_buffered_block_retries_once_with_the_nudge_then_uses_the_fallback(motor):
    motor._ollama_chat_with_watchdog = MagicMock(
        return_value={"message": {"content": CODE_REPLY}}
    )

    dialogo = motor._generar_dialogo("dame el codigo", source="direct", commit_history=True)

    lines = i18n_active.guardrail_fallback_lines()
    assert dialogo in lines[5:7]
    # first attempt + exactly one retry
    assert motor._ollama_chat_with_watchdog.call_count == 2
    retry_messages = motor._ollama_chat_with_watchdog.call_args_list[1].kwargs["messages"]
    assert i18n_active.guardrail_retry_nudge(RULE) in retry_messages[-1]["content"]


def test_buffered_block_accepts_a_spoken_retry(motor):
    motor._ollama_chat_with_watchdog = MagicMock(
        side_effect=[
            {"message": {"content": CODE_REPLY}},
            {"message": {"content": "Mejor te lo explico hablando, sin leer símbolos."}},
        ]
    )

    dialogo = motor._generar_dialogo("dame el codigo", source="direct", commit_history=True)

    assert dialogo == "Mejor te lo explico hablando, sin leer símbolos."
    assert motor._ollama_chat_with_watchdog.call_count == 2
