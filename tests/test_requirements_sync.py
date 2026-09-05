"""Test synchronization of Home Assistant dependency pins."""

import json
from pathlib import Path
import runpy

import pytest


@pytest.mark.parametrize(
    ("minimum", "development", "existing", "expected", "source"),
    [
        ("", "==1.1.0", "==1.0.0", "==1.1.0", "dev"),
        ("", "==1.1.0", "", "==1.1.0", "dev"),
        ("==1.0.0", "==1.1.0", "==0.9.0", ">=1.0.0,<=1.1.0", "2026.5.0"),
        ("==1.1.0", "==1.1.0", "==1.0.0", "==1.1.0", "2026.5.0"),
        ("==1.0.0", "", "==0.9.0", ">=1.0.0", "2026.5.0"),
        ("", "", "==1.0.0", "==1.0.0", None),
        ("", ">=1.1.0", "==1.0.0", "==1.0.0", None),
    ],
)
def test_sync_homeassistant_requirements(  # noqa: PLR0917
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    minimum: str,
    development: str,
    existing: str,
    expected: str,
    source: str | None,
) -> None:
    """Use dev pins for new dependencies while retaining existing range behavior."""
    script = Path(__file__).resolve().parents[1] / "scripts/check-requirements-sync"
    namespace = runpy.run_path(str(script))
    sync = namespace["sync_homeassistant_ignored_requirements"]
    namespace = sync.__globals__

    hacs = tmp_path / "hacs.json"
    hacs.write_text(json.dumps({"homeassistant": "2026.5.0"}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    original = [f"gazetteer-matcher{existing}"] if existing else []
    manifest.write_text(json.dumps({"requirements": original}), encoding="utf-8")
    dependabot = tmp_path / "dependabot.yml"
    dependabot.write_text(
        'updates:\n  - package-ecosystem: "pip"\n'
        '    ignore:\n      - dependency-name: "gazetteer-matcher"\n',
        encoding="utf-8",
    )
    for name, path in (
        ("REPO_ROOT", tmp_path),
        ("HACS", hacs),
        ("MANIFEST", manifest),
        ("DEPENDABOT", dependabot),
    ):
        monkeypatch.setitem(namespace, name, path)

    def download(version: str, directory: Path) -> Path:
        specifier = {"2026.5.0": minimum, "dev": development}[version]
        path = directory / f"{version}.txt"
        path.write_text(
            f"gazetteer-matcher{specifier}\n" if specifier else "",
            encoding="utf-8",
        )
        return path

    monkeypatch.setitem(namespace, "download_ha_requirements_all", download)
    requirements = namespace["load_requirements_from_lines"](original)
    updated = sync(original.copy(), requirements)

    assert updated == [f"gazetteer-matcher{expected}"]
    assert json.loads(manifest.read_text(encoding="utf-8"))["requirements"] == (
        updated if existing else []
    )
    output = capsys.readouterr().out
    if source is None:
        assert output == ""
    else:
        assert f"from Home Assistant {source}" in output

    # A second run should leave both files unchanged and report no updates.
    manifest_before = manifest.read_text(encoding="utf-8")
    requirements = namespace["load_requirements_from_lines"](updated)
    assert sync(updated.copy(), requirements) == updated
    assert manifest.read_text(encoding="utf-8") == manifest_before
    assert capsys.readouterr().out == ""
