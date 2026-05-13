import re
import time
from collections import Counter, defaultdict, deque
from typing import Any


class IntentClassifier:
    """Rule-based chat intent classifier.

    This is intentionally deterministic and cheap.  It turns high-volume chat
    into broad intent buckets before the LLM sees it.
    """

    INTENT_LABELS = {
        "greeting_request": "saludos, cumpleaños y pedidos de atención",
        "join_request": "pedidos para jugar o unirse al servidor",
        "roblox_friend": "solicitudes de amistad/agregar en Roblox",
        "game_question": "preguntas sobre el juego o cómo unirse",
        "video_collab": "pedidos de videos o colaboraciones",
        "game_suggestion": "sugerencias de juegos",
        "trade_request": "tradeos, regalos o valores de items",
        "creator_feedback": "opiniones, críticas o reacciones al streamer/juego",
        "copypasta": "copypasta o frase repetida por el chat",
        "other": "otros temas frecuentes",
    }

    _PATTERNS = {
        "greeting_request": [
            r"\bsal[uú]dame\b", r"\bme saludas\b", r"\bsaludos?\b",
            r"\bfeliz cumple", r"\bcumplea", r"\bmi sue[nñ]o\b",
            r"\bsoy (tu )?fan\b", r"\byoutuber favorito\b",
        ],
        "join_request": [
            r"\bpuedo jugar\b", r"\bpuedo participar\b", r"\bme puedo (meter|unir)\b",
            r"\bme aceptas\b", r"\baceptas soli\b", r"\bme uno\b",
            r"\bc[oó]mo me uno\b", r"\bserver (privado|p[uú]blico)\b",
            r"\bjugar contigo\b", r"\bjugar con vos\b",
        ],
        "roblox_friend": [
            r"\bagregame\b", r"\bagr[eé]game\b", r"\ba[nñ]ad", r"\bsoli\b",
            r"\bsolicitud\b", r"\bamigo en roblox\b", r"\bmanda(s)? soli\b",
        ],
        "game_question": [
            r"\bc[oó]mo se llama el juego\b", r"\bcomo se llama el juego\b",
            r"\bqu[eé] juego\b", r"\bcu[aá]l es el juego\b", r"\btu id\b",
            r"\bc[oó]mo (entro|me meto|me uno)\b", r"\bpor qu[eé]\b",
            r"\bcu[aá]ndo\b", r"\bd[oó]nde\b",
        ],
        "video_collab": [
            r"\b(video|v[ií]deo)\s+con\b",
            r"\bcolab(oraci[oó]n|ora|orar|oraci[oó]nes)?\s+con\b",
            r"\bconoces\s+a\b",
            r"\b(cu[aá]ndo|cuando)\s+(vuelve(n)?|regresa(n)?)\b",
            r"\bpa\s+(cu[aá]ndo|cuando)\s+(un\s+)?(video|v[ií]deo)\b",
        ],
        "game_suggestion": [
            r"\bjuega\b", r"\bjuego de terror\b", r"\brecomiendo\s+(que\s+)?(juegues|probar)\b",
            r"\bdeber[ií]as\s+jugar\b",
        ],
        "trade_request": [
            r"\btrade", r"\btradeas\b", r"\bregalar\b", r"\bte doy\b",
            r"\bmill[oó]n", r"\b\d+\s*m\b", r"\bvalor\b", r"\bog\b",
        ],
        "creator_feedback": [
            r"\bte amo\b", r"\bte quiero\b", r"\beres el mejor\b",
            r"\bno lee\b", r"\bpor qu[eé] haces en vivo\b", r"\bbodrio\b",
            r"\bme da asco\b", r"\basco\b", r"\bcrack\b",
        ],
        "copypasta": [
            r"\badmin abuse\b", r"\bdigan 67\b", r"\bspam al creador\b",
        ],
    }

    _PRIORITY = [
        "copypasta",
        "video_collab",
        "roblox_friend",
        "join_request",
        "greeting_request",
        "trade_request",
        "game_suggestion",
        "game_question",
        "creator_feedback",
    ]

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._compiled = {
            intent: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
            for intent, patterns in self._PATTERNS.items()
        }

    def classify(self, text: str) -> str:
        return self.classify_message(text)["intent"]

    def classify_message(self, text: str) -> dict[str, Any]:
        normalized = self._normalize(text)
        if self._looks_like_copypasta(normalized):
            return {"intent": "copypasta", "entity": None}
        for intent in self._PRIORITY:
            patterns = self._compiled.get(intent, [])
            if any(pattern.search(normalized) for pattern in patterns):
                return {"intent": intent, "entity": self.extract_entity(text, intent)}
        return {"intent": "other", "entity": self.extract_entity(text, "other")}

    def label(self, intent: str) -> str:
        return self.INTENT_LABELS.get(intent, self.INTENT_LABELS["other"])

    def extract_entity(self, text: str, intent: str) -> str | None:
        """Extract runtime entities without hardcoding streamer/collab names."""
        normalized = self._normalize(text)
        patterns_by_intent = {
            "video_collab": [
                r"(?:video|v[ií]deo)\s+con\s+(.+)",
                r"colab(?:oraci[oó]n|ora|orar|oraci[oó]nes)?\s+con\s+(.+)",
                r"conoces\s+a\s+(.+)",
                r"(?:cu[aá]ndo|cuando)\s+(?:vuelve(?:n)?|regresa(?:n)?)\s+(.+)",
            ],
            "game_suggestion": [
                r"juega\s+(.+)",
                r"juego\s+de\s+(.+)",
                r"recomiendo\s+(?:que\s+)?(?:juegues|probar)\s+(.+)",
                r"deber[ií]as\s+jugar\s+(.+)",
            ],
            "trade_request": [
                r"tradeas\s+por\s+(.+)",
                r"(?:te\s+doy|regalar)\s+(.+)",
            ],
        }
        for pattern in patterns_by_intent.get(intent, []):
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                return self._clean_entity(match.group(1))
        return self._frequent_term_entity(normalized)

    @staticmethod
    def _clean_entity(value: str) -> str | None:
        value = re.split(r"\b(porfa|porfavor|por favor|pls|plis|please|cuando|cu[aá]ndo)\b", value)[0]
        value = " ".join(value.strip(" .,;:!?¡¿\"'").split())
        if not value or len(value) < 3:
            return None
        words = value.split()[:5]
        return " ".join(words)

    @staticmethod
    def _frequent_term_entity(normalized: str) -> str | None:
        stopwords = {
            "abraham", "abram", "habraham", "abrahaham", "kira", "hola",
            "por", "favor", "porfa", "plis", "please", "puedo", "quiero",
            "jugar", "juego", "video", "cuando", "cuándo", "como", "cómo",
            "que", "qué", "con", "para", "soy", "fan", "me", "mi", "tu",
            "el", "la", "los", "las", "un", "una", "de", "del", "y", "en",
        }
        candidates = [
            word for word in normalized.split()
            if len(word) >= 4 and word not in stopwords and not word.isdigit()
        ]
        if not candidates:
            return None
        return Counter(candidates).most_common(1)[0][0]

    @staticmethod
    def _normalize(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^\wáéíóúüñ\s¿?]+", " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())

    @staticmethod
    def _looks_like_copypasta(text: str) -> bool:
        words = text.split()
        if len(words) < 8:
            return False
        unique_ratio = len(set(words)) / len(words)
        return unique_ratio < 0.45


