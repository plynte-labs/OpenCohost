"""Keep model-inclusive NSIS payloads out of a single solid compression block."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex


TAURI_DIR = Path(__file__).resolve().parent.parent / "OpenCohost_UI" / "src-tauri"


def _configured_template() -> str:
    config = json.loads((TAURI_DIR / "tauri.conf.json").read_text(encoding="utf-8"))
    nsis = config["bundle"]["windows"]["nsis"]
    assert nsis["compression"] == "zlib"
    return (TAURI_DIR / nsis["template"]).read_text(encoding="utf-8")


def test_nsis_compresses_files_individually_without_changing_configured_algorithm():
    template = _configured_template()
    directives = re.findall(r"^\s*SetCompressor\s+([^\r\n]+)", template, re.MULTILINE)
    assert len(directives) == 1, "Exactly one compressor must govern the payload"
    arguments = shlex.split(directives[0])
    assert "/SOLID" not in [argument.upper() for argument in arguments], (
        "Solid compression merges the model-inclusive payload into a block that exceeds NSIS's 31-bit mmap limit"
    )
    assert arguments == ["{{compression}}"], "Keep the algorithm supplied by Tauri configuration"


def test_nsis_preserves_complete_resource_iteration_and_file_destinations():
    template = _configured_template()
    resource_blocks = re.findall(r"{{#each resources}}(.*?){{/each}}", template, re.DOTALL)
    copy_commands = [
        shlex.split(line.strip())
        for block in resource_blocks
        for line in block.splitlines()
        if re.match(r"\s*File\s", line)
    ]
    assert copy_commands == [["File", "/a", "/oname={{this.[1]}}", "{{no-escape @key}}"]], (
        "Every resource must retain its mapped destination and unescaped source; do not filter the payload"
    )
    install_block = next(block for block in resource_blocks if re.search(r"\bFile\s", block))
    assert not re.search(r"{{[#^]|^\s*!if", install_block, re.MULTILINE), "Resource copies must stay unconditional"
    directory_blocks = re.findall(r"{{#each resources_dirs}}(.*?){{/each}}", template, re.DOTALL)
    assert len(directory_blocks) == 1
    directory_commands = [shlex.split(line.strip()) for line in directory_blocks[0].splitlines() if line.strip()]
    assert directory_commands == [["CreateDirectory", "$INSTDIR\\{{this}}"]]
