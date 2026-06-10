# TTS Markdown Emphasis Sanitizer

## Overview

Kira/LLMs often emit Markdown emphasis such as `*hola*` or `**importante**`. The current TTS cleanup removes the full emphasized span, causing spoken words to disappear. The fix should strip only Markdown emphasis markers while preserving the emphasized text.

## Functional Requirements

- Convert `*hola*` to `hola` before TTS chunking.
- Convert `**hola**` to `hola` before TTS chunking.
- Convert `***hola***` to `hola` before TTS chunking if encountered.
- Preserve mathematical and code-like expressions such as `5*10=50`, `a*b`, `2 ** 8`, and `precio * descuento`.
- Never delete emphasized content just because Markdown markers are present.

## Non-Functional Requirements

- Keep the sanitizer conservative and fast.
- Use a fast path when no `*` exists.
- Use a bounded regex pass, not loops that add noticeable latency.
- Do not implement a full Markdown parser.

## Acceptance Criteria

- Tests fail before implementation for emphasis preservation.
- Tests cover math/expression preservation.
- Targeted LLM/TTS tests pass after implementation.
