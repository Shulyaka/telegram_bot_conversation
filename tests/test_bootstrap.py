"""Test bootstrap's installation flow without downloading or installing packages."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Stand-ins record commands and provide release-specific inputs to the real scripts.
TOOL = r"""
import json
import os
from pathlib import Path
import runpy
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["BOOTSTRAP_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
if name == "python3":
    if args[0] == "scripts/collect-integration-requirements":
        if os.environ.get("FAIL_MANIFEST"):
            sys.exit(1)
        namespace = runpy.run_path(args[0])
        main = namespace["main"]
        def manifest(version, domain):
            assert domain == "conversation"
            return {"requirements": [
                "hassil==3.5.0" if version == "2026.5.0" else "hassil==3.12.0"
            ]}
        main.__globals__["load_ha_manifest"] = manifest
        sys.argv = args
        sys.exit(main())
    os.execv(sys.executable, [sys.executable, *args])
elif name == "curl":
    url = args[1]
    if url == "https://pypi.org/pypi/homeassistant/json":
        print(json.dumps({"info": {"version": "2026.9.1"}}))
    else:
        destination = Path(args[args.index("-o") + 1])
        destination.write_text("mypy==1.0\n" if url.endswith("requirements_dev.txt") else "")
elif name == "uv":
    if args[1] == "compile":
        combined = Path(args[-1]).name == "ha-requirements.txt"
        if os.environ.get("FAIL_COMPILE") or (combined and os.environ.get("CONFLICT_COMPILE")):
            sys.exit(1)
        contents = ""
        contents += "pytest-homeassistant-custom-component==0.13.329\n"
        Path(args[args.index("--output-file") + 1]).write_text(contents)
    elif args[1] == "install":
        inputs = [Path(args[index + 1]).name for index, arg in enumerate(args) if arg == "--requirement"]
        combined = "project-requirements.txt" in inputs and "ha-requirements.txt" in inputs
        if (combined and os.environ.get("CONFLICT_INSTALL")) or os.environ.get("FAIL_INSTALL"):
            sys.exit(1)
        if inputs == ["ha-requirements.txt"] and os.environ.get("FAIL_HA_INSTALL"):
            sys.exit(1)
        contents = []
        def read_requirements(path):
            for line in Path(path).read_text().splitlines():
                if line.startswith("-r "):
                    read_requirements(line[3:])
                elif line:
                    contents.append(line)
        for index, arg in enumerate(args):
            if arg == "--requirement":
                read_requirements(args[index + 1])
        with open(os.environ["INSTALLED_REQUIREMENTS"], "a") as installed:
            installed.write("\n".join(contents) + "\n")
        with open(os.environ["BOOTSTRAP_LOG"], "a") as log:
            log.write(json.dumps(["installed", *contents]) + "\n")
    else:
        sys.exit(2)
"""


@pytest.fixture
def bootstrap(tmp_path: Path):
    """Run bootstrap in a checkout with spaces in its temporary directory path."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    for name in ("bootstrap", "collect-integration-requirements"):
        shutil.copy2(REPO_ROOT / "scripts" / name, project / "scripts" / name)
    (project / "requirements.txt").write_text("pytest-homeassistant-custom-component\n")
    (project / "hacs.json").write_text('{"homeassistant": "2026.5.0"}')
    component = project / "custom_components" / "custom"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text('{"dependencies": ["conversation"]}')
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("uv", "curl", "python3", "pre-commit", "prek"):
        tool = tools / name
        tool.write_text(f"#!{sys.executable}\n{TOOL}")
        tool.chmod(0o755)
    temporary = tmp_path / "temporary files"
    temporary.mkdir()
    log = tmp_path / "commands.jsonl"
    installed = tmp_path / "installed.txt"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "TMPDIR": str(temporary),
        "BOOTSTRAP_LOG": str(log),
        "INSTALLED_REQUIREMENTS": str(installed),
        "VIRTUAL_ENV": "",
    }
    env.pop("HA_VERSION", None)

    def run(*arguments, **settings):
        result = subprocess.run(
            ["sh", "scripts/bootstrap", *arguments],
            cwd=project,
            env={**env, **settings},
            capture_output=True,
            text=True,
            check=False,
        )
        commands = [json.loads(line) for line in log.read_text().splitlines()]
        requirements = installed.read_text() if installed.exists() else ""
        assert not list(temporary.iterdir())
        return result, commands, requirements

    return run


