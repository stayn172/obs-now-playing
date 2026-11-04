import os

from tools.licenses import license_name, runtime_distributions, write_notices

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQS = os.path.join(ROOT, "requirements.txt")


def test_runtime_tree_includes_direct_and_transitive_deps():
    names = {d.metadata["Name"].lower() for d in runtime_distributions(REQS)}
    assert {"aiohttp", "pystray", "pillow"} <= names
    assert "yarl" in names            # transitive (via aiohttp)
    assert "pytest" not in names      # dev-only stays out


def test_write_notices(tmp_path):
    names = write_notices(str(tmp_path), REQS)
    text = (tmp_path / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    assert "Python" in names and "pystray" in text and "LGPL" in text
    assert any((tmp_path / "licenses" / "pystray").iterdir())
    # every row keeps its columns apart (long versions like 4!10.0.28000.2957)
    for line in text.splitlines():
        if line.startswith("winrt-"):
            assert "  " in line.split(" ", 1)[1] or line.count(" ") >= 2


def test_license_name_falls_back_to_something():
    for d in runtime_distributions(REQS):
        assert license_name(d)
