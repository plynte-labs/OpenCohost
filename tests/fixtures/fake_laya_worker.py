"""Fake Laya worker for laya-decision-layer-20261009 client tests.

Speaks the real worker's JSON-lines protocol on stdio with canned answers, so
no test ever imports ``laya`` or loads a model. Stdlib only: it runs under
``python -I``. Behavior comes from env:

  FAKE_LAYA_MODE     ok | hang | crash | crash_once | malformed | not_ready |
                     slow_ping | exit_at_start | unavailable
  FAKE_LAYA_ANSWERS  JSON {qid: answer}; default: first option at 0.9
  FAKE_LAYA_SPAWNS   file that gets one line per process start
  FAKE_LAYA_ENV_OUT  file that gets the engine env the client passed (JSON)
"""
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_LAYA_MODE", "ok")
OUT = sys.stdout.buffer


def _send(obj):
    OUT.write((json.dumps(obj) + "\n").encode("utf-8"))
    OUT.flush()


def _spawn_count():
    path = os.environ.get("FAKE_LAYA_SPAWNS")
    if not path:
        return 1
    with open(path, "a", encoding="utf-8") as f:
        f.write("spawn\n")
    with open(path, encoding="utf-8") as f:
        return len(f.read().splitlines())


def _answers(questions):
    canned = json.loads(os.environ.get("FAKE_LAYA_ANSWERS", "{}") or "{}")
    out = {}
    for qid, q in questions.items():
        if qid in canned:
            out[qid] = canned[qid]
        else:
            out[qid] = {"choice": next(iter(q["criteria"])), "answer_confidence": 0.9}
    return out


def _record_env():
    path = os.environ.get("FAKE_LAYA_ENV_OUT")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: os.environ.get(k) for k in ("OPENCOHOST_LAYA_ENGINE", "OPENCOHOST_LAYA_ONNX_DIR")}, f)


def main():
    spawn = _spawn_count()
    _record_env()
    if MODE == "exit_at_start":
        sys.exit(3)
    for raw in sys.stdin.buffer:
        msg = json.loads(raw.decode("utf-8"))
        if msg.get("op") == "ping":
            if MODE == "slow_ping":
                time.sleep(30)
            if MODE == "unavailable":
                _send({"id": msg.get("id"), "ok": True, "ready": False, "error": "onnx_sha256_mismatch"})
                continue
            _send({"id": msg.get("id"), "ok": True, "ready": MODE != "not_ready"})
            continue
        if MODE == "hang":
            time.sleep(30)
            continue
        if MODE == "crash" or (MODE == "crash_once" and spawn == 1):
            sys.exit(5)
        if MODE == "malformed":
            OUT.write(b"this is not json\n")
            OUT.flush()
            continue
        _send({
            "id": msg["id"],
            "ok": True,
            "answers": _answers(msg["questions"]),
            "routing_model": "fake",
            "state_len": len(msg["state"]),
        })


if __name__ == "__main__":
    main()
