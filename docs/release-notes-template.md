# OpenCohost `<version>` release notes

> **Maintainer notes (delete this block before publishing)**
>
> - Piper voice license: resolved 2026-11-12. The voice card for `es_MX-claude-high` states Apache-2.0 (source: the HirCoir Piper-TTS-Spanish Space). It states no license for the underlying recordings, so that residual risk is the owner's. Re-check the card if the voice file changes. Recorded in THIRD-PARTY-NOTICES.md.
> - GPL scope: resolved 2026-11-12. The download is offered under GPL-3.0 section 6(d) (see the last section). Keep the source links available for as long as the installer is distributed. This is not legal advice; have a lawyer review the section before publishing.
> - Do not hand-write `SHA256SUMS`. Generate it from the final release assets.

## Breaking changes

**Selective data upgrade in 0.5.0: memories start fresh.** Your profiles, settings, voice/locale preferences, assets, and API keys from 0.3.0 and 0.5.0-alpha are preserved, while memories from earlier versions are not imported because the memory storage format changed.

To upgrade:

1. Run the new installer. It tries to remove the previous version itself.
2. If the installer reports that it cannot, uninstall the previous version from Windows Settings -> Apps first, then run the installer again.
3. Optional: the previous version may leave a `resources\runtime` folder inside its old install folder. You can delete it manually to reclaim space.

## Highlights

> DRAFT (owner review). Edit or cut before publishing.

- DRAFT (owner review): One installer for the whole app. The desktop app, the local engine, its Python runtime and the offline voices and models are bundled, so Kira can run without extra downloads.
- DRAFT (owner review): Interruptible, streamed replies with an inference watchdog and rollback, so a stalled model does not freeze the show.
- DRAFT (owner review): Stronger safety layer: a contextual crisis detector (laughter or joke markers never lower an explicit self-harm statement) and per-sentence guardrails on streamed replies.
- DRAFT (owner review): First-run setup wizard with a resumable checkpoint, and a selective upgrade from 0.3.0 that keeps your profiles, settings, assets and API keys while memories start fresh.
- DRAFT (owner review): Kira's 3D avatar (VRM, VRM Public License 1.0) with lip-sync. The pixiv VRoid animation pack is not distributed, so the avatar uses CC0 animation clips.
- DRAFT (owner review): Cloud provider templates (Gemini, DeepSeek, Anthropic, OpenRouter) with a key test, and on-demand screen vision.
- DRAFT (owner review): Topic-progression variants for agenda mode are on by default and can be turned off from a toggle in the Agenda panel.
- DRAFT (owner review): Piper voices are bundled; because Piper is GPL-3.0, the installer is offered under GPL-3.0 with the Corresponding Source below. A new-version notice is on by default and can be turned off.

## Install

1. Download `OpenCohost-<version>-x64-setup.exe` from this release's assets.
2. Check the file against `SHA256SUMS` (see the Checksums section).
3. Run the installer. TODO: confirm the requirements and first-run steps against `docs/QUICKSTART.md`.

## Windows SmartScreen

The installer is not code-signed in this release. Windows SmartScreen may show "Windows protected your PC". Before you run it, verify the file against `SHA256SUMS.txt` (see the Checksums section). If the hash matches, choose **More info** and then **Run anyway**.

## Known issues

- DRAFT (owner review): Windows is the only platform validated end to end.
- DRAFT (owner review): The installer is unsigned, so Windows SmartScreen warns on first run (see Windows SmartScreen above).
- DRAFT (owner review): OBS control is not included in the installer build. `obsws-python` is GPL-3.0-only and is the opt-in `obs` extra for source installs only.
- DRAFT (owner review): Recovery from a stall in the middle of a streamed reply has not yet faced a live adverse case, so treat streaming stability as unproven. The kill switch is `OPENCOHOST_LLM_STREAMING=0` plus a restart.
- DRAFT (owner review): Gemini (Gemini 3.5 Flash, on a clean VM install) and NVIDIA NIM were verified live. DeepSeek, Anthropic, OpenRouter and cloud vision were tested only with simulated calls.
- DRAFT (owner review): Still needing live validation: chat ingestion (not yet tested on a live stream), the clause sanitizer, a true cold model load, the push-to-talk bridge after a backend restart, and the Content Security Policy in the installed app.
- DRAFT (owner review): The crisis detector deliberately treats a statement framed as in-game (for example "en el juego", a checkpoint) as game talk and dampens it.
- DRAFT (owner review): Agenda topic progression (on by default) uses short fixed-length lines. Reasoning or heavily quantized models may produce short or empty agenda lines; turn progression off in the Agenda panel if that happens.

## Checksums (SHA256SUMS)

Every release asset has an entry in `SHA256SUMS`. To verify a download, compute its hash and compare it with the matching entry:

```powershell
Get-FileHash -Algorithm SHA256 .\OpenCohost-<version>-x64-setup.exe
```

TODO: paste the generated `SHA256SUMS` content here.

## Corresponding Source (GPL-3.0)

The Windows installer (`OpenCohost-<version>-x64-setup.exe`) distributes GPL-3.0 components (`piper-tts` and the espeak-ng it embeds) and is, as a combined work, offered under GPL-3.0. OpenCohost's own source stays MIT. Under GPL-3.0 section 6(d), the Corresponding Source is offered here, from the same place as the installer, at no charge:

| Component | Version | License | Corresponding Source |
|---|---|---|---|
| OpenCohost | `<version>` | MIT | Source at the release tag `v<version>`: https://github.com/plynte-labs/OpenCohost/tree/v<version> (also the release's "Source code" archives) |
| `piper-tts` | 1.4.2 | GPL-3.0-or-later | Repository tag `v1.4.2`, which includes the CMake build that fetches and statically links espeak-ng: https://github.com/OHF-Voice/piper1-gpl/tree/v1.4.2 . The PyPI sdist alone is NOT complete Corresponding Source: it omits the CMake files, `espeakbridge.c` and espeak-ng. |
| espeak-ng (embedded in `piper-tts`) | commit `212928b394a96e8fd2096616bfd54e17845c48f6` (untagged snapshot after 1.52.0, pinned by piper1-gpl `v1.4.2` `CMakeLists.txt`) | GPL-3.0-or-later | https://github.com/espeak-ng/espeak-ng/tree/212928b394a96e8fd2096616bfd54e17845c48f6 |

`obsws-python` (GPL-3.0-only) is not distributed in the installer; it is the opt-in `obs` extra for source installs.

Before publishing, mirror the piper1-gpl `v1.4.2` source archive and the espeak-ng `212928b` source archive as release assets, so the source stays available from the same place as long as the installer is offered (GPL-3.0 section 6(d)).

The versions match the pins in `uv.lock` and the table in THIRD-PARTY-NOTICES.md. Keep these links working for as long as this installer is distributed. If a link breaks, mirror the sdists as release assets.

**License text.** The GPL-3.0 text and the MPL-2.0 notice for `certifi` must accompany the installer. Open item: verify that the installed Python packages carry their license files (the `piper-tts` wheel ships `COPYING` in its dist-info) and that THIRD-PARTY-NOTICES.md is installed. The installer's own license page is the MIT `LICENSE`.

*Not legal advice. Review this section before publishing.*
