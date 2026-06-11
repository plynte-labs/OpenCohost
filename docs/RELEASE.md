# Release Process

This document describes how to cut a new OpenCohost release.

## Prerequisites

- Write access to `plynte-labs/opencohost` on GitHub.
- The CI workflow (`.github/workflows/ci.yml`) must be green on `master`.

## Steps

### 1. Bump the version

Edit `opencohost/__init__.py` and update `__version__`:

```python
__version__ = "X.Y.Z"
```

Commit the change to `master` (directly or via a PR):

```
git commit -m "chore(release): bump version to X.Y.Z"
```

### 2. Tag the release

Create an annotated tag that matches `__version__` exactly (with a `v` prefix):

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

Pushing the tag triggers `.github/workflows/release.yml`. The workflow:

1. Verifies the tag matches `opencohost/__version__` — fails fast if they differ.
2. Builds `opencohost-src-X.Y.Z.zip` (source tree: `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE`, and the `opencohost/` package).
3. Computes SHA256 of the zip and bakes it into `packaging/_release_meta.py`.
4. Builds `OpenCohost-Setup-X.Y.Z.exe` on a Windows runner via PyInstaller.
5. Creates a **draft** GitHub release with three assets attached:
   - `opencohost-src-X.Y.Z.zip`
   - `OpenCohost-Setup-X.Y.Z.exe`
   - `SHA256SUMS.txt`

### 3. Review and publish the draft

Open the draft release at `https://github.com/plynte-labs/opencohost/releases`.

- Verify the attached assets are present and the auto-generated release notes look correct.
- Edit the description if needed.
- Click **Publish release**.

Publishing makes the assets publicly downloadable. The launcher exe fetches
`opencohost-src-X.Y.Z.zip` from this URL on first run, verifies the SHA256,
and installs the package into a managed venv via `uv`.

## Version mismatch guard

The release workflow will exit with a non-zero status if the tag (e.g. `v0.2.0`)
does not match `opencohost.__version__` (e.g. `0.1.0`). Fix by bumping the
version in `opencohost/__init__.py` and re-tagging.

## Re-running a failed release

If the workflow fails partway:

1. Delete the tag locally and remotely:
   ```bash
   git tag -d vX.Y.Z
   git push origin :refs/tags/vX.Y.Z
   ```
2. Fix the issue, commit, and re-tag.

If a draft release was already created, delete it from the GitHub UI before
re-pushing the tag to avoid a name conflict.
