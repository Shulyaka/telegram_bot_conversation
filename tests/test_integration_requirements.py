"""Test dependency collection without importing Home Assistant."""

import json
from pathlib import Path
import runpy
from urllib.error import HTTPError

import pytest


@pytest.fixture
def collector():
    """Load the standalone bootstrap helper."""
    script = (
        Path(__file__).resolve().parents[1] / "scripts/collect-integration-requirements"
    )
    return runpy.run_path(str(script))


def write_manifest(root: Path, domain: str, **manifest) -> None:
    """Write a local integration manifest."""
    directory = root / domain
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({"domain": domain, **manifest}), encoding="utf-8"
    )


def test_dependency_graph(collector, tmp_path: Path) -> None:
    """Follow multiple roots, optional dependencies and cycles only once."""
    write_manifest(
        tmp_path,
        "custom_one",
        dependencies=["conversation", "custom_two"],
        after_dependencies=["tts"],
        requirements=["custom-package[extra]>=1.0; python_version >= '3.14'"],
    )
    write_manifest(tmp_path, "custom_two", dependencies=["conversation"])
    upstream = {
        "conversation": {
            "dependencies": ["http"],
            "requirements": ["hassil==3.5.0"],
        },
        "http": {"dependencies": ["custom_one"]},
        "tts": {
            "dependencies": ["http"],
            "after_dependencies": ["media_player"],
            "requirements": ["hassil==3.5.0"],
        },
        "media_player": {"requirements": ["mutagen==1.47.0"]},
    }
    calls = []

    def load(domain):
        calls.append(domain)
        return upstream[domain]

    assert collector["collect_requirements"](tmp_path, load) == [
        "custom-package[extra]>=1.0; python_version >= '3.14'",
        "hassil==3.5.0",
        "mutagen==1.47.0",
    ]
    assert sorted(calls) == sorted(upstream)


@pytest.mark.parametrize(
    ("version", "requirements"),
    [
        ("2026.5.0", ["hassil==3.5.0", "home-assistant-intents==2026.5.5"]),
        (
            "2026.9.1",
            [
                "gazetteer-matcher==1.1.0",
                "hassil==3.12.0",
                "home-assistant-intents==2026.8.28",
            ],
        ),
    ],
)
def test_release_requirements(
    collector, tmp_path: Path, version: str, requirements: list[str]
) -> None:
    """A dependency introduced in a newer release stays out of older setups."""
    write_manifest(tmp_path, "custom", dependencies=["conversation"])
    manifests = {version: {"requirements": requirements}}
    assert (
        collector["collect_requirements"](tmp_path, lambda domain: manifests[version])
        == requirements
    )


def test_no_local_manifests(collector, tmp_path: Path) -> None:
    """Fail clearly when called with the wrong integration directory."""
    with pytest.raises(ValueError, match="No integration manifests"):
        collector["collect_requirements"](tmp_path, lambda domain: {})


def test_manifest_download_failure(collector, monkeypatch: pytest.MonkeyPatch) -> None:
    """Report missing upstream manifests instead of silently omitting packages."""
    load = collector["load_ha_manifest"]

    def fail(url, **kwargs):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setitem(load.__globals__, "urlopen", fail)
    with pytest.raises(RuntimeError, match="Home Assistant 2026.5.0.*missing"):
        load("2026.5.0", "missing")
