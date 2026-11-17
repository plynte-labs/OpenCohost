"""Offline Agenda Deck Benchmark Harness & Log Analyzer.

Analyzes OpenCohost session logs (``turn_events.jsonl``, ``prompt_trace/*.jsonl``,
and ``backend.log`` / ``opencohost_*.log``) strictly offline to compare Agenda Deck
variants (``off``, ``v1``..``v5``) and models side-by-side.

Key design invariants:
1. Guardrail attribution is joined strictly by ``tid`` and deterministic
   pregen-slot attempt lineage — NEVER by timestamp proximity.
2. Pre-turn or earlier-turn guardrail rejections (including background pregen
   preview retries) are kept strictly separate from the spoken turn's status.
3. Raw viewer chat blocks are stripped before metadata extraction and are NEVER
   stored or emitted in any output format.
4. Zero network or cloud API calls are ever made.
"""
from __future__ import annotations

import html
import json
import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

AGENDA_SOURCES = frozenset({"kira-agenda", "kira-agenda-stop"})

_LOG_LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] \[[^\]]*\] (.*)$")
_KV_RE = re.compile(r"(\w+)=([^\s]+)")
_PREGEN_REJECT_RE = re.compile(
    r"Agenda:\s*(?:prefetch\s+rechazado|salida\s+rechazada)\s*\(([^)]+(?:\([^)]*\))?[^)]*)\)",
    re.IGNORECASE,
)
_OUTPUT_GUARD_RE = re.compile(
    r"Salida bloqueada por guardrail:\s*(?:Non-negotiable violation\s*\[([^\]]+)\]:\s*)?(.*)$",
    re.IGNORECASE,
)
_TTS_DONE_RE = re.compile(r"Pipeline TTS completado:\s*\d+/\d+\s+fragmentos\s+en\s+([0-9.]+)s")
_PREGEN_BOUNDARY_RE = re.compile(
    r"Pregen boundary:\s+draft=(\S+)\s+source=(\S+)\s+gap_ms=(-?\d+)\s+gen_ms=(-?\d+)\s+speech_ms=(-?\d+)"
)

# Blocks inside prompt_trace user prompts that may contain untrusted viewer chat or PTT.
_CHAT_BLOCK_PATTERNS = (
    re.compile(
        r"===CHAT_VIEWERS_DATO_NO_CONFIABLE_INICIO===.*?===CHAT_VIEWERS_DATO_NO_CONFIABLE_FIN===",
        re.DOTALL,
    ),
    re.compile(
        r"===CHAT_VIEWERS_UNTRUSTED_START===.*?===CHAT_VIEWERS_UNTRUSTED_END===",
        re.DOTALL,
    ),
    re.compile(
        r"(?:CHAT COMPACTO DE VIEWERS|COMPACT VIEWER CHAT).*?(?=\n[A-ZÁÉÍÓÚÑ_ ]{4,}:|\Z)",
        re.DOTALL,
    ),
)

_TOPIC_RE = re.compile(r"^(?:TEMA APROBADO|APPROVED TOPIC)\s*:\s*(.+)$", re.MULTILINE)
_ANGLE_RE = re.compile(r"^(?:ÁNGULO|ANGLE)\s*:\s*(.+)$", re.MULTILINE)
_MOVE_RE = re.compile(
    r"(?:con este movimiento|with this move)\s*:\s*(.+?)(?:\s+No lo anuncies|\s+No anuncies|\s+Do not announce|\n|$)",
    re.IGNORECASE,
)
_FACET_RE = re.compile(
    r"(?:^(?:FACETA[^:]*|FACET[^:]*|SEGMENTO[^:]*|SEGMENT[^:]*)\s*:\s*(.+)$|(?:Foco de esta etapa|Stage focus)\s*:\s*(.+?)(?:\s+(?:Ejecútalo con este movimiento|Carry it out with this move)\s*:|\n|$))",
    re.MULTILINE | re.IGNORECASE,
)
_VARIANT_LOG_RE = re.compile(
    r"(?:agenda_progression_variant|AGENDA_PROGRESSION(?:_VARIANT)?)\s*=\s*(off|v[1-5])\b",
    re.IGNORECASE,
)

_SPANISH_STOPWORDS = frozenset({
    "a", "acá", "ahí", "al", "algo", "algunas", "algunos", "allá", "allí", "ante",
    "antes", "aquí", "así", "aun", "aunque", "bajo", "bien", "cada", "casi", "como",
    "cómo", "con", "contra", "cual", "cuando", "cuándo", "cuanto", "de", "del",
    "desde", "donde", "dónde", "dos", "el", "él", "ella", "ellas", "ellos", "en",
    "entre", "era", "eres", "es", "esa", "esas", "ese", "eso", "esos", "esta",
    "está", "están", "estar", "estas", "este", "esto", "estos", "estoy", "fue",
    "fueron", "ha", "haber", "había", "hace", "hacer", "hacia", "han", "hasta",
    "hay", "he", "hoy", "la", "las", "le", "les", "lo", "los", "luego", "más",
    "me", "menos", "mi", "mis", "mismo", "mucho", "muy", "nada", "ni", "no",
    "nos", "nosotros", "nuestra", "nuestro", "nunca", "o", "otra", "otras", "otro",
    "otros", "para", "pero", "poco", "por", "porque", "puede", "pueden", "puedes",
    "pues", "que", "qué", "quien", "quién", "quienes", "sea", "sean", "ser",
    "sería", "si", "sí", "siempre", "sin", "sino", "sobre", "solo", "sólo", "son",
    "soy", "su", "sus", "tal", "también", "tan", "tanto", "te", "tener", "tengo",
    "ti", "tiene", "tienen", "toda", "todas", "todo", "todos", "tu", "tú", "tus",
    "un", "una", "unas", "uno", "unos", "usted", "va", "vamos", "van", "vas",
    "vez", "y", "ya", "yo",
})

_ENGLISH_STOPWORDS = frozenset({
    "a", "about", "after", "all", "also", "am", "an", "and", "any", "are", "as",
    "at", "be", "because", "been", "before", "being", "between", "both", "but",
    "by", "can", "could", "did", "do", "does", "doing", "done", "down", "during",
    "each", "even", "every", "for", "from", "further", "get", "got", "had", "has",
    "have", "having", "he", "her", "here", "hers", "him", "his", "how", "i", "if",
    "in", "into", "is", "it", "its", "just", "like", "make", "many", "me", "more",
    "most", "my", "never", "no", "nor", "not", "now", "of", "off", "on", "once",
    "one", "only", "or", "other", "our", "out", "over", "own", "really", "same",
    "she", "should", "so", "some", "still", "such", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "though", "through",
    "to", "too", "under", "until", "up", "us", "very", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would",
    "you", "your", "yours",
})

_STOPWORDS = _SPANISH_STOPWORDS | _ENGLISH_STOPWORDS

# Curated self-help / cliché metaphor families observed in local 9B agenda loops.
_METAPHOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "muro": ("muro", "muros", "pared", "paredes"),
    "espejo": ("espejo", "espejos", "reflejo", "reflejos", "espejismo"),
    "piedra": ("piedra", "piedras"),
    "laberinto": ("laberinto", "laberintos"),
    "callejon": ("callejon", "callejones"),
    "eco": ("eco", "ecos"),
    "friccion": ("friccion", "rozamiento"),
    "marioneta": ("marioneta", "marionetas", "titere", "titeres", "hilos"),
    "sombra": ("sombra", "sombras"),
    "puente": ("puente", "puentes"),
    "filtro": ("filtro", "filtros", "depurado", "depurada", "depuradas"),
    "hoja_calculo": ("ecuacion", "calculo", "algoritmo", "algoritmos", "guion"),
    "ruido": ("ruido", "silencio"),
    "jaula": ("jaula", "jaulas", "prision"),
}

_METAPHOR_TOKEN_TO_FAMILY: dict[str, str] = {
    tok: fam for fam, toks in _METAPHOR_FAMILIES.items() for tok in toks
}

# Known deck/formula opening patterns (normalized ASCII lowercase).
_SKELETON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dentro de <N> anos ...", re.compile(r"^dentro de (?:\d+|diez|cinco|veinte) anos\b")),
    ("el error (mas comun|clasico) ...", re.compile(r"^el error (?:mas comun|clasico)\b")),
    ("lo mas (curioso|ironico|aterrador|triste|dificil|bonito) ...", re.compile(r"^lo mas (?:curioso|ironico|aterrador|triste|dificil|bonito|gracioso)\b")),
    ("admito que ...", re.compile(r"^admito que\b")),
    ("no se trata de ... sino de ...", re.compile(r"^no se trata de\b")),
    ("a veces me pregunto ...", re.compile(r"^a veces me pregunto\b")),
    ("imagina(te) que ...", re.compile(r"^imaginate? (?:que|esto)\b")),
    ("te has parado a pensar ...", re.compile(r"^te has parado\b")),
    ("te imaginas ...", re.compile(r"^te imaginas\b")),
    ("y eso / y si ...", re.compile(r"^y (?:eso|si|en eso)\b")),
    ("ten years from now ...", re.compile(r"^(?:ten|\d+) years from now\b")),
    ("the most common mistake ...", re.compile(r"^the (?:most common|classic) mistake\b")),
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class NumericSummary:
    count: int = 0
    total: float = 0.0
    mean: float = 0.0
    median: float = 0.0
    min: float = 0.0
    max: float = 0.0
    p90: float = 0.0


