"""pyproject must not allow an edge-tts that gets HTTP 403 on every synth (7.2.8)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_cloud_tts_extra_requires_edge_tts_7_3_1_or_newer():
    text = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
    m = re.search(r'"edge-tts>=(\d+)\.(\d+)\.(\d+)"', text)
    assert m, "edge-tts floor not found in pyproject.toml"
    assert tuple(int(x) for x in m.groups()) >= (7, 3, 1)
