# Runtime validation — 2026-07-25 batch

What shipped today that **tests cannot prove**. Each item below failed in real use before, or is a
behavior change no assertion can confirm. Ordered by what breaks worst if it is wrong.

Automated coverage already passed: 320 Python + 143 UI focused tests for Lote 1, plus the Lote 2
suites. That proves the code does what it was written to do — not that it does what you want.

---

## A. Feed ordering — the bug you reproduced

**Why it needs you:** the fix depends on real clocks, a real backgrounded window, and real poll
throttling under WebView2. jsdom has none of those.

1. Start a stream session, put the app **behind OBS or a fullscreen game**, and do not click it.
2. Hold F10, ask something that produces a long reply.
3. Bring the window back.

**Expect:** your `Vos · voz` bubble sits **above** Kira's reply. Every row shows an `HH:MM` clock.

**This is the case that used to invert.** If it still inverts, capture the two timestamps shown on
the rows — that tells me immediately whether the reply took the server stamp or fell back.

4. Now do **two holds back to back**, quickly, still backgrounded. Second question, second answer.

**Expect:** strict chronological order across all four rows. This is the per-hold stamp — a second
hold can no longer borrow the first hold's reading.

5. While Kira is thinking (long reply pending), let an agenda or model event land.

**Expect:** the feed still follows down if you are at the bottom, or the "Ver lo más reciente" pill
appears if you are scrolled up. Before, both were frozen for the entire thinking window.

6. Scroll up mid-session and let a reply arrive. **Expect:** you are NOT yanked to the bottom.

---

## B. Voice turns keep their context — the regression the judges caught

**Why it needs you:** this one was introduced and fixed within the same batch. It is the highest-risk
item here because it is invisible when broken — Kira just answers slightly worse.

1. **Arm an editorial card** on a topic (e.g. a game delay).
2. Ask about that topic **by voice** (F10), not typed.

**Expect:** she uses the card's facts. Before the fix she would have answered with none of them —
the card would have been silently ignored on the voice path only.

3. Talk long enough for history to roll over, then ask **by voice**: *"¿de qué estábamos hablando
hace un rato?"*

**Expect:** she recalls. Then ask the **same question typed** and confirm both paths behave the same.
The bug was that only the typed path worked.

4. Note whether an armed **single-use** card is consumed by the voice question. It should be —
symmetric with typing — but this is a recorded consequence pending your sign-off, not a decision.

---

## C. Provenance — the "viewer before" hallucination

1. With **no chat platform connected at all**, type several turns and let her refer back to them.

**Expect:** no invented viewer, no "el espectador de antes". Typed turns now carry a speaker frame in
history, as PTT already did.

2. Mixed session: alternate typed and voice turns on the same topic.

**Expect:** she does not confuse who said what.

**Open question you have not answered yet:** whether the *live* payload of a typed turn should also
carry the frame (PTT frames both). Right now only history is framed, so the turn being answered is
the one unattributed line in the prompt. Details in
`conductor/tracks/lote1_open_questions_20260725/proposal.md` §A4. **This one needs your ear, not a
test** — framing it changes generation input on every typed turn and can shift her register.

---

## D. Memory hygiene

1. Say only *"como vas?"* / *"buenas, todo bien?"*. **Expect:** no memoria captured.
2. Say *"como vas con el parche de bloodborne"*. **Expect:** captured — it has real content.
3. Type a turn with a fact in the **second half** of the sentence, e.g.
   *"el parche 1.09 de bloodborne rompió el framerate en el bosque"*. Later ask her about it.

**Expect:** she recalls the **framerate** part. The speaker frame used to eat 3 of the 8 words the
digest keeps, so the tail — the actual fact — was dropped.

4. Open the memoria panel. **Expect:** auto-captured rows carry the `borrador` badge; ones you edited
or pinned do not.

---

## E. Memory promotion (Lote 2)

**Status: verify this section against the final commit — it is the newest work.**

1. Accumulate drafts across a session, then **restart the app**. Promotion runs at startup.
2. Check the logs for the sweep's counts line (considered / promoted / rejected, with reasons).

**Expect:** counts appear. **If you see nothing at all, tell me** — a silent no-op on your cloud
provider was a real defect this batch, and the absence of that line is exactly its signature.

3. Confirm a vague draft (*"algo técnico, no me acuerdo del detalle"*) is rejected, and a specific
one is promoted and rewritten to be self-contained.
4. **Delete the "Luke Oxide" memoria manually** — it predates this work and promotion will not remove
an already-stored bad row on its own.
5. Edit a memoria by hand right after a restart, while the sweep may still be running.
   **Expect:** your edit survives. The judge must never overwrite an operator edit.
6. Mute a draft in the panel, restart, and confirm it **stays muted**.

**Your approved decision to be aware of:** the judge sends the draft batch to your active cloud
provider. Muted and private rows are excluded.

---

## F. Provider behavior

1. Run a session on **cloud** (GLM-5.2) and confirm the header/status shows the cloud model, not the
   local one. That mismatch was a shipped bug.
2. Switch provider mid-session and confirm nothing wedges.
3. Run the same checks in A–D on **local** (Ollama) and note any difference. The engine should be
   provider-agnostic; if something only works on one, that is the bug class that bit twice today.

---

## Still not exercised — the standing release gate

`heavy_model_inference_recovery` has never been validated against a real heavy or stalling model.
It is unrelated to this batch and remains the outstanding gate from `AGENT_HANDOFF.md`.

---

## If something is wrong

Give me the **symptom and the timestamps on the rows**, not a diagnosis. Twice today the reported
symptom pointed at the wrong subsystem — the transcript echo turned out to live in a third repo, and
"she doesn't remember" turned out to be a storage-quality problem, not a recall problem.
