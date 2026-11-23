# ADR-070: Dynamic Cloud Vision Capability Detection

**Status:** Proposed  
**Date:** 2026-11-22  
**Note:** Renumbered from ADR-069 on integration; `ADR-069` is owned by `ADR-069-interrupt-on-direct-arrival.md`.  
**Scope:** Cloud LLM direct vision routing (`opencohost/core/vision/`, `opencohost/api/routers/llm_provider.py`, `OpenCohost_UI/src/`)

## Summary

Replace static preset model allowlists as the sole source of truth for cloud vision support with a **live 1×1 PNG vision probe cached on the provider profile**, paired with a visible user override. This removes the need to hardcode new vision-language models (e.g. NVIDIA NIM `nvidia/nemotron-3.5-super-vl-120b-a12b`) every time a provider releases one.

---

## Problem

OpenCohost's cloud wire layer already supports OpenAI-compatible `image_url` content parts and provider-specific reasoning kwargs (including NVIDIA NIM `chat_template_kwargs`), yet newly released vision-capable chat models are rejected by default:

1. **Static allowlist bottleneck:** `profile_model_vision()` (`opencohost/core/vision/direct_cloud.py`) and `directVisionAvailability()` (`OpenCohost_UI/src/api/llmProvider.ts`) only return vision-ready when either:
   - The exact model ID appears in `LLM_PROVIDER_PRESETS[preset]["models"]` with `vision: true`, or
   - The user manually enables the `model_vision` checkbox, which is collapsed inside `<details>` (Advanced) in `OpenCohost_UI/src/features/controles/ProviderCard.tsx`.
2. **Provider dropdown exclusion:** `OpenCohost_UI/src/features/controles/VisionSourceSection.tsx` filters cloud vision providers by `p.models.length === 0 || p.models.some((m) => m.vision)`. Any preset whose curated list contains only text models (previously `nvidia_nim`) is hidden from the cloud vision dropdown completely.
3. **API metadata gap:** The standard OpenAI-compatible `GET /v1/models` endpoint (called by `opencohost/api/routers/llm_provider.py`) does not define a universal `vision` capability field across providers. Only a subset (such as OpenRouter's `architecture.input_modalities`) expose modality metadata.

---

## Solution Options & Tradeoffs

| Option | Mechanism | Pros | Cons |
| :--- | :--- | :--- | :--- |
| **A. Static Preset Allowlist** *(current)* | Maintain hardcoded `vision: true/false` lists in `settings.py` and `llmProvider.ts`. | Zero network latency; deterministic offline. | Breaks on every new model release; duplicates lists across Python and TypeScript; hides entire providers when no curated model has `vision: true`. |
| **B. Provider Metadata + Name Heuristic** | Read `input_modalities` when available (e.g. OpenRouter); fall back to model-ID regex (`-vl`, `vision`, `gpt-4o`, `gemini`, `claude-3`). | Instant; zero extra inference calls. | Heuristics misclassify models with non-obvious names (false positives cause runtime 400 errors; false negatives block valid VLMs). |
| **C. Live 1×1 PNG Vision Probe + Cache** *(recommended)* | On `Test Connection` (or model save / explicit "Detect vision" action), send a minimal 1×1 transparent PNG (`max_tokens: 1`) to `POST /v1/chat/completions` and cache the boolean result on the profile. | Works for **any** OpenAI-compatible provider and **any** future model with zero hardcoded lists; tests the real wire path. | Costs 1 minimal API call once per model change; must handle transient network/rate-limit errors distinctly from "unsupported modality". |

---

## Recommended Design (Option C + Visible Override)

### Resolution Precedence
1. **Explicit user override (`model_vision_override`)** — if the user manually toggles vision on/off in the UI, always respect it.
2. **Cached probe result (`model_vision` + `model_vision_probed_model`)** — valid while `profile.model == model_vision_probed_model`.
3. **Provider metadata (`input_modalities`)** — if `GET /v1/models` returns `"image"` in `architecture.input_modalities`, treat as `true` without needing a probe.
4. **Curated preset fallback (`LLM_PROVIDER_PRESETS`)** — fast default for well-known models before a probe has run.

### Probe Behavior
- **Payload:** Standard OpenAI `chat.completions` request with `max_tokens: 1`, temperature `0`, a 1×1 transparent PNG base64 `data:image/png;base64,...` `image_url` part, and a 1-word text prompt.
- **Outcome classification:**
  - `200 OK` → `supported` (`model_vision = true`).
  - `400` / `422` indicating unsupported image/multimodal input → `unsupported` (`model_vision = false`).
  - `401` / `403` / `429` / `5xx` / timeout → `indeterminate` (do not overwrite existing setting; surface a retryable status).
- **Cache invalidation:** Changing `profile.model` or `profile.base_url` clears the cached probe result so a newly selected model is re-evaluated.
- **Privacy & logging:** Reuse the fixed-literal logging discipline from `direct_cloud.py` (never log API keys, prompts, or payloads).

### UI Changes
- Expose the `model_vision` toggle visibly whenever a cloud model is configured (instead of burying it inside Advanced `<details>`), showing its state (`Verified via probe`, `From preset`, `Manual override`, or `Unknown — click to test`).
- Do not hide OpenAI-compatible presets from `VisionSourceSection.tsx` solely because their static sample list lacks a `vision: true` entry.

---

## Implementation Plan (for Main PC)

### Backend
- [ ] Add a vision probe helper in `opencohost/core/vision/cloud_probe.py` (1×1 PNG payload, strict status classification: `supported` / `unsupported` / `indeterminate`).
- [ ] Wire probe invocation into `POST /api/llm-provider/test` (optional `probe_vision: bool = True` flag) or add `POST /api/llm-provider/probe-vision` in `opencohost/api/routers/llm_provider.py`.
- [ ] Persist `model_vision_probed_model` alongside `model_vision` in provider profile storage and invalidate when `model` or `base_url` changes.
- [ ] Parse `architecture.input_modalities` in `GET /models` responses when present.
- [ ] Add unit tests covering `200`, `400/422` unsupported image, `429/5xx` indeterminate, and cache invalidation on model change.

### Frontend (`OpenCohost_UI/`)
- [ ] Surface `model_vision` status and manual override outside `<details>` in `OpenCohost_UI/src/features/controles/ProviderCard.tsx`.
- [ ] Update `OpenCohost_UI/src/features/controles/VisionSourceSection.tsx` so cloud presets are not excluded solely due to an empty/text-only static model sample list.
- [ ] Add Vitest coverage for probed, indeterminate, and overridden states.

---

## Out of Scope
- Local Ollama vision model discovery (`GET /api/vision/models` already uses Ollama metadata + `vision_by_name()`).
- Wayland screen-capture transport (`mss` vs. `xdg-desktop-portal` — tracked separately under Linux integration).
- Changing the OpenAI `image_url` request wire format in `direct_cloud.py` or `cloud_describer.py`.
