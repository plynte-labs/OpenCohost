"""Pasted-image support for POST /api/chat/turn (route A).

The operator pastes an image into the composer. The configured vision model
describes it and the chat LLM receives that description as TEXT inside the
operator's turn; the chat LLM never receives image bytes.

PRIVACY: the decoded bytes and the base64 live only in local variables of the
request that carries them. Nothing here logs, caches or persists them; the only
loggable facts are the byte size and the mime type.
"""

import base64
import binascii
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from opencohost.config.vision_settings import DESCRIBE_TIMEOUT_MAX_S
from opencohost.core.vision import direct_cloud
from opencohost.core.vision.controller import DEFAULT_COLD_START_WAIT_S
from opencohost.core.vision.describer import VisionDescribeError
from opencohost.core.vision.sanitize import sanitize_description
from opencohost.i18n import active as i18n_active

logger = logging.getLogger("opencohost.api.main")

IMAGE_MAX_BYTES = 8 * 1024 * 1024
ALLOWED_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")
DESCRIPTION_MAX_CHARS = 2000

# runtime-fixes-20261110 T7. The ONE definition of how many images a message may
# carry. Owner choice (2026-11-10); the code holds no lower technical bound: the
# describer takes any number of frames per request, but the 2B/4B vision model
# is weak at multi-image reasoning (core/vision/mosaic.py measured "a poor frame
# order past three"), so each image is described in its own call. Exposed to the
# UI as ``max_images`` on GET /api/vision.
MAX_IMAGES = 3
# All descriptions of one message together may grow to 1.5x what a single image
# was allowed (2000 -> 3000 chars); each image gets an equal share.
DESCRIPTIONS_TOTAL_MAX_CHARS = DESCRIPTION_MAX_CHARS * 3 // 2
# Time: one cold start (waited once) plus one describe call per image, each at
# the largest describe timeout the settings allow.
COLD_START_WAIT_S = DEFAULT_COLD_START_WAIT_S
DESCRIBE_CALL_MAX_S = float(DESCRIBE_TIMEOUT_MAX_S)

_DATA_URL_RE = re.compile(r"^data:([^;,]*)(?:;[^,]*)?;base64,", re.IGNORECASE)
# Base64 is 4 chars per 3 bytes; a longer string cannot decode under the cap.
_MAX_B64_CHARS = (IMAGE_MAX_BYTES + 2) // 3 * 4 + 4

DESCRIBE_FAILURE_STATUS = {"timeout": 504, "unavailable": 503}

# Stable codes the UI maps to a message (runtime-fixes-20261110 T3). Keyed by the
# ``detail`` the controller attaches to an "unavailable" error. Anything else
# keeps the original ``vision_describe_<reason>`` code.
_UNAVAILABLE_BY_DETAIL = {
    "model_warming": (503, "vision_starting"),
    "daemon_not_ready": (503, "vision_failed"),
    "model_missing": (503, "vision_failed"),
    "vision_off": (422, "vision_off"),
    "vision_not_wired": (422, "vision_off"),
}


def describe_failure_code(exc: VisionDescribeError) -> tuple[int, str]:
    """HTTP status and stable error code for a failed describe call."""
    if exc.reason == "unavailable":
        mapped = _UNAVAILABLE_BY_DETAIL.get(getattr(exc, "detail", ""))
        if mapped is not None:
            return mapped
    return DESCRIBE_FAILURE_STATUS.get(exc.reason, 502), f"vision_describe_{exc.reason}"


def description_cap(count: int) -> int:
    """Characters one description may keep when the message carries ``count`` images."""
    if count <= 1:
        return DESCRIPTION_MAX_CHARS
    return DESCRIPTIONS_TOTAL_MAX_CHARS // count


def turn_budget_s(count: int) -> float:
    """Worst-case seconds the describe stage of a ``count``-image message may take."""
    return COLD_START_WAIT_S + max(count, 1) * DESCRIBE_CALL_MAX_S