@dataclass
class GuardEventRecord:
    tid: str
    turn_index: int
    topic_index: int
    stage: str  # "output_guard" | "turn_state" | "live_agenda" | "pregen_preview"
    reason: str
    attempt_turn_id: str = ""
    outcome: str = ""  # "retried_and_spoken" | "fallback" | "blocked" | "pregen_retried" | "pregen_evicted"


@dataclass
class AttemptRecord:
    attempt_turn_id: str
    tid: str
    source: str
    model: str
    ts: str
    status: str  # "accepted" | "rejected" | "evicted" | "discarded"
    stage: str = ""  # "foreground" | "output_guard" | "pregen_preview" | "live_agenda"
    guard_code: str = ""
    guard_detail: str = ""
    topic_title: str = ""
    angle: str = ""
    move_text: str = ""
    facet_text: str = ""
    variant_hint: str = ""
    reply: str = ""
    word_count: int = 0
    char_count: int = 0
    sentence_count: int = 0


@dataclass
class SpokenTurnRecord:
    tid: str
    turn_index: int
    topic_index: int
    topic_title: str
    angle: str
    source: str
    model: str
    variant_hint: str
    served_pregen: bool
    accepted_attempt_turn_id: str
    move_text: str
    facet_text: str
    spoken_text: str
    word_count: int
    sentence_count: int
    char_count: int
    duration_s: Optional[float]
    llm_ms: Optional[int]
    engine_ms: Optional[int]
    outcome: str
    latency_outcome: str
    spoken_turn_blocked: bool
    was_retried_foreground: bool
    tid_guard_events: list[GuardEventRecord] = field(default_factory=list)
    tid_rejected_attempts: list[AttemptRecord] = field(default_factory=list)
    pregen_rejected_attempts: list[AttemptRecord] = field(default_factory=list)


@dataclass
class GuardSummary:
    spoken_turns_blocked: int = 0
    foreground_retried_turns: int = 0
    foreground_guards_by_reason: dict[str, int] = field(default_factory=dict)
    pregen_rejections_total: int = 0
    pregen_rejections_by_reason: dict[str, int] = field(default_factory=dict)
    foreground_guard_events: list[GuardEventRecord] = field(default_factory=list)
    pregen_rejected_attempts: list[AttemptRecord] = field(default_factory=list)


@dataclass
class DuplicateSentenceMatch:
    scope: str  # "within_topic" | "across_topics"
    kind: str  # "exact" | "near"
    similarity: float
    topic_a: int
    turn_a: int
    topic_b: int
    turn_b: int
    sentence_a: str
    sentence_b: str = ""


@dataclass
class NgramRepeat:
    ngram: str
    n: int
    count: int
    distinct_turns: int
    distinct_topics: int


@dataclass
class OpeningSkeletonMatch:
    skeleton: str
    count: int
    distinct_topics: int
    turn_refs: list[str] = field(default_factory=list)


@dataclass
class MetaphorLoopHit:
    family: str
    count: int
    distinct_turns: int
    distinct_topics: int
    terms_seen: dict[str, int] = field(default_factory=dict)


@dataclass
class ContentWordHit:
    word: str
    count: int
    distinct_turns: int


@dataclass
class RepetitionSummary:
    exact_duplicate_sentences_within_topic: int = 0
    exact_duplicate_sentences_across_topics: int = 0
    near_duplicate_sentences_within_topic: int = 0
    near_duplicate_sentences_across_topics: int = 0
    duplicate_matches: list[DuplicateSentenceMatch] = field(default_factory=list)
    repeated_ngrams: dict[int, list[NgramRepeat]] = field(default_factory=lambda: {3: [], 4: [], 5: []})
    cross_topic_opening_skeletons: list[OpeningSkeletonMatch] = field(default_factory=list)
    all_opening_skeletons: list[OpeningSkeletonMatch] = field(default_factory=list)


@dataclass
class TopicSummary:
    topic_index: int
    title: str
    angle: str
    turn_count: int
    total_duration_s: float
    word_stats: NumericSummary
    sentence_stats: NumericSummary
    char_stats: NumericSummary
    duration_stats: NumericSummary
    exact_dup_sentences: int
    near_dup_sentences: int
    spoken_turns_blocked: int
    foreground_retried_turns: int
    pregen_rejections: int
    top_content_words: list[ContentWordHit] = field(default_factory=list)
    metaphor_loops: list[MetaphorLoopHit] = field(default_factory=list)


@dataclass
class RunAnalysis:
    label: str
    variant: str
    model: str
    log_dir: str
    spoken_turns: list[SpokenTurnRecord]
    rejected_attempts: list[AttemptRecord]
    topics: list[TopicSummary]
    word_stats: NumericSummary
    sentence_stats: NumericSummary
    char_stats: NumericSummary
    duration_stats: NumericSummary
    llm_ms_stats: NumericSummary
    total_duration_s: float
    guard_summary: GuardSummary
    repetition: RepetitionSummary
    top_content_words: list[ContentWordHit]
    metaphor_loops: list[MetaphorLoopHit]


# ---------------------------------------------------------------------------
# Text normalization & NLP-free offline metrics helpers
# ---------------------------------------------------------------------------


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    lowered = strip_accents(text.lower())
    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def tokenize_words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)?", text.lower(), flags=re.UNICODE)


def split_sentences(text: str) -> list[str]:
    """Split Spanish/English spoken output into clean sentences without breaking decimals."""
    if not text or not text.strip():
        return []
    # Protect decimal numbers like 3.8 or 9.5
    protected = re.sub(r"(\d)\.(\d)", r"\1<DOT>\2", text.strip())
    # Split on newlines or terminal punctuation followed by whitespace/end
    chunks = re.split(r"(?<=[.!?…])\s+|\n+", protected)
    out: list[str] = []
    for chunk in chunks:
        restored = chunk.replace("<DOT>", ".").strip()
        if restored and re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]", restored):
            out.append(restored)
    return out


def summarize_numbers(values: Iterable[float]) -> NumericSummary:
    nums = sorted(float(v) for v in values)
    n = len(nums)
    if n == 0:
        return NumericSummary()
    total = sum(nums)
    p90_idx = min(n - 1, math.ceil(0.90 * n) - 1) if n > 1 else 0
    return NumericSummary(
        count=n,
        total=round(total, 2),
        mean=round(total / n, 2),
        median=round(float(statistics.median(nums)), 2),
        min=round(nums[0], 2),
        max=round(nums[-1], 2),
        p90=round(nums[p90_idx], 2),
    )


def strip_untrusted_chat(prompt_text: str) -> str:
    """Remove all viewer-chat blocks before reading any topic/variant metadata."""
    out = prompt_text
    for pat in _CHAT_BLOCK_PATTERNS:
        out = pat.sub("", out)
    return out


