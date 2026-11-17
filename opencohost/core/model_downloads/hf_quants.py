"""List the GGUF quantizations of a public Hugging Face repo (ADR-063).

Reads the public model tree API::

    GET https://huggingface.co/api/models/{owner}/{repo}/tree/main?recursive=true

which answers a JSON list of entries shaped like::

    {"type": "file", "path": "Q8_0/M-Q8_0-00001-of-00002.gguf", "size": 5000000000,
     "oid": "...", "lfs": {"oid": "...", "size": 5000000000, "pointerSize": 135}}

The parser is defensive: anything that is not a ``file`` entry with a
``.gguf`` path and an integer size is ignored. ``lfs.size`` (the real blob
size) wins over the top-level ``size``. Split files (``-00001-of-00002``)
are summed per quant; distinct variants of one quant (``-mtp``) are not.
Vision projectors (``mmproj-*``) are skipped because
Ollama pulls them alongside the model, not as a selectable quant.

Known limit: the tree API paginates very large repos (Link header); only
the first page is read. GGUF repos are small enough in practice.

Each quant maps to the ready-to-pull tag ``hf.co/{owner}/{repo}:{QUANT}``,
the form T1 verified against Ollama 0.30.11.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from opencohost.core.model_downloads.tags import is_valid_hf_name

HF_API_BASE = "https://huggingface.co/api/models"
HF_TIMEOUT_SECONDS = 10.0

# VRAM-fit hint: a model whose file needs this much headroom (KV cache,
# runtime buffers) "fits"; one that only fits byte-for-byte is "tight".
VRAM_HEADROOM_FACTOR = 1.2

_QUANT_TOKEN_RE = re.compile(
    r"^(?:I?Q\d+(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP4(?:_MOE)?)$", re.IGNORECASE
)
_SPLIT_SUFFIX_RE = re.compile(r"-\d{5}-of-\d{5}$", re.IGNORECASE)


class HfRepoNotFoundError(Exception):
    """The repo does not exist, or is private/gated for anonymous reads."""


class HfUpstreamError(Exception):
    """Hugging Face could not be reached or answered something unusable."""


def _http_get(url: str, *, params: Optional[dict] = None, timeout: Optional[float] = None):
    import httpx

    return httpx.get(url, params=params, timeout=timeout, follow_redirects=True)


def _entry_size(entry: dict) -> Optional[int]:
    lfs = entry.get("lfs")
    candidates = []
    if isinstance(lfs, dict):
        candidates.append(lfs.get("size"))
    candidates.append(entry.get("size"))
    for value in candidates:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _quant_of(path: str) -> Optional[tuple[str, str, bool]]:
    """Return ``(quant, variant, is_base)`` for a GGUF path, or None.

    ``variant`` is the path minus any split suffix, so the shards of one file
    share it. ``is_base`` is True when the quant token ends the file name
    (``M-IQ2_XS.gguf``) rather than preceding a suffix (``M-IQ2_XS-mtp.gguf``).
    """
    filename = path.rsplit("/", 1)[-1]
    if filename.lower().startswith("mmproj"):
        return None
    stem = _SPLIT_SUFFIX_RE.sub("", filename[: -len(".gguf")])
    tokens = re.split(r"[-.]", stem)
    for index in range(len(tokens) - 1, -1, -1):
        if _QUANT_TOKEN_RE.fullmatch(tokens[index]):
            variant = path[: len(path) - len(filename)] + stem
            return tokens[index].upper(), variant.lower(), index == len(tokens) - 1
    return None


def parse_gguf_quants(tree: Any) -> list[dict]:
    """Group a tree listing into ``[{quant, size_bytes, file_count}]`` by size.

    Shards of one split file are summed. Distinct variants of the same quant
    (``M-IQ2_XS.gguf`` and ``M-IQ2_XS-mtp.gguf``) are separate files, never
    summed: the base variant is what Ollama pulls for ``:QUANT``, so it is
    reported; without a base, the smallest variant is.
    """
    if not isinstance(tree, list):
        return []
    variants: dict[str, dict[str, dict]] = {}
    for entry in tree:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.lower().endswith(".gguf"):
            continue
        size = _entry_size(entry)
        parsed = _quant_of(path)
        if size is None or parsed is None:
            continue
        quant, variant, is_base = parsed
        slot = variants.setdefault(quant, {}).setdefault(
            variant, {"size_bytes": 0, "file_count": 0, "is_base": is_base}
        )
        slot["size_bytes"] += size
        slot["file_count"] += 1
    grouped = []
    for quant, by_variant in variants.items():
        chosen = min(
            by_variant.values(), key=lambda v: (not v["is_base"], v["size_bytes"])
        )
        grouped.append(
            {"quant": quant, "size_bytes": chosen["size_bytes"], "file_count": chosen["file_count"]}
        )
    return sorted(grouped, key=lambda q: (q["size_bytes"], q["quant"]))


def vram_fit(size_bytes: int, vram_mb: Optional[int]) -> str:
    """``fits`` | ``tight`` | ``too_large`` | ``unknown`` (no GPU detected)."""
    if not vram_mb or vram_mb <= 0:
        return "unknown"
    vram_bytes = vram_mb * 1024 * 1024
    if size_bytes * VRAM_HEADROOM_FACTOR <= vram_bytes:
        return "fits"
    if size_bytes <= vram_bytes:
        return "tight"
    return "too_large"


def fetch_repo_tree(owner: str, repo: str) -> list:
    url = f"{HF_API_BASE}/{owner}/{repo}/tree/main"
    try:
        resp = _http_get(url, params={"recursive": "true"}, timeout=HF_TIMEOUT_SECONDS)
    except Exception as exc:  # transport errors, timeouts
        raise HfUpstreamError(type(exc).__name__) from None
    status = getattr(resp, "status_code", None)
    if status in (401, 403, 404):
        raise HfRepoNotFoundError(str(status))
    if status != 200:
        raise HfUpstreamError(str(status))
    try:
        data = resp.json()
    except Exception as exc:
        raise HfUpstreamError(type(exc).__name__) from None
    if not isinstance(data, list):
        raise HfUpstreamError("unexpected_shape")
    return data


def list_quants(owner: str, repo: str, *, vram_mb: Optional[int]) -> dict:
    """Quant list with ready-to-pull tags and a VRAM-fit hint per quant.

    Raises ``ValueError`` for an unsafe owner/repo name (before any request),
    ``HfRepoNotFoundError`` and ``HfUpstreamError`` as documented above.
    """
    if not (is_valid_hf_name(owner) and is_valid_hf_name(repo)):
        raise ValueError("invalid_hf_repo")
    quants = parse_gguf_quants(fetch_repo_tree(owner, repo))
    for quant in quants:
        quant["tag"] = f"hf.co/{owner}/{repo}:{quant['quant']}"
        quant["vram_fit"] = vram_fit(quant["size_bytes"], vram_mb)
    return {
        "owner": owner,
        "repo": repo,
        "vram_mb": vram_mb if vram_mb and vram_mb > 0 else None,
        "quants": quants,
    }
