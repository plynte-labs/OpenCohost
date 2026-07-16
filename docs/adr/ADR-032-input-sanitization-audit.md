# ADR-032 — Input Sanitization Audit (Frontend + Backend)

**Date:** 2026-07-15
**Status:** Accepted (audit executed, one hardening applied, one release decision open)
**Driver:** Owner request during the UI polish session: "falta ver o auditar si los inputs están sanitizados."

## Objective

Verify, with code evidence, that every user-controlled or LLM-controlled string
entering the OpenCohost stack is neutralized before it can (a) execute in the
operator's webview (XSS), (b) reach a code-execution or filesystem sink in the
backend, or (c) trigger unintended network activity from the operator's machine.
The audit became urgent after this session introduced a markdown renderer for
Kira's replies — the first component that intentionally converts LLM output
into rich DOM.

## Where we searched

Read-only audit (no builds, no edits) across both repos:

| Surface | Files inspected |
|---|---|
| Markdown render path | `OpenCohost_UI/src/components/ui/Markdown.tsx`; installed `react-markdown@10.1.0` source (`lib/index.js` — `defaultUrlTransform`, raw-node handling) |
| DOM injection sinks | grep of all `OpenCohost_UI/src/` for `dangerouslySetInnerHTML` / `eval` / `document.write` / dynamic `href=` / `window.open` |
| Operator inputs | chat composer (`api/chat.ts` → `POST /api/chat/turn`), profile forms (`AgendaPanel`, `ProfileEditor`), command palette (mock) |
| Backend validation | `opencohost/api/main.py` (`_validate_chat_text`, `_PROFILE_NAME_MAX_LENGTH`, `_PROFILE_PROMPT_MAX_LENGTH`, music import), `opencohost/core/profiles.py` (persistence), `opencohost/api/auth.py` |
| Transcript path | `opencohost/api/ptt_session.py` (WhisperLive → PTT buffer → `process_context`), `api/models.py` (event whitelist) |
| Client storage | every `localStorage` read-back (`useTheme`, `useAlertStyle`, `useDensity`, volume, `oc-welcome`, `oc-collapse-*`) |

## What we found

**No P0 issues.** Justification per hypothesis:

1. **Hypothesis: LLM replies could inject markup.** Refuted. `react-markdown`
   is used without `rehype-raw`; raw HTML nodes degrade to escaped text
   (verified in the installed package source and pinned by a regression test in
   `Markdown.test.tsx`). React JSX interpolation escapes every other text
   render path.
2. **Hypothesis: `javascript:`/`data:` URLs in links.** Refuted.
   `defaultUrlTransform` allowlists `http/https/ircs/irc/mailto/xmpp` only;
   links also carry `rel="noreferrer"` (implies `noopener`).
3. **Hypothesis: remote-image beaconing from replies.** CONFIRMED (P2).
   `![x](https://host/p.png)` rendered a live `<img>` → outbound request
   (tracking-pixel pattern). **Hardening applied same day:**
   `disallowedElements={["img"]}` in `Markdown.tsx` + regression test. The
   image node is dropped entirely.
4. **Hypothesis: profile name path traversal.** Refuted. Profile names are JSON
   dict keys in `profiles.json`, never filesystem paths; writes are atomic
   (`mkstemp` + `os.replace`). Music import is the only user-driven file
   operation and is allowlist+size+`is_relative_to` guarded.
5. **Hypothesis: user strings reach exec sinks.** Refuted. No
   `eval`/`exec`/`subprocess`/`os.system` with user data anywhere in the
   backend; length caps (chat ≤4000, name ≤100, prompt ≤20000) enforced
   server-side before dispatch/persistence.
6. **Hypothesis: transcript or event feed bypasses React escaping.** Refuted.
   PTT transcript stays RAM → `process_context` (same dispatch as chat; HTTP
   bodies expose character counts only). The event feed is a closed whitelist
   with `detail` always null.
7. **Hypothesis: localStorage values reach a JS sink.** Refuted. Every
   read-back is enum/type-constrained before being written to `dataset.*`.

**P1 (open, release decision, not a code bug):** mutating `/api/*` endpoints
accept unauthenticated requests while `OPENCOHOST_API_AUTH` is off (the
default), and CORS admits browser dev origins. Acceptable for today's
loopback single-operator topology; **must be revisited before any non-local
exposure** (enforce the operator token, or bind strictly to 127.0.0.1 and drop
browser-origin CORS). Recorded as an open debt, not fixed here.

## Decision

- Keep `react-markdown` + `remark-gfm` (no `rehype-raw`, images disallowed) as
  the only sanctioned path from LLM output to DOM. Any future plugin addition
  to this component requires re-running item 1–3 of this audit.
- The "no raw HTML" and "no images" guarantees are pinned by regression tests
  so a dependency bump cannot silently reopen them.
- Auth enforcement remains OFF by default until the owner's release decision;
  the closeout doc and Engram carry the debt.

## Consequences

The stack is safe to keep developing on. The one structural risk left is
topological (loopback assumption), not code-level — it is owned by the release
checklist, not by any component.
