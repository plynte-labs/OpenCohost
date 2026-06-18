"""i18n-core — the locale contract for OpenCohost.

The single seam every engine reads from for locale-driven resources (TTS voice,
LLM persona, guardrails, …). Adding a language = adding a bundle (data), never
engine code.

See conductor/tracks/english_compatibility_i18n_20260617/proposal.md.
"""
