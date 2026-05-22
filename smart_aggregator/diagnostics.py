from typing import Dict, Set


class FilterDiagnostics:
    def __init__(self):
        self.seen: int = 0
        self.accepted: int = 0
        self.rejected: int = 0
        self.by_reason: Dict[str, int] = {}

    def record_seen(self) -> None:
        self.seen += 1

    def record_accepted(self) -> None:
        self.accepted += 1

    def record_rejected(self, reason: str) -> None:
        self.rejected += 1
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def get_diagnostics(self) -> dict:
        return {
            "seen": self.seen,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "by_reason": dict(self.by_reason),
        }

    def reset_diagnostics(self) -> None:
        self.seen = 0
        self.accepted = 0
        self.rejected = 0
        self.by_reason.clear()
