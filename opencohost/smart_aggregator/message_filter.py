import re
from typing import Optional
from collections import Counter

class MessageFilter:
    def __init__(self, config: dict):
        self.min_words = config.get("min_words", 3)
        self.min_char_length = config.get("min_char_length", 10)
        self.discard_emojis_only = config.get("discard_emojis_only", True)
        self.discard_links = config.get("discard_links", True)
        self.discard_mentions = config.get("discard_mentions", True)

        # Quality filters
        self.discard_repetitive_chars = config.get("discard_repetitive_chars", True)
        self.repetitive_char_threshold = config.get("repetitive_char_threshold", 0.50)
        self.discard_repeated_words = config.get("discard_repeated_words", True)
        self.repeated_word_max = config.get("repeated_word_max", 3)
        self.min_unique_word_ratio = config.get("min_unique_word_ratio", 0.25)
        self.discard_gibberish = config.get("discard_gibberish", True)
        self.min_vowel_ratio = config.get("min_vowel_ratio", 0.10)
        self.min_token_vowel_ratio = config.get("min_token_vowel_ratio", 0.22)
        self.max_token_length = config.get("max_token_length", 24)
        self.max_gibberish_token_ratio = config.get("max_gibberish_token_ratio", 0.35)
        self.discard_ascii_art = config.get("discard_ascii_art", True)

        # Penalty: messages with 1-4 words after filtering get a quality score
        # that the aggregator can use to deprioritize them
        self.short_word_threshold = config.get("short_word_threshold", 4)
        self.min_quality_score = config.get("min_quality_score", 0.3)

        whitelist_cfg = config.get("whitelist", {})
        self.whitelist_enabled = whitelist_cfg.get("enabled", True)
        self.whitelist_users = set(u.lower() for u in whitelist_cfg.get("users", []))

        self._url_pattern = re.compile(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        )
        self._mention_pattern = re.compile(r'@[\w.-]+')
        self._custom_emoji_pattern = re.compile(r':[a-zA-Z0-9_+\-]+:')

        # Box-drawing and ASCII art characters
        self._box_drawing_pattern = re.compile(
            r'[\u2500-\u257F\u2580-\u259F\u25E2-\u25E5\u2B0C-\u2B1B]'
        )

        self._last_rejection_reason: Optional[str] = None

    @staticmethod
    def _sanitize_chat_text(text: str) -> str:
        """Strip ANSI escapes, control chars, and null bytes from chat text."""
        import re
        text = text.replace("\x00", "")
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
        text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
        return " ".join(text.split())

    def filter(self, message: dict) -> Optional[dict]:
        self._last_rejection_reason = None
        user = message.get("user", "")
        text = message.get("text", "")
        timestamp = message.get("timestamp", 0)

        if not isinstance(text, str):
            self._last_rejection_reason = "invalid_type"
            return None

        text_stripped = text.strip()
        if not text_stripped:
            self._last_rejection_reason = "empty"
            return None

        text_stripped = self._sanitize_chat_text(text_stripped)
        text_stripped = text_stripped[:500]
        if not text_stripped:
            self._last_rejection_reason = "empty"
            return None

        if self.whitelist_enabled and user.lower() in self.whitelist_users:
            return {"user": user, "text": text_stripped, "timestamp": timestamp}

        if self.discard_emojis_only and self._has_only_emojis(text_stripped):
            self._last_rejection_reason = "emoji_only"
            return None

        if self.discard_links and self._url_pattern.search(text_stripped):
            self._last_rejection_reason = "link"
            return None

        if self.discard_mentions and self._mention_pattern.search(text_stripped):
            self._last_rejection_reason = "mention"
            return None

        text_clean = self._strip_custom_emojis(text_stripped)
        if not text_clean:
            self._last_rejection_reason = "emoji_only"
            return None

        if len(text_clean) < self.min_char_length:
            self._last_rejection_reason = "too_short_chars"
            return None

        words = text_clean.split()
        if len(words) < self.min_words:
            self._last_rejection_reason = "too_short_words"
            return None

        # --- Quality filters (after basic length check) ---

        if self.discard_ascii_art and self._is_ascii_art(text_clean):
            self._last_rejection_reason = "ascii_art"
            return None

        if self.discard_repetitive_chars and self._has_repetitive_chars(text_clean):
            self._last_rejection_reason = "repetitive_chars"
            return None

        if self.discard_repeated_words and self._has_repeated_words(words):
            self._last_rejection_reason = "repeated_words"
            return None

        if self.discard_gibberish and self._is_gibberish(text_clean):
            self._last_rejection_reason = "gibberish"
            return None

        quality = self._quality_score(words, text_clean)

        return {
            "user": user,
            "text": text_clean,
            "timestamp": timestamp,
            "quality": quality,
        }

    def _quality_score(self, words: list, text: str = "") -> float:
        """Return 0.0-1.0 quality score. Low = noisy, High = meaningful."""
        if not words:
            return 0.0

        score = 1.0

        # Penalize short low-context messages, but allow direct questions.
        if len(words) <= self.short_word_threshold and not self._has_relevance_signal(text, words):
            score *= 0.3

        # Penalize low unique word ratio
        unique_ratio = len(set(w.lower() for w in words)) / len(words)
        if unique_ratio < self.min_unique_word_ratio:
            score *= 0.2
        elif unique_ratio < 0.5:
            score *= 0.5

        return round(score, 2)

    def _has_repetitive_chars(self, text: str) -> bool:
        """Check if a single character dominates the message (>50%)."""
        if len(text) < 5:
            return False
        counts = Counter(text.lower().replace(" ", ""))
        if not counts:
            return False
        top_char, top_count = counts.most_common(1)[0]
        ratio = top_count / len(text.replace(" ", ""))
        return ratio > self.repetitive_char_threshold

    def _has_repeated_words(self, words: list) -> bool:
        """Check if same word appears >N times or unique ratio is very low."""
        if len(words) < 4:
            return False
        lower_words = [w.lower() for w in words]
        counts = Counter(lower_words)
        # Same word appears more than threshold times
        if any(c > self.repeated_word_max for c in counts.values()):
            return True
        # Unique word ratio too low (e.g. "fe fe fe fe fe fe")
        unique_ratio = len(counts) / len(words)
        return unique_ratio < self.min_unique_word_ratio

    def _is_gibberish(self, text: str) -> bool:
        """Detect random keystrokes: low vowel ratio, bad tokens, or consonant noise."""
        if len(text) < 6:
            return False
        vowels = set("aeiouáéíóúüAEIOUÁÉÍÓÚÜ")
        vowel_count = sum(1 for c in text if c in vowels)
        alpha_chars = [c for c in text if c.isalpha()]
        if not alpha_chars:
            return True
        vowel_ratio = vowel_count / len(alpha_chars)
        if vowel_ratio < self.min_vowel_ratio:
            return True

        words = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ']+", text)
        return self._has_gibberish_tokens(words)

    def _has_gibberish_tokens(self, words: list[str]) -> bool:
        """Reject messages dominated by long random-looking tokens."""
        candidates = [w for w in words if len(w) >= 8]
        if not candidates:
            return False

        bad_tokens = [w for w in candidates if self._is_bad_token(w)]
        bad_ratio = len(bad_tokens) / len(candidates)
        bad_chars = sum(len(w) for w in bad_tokens)
        total_chars = sum(len(w) for w in words) or 1

        return (
            bad_ratio > self.max_gibberish_token_ratio
            or bad_chars / total_chars > 0.45
        )

    def _is_bad_token(self, token: str) -> bool:
        """Detect a single long token that looks like keyboard smash/spam."""
        cleaned = re.sub(r"[^\wáéíóúüñÁÉÍÓÚÜÑ]", "", token).lower()
        if len(cleaned) > self.max_token_length:
            return True

        alpha_chars = [c for c in cleaned if c.isalpha()]
        if len(alpha_chars) < 8:
            return False

        vowels = set("aeiouáéíóúü")
        vowel_ratio = sum(1 for c in alpha_chars if c in vowels) / len(alpha_chars)
        if vowel_ratio < self.min_token_vowel_ratio:
            return True

        if self._longest_consonant_run(cleaned) >= 5:
            return True

        return self._has_repeated_ngram(cleaned)

    def _longest_consonant_run(self, text: str) -> int:
        vowels = set("aeiouáéíóúü")
        longest = current = 0
        for ch in text:
            if ch.isalpha() and ch not in vowels:
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return longest

    def _has_repeated_ngram(self, text: str) -> bool:
        if len(text) < 12:
            return False
        for size in (2, 3):
            chunks = [text[i:i + size] for i in range(0, len(text) - size + 1, size)]
            counts = Counter(chunks)
            if counts and counts.most_common(1)[0][1] >= 4:
                return True
        return False

    def _has_relevance_signal(self, text: str, words: list[str]) -> bool:
        if "?" in text or "¿" in text:
            return True
        relevant = {
            "kira", "que", "qué", "como", "cómo", "cuando", "cuándo",
            "donde", "dónde", "porque", "porqué", "juego", "jugando",
            "puedes", "puedo", "ayuda", "explica",
        }
        lower_words = {w.lower().strip(".,!¡¿?;:") for w in words}
        return bool(lower_words & relevant)

    def _is_ascii_art(self, text: str) -> bool:
        """Detect box-drawing characters or ASCII art patterns."""
        if self._box_drawing_pattern.search(text):
            return True
        # Detect lines of repeated characters (e.g. "=======" or "-------")
        if re.search(r'(=|-|_|#|\*){10,}', text):
            return True
        return False
    
    def _has_only_emojis(self, text: str) -> bool:
        text = self._custom_emoji_pattern.sub("", text).strip()
        if not text:
            return True
        for ch in text:
            if ch.isspace():
                continue
            if not self._is_emoji_char(ch):
                return False
        return True
    
    def _is_emoji_char(self, ch: str) -> bool:
        cp = ord(ch)
        # Emoji ranges (simplified but covers most common)
        if 0x1F600 <= cp <= 0x1F64F:  # emoticons
            return True
        if 0x1F300 <= cp <= 0x1F5FF:  # symbols & pictographs
            return True
        if 0x1F680 <= cp <= 0x1F6FF:  # transport & map
            return True
        if 0x1F1E6 <= cp <= 0x1F1FF:  # flags
            return True
        if 0x2600 <= cp <= 0x26FF:    # misc symbols
            return True
        if 0x2700 <= cp <= 0x27BF:    # dingbats
            return True
        if 0xFE00 <= cp <= 0xFE0F:    # variation selectors
            return True
        if 0x1F900 <= cp <= 0x1F9FF:  # supplemental symbols
            return True
        if 0x1F018 <= cp <= 0x1F270:  # ARIB symbols
            return True
        if cp in (0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC,
                  0x23F0, 0x23F3, 0x25FD, 0x25FE, 0x2614, 0x2615,
                  0x2648, 0x2649, 0x264A, 0x264B, 0x264C, 0x264D,
                  0x264E, 0x264F, 0x2650, 0x2651, 0x2652, 0x2653,
                  0x267F, 0x2693, 0x26A1, 0x26AA, 0x26AB, 0x26BD,
                  0x26BE, 0x26C4, 0x26C5, 0x26CE, 0x26D4, 0x26EA,
                  0x26F2, 0x26F3, 0x26F5, 0x26FA, 0x26FD, 0x2705,
                  0x2728, 0x274C, 0x274E, 0x2753, 0x2754, 0x2755,
                  0x2795, 0x2796, 0x2797, 0x27B0, 0x27BF):
            return True
        return False

    def _strip_custom_emojis(self, text: str) -> str:
        text = self._custom_emoji_pattern.sub(" ", text)
        return " ".join(text.split())

    def explain_filter(self, message: dict) -> Optional[str]:
        self.filter(message)
        return self._last_rejection_reason

    @property
    def last_rejection_reason(self) -> Optional[str]:
        return self._last_rejection_reason
