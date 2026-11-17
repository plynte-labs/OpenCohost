"""Chat->Kira reaction core, shared by the CTK shell and the headless API host.

Extracted VERBATIM from `opencohost/ui/smart_aggregator_ui.py` (the
`ChatReactionCore` class body) and `opencohost/ui/agenda_audio_controller.py`
(`on_smart_aggregated_context`, `kira_agenda_consume_pending_chat_if_due`).
The move exists for exactly one reason: `opencohost/api` has a hard
"NEVER imports opencohost.ui or customtkinter" contract (api/main.py module
docstring, engine_host.py's _MOTOR_EVENT_WHITELIST comment), so the API host
could not reuse the CTK routing where it lived. Both former homes re-export /
delegate to this module, keeping a single source of truth for the routing,
the persona prompt, and the untrusted-chat containment.

SECURITY: every path that puts viewer text into a prompt routes through
`wrap_untrusted_chat` (read-only data delimiters) or the ChatContextPacket --
do not add a path that bypasses either (project rule: never expose raw chat
in LLM prompts, logs, or persistence).
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable

from opencohost.config.logger import get_logger
from opencohost.i18n.active import get_active_bundle
from opencohost.i18n.contract import resolve
from opencohost.smart_aggregator.chat_input_contract import ChatContextPacketBuilder
from opencohost.smart_aggregator.highlight_scoring import (
    HighlightConfig,
    heuristic_score,
    is_unsafe,
    select_best,
)
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaState,
    wrap_untrusted_chat,
)

logger = get_logger()

# Chat-reaction prompt text is locale data: the llm.chat_reaction.* slots in the
# bundles under opencohost/locales. The literals below are the neutral-Spanish
# base (the es bundle values), used only when the active bundle does not ship a
# slot. They are never another locale's text. Viewer-chat fences are not here:
# wrap_untrusted_chat owns those machine delimiters.
_CHAT_SLOT_PREFIX = "llm.chat_reaction."
_TASK_DEFAULT = "\n".join((
    "TAREA: responde en directo como Kira, la co-host del stream, con actitud.",
    "SALIDA PERMITIDA: solo la frase final que Kira diría en voz alta.",
    "PERSONALIDAD: sarcasmo seco, humor ácido, cero condescendencia. No seas predecible.",
    "Si el chat repite siempre lo mismo, señálalo con ironía, no con una queja genérica.",
    "No expliques el resumen, no enumeres datos y no describas tu proceso.",
    "PROHIBIDO mencionar cantidades de mensajes, autores, ejemplos, 'temas/personas', "
    "'intención dominante', 'contexto privado', 'resumen', 'mensaje destacado' o 'el chat dice'.",
    "PROHIBIDO empezar con 'Parece que', 'Bueno, parece', 'Bien, parece', 'Voy a', "
    "'Tengo que', 'El chat está', 'energía del flujo' o 'mantener la energía'.",
    "PROHIBIDO usar fórmulas repetitivas como 'es como decir que', 'es como si', "
    "'eso es como'. NUNCA uses símiles forzados.",
    "Si el contexto interno es confuso o pobre, haz una reacción general corta sin inventar detalles.",
    "Responde en 1-2 frases cortas, con personalidad de Kira: broma, crítica o comentario concreto.",
    "No repitas el mismo tipo de reacción del turno anterior. Varía entre burla, reflexión y dato curioso.",
))
_HIGHLIGHT_LINE_DEFAULT = "Referencia opcional privada; NO nombres al autor ni digas que es destacado:"
_ANTI_REPEAT_HEADER_DEFAULT = "ÚLTIMAS RESPUESTAS DE KIRA (NO repetir estructura ni fraseo):"
_CONTEXT_HEADER_DEFAULT = "Mensajes recientes del chat:"


def _chat_slot(name: str, default: str) -> str:
    """Resolve one ``llm.chat_reaction.<name>`` string from the active bundle.

    Same degrade rule as ``i18n.active._slot``: a missing, empty or non-string
    slot yields the neutral-Spanish default, and an i18n failure never silences
    the chat reaction.
    """
    try:
        value = resolve(get_active_bundle(), _CHAT_SLOT_PREFIX + name, default=default)
    except Exception:
        return default
    return value if isinstance(value, str) and value.strip() else default


class ChatReactionCore:
    """Standalone RF3 chat reaction: aggregated context -> Kira prompt -> enqueue.

    Body moved from SmartAggregatorUI (which now delegates here). UI-only
    side effects are injected callables so this class stays headless:
    `on_log` (CTK: the shell log queue; API host: dropped -- see the wiring
    comment in engine_host) and `on_joyita` (CTK: OBS highlight overlay; API
    host: no-op, the overlay is a desktop-shell feature).
    """

    def __init__(
        self,
        motor_ia: Any,
        *,
        is_busy: Callable[[], bool] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_joyita: Callable[[str], None] | None = None,
    ) -> None:
        self._motor_ia = motor_ia
        # Default mirrors SmartAggregatorUI.is_busy so both hosts agree on
        # what "busy" means; CTK injects its own so instance-level overrides
        # in the shell keep working.
        self._is_busy = is_busy or self._default_is_busy
        self._on_log = on_log or (lambda msg: None)
        self._on_joyita = on_joyita or (lambda text: None)

    def _default_is_busy(self) -> bool:
        if not self._motor_ia:
            return True
        return self._motor_ia.is_processing or self._motor_ia.is_speaking

    def on_aggregated_context(self, data: dict) -> None:
        """Handle aggregated chat context — prompts Kira to react."""
        intent_summary = data.get("intent_summary") or {}
        intent_prompt = intent_summary.get("prompt")
        top_intents = intent_summary.get("top_intents") or []
        if self._is_busy():
            # Motor busy — enqueue to priority queue instead of dropping
            context = data.get("context", [])[-12:]
            if not context and not intent_prompt:
                return
            lines = [f"- {m.get('user', '')}: {m.get('text', '')}" for m in context]
            chat_context = intent_prompt or (_chat_slot("context_header", _CONTEXT_HEADER_DEFAULT) + "\n" + "\n".join(lines))
            last_lines = self._recent_kira_lines(3)
            prompt = self._build_kira_chat_prompt(chat_context, last_kira_lines=last_lines)
            # No explicit priority: enqueue() resolves the STREAM tier from
            # source="chat" at the live STREAM_OVER_AGENDA setting. The old
            # `priority=1` predates the tier split and would pin viewer chat
            # into the direct tier, above a typed owner question.
            self._motor_ia.enqueue(prompt, source="chat")
            self._on_log("[SmartAggregator] Kira ocupada — contexto encolado en cola prioritaria.")
            return

        context = data.get("context", [])[-12:]
        if not context and not intent_prompt:
            return

        highlight = self._select_highlight(
            context, data.get("highlight_cfg"), data.get("highlight_counters")
        )
        if highlight:
            lowered = highlight.lower()
            has_question = "?" in highlight or "¿" in highlight
            has_emoji = any(ord(c) > 0x1F000 for c in highlight)
            has_humor = any(m in lowered for m in ("jaja", "jeje", "lol", "xd", "😂", "🤣"))
            score = self._joyita_score_raw(highlight)
            if (has_question or has_emoji or has_humor) and score >= 100:
                formatted = self._format_joyita_for_obs(highlight)
                self._on_joyita(formatted)
        lines = [f"- {m.get('user', '')}: {m.get('text', '')}" for m in context]
        chat_context = intent_prompt or (_chat_slot("context_header", _CONTEXT_HEADER_DEFAULT) + "\n" + "\n".join(lines))
        last_lines = self._recent_kira_lines(3)
        prompt = self._build_kira_chat_prompt(chat_context, highlight, last_lines)
        # Same tier resolution as the busy path above: stream tier via source.
        self._motor_ia.enqueue(prompt, source="chat")
        if top_intents:
            top = top_intents[0]
            self._on_log(
                f"[SmartAggregator] Tema dominante: {top.get('label')} ({top.get('count')} msgs)."
            )
        self._on_log("[SmartAggregator] Contexto agregado enviado a Kira.")

    def _recent_kira_lines(self, count: int = 3) -> str:
        """Extract Kira's last N responses from motor IA history for anti-repeat.

        Reads the LIVE deque from outside the engine, so the engine's
        _HISTORY_ASSISTANT_ONLY_SOURCES prompt gate cannot cover it — but the
        role=="assistant" filter below already yields the same view the owner
        ruled for stream turns (Q3: only Kira's own last replies). Do not
        "unify" this to include user slots: they hold owner text, agenda
        sentinels, and 800-char template boilerplate, none of which belongs
        in a viewer-chat prompt (pinned by
        test_chat_history_gate.test_recent_kira_lines_reads_only_assistant_slots).
        """
        if not self._motor_ia:
            return ""
        historial = getattr(self._motor_ia, "historial", None)
        if not historial:
            return ""
        # RACE (judgment day 2026-08-13, Finding 1): this reads the LIVE deque
        # from a thread the engine does not control (chat source reader), while
        # _commit_history (llm_engine_memorias.py) and _build_generation_request
        # (llm_engine.py ~:2651) both append/iterate it under _history_lock from
        # the engine/agenda-speaker threads. An unguarded `list(historial)` here
        # can land mid-append and raise "RuntimeError: deque mutated during
        # iteration" -- swallowed by engine_host._on_aggregated_context's outer
        # except, silently dropping the whole reaction. Reaching into the
        # engine's own lock (rather than adding a new public accessor) keeps
        # this a one-file fix and matches how every other historial reader in
        # this codebase already synchronizes -- see llm_engine.py:2651's
        # identical `with self._history_lock: history_snapshot = list(self.historial)`.
        # getattr-guarded: several tests build a MotorVocalIA via `__new__`
        # (never running __init__, so _history_lock does not exist) and this
        # must still degrade to the old unlocked read instead of raising.
        lock = getattr(self._motor_ia, "_history_lock", None)
        with lock if lock is not None else contextlib.nullcontext():
            items = list(historial)
        kira_msgs = [
            m.get("content", "") for m in items[-20:]
            if isinstance(m, dict) and m.get("role") == "assistant"
        ][-count:]
        return "\n".join(f"- {msg[:120]}" for msg in kira_msgs if msg)

    @staticmethod
    def _build_kira_chat_prompt(chat_context: str, highlight: str = "", last_kira_lines: str = "") -> str:
        """Build a chat prompt that prevents internal summaries leaking on air.

        Instruction text and labels come from the active locale bundle
        (llm.chat_reaction.*). The viewer-chat fences are machine delimiters and
        are never localized.
        """
        highlight_line = ""
        if highlight:
            highlight_line = (
                _chat_slot("highlight_line", _HIGHLIGHT_LINE_DEFAULT) + "\n"
                # The highlight is raw viewer text too -- _select_highlight returns
                # "{user}: {text}" verbatim -- and it sits in the instruction region,
                # above the chat fence. Unfenced it could carry a forged delimiter and
                # close that fence from trusted ground, so it gets its own fence.
                f"{wrap_untrusted_chat(highlight)}\n\n"
            )
        anti_repeat = ""
        if last_kira_lines:
            anti_repeat = (
                _chat_slot("anti_repeat_header", _ANTI_REPEAT_HEADER_DEFAULT) + "\n"
                f"{last_kira_lines}\n\n"
            )
        task = _chat_slot("task", _TASK_DEFAULT) + "\n\n"
        # Untrusted viewer chat (raw usernames + text, see on_aggregated_context)
        # is wrapped in read-only, forgery-resistant data delimiters shared with
        # the agenda prompt path — see kira_agenda_controller.wrap_untrusted_chat.
        return task + anti_repeat + highlight_line + wrap_untrusted_chat(chat_context)

    @staticmethod
    def _select_highlight(
        context: list[dict],
        cfg: HighlightConfig | None = None,
        counters: dict | None = None,
    ) -> str:
        """Pick the best 'joyita' message to highlight from recent context.

        Heuristic scoring (questions, humor/emoji, unusual messages) plus an
        optional per-message Laya verdict (``msg["laya"] = (label, conf)``),
        see ``highlight_scoring``. Returns "" when nothing reaches the floor
        ``cfg.highlight_min_score``. Deterministic and cheap -- no LLM calls;
        ``counters`` (counts only) is updated in place when given.
        """
        best = select_best(context, cfg or HighlightConfig(), counters)
        if best is None:
            return ""
        return f"{best.get('user', '')}: {best.get('text', '')}"

    @staticmethod
    def _is_joyita_unsafe(text: str, lowered: str) -> bool:
        """Reject messages containing links, @mentions, or advertising."""
        return is_unsafe(text, lowered)

    @staticmethod
    def _joyita_score_raw(highlight: str) -> int:
        """Re-compute the joyita score from a formatted highlight string.

        Used by the OBS gateway to enforce the score >= 100 threshold without
        re-parsing the raw context dicts.
        """
        # highlight format: "user: text"
        text = highlight.split(":", 1)[1].strip() if ":" in highlight else highlight
        return heuristic_score(text)

    @staticmethod
    def _format_joyita_for_obs(highlight: str) -> str:
        """Format a joyita for OBS display: word-wrap into 2–3 lines.

        Without wrapping, OBS renders long text in a single line, forcing
        the streamer to use huge font sizes that blow up the scene layout.
        """
        # Already clean: strip control chars, null bytes
        text = highlight.replace("\x00", "").strip()
        if not text:
            return ""

        MAX_CHARS_PER_LINE = 38
        MIN_CHARS_TO_WRAP = 42

        if len(text) <= MIN_CHARS_TO_WRAP:
            return text

        # Try to split on word boundaries
        words = text.split()
        lines = []
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > MAX_CHARS_PER_LINE:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            lines.append(current)

        # Cap at 3 lines — drop excess words
        if len(lines) > 3:
            lines = lines[:3]
            # Add "…" to last line to indicate truncation
            if not lines[-1].endswith("…"):
                lines[-1] = lines[-1].rstrip()[:-3].rstrip() + "…"

        return "\n".join(lines)


def on_smart_aggregated_context(
    *,
    data: dict,
    kira_agenda: Any,
    motor_ia: Any,
    smart_agg_ui: Any,
    enqueue_kira_agenda_action: Callable[..., None],
    kira_agenda_update_status: Callable[[], None],
    set_pending_compact_chat: Callable[[str], None],
    log_accion: Callable[[str], None],
    **_: Any,
) -> None:
    """Route compact chat either to Agenda Mode or the existing RF3 reaction."""
    # When the controller is PAUSED_NEEDS_OPERATOR, chat must fall
    # through to the standalone RF3 reaction path — otherwise chat
    # responses are silently dropped while the operator sees a frozen UI.
    if (
        kira_agenda
        and kira_agenda.state not in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}
        # When IDLE with no active topic and nothing queued, co-host has
        # exhausted all planned topics.  Let chat fall through to the
        # standalone RF3 reaction path instead of being silently consumed
        # by next_action() which returns none() in IDLE+empty state.
        and not (
            kira_agenda.state == AgendaState.IDLE
            and kira_agenda.active_topic is None
            and not kira_agenda.queued_topics()
        )
    ):
        intent_summary = data.get("intent_summary") or {}
        compact_chat = intent_summary.get("prompt") or ""
        if not compact_chat:
            # SECURITY: this fallback joins UNSUMMARIZED viewer chat. It is
            # contained downstream by _build_prompt's read-only data delimiters
            # (KiraAgendaController._wrap_untrusted_chat), which make prompt
            # injection structurally inert. The full fix — routing this path
            # through the structured ChatContextPacket so RAW chat never reaches
            # the prompt — is the "Raw-Chat Prompt Exposure" track in
            # conductor/tracks.md. Do not remove the downstream delimiters.
            context = data.get("context", [])[-6:]
            compact_chat = "\n".join(m.get("text", "") for m in context if m.get("text"))
        if getattr(motor_ia, "is_processing", False) or getattr(motor_ia, "is_speaking", False):
            set_pending_compact_chat(compact_chat.strip())
            kira_agenda_update_status()
            return
        action = kira_agenda.next_action(
            motor_busy=getattr(motor_ia, "is_processing", False),
            kira_speaking=getattr(motor_ia, "is_speaking", False),
            compact_chat=compact_chat,
        )
        enqueue_kira_agenda_action(action)
        kira_agenda_update_status()
        return
    # ── Standalone RF3 path ─────────────────────────────────────────
    # Phase B: use ChatContextPacket instead of defective compact_chat
    import opencohost.smart_aggregator.chat_input_contract as _ic
    if _ic.USE_INPUT_CONTRACT_PROMPT:
        try:
            context = data.get("context", [])
            intent_summary = data.get("intent_summary", {})
            old_compact = intent_summary.get("prompt", "")

            builder = ChatContextPacketBuilder()
            packet = builder.build(context)

            if not packet.should_call_llm:
                # No useful signal — stay silent instead of generating
                # "qué paz", "qué silencio", etc.
                log_accion(
                    f"[InputContract] stay_silent: "
                    f"msgs={packet.total_messages} users={packet.unique_users} "
                    f"event={packet.primary_event} confidence={packet.confidence:.2f}"
                )
                return

            # Valid signal: use packet context as prompt source
            new_context = packet.to_prompt_context()
            # Inject into data dict for smart_agg_ui
            data["intent_summary"]["prompt"] = new_context
            data["_source_used"] = "input_contract"

            log_accion(
                f"[InputContract] using packet: "
                f"event={packet.primary_event} goal={packet.response_goal} "
                f"highlight={'yes' if packet.selected_highlight else 'no'} "
                f"clusters={len(packet.topic_clusters)} "
                f"old_compact={old_compact[:80]!r}"
            )
        except Exception:
            # Fallback: use old compact_chat on any error
            data["_source_used"] = "fallback_old_compact"
    else:
        data["_source_used"] = "old_compact"

    smart_agg_ui.on_aggregated_context(data)


def kira_agenda_consume_pending_chat_if_due(
    *,
    kira_agenda: Any,
    get_pending_compact_chat: Callable[[], str],
    set_pending_compact_chat: Callable[[str], None],
    enqueue_kira_agenda_action: Callable[..., None],
    kira_agenda_update_status: Callable[[], None],
    **_: Any,
) -> bool:
    compact_chat = get_pending_compact_chat().strip()
    if not compact_chat or kira_agenda is None:
        return False
    if not hasattr(kira_agenda, "chat_signal_due") or not kira_agenda.chat_signal_due():
        return False
    action = kira_agenda.next_action(compact_chat=compact_chat)
    if action.kind != "enqueue":
        return False
    set_pending_compact_chat("")
    enqueue_kira_agenda_action(action)
    kira_agenda_update_status()
    return True
