"""laya-decision-layer-20261009 T7: parity of the torch-free Laya runtime with
laya's own torch path.

The golden file ``tests/fixtures/laya_parity/laya_parity.json`` was recorded
by ``scripts/make_laya_parity_fixtures.py`` under the Laya env (laya 0.4.1,
``Router.predict(model="multilingual")``). It holds the collated model inputs
and the final answers for our four decision questions over 20 states.

The real tokenizer (tokenizer.json, 34 MB) and graph (~650 MB) are not
committed, so the input and probability checks are opt-in: point
``OPENCOHOST_LAYA_PARITY_DIR`` at an exported artifacts dir (see
``scripts/export_laya_onnx.py``). Without it they skip; the tests never read
the app's default ONNX dir on their own. No test imports torch or laya.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from opencohost.core.decision import laya_onnx as lo
from opencohost.core.decision.questions import QUESTIONS

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "laya_parity", "laya_parity.json")
PARITY_ENV = "OPENCOHOST_LAYA_PARITY_DIR"
PROB_TOL = 1e-3


def _golden():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _parity_dir(*required):
    path = os.environ.get(PARITY_ENV, "").strip()
    if not path:
        pytest.skip(
            f"{PARITY_ENV} unset: the real tokenizer.json (34 MB) and graph are not committed; "
            "point it at an export from scripts/export_laya_onnx.py to run parity"
        )
    missing = [name for name in required if not os.path.isfile(os.path.join(path, name))]
    if missing:
        pytest.skip(f"{PARITY_ENV}={path} lacks {missing}")
    return path


def test_fixture_questions_are_the_app_questions():
    # The fixture predates the ``web`` decision (web-fetch-laya-20261009), a
    # plain 2-option choice like the others; every question it covers must
    # still be byte-identical to the app's.
    golden = _golden()["questions"]
    app = json.loads(json.dumps(dict(QUESTIONS)))
    assert set(app) - set(golden) == {"web", "clock"}
    assert {k: app[k] for k in golden} == golden


def test_fixture_covers_spanish_english_and_truncation():
    cases = _golden()["cases"]
    assert len(cases) >= 20
    assert any(len(call["inputs"]["input_ids"][0]) == 1024 for c in cases for call in c["calls"])


def test_input_builder_reproduces_fixture_ids_and_masks():
    path = _parity_dir("tokenizer.json", "tokenizer_config.json", "rl_agent_config.json")
    tok = lo.LayaTokenizer.from_dir(path)
    with open(os.path.join(path, "rl_agent_config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    golden = _golden()
    for case in golden["cases"]:
        for call in case["calls"]:
            qids = call["qids"]
            internal = {qid: lo.to_internal(golden["questions"][qid]) for qid in qids}
            items = lo.encode_state(tok, case["state"], qids, internal, cfg["max_len"], cfg["head_max_len"])
            batch = lo.collate(items, tok.pad_token_id)
            for name, expected in call["inputs"].items():
                got = batch[name]
                if name == "marker_pos" or name == "marker_mask":
                    got = got[:, : len(expected[0])]
                assert got.tolist() == expected, (name, qids, len(case["state"]))


@pytest.fixture(scope="module")
def onnx_model():
    path = _parity_dir(*lo.REQUIRED_FILES, lo.MANIFEST)
    return lo.LayaOnnx(path)


def test_probabilities_match_torch(onnx_model):
    golden = _golden()
    max_diff = 0.0
    for case in golden["cases"]:
        for call in case["calls"]:
            qs = {qid: golden["questions"][qid] for qid in call["qids"]}
            got = onnx_model.predict(case["state"], qs)["answers"]
            for qid, want in call["answers"].items():
                assert got[qid]["choice"] == want["choice"], (qid, len(case["state"]))
                for key in ("answer_confidence", "confidence"):
                    max_diff = max(max_diff, abs(got[qid][key] - want[key]))
                for option, p in want["probabilities"].items():
                    max_diff = max(max_diff, abs(got[qid]["probabilities"][option] - p))
    print(f"laya onnx parity: max abs diff {max_diff:.6f}")
    assert max_diff <= PROB_TOL


def test_routing_model_is_the_checkpoint(onnx_model):
    result = onnx_model.predict("hola", {"vision": QUESTIONS["vision"]})
    assert result["routing"]["model"] == "multilingual"
    assert np.isclose(sum(result["answers"]["vision"]["probabilities"].values()), 1.0, atol=2e-4)
