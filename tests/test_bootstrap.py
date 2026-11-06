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
                "hassil==3.12.0" if version == "2026.11.0" else "hassil==3.13.0"
            ]}
        main.__globals__["load_ha_manifest"] = manifest
        sys.argv = args
        sys.exit(main())
    os.execv(sys.executable, [sys.executable, *args])
elif name == "curl":
    if os.environ.get("FAIL_DOWNLOAD"):
        sys.exit(1)
    url = args[1]
    if url == "https://pypi.org/pypi/homeassistant/json":
        print(json.dumps({"info": {"version": "2026.11.1"}}))
    else:
        destination = Path(args[args.index("-o") + 1])
        contents = {
            "requirements_all.txt": "-r requirements.txt\nall-integrations==1.0\n",
            "requirements.txt": "upstream-core==1.0\n",
            "requirements_test.txt": "-r requirements_test_pre_commit.txt\nupstream-tests==1.0\n",
            "requirements_test_pre_commit.txt": "upstream-lint==1.0\n",
            "package_constraints.txt": "",
        }
        destination.write_text(contents[destination.name])
elif name == "uv":
    if args[:2] == ["pip", "install"]:
        if os.environ.get("FAIL_INSTALL"):
            sys.exit(1)
        contents = []
        def read_requirements(path):
            path = Path(path)
            for line in path.read_text().splitlines():
                if line.startswith("-r "):
                    read_requirements(path.parent / line[3:])
                elif line:
                    contents.append(line)
        for index, arg in enumerate(args):
            if arg == "--requirement":
                read_requirements(args[index + 1])
        with open(os.environ["INSTALLED_REQUIREMENTS"], "a") as installed:
            installed.write("\n".join(contents) + "\n")
    elif args[:2] != ["pip", "uninstall"]:
        sys.exit(2)
"""


@pytest.fixture
def bootstrap(tmp_path: Path):
    """Run bootstrap in a checkout with spaces in its temporary directory path."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    for name in ("bootstrap", "collect-integration-requirements"):
        shutil.copy2(REPO_ROOT / "scripts" / name, project / "scripts" / name)
    (project / "requirements.txt").write_text("pytest-homeassistant\n")
    (project / "hacs.json").write_text('{"homeassistant": "2026.11.0"}')
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
        commands = (
            [json.loads(line) for line in log.read_text().splitlines()]
            if log.exists()
            else []
        )
        requirements = installed.read_text() if installed.exists() else ""
        assert not list(temporary.iterdir())
        return result, commands, requirements

    return run


@pytest.mark.parametrize(
    ("version", "selected"),
    [
        ("hacs", "2026.11.0"),
        ("latest", "2026.11.1"),
        ("2026.11.0", "2026.11.0"),
        ("2026.12.0b1", "2026.12.0b1"),
    ],
)
def test_bootstrap_version(bootstrap, version: str, selected: str) -> None:
    """Install matching HA and test packages together with integration requirements."""
    result, commands, requirements = bootstrap(HA_VERSION=version)
    assert result.returncode == 0, result.stderr
    assert f"homeassistant=={selected}" in requirements.splitlines()
    assert f"pytest-homeassistant=={selected}" in requirements.splitlines()
    expected = "3.12.0" if selected == "2026.11.0" else "3.13.0"
    assert f"hassil=={expected}" in requirements
    uv = [command for command in commands if command[0] == "uv"]
    assert [command[2] for command in uv] == ["install", "uninstall"]
    assert uv[0].count("--constraint") == 2
    assert "--system" in uv[0]
    assert "--override" not in uv[0]
    assert ("--prerelease" in uv[0]) == (version == "2026.12.0b1")
    assert uv[1] == [
        "uv",
        "pip",
        "uninstall",
        "--system",
        "pytest-homeassistant-custom-component",
    ]
    assert not any(
        "requirements_dev.txt" in arg for command in commands for arg in command
    )
    assert commands[-2:] == [["pre-commit", "uninstall"], ["prek", "install"]]


def test_bootstrap_default(bootstrap) -> None:
    """Use the HACS minimum when no version override is supplied."""
    result, _, requirements = bootstrap()
    assert result.returncode == 0, result.stderr
    assert "homeassistant==2026.11.0" in requirements.splitlines()
    assert "pytest-homeassistant==2026.11.0" in requirements.splitlines()


@pytest.mark.parametrize("version", ["dev", "feature/test"])
def test_bootstrap_git_ref(bootstrap, version: str) -> None:
    """Build both packages from the selected monorepo ref and refresh cached builds."""
    result, commands, requirements = bootstrap(HA_VERSION=version)
    assert result.returncode == 0, result.stderr
    source = f"git+https://github.com/home-assistant/core.git@{version}"
    assert f"homeassistant @ {source}" in requirements.splitlines()
    assert (
        f"pytest-homeassistant @ {source}#subdirectory=packages/pytest-homeassistant"
        in requirements.splitlines()
    )
    uv = [command for command in commands if command[:3] == ["uv", "pip", "install"]]
    assert len(uv) == 1
    refreshed = [
        uv[0][index + 1]
        for index, arg in enumerate(uv[0])
        if arg == "--reinstall-package"
    ]
    assert set(refreshed) == {"homeassistant", "pytest-homeassistant"}
    assert "hassil==3.13.0" in requirements


def test_bootstrap_virtualenv(bootstrap, tmp_path: Path) -> None:
    """Install into an active virtual environment without requesting a system install."""
    result, commands, _ = bootstrap(VIRTUAL_ENV=str(tmp_path / "venv"))
    assert result.returncode == 0, result.stderr
    uv = [command for command in commands if command[0] == "uv"]
    assert all("--system" not in command for command in uv)
    assert uv[1] == ["uv", "pip", "uninstall", "pytest-homeassistant-custom-component"]


def test_full_bootstrap(bootstrap) -> None:
    """Full setup still includes the manifest requirements and upstream files."""
    result, commands, requirements = bootstrap("--full")
    assert result.returncode == 0, result.stderr
    assert "hassil==3.12.0" in requirements
    assert "all-integrations==1.0" in requirements
    assert "upstream-core==1.0" in requirements
    assert "upstream-tests==1.0" in requirements
    assert "upstream-lint==1.0" in requirements
    assert "pytest-homeassistant==2026.11.0" in requirements
    assert [command[2] for command in commands if command[0] == "uv"] == [
        "install",
        "uninstall",
    ]
    urls = [command[2] for command in commands if command[0] == "curl"]
    for filename in (
        "requirements.txt",
        "requirements_test.txt",
        "requirements_test_pre_commit.txt",
    ):
        assert any(url.endswith(f"/{filename}") for url in urls)


@pytest.mark.parametrize("arguments", [(), ("--full",)])
@pytest.mark.parametrize("failure", ["FAIL_DOWNLOAD", "FAIL_INSTALL", "FAIL_MANIFEST"])
def test_bootstrap_failure(bootstrap, arguments: tuple[str, ...], failure: str) -> None:
    """Abort on missing inputs or dependency conflicts without retrying installations."""
    result, commands, _ = bootstrap(*arguments, **{failure: "1"})
    assert result.returncode != 0
    assert not any(command[0] in {"pre-commit", "prek"} for command in commands)
    uv = [command for command in commands if command[0] == "uv"]
    assert len(uv) == (1 if failure == "FAIL_INSTALL" else 0)


def test_bootstrap_invalid_argument(bootstrap) -> None:
    """Reject unsupported options before downloading or installing dependencies."""
    result, commands, _ = bootstrap("--unknown")
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert commands == []