@pytest.mark.parametrize("version", ["hacs", "latest", "dev", "2026.9.0b6"])
def test_bootstrap_version(bootstrap, version: str) -> None:
    """Install selected integration requirements once, after a successful resolve."""
    result, commands, requirements = bootstrap(HA_VERSION=version)
    assert result.returncode == 0, result.stderr
    expected = "3.5.0" if version == "hacs" else "3.12.0"
    assert f"hassil=={expected}" in requirements
    uv = [command for command in commands if command[0] == "uv"]
    assert [command[2] for command in uv] == ["compile", "install"]
    assert "pytest-homeassistant-custom-component==0.13.329" in requirements
    assert all("--override" not in command for command in uv)
    assert all(
        ("--prerelease" in command) == (version == "2026.9.0b6") for command in uv
    )


def test_full_bootstrap(bootstrap) -> None:
    """Full setup still includes the manifest requirements and upstream files."""
    result, commands, requirements = bootstrap("--full")
    assert result.returncode == 0, result.stderr
    assert "hassil==3.5.0" in requirements
    assert [command[2] for command in commands if command[0] == "uv"] == ["install"]
    urls = [command[2] for command in commands if command[0] == "curl"]
    assert any(url.endswith("/requirements_test.txt") for url in urls)


@pytest.mark.parametrize("failure", ["FAIL_COMPILE", "FAIL_INSTALL", "FAIL_MANIFEST"])
def test_bootstrap_failure(bootstrap, failure: str) -> None:
    """Stop when neither resolution strategy works or required input is missing."""
    result, commands, _ = bootstrap(**{failure: "1"})
    assert result.returncode != 0
    assert not any(command[0] in {"pre-commit", "prek"} for command in commands)
    installations = [
        command for command in commands if command[:3] == ["uv", "pip", "install"]
    ]
    assert len(installations) == (2 if failure == "FAIL_INSTALL" else 0)


@pytest.mark.parametrize("version", ["hacs", "latest", "dev"])
@pytest.mark.parametrize("conflict", ["CONFLICT_COMPILE", "CONFLICT_INSTALL"])
def test_split_install(bootstrap, version: str, conflict: str) -> None:
    """Resolve PHACC independently, then install the selected HA dependencies last."""
    result, commands, _ = bootstrap(HA_VERSION=version, **{conflict: "1"})
    assert result.returncode == 0, result.stderr
    assert "best-effort environment" in result.stdout
    installed = [command[1:] for command in commands if command[0] == "installed"]
    assert len(installed) == 2
    assert "pytest-homeassistant-custom-component==0.13.329" in installed[0]
    assert "mypy==1.0" in installed[0]
    assert not any("pytest-homeassistant" in item for item in installed[1])
    selected = "2026.5.0" if version == "hacs" else "2026.9.1"
    expected = (
        "homeassistant @ git+https://github.com/home-assistant/core.git@dev"
        if version == "dev"
        else f"homeassistant=={selected}"
    )
    assert expected in installed[1]
    assert f"hassil=={'3.5.0' if version == 'hacs' else '3.12.0'}" in installed[1]
    uv = [command for command in commands if command[0] == "uv"]
    assert "--constraint" not in uv[-2]
    assert "--constraint" in uv[-1]
    if conflict == "CONFLICT_COMPILE":
        assert [command[2] for command in uv] == [
            "compile",
            "compile",
            "install",
            "install",
        ]
        assert "--constraint" not in uv[1]
    assert commands[-2:] == [["pre-commit", "uninstall"], ["prek", "install"]]


def test_full_split_install(bootstrap) -> None:
    """Full setup also retries independently when the combined install conflicts."""
    result, commands, requirements = bootstrap("--full", CONFLICT_INSTALL="1")
    assert result.returncode == 0, result.stderr
    assert "hassil==3.5.0" in requirements
    installs = [
        command for command in commands if command[:3] == ["uv", "pip", "install"]
    ]
    assert len(installs) == 3
    assert "--constraint" not in installs[1]
    assert "--constraint" in installs[2]
    assert commands[-1] == ["prek", "install"]


def test_split_ha_install_failure(bootstrap) -> None:
    """A failed HA install after the fallback must still report failure."""
    result, commands, _ = bootstrap(CONFLICT_COMPILE="1", FAIL_HA_INSTALL="1")
    assert result.returncode != 0
    assert not any(command[0] in {"pre-commit", "prek"} for command in commands)
