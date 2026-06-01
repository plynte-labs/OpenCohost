# Exploration — Streaming Speech Pipeline

## Context

Track B targets the main latency bottleneck: the current LLM path waits for a full non-streaming response before TTS can begin. Existing TTS already chunks text once `_hablar()` receives the complete response, so the first useful seam is before `_hablar()`.

## Current Flow

- `MotorVocalIA.run()` and priority paths enqueue/process `"process_context"` commands.
- `MotorVocalIA._ejecutar_inferencia()` calls `_generar_dialogo(...)` and then `_hablar(dialogo, source=source)`.
- `_generar_dialogo()` is non-streaming: it builds messages/options, calls Ollama once, applies retries/guardrails/sanitization, commits history, and returns full text.
- `_hablar(texto_a_generar, source="direct")` is the legacy speech boundary and must keep its public behavior.

## Legacy `_hablar()` Boundary

Preserve the existing `_hablar()` call contract:

- sets speaking state and source
- emits `speaking_start` / `speaking_end`
- validates heavy TTS reference WAV
- cleans and chunks text
- synthesizes Edge-TTS or Qwen3-TTS chunks
- plays chunks in order
- cleans temporary files
- resets state on success, failure, or early exit

Do not rewrite `_hablar()` wholesale. It remains the fallback and behavior-preservation oracle.

## Existing Splitting

Current sentence-ish splitting lives inside `_hablar()` only:

- strips actions/quotes/newlines
- splits with `re.split(r'(?<=[.!?])\s+', texto_limpio)`
- further splits long chunks on comma/semicolon
- filters tiny chunks

There is no standalone streaming sentence splitter yet.

## Proposed Strangler Architecture

Keep the legacy path:

```text
_ejecutar_inferencia() -> _generar_dialogo() -> _hablar()
```

Add a gated streaming path:

```text
_ejecutar_inferencia() -> StreamingSpeechPipeline.run(...)
```

Candidate components:

- `OllamaStreamingLLM`: yields text deltas from Ollama streaming.
- `SentenceSplitter`: buffers deltas and emits stable sentence chunks.
- `TTSChunkSynthesizer`: synthesizes each emitted sentence using the same TTS rules.
- `PlaybackQueue`: preserves order and starts playback as soon as the first chunk is ready.
- `CancellationToken`: stops LLM consumption, TTS enqueue, and playback safely.

## Validated Architecture Constraints

- CustomTkinter is not thread-safe. Pipeline workers should emit motor events only; UI mutation stays in `AppShell` and is scheduled through `_safe_after()` / `.after()`.
- Spanish sentence splitting needs explicit handling for opening punctuation (`¿`, `¡`) and common abbreviations (`Dr.`, `Dra.`, `Sr.`, `Sra.`, `ej.`, `vs.`, `pág.`).
- Qwen3-TTS/heavy synthesis should remain sequential by default. Do not introduce parallel heavy synthesis without a separate design and backpressure tests.
- On Windows, `pygame.mixer.music` can lock the loaded audio file. Cancellation should stop/unload playback before deleting temporary files.

## Fallback Rules

- If streaming fails before any audio starts, fall back once to `_generar_dialogo() -> _hablar()`.
- If audio already started, do not replay the full legacy response.
- Never mark success if generation/synthesis/playback failed before valid output.
- Do not warm, switch, or prepare unnecessary resources just because streaming is enabled.

## Non-Goals

- Do not touch SmartAggregator raw-chat policy.
- Do not alter LiveVoice continuous or PTT pipelines except through existing inference entry points.
- Do not disturb Qwen offline/cache startup behavior.
- Do not test every possible LLM/TTS case; use stress-first tests where risk justifies it.
