"""Shared helpers for real-env tests: availability probes + hard timeout."""
from __future__ import annotations
import threading
import pytest


def require_ollama():
    """Skip unless the local Ollama daemon answers list()."""
    try:
        import ollama
        ollama.list()
    except Exception as exc:  # transport down, package issue, etc.
        pytest.skip(f"Ollama not reachable: {exc!r}")


def _canon(tag: str) -> str:
    tag = (tag or "").strip()
    return tag[:-7] if tag.endswith(":latest") else tag


def require_model(tag: str):
    """Skip unless `tag` is present in `ollama list` (handles :latest)."""
    require_ollama()
    import ollama
    resp = ollama.list()
    models = getattr(resp, "models", None)
    if models is None and isinstance(resp, dict):
        models = resp.get("models", [])
    installed = set()
    for m in models or []:
        name = m.get("model") if isinstance(m, dict) else getattr(m, "model", None)
        if name:
            installed.add(_canon(name))
    want = _canon(tag)
    if want not in installed and not any(e.startswith(want + ":") for e in installed):
        pytest.skip(f"model {tag!r} not installed (have: {sorted(installed)})")


def run_bounded(fn, *, seconds: float, **kwargs):
    """Run fn() in a daemon thread; fail if it exceeds `seconds`. Re-raises fn errors.

    Manual hard-timeout because pytest-timeout is not installed in flux_env.
    """
    box = {}

    def worker():
        try:
            box["result"] = fn(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        pytest.fail(f"real-env call exceeded hard timeout of {seconds:.1f}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")
