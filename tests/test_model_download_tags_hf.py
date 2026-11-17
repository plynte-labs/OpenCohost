"""Tag validation and Hugging Face GGUF quant parsing (model-download-manager T3).

The HF API is always faked: ``hf_quants._http_get`` is monkeypatched, so no
test reaches huggingface.co.
"""

from __future__ import annotations

import httpx
import pytest

from opencohost.core.model_downloads import hf_quants
from opencohost.core.model_downloads.tags import is_valid_hf_name, is_valid_model_tag

GB = 1_000_000_000


@pytest.mark.parametrize(
    "tag",
    [
        "llama3",
        "llama3:8b",
        "gemma4:e4b",
        "qwen2.5:0.5b",
        "qwen3:1.7b",
        "library/qwen3:1.7b",
        "someuser/my-model:q4_K_M",
        "hf.co/bartowski/SmolLM2-135M-Instruct-GGUF:Q4_K_M",
        "hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF",
        "hf.co/unsloth/Qwen3-8B-GGUF:UD-Q4_K_XL",
    ],
)
def test_valid_tags(tag):
    assert is_valid_model_tag(tag) is True


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "   ",
        None,
        123,
        "http://evil/x",
        "https://hf.co/a/b",
        "hf.co/a",
        "hf.co/a/b/c",
        "hf.co/../b:Q4",
        "hf.co/a/..:Q4",
        "../etc/passwd",
        "a/b/c",
        "evil.com/x",
        "registry.ollama.ai/library/llama3",
        "llama3:",
        ":8b",
        "llama 3",
        "a\\b",
        "llama3;rm -rf",
        "-llama",
        "hf.co/a/b:Q4 K",
        "llama3:8b:extra",
        "x" * 300,
        "llama3\n",
    ],
)
def test_invalid_tags(tag):
    assert is_valid_model_tag(tag) is False


@pytest.mark.parametrize("name, ok", [("bartowski", True), ("Qwen3.8-27B_x", True), ("..", False), (".hidden", False), ("a/b", False), ("", False), ("x" * 97, False), ("bad$", False)])
def test_hf_name_validation(name, ok):
    assert is_valid_hf_name(name) is ok


TREE = [
    {"type": "directory", "oid": "d", "size": 0, "path": "Q8_0"},
    {"type": "file", "oid": "o", "size": 1570, "path": ".gitattributes"},
    {"type": "file", "oid": "o", "size": 135, "path": "README.md"},
    {"type": "file", "size": 4_920_000_000, "path": "Qwen3-8B-Q4_K_M.gguf", "lfs": {"oid": "s", "size": 4_920_000_000, "pointerSize": 135}},
    # lfs.size wins over the pointer-sized top-level size
    {"type": "file", "size": 135, "path": "Qwen3-8B-IQ4_XS.gguf", "lfs": {"oid": "s", "size": 4_500_000_000}},
    {"type": "file", "size": 5_000_000_000, "path": "Q8_0/Qwen3-8B-Q8_0-00001-of-00002.gguf", "lfs": {"size": 5_000_000_000}},
    {"type": "file", "size": 3_700_000_000, "path": "Q8_0/Qwen3-8B-Q8_0-00002-of-00002.gguf", "lfs": {"size": 3_700_000_000}},
    {"type": "file", "size": 900_000_000, "path": "mmproj-Qwen3-8B-F16.gguf", "lfs": {"size": 900_000_000}},
    {"type": "file", "size": 16 * GB, "path": "Qwen3-8B.BF16.gguf", "lfs": {"size": 16 * GB}},
    {"type": "file", "size": 10, "path": "weird-name.gguf"},
    "garbage",
    {"path": None},
    {"type": "file", "path": "x-Q2_K.gguf", "size": "not-an-int"},
    {"type": "file", "path": "x-Q3_K_S.gguf", "size": True},
]


def test_parse_gguf_quants_groups_split_files_and_sorts_by_size():
    quants = hf_quants.parse_gguf_quants(TREE)
    assert [q["quant"] for q in quants] == ["IQ4_XS", "Q4_K_M", "Q8_0", "BF16"]
    by = {q["quant"]: q for q in quants}
    assert by["IQ4_XS"]["size_bytes"] == 4_500_000_000
    assert by["Q8_0"]["size_bytes"] == 8_700_000_000
    assert by["Q8_0"]["file_count"] == 2
    assert by["Q4_K_M"]["file_count"] == 1


