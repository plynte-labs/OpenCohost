"""i18n-core — CLI to set the next-boot locale.

    python -m opencohost.i18n --set-locale en     # takes effect next start
    python -m opencohost.i18n --show-locale
    python -m opencohost.i18n --list

The CLI validates the requested code against the locales actually available
(official + community bundles) and rejects unknown ones before persisting.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from opencohost.i18n import state, tags
from opencohost.i18n.registry import (
    community_locales_dir,
    discover_bundles,
    official_locales_dir,
)


def available_codes() -> list[str]:
    """All locale codes available to load (official + community)."""
    codes = set(discover_bundles(official_locales_dir(), "official"))
    codes |= set(discover_bundles(community_locales_dir(), "community"))
    return sorted(codes)


def main(
    argv: Sequence[str] | None = None,
    *,
    locale_file: Path | str | None = None,
    codes: Sequence[str] | None = None,
) -> int:
    """Run the i18n CLI. Returns a process exit code (0 ok, 2 bad usage)."""
    parser = argparse.ArgumentParser(
        prog="opencohost-i18n",
        description="Set the locale OpenCohost uses on its next start.",
    )
    parser.add_argument("--set-locale", metavar="CODE", help="set the next-boot locale")
    parser.add_argument("--show-locale", action="store_true", help="print the persisted locale")
    parser.add_argument("--list", action="store_true", help="list available locales")
    args = parser.parse_args(argv)

    available = list(codes) if codes is not None else available_codes()

    if args.set_locale:
        # Normalize (BCP 47) and match to a real bundle: 'EN', 'en_US' → 'en'.
        code = tags.match(args.set_locale, available)
        if code is None:
            print(
                f"unknown locale '{args.set_locale.strip()}'. available: "
                f"{', '.join(available) if available else '(none)'}"
            )
            return 2
        state.set_locale(code, locale_file)
        print(f"locale set to '{code}' — takes effect on next start")
        return 0

    if args.list:
        print("\n".join(available) if available else "(none)")
        return 0

    if args.show_locale:
        print(state.get_locale(locale_file))
        return 0

    parser.print_help()
    return 0
