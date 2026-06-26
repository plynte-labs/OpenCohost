from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol


class _ProcessLike(Protocol):
    stderr: object

    def poll(self) -> int | None: ...


@dataclass(frozen=True)
class OllamaStartupResult:
    status: str
    diagnostic: str
    process: Optional[_ProcessLike] = None


class OllamaStartupManager:
    """Start Ollama with observable readiness diagnostics."""

    def __init__(
        self,
        *,
        popen: Callable[..., _ProcessLike] = subprocess.Popen,
        is_ready: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        base_env: Optional[Mapping[str, str]] = None,
        timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.5,
        creationflags: Optional[int] = None,
        stdout_log_path: Optional[str] = None,
        stderr_log_path: Optional[str] = None,
        stderr_snippet_chars: int = 500,
    ) -> None:
        self._popen = popen
        self._is_ready = is_ready
        self._sleep = sleep
        self._monotonic = monotonic
        self._base_env = base_env
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._creationflags = creationflags
        self._stdout_log_path = stdout_log_path
        self._stderr_log_path = stderr_log_path
        self._stderr_snippet_chars = stderr_snippet_chars

    def start_and_wait(self, ollama_exe: str, ollama_models_dir: str) -> OllamaStartupResult:
        if self._is_ready():
            return OllamaStartupResult(
                status="already_running",
                diagnostic="Ollama ya estaba respondiendo antes de iniciar un proceso nuevo.",
            )

        env = dict(self._base_env if self._base_env is not None else os.environ)
        env["OLLAMA_MODELS"] = ollama_models_dir
        # RAM ceilings (ram_llm_hardening_20260626 Phase 0 / A3): one runner, one
        # loaded model — each runner/model carries its own KV cache. setdefault so
        # an operator who already exported these keeps their chosen value.
        env.setdefault("OLLAMA_NUM_PARALLEL", "1")
        env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
        creationflags = self._creationflags
        if creationflags is None:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        stdout_target = subprocess.PIPE
        stderr_target = subprocess.PIPE
        stdout_handle = None
        stderr_handle = None
        try:
            if self._stdout_log_path:
                Path(self._stdout_log_path).parent.mkdir(parents=True, exist_ok=True)
                stdout_handle = open(self._stdout_log_path, "a", encoding="utf-8")
                stdout_target = stdout_handle
            if self._stderr_log_path:
                Path(self._stderr_log_path).parent.mkdir(parents=True, exist_ok=True)
                stderr_handle = open(self._stderr_log_path, "a", encoding="utf-8")
                stderr_target = stderr_handle

            process = self._popen(
                [ollama_exe, "serve"],
                stdout=stdout_target,
                stderr=stderr_target,
                text=True,
                creationflags=creationflags,
                env=env,
            )
        except Exception as exc:
            return OllamaStartupResult(
                status="process_exited_early",
                diagnostic=f"No se pudo iniciar Ollama: {exc}",
            )
        finally:
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()

        deadline = self._monotonic() + self._timeout_seconds
        while True:
            if self._is_ready():
                return OllamaStartupResult(
                    status="ready",
                    diagnostic="Ollama inicio correctamente.",
                    process=process,
                )

            exit_code = process.poll()
            if exit_code is not None:
                snippet = self._read_stderr_snippet(process)
                return OllamaStartupResult(
                    status="process_exited_early",
                    diagnostic=(
                        f"Ollama termino antes de estar listo (codigo {exit_code}). "
                        f"stderr: {snippet or 'sin stderr capturado'}"
                    ),
                    process=process,
                )

            now = self._monotonic()
            if now >= deadline:
                snippet = self._read_stderr_snippet(process)
                return OllamaStartupResult(
                    status="timeout_waiting_ready",
                    diagnostic=(
                        f"Ollama no respondio en {self._timeout_seconds:.0f}s. "
                        f"OLLAMA_MODELS={ollama_models_dir}. "
                        f"stderr: {snippet or 'sin stderr capturado'}"
                    ),
                    process=process,
                )

            self._sleep(min(self._poll_interval_seconds, max(0.0, deadline - now)))

    def _read_stderr_snippet(self, process: _ProcessLike) -> str:
        if process.poll() is None:
            return self._read_log_tail(self._stderr_log_path)
        stderr = getattr(process, "stderr", None)
        read = getattr(stderr, "read", None)
        if not callable(read):
            text = self._read_log_tail(self._stderr_log_path)
        else:
            try:
                text = read() or ""
            except Exception:
                text = ""
        return str(text).strip()[: self._stderr_snippet_chars]

    def _read_log_tail(self, path: Optional[str]) -> str:
        if not path:
            return ""
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")[-self._stderr_snippet_chars :]
        except Exception:
            return ""