class ChatImageError(Exception):
    def __init__(self, status_code: int, detail: str, extra: Optional[dict] = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        # Counts and indexes only (never content); merged into the JSON body.
        self.extra = dict(extra or {})

    def body(self) -> dict:
        return {"detail": self.detail, **self.extra}


def _matches_magic(mime: str, data: bytes) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def decode_image(data: str, mime_type: Optional[str]) -> tuple[bytes, str]:
    """Validate and decode one pasted image. Raises ChatImageError (4xx)."""
    raw = data or ""
    mime = (mime_type or "").strip().lower()
    match = _DATA_URL_RE.match(raw)
    if match:
        if not mime:
            mime = match.group(1).strip().lower()
        raw = raw[match.end():]
    if mime not in ALLOWED_MIME_TYPES:
        raise ChatImageError(422, "image mime type must be png, jpeg or webp")
    if len(raw) > _MAX_B64_CHARS:
        raise ChatImageError(413, "image exceeds max size of 8 MB")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ChatImageError(422, "image is not valid base64") from None
    if len(decoded) > IMAGE_MAX_BYTES:
        raise ChatImageError(413, "image exceeds max size of 8 MB")
    if not decoded or not _matches_magic(mime, decoded):
        raise ChatImageError(422, "image content does not match its mime type")
    return decoded, mime


def check_vision_ready(host) -> None:
    """The describe path needs vision on and a model configured."""
    status = host.vision_status() if hasattr(host, "vision_status") else {}
    if not status.get("enabled"):
        raise ChatImageError(422, "vision_disabled")
    if not status.get("model"):
        raise ChatImageError(422, "vision_model_missing")


def describe_image(host, image: bytes, text: str, *, max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
    """Describe through the existing vision describer. Blocking: call it from a
    sync route (FastAPI runs those in the worker threadpool)."""
    question = text.strip() or i18n_active.pasted_image_question()
    describer = getattr(host, "vision_describe_image", None)
    if not callable(describer):
        raise ChatImageError(503, "vision_describe_unavailable")
    try:
        description = describer(image, question)
    except VisionDescribeError as exc:
        status, code = describe_failure_code(exc)
        logger.info("[CHAT_IMAGE] failed code=%s", code)
        raise ChatImageError(status, code) from None
    # Square brackets could forge the block delimiters of the prompt scaffold.
    body = sanitize_description(description or "", max_chars)
    body = body.replace("[", "(").replace("]", ")").strip()
    if not body:
        raise ChatImageError(502, "vision_describe_empty")
    return body


def compose_turn_text(text: str, description: str) -> str:
    block = i18n_active.pasted_image_block().format(description=description)
    text = text.strip()
    return f"{text}\n\n{block}" if text else block


def compose_turn_text_multi(text: str, descriptions: list[str]) -> str:
    """The operator's text followed by one delimited block per image. One image
    composes exactly like :func:`compose_turn_text`; several are labelled
    "image i of n" inside their own block, so ``strip_pasted_image_block`` still
    removes each of them as before."""
    if len(descriptions) <= 1:
        return compose_turn_text(text, descriptions[0] if descriptions else "")
    total = len(descriptions)
    label = i18n_active.pasted_image_label()
    template = i18n_active.pasted_image_block()
    blocks = [
        template.format(description=f"{label.format(index=i, total=total)}\n{description}")
        for i, description in enumerate(descriptions, start=1)
    ]
    joined = "\n\n".join(blocks)
    text = text.strip()
    return f"{text}\n\n{joined}" if text else joined


def direct_route(host) -> bool:
    """P2: True when this host sends pasted images straight to its cloud chat
    model (no describe call). A host without the hook, or whose check fails, is
    the describe path."""
    check = getattr(host, "vision_direct_eligible", None)
    stash = getattr(host, "vision_stash_images", None)
    if not callable(check) or not callable(stash):
        return False
    try:
        return bool(check())
    except Exception:
        return False


def compose_direct_turn_text(text: str, count: int) -> str:
    """The operator's text plus ONE delimited block holding a fixed note (no
    description exists: the cloud model looks at the attached images). Same block
    template as a described image, so ``strip_pasted_image_block`` removes it
    from memory exactly like one."""
    note = direct_cloud.DIRECT_PASTED_NOTE
    if count > 1:
        note = f"{note} ({count} images)"
    block = i18n_active.pasted_image_block().format(description=note)
    text = text.strip()
    return f"{text}\n\n{block}" if text else block


def request_images(single, many) -> list:
    """The images of a request: the legacy ``image`` field or the ``images``
    list (never both). Raises ChatImageError(422) over the limit."""
    if single is not None and many:
        raise ChatImageError(422, "images_conflict")
    items = [single] if single is not None else list(many or [])
    if len(items) > MAX_IMAGES:
        raise ChatImageError(
            422, "too_many_images", {"max_images": MAX_IMAGES, "received": len(items)}
        )
    return items


def decode_images(items: list) -> list[tuple[bytes, str]]:
    """Validate and decode every image up front, so a bad third image costs no
    vision time. The error names the offending position."""
    decoded = []
    for index, item in enumerate(items):
        try:
            decoded.append(decode_image(item.data, item.mime_type))
        except ChatImageError as exc:
            exc.extra = {"image_index": index, "image_total": len(items), **exc.extra}
            raise
    return decoded


def describe_images(host, images: list[tuple[bytes, str]], text: str) -> list[str]:
    """Describe each image in its own call, in order. All-or-nothing: the first
    failure raises with ``image_index``, ``image_total`` and ``described`` (counts
    only) and nothing is returned. The total time is bounded by
    :func:`turn_budget_s`: once the budget for the images already attempted is
    spent, the next image is not started."""
    total = len(images)
    cap = description_cap(total)
    started = time.monotonic()
    descriptions: list[str] = []
    for index, (image, mime) in enumerate(images):
        allowed = COLD_START_WAIT_S + index * DESCRIBE_CALL_MAX_S
        try:
            if index and time.monotonic() - started > allowed:
                logger.info("[CHAT_IMAGE] failed code=vision_describe_timeout budget_spent index=%d", index)
                raise ChatImageError(504, "vision_describe_timeout")
            # Only size and mime are ever logged about an image.
            logger.info(
                "[CHAT_IMAGE] describing index=%d/%d size=%d mime=%s", index + 1, total, len(image), mime
            )
            descriptions.append(describe_image(host, image, text, max_chars=cap))
        except ChatImageError as exc:
            exc.extra = {
                "image_index": index,
                "image_total": total,
                "described": len(descriptions),
                **exc.extra,
            }
            raise
    logger.info(
        "[CHAT_IMAGE] described count=%d total_ms=%d", total, int((time.monotonic() - started) * 1000)
    )
    return descriptions
