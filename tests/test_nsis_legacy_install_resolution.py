"""Static contract: the NSIS reinstall path must find an install made under an
older publisher key and must never run the old uninstaller with an empty `_?=`."""

from __future__ import annotations

import re
from pathlib import Path

NSI = Path(__file__).parent.parent / "OpenCohost_UI" / "src-tauri" / "custom-installer.nsi"


def _text() -> str:
    return NSI.read_text(encoding="utf-8")


def _function(name: str) -> str:
    m = re.search(rf"^Function {name}\b(.*?)^FunctionEnd", _text(), re.S | re.M)
    assert m, f"Function {name} missing"
    return m.group(1)


def _section(name: str) -> str:
    m = re.search(rf"^Section {name}\b(.*?)^SectionEnd", _text(), re.S | re.M)
    assert m, f"Section {name} missing"
    return m.group(1)


def test_resolver_function_exists_with_full_fallback_chain():
    body = _function("ResolvePreviousInstallDir")
    assert '"${MANUPRODUCTKEY}"' in body
    assert '"${UNINSTKEY}" "InstallLocation"' in body
    assert '"${UNINSTKEY}" "UninstallString"' in body
    assert 'Software\opencohost\${PRODUCTNAME}' in body
    # quotes are stripped by the helper the resolver calls
    assert "Call _StripQuotes" in body
    assert '$\\"' in _function("_StripQuotes")


def test_reinstall_path_uses_resolver_and_guards_empty_dir():
    body = _function("PageLeaveReinstall")
    assert "Call ResolvePreviousInstallDir" in body
    assert 'ReadRegStr $4 SHCTX "${MANUPRODUCTKEY}"' not in body
    guard = body.index('StrCmp $4 ""')
    run = body.index("_?=$4")
    assert guard < run, "empty-dir guard must precede the _?= uninstall command"
    assert "previous version from Windows Settings" in body


def test_restore_previous_install_location_uses_resolver():
    body = _function("RestorePreviousInstallLocation")
    assert "Call ResolvePreviousInstallDir" in body
    assert 'ReadRegStr $4 SHCTX "${MANUPRODUCTKEY}"' not in body


def test_legacy_opencohost_key_is_read_only_and_preserved_across_install_and_uninstall():
    """Preserve HKCU\\Software\\opencohost\\OpenCohost across 0.3.0 -> 0.5.0
    upgrades so setup_state.detect_legacy_install() reliably sees the prior install."""
    full_lines = [
        line.strip()
        for line in _text().splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]
    legacy_refs = [line for line in full_lines if r"Software\opencohost" in line]
    assert legacy_refs == [
        'ReadRegStr $4 SHCTX "Software\\opencohost\\${PRODUCTNAME}" ""'
    ]
    assert legacy_refs[0] in _function("ResolvePreviousInstallDir")

    install_body = _section("Install")
    assert 'WriteRegStr SHCTX "${MANUPRODUCTKEY}" "" $INSTDIR' in install_body
    assert "DeleteReg" not in install_body
    assert "Software\\opencohost" not in install_body.lower()

    uninstall_body = _section("Uninstall")
    uninstall_lines = [
        line.strip()
        for line in uninstall_body.splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]
    # Never delete the legacy OpenCohost subkey (case-insensitive on Windows).
    assert not any(
        "DeleteReg" in line
        and ("opencohost\\${productname}" in line.lower() or "opencohost\\opencohost" in line.lower())
        for line in uninstall_lines
    )
    # Any DeleteRegKey targeting the parent Software\OpenCohost key must use /ifempty
    # so an existing Software\opencohost\OpenCohost subkey keeps it alive.
    parent_deletes = [
        line for line in uninstall_lines if re.search(r'DeleteReg\w+.*"Software\\OpenCohost"', line, re.I)
    ]
    assert parent_deletes == ['DeleteRegKey /ifempty HKCU "Software\\OpenCohost"']

    # Upgrade uninstall passes /UPDATE /P so the older uninstaller also keeps its registry state.
    reinstall_body = _function("PageLeaveReinstall")
    assert '"/UPDATE"' in reinstall_body or "/UPDATE" in reinstall_body
    assert '"/P"' in reinstall_body or "/P" in reinstall_body
