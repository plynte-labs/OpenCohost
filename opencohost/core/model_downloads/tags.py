"""Validation of model tags accepted by the in-app download manager (ADR-063).

Two shapes are accepted, nothing else:

- Ollama library tags: ``name[:tag]`` or ``namespace/name[:tag]``. The
  namespace may not contain a dot, so a tag can never name another registry
  host (``evil.com/x``, ``registry.ollama.ai/...``).
- Hugging Face GGUF repos pulled through Ollama:
  ``hf.co/<owner>/<repo>[:<quant>]``.

Schemes, whitespace, backslashes, ``..`` segments and anything longer than
``MAX_TAG_LENGTH`` are rejected before a tag reaches Ollama or the job record.
"""

from __future__ import annotations

import re

MAX_TAG_LENGTH = 200
HF_PREFIX = "hf.co/"

# One path segment of an Ollama name: starts alphanumeric, then [A-Za-z0-9._-].
_OLLAMA_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
_OLLAMA_NAMESPACE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"  # no dots: never a registry host
_TAG_PART = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"

_OLLAMA_TAG_RE = re.compile(
    rf"^(?:{_OLLAMA_NAMESPACE}/)?{_OLLAMA_SEGMENT}(?::{_TAG_PART})?$"
)
# Hugging Face owner / repo names: alphanumeric start, [A-Za-z0-9._-], <= 96.
_HF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_HF_QUANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def is_valid_hf_name(name: object) -> bool:
    """True for a Hugging Face owner or repo name safe to put in a URL path."""
    if not isinstance(name, str) or ".." in name:
        return False
    return _HF_NAME_RE.fullmatch(name) is not None


def _is_valid_hf_tag(tag: str) -> bool:
    rest = tag[len(HF_PREFIX):]
    repo_path, sep, quant = rest.partition(":")
    if sep and _HF_QUANT_RE.fullmatch(quant) is None:
        return False
    parts = repo_path.split("/")
    if len(parts) != 2:
        return False
    return all(is_valid_hf_name(part) for part in parts)


def is_valid_model_tag(tag: object) -> bool:
    """True when *tag* is an Ollama library tag or an ``hf.co/`` GGUF tag."""
    if not isinstance(tag, str) or not tag or len(tag) > MAX_TAG_LENGTH:
        return False
    if ".." in tag:
        return False
    if tag.startswith(HF_PREFIX):
        return _is_valid_hf_tag(tag)
    return _OLLAMA_TAG_RE.fullmatch(tag) is not None
