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
| `piper-tts` 1.4.2 | **GPL-3.0-or-later** | `local-tts` | imported in-process (`opencohost/core/speech/backends/tts_piper.py`) |
| `obsws-python` 1.8.0 | **GPL-3.0-only** | `integrations` | imported in-process (`opencohost/avatar/obs_client.py`) |

Both are optional extras, and neither ships in this repository. But note that
`packaging/launcher.py` installs `local-tts` **by default** (`WHEEL_EXTRAS`), so
the standard installer produces a machine where an MIT application imports
GPL-3.0 code in the same process.

For running OpenCohost yourself, or for distributing it as source, this is the
ordinary optional-dependency posture. If you intend to ship a **bundled binary**
that includes either package, take advice — a combined work linking GPL-3.0
in-process is generally expected to be GPL-3.0 as a whole. Your options are to
exclude those extras from the bundle, or to accept the GPL terms for your build.

`OpenCohost-Setup.exe` also bundles `certifi`'s CA bundle (see
`.github/workflows/release.yml`), which is genuine redistribution of an MPL-2.0
file. If you build your own installer, carry MPL-2.0's notice with it.

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
| `obsws-python` | 1.8.0 | **GPL-3.0-only** | `integrations` |

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

## Not covered by this file

**Language models.** OpenCohost distributes no model weights. Ollama downloads
them on your machine under their own terms, and community licenses such as
Llama's and Gemma's are not OSI-approved and carry acceptable-use and
redistribution conditions that MIT does not grant you. If you redistribute a
product with a model bundled or preselected, read that model's license.

**Piper voices.** The default voices named in `opencohost/config/settings.py`
are not shipped here. Piper voices carry per-voice licenses that vary from MIT
to CC-BY to dataset-restricted. Check the voice you ship.

**Kira's artwork.** The images under `assets/avatar/kira/` and
`OpenCohost_UI/public/` have no attribution recorded in this repository. Their
provenance is not established here, so do not assume MIT covers them. This is
noted as an open item rather than a permission.

---

If you find an error or an omission in this file, please open an issue. It is
maintained by hand and is a good-faith summary, not a legal opinion.
