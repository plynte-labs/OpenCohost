# Third-Party Notices

OpenCohost itself is MIT-licensed (see [LICENSE](LICENSE)). This file records the
licenses of the dependencies it relies on, because a few of them are **not** MIT
and that matters if you plan to redistribute.

No third-party source is vendored into this repository. Everything below is
fetched from PyPI, npm, or crates.io at install time, onto the machine that runs
it. The distinction matters: this repository distributes no copyleft code, but a
**built and bundled** copy of OpenCohost does.

Versions are the ones resolved in this project's lockfiles as of 2026-08-14, and
each license was read from the installed package's own metadata.

---

## Read this first if you intend to redistribute or sell

The MIT license on OpenCohost lets you fork it, modify it, and charge money for
it. That permission covers **this repository's code**. It does not, and cannot,
relicense the dependencies.

Two of them are GPL:

| Package | License | Extra | Linkage |
|---|---|---|---|
| `piper-tts` 1.4.2 | **GPL-3.0-or-later** | `local-tts` | imported in-process (`opencohost/core/speech/backends/tts_piper.py`); statically embeds espeak-ng (GPL-3.0-or-later) in its compiled `espeakbridge` module |
| `obsws-python` 1.8.0 | **GPL-3.0-only** | `obs` | imported in-process (`opencohost/avatar/obs_client.py`) |

Only `piper-tts` **ships** in the Windows installer (`OpenCohost-<version>-x64-setup.exe`). The
installer's Python runtime is staged by `packaging/stage_installer_resources.py`
from a locked export of `SUPPORTED_EXTRAS` (`local-tts`, `cloud-tts`,
`integrations`, `vision`), and that export includes `piper-tts`. The staged export
includes `local-tts` by default, so a standard install runs GPL-3.0 code in the
same process as the MIT application.

`obsws-python` is **not** in the installer (owner decision 2026-11-16). It lives in
the separate opt-in `obs` extra, which `SUPPORTED_EXTRAS` deliberately excludes,
because a GPL-3.0-only component would make the whole installer GPL-3.0-only.
Installer users get OBS control only by installing `obsws-python` themselves; an
MIT obs-websocket client is planned to replace it.

**Owner decision.** The distributed installer, as a combined work, is subject to
GPL-3.0 for `piper-tts` and its embedded espeak-ng. OpenCohost's own source remains
MIT.

Because the installer distributes `piper-tts`, the GPL-3.0 source obligation
applies to it. `obsws-python` is listed for source installs that add the `obs`
extra. The source is identified below by upstream URL and
the versions pinned in `uv.lock`:

| Package | Version | Upstream source | License |
|---|---|---|---|
| `piper-tts` | 1.4.2 | https://github.com/OHF-voice/piper1-gpl (tag `v1.4.2`) | GPL-3.0-or-later |
| espeak-ng (embedded in `piper-tts`) | commit `212928b394a96e8fd2096616bfd54e17845c48f6` (untagged, after 1.52.0) | https://github.com/espeak-ng/espeak-ng/tree/212928b394a96e8fd2096616bfd54e17845c48f6 | GPL-3.0-or-later |
| `obsws-python` | 1.8.0 | https://github.com/aatikturk/obsws-python | GPL-3.0-only |

Running OpenCohost from source with these extras installed is the ordinary
optional-dependency posture. The GPL terms bind the installer as distributed.

`OpenCohost-<version>-x64-setup.exe` also ships `certifi` (MPL-2.0, CA bundle). `certifi` is not
named in `packaging/stage_installer_resources.py` or `.github/workflows/release.yml`.
It arrives as a transitive dependency of `httpx`, `httpcore`, `requests` and
`edge-tts` in `uv.lock`, and `stage_installer_resources.py` stages that locked
dependency set. This is genuine redistribution of an MPL-2.0 file, so carry
MPL-2.0's notice with the installer.

---

## Python — core dependencies

Installed for every user, not behind an extra.

| Package | Version | License |
|---|---|---|
| `pygame` | 2.6.1 | LGPL |
| `pynput` | 1.8.2 | LGPLv3 |
| `certifi` | 2026.5.20 | MPL-2.0 |

