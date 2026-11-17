"""Laya decision worker (laya-decision-layer-20261009).

Standalone script, run as ``<python> -I <path>/laya_worker.py``. It must never
import the app package. Two engines, picked by ``OPENCOHOST_LAYA_ENGINE``:

  onnx   (default) the exported model under the app's own interpreter, through
         the sibling ``laya_onnx.py`` (numpy + tokenizers + onnxruntime), loaded
         by file path because ``-I`` keeps the script dir off ``sys.path``.
         Artifacts come from ``OPENCOHOST_LAYA_ONNX_DIR`` and are checked
         against their manifest sha256 at load; a missing or tampered export
         answers the ping as not ready with a reason code
         (``onnx_artifacts_missing``, ``onnx_manifest_invalid``,
         ``onnx_sha256_mismatch``, ``onnx_load_<Error>``).
  torch  ``laya.Router`` itself, under ``OPENCOHOST_LAYA_PYTHON`` (an env with
         laya, torch and transformers 5.x).

Protocol: JSON lines on stdio, one reply per line read.
  request   {"id", "state", "questions", "max_len"?}
  response  {"id", "ok", "answers": {qid: {"choice"?, "probabilities"?,
             "answer_confidence"?, "confidence"?}}, "routing_model"?, "error"?}
  health    {"op": "ping", "id"?} -> {"id", "ok": true, "ready": bool, "error"?}

The router is preloaded (one warm predict) before the loop starts, so the
first ping answers only once the model is ready. Any failure is a short
error code; the state is never printed, logged or echoed. Library output is
sent to stderr so it cannot corrupt the protocol on stdout.

Checkpoint: ``OPENCOHOST_LAYA_CHECKPOINT`` empty lets ``laya.Router`` route
by language on its own (Spanish goes to the multilingual checkpoint); a value
pins that checkpoint on every predict (``model=<name>``). The onnx engine
holds the one exported checkpoint and ignores the pin.

Laya API (laya 0.4.1, checked in the real env on 2026-10-09):
``Router(preload=True)``; ``router.predict(state, questions, model=None,
max_len=None)`` returns ``{"answers": {qid: {"action", "answer_confidence",
"choice", "confidence", "probabilities", "type"}}, "routing": {"model"}}``.
"""
import importlib.util
import json
import os
import sys

ENGINE_ONNX = "onnx"
ENGINE_TORCH = "torch"
_ANSWER_KEYS = ("choice", "probabilities", "answer_confidence", "confidence")
_WARM_STATE = "hola, ¿qué tal?"
_WARM_QUESTIONS = {
    "warm": {
        "type": "choice",
        "instructions": "Is this a greeting?",
        "criteria": {"yes": "It is a greeting.", "no": "It is not a greeting."},
    }
}


def _import_laya():
    import laya  # noqa: PLC0415 -- only in the worker env

    return laya


def _load_sibling(name):
    """A module next to this script, by file path (``-I`` drops the script dir)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location("laya_worker_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_laya_onnx():
    return _load_sibling("laya_onnx")


def resolve_engine(raw):
    """``torch`` only when asked for; anything else is the default ``onnx``."""
    return ENGINE_TORCH if str(raw or "").strip().lower() == ENGINE_TORCH else ENGINE_ONNX


def load_router(engine=ENGINE_TORCH, onnx_dir=None):
    """The preloaded model behind ``predict``. The ONE place that knows how
    each engine loads."""
    if engine == ENGINE_ONNX:
        runtime = _import_laya_onnx()
        return runtime.LayaOnnx(onnx_dir or runtime.resolve_onnx_dir())
    laya = _import_laya()
    return laya.Router(preload=True)


def load_error_code(engine, exc):
    """Short reason code for a failed load, sent with the ping. The torch
    engine keeps its historical bare not-ready answer."""
    if engine != ENGINE_ONNX:
        return None
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.startswith("onnx_"):
        return code
    return f"onnx_load_{type(exc).__name__}"


def _predict(router, state, questions, max_len, checkpoint=""):
    """Run one forward pass. The ONE place that knows Laya's predict call and
    result shape; returns ``(answers, routing_model)`` or raises ValueError."""
    kwargs = {"max_len": max_len} if isinstance(max_len, int) and max_len > 0 else {}
    if checkpoint:
        kwargs["model"] = checkpoint
    result = router.predict(state, questions, **kwargs)
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise ValueError("bad_result")
    answers = {}
    for qid, raw in result["answers"].items():
        if not isinstance(raw, dict):
            continue
        answer = {}
        for key in _ANSWER_KEYS:
            value = raw.get(key)
            if value is None:
                continue
            if key == "probabilities" and isinstance(value, dict):
                value = {str(k): float(v) for k, v in value.items()}
            elif key != "choice":
                value = float(value)
            answer[key] = value
        answers[str(qid)] = answer
    routing = result.get("routing")
    model = routing.get("model") if isinstance(routing, dict) else None
    return answers, (str(model) if model is not None else None)


def handle(router, msg, checkpoint="", load_error=None):
    """One reply for one decoded message. Never raises."""
    if not isinstance(msg, dict):
        return {"id": None, "ok": False, "error": "bad_request"}
    msg_id = msg.get("id")
    if msg.get("op") == "ping":
        reply = {"id": msg_id, "ok": True, "ready": router is not None}
        if router is None and load_error:
            reply["error"] = load_error
        return reply
    if router is None:
        return {"id": msg_id, "ok": False, "error": "not_ready"}
    state, questions = msg.get("state"), msg.get("questions")
    if not isinstance(state, str) or not isinstance(questions, dict) or not questions:
        return {"id": msg_id, "ok": False, "error": "bad_request"}
    try:
        answers, routing_model = _predict(router, state, questions, msg.get("max_len"), checkpoint)
    except ValueError:
        return {"id": msg_id, "ok": False, "error": "bad_result"}
    except Exception as exc:
        return {"id": msg_id, "ok": False, "error": f"predict_{type(exc).__name__}"}
    reply = {"id": msg_id, "ok": True, "answers": answers}
    if routing_model is not None:
        reply["routing_model"] = routing_model
    return reply


def serve(router, stdin, stdout, checkpoint="", load_error=None):
    """Read requests until EOF; one JSON reply per non-empty line."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
        except Exception:
            reply = {"id": None, "ok": False, "error": "bad_json"}
        else:
            reply = handle(router, msg, checkpoint, load_error)
        stdout.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        stdout.flush()


def main():
    proto_in, proto_out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # library prints must never reach the protocol
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    checkpoint = os.environ.get("OPENCOHOST_LAYA_CHECKPOINT", "").strip()
    engine = resolve_engine(os.environ.get("OPENCOHOST_LAYA_ENGINE"))
    onnx_dir = os.environ.get("OPENCOHOST_LAYA_ONNX_DIR", "").strip() or None
    router, load_error = None, None
    try:
        router = load_router(engine, onnx_dir)
        _predict(router, _WARM_STATE, _WARM_QUESTIONS, None, checkpoint)
    except Exception as exc:
        router = None
        load_error = load_error_code(engine, exc)
        sys.stderr.write(f"laya worker: {engine} load failed ({load_error or type(exc).__name__})\n")
    serve(router, proto_in, proto_out, checkpoint, load_error)


if __name__ == "__main__":
    main()
