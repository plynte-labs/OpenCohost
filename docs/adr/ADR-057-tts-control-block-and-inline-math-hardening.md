# ADR-057: Harden TTS Control-Block Removal and Inline-Math Classification

**Date**: 2026-09-07  
**Status**: Accepted for the four confirmed findings; functional verification complete, native adversarial review pending  
**Scope**: `opencohost/core/speech/tts_sanitizer.py` and its focused regression tests

---

## Decision summary

OpenCohost now removes recognized TTS control blocks with a bounded, stack-balanced scanner,
fails closed to end-of-input for malformed or unclosed blocks, and rechecks for reconstructed
control tags after every relevant text transformation. Inline dollar spans are classified as
LaTeX only when they contain explicit TeX syntax or a compact subscript expression such as
`x_2`.

This closes the four confirmed audit findings without changing the existing one-to-three-digit
numbered-list policy. It is speech filtering for a single sanitizer call, not a browser HTML
sanitizer and not proof about control fragments split across streaming calls.

## Context and causes

The prior implementation had three structural weaknesses:

1. Cross-tag regular expressions accepted a closing tag from the wrong family and did not track
   nesting depth. A mismatched or partially closed private block could therefore expose its
   trailing text.
2. Presentation-tag removal, non-Latin filtering, and Markdown emphasis cleanup ran after some
   control checks. Those transformations could reconstruct a recognized tag after the last
   effective guard.
3. Any underscore inside a dollar-delimited span was treated as LaTeX. Ordinary monetary prose
   containing a snake_case plan name could therefore be replaced by the localized formula phrase.

Unsafe HTML-like speech controls shared the first two causes with protocol controls. These
controls prevent unintended speech; no claim is made about XSS or browser rendering safety.

## Decisions

### D1: Use one stack-balanced control-block scanner

Protocol controls (`think`, `analysis`, `reasoning`, `tool_call`, `tool_response`) and unsafe
HTML-like controls (`script`, `style`, `svg`, `template`) use the same bounded scanner. Matching
open/close tags remove the complete block, including nested blocks. A mismatched closer or an
unclosed recognized opener drops the remainder of the input.

Recognized opening variants such as `<script/x>` remain controls and are paired with the
corresponding closing tag.

### D2: Revalidate after transformations that can reconstruct controls

Control-block removal runs after presentation-tag removal, after non-Latin filtering, and once
more after emphasis and residual-asterisk cleanup. The last check is deliberately the final
text-producing stage. This covers inputs such as `<**think**>SECRET`, whose first cleanup step
reconstructs `<think>SECRET`.

### D3: Keep inline-math recognition conservative

A dollar span is transformed only when its body contains explicit TeX indicators such as a
backslash, caret, or braces, or a compact subscript form such as `x_2`. Snake_case inside prose
is insufficient evidence of LaTeX, so prices remain unchanged.

### D4: Preserve the numbered-list policy

The existing rule intentionally strips a leading one-to-three-digit numbered-list marker.
Consequently, `- 99. Fue un buen ano.` remains `Fue un buen ano.`, while a four-digit year such
as `1999` is preserved. The audit identified ambiguity, not a confirmed regression, so changing
this policy requires a separate product decision.

## Audit disposition

| Audit class | Before | After | Disposition |
|---|---|---|---|
| Mismatched or nested protocol blocks | `<think>A</analysis>SECRET` and an unclosed nested `<think>` exposed `SECRET` | Both return `""`; correctly closed nested controls preserve following `Public.` | Closed |
| Obfuscated protocol delimiters | Zero-width or removable presentation markup could reconstruct `<think>` after the protocol pass | `<th\u200bink>SECRET`, `<thi<b></b>nk>SECRET`, `<**think**>SECRET`, and `<*think*>SECRET` return `""` in one call | Closed |
| Unsafe HTML-like control blocks | A cross-family closer could expose `SECRET`; recognized opening variants were inconsistently bounded | `<script>A</style>SECRET` returns `""`; `<script/x>SECRET</script>Public.` returns `Public.`; emphasis-obfuscated unsafe tags return `""` | Closed |
| Dollar spans containing snake_case prose | `Cuesta $5 en plan_basico y $10 en premium.` became `Cuesta una fórmula10 en premium.` | Input is returned unchanged; `$x_2$` remains recognized inline math | Closed |
| One-to-three-digit numbered-list ambiguity | `- 99. Fue un buen ano.` loses the list marker and leading number | Behavior intentionally unchanged; `- 1999. ...` continues to preserve `1999` | Deferred policy decision, not a confirmed regression |