The LGPL packages are installed unmodified from PyPI, imported dynamically, and
replaceable by the user, so the relinking freedom LGPL exists to protect is
already intact. What is owed is this notice.

## Python — optional extras

| Package | Version | License | Extra |
|---|---|---|---|
| `edge-tts` | 7.2.8 | LGPLv3 | `cloud-tts` |
| `piper-tts` | 1.4.2 | **GPL-3.0-or-later** | `local-tts` |
| `obsws-python` | 1.8.0 | **GPL-3.0-only** | `obs` (not in the installer) |

Everything else in `pyproject.toml` resolves to MIT, BSD, Apache-2.0, or PSF.

## Rust — via Tauri

These transitive crates are MPL-2.0 (file-level weak copyleft, unmodified, so
notice is the obligation):

`cssparser` 0.36.0, `cssparser-macros` 0.6.1, `selectors` 0.36.1,
`dtoa-short` 0.3.5, `option-ext` 0.2.0

The rest of the Rust dependency graph is MIT, Apache-2.0, BSD, or dual
MIT/Apache-2.0. No GPL, LGPL, or AGPL crate appears in `Cargo.lock`.

## JavaScript — front end

All runtime dependencies of `OpenCohost_UI` resolve to MIT, ISC, Apache-2.0,
BSD-2-Clause, or BSD-3-Clause. No copyleft. `autoprefixer`, `browserslist`, and
`caniuse-lite` carry CC-BY-4.0 data but are build-time only and emit nothing
into the bundle.

---

## Bundled offline assets & models (installer)

Since version 0.3.0-alpha.1, the packaged Windows installer (`OpenCohost-<version>-x64-setup.exe`)
bundles the following models and tools for offline-first readiness:

