import time
import json
import re
from typing import Optional, Callable, Dict, Any, List

class VibeThermometer:
    def __init__(self, config: dict, llm_interface: Optional[Callable] = None, is_busy_callback: Optional[Callable[[], bool]] = None):
        self.config = config
        self.window_seconds = config.get("window_seconds", 120)
        self.emotions = config.get("emotions", ["excitement", "sadness", "anger", "joy", "confusion", "neutral"])
        self.prompt_template = config.get("llm_prompt_template", "")
        self.busy_fallback = config.get("busy_fallback_temperature", 50.0)
        self.missing_llm_fallback = config.get("missing_llm_fallback_temperature", 50.0)
        
        self._llm_interface = llm_interface
        self._is_busy_callback = is_busy_callback
        self._buffer: List[Dict[str, Any]] = []
        self._window_start: Optional[float] = None

    def set_llm_interface(self, llm_interface: Optional[Callable]):
        self._llm_interface = llm_interface
    
    def add_message(self, message: dict):
        ts = message.get("timestamp", time.time())
        self._buffer.append({
            "user": message.get("user", ""),
            "text": message.get("text", ""),
            "timestamp": ts
        })
        if self._window_start is None:
            self._window_start = ts
    
    def compute_vibe(self, force: bool = False) -> Optional[Dict[str, Any]]:
        if not self._buffer or self._window_start is None:
            return {"emotions": {e: 0.0 for e in self.emotions}, "temperature": 0.0, "window_duration": 0}
        
        now = time.time()
        elapsed = now - self._window_start
        
        if not force and elapsed < self.window_seconds:
            return None
        
        if self._is_busy_callback and self._is_busy_callback():
            emotions = {e: 0.0 for e in self.emotions}
            emotions["neutral"] = 1.0
            return {
                "emotions": emotions,
                "temperature": self.busy_fallback,
                "window_duration": int(elapsed),
                "note": "fallback_due_to_busy"
            }
        
        result = self._infer_vibe()
        result["window_duration"] = int(elapsed)
        return result
    
    def reset(self):
        self._buffer.clear()
        self._window_start = None
    
    def _infer_vibe(self) -> Dict[str, Any]:
        messages_text = "\n".join([f"- {m['user']}: {m['text']}" for m in self._buffer])
        prompt = self.prompt_template.format(window=self.window_seconds, messages=messages_text)
        
        default_emotions = {e: 0.0 for e in self.emotions}
        default_emotions["neutral"] = 1.0
        default_result = {"emotions": default_emotions, "temperature": 50.0}
        
        if self._llm_interface:
            try:
                raw = self._llm_interface(prompt)
                if isinstance(raw, dict):
                    return self._format_dict_response(raw)
                return self._parse_llm_response(raw)
            except Exception:
                default_result["note"] = "fallback_due_to_llm_error"
                return default_result

        fallback = {"emotions": default_emotions, "temperature": self.missing_llm_fallback}
        fallback["note"] = "fallback_due_to_missing_llm_interface"
        return fallback
    
    def _format_dict_response(self, data: dict) -> Dict[str, Any]:
        default_emotions = {e: 0.0 for e in self.emotions}
        default_emotions["neutral"] = 1.0
        default_result = {"emotions": default_emotions, "temperature": 50.0}
        
        if not isinstance(data, dict):
            return default_result
        
        emotions = data.get("emotions", {})
        temperature = data.get("temperature", 50.0)
        
        normalized = {}
        for e in self.emotions:
            val = emotions.get(e, 0.0)
            if isinstance(val, (int, float)):
                normalized[e] = max(0.0, min(1.0, float(val)))
            else:
                normalized[e] = 0.0
        
        if not any(v > 0 for v in normalized.values()):
            normalized["neutral"] = 1.0
        
        if isinstance(temperature, (int, float)):
            temperature = max(0.0, min(100.0, float(temperature)))
        else:
            temperature = 50.0
        
        return {"emotions": normalized, "temperature": temperature}
    
    def _parse_llm_response(self, raw: str) -> Dict[str, Any]:
        default_emotions = {e: 0.0 for e in self.emotions}
        default_emotions["neutral"] = 1.0
        default_result = {"emotions": default_emotions, "temperature": 50.0}
        
        if not raw:
            default_result["note"] = "fallback_due_to_empty_llm_response"
            return default_result
        
        try:
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
            else:
                data = json.loads(raw)
            
            emotions = data.get("emotions", {})
            temperature = data.get("temperature", 50.0)
            
            normalized = {}
            for e in self.emotions:
                val = emotions.get(e, 0.0)
                if isinstance(val, (int, float)):
                    normalized[e] = max(0.0, min(1.0, float(val)))
                else:
                    normalized[e] = 0.0
            
            if not any(v > 0 for v in normalized.values()):
                normalized["neutral"] = 1.0
            
            if isinstance(temperature, (int, float)):
                temperature = max(0.0, min(100.0, float(temperature)))
            else:
                temperature = 50.0
            
            return {"emotions": normalized, "temperature": temperature}
        except (json.JSONDecodeError, ValueError, AttributeError):
            default_result["note"] = "fallback_due_to_parse_error"
            return default_result
