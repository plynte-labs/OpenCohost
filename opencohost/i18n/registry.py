"""i18n-core — bundle registry: discover, load, chain, validate locale bundles.

A bundle on disk is a directory ``<code>/`` containing ``manifest.yaml``.
Official bundles ship in the package (``opencohost/locales/``); community
bundles are drop-in mods under the user data dir (``USER_DATA_DIR/locales/``) —
unofficial, no guarantee, at the user's own risk.

Security property: the **source directory is authoritative for tier**, not the
manifest. A community mod cannot claim ``tier: official`` to dodge the warning;
the registry stamps the tier from where the bundle was found.

See the contract in :mod:`opencohost.i18n.contract` and the track proposal at
conductor/tracks/english_compatibility_i18n_20260617/proposal.md.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - registry degrades without PyYAML
    yaml = None

from opencohost.i18n.contract import (
    SCHEMA_VERSION,
    TIER_COMMUNITY,
    TIER_OFFICIAL,
    TIERS,
    LocaleBundle,
)

MANIFEST_NAME = "manifest.yaml"

# Slots required per tier for a bundle to be COMPLETE (checked in strict mode).
# Security is the differentiator: an official locale MUST author its own
# guardrails; a community mod may omit them and run degraded (user's own risk).
REQUIRED_SLOTS_BY_TIER = {
    TIER_OFFICIAL: ("guardrails",),
    TIER_COMMUNITY: (),
}


class BundleLoadError(Exception):
    """A bundle could not be loaded (missing/malformed manifest, bad chain)."""


@dataclass(frozen=True)
class BundleValidation:
    code: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def official_locales_dir() -> Path:
    """Directory of in-tree, project-authored bundles (es, en)."""
    return Path(__file__).resolve().parent.parent / "locales"


def community_locales_dir() -> Path:
    """Directory of user-installed community mods (unofficial)."""
    from opencohost.config.storage import USER_DATA_DIR

    return Path(USER_DATA_DIR) / "locales"


def load_bundle(locale_dir: Path | str, tier: str) -> LocaleBundle:
    """Load one bundle from ``locale_dir``, stamping the authoritative ``tier``.

    ``tier`` comes from the source directory (official vs community), NOT the
    manifest — that is a security boundary. Raises :class:`BundleLoadError` on a
    missing or malformed manifest.
    """
    locale_dir = Path(locale_dir)
    manifest = locale_dir / MANIFEST_NAME
    if yaml is None:
        raise BundleLoadError("PyYAML is not available")
    if not manifest.exists():
        raise BundleLoadError(f"no {MANIFEST_NAME} in {locale_dir}")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BundleLoadError(f"cannot read manifest in {locale_dir}: {exc}") from exc
    if not isinstance(data, dict):
        raise BundleLoadError(f"malformed manifest in {locale_dir}")
    code = (data.get("meta") or {}).get("code") or locale_dir.name
    return LocaleBundle(code=code, tier=tier, data=data, fallback=None)


def discover_bundles(directory: Path | str, tier: str) -> dict[str, LocaleBundle]:
    """Discover all bundles directly under ``directory``, stamping ``tier``.

    Subdirectories without a ``manifest.yaml`` are skipped. A missing directory
    yields an empty mapping (a fresh install has no community mods).
    """
    directory = Path(directory)
    out: dict[str, LocaleBundle] = {}
    if not directory.is_dir():
        return out
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or not (child / MANIFEST_NAME).exists():
            continue
        bundle = load_bundle(child, tier)
        out[bundle.code] = bundle
    return out


def build_chain(
    code: str,
    bundles: dict[str, LocaleBundle],
    _seen: tuple[str, ...] = (),
) -> LocaleBundle:
    """Return ``code``'s bundle with its ``meta.fallback`` chain wired in.

    Follows the first entry of ``meta.fallback`` recursively. Raises
    :class:`BundleLoadError` on an unknown locale or a cycle.
    """
    if code in _seen:
        raise BundleLoadError(f"fallback cycle through '{code}'")
    if code not in bundles:
        raise BundleLoadError(f"unknown locale '{code}'")
    bundle = bundles[code]
    chain = (bundle.data.get("meta") or {}).get("fallback") or []
    fallback = None
    if chain:
        fallback = build_chain(chain[0], bundles, _seen + (code,))
    return dataclasses.replace(bundle, fallback=fallback)


def validate_bundle(bundle: LocaleBundle, *, strict: bool = False) -> BundleValidation:
    """Validate a bundle.

    Structural checks always run (meta present, code set, known tier). With
    ``strict=True`` (the official-tier / CI gate) missing tier-required slots
    become errors — this is where "official must author guardrails" bites.
    """
    errors: list[str] = []
    warnings: list[str] = []

    meta = bundle.data.get("meta") if isinstance(bundle.data, dict) else None
    if not isinstance(meta, dict):
        errors.append("missing 'meta' section")
        return BundleValidation(bundle.code, False, errors, warnings)

    if not meta.get("code"):
        errors.append("meta.code is required")
    if bundle.tier not in TIERS:
        errors.append(f"unknown tier '{bundle.tier}'")

    manifest_tier = meta.get("tier")
    if manifest_tier and manifest_tier != bundle.tier:
        warnings.append(
            f"manifest tier '{manifest_tier}' overridden by source tier '{bundle.tier}'"
        )

    schema_version = meta.get("schema_version")
    if schema_version is None:
        warnings.append("meta.schema_version missing (assuming current)")
    elif schema_version != SCHEMA_VERSION:
        warnings.append(
            f"manifest schema_version {schema_version} != current {SCHEMA_VERSION}"
        )

    if strict:
        for slot in REQUIRED_SLOTS_BY_TIER.get(bundle.tier, ()):
            if slot not in bundle.data:
                errors.append(f"{bundle.tier} bundle missing required slot '{slot}'")

    return BundleValidation(bundle.code, not errors, errors, warnings)