class IntentAggregator:
    """Keeps a rolling window of classified chat intents and builds summaries."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.window_seconds = float(self.config.get("window_seconds", 60))
        self.max_examples_per_intent = int(self.config.get("max_examples_per_intent", 3))
        self.top_intents = int(self.config.get("top_intents", 5))
        self.min_count = int(self.config.get("min_count", 2))
        self.include_other = bool(self.config.get("include_other", False))
        self.classifier = IntentClassifier(self.config)
        self._items: deque[dict[str, Any]] = deque()
        self._seen_texts: dict[str, float] = {}
        self.duplicate_window_seconds = float(self.config.get("duplicate_window_seconds", 45))

    def add_message(self, message: dict) -> dict:
        timestamp = self._coerce_timestamp(message.get("timestamp", time.time()))
        text = message.get("text", "")
        classification = self.classifier.classify_message(text)
        intent = classification["intent"]
        entity = classification.get("entity")
        normalized = IntentClassifier._normalize(text)

        item = {
            "user": message.get("user", ""),
            "text": text,
            "timestamp": timestamp,
            "intent": intent,
            "entity": entity,
            "label": self.classifier.label(intent),
            "duplicate": self._is_duplicate(normalized, timestamp),
        }
        self._items.append(item)
        self._seen_texts[normalized] = timestamp
        self._prune(timestamp)
        return item

    def summarize(self, now: float | None = None) -> dict:
        now = self._coerce_timestamp(now if now is not None else time.time())
        self._prune(now)
        counts = Counter(item["intent"] for item in self._items)
        duplicates = Counter(item["intent"] for item in self._items if item.get("duplicate"))
        entities: dict[str, Counter] = defaultdict(Counter)
        examples: dict[str, list[dict]] = defaultdict(list)

        for item in self._items:
            if item.get("entity"):
                entities[item["intent"]][item["entity"]] += 1
            bucket = examples[item["intent"]]
            if len(bucket) < self.max_examples_per_intent and not item.get("duplicate"):
                bucket.append({"user": item["user"], "text": item["text"], "entity": item.get("entity")})

        ranked = [
            {
                "intent": intent,
                "label": self.classifier.label(intent),
                "count": count,
                "duplicates": duplicates.get(intent, 0),
                "entities": [entity for entity, _ in entities.get(intent, Counter()).most_common(3)],
                "examples": examples.get(intent, []),
            }
            for intent, count in counts.most_common(self.top_intents)
            if count >= self.min_count and (intent != "other" or self.include_other)
        ]

        return {
            "total_messages": len(self._items),
            "top_intents": ranked,
            "prompt": self.to_prompt(ranked),
        }

    def recent_messages(self, max_messages: int = 20) -> list[dict[str, Any]]:
        """Return compact in-memory examples for the current Kira context."""
        items = [item for item in self._items if not item.get("duplicate")]
        return [
            {"user": item.get("user", ""), "text": item.get("text", ""), "timestamp": item.get("timestamp")}
            for item in items[-max_messages:]
        ]

    def to_prompt(self, ranked: list[dict]) -> str:
        if not ranked:
            return "No hay un tema dominante claro en el chat filtrado."

        lines = [
            "CONTEXTO PRIVADO DEL CHAT PARA KIRA (NO LEER LITERAL):",
            "Usar solo como señal interna para improvisar una reacción natural.",
        ]
        for item in ranked:
            entity_note = ""
            if item.get("entities"):
                entity_note = f" Referencias detectadas: {', '.join(item['entities'])}."
            duplicate_note = " Conversación repetitiva." if item.get("duplicates") else ""
            lines.append(f"- Intención dominante: {item['label']}.{entity_note}{duplicate_note}")
        return "\n".join(lines)

    def reset(self) -> None:
        self._items.clear()
        self._seen_texts.clear()

    def _is_duplicate(self, normalized: str, timestamp: float) -> bool:
        if not normalized:
            return False
        previous = self._seen_texts.get(normalized)
        return previous is not None and (timestamp - previous) <= self.duplicate_window_seconds

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._items and self._items[0]["timestamp"] < cutoff:
            self._items.popleft()
        old_seen = [text for text, ts in self._seen_texts.items() if ts < cutoff]
        for text in old_seen:
            self._seen_texts.pop(text, None)

    @staticmethod
    def _coerce_timestamp(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return time.time()
