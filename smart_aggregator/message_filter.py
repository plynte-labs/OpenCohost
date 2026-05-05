import re
from typing import Optional

class MessageFilter:
    def __init__(self, config: dict):
        self.min_words = config.get("min_words", 3)
        self.min_char_length = config.get("min_char_length", 10)
        self.discard_emojis_only = config.get("discard_emojis_only", True)
        self.discard_links = config.get("discard_links", True)
        self.discard_mentions = config.get("discard_mentions", True)
        
        whitelist_cfg = config.get("whitelist", {})
        self.whitelist_enabled = whitelist_cfg.get("enabled", True)
        self.whitelist_users = set(u.lower() for u in whitelist_cfg.get("users", []))
        
        self._url_pattern = re.compile(
            r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        )
        self._mention_pattern = re.compile(r'@[\w.-]+')
        self._custom_emoji_pattern = re.compile(r':[a-zA-Z0-9_+\-]+:')
    
    def filter(self, message: dict) -> Optional[dict]:
        user = message.get("user", "")
        text = message.get("text", "")
        timestamp = message.get("timestamp", 0)
        
        if not isinstance(text, str):
            return None
        
        text_stripped = text.strip()
        if not text_stripped:
            return None
        
        if self.whitelist_enabled and user.lower() in self.whitelist_users:
            return {"user": user, "text": text_stripped, "timestamp": timestamp}
        
        if self.discard_emojis_only and self._has_only_emojis(text_stripped):
            return None
        
        if self.discard_links and self._url_pattern.search(text_stripped):
            return None
        
        if self.discard_mentions and self._mention_pattern.search(text_stripped):
            return None

        text_clean = self._strip_custom_emojis(text_stripped)
        if not text_clean:
            return None

        if len(text_clean) < self.min_char_length:
            return None
        
        words = text_clean.split()
        if len(words) < self.min_words:
            return None
        
        return {"user": user, "text": text_clean, "timestamp": timestamp}
    
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
