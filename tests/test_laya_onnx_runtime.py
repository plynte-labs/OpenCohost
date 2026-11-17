"""laya-decision-layer-20261009 T7: the torch-free Laya runtime, unit level.

Everything here runs without the exported model: a tiny word-level tokenizer
built in ``tmp_path`` stands in for mmBERT's, and a fake session stands in for
ONNX Runtime. The real-model parity checks live in test_laya_onnx_parity.py.
No test imports torch or laya, and none touches the real default ONNX dir.
"""
from __future__ import annotations

import hashlib
import json
import os

import numpy as np
import pytest

from opencohost.core.decision import laya_onnx as lo

SPECIALS = ["<pad>", "<unk>", "<bos>", "<eos>", "<mask>"]
WORDS = (
    "choice question : is it a greeting ? yes no it is not hola mira esto look no_look "
    "one two three four five six seven eight nine ten"
).split()


def _write_tokenizer(dirpath):
    from tokenizers import Tokenizer, models, pre_tokenizers

    vocab = {tok: i for i, tok in enumerate(SPECIALS + WORDS)}
    tok = Tokenizer(models.WordLevel(vocab=vocab, unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tok.add_special_tokens(SPECIALS)
    tok.save(os.path.join(dirpath, "tokenizer.json"))
    with open(os.path.join(dirpath, "tokenizer_config.json"), "w", encoding="utf-8") as f:
        json.dump({
            "cls_token": "<bos>", "sep_token": "<eos>", "mask_token": "<mask>",
            "pad_token": "<pad>", "unk_token": "<unk>",
        }, f)
    return vocab


@pytest.fixture
def tokdir(tmp_path):
    vocab = _write_tokenizer(str(tmp_path))
    return str(tmp_path), vocab


@pytest.fixture
def tok(tokdir):
    return lo.LayaTokenizer.from_dir(tokdir[0])


def _q(criteria=None, instructions="is it a greeting ?"):
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": criteria if criteria is not None else {"yes": "it is", "no": "it is not"},
    }


# --- artifacts dir -----------------------------------------------------------


def test_default_dir_is_localappdata_and_independent_of_user_data_dir(monkeypatch):
    env = {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}
    expected = os.path.join(r"C:\Users\x\AppData\Local", "OpenCohost", "models", "laya", "multilingual")
    assert lo.default_onnx_dir(env) == expected
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", r"Z:\elsewhere")
    assert lo.default_onnx_dir(env) == expected


def test_default_dir_falls_back_without_localappdata():
    expected = os.path.join(
        os.path.expanduser("~"), ".local", "share", "OpenCohost", "models", "laya", "multilingual"
    )
    assert lo.default_onnx_dir({}) == expected


def test_env_override_wins(tmp_path):
    env = {"LOCALAPPDATA": "C:/x", "OPENCOHOST_LAYA_ONNX_DIR": str(tmp_path)}
    assert lo.resolve_onnx_dir(env) == str(tmp_path)
    assert lo.resolve_onnx_dir({"LOCALAPPDATA": "C:/x", "OPENCOHOST_LAYA_ONNX_DIR": "  "}) == lo.default_onnx_dir(
        {"LOCALAPPDATA": "C:/x"}
    )


def _fake_artifacts(dirpath, files=None):
    files = files or {name: name.encode() for name in lo.REQUIRED_FILES}
    for name, data in files.items():
        with open(os.path.join(dirpath, name), "wb") as f:
            f.write(data)
    manifest = {"format": 1, "checkpoint": "multilingual",
                "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    with open(os.path.join(dirpath, lo.MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return manifest


def test_laya_installed_resources_used_without_override(tmp_path):
    installed = tmp_path / "models" / "laya" / "multilingual"
    installed.mkdir(parents=True)
    _fake_artifacts(str(installed))
    env = {"OPENCOHOST_RESOURCES_DIR": str(tmp_path), "OPENCOHOST_LAYA_ONNX_DIR": ""}
    assert lo.resolve_onnx_dir(env) == str(installed)


def test_laya_override_is_authoritative(tmp_path):
    env = {"OPENCOHOST_RESOURCES_DIR": str(tmp_path), lo.ENV_DIR: "missing-explicit-model"}
    assert lo.resolve_onnx_dir(env) == "missing-explicit-model"
    assert not lo.artifacts_present(lo.resolve_onnx_dir(env))


def test_laya_partial_installed_assets_do_not_fall_back(tmp_path):
    installed = tmp_path / "models" / "laya" / "multilingual"
    installed.mkdir(parents=True)
    (installed / "laya.onnx").write_bytes(b"partial")
    selected = lo.resolve_onnx_dir({"OPENCOHOST_RESOURCES_DIR": str(tmp_path)})
    assert selected == str(installed)
    with pytest.raises(lo.ArtifactsUnavailable):
        lo.verify_artifacts(selected)


def test_missing_dir_is_unavailable(tmp_path):
    missing = str(tmp_path / "nope")
    assert lo.artifacts_present(missing) is False
    with pytest.raises(lo.ArtifactsUnavailable) as err:
        lo.verify_artifacts(missing)
    assert err.value.code == "onnx_artifacts_missing"


def test_complete_artifacts_verify(tmp_path):
    manifest = _fake_artifacts(str(tmp_path))
    assert lo.artifacts_present(str(tmp_path)) is True
    assert lo.verify_artifacts(str(tmp_path)) == manifest


def test_missing_listed_file_is_unavailable(tmp_path):
    _fake_artifacts(str(tmp_path))
    os.remove(tmp_path / "tokenizer.json")
    assert lo.artifacts_present(str(tmp_path)) is False
    with pytest.raises(lo.ArtifactsUnavailable) as err:
        lo.verify_artifacts(str(tmp_path))
    assert err.value.code == "onnx_artifacts_missing"


def test_manifest_without_a_required_file_is_invalid(tmp_path):
    files = {name: b"x" for name in lo.REQUIRED_FILES if name != "rl_agent_config.json"}
    _fake_artifacts(str(tmp_path), files)
    with pytest.raises(lo.ArtifactsUnavailable) as err:
        lo.verify_artifacts(str(tmp_path))
    assert err.value.code == "onnx_manifest_invalid"


def test_garbage_manifest_is_invalid(tmp_path):
    _fake_artifacts(str(tmp_path))
    (tmp_path / lo.MANIFEST).write_text("{not json", encoding="utf-8")
    with pytest.raises(lo.ArtifactsUnavailable) as err:
        lo.verify_artifacts(str(tmp_path))
    assert err.value.code == "onnx_manifest_invalid"


def test_sha256_mismatch_is_unavailable(tmp_path):
    _fake_artifacts(str(tmp_path))
    (tmp_path / lo.GRAPH).write_bytes(b"tampered")
    assert lo.artifacts_present(str(tmp_path)) is True  # stat only
    with pytest.raises(lo.ArtifactsUnavailable) as err:
        lo.verify_artifacts(str(tmp_path))
    assert err.value.code == "onnx_sha256_mismatch"


def test_manifest_path_traversal_is_invalid(tmp_path):
    _fake_artifacts(str(tmp_path))
    data = json.loads((tmp_path / lo.MANIFEST).read_text(encoding="utf-8"))
    data["files"]["../evil.onnx"] = "0" * 64
    (tmp_path / lo.MANIFEST).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(lo.ArtifactsUnavailable) as err:
        lo.verify_artifacts(str(tmp_path))
    assert err.value.code == "onnx_manifest_invalid"


# --- tokenizer + sequence building ---------------------------------------------


def test_tokenizer_special_ids(tok, tokdir):
    _, vocab = tokdir
    assert (tok.cls_token_id, tok.sep_token_id, tok.mask_token_id, tok.pad_token_id) == (
        vocab["<bos>"], vocab["<eos>"], vocab["<mask>"], vocab["<pad>"],
    )
    assert tok.mask_token == "<mask>"
    assert tok.encode("hola mira") == [vocab["hola"], vocab["mira"]]
    assert tok.encode("one two three", max_length=2) == [vocab["one"], vocab["two"]]


def test_render_options_choice():
    q = lo.to_internal({"type": "choice", "instructions": "x", "criteria": {"yes": "it is", "no": "", 3: None}})
    assert lo.render_options(q) == ["yes: it is", "no", "3"]
    listed = lo.to_internal({"type": "choice", "instructions": "x", "criteria": ["a", "b"]})
    assert lo.render_options(listed) == ["a", "b"]
    structured = lo.to_internal({"type": "choice", "instructions": "x", "criteria": {"a": {"k": 1}}})
    assert lo.render_options(structured) == ['a: {"k": 1}']


def test_unsupported_question_type_is_rejected():
    with pytest.raises(ValueError):
        lo.to_internal({"type": "score", "instructions": "x", "criteria": ["a", "b"]})


def test_build_sequence_layout(tok, tokdir):
    _, v = tokdir
    q = lo.to_internal(_q())
    state_ids = tok.encode("hola mira esto")
    ids, markers = lo.build_sequence(tok, q, state_ids, max_len=64, head_max_len=32)
    # [CLS] "choice question: <ins>" [SEP] [MASK] opt0 [MASK] opt1 [SEP] state [SEP]
    expected_head = [v["<bos>"]] + tok.encode("choice question: is it a greeting ?") + [v["<eos>"]]
    opt0 = [v["<mask>"]] + tok.encode(" yes: it is")
    opt1 = [v["<mask>"]] + tok.encode(" no: it is not")
    assert ids == expected_head + opt0 + opt1 + [v["<eos>"]] + state_ids + [v["<eos>"]]
    assert markers == [len(expected_head), len(expected_head) + len(opt0)]


def test_state_is_right_truncated_to_the_room_left(tok):
    q = lo.to_internal(_q())
    state_ids = tok.encode("one two three four five six seven eight nine ten")
    head_ids, _ = lo.build_sequence(tok, q, [], max_len=512, head_max_len=32)
    head_len = len(head_ids) - 1  # without the closing [SEP]
    ids, markers = lo.build_sequence(tok, q, state_ids, max_len=head_len + 4, head_max_len=32)
    assert len(ids) == head_len + 4
    assert ids[head_len:] == state_ids[:3] + [tok.sep_token_id]
    assert len(markers) == 2


def test_left_truncation_keeps_the_tail(tok):
    q = lo.to_internal(_q())
    state_ids = tok.encode("one two three four five six seven eight nine ten")
    head_ids, _ = lo.build_sequence(tok, q, [], max_len=512, head_max_len=32)
    head_len = len(head_ids) - 1
    ids, _ = lo.build_sequence(tok, q, state_ids, max_len=head_len + 4, head_max_len=32, truncate_left=True)
    assert ids[head_len:] == state_ids[-3:] + [tok.sep_token_id]


def test_options_are_capped_when_the_head_budget_is_tight(tok):
    long_desc = " ".join(["one two three four five"] * 4)
    q = lo.to_internal(_q({"yes": long_desc, "no": long_desc}))
    ids, markers = lo.build_sequence(tok, q, [], max_len=512, head_max_len=24)
    # opt_budget < 16 -> each option keeps max(4, (24 - 16) // 2) = 4 ids, [MASK] included
    assert markers[1] - markers[0] == 4
    assert ids[markers[0]] == tok.mask_token_id and ids[markers[1]] == tok.mask_token_id


def test_option_order_permutes_slots_and_unpermute_restores(tok):
    q = lo.to_internal({**_q(), "option_order": [1, 0]})
    ids, markers = lo.build_sequence(tok, q, [], max_len=64, head_max_len=32, option_order=q["option_order"])
    assert ids[markers[0] + 1: markers[1]] == tok.encode(" no: it is not")
    p = np.array([0.8, 0.2])  # slot order: slot 0 holds option 1
    np.testing.assert_allclose(lo.unpermute_probs(p, [1, 0]), [0.2, 0.8])
    np.testing.assert_allclose(lo.unpermute_probs(p, None), p)


def test_mask_token_in_state_and_text_is_neutralised(tok):
    state_ids = lo.encode_state_text(tok, "hola <mask> mira")
    assert tok.mask_token_id not in state_ids
    q = lo.to_internal(_q(instructions="is it <mask> greeting ?"))
    ids, markers = lo.build_sequence(tok, q, state_ids, max_len=64, head_max_len=32)
    assert [i for i, t in enumerate(ids) if t == tok.mask_token_id] == markers


def test_too_many_options_for_max_len_raises(tok):
    q = lo.to_internal(_q())
    with pytest.raises(ValueError):
        lo.encode_state(tok, "hola", ["q"], {"q": q}, max_len=8, head_max_len=32)


def test_collate_pads_and_masks():
    items = [
        {"ids": [2, 5, 6, 3], "markers": [1, 2], "qtype": 0},
        {"ids": [2, 7, 3], "markers": [1], "qtype": 0},
    ]
    b = lo.collate(items, pad_id=0)
    assert b["input_ids"].dtype == np.int64 and b["input_ids"].tolist() == [[2, 5, 6, 3], [2, 7, 3, 0]]
    assert b["attention_mask"].tolist() == [[1, 1, 1, 1], [1, 1, 1, 0]]
    assert b["marker_pos"].tolist() == [[1, 2], [1, 0]]
    assert b["marker_mask"].dtype == np.bool_ and b["marker_mask"].tolist() == [[True, True], [True, False]]
    assert b["qtype"].tolist() == [0, 0]


def test_collate_pads_single_option_markers_to_two():
    b = lo.collate([{"ids": [2, 4, 3], "markers": [1], "qtype": 0}], pad_id=0)
    assert b["marker_pos"].shape == (1, 2) and b["marker_mask"].tolist() == [[True, False]]


# --- decoding ----------------------------------------------------------------


def test_temperatures_are_clamped_and_bucketed():
    cfg = {"temperature": [2.0, 1.0, 1.0], "temperature_by_options": {"choice:3-5": 0.1, "choice:2": "x"}}
    temps = lo.Temperatures.from_config(cfg)
    assert temps.for_question(0, 2) == 1.0  # malformed bucket entry -> neutral 1.0
    assert temps.for_question(0, 3) == lo.TEMP_MIN  # 0.1 is clamped up
    assert temps.for_question(0, 7) == 2.0  # no bucket -> per-type temperature
    assert lo.Temperatures.from_config({}).for_question(0, 2) == 1.0


def test_decode_choice_matches_softmax_and_confidences():
    q = lo.to_internal(_q({"look": "a", "no_look": "b", "maybe": "c"}))
    logits = np.array([1.0, 2.0, 0.5, -10000.0], dtype=np.float32)
    ans = lo.decode_choice(logits, 3, q, 2.0)
    z = np.array([1.0, 2.0, 0.5]) / 2.0
    p = np.exp(z - z.max()) / np.exp(z - z.max()).sum()
    assert ans["choice"] == "no_look"
    assert ans["probabilities"] == {k: round(float(v), 4) for k, v in zip(["look", "no_look", "maybe"], p)}
    assert ans["answer_confidence"] == round(float(p.max()), 4)
    entropy = -(p * np.log(p)).sum()
    assert ans["confidence"] == round(float(1 - entropy / np.log(3)), 4)


# --- end to end with a fake session -------------------------------------------


class FakeSession:
    def __init__(self):
        self.feeds = []

    def run(self, outputs, feeds):
        self.feeds.append(feeds)
        n, k = feeds["marker_pos"].shape
        logits = np.tile(np.arange(k, dtype=np.float32), (n, 1))  # last option wins
        return [logits]


def _model_dir(tmp_path, tokdir_path):
    for name in ("tokenizer.json", "tokenizer_config.json"):
        with open(os.path.join(tokdir_path, name), "rb") as src, open(tmp_path / name, "wb") as dst:
            dst.write(src.read())
    (tmp_path / "rl_agent_config.json").write_text(
        json.dumps({"max_len": 64, "head_max_len": 32, "temperature": [1.0, 1.0, 1.0]}), encoding="utf-8"
    )
    (tmp_path / lo.GRAPH).write_bytes(b"graph")
    files = {n: (tmp_path / n).read_bytes() for n in lo.REQUIRED_FILES}
    manifest = {"format": 1, "checkpoint": "multilingual",
                "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    (tmp_path / lo.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    return str(tmp_path)


def test_predict_end_to_end_with_fake_session(tmp_path, tokdir):
    out = tmp_path / "model"
    out.mkdir()
    model_dir = _model_dir(out, tokdir[0])
    session = FakeSession()
    model = lo.LayaOnnx(model_dir, session_factory=lambda path: session)
    result = model.predict("hola mira", {"a": _q(), "b": _q({"x": "one", "y": "two", "z": "three"})})
    assert result["routing"] == {"model": "multilingual"}
    assert result["answers"]["a"]["choice"] == "no"
    assert result["answers"]["b"]["choice"] == "z"
    assert set(result["answers"]["a"]) == {"choice", "probabilities", "answer_confidence", "confidence"}
    feeds = session.feeds[0]
    assert set(feeds) == {"input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype"}
    assert feeds["input_ids"].shape[0] == 2


def test_load_refuses_tampered_artifacts(tmp_path, tokdir):
    out = tmp_path / "model"
    out.mkdir()
    model_dir = _model_dir(out, tokdir[0])
    (out / lo.GRAPH).write_bytes(b"other graph")
    with pytest.raises(lo.ArtifactsUnavailable) as err:
        lo.LayaOnnx(model_dir, session_factory=lambda path: FakeSession())
    assert err.value.code == "onnx_sha256_mismatch"


def test_module_never_imports_torch_laya_or_the_app():
    with open(lo.__file__, encoding="utf-8") as f:
        imports = [ln.strip() for ln in f if ln.strip().startswith(("import ", "from "))]
    roots = {ln.split()[1].split(".")[0] for ln in imports}
    assert roots <= {"__future__", "hashlib", "json", "math", "os", "typing", "numpy", "onnxruntime", "tokenizers"}