# Real tree of ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF (2026-09-24): every quant
# ships a base file plus an ``-mtp`` variant. Ollama pulls the base file for
# ``:IQ2_XS``; the variant must not be summed into the quant's size.
MTP_TREE = [
    {"type": "file", "path": "Qwen3.8-27B-GSQ-RCO-IQ2_S-mtp.gguf", "lfs": {"size": 9_607_981_120}},
    {"type": "file", "path": "Qwen3.8-27B-GSQ-RCO-IQ2_S.gguf", "lfs": {"size": 9_259_510_912}},
    {"type": "file", "path": "Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf", "lfs": {"size": 8_771_311_680}},
    {"type": "file", "path": "Qwen3.8-27B-GSQ-RCO-IQ2_XS.gguf", "lfs": {"size": 8_422_841_472}},
    {"type": "file", "path": "imatrix-qwen3.8-27b.gguf", "lfs": {"size": 13_642_624}},
    {"type": "file", "path": "mmproj-Qwen3.8-27B-BF16.gguf", "lfs": {"size": 931_146_528}},
]


def test_parse_gguf_quants_does_not_sum_distinct_variants_of_a_quant():
    by = {q["quant"]: q for q in hf_quants.parse_gguf_quants(MTP_TREE)}
    assert set(by) == {"IQ2_XS", "IQ2_S"}
    assert by["IQ2_XS"] == {"quant": "IQ2_XS", "size_bytes": 8_422_841_472, "file_count": 1}
    assert by["IQ2_S"]["size_bytes"] == 9_259_510_912


def test_parse_gguf_quants_falls_back_to_smallest_variant_without_a_base_file():
    tree = [
        {"type": "file", "path": "M-Q4_K_M-imat.gguf", "lfs": {"size": 5_000}},
        {"type": "file", "path": "M-Q4_K_M-mtp.gguf", "lfs": {"size": 4_000}},
    ]
    assert hf_quants.parse_gguf_quants(tree) == [
        {"quant": "Q4_K_M", "size_bytes": 4_000, "file_count": 1}
    ]


def test_parse_gguf_quants_tolerates_non_list():
    assert hf_quants.parse_gguf_quants({"error": "x"}) == []
    assert hf_quants.parse_gguf_quants(None) == []


@pytest.mark.parametrize(
    "size, vram_mb, fit",
    [
        (4_500_000_000, 8192, "fits"),
        (7_500_000_000, 8192, "tight"),
        (8_700_000_000, 8192, "too_large"),
        (1_000_000, 0, "unknown"),
        (1_000_000, None, "unknown"),
    ],
)
def test_vram_fit(size, vram_mb, fit):
    assert hf_quants.vram_fit(size, vram_mb) == fit


class FakeResp:
    def __init__(self, status_code, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def test_list_quants_builds_ready_to_pull_tags(monkeypatch):
    seen = {}

    def fake_get(url, *, params=None, timeout=None):
        seen.update(url=url, params=params, timeout=timeout)
        return FakeResp(200, TREE)

    monkeypatch.setattr(hf_quants, "_http_get", fake_get)
    result = hf_quants.list_quants("bartowski", "Qwen3-8B-GGUF", vram_mb=8192)
    assert seen["url"] == "https://huggingface.co/api/models/bartowski/Qwen3-8B-GGUF/tree/main"
    assert seen["params"] == {"recursive": "true"}
    assert seen["timeout"] == hf_quants.HF_TIMEOUT_SECONDS
    assert result["owner"] == "bartowski"
    assert result["repo"] == "Qwen3-8B-GGUF"
    assert result["vram_mb"] == 8192
    q4 = next(q for q in result["quants"] if q["quant"] == "Q4_K_M")
    assert q4["tag"] == "hf.co/bartowski/Qwen3-8B-GGUF:Q4_K_M"
    assert q4["vram_fit"] == "fits"


@pytest.mark.parametrize("status", [401, 404])
def test_fetch_maps_missing_or_gated_repo_to_not_found(monkeypatch, status):
    monkeypatch.setattr(hf_quants, "_http_get", lambda url, **kw: FakeResp(status, {}))
    with pytest.raises(hf_quants.HfRepoNotFoundError):
        hf_quants.list_quants("a", "b", vram_mb=0)


@pytest.mark.parametrize(
    "behaviour",
    [
        lambda url, **kw: FakeResp(500, {}),
        lambda url, **kw: FakeResp(200, raise_json=True),
        lambda url, **kw: FakeResp(200, {"not": "a list"}),
    ],
)
def test_fetch_maps_upstream_problems_to_upstream_error(monkeypatch, behaviour):
    monkeypatch.setattr(hf_quants, "_http_get", behaviour)
    with pytest.raises(hf_quants.HfUpstreamError):
        hf_quants.list_quants("a", "b", vram_mb=0)


def test_fetch_maps_transport_error_to_upstream_error(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(hf_quants, "_http_get", boom)
    with pytest.raises(hf_quants.HfUpstreamError):
        hf_quants.list_quants("a", "b", vram_mb=0)


def test_list_quants_rejects_invalid_names_before_any_request(monkeypatch):
    calls = []
    monkeypatch.setattr(hf_quants, "_http_get", lambda url, **kw: calls.append(url))
    with pytest.raises(ValueError):
        hf_quants.list_quants("..", "b", vram_mb=0)
    assert calls == []