## Alternatives and tradeoffs

| Alternative | Decision and tradeoff |
|---|---|
| Extend the cross-tag regular expressions | Rejected. Regex matching does not represent nesting or same-family closure reliably. |
| Repeatedly sanitize until reaching a fixed point | Rejected. It makes transformation count implicit and can broaden destructive behavior. Explicit checks at reconstruction boundaries are easier to reason about and test. |
| Use a general HTML parser | Rejected for this bounded speech-control vocabulary. It adds broader HTML semantics and dependency surface without solving protocol-token policy by itself. |
| Treat every underscore in `$...$` as LaTeX | Rejected because it corrupts ordinary price prose. Conservative recognition may leave some unusual formulas spoken literally; preserving normal speech is the safer default. |
| Change the short-number list rule now | Deferred. It is an established policy with ambiguous examples and is independent of the four confirmed failures. |

## Verification evidence

### Strict-TDD implementation evidence reported by Terra

Terra reported two independently observed RED stages before the corresponding production fixes:

- Original four audit classes: `14 failed, 62 passed` in
  `tests/test_tts_markdown_normalizer.py`.
- Final post-emphasis reconstruction pins: `18 failed, 76 passed` before moving the final guard.

After the bounded corrections and source normalization, Terra reported:

- `120 passed` for `tests/test_tts_markdown_normalizer.py` plus
  `tests/test_tts_nonlatin_filter.py` in 56.18 seconds.
- `3 passed` for the existing precise Markdown emphasis and math/asterisk tests in
  `tests/test_llm_engine_timeouts.py`.

These RED results are attributed implementation evidence, not results reproduced by the ADR
author after the fixes.

### Independent final functional verification

The final on-disk bytes were independently verified with Python 3.13.13 and pytest 9.0.3:

```powershell
python -m pytest tests/test_tts_markdown_normalizer.py tests/test_tts_nonlatin_filter.py `
  tests/test_clause_sanitizer_chat_default.py tests/test_clause_sanitizer_e2e.py `
  tests/test_clause_sanitizer_seam.py tests/test_clause_sanitizer.py `
  tests/test_llm_engine_timeouts.py::test_tts_sanitizer_preserves_markdown_emphasis_content `
  tests/test_llm_engine_timeouts.py::test_tts_sanitizer_keeps_math_and_code_like_asterisks `
  tests/test_llm_engine_timeouts.py::test_tts_sanitizer_fast_path_without_asterisks_returns_same_text -q
```

Result: **190 passed in 146.35 seconds**. This comprises 120 focused TTS tests, 67 original
clause-sanitizer tests, and 3 precise LLM math/asterisk regression tests.

Additional direct property probes passed for all nine recognized control tags under `*tag*`
and `**tag**`, the matched-block-removal concatenation case, closed nested controls preserving
`Public.`, monetary prose identity, and both short-number/four-digit controls.

Check-only validation:

```powershell
python -m ruff check opencohost/core/speech/tts_sanitizer.py tests/test_tts_markdown_normalizer.py
git diff --check -- opencohost/core/speech/tts_sanitizer.py tests/test_tts_markdown_normalizer.py
```

Both commands exited 0. No TTS engine, model startup, audio playback, network request, or model
download was used.

## Scope and limitations

- This decision closes only the four confirmed single-call TTS sanitizer findings above.
- The four `test_clause_sanitizer*.py` files exercise `repetition_guard`; they are regression
  evidence, not direct proof of TTS control filtering.
- Control text split across separate streaming sanitizer calls was not proven reachable through
  a consumer and remains out of scope.
- The short-number/list ambiguity remains intentionally unchanged.
- Native adversarial review has not started for this candidate. This ADR claims functional
  verification only and does not claim a native receipt, gate allow, or review approval.

## Rollback

Rollback is confined to these files:

1. `opencohost/core/speech/tts_sanitizer.py`
2. `tests/test_tts_markdown_normalizer.py`
3. `docs/adr/ADR-057-tts-control-block-and-inline-math-hardening.md`

Reverting the source without its regression tests would erase the proof boundary; revert source,
tests, and this ADR together. No unrelated dirty files are part of this decision.