| Asset | Source / Model | License | Notes |
|---|---|---|---|
| Laya Multilingual ONNX | ConvAI Innovations / NandhaKishorM (`convaiinnovations/laya`, `multilingual` checkpoint; https://huggingface.co/convaiinnovations/laya, https://github.com/NandhaKishorM/laya) | **Apache-2.0** | Converted to ONNX from upstream checkpoint revision `7b928d828b7b0e022f929d9bd2e44165aa270148` using the export in `scripts/export_laya_onnx.py` (upstream `scripts/export_onnx.py`, commit `1adc59f7`). The files are modified (format-converted) from the originals; the Apache-2.0 license and notice are retained. For local turn decision bus & routing. Checked 2026-11-12. |
| mmBERT-base | Johns Hopkins University CLSP (`jhu-clsp/mmBERT-base`; https://huggingface.co/jhu-clsp/mmBERT-base) | **MIT** | Base model of the Laya `multilingual` checkpoint; its MIT copyright and permission notice must accompany the converted weights. Checked 2026-11-12. |
| Paraphrase-Multilingual-MiniLM-L12-v2 ONNX | Xenova / sentence-transformers (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`) | **Apache-2.0** | Permissive; compatible with MIT binary redistribution. Powers Memory v5 local semantic search. |
| Piper Voice | `es_MX-claude-high.onnx` (Rhasspy Piper Voices; voice card says the voice comes from https://huggingface.co/spaces/HirCoir/Piper-TTS-Spanish; card at https://huggingface.co/rhasspy/piper-voices/tree/main/es/es_MX/claude/high) | **Apache-2.0 per the voice card (fetched 2026-11-12)** | The card lists one speaker and states no license or provenance for the underlying recordings or dataset; the repository-level tag (MIT) is superseded by the card. "claude" is a speaker label. Powers Kira's default offline local voice. |
| Piper Voice (English) | `en_US-kristin-medium.onnx` (Rhasspy Piper Voices; https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/kristin/medium; training details linked from the card at brycebeattie.com/files/tts/) | **Public domain per the voice card (fetched 2026-11-12)** | Single female US English speaker, medium quality, 22,050 Hz. The card states the voice was trained from scratch on about 11.5 hours of recordings assembled by its creator, all taken from LibriVox.org, which lists the dataset as public domain. Shipped as `en_US-kristin-medium.onnx` plus its `.onnx.json`. |
| Microsoft Visual C++ Redistributable (MSVC CRT) runtime DLLs | Microsoft Corporation (`REDIST.TXT`) | **Microsoft Visual Studio license terms (redistributable files)** | Ten x64 C/C++ runtime DLLs, redistributed unmodified from the Visual Studio `VC\Redist` tree (not debug, not preview; https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution), staged beside the standalone Python runtime (app-local deployment). Release builds verify the Microsoft signature and a file version >= 14.50.35719.0 and record the exact SHA-256 of every file in `msvc-crt-manifest.json`. |
| CPython 3.12.4 runtime | Python Software Foundation (staged by `packaging/stage_installer_resources.py` via `uv python install 3.12.4`, directory `cpython-3.12.4-windows-x86_64-none`) | **PSF License Agreement (Python-2.0)** | Standalone Python runtime that runs the engine inside the installer. Its `LICENSE.txt` and `COPYING` stay in the staged runtime folder. |
| Microsoft Edge WebView2 Runtime | Microsoft Corporation | **Microsoft WebView2 Runtime license terms** | Required by the Tauri desktop shell. The installer embeds Microsoft's WebView2 bootstrapper (`webviewInstallMode: embedBootstrapper` in `tauri.conf.json`), which installs the runtime only if the PC lacks it; the runtime itself is not redistributed in this repository. |
| uv binary | Astral Software Inc. (`astral-sh/uv` 0.11.6) | **MIT / Apache-2.0** | Dual-licensed tool binary used by the installer to bootstrap the local Python venv. |

---

## Not covered by this file

**Conversational LLMs.** OpenCohost distributes no conversational LLM weights (e.g. Llama, Gemma, Qwen). Ollama downloads
them on your machine under their own terms, and community licenses carry acceptable-use and redistribution conditions that MIT does not grant you.

**Additional Piper voices.** Additional or optional voices beyond the bundled `es_MX-claude-high` carry per-voice licenses that vary from MIT to CC-BY to dataset-restricted. Check the voice you ship or configure.

**Kira VRM avatar: first-party.** The model
`OpenCohost_UI/src-tauri/resources/vrm/Opencohost Kira v3.vrm` was created by the
project owner, FranGuh, in VRoid Studio. It is first-party work, not third-party
material. **It is NOT covered by the project's MIT license.** It is licensed under the
**VRM Public License 1.0**, https://vrm.dev/licenses/1.0/, with the permissions embedded
in the file's VRM metadata (owner decision 2026-11-16): avatar use by everyone;
commercial use by individuals and corporations; redistribution allowed; attribution
required (credit "Kira by FranGuh"); no use in excessively violent, excessively
sexual, political or religious, or anti-social or hateful content. Altered versions may be redistributed. The
file is tracked in the UI repository and bundled as a Tauri resource, so it ships
inside the installer.

**Kira VRM animations.** The `.vrma` clips under `OpenCohost_UI/public/animations/`
carry their own licences and provenance. The per-file terms are recorded in
`OpenCohost_UI/public/animations/THIRD_PARTY_LICENSES.md` (UI repository); that
file, not this one, is the statement of record for them.

**Kira artwork: AI-generated.** The Kira images under `assets/avatar/kira/` and the
`kira-*` images under `OpenCohost_UI/public/` were generated with ChatGPT (OpenAI)
for this project. They are not MIT and not covered by the VRM's license:
they are distributed only as part of OpenCohost, with no reuse outside it without the
owner's permission.

The project's MIT license covers the source code. To use your own character, load your
own VRM; its terms are yours.

**VRM metadata.** The model was re-exported from VRoid Studio 2.16.0 on 2026-11-16.
Its embedded VRM 1.0 meta matches this section: `licenseUrl` https://vrm.dev/licenses/1.0/,
copyright FranGuh, `avatarPermission: everyone`, `commercialUsage: corporation`,
`allowRedistribution: true`, `modification: allowModificationRedistribution`,
`creditNotation: required`, and the four content-usage flags set to false.

---

If you find an error or an omission in this file, please open an issue. It is
maintained by hand and is a good-faith summary, not a legal opinion.
