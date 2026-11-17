"""Torch-free Laya runtime: numpy + ``tokenizers`` + ONNX Runtime
(laya-decision-layer-20261009 T7).

Runs an exported Laya checkpoint (``scripts/export_laya_onnx.py``) with no
torch and no transformers, so the decision worker can live in the app's own
interpreter. Only the inference path the decision layer uses is ported, and
only ``choice`` questions:

  sequence  [CLS] "choice question: <instructions>" [SEP]
            [MASK] opt0 [MASK] opt1 ... [SEP] <state> [SEP]
  options   ``"<label>: <description>"``, each capped at 48 tokens, all of
            them capped together when the head budget runs short
  state     tokenized once, right-truncated to the room the head leaves
            (left-truncated for a conversation list), ``[MASK]`` neutralised
  batch     one row per question, right-padded; markers padded to >= 2
  decode    logits / clamped per-bucket temperature -> softmax -> unpermute
            -> ``answer_confidence`` (max p) and ``confidence`` (1 - H/log k)

Ported from laya 0.4.1 (Copyright ConvAI Innovations, Apache License 2.0,
https://github.com/NandhaKishorM/laya): ``laya/common.py`` (serialize_state,
render_criterion, render_options, build_head, build_sequence, collate_items,
unpermute_probs, answer_confidence, confidence_from_probs, temp_bucket,
clamp_temperature) and ``laya/onnx_agent.py`` (``ONNXAgent._to_internal``,
``_encode_state``, ``_decode_answers``, ``_infer_batch``). Parity with laya's
torch path is pinned by tests/test_laya_onnx_parity.py.

This file is also loaded by path from the worker process, so it must import
nothing from the app package. ``onnxruntime`` and ``tokenizers`` are imported
lazily; importing this module needs only numpy.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

ENV_DIR = "OPENCOHOST_LAYA_ONNX_DIR"
MANIFEST = "manifest.json"
GRAPH = "laya.onnx"
CONFIG = "rl_agent_config.json"
REQUIRED_FILES = (GRAPH, "tokenizer.json", "tokenizer_config.json", CONFIG)
DEFAULT_CHECKPOINT = "multilingual"

QTYPES = {"choice": 0, "score": 1, "noul": 2}
_QTYPE_NAMES = {v: k for k, v in QTYPES.items()}
OPTION_MAX_TOKENS = 48
TEMP_MIN = 0.5
TEMP_MAX = 5.0

CODE_MISSING = "onnx_artifacts_missing"
CODE_MANIFEST = "onnx_manifest_invalid"
CODE_SHA256 = "onnx_sha256_mismatch"


class ArtifactsUnavailable(Exception):
    """The exported model cannot be used; ``code`` is a short reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- artifacts dir -----------------------------------------------------------