def extract_prompt_metadata(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Extract ONLY non-chat agenda metadata from prompt_trace messages."""
    user_contents = [
        str(m.get("content", ""))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    if not user_contents:
        return {"topic_title": "", "angle": "", "move": "", "facet": "", "variant_hint": ""}

    last_user = strip_untrusted_chat(user_contents[-1])
    topic_m = _TOPIC_RE.search(last_user)
    angle_m = _ANGLE_RE.search(last_user)
    move_m = _MOVE_RE.search(last_user)
    facet_m = _FACET_RE.search(last_user)
    facet_val = ""
    if facet_m:
        facet_val = (facet_m.group(1) or facet_m.group(2) or "").strip()
    variant_hint = detect_variant_from_prompt(last_user)
    return {
        "topic_title": topic_m.group(1).strip() if topic_m else "",
        "angle": angle_m.group(1).strip() if angle_m else "",
        "move": move_m.group(1).strip() if move_m else "",
        "facet": facet_val,
        "variant_hint": variant_hint,
    }


def detect_variant_from_prompt(sanitized_user_prompt: str) -> str:
    """Infer agenda variant (off, v1..v5) from prompt structure markers."""
    low = strip_accents(sanitized_user_prompt.lower())
    has_move = (
        "con este movimiento:" in low
        or "with this move:" in low
        or "palabras que ya se usaron varias veces" in low
        or "words already used several times" in low
    )
    has_cap = any(
        marker in low
        for marker in (
            "maximo 3 frases",
            "hasta 3 frases",
            "max 3 sentences",
            "at most 3 short",
            "up to 3 short",
            "110 tokens",
            "limite estricto",
            "strict cap",
            "3 oraciones",
        )
    )
    has_ledger_v2 = any(
        marker in low
        for marker in (
            "frases ya dichas",
            "no reutilices frases",
            "oraciones previas",
            "aperturas ya usadas",
            "aperturas y giros ya usados",
            "do not reuse sentences",
            "recent sentences",
            "used openings",
            "openings and phrasings already used",
        )
    )
    has_facet = any(
        marker in low
        for marker in (
            "foco de esta etapa:",
            "stage focus:",
            "no anuncies la etapa",
            "do not announce the stage",
            "faceta",
            "facet:",
            "facet ",
            "segmento del turno",
            "turn segment",
        )
    )
    has_family = "familia de movimiento" in low or "move family" in low

    if has_facet:
        return "v5"
    if has_ledger_v2:
        return "v4"
    if has_family:
        return "v3"
    if has_cap and has_move:
        return "v2"
    if has_cap and not has_move:
        return "v2"
    if has_move:
        return "v1"
    return "off"


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------


def _load_turn_events(log_dir: Path) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    for idx in range(5, 0, -1):
        p = log_dir / f"turn_events.jsonl.{idx}"
        if p.exists():
            candidates.append(p)
    primary = log_dir / "turn_events.jsonl"
    if primary.exists():
        candidates.append(primary)

    events: list[dict[str, Any]] = []
    for path in candidates:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("tid"):
                events.append(obj)
    return events


def _load_prompt_traces(
    log_dir: Path, *, dates: Optional[set[str]] = None
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    pt_dir = log_dir / "prompt_trace"
    if not pt_dir.is_dir():
        return {}, []
    by_turn: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for path in sorted(pt_dir.glob("*.jsonl")):
        if dates and path.stem not in dates:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("turn"):
                by_turn[str(obj["turn"])] = obj
                ordered.append(obj)
    return by_turn, ordered


def _find_session_logs(log_dir: Path, *, dates: Optional[set[str]] = None) -> list[Path]:
    logs: list[Path] = []
    for p in sorted(log_dir.glob("opencohost_*.log")):
        m = re.match(r"opencohost_(\d{8})_\d{6}\.log$", p.name)
        if not m:
            continue
        if dates is not None and m.group(1) not in dates:
            continue
        logs.append(p)
    backend = log_dir / "backend.log"
    if backend.exists():
        logs.append(backend)
    return logs


def _hms_to_seconds(hms: str) -> int:
    hh, mm, ss = (int(x) for x in hms.split(":"))
    return hh * 3600 + mm * 60 + ss


def _parse_kv(msg: str) -> dict[str, str]:
    return {k: v for k, v in _KV_RE.findall(msg)}


def _extract_guard_code(reason_text: str) -> str:
    m = re.match(r"([a-z0-9_]+)", reason_text.strip(), flags=re.IGNORECASE)
    return m.group(1).lower() if m else "guardrail"


# ---------------------------------------------------------------------------
# Strict tid + Pregen-Slot Lineage Joiner
# ---------------------------------------------------------------------------


@dataclass
class _ParsedLogContext:
    tid_attempts: dict[str, list[str]]  # tid -> list of prompt_trace turn_hex
    tid_guard_logs: dict[str, list[tuple[str, str, str]]]  # tid -> [(stage, code, detail)]
    tid_speech_s: dict[str, float]
    tid_model_override: dict[str, str]
    trace_model_override: dict[str, str]  # turn_hex -> cloud_model when transport=cloud
    tid_served_pregen: dict[str, str]  # tid -> accepted pregen turn_hex
    pregen_rejections_for_accepted: dict[str, list[AttemptRecord]]  # accepted_hex -> prior rejected attempts
    all_rejected_attempts: dict[str, AttemptRecord]  # turn_hex -> AttemptRecord
    variant_from_logs: Optional[str]


def _parse_session_logs(
    log_files: list[Path],
    trace_by_turn: dict[str, dict[str, Any]],
) -> _ParsedLogContext:
    """Parse backend/session logs sequentially to build exact tid & pregen lineage."""
    tid_attempts: dict[str, list[str]] = defaultdict(list)
    tid_guard_logs: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    tid_speech_s: dict[str, float] = {}
    tid_model_override: dict[str, str] = {}
    trace_model_override: dict[str, str] = {}
    tid_served_pregen: dict[str, str] = {}
    pregen_rejections_for_accepted: dict[str, list[AttemptRecord]] = {}
    all_rejected_attempts: dict[str, AttemptRecord] = {}
    seen_guard_keys: set[tuple[str, str, str, str]] = set()
    variant_from_logs: Optional[str] = None

    speech_start_sec: dict[str, int] = {}
    pending_fg_tid: Optional[str] = None
    cur_fg_attempts: dict[str, list[str]] = defaultdict(list)
    last_fg_guard: dict[str, tuple[str, str]] = {}  # tid -> (code, detail)
    last_trace_hex: Optional[str] = None
    last_trace_is_fg: bool = False
    last_trace_source: str = ""

    # Pregen state machine (per server session)
    last_pregen_hex: Optional[str] = None
    pregen_pending_rejections: list[AttemptRecord] = []
    pregen_pending_guard: Optional[tuple[str, str]] = None
    accepted_pregen_slot: Optional[tuple[str, list[AttemptRecord]]] = None
    frozen_pregen_slot: Optional[tuple[str, list[AttemptRecord]]] = None
    last_cloud_model: Optional[str] = None

    def _build_rejected_record(
        turn_hex: str,
        *,
        tid: str,
        stage: str,
        guard_code: str,
        guard_detail: str,
        source: str,
    ) -> AttemptRecord:
        tr = trace_by_turn.get(turn_hex, {})
        meta = extract_prompt_metadata(tr.get("messages", [])) if tr else {}
        reply = str(tr.get("reply") or "")
        return AttemptRecord(
            attempt_turn_id=turn_hex,
            tid=tid,
            source=str(tr.get("source") or source),
            model=str(tr.get("model") or ""),
            ts=str(tr.get("ts") or ""),
            status="rejected",
            stage=stage,
            guard_code=guard_code,
            guard_detail=guard_detail,
            topic_title=meta.get("topic_title", ""),
            angle=meta.get("angle", ""),
            move_text=meta.get("move", ""),
            facet_text=meta.get("facet", ""),
            variant_hint=meta.get("variant_hint", ""),
            reply=reply,
            word_count=len(tokenize_words(reply)),
            char_count=len(reply),
            sentence_count=len(split_sentences(reply)),
        )

    def _reset_pregen_session() -> None:
        nonlocal pending_fg_tid, last_trace_hex, last_trace_is_fg
        nonlocal last_pregen_hex, pregen_pending_rejections, pregen_pending_guard
        nonlocal accepted_pregen_slot, frozen_pregen_slot, last_cloud_model
        pending_fg_tid = None
        cur_fg_attempts.clear()
        last_trace_hex = None
        last_trace_is_fg = False
        last_pregen_hex = None
        pregen_pending_rejections = []
        pregen_pending_guard = None
        accepted_pregen_slot = None
        frozen_pregen_slot = None
        last_cloud_model = None

    for path in log_files:
        _reset_pregen_session()
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            vm = _VARIANT_LOG_RE.search(raw_line)
            if vm:
                variant_from_logs = vm.group(1).lower()

            parsed = _LOG_LINE_RE.match(raw_line.rstrip("\r\n"))
            if not parsed:
                if "Started server process" in raw_line or "Application startup complete" in raw_line:
                    _reset_pregen_session()
                continue
            hms, _level, msg = parsed.groups()

            if "Motor IA inicializado" in msg:
                _reset_pregen_session()
                continue

            if msg.startswith("[TURN_STATE] "):
                fields = _parse_kv(msg)
                tid = fields.get("tid", "")
                event = fields.get("event", "")
                if not tid:
                    continue
                if event == "begin":
                    pending_fg_tid = tid
                    last_cloud_model = None
                elif event == "guard_block":
                    code, detail = last_fg_guard.pop(tid, ("guard_block", "blocked"))
                    cur_atts = cur_fg_attempts.get(tid) or tid_attempts.get(tid) or []
                    att_hex = cur_atts[-1] if cur_atts else ""
                    g_key = (tid, att_hex, "output_guard", code)
                    if g_key not in seen_guard_keys:
                        seen_guard_keys.add(g_key)
                        tid_guard_logs[tid].append(("output_guard", code, detail))
                    if att_hex and att_hex not in all_rejected_attempts:
                        all_rejected_attempts[att_hex] = _build_rejected_record(
                            att_hex,
                            tid=tid,
                            stage="output_guard",
                            guard_code=code,
                            guard_detail=detail,
                            source=fields.get("source", "kira-agenda"),
                        )
                elif event == "served_pregen":
                    slot = accepted_pregen_slot or frozen_pregen_slot
                    if slot is not None:
                        acc_hex, rej_list = slot
                        if tid not in tid_served_pregen:
                            tid_served_pregen[tid] = acc_hex
                        if rej_list and acc_hex not in pregen_rejections_for_accepted:
                            pregen_rejections_for_accepted[acc_hex] = list(rej_list)
                        if slot is accepted_pregen_slot:
                            accepted_pregen_slot = None
                        else:
                            frozen_pregen_slot = None
                        last_pregen_hex = None
                    if pending_fg_tid == tid:
                        pending_fg_tid = None
                elif event in ("complete", "fail", "drop", "refuse", "expire"):
                    if pending_fg_tid == tid:
                        pending_fg_tid = None
                continue

            if msg.startswith("[PROMPT_TRACE] "):
                fields = _parse_kv(msg)
                turn_hex = fields.get("turn", "")
                tid = fields.get("tid", "")
                src = fields.get("source", "")
                if not turn_hex:
                    continue
                last_trace_hex = turn_hex
                last_trace_source = src
                if tid:
                    last_trace_is_fg = True
                    pending_fg_tid = tid
                    cur_fg_attempts[tid].append(turn_hex)
                    if turn_hex not in tid_attempts[tid]:
                        tid_attempts[tid].append(turn_hex)
                    if last_cloud_model:
                        tid_model_override[tid] = last_cloud_model
                else:
                    last_trace_is_fg = False
                    if src.startswith("kira-agenda") or src == "guardrail_retry":
                        last_pregen_hex = turn_hex
                continue

            if "[MODEL_TRACE]" in msg:
                fields = _parse_kv(msg)
                if fields.get("transport") == "cloud" and fields.get("cloud_model"):
                    cloud_model = fields["cloud_model"]
                    last_cloud_model = cloud_model
                    if last_trace_is_fg and pending_fg_tid:
                        tid_model_override[pending_fg_tid] = cloud_model
                    if last_trace_hex:
                        trace_model_override[last_trace_hex] = cloud_model
                continue

            if "Salida bloqueada por guardrail:" in msg:
                gm = _OUTPUT_GUARD_RE.search(msg)
                code = (gm.group(1) or "output_guard").strip() if gm else "output_guard"
                detail = (gm.group(2) or msg).strip() if gm else msg
                if last_trace_is_fg and pending_fg_tid:
                    last_fg_guard[pending_fg_tid] = (code, detail)
                elif last_pregen_hex and last_pregen_hex not in all_rejected_attempts:
                    pregen_pending_guard = (code, detail)
                    all_rejected_attempts[last_pregen_hex] = _build_rejected_record(
                        last_pregen_hex,
                        tid="",
                        stage="pregen_output_guard",
                        guard_code=code,
                        guard_detail=detail,
                        source=last_trace_source or "kira-agenda",
                    )
                    pregen_pending_rejections.append(all_rejected_attempts[last_pregen_hex])
                continue

            if "Agenda prefetch response" in msg and last_pregen_hex:
                # Tentative acceptance unless immediately followed by prefetch rechazado
                accepted_pregen_slot = (last_pregen_hex, list(pregen_pending_rejections))
                pregen_pending_guard = None
                continue

            if "Agenda: prefetch rechazado" in msg or "Agenda: salida rechazada" in msg:
                rm = _PREGEN_REJECT_RE.search(msg)
                raw_reason = rm.group(1).strip() if rm else "agenda_guardrail"
                code = _extract_guard_code(raw_reason)
                if "salida rechazada" in msg and pending_fg_tid:
                    cur_atts = cur_fg_attempts.get(pending_fg_tid) or tid_attempts.get(pending_fg_tid) or []
                    att_hex = cur_atts[-1] if cur_atts else ""
                    g_key = (pending_fg_tid, att_hex, "live_agenda", code)
                    if g_key not in seen_guard_keys:
                        seen_guard_keys.add(g_key)
                        tid_guard_logs[pending_fg_tid].append(("live_agenda", code, raw_reason))
                    if att_hex and att_hex not in all_rejected_attempts:
                        all_rejected_attempts[att_hex] = _build_rejected_record(
                            att_hex,
                            tid=pending_fg_tid,
                            stage="live_agenda",
                            guard_code=code,
                            guard_detail=raw_reason,
                            source="kira-agenda",
                        )
                elif last_pregen_hex:
                    if last_pregen_hex not in all_rejected_attempts:
                        rec = _build_rejected_record(
                            last_pregen_hex,
                            tid="",
                            stage="pregen_preview",
                            guard_code=code,
                            guard_detail=raw_reason,
                            source=last_trace_source or "kira-agenda",
                        )
                        all_rejected_attempts[last_pregen_hex] = rec
                        pregen_pending_rejections.append(rec)
                    # Cancel tentative acceptance of this rejected draft
                    if accepted_pregen_slot and accepted_pregen_slot[0] == last_pregen_hex:
                        accepted_pregen_slot = None
                    last_pregen_hex = None
                continue

            if "Pregen descartado" in msg:
                if accepted_pregen_slot:
                    accepted_pregen_slot = None
                pregen_pending_rejections.clear()
                last_pregen_hex = None
                continue

            bm = _PREGEN_BOUNDARY_RE.search(msg)
            if bm:
                draft_state = bm.group(1)
                if draft_state == "evicted":
                    accepted_pregen_slot = None
                    pregen_pending_rejections.clear()
                    last_pregen_hex = None
                elif draft_state == "rejected":
                    accepted_pregen_slot = None
                    pregen_pending_rejections.clear()
                    last_pregen_hex = None
                elif draft_state == "frozen" and accepted_pregen_slot is not None:
                    frozen_pregen_slot = accepted_pregen_slot
                    accepted_pregen_slot = None
                elif draft_state in ("used", "late", "resumed"):
                    # Already consumed or about to be consumed by served_pregen
                    pregen_pending_rejections.clear()
                continue

            if msg.startswith("[TURN_SPEECH] "):
                fields = _parse_kv(msg)
                tid = fields.get("tid", "")
                state = fields.get("state", "")
                if tid and state == "started":
                    speech_start_sec[tid] = _hms_to_seconds(hms)
                elif tid and state == "finished" and tid in speech_start_sec:
                    delta = _hms_to_seconds(hms) - speech_start_sec[tid]
                    if delta < 0:
                        delta += 86400
                    tid_speech_s[tid] = float(delta)
                continue

            if "Pipeline TTS completado:" in msg and pending_fg_tid is None:
                # Fallback speech duration if [TURN_SPEECH] didn't match
                tm = _TTS_DONE_RE.search(msg)
                if tm and speech_start_sec:
                    pass  # [TURN_SPEECH] always follows on the next line

    return _ParsedLogContext(
        tid_attempts=dict(tid_attempts),
        tid_guard_logs=dict(tid_guard_logs),
        tid_speech_s=tid_speech_s,
        tid_model_override=tid_model_override,
        trace_model_override=trace_model_override,
        tid_served_pregen=tid_served_pregen,
        pregen_rejections_for_accepted=pregen_rejections_for_accepted,
        all_rejected_attempts=all_rejected_attempts,
        variant_from_logs=variant_from_logs,
    )


# ---------------------------------------------------------------------------
# Repetition, Skeletons, and Metaphor Analysis
# ---------------------------------------------------------------------------


def _normalize_sentence(sent: str) -> str:
    return normalize_text(sent)


def _jaccard(tokens_a: set[str], tokens_b: set[str]) -> float:
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _classify_opening_skeleton(first_sentence: str) -> str:
    norm = _normalize_sentence(first_sentence)
    for label, pat in _SKELETON_PATTERNS:
        if pat.search(norm):
            return label
    words = norm.split()
    return " ".join(words[:4]) if len(words) >= 4 else norm


def _analyze_repetitions(turns: list[SpokenTurnRecord]) -> RepetitionSummary:
    summary = RepetitionSummary()

    # 1. Exact & near-duplicate sentences (within topic & across topics)
    sentences: list[tuple[int, int, str, str, set[str]]] = []
    for t in turns:
        for s in split_sentences(t.spoken_text):
            norm = _normalize_sentence(s)
            toks = set(norm.split())
            if len(toks) >= 4:
                sentences.append((t.topic_index, t.turn_index, s, norm, toks))

    seen_exact: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for topic_idx, turn_idx, raw_s, norm_s, _ in sentences:
        seen_exact[norm_s].append((topic_idx, turn_idx, raw_s))

    exact_pairs_seen: set[tuple[int, int, int, int, str]] = set()
    for norm_s, occs in seen_exact.items():
        if len(occs) < 2:
            continue
        base_top, base_turn, base_raw = occs[0]
        for cur_top, cur_turn, cur_raw in occs[1:]:
            scope = "within_topic" if cur_top == base_top else "across_topics"
            if scope == "within_topic":
                summary.exact_duplicate_sentences_within_topic += 1
            else:
                summary.exact_duplicate_sentences_across_topics += 1
            exact_pairs_seen.add((base_top, base_turn, cur_top, cur_turn, norm_s))
            summary.duplicate_matches.append(
                DuplicateSentenceMatch(
                    scope=scope,
                    kind="exact",
                    similarity=1.0,
                    topic_a=base_top,
                    turn_a=base_turn,
                    topic_b=cur_top,
                    turn_b=cur_turn,
                    sentence_a=base_raw,
                    sentence_b=cur_raw,
                )
            )

    # Near duplicates (exclude exact matches, require >= 5 tokens)
    for i in range(len(sentences)):
        top_a, turn_a, raw_a, norm_a, toks_a = sentences[i]
        if len(toks_a) < 5:
            continue
        for j in range(i + 1, len(sentences)):
            top_b, turn_b, raw_b, norm_b, toks_b = sentences[j]
            if len(toks_b) < 5 or norm_a == norm_b:
                continue
            jac = _jaccard(toks_a, toks_b)
            seq = SequenceMatcher(None, norm_a, norm_b).ratio() if jac >= 0.55 else 0.0
            sim = max(jac, seq)
            if jac >= 0.70 or seq >= 0.80:
                scope = "within_topic" if top_a == top_b else "across_topics"
                if scope == "within_topic":
                    summary.near_duplicate_sentences_within_topic += 1
                else:
                    summary.near_duplicate_sentences_across_topics += 1
                summary.duplicate_matches.append(
                    DuplicateSentenceMatch(
                        scope=scope,
                        kind="near",
                        similarity=round(sim, 3),
                        topic_a=top_a,
                        turn_a=turn_a,
                        topic_b=top_b,
                        turn_b=turn_b,
                        sentence_a=raw_a,
                        sentence_b=raw_b,
                    )
                )

    # 2. Repeated n-grams (3, 4, 5)
    for n in (3, 4, 5):
        ngram_total: Counter[str] = Counter()
        ngram_turns: dict[str, set[int]] = defaultdict(set)
        ngram_topics: dict[str, set[int]] = defaultdict(set)
        for t in turns:
            words = [strip_accents(w) for w in tokenize_words(t.spoken_text)]
            if len(words) < n:
                continue
            for idx in range(len(words) - n + 1):
                gram_words = words[idx : idx + n]
                if all(w in _STOPWORDS for w in gram_words):
                    continue
                gram = " ".join(gram_words)
                ngram_total[gram] += 1
                ngram_turns[gram].add(t.turn_index)
                ngram_topics[gram].add(t.topic_index)

        repeats = [
            NgramRepeat(
                ngram=gram,
                n=n,
                count=cnt,
                distinct_turns=len(ngram_turns[gram]),
                distinct_topics=len(ngram_topics[gram]),
            )
            for gram, cnt in ngram_total.items()
            if len(ngram_turns[gram]) >= 2
        ]
        repeats.sort(key=lambda r: (-r.distinct_turns, -r.count, r.ngram))
        summary.repeated_ngrams[n] = repeats[:25]

    # 3. Opening-phrase skeletons
    skel_counts: Counter[str] = Counter()
    skel_topics: dict[str, set[int]] = defaultdict(set)
    skel_refs: dict[str, list[str]] = defaultdict(list)
    for t in turns:
        sents = split_sentences(t.spoken_text)
        if not sents:
            continue
        skel = _classify_opening_skeleton(sents[0])
        if not skel:
            continue
        skel_counts[skel] += 1
        skel_topics[skel].add(t.topic_index)
        skel_refs[skel].append(f"T{t.topic_index}.{t.turn_index}")

    all_skels = [
        OpeningSkeletonMatch(
            skeleton=skel,
            count=cnt,
            distinct_topics=len(skel_topics[skel]),
            turn_refs=skel_refs[skel],
        )
        for skel, cnt in skel_counts.items()
        if cnt >= 2
    ]
    all_skels.sort(key=lambda s: (-s.distinct_topics, -s.count, s.skeleton))
    summary.all_opening_skeletons = all_skels
    summary.cross_topic_opening_skeletons = [s for s in all_skels if s.distinct_topics >= 2]

    return summary


def _analyze_content_and_metaphors(
    turns: list[SpokenTurnRecord],
    *,
    topic_titles_and_angles: Iterable[str],
) -> tuple[list[ContentWordHit], list[MetaphorLoopHit]]:
    title_words: set[str] = set()
    for text in topic_titles_and_angles:
        for w in tokenize_words(text):
            title_words.add(strip_accents(w))

    word_counts: Counter[str] = Counter()
    word_turns: dict[str, set[int]] = defaultdict(set)
    fam_counts: Counter[str] = Counter()
    fam_turns: dict[str, set[int]] = defaultdict(set)
    fam_topics: dict[str, set[int]] = defaultdict(set)
    fam_terms: dict[str, Counter[str]] = defaultdict(Counter)

    for t in turns:
        for w in tokenize_words(t.spoken_text):
            norm_w = strip_accents(w)
            if len(norm_w) < 4 or norm_w in _STOPWORDS or norm_w in title_words:
                continue
            word_counts[norm_w] += 1
            word_turns[norm_w].add(t.turn_index)
            fam = _METAPHOR_TOKEN_TO_FAMILY.get(norm_w)
            if fam:
                fam_counts[fam] += 1
                fam_turns[fam].add(t.turn_index)
                fam_topics[fam].add(t.topic_index)
                fam_terms[fam][norm_w] += 1

    top_words = [
        ContentWordHit(word=w, count=cnt, distinct_turns=len(word_turns[w]))
        for w, cnt in word_counts.most_common(20)
        if len(word_turns[w]) >= 2
    ]

    metaphors = [
        MetaphorLoopHit(
            family=fam,
            count=cnt,
            distinct_turns=len(fam_turns[fam]),
            distinct_topics=len(fam_topics[fam]),
            terms_seen=dict(fam_terms[fam].most_common()),
        )
        for fam, cnt in fam_counts.items()
        if cnt >= 2 or len(fam_turns[fam]) >= 2
    ]
    metaphors.sort(key=lambda m: (-m.distinct_turns, -m.count, m.family))
    return top_words, metaphors


# ---------------------------------------------------------------------------
# Run Assembly & Splitting
# ---------------------------------------------------------------------------


def _resolve_run_label(variant: str, model: str, idx: int, explicit_label: Optional[str]) -> str:
    if explicit_label:
        return explicit_label
    short_model = model.split("/")[-1].split(":")[0] if model else "unknown"
    return f"run{idx}_{variant}_{short_model}"


def _build_run_analysis(
    *,
    label: str,
    explicit_variant: Optional[str],
    log_dir: Path,
    agenda_events: list[dict[str, Any]],
    log_ctx: _ParsedLogContext,
    trace_by_turn: dict[str, dict[str, Any]],
    enforce_time_window: bool = False,
) -> RunAnalysis:
    spoken_turns: list[SpokenTurnRecord] = []
    topic_index = 0
    current_topic_title = ""
    current_angle = ""
    seen_topic_titles: list[str] = []

    fg_guard_events: list[GuardEventRecord] = []
    pregen_rejected_all: list[AttemptRecord] = []
    seen_pregen_ids: set[str] = set()

    for turn_idx, ev in enumerate(agenda_events):
        tid = str(ev.get("tid", ""))
        source = str(ev.get("source", "kira-agenda"))
        served_pregen = bool(ev.get("events", {}).get("served_pregen"))

        # Strict tid / pregen-slot join
        fg_attempt_ids = log_ctx.tid_attempts.get(tid, [])
        accepted_hex = ""
        tid_rejected: list[AttemptRecord] = []
        pregen_rejected: list[AttemptRecord] = []

        if served_pregen and tid in log_ctx.tid_served_pregen:
            accepted_hex = log_ctx.tid_served_pregen[tid]
            pregen_rejected = list(log_ctx.pregen_rejections_for_accepted.get(accepted_hex, []))
        elif fg_attempt_ids:
            # All attempts except the last (or any marked rejected) are rejected foreground attempts
            for att_hex in fg_attempt_ids:
                if att_hex in log_ctx.all_rejected_attempts:
                    tid_rejected.append(log_ctx.all_rejected_attempts[att_hex])
                else:
                    accepted_hex = att_hex
            if not accepted_hex:
                accepted_hex = fg_attempt_ids[-1]

        trace = trace_by_turn.get(accepted_hex, {}) if accepted_hex else {}
        meta = extract_prompt_metadata(trace.get("messages", [])) if trace else {}
        topic_title = meta.get("topic_title") or current_topic_title or "Unknown Topic"
        angle = meta.get("angle") or current_angle

        if topic_index == 0 or (meta.get("topic_title") and meta["topic_title"] != current_topic_title):
            topic_index += 1
            current_topic_title = topic_title
            current_angle = angle
            seen_topic_titles.append(topic_title)
            if angle:
                seen_topic_titles.append(angle)

        spoken_text = str(trace.get("reply") or "")
        model = (
            str(ev.get("model") or "")
            or log_ctx.tid_model_override.get(tid, "")
            or (log_ctx.trace_model_override.get(accepted_hex, "") if accepted_hex else "")
            or str(trace.get("model") or "")
        )
        outcome = str(ev.get("outcome") or "completed")
        latency_outcome = str(ev.get("latency_outcome") or ("served_pregen" if served_pregen else "ok"))

        # Guard events strictly attributed to THIS tid
        turn_guards: list[GuardEventRecord] = []
        logged_guards = log_ctx.tid_guard_logs.get(tid, [])
        for stage, code, detail in logged_guards:
            rec = GuardEventRecord(
                tid=tid,
                turn_index=turn_idx,
                topic_index=topic_index,
                stage=stage,
                reason=code,
                attempt_turn_id=tid_rejected[-1].attempt_turn_id if tid_rejected else "",
                outcome="fallback" if latency_outcome == "fallback" else "retried_and_spoken",
            )
            turn_guards.append(rec)
            fg_guard_events.append(rec)

        # Also respect turn_events.guards if backend.log didn't carry a detailed reason line
        te_guards = [str(g) for g in ev.get("guards", []) if g]
        if te_guards and not logged_guards:
            for g in te_guards:
                rec = GuardEventRecord(
                    tid=tid,
                    turn_index=turn_idx,
                    topic_index=topic_index,
                    stage="turn_state",
                    reason=g,
                    outcome="retried_and_spoken" if latency_outcome == "retried" else latency_outcome,
                )
                turn_guards.append(rec)
                fg_guard_events.append(rec)

        for rej in pregen_rejected:
            if rej.attempt_turn_id not in seen_pregen_ids:
                seen_pregen_ids.add(rej.attempt_turn_id)
                pregen_rejected_all.append(rej)

        was_retried_fg = bool(tid_rejected) or ("guard_retry" in te_guards) or (latency_outcome == "retried")
        spoken_blocked = (
            outcome in ("failed", "refused", "dropped")
            or latency_outcome == "fallback"
            or (bool(turn_guards) and not was_retried_fg and not spoken_text)
        )

        timings = ev.get("timings") or {}
        duration_s = log_ctx.tid_speech_s.get(tid)
        if duration_s is None and timings.get("engine_ms") is not None and outcome == "completed":
            # Fallback only when speech timestamps are absent
            duration_s = None

        spoken_turns.append(
            SpokenTurnRecord(
                tid=tid,
                turn_index=turn_idx,
                topic_index=topic_index,
                topic_title=topic_title,
                angle=angle,
                source=source,
                model=model,
                variant_hint=meta.get("variant_hint", ""),
                served_pregen=served_pregen,
                accepted_attempt_turn_id=accepted_hex,
                move_text=meta.get("move", ""),
                facet_text=meta.get("facet", ""),
                spoken_text=spoken_text,
                word_count=len(tokenize_words(spoken_text)),
                sentence_count=len(split_sentences(spoken_text)),
                char_count=len(spoken_text),
                duration_s=duration_s,
                llm_ms=timings.get("llm_ms"),
                engine_ms=timings.get("engine_ms"),
                outcome=outcome,
                latency_outcome=latency_outcome,
                spoken_turn_blocked=spoken_blocked,
                was_retried_foreground=was_retried_fg,
                tid_guard_events=turn_guards,
                tid_rejected_attempts=tid_rejected,
                pregen_rejected_attempts=pregen_rejected,
            )
        )

    # Also include any evicted/orphaned pregen rejections whose timestamps fall within this run
    run_rejected_attempts: list[AttemptRecord] = []
    seen_rej_ids: set[str] = set()
    for t in spoken_turns:
        for rej in [*t.tid_rejected_attempts, *t.pregen_rejected_attempts]:
            if rej.attempt_turn_id not in seen_rej_ids:
                seen_rej_ids.add(rej.attempt_turn_id)
                run_rejected_attempts.append(rej)

    # Include evicted/orphan pregen rejections ONLY when their trace falls within this run
    run_tids = {t.tid for t in spoken_turns}
    run_ts_ms = [int(e["ts"]) for e in agenda_events if e.get("ts")]
    min_run_ms = min(run_ts_ms) - 180_000 if (enforce_time_window and run_ts_ms) else None
    max_run_ms = max(run_ts_ms) + 5_000 if (enforce_time_window and run_ts_ms) else None
    for att_hex, rej in log_ctx.all_rejected_attempts.items():
        if att_hex in seen_rej_ids:
            continue
        if rej.tid:
            if rej.tid in run_tids:
                seen_rej_ids.add(att_hex)
                run_rejected_attempts.append(rej)
            continue
        if att_hex not in trace_by_turn:
            continue
        if min_run_ms is not None and max_run_ms is not None and rej.ts:
            try:
                rej_ms = int(datetime.fromisoformat(rej.ts).timestamp() * 1000)
            except ValueError:
                rej_ms = None
            if rej_ms is not None and not (min_run_ms <= rej_ms <= max_run_ms):
                continue
        seen_rej_ids.add(att_hex)
        run_rejected_attempts.append(rej)

    # Variant resolution
    detected_variants = [t.variant_hint for t in spoken_turns if t.variant_hint and t.variant_hint != "off"]
    inferred_variant = (
        explicit_variant
        or log_ctx.variant_from_logs
        or (Counter(detected_variants).most_common(1)[0][0] if detected_variants else "off")
    )
    dominant_model = Counter(t.model for t in spoken_turns if t.model).most_common(1)[0][0] if spoken_turns else ""

    # Per-topic summaries
    topics_grouped: dict[int, list[SpokenTurnRecord]] = defaultdict(list)
    for t in spoken_turns:
        topics_grouped[t.topic_index].append(t)

    topic_summaries: list[TopicSummary] = []
    for top_idx in sorted(topics_grouped):
        t_turns = topics_grouped[top_idx]
        t_rep = _analyze_repetitions(t_turns)
        t_words, t_metaphors = _analyze_content_and_metaphors(
            t_turns,
            topic_titles_and_angles=[t_turns[0].topic_title, t_turns[0].angle],
        )
        t_durs = [t.duration_s for t in t_turns if t.duration_s is not None]
        topic_summaries.append(
            TopicSummary(
                topic_index=top_idx,
                title=t_turns[0].topic_title,
                angle=t_turns[0].angle,
                turn_count=len(t_turns),
                total_duration_s=round(sum(t_durs), 2),
                word_stats=summarize_numbers(t.word_count for t in t_turns),
                sentence_stats=summarize_numbers(t.sentence_count for t in t_turns),
                char_stats=summarize_numbers(t.char_count for t in t_turns),
                duration_stats=summarize_numbers(t_durs),
                exact_dup_sentences=t_rep.exact_duplicate_sentences_within_topic,
                near_dup_sentences=t_rep.near_duplicate_sentences_within_topic,
                spoken_turns_blocked=sum(1 for t in t_turns if t.spoken_turn_blocked),
                foreground_retried_turns=sum(1 for t in t_turns if t.was_retried_foreground),
                pregen_rejections=sum(len(t.pregen_rejected_attempts) for t in t_turns),
                top_content_words=t_words,
                metaphor_loops=t_metaphors,
            )
        )

    # Guard summary
    fg_by_reason: Counter[str] = Counter()
    for g in fg_guard_events:
        fg_by_reason[g.reason] += 1

    pregen_by_reason: Counter[str] = Counter()
    all_pregen_rejects = [r for r in run_rejected_attempts if r.stage.startswith("pregen")]
    for r in all_pregen_rejects:
        pregen_by_reason[r.guard_code] += 1

    guard_summary = GuardSummary(
        spoken_turns_blocked=sum(1 for t in spoken_turns if t.spoken_turn_blocked),
        foreground_retried_turns=sum(1 for t in spoken_turns if t.was_retried_foreground),
        foreground_guards_by_reason=dict(fg_by_reason),
        pregen_rejections_total=len(all_pregen_rejects),
        pregen_rejections_by_reason=dict(pregen_by_reason),
        foreground_guard_events=fg_guard_events,
        pregen_rejected_attempts=all_pregen_rejects,
    )

    rep_summary = _analyze_repetitions(spoken_turns)
    top_content, metaphors = _analyze_content_and_metaphors(
        spoken_turns, topic_titles_and_angles=seen_topic_titles
    )
    all_durs = [t.duration_s for t in spoken_turns if t.duration_s is not None]
    all_llm_ms = [t.llm_ms for t in spoken_turns if t.llm_ms is not None]

    return RunAnalysis(
        label=label,
        variant=inferred_variant,
        model=dominant_model,
        log_dir=str(log_dir),
        spoken_turns=spoken_turns,
        rejected_attempts=run_rejected_attempts,
        topics=topic_summaries,
        word_stats=summarize_numbers(t.word_count for t in spoken_turns),
        sentence_stats=summarize_numbers(t.sentence_count for t in spoken_turns),
        char_stats=summarize_numbers(t.char_count for t in spoken_turns),
        duration_stats=summarize_numbers(all_durs),
        llm_ms_stats=summarize_numbers(all_llm_ms),
        total_duration_s=round(sum(all_durs), 2),
        guard_summary=guard_summary,
        repetition=rep_summary,
        top_content_words=top_content,
        metaphor_loops=metaphors,
    )


def _split_agenda_events(events: list[dict[str, Any]], gap_seconds: int = 180) -> list[list[dict[str, Any]]]:
    """Split a combined turn_events list into distinct runs on model change or large idle gap."""
    if not events:
        return []
    groups: list[list[dict[str, Any]]] = [[events[0]]]
    prev_model = str(events[0].get("model") or "")
    prev_ts = int(events[0].get("ts") or 0)
    for ev in events[1:]:
        cur_model = str(ev.get("model") or "")
        cur_ts = int(ev.get("ts") or 0)
        gap_s = (cur_ts - prev_ts) / 1000.0 if (cur_ts and prev_ts) else 0.0
        model_changed = bool(cur_model and prev_model and cur_model != prev_model)
        if model_changed or gap_s > gap_seconds:
            groups.append([ev])
        else:
            groups[-1].append(ev)
        if cur_model:
            prev_model = cur_model
        if cur_ts:
            prev_ts = cur_ts
    return groups


def analyze_log_dir(
    log_dir: str | Path,
    *,
    label: Optional[str] = None,
    variant: Optional[str] = None,
    dates: Optional[set[str]] = None,
    split_runs: bool = False,
) -> list[RunAnalysis]:
    """Analyze an OpenCohost log directory offline and return one or more RunAnalysis objects."""
    root = Path(log_dir)
    all_events = _load_turn_events(root)
    agenda_events = [e for e in all_events if str(e.get("source", "")) in AGENDA_SOURCES]

    # Auto-infer dates from turn_events timestamps when not explicitly provided
    if dates is None and agenda_events:
        inferred_dates = {
            datetime.fromtimestamp(int(e["ts"]) / 1000.0).strftime("%Y%m%d")
            for e in agenda_events
            if e.get("ts")
        }
        dates = inferred_dates or None

    trace_by_turn, _ordered_traces = _load_prompt_traces(root, dates=dates)
    log_ctx = _parse_session_logs(_find_session_logs(root, dates=dates), trace_by_turn)

    if not agenda_events:
        return [
            _build_run_analysis(
                label=label or root.name,
                explicit_variant=variant,
                log_dir=root,
                agenda_events=[],
                log_ctx=log_ctx,
                trace_by_turn=trace_by_turn,
            )
        ]

    groups = _split_agenda_events(agenda_events) if split_runs else [agenda_events]
    runs: list[RunAnalysis] = []
    for idx, grp in enumerate(groups, start=1):
        grp_model = str(grp[0].get("model") or "")
        run_label = label if (label and len(groups) == 1) else (
            f"{label}_{idx}" if label else _resolve_run_label(variant or "auto", grp_model, idx, None)
        )
        run = _build_run_analysis(
            label=run_label,
            explicit_variant=variant,
            log_dir=root,
            agenda_events=grp,
            log_ctx=log_ctx,
            trace_by_turn=trace_by_turn,
            enforce_time_window=len(groups) > 1,
        )
        if label is None and variant is None:
            short_m = run.model.split("/")[-1].split(":")[0] if run.model else "unknown"
            run.label = f"run{idx}_{run.variant}_{short_m}"
        runs.append(run)
    return runs


def compare_runs(runs: list[RunAnalysis]) -> dict[str, Any]:
    """Produce a serializable comparison dictionary across one or more RunAnalysis objects."""
    return {
        "schema_version": 1,
        "offline": True,
        "runs": [asdict(r) for r in runs],
    }


# ---------------------------------------------------------------------------
# Renderers: JSON, CLI Table, Markdown, Self-Contained HTML
# ---------------------------------------------------------------------------


def render_json_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def _fmt_num(val: float) -> str:
    return f"{val:.1f}" if isinstance(val, float) else str(val)


def render_cli_table(report: dict[str, Any]) -> str:
    runs = report.get("runs", [])
    if not runs:
        return "No agenda runs found."

    headers = [
        "Run",
        "Variant",
        "Model",
        "Turns",
        "Words(mean/p90)",
        "Sents(mean)",
        "Chars(mean)",
        "Dur(s/turn|tot)",
        "DupSent(in/cross)",
        "Skel(cross)",
        "MetaLoops",
        "SpokenBlk/FgRetry/PregenRej",
    ]
    rows: list[list[str]] = []
    for r in runs:
        ws = r["word_stats"]
        ss = r["sentence_stats"]
        cs = r["char_stats"]
        ds = r["duration_stats"]
        rep = r["repetition"]
        gs = r["guard_summary"]
        dup_in = rep["exact_duplicate_sentences_within_topic"] + rep["near_duplicate_sentences_within_topic"]
        dup_cross = rep["exact_duplicate_sentences_across_topics"] + rep["near_duplicate_sentences_across_topics"]
        short_model = (r.get("model") or "-").split("/")[-1][:24]
        rows.append([
            str(r["label"]),
            str(r["variant"]),
            short_model,
            str(len(r["spoken_turns"])),
            f"{_fmt_num(ws['mean'])}/{_fmt_num(ws['p90'])}",
            _fmt_num(ss["mean"]),
            _fmt_num(cs["mean"]),
            f"{_fmt_num(ds['mean'])}s | {_fmt_num(r['total_duration_s'])}s",
            f"{dup_in}/{dup_cross}",
            str(len(rep["cross_topic_opening_skeletons"])),
            str(sum(m["count"] for m in r["metaphor_loops"])),
            f"{gs['spoken_turns_blocked']}/{gs['foreground_retried_turns']}/{gs['pregen_rejections_total']}",
        ])

    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    sep = " | "
    lines = [
        sep.join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "-+-".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in rows:
        lines.append(sep.join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def render_markdown_report(report: dict[str, Any]) -> str:
    runs = report.get("runs", [])
    lines: list[str] = [
        "# OpenCohost Agenda Deck Benchmark Report",
        "",
        "## Side-by-Side Run Comparison",
        "",
        "| Run | Variant | Model | Turns | Words (mean / med / min-max / p90) | Sentences (mean) | Chars (mean) | Turn Dur (mean) | Total Dur | Dup Sents (in / cross) | Cross-Topic Skeletons | Metaphor Hits | Spoken Blocked | Fg Retried | Pregen Rejected |",
        "|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in runs:
        ws = r["word_stats"]
        ss = r["sentence_stats"]
        cs = r["char_stats"]
        ds = r["duration_stats"]
        rep = r["repetition"]
        gs = r["guard_summary"]
        dup_in = rep["exact_duplicate_sentences_within_topic"] + rep["near_duplicate_sentences_within_topic"]
        dup_cross = rep["exact_duplicate_sentences_across_topics"] + rep["near_duplicate_sentences_across_topics"]
        short_model = (r.get("model") or "-").split("/")[-1]
        lines.append(
            f"| {r['label']} | `{r['variant']}` | `{short_model}` | {len(r['spoken_turns'])} "
            f"| {ws['mean']} / {ws['median']} / {ws['min']:.0f}-{ws['max']:.0f} / {ws['p90']} "
            f"| {ss['mean']} | {cs['mean']} | {ds['mean']}s | {r['total_duration_s']}s "
            f"| {dup_in} / {dup_cross} | {len(rep['cross_topic_opening_skeletons'])} "
            f"| {sum(m['count'] for m in r['metaphor_loops'])} "
            f"| {gs['spoken_turns_blocked']} | {gs['foreground_retried_turns']} | {gs['pregen_rejections_total']} |"
        )

    for r in runs:
        lines.extend(["", f"## Run `{r['label']}` (`{r['variant']}` · `{r.get('model') or '-'}`)", ""])
        if r["topics"]:
            lines.extend([
                "### Per-Topic Breakdown",
                "",
                "| # | Topic | Turns | Total Dur | Words (mean/p90) | Dup Sents (exact/near) | Fg Retries | Pregen Rej |",
                "|---:|---|---:|---:|---|---:|---:|---:|",
            ])
            for t in r["topics"]:
                tw = t["word_stats"]
                lines.append(
                    f"| {t['topic_index']} | {t['title']} | {t['turn_count']} | {t['total_duration_s']}s "
                    f"| {tw['mean']}/{tw['p90']} | {t['exact_dup_sentences']}/{t['near_dup_sentences']} "
                    f"| {t['foreground_retried_turns']} | {t['pregen_rejections']} |"
                )

        gs = r["guard_summary"]
        lines.extend(["", "### Guardrail Attribution (Strict `tid` & Pregen-Slot Lineage)", ""])
        if gs["foreground_guard_events"]:
            lines.append("- **Foreground `tid` guard events:**")
            for g in gs["foreground_guard_events"]:
                lines.append(
                    f"  - `tid={g['tid']}` (turn #{g['turn_index']}, topic #{g['topic_index']}): "
                    f"`{g['reason']}` via `{g['stage']}` -> `{g['outcome']}` (attempt `{g['attempt_turn_id'] or '-'}`)"
                )
        else:
            lines.append("- **Foreground `tid` guard events:** 0")

        if gs["pregen_rejected_attempts"]:
            lines.append("- **Pre-turn background pregen rejections (NOT spoken turn blocks):**")
            for p in gs["pregen_rejected_attempts"]:
                lines.append(
                    f"  - attempt `{p['attempt_turn_id']}`: `{p['guard_code']}` (`{p['stage']}`)"
                )
        else:
            lines.append("- **Pre-turn background pregen rejections:** 0")

        rep = r["repetition"]
        if rep["cross_topic_opening_skeletons"]:
            lines.extend(["", "### Cross-Topic Opening Skeletons", ""])
            for s in rep["cross_topic_opening_skeletons"]:
                lines.append(
                    f"- `{s['skeleton']}` — {s['count']} turns across {s['distinct_topics']} topics ({', '.join(s['turn_refs'])})"
                )

        if r["metaphor_loops"]:
            lines.extend(["", "### Metaphor / Cliché Loops", ""])
            for m in r["metaphor_loops"][:10]:
                terms = ", ".join(f"{k}×{v}" for k, v in m["terms_seen"].items())
                lines.append(
                    f"- `{m['family']}`: {m['count']} hits across {m['distinct_turns']} turns ({terms})"
                )

    return "\n".join(lines) + "\n"


def render_html_report(report: dict[str, Any]) -> str:
    """Render a self-contained offline HTML comparison report with strict tid badges."""
    runs = report.get("runs", [])
    safe_runs_json = json.dumps(runs, ensure_ascii=False).replace("</", "<\\/")

    metric_cards = []
    for r in runs:
        ws = r["word_stats"]
        ds = r["duration_stats"]
        gs = r["guard_summary"]
        rep = r["repetition"]
        dup_tot = (
            rep["exact_duplicate_sentences_within_topic"]
            + rep["exact_duplicate_sentences_across_topics"]
            + rep["near_duplicate_sentences_within_topic"]
            + rep["near_duplicate_sentences_across_topics"]
        )
        metric_cards.append(
            f"""<div class="metric-card">
              <div class="metric-title">{html.escape(str(r['label']))} · <span class="badge">{html.escape(str(r['variant']))}</span></div>
              <div class="metric-row"><span>Model</span><span>{html.escape((r.get('model') or '-').split('/')[-1])}</span></div>
              <div class="metric-row"><span>Words / turn (mean / p90)</span><span>{ws['mean']} / {ws['p90']}</span></div>
              <div class="metric-row"><span>Duration (turn / total)</span><span>{ds['mean']}s / {r['total_duration_s']}s</span></div>
              <div class="metric-row"><span>Dup sentences (exact+near)</span><span>{dup_tot}</span></div>
              <div class="metric-row"><span>Cross-topic skeletons</span><span>{len(rep['cross_topic_opening_skeletons'])}</span></div>
              <div class="metric-row"><span>Spoken blocked / Fg retry / Pregen rej</span><span>{gs['spoken_turns_blocked']} / {gs['foreground_retried_turns']} / {gs['pregen_rejections_total']}</span></div>
            </div>"""
        )

    columns_html = []
    for r in runs:
        cards_html = []
        for t in r["spoken_turns"]:
            guard_badges = []
            if t["spoken_turn_blocked"]:
                guard_badges.append('<span class="tag tag-warn">SPOKEN BLOCKED</span>')
            if t["was_retried_foreground"]:
                reasons = ", ".join(g["reason"] for g in t["tid_guard_events"]) or "guard_retry"
                guard_badges.append(
                    f'<span class="tag tag-retry">FG RETRY ({html.escape(reasons)})</span>'
                )
            if t["pregen_rejected_attempts"]:
                pre_reasons = ", ".join(p["guard_code"] for p in t["pregen_rejected_attempts"])
                guard_badges.append(
                    f'<span class="tag tag-pregen">PREGEN RETRIED BEFORE SPEAK ({html.escape(pre_reasons)})</span>'
                )

            card_cls = "turn-card"
            if t["spoken_turn_blocked"]:
                card_cls += " guardrail"
            elif t["was_retried_foreground"]:
                card_cls += " retried"

            dur_str = f"{t['duration_s']:.1f}s" if t["duration_s"] is not None else "-"
            cards_html.append(
                f"""<div class="{card_cls}" data-topic="{t['topic_index']}">
                  <div class="turn-meta">
                    <span>#{t['turn_index']+1} · Topic {t['topic_index']}: {html.escape(t['topic_title'])} · <code>{html.escape(t['tid'])}</code></span>
                    <span>{t['word_count']}w · {t['sentence_count']}s · {dur_str}</span>
                  </div>
                  <div class="badges">{' '.join(guard_badges)}</div>
                  <div class="turn-text">{html.escape(t['spoken_text'])}</div>
                </div>"""
            )
        columns_html.append(
            f"""<div class="column">
              <div class="col-banner">
                <h2>{html.escape(str(r['label']))} ({html.escape(str(r['variant']))})</h2>
                <p>{html.escape(str(r.get('model') or '-'))}</p>
              </div>
              {''.join(cards_html)}
            </div>"""
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>OpenCohost · Agenda Deck Benchmark Comparison</title>
  <style>
    :root {{ --bg:#0d1117; --surface:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --info:#58a6ff; --warn:#f85149; --retry:#d29922; --ok:#3fb950; }}
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ font-family:system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); padding:20px; line-height:1.5; }}
    .container {{ max-width:1700px; margin:0 auto; }}
    header {{ border-bottom:1px solid var(--border); padding-bottom:14px; margin-bottom:18px; }}
    h1 {{ font-size:1.35rem; }}
    .subtitle {{ color:var(--muted); font-size:0.86rem; margin-top:4px; }}
    .metrics-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(270px,1fr)); gap:12px; margin-bottom:20px; }}
    .metric-card, .col-banner, .turn-card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px 14px; }}
    .metric-title {{ font-weight:600; margin-bottom:8px; color:var(--info); }}
    .metric-row {{ display:flex; justify-content:space-between; font-size:0.84rem; margin-bottom:4px; }}
    .comparison-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:14px; align-items:start; }}
    .column {{ display:flex; flex-direction:column; gap:10px; }}
    .turn-card.guardrail {{ border-color:var(--warn); }}
    .turn-card.retried {{ border-color:var(--retry); }}
    .turn-meta {{ display:flex; justify-content:space-between; gap:8px; font-size:0.74rem; color:var(--muted); border-bottom:1px solid var(--border); padding-bottom:6px; margin-bottom:6px; }}
    .badges {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:6px; }}
    .tag {{ padding:2px 6px; border-radius:4px; font-size:0.7rem; font-weight:600; }}
    .tag-warn {{ background:rgba(248,81,73,.2); color:var(--warn); }}
    .tag-retry {{ background:rgba(210,153,34,.2); color:var(--retry); }}
    .tag-pregen {{ background:rgba(88,166,255,.18); color:var(--info); }}
    .badge {{ background:rgba(63,185,80,.18); color:var(--ok); padding:2px 6px; border-radius:4px; font-size:0.75rem; }}
    .turn-text {{ font-size:0.87rem; white-space:pre-line; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>OpenCohost · Agenda Deck Benchmark Comparison</h1>
      <p class="subtitle">Offline tid-joined analysis (pre-turn pregen retries separated from spoken turn status; raw viewer chat excluded)</p>
    </header>
    <section class="metrics-grid">{''.join(metric_cards)}</section>
    <section class="comparison-grid">{''.join(columns_html)}</section>
  </div>
  <script id="benchmark-data" type="application/json">{safe_runs_json}</script>
</body>
</html>
"""
