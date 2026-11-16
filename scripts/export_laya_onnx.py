"""Export a Laya checkpoint to ONNX for the torch-free decision worker
(laya-decision-layer-20261009 T6).

Run ONCE under the Laya env (it needs torch, laya, onnx and onnxscript), never
under the app's runtime env:

    set HF_HOME=<hf-cache>
    set HF_HUB_OFFLINE=1
    <laya-env-python> scripts\\export_laya_onnx.py ^
        --checkpoint <hf-cache>\\hub\\models--convaiinnovations--laya\\snapshots\\<rev>\\multilingual

``--out`` defaults to the worker's default artifacts dir
(``%LOCALAPPDATA%/OpenCohost/models/laya/multilingual``, see
``opencohost/core/decision/laya_onnx.py::default_onnx_dir``). The checkpoint
dir is only read. The output dir receives:

  laya.onnx (+ laya.onnx.data when the exporter writes external weights)
  tokenizer.json, tokenizer_config.json   (copied from <checkpoint>/tokenizer/)
  rl_agent_config.json                    (max_len, head_max_len, temperatures)
  manifest.json                           (sha256 of every file above + source revision)

``manifest.json`` is written last, so its presence marks a complete export.
The worker refuses to load (state ``unavailable``) when any digest differs.

The export itself is ``export_to_onnx`` from the upstream Laya repository,
``scripts/export_onnx.py`` at commit 1adc59f7e371deb601fcfa18a14e25db238addcc,
https://github.com/NandhaKishorM/laya/blob/main/scripts/export_onnx.py
(Copyright ConvAI Innovations, Apache License 2.0). It is reproduced below
with the print calls dropped and two comments shortened; the dummy shapes,
input/output names, dynamic axes and opset are upstream's, which is what
``laya.ONNXAgent`` and our port in ``laya_onnx.py`` feed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAYA_ONNX = os.path.join(_HERE, os.pardir, "opencohost", "core", "decision", "laya_onnx.py")

GRAPH = "laya.onnx"


def _laya_onnx_module():
    """``laya_onnx.py`` loaded by path, so this script never imports the app package."""
    spec = importlib.util.spec_from_file_location("laya_onnx_for_export", os.path.abspath(_LAYA_ONNX))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- upstream: NandhaKishorM/laya scripts/export_onnx.py (Apache-2.0) ---------


def export_to_onnx(model_id_or_path: str, output_path: str):
    import torch
    from laya.agent import Agent

    agent = Agent(model_id_or_path, compile=False, device="cpu")

    # 1. Dummy tensors for tracing. torch.export specialises any dimension that is 1 (or equal
    # to another) at trace time, so a batch-1 dummy baked batch=1 into the decision head's
    # attention: the export ran at batch 1 and failed at batch >= 2 (#695). Batch, sequence and
    # marker counts are therefore all > 1 and pairwise different. laya-ts's exporter does the same.
    batch, seq_len, num_markers = 2, 17, 3
    dummy_input_ids = torch.randint(0, 100, (batch, seq_len), dtype=torch.long)
    dummy_attention_mask = torch.ones((batch, seq_len), dtype=torch.long)
    dummy_marker_pos = torch.tensor([[1, 5, 9]] * batch, dtype=torch.long)
    dummy_marker_mask = torch.ones((batch, num_markers), dtype=torch.bool)
    dummy_qtype = torch.zeros(batch, dtype=torch.long)

    inputs = (
        dummy_input_ids,
        dummy_attention_mask,
        dummy_marker_pos,
        dummy_marker_mask,
        dummy_qtype,
    )

    # 2. Dynamic dimensions (upstream comment: `dynamic_axes` is accepted by every supported
    # torch, and the dynamo exporter converts it to Dims).
    dynamic_axes = {
        "input_ids": {0: "batch_size", 1: "seq_len"},
        "attention_mask": {0: "batch_size", 1: "seq_len"},
        "marker_pos": {0: "batch_size", 1: "num_markers"},
        "marker_mask": {0: "batch_size", 1: "num_markers"},
        "qtype": {0: "batch_size"},
        "logits": {0: "batch_size", 1: "num_markers"},
        "act_logits": {0: "batch_size"},
    }
    input_names = ["input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype"]
    output_names = ["logits", "act_logits"]

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    torch.onnx.export(
        agent.model,
        inputs,
        output_path,
        export_params=True,
        opset_version=18,
        do_constant_folding=True,
        input_names=input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
    )


# --- end of upstream code -----------------------------------------------------


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_revision(checkpoint: str) -> str | None:
    """The HF snapshot revision in ``.../snapshots/<rev>/<subfolder>``, if any."""
    parts = os.path.normpath(os.path.abspath(checkpoint)).split(os.sep)
    if "snapshots" in parts:
        i = parts.index("snapshots")
        if i + 1 < len(parts) and re.fullmatch(r"[0-9a-f]{40}", parts[i + 1]):
            return parts[i + 1]
    return None


def _versions() -> dict:
    out = {"python": sys.version.split()[0]}
    for name in ("torch", "laya", "onnx", "onnxscript", "transformers"):
        try:
            mod = __import__(name)
            out[name] = str(getattr(mod, "__version__", "?"))
        except Exception:
            out[name] = None
    return out


def export(checkpoint: str, out_dir: str) -> dict:
    """Export ``checkpoint`` into ``out_dir`` and write ``manifest.json``.
    Returns the manifest."""
    lo = _laya_onnx_module()
    checkpoint = os.path.abspath(checkpoint)
    cfg_path = os.path.join(checkpoint, "rl_agent_config.json")
    tok_dir = os.path.join(checkpoint, "tokenizer")
    for path in (cfg_path, os.path.join(tok_dir, "tokenizer.json"), os.path.join(tok_dir, "tokenizer_config.json")):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
    with open(cfg_path, encoding="utf-8") as f:
        if json.load(f).get("option_layout", "sequential") != "sequential":
            raise ValueError("only sequential-layout checkpoints export to the five-input graph")

    os.makedirs(out_dir, exist_ok=True)
    stale = os.path.join(out_dir, lo.MANIFEST)
    if os.path.exists(stale):
        os.remove(stale)  # an interrupted re-export must not look complete

    export_to_onnx(checkpoint, os.path.join(out_dir, GRAPH))
    shutil.copyfile(cfg_path, os.path.join(out_dir, "rl_agent_config.json"))
    for name in ("tokenizer.json", "tokenizer_config.json"):
        shutil.copyfile(os.path.join(tok_dir, name), os.path.join(out_dir, name))

    files = sorted(
        name for name in os.listdir(out_dir)
        if name != lo.MANIFEST and os.path.isfile(os.path.join(out_dir, name))
    )
    manifest = {
        "format": 1,
        "graph": GRAPH,
        "checkpoint": os.path.basename(checkpoint.rstrip("\\/")),
        "source": {
            "repo": "convaiinnovations/laya",
            "revision": _source_revision(checkpoint),
            "subfolder": os.path.basename(checkpoint.rstrip("\\/")),
        },
        "exporter": {
            "upstream": "https://github.com/NandhaKishorM/laya/blob/1adc59f7e371deb601fcfa18a14e25db238addcc/scripts/export_onnx.py",
            "versions": _versions(),
        },
        "files": {name: _sha256(os.path.join(out_dir, name)) for name in files},
    }
    tmp = os.path.join(out_dir, lo.MANIFEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, os.path.join(out_dir, lo.MANIFEST))
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkpoint", required=True, help="local Laya checkpoint dir (e.g. <snapshot>/multilingual)")
    parser.add_argument("--out", default=None, help="output dir (default: the worker's default ONNX dir)")
    args = parser.parse_args(argv)
    out_dir = args.out or _laya_onnx_module().default_onnx_dir()
    manifest = export(args.checkpoint, out_dir)
    print(json.dumps({"out": out_dir, "files": manifest["files"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