def default_onnx_dir(env: Optional[dict] = None) -> str:
    """``%LOCALAPPDATA%/OpenCohost/models/laya/multilingual``, or
    ``~/.local/share/...`` without LOCALAPPDATA. Independent of the app's
    user-data dir on purpose: in dev that is the repo checkout."""
    env = os.environ if env is None else env
    base = (env.get("LOCALAPPDATA") or "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "OpenCohost", "models", "laya", DEFAULT_CHECKPOINT)


def resolve_onnx_dir(env: Optional[dict] = None) -> str:
    env = os.environ if env is None else env
    override = (env.get(ENV_DIR) or "").strip()
    if override:
        return override
    resources = (env.get("OPENCOHOST_RESOURCES_DIR") or "").strip()
    if resources:
        installed = os.path.join(resources, "models", "laya", DEFAULT_CHECKPOINT)
        # Select incomplete installations too: integrity checks report the
        # failure instead of silently substituting a developer's cached model.
        if os.path.exists(installed):
            return installed
    return default_onnx_dir(env)


def _read_manifest(path: str) -> dict:
    try:
        with open(os.path.join(path, MANIFEST), encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        raise ArtifactsUnavailable(CODE_MISSING) from None
    except Exception:
        raise ArtifactsUnavailable(CODE_MANIFEST) from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not all(name in files for name in REQUIRED_FILES):
        raise ArtifactsUnavailable(CODE_MANIFEST)
    for name, digest in files.items():
        if (
            not isinstance(name, str)
            or os.path.basename(name) != name
            or name in ("", ".", "..")
            or not isinstance(digest, str)
        ):
            raise ArtifactsUnavailable(CODE_MANIFEST)
    return manifest


def artifacts_present(path: str) -> bool:
    """Cheap check (stat only): a readable manifest and every file it lists."""
    try:
        manifest = _read_manifest(path)
    except ArtifactsUnavailable:
        return False
    return all(os.path.isfile(os.path.join(path, name)) for name in manifest["files"])


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_artifacts(path: str) -> dict:
    """Return the manifest when every listed file matches its sha256;
    raise ``ArtifactsUnavailable`` otherwise."""
    manifest = _read_manifest(path)
    for name, expected in manifest["files"].items():
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            raise ArtifactsUnavailable(CODE_MISSING)
        if _sha256(full) != expected.lower():
            raise ArtifactsUnavailable(CODE_SHA256)
    return manifest


# --- tokenizer ---------------------------------------------------------------


def _token_text(value) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("content")
    return value if isinstance(value, str) and value else None


class LayaTokenizer:
    """``tokenizer.json`` through ``tokenizers`` plus the special tokens named
    in ``tokenizer_config.json``. Never pads or truncates on its own."""

    def __init__(self, tokenizer, config: dict) -> None:
        self._tok = tokenizer
        self._tok.no_truncation()
        self._tok.no_padding()
        names = {}
        for role in ("cls", "sep", "mask", "pad"):
            text = _token_text(config.get(f"{role}_token"))
            token_id = self._tok.token_to_id(text) if text else None
            if token_id is None:
                raise ValueError(f"tokenizer has no {role} token")
            names[role] = (text, token_id)
        self.mask_token = names["mask"][0]
        self.cls_token_id = names["cls"][1]
        self.sep_token_id = names["sep"][1]
        self.mask_token_id = names["mask"][1]
        self.pad_token_id = names["pad"][1]

    @classmethod
    def from_dir(cls, path: str) -> "LayaTokenizer":
        from tokenizers import Tokenizer

        with open(os.path.join(path, "tokenizer_config.json"), encoding="utf-8") as f:
            config = json.load(f)
        return cls(Tokenizer.from_file(os.path.join(path, "tokenizer.json")), config)

    def encode(self, text: str, max_length: Optional[int] = None) -> List[int]:
        """Ids without special tokens; ``max_length`` keeps the first ids, as
        ``truncation=True, max_length=n`` does in transformers."""
        ids = list(self._tok.encode(text, add_special_tokens=False).ids)
        return ids[:max_length] if max_length is not None else ids


# --- questions ---------------------------------------------------------------


def serialize_state(state) -> str:
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)


def render_criterion(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "), default=str)


def to_internal(qdef: Dict[str, Any]) -> Dict[str, Any]:
    """A validated ``choice`` question in laya's internal shape."""
    if not isinstance(qdef, dict) or qdef.get("type") != "choice":
        raise ValueError("only choice questions are supported")
    crit = qdef.get("criteria")
    if isinstance(crit, list):
        crit = {c: None for c in crit}
    if not isinstance(crit, dict) or not crit:
        raise ValueError("choice criteria must be a non-empty mapping or list")
    ins = qdef.get("instructions")
    if not isinstance(ins, str):
        ins = json.dumps(ins, ensure_ascii=False)
    q = {"t": "choice", "ins": ins, "crit": crit}
    if "option_order" in qdef:
        order = [int(i) for i in qdef["option_order"]]
        if sorted(order) != list(range(len(crit))):
            raise ValueError("option_order must be a permutation of the options")
        q["option_order"] = order
    return q


def render_options(q: Dict[str, Any]) -> List[str]:
    return [
        str(k) if v is None or v == "" else "%s: %s" % (k, render_criterion(v))
        for k, v in q["crit"].items()
    ]


def build_head(tok: LayaTokenizer, q: Dict[str, Any], head_max_len: int,
               option_order: Optional[Sequence[int]] = None):
    """``[CLS] <type> question: <ins> [SEP] [MASK] opt0 [MASK] opt1 ... [SEP]``
    and the ``[MASK]`` position of each option slot."""
    mask = tok.mask_token
    opts = render_options(q)
    order = list(option_order) if option_order is not None else list(range(len(opts)))
    ins = str(q["ins"]).replace(mask, " ")
    head_ids = tok.encode("%s question: %s" % (q["t"], ins))
    opt_ids = [
        [tok.mask_token_id] + tok.encode(" " + opts[i].replace(mask, " "), max_length=OPTION_MAX_TOKENS)
        for i in order
    ]
    opt_budget = head_max_len - sum(len(o) for o in opt_ids)
    if opt_budget < 16:
        per = max(4, (head_max_len - 16) // max(1, len(opt_ids)))
        opt_ids = [o[:per] for o in opt_ids]
        opt_budget = head_max_len - sum(len(o) for o in opt_ids)
    head_ids = head_ids[: max(8, opt_budget)]
    ids = [tok.cls_token_id] + head_ids + [tok.sep_token_id]
    markers = []
    for o in opt_ids:
        markers.append(len(ids))
        ids.extend(o)
    ids.append(tok.sep_token_id)
    return ids, markers


def build_sequence(tok: LayaTokenizer, q: Dict[str, Any], state_ids: List[int], max_len: int,
                   head_max_len: int, option_order: Optional[Sequence[int]] = None,
                   truncate_left: bool = False):
    """The full row: head + the state clamped to the room left + ``[SEP]``."""
    ids, markers = build_head(tok, q, head_max_len, option_order)
    room = max(0, max_len - len(ids) - 1)
    st = state_ids[max(0, len(state_ids) - room):] if truncate_left else state_ids[:room]
    ids = ids + st + [tok.sep_token_id]
    return ids[:max_len], [m for m in markers if m < max_len]


def encode_state_text(tok: LayaTokenizer, state) -> List[int]:
    return tok.encode(serialize_state(state).replace(tok.mask_token, " "))


def encode_state(tok: LayaTokenizer, state, qids: Sequence[str], internal: Dict[str, Dict],
                 max_len: int, head_max_len: int) -> List[Dict[str, Any]]:
    """One row per question; the state is tokenized once for all of them."""
    truncate_left = isinstance(state, list)
    state_ids = encode_state_text(tok, state)
    items = []
    for qid in qids:
        q = internal[qid]
        ids, markers = build_sequence(tok, q, state_ids, max_len, head_max_len,
                                      q.get("option_order"), truncate_left)
        if len(markers) != len(q["crit"]):
            raise ValueError("question %r: only %d of its %d option markers fit in max_len=%d"
                             % (qid, len(markers), len(q["crit"]), max_len))
        items.append({"ids": ids, "markers": markers, "qtype": QTYPES[q["t"]]})
    return items


def collate(items: List[Dict[str, Any]], pad_id: int) -> Dict[str, np.ndarray]:
    """Right-padded batch in the graph's five input names and dtypes."""
    n = len(items)
    length = max(len(it["ids"]) for it in items)
    # The exported head takes topk(2) of the option probabilities, so a run
    # whose rows all have one option needs a masked second slot (ONNXAgent).
    kmax = max(2, max(len(it["markers"]) for it in items))
    input_ids = np.full((n, length), pad_id, dtype=np.int64)
    attention = np.zeros((n, length), dtype=np.int64)
    marker_pos = np.zeros((n, kmax), dtype=np.int64)
    marker_mask = np.zeros((n, kmax), dtype=bool)
    for i, it in enumerate(items):
        input_ids[i, : len(it["ids"])] = it["ids"]
        attention[i, : len(it["ids"])] = 1
        k = len(it["markers"])
        marker_pos[i, :k] = it["markers"]
        marker_mask[i, :k] = True
    qtype = np.array([it["qtype"] for it in items], dtype=np.int64)
    return {
        "input_ids": input_ids,
        "attention_mask": attention,
        "marker_pos": marker_pos,
        "marker_mask": marker_mask,
        "qtype": qtype,
    }


# --- decoding ----------------------------------------------------------------


def clamp_temperature(t, lo: float = TEMP_MIN, hi: float = TEMP_MAX) -> float:
    if isinstance(t, bool):
        return 1.0
    try:
        t = float(t)
    except (TypeError, ValueError):
        return 1.0
    if t != t or t in (float("inf"), float("-inf")):
        return 1.0
    return min(hi, max(lo, t))


def temp_bucket(qtype: int, k: int) -> str:
    size = "2" if k <= 2 else "3-5" if k <= 5 else "6-10" if k <= 10 else "11+"
    return "%s:%s" % (_QTYPE_NAMES[int(qtype)], size)


class Temperatures:
    """The checkpoint's calibration: per-type temperatures, overridden per
    option-count bucket, every value clamped to [TEMP_MIN, TEMP_MAX]."""

    def __init__(self, per_type: Sequence[float], by_options: Dict[str, float]) -> None:
        self.per_type = list(per_type)
        self.by_options = dict(by_options)

    @classmethod
    def from_config(cls, cfg: dict) -> "Temperatures":
        raw = cfg.get("temperature", [1.0, 1.0, 1.0])
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raw = [1.0, 1.0, 1.0]
        by_options = cfg.get("temperature_by_options") or {}
        if not isinstance(by_options, dict):
            by_options = {}
        return cls([clamp_temperature(t) for t in raw],
                   {k: clamp_temperature(v) for k, v in by_options.items()})

    def for_question(self, qtype: int, k: int) -> float:
        return self.by_options.get(temp_bucket(qtype, k), self.per_type[qtype])


def unpermute_probs(p: np.ndarray, option_order: Optional[Sequence[int]]) -> np.ndarray:
    """Slot order back to the caller's option order (identity without one)."""
    if option_order is None or len(option_order) != len(p):
        return p
    canonical = np.empty_like(p)
    canonical[np.asarray(option_order, dtype=int)] = p
    return canonical


def answer_confidence(p: np.ndarray, k: int) -> float:
    if k < 1:
        return 1.0
    return float(np.clip(np.max(p[:k]), 0.0, 1.0))


def confidence_from_probs(p: np.ndarray, k: int) -> float:
    if k < 2:
        return 1.0
    p = p[:k]
    ent = -(p * np.log(np.clip(p, 1e-12, 1.0))).sum()
    return float(np.clip(1.0 - ent / math.log(k), 0.0, 1.0))


def decode_choice(logit_row: np.ndarray, k: int, q: Dict[str, Any], temperature: float) -> Dict[str, Any]:
    """One ``choice`` answer in the shape the worker returns."""
    z = np.asarray(logit_row[:k], dtype=np.float64) / temperature
    p = np.exp(z - z.max())
    p = p / p.sum()
    p = unpermute_probs(p, q.get("option_order"))
    keys = list(q["crit"].keys())
    return {
        "choice": keys[int(p.argmax())],
        "probabilities": {kk: round(float(v), 4) for kk, v in zip(keys, p)},
        "answer_confidence": round(answer_confidence(p, k), 4),
        "confidence": round(confidence_from_probs(p, k), 4),
    }


# --- model -------------------------------------------------------------------


def _default_session(graph_path: str):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(graph_path, sess_options=options, providers=["CPUExecutionProvider"])


class LayaOnnx:
    """An exported Laya checkpoint, verified against its manifest at load.

    ``predict(state, questions, max_len=None, head_max_len=None, model=None)``
    mirrors ``laya.Router.predict`` for the keys the worker reads:
    ``{"answers": {qid: {choice, probabilities, answer_confidence,
    confidence}}, "routing": {"model": <checkpoint>}}``. ``model`` is
    accepted for call compatibility; an export holds one checkpoint.
    """

    def __init__(self, path: str, *, verify: bool = True, session_factory=None) -> None:
        manifest = verify_artifacts(path) if verify else _read_manifest(path)
        self.path = path
        self.checkpoint = str(manifest.get("checkpoint") or DEFAULT_CHECKPOINT)
        with open(os.path.join(path, CONFIG), encoding="utf-8") as f:
            self.cfg = json.load(f)
        if self.cfg.get("option_layout", "sequential") != "sequential":
            raise ArtifactsUnavailable(CODE_MANIFEST)
        self.max_len = int(self.cfg.get("max_len", 512))
        self.head_max_len = int(self.cfg.get("head_max_len", 192))
        self.temperatures = Temperatures.from_config(self.cfg)
        self.tok = LayaTokenizer.from_dir(path)
        self.session = (session_factory or _default_session)(os.path.join(path, manifest.get("graph") or GRAPH))

    def predict(self, state, questions: Dict[str, Dict[str, Any]], max_len: Optional[int] = None,
                head_max_len: Optional[int] = None, model: Optional[str] = None) -> Dict[str, Any]:
        if state is None:
            raise TypeError("state must not be None")
        qids = list(questions)
        internal = {qid: to_internal(questions[qid]) for qid in qids}
        answers: Dict[str, Any] = {}
        if qids:
            items = encode_state(self.tok, state, qids, internal,
                                 self.max_len if max_len is None else int(max_len),
                                 self.head_max_len if head_max_len is None else int(head_max_len))
            logits = self.session.run(["logits"], collate(items, self.tok.pad_token_id))[0]
            for row, qid in enumerate(qids):
                k = len(items[row]["markers"])
                t = self.temperatures.for_question(items[row]["qtype"], k)
                answers[qid] = decode_choice(logits[row], k, internal[qid], t)
        return {"answers": answers, "routing": {"model": self.checkpoint}}


def load(path: Optional[str] = None) -> LayaOnnx:
    return LayaOnnx(path or resolve_onnx_dir())
