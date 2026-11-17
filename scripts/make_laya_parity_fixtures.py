"""Golden parity fixtures for the torch-free Laya runtime
(laya-decision-layer-20261009 T7).

Records, from laya's own torch path (``laya.Router``, ``multilingual``
checkpoint), the encoded model inputs and the final answers for our decision
questions over ~20 Spanish/English states. ``tests/test_laya_onnx_parity.py``
replays them against ``opencohost/core/decision/laya_onnx.py`` with no torch.

Two steps, because this script runs under the Laya env, which cannot import the
app package:

    # 1. dump the app's question sets (any env with the app's deps)
    <app-env-python> -c "import json; from opencohost.core.decision.questions import QUESTIONS; print(json.dumps(dict(QUESTIONS), ensure_ascii=False))" > questions.json

    # 2. record the fixtures under the Laya env (HF cache read only)
    set HF_HOME=<hf-cache>
    set HF_HUB_OFFLINE=1
    <laya-env-python> scripts\\make_laya_parity_fixtures.py --questions questions.json

Output: ``tests/fixtures/laya_parity/laya_parity.json``. It holds the states,
the question sets, per call the collated ``input_ids``/``attention_mask``/
``marker_pos``/``marker_mask``/``qtype`` and laya's answers. No weights.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(_HERE, os.pardir, "tests", "fixtures", "laya_parity", "laya_parity.json")
CHECKPOINT = "multilingual"

# The calls the app makes: the owner trio in one forward pass, and the viewer
# ``respond`` decision on its own.
CALLS = (("vision", "memory", "reply_length"), ("respond",))

_LONG = (
    "Bueno, te cuento lo que pasó hoy en el stream porque fue una locura total. "
    "Empezamos jugando al nuevo juego de terror, luego se cayó el internet dos veces, "
    "el chat se puso a pedir que cantara y al final terminamos hablando de recetas. "
) * 30 + "Entonces, ¿qué opinas, lo repetimos mañana?"

STATES = (
    # The parent smoke set (Spanish).
    "hola",
    "jajaja",
    "¿qué ves en mi pantalla?",
    "mira este jefe, ¿cómo lo mato?",
    "¿te acuerdas de lo que te conté ayer de mi perro?",
    "¿cuál es mi comida favorita?",
    "explícame paso a paso cómo funciona la fotosíntesis",
    "¿qué opinas de los lunes?",
    "lee lo que dice ese texto",
    "dame una lista de 5 juegos de terror",
    # English.
    "hey Kira, what's up?",
    "look at this boss, how do I beat it?",
    "do you remember what I told you about my sister last week?",
    "explain in detail how ranked matchmaking works and compare it with casual",
    "lol",
    "what do you think about pineapple on pizza?",
    # Edge inputs: mixed language, a literal mask token, empty-ish, long (truncated).
    "jaja ok pero what is that thing on the left of the screen?",
    "escribe <mask> en el chat",
    "   ",
    _LONG,
)


def _plain(x):
    return x.tolist() if hasattr(x, "tolist") else x


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", required=True, help="JSON dump of opencohost QUESTIONS")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args(argv)
    with open(args.questions, encoding="utf-8-sig") as f:
        questions = json.load(f)

    import laya
    from laya.common import collate_items

    router = laya.Router(preload=False)
    agent = router.load(CHECKPOINT)

    cases = []
    for state in STATES:
        calls = []
        for qids in CALLS:
            qs = {qid: questions[qid] for qid in qids}
            internal = {qid: agent._to_internal(qs[qid]) for qid in qids}
            items = agent._encode_state(state, list(qids), internal)
            batch = collate_items([items], agent.tok.pad_token_id)
            result = router.predict(state, qs, model=CHECKPOINT)
            answers = {
                qid: {
                    key: _plain(result["answers"][qid][key])
                    for key in ("choice", "probabilities", "answer_confidence", "confidence")
                }
                for qid in qids
            }
            calls.append({
                "qids": list(qids),
                "inputs": {
                    name: _plain(batch[name])
                    for name in ("input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype")
                },
                "answers": answers,
                "routing_model": (result.get("routing") or {}).get("model"),
            })
        cases.append({"state": state, "calls": calls})

    payload = {
        "source": {
            "laya": getattr(laya, "__version__", "?"),
            "checkpoint": CHECKPOINT,
            "path": "laya.Router.predict(model='multilingual'); inputs from Agent._encode_state + collate_items",
        },
        "questions": questions,
        "cases": cases,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"wrote {len(cases)} cases to {os.path.abspath(args.out)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
