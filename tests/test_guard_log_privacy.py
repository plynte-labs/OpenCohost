"""S5 (guardrails-vulnerable-users): the output guard never logs reply text."""
from __future__ import annotations

import logging
import re

from opencohost.config.validation import output_guard

_REPLY = "Claro, el telefono de Marta es 555-123-4567 y vive en la calle Alcala"


def test_blocked_reply_leaves_no_text_or_digits_in_logs(caplog):
    with caplog.at_level(logging.DEBUG, logger="opencohost.config.validation"):
        allowed, _ = output_guard(_REPLY, source="direct")
    assert allowed is False
    records = [r.getMessage() for r in caplog.records]
    assert records, "expected a block log record"
    joined = " ".join(records)
    assert not re.search(r"\d{4,}", joined)
    for word in ("Marta", "Alcala", "telefono"):
        assert word not in joined
    assert "chars=" in joined
