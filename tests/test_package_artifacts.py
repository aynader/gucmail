from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
import zipfile
from importlib.resources import files
from pathlib import Path, PurePosixPath

import pytest

import gucmail

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "gucmail"
HELP_ROOT = SOURCE_ROOT / "help"
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = gucmail.__version__
ARTIFACT_DIR = Path(os.environ.get("GUCMAIL_ARTIFACT_DIR", ROOT / "dist"))
REQUIRE_ARTIFACTS = os.environ.get("GUCMAIL_REQUIRE_RELEASE_ARTIFACTS") == "1"

FORBIDDEN_ARCHIVE_PARTS = {
    ".coverage",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "agents.md",
    "creds.env",
    "guc.py",
    "secondpass.md",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".env",
    ".key",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
}
ALLOWED_SDIST_ROOTS = {
    ".gitignore",
    "CHANGELOG.md",
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "docs",
    "pyproject.toml",
    "qa",
    "src",
    "tests",
}


def _corpus_counts(corpus: object) -> tuple[int, int]:
    assert isinstance(corpus, dict)
    assert corpus.get("schema") == "gucmail.command-corpus/v1"
    groups = corpus.get("groups")
    assert isinstance(groups, list)
    phrases = [phrase for group in groups for phrase in group["phrases"]]
    return len(groups), len(phrases)


def _source_package_members() -> set[str]:
    return {
        PurePosixPath("gucmail", *path.relative_to(SOURCE_ROOT).parts).as_posix()
        for path in SOURCE_ROOT.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
    }


def _release_artifact(pattern: str, *, kind: str) -> Path:
    matches = sorted(ARTIFACT_DIR.glob(pattern), key=lambda path: path.stat().st_mtime)
    if not matches:
        message = f"No {kind} for version {VERSION} exists in {ARTIFACT_DIR}."
        if REQUIRE_ARTIFACTS:
            pytest.fail(message)
        pytest.skip(f"{message} Set GUCMAIL_REQUIRE_RELEASE_ARTIFACTS=1 in the release gate.")
    return matches[-1]


def _assert_archive_names_safe(names: set[str]) -> None:
    violations: list[str] = []
    for name in names:
        path = PurePosixPath(name)
        lowered_parts = {part.casefold() for part in path.parts}
        if path.is_absolute() or ".." in path.parts:
            violations.append(name)
            continue
        if lowered_parts & FORBIDDEN_ARCHIVE_PARTS:
            violations.append(name)
            continue
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            violations.append(name)
            continue
        if any(part.casefold().startswith(".tmp-") for part in path.parts):
            violations.append(name)
    assert not violations, f"Private or runtime files entered the release archive: {violations}"


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_gucmail(venv: Path) -> Path:
    return venv / ("Scripts/gucmail.exe" if os.name == "nt" else "bin/gucmail")


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": "", "GUCMAIL_PASSWORD": ""}
    if os.environ.get("GUCMAIL_AUDIT_GUARDED") == "1":
        from qa.offline.children import guarded_environment

        environment = guarded_environment(environment)
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(Path(environment["GUCMAIL_AUDIT_PRIVATE"]) / "bootstrap"), str(ROOT))
        )  # No source package on the installed interpreter's initial search path.
        command = [argument for argument in command if argument != "-I"]
        if Path(command[0]).name.casefold() == "gucmail.exe":
            # Execute the generated launcher's actual ZIP entry-point payload
            # through the owned Python interpreter, retaining sitecustomize.
            # Native Win32 launcher execution remains separately unverified.
            command = [str(Path(command[0]).with_name("python.exe")), *command]
    return subprocess.run(  # noqa: S603 - fixed test commands, never user input
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=environment,
    )


def test_help_and_parser_resources_are_declared_and_importable() -> None:
    artifacts = PROJECT["tool"]["hatch"]["build"]["artifacts"]
    assert "src/gucmail/data/builtin-themes.json.gz" in artifacts
    assert "src/gucmail/data/command_corpus.json" in artifacts
    assert "src/gucmail/data/shortcut_catalog.json" in artifacts
    assert "src/gucmail/help/catalog.toml" in artifacts
    assert "src/gucmail/help/pages/**/*.md" in artifacts

    help_root = files("gucmail.help")
    assert help_root.joinpath("catalog.toml").is_file()
    assert help_root.joinpath("pages", "start", "overview.md").is_file()
    corpus = json.loads(
        files("gucmail.data").joinpath("command_corpus.json").read_text(encoding="utf-8")
    )
    assert corpus["schema"] == "gucmail.command-corpus/v1"
    groups = corpus["groups"]
    assert isinstance(groups, list)
    phrases = [phrase for group in groups for phrase in group["phrases"]]
    assert len(groups) >= 70
    assert len(phrases) >= 450
    assert len({phrase.casefold() for phrase in phrases}) == len(phrases)


def test_project_metadata_is_release_consistent() -> None:
    project = PROJECT["project"]
    classifiers = set(project["classifiers"])
    # One version source: hatch reads __init__.py, nothing else repeats the number.
    assert "version" not in project
    assert project["dynamic"] == ["version"]
    assert PROJECT["tool"]["hatch"]["version"]["path"] == "src/gucmail/__init__.py"
    assert re.fullmatch(r"\d+\.\d+\.\d+", gucmail.__version__)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {gucmail.__version__}" in changelog
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert "Development Status :: 4 - Beta" in classifiers
    assert "Development Status :: 5 - Production/Stable" not in classifiers
    assert project["license"] == "MIT"
    assert project["scripts"] if "scripts" in project else PROJECT["project"]["scripts"]
    assert PROJECT["project"]["scripts"]["gucmail"] == "gucmail.entry:main"
    assert PROJECT["build-system"]["requires"] == ["hatchling>=1.27,<2"]
    assert "hatchling>=1.27,<2" in project["optional-dependencies"]["dev"]


def test_sdist_policy_is_an_explicit_privacy_allowlist() -> None:
    target = PROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]
    includes = set(target["include"])
    excludes = {value.casefold() for value in target["exclude"]}
    assert set(target["only-include"]) == {value.lstrip("/") for value in includes}
    assert target["skip-excluded-dirs"] is True
    assert {
        "/.gitignore",
        "/src",
        "/docs",
        "/README.md",
        "/pyproject.toml",
    } <= includes
    assert {"/agents.md", "/secondpass.md", "/creds.env", "/guc.py", "/*.png"} <= excludes


def test_every_project_artifact_in_release_directory_is_privacy_safe() -> None:
    artifacts = sorted(
        [*ARTIFACT_DIR.glob("gucmail-*.whl"), *ARTIFACT_DIR.glob("gucmail-*.tar.gz")]
    )
    if not artifacts:
        message = f"No GUC Mail artifacts exist in {ARTIFACT_DIR}."
        if REQUIRE_ARTIFACTS:
            pytest.fail(message)
        pytest.skip(message)

    for artifact in artifacts:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                names = set(archive.namelist())
        else:
            with tarfile.open(artifact, mode="r:gz") as archive:
                names = {member.name for member in archive.getmembers()}
        _assert_archive_names_safe(names)


def test_built_wheel_contains_the_complete_package_and_no_private_files() -> None:
    wheel = _release_artifact(f"gucmail-{VERSION}-*.whl", kind="wheel")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
        entry_points = archive.read(entry_points_name).decode("utf-8")
        corpus = json.loads(archive.read("gucmail/data/command_corpus.json"))
        stale = [
            name
            for name in _source_package_members() & names
            if archive.read(name) != (ROOT / "src" / name).read_bytes()
        ]
        assert not stale, f"Wheel contains stale package contents: {sorted(stale)}"

    _assert_archive_names_safe(names)
    missing = sorted(_source_package_members() - names)
    assert not missing, f"Wheel omitted package files: {missing}"
    assert "gucmail/help/catalog.toml" in names
    assert (
        len(
            {
                name
                for name in names
                if name.startswith("gucmail/help/pages/") and name.endswith(".md")
            }
        )
        >= 45
    )
    assert "gucmail/py.typed" in names
    assert f"Version: {VERSION}" in metadata
    assert "Classifier: Development Status :: 4 - Beta" in metadata
    assert "License-Expression: MIT" in metadata
    assert "gucmail = gucmail.entry:main" in entry_points
    group_count, phrase_count = _corpus_counts(corpus)
    assert group_count >= 70
    assert phrase_count >= 450


def test_built_sdist_honors_the_privacy_allowlist() -> None:
    sdist = _release_artifact(f"gucmail-{VERSION}.tar.gz", kind="source archive")
    with tarfile.open(sdist, mode="r:gz") as archive:
        members = archive.getmembers()
        names = {member.name for member in members}
        files_by_name = {member.name: member for member in members if member.isfile()}

        roots: set[str] = set()
        for name in names:
            parts = PurePosixPath(name).parts
            if len(parts) >= 2:
                roots.add(parts[1])

        pyproject_name = next(name for name in files_by_name if name.endswith("/pyproject.toml"))
        archive_root = PurePosixPath(pyproject_name).parent
        for package_member in _source_package_members():
            name = str(archive_root / "src" / package_member)
            assert name in files_by_name, f"Source archive omitted {package_member}"
            stream = archive.extractfile(files_by_name[name])
            assert stream is not None
            assert stream.read() == (ROOT / "src" / package_member).read_bytes(), (
                f"Source archive contains stale contents: {package_member}"
            )
        # Reproduction scripts and regression tests are part of the source release.
        # Matching only product modules would allow a stale qualification harness.
        for directory in (ROOT / "qa", ROOT / "tests"):
            for source in directory.rglob("*.py"):
                if "__pycache__" in source.parts:
                    continue
                name = str(archive_root / source.relative_to(ROOT).as_posix())
                assert name in files_by_name, f"Source archive omitted {source.name}"
                stream = archive.extractfile(files_by_name[name])
                assert stream is not None and stream.read() == source.read_bytes(), (
                    f"Source archive contains stale harness: {source.relative_to(ROOT)}"
                )
        fixture_name = str(
            PurePosixPath(pyproject_name).parent / "tests/fixtures/guc-logo-source.png"
        )
        fixture = archive.extractfile(fixture_name)
        assert fixture is not None
        assert fixture.read() == (ROOT / "tests/fixtures/guc-logo-source.png").read_bytes()
        packaged_project = tomllib.loads(
            archive.extractfile(files_by_name[pyproject_name]).read().decode("utf-8")  # type: ignore[union-attr]
        )

    _assert_archive_names_safe(names)
    assert roots <= ALLOWED_SDIST_ROOTS, (
        f"Unapproved sdist roots: {sorted(roots - ALLOWED_SDIST_ROOTS)}"
    )
    assert {"src", "docs", "README.md", "pyproject.toml"} <= roots
    assert packaged_project["project"]["dynamic"] == ["version"]
    assert not any(
        PurePosixPath(name).suffix.casefold() == ".png" and len(PurePosixPath(name).parts) == 2
        for name in names
    )


@pytest.mark.slow
def test_isolated_wheel_install_loads_resources_and_entry_point(tmp_path: Path) -> None:
    wheel = _release_artifact(f"gucmail-{VERSION}-*.whl", kind="wheel")
    venv = tmp_path / "clean-install"
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    # Reuse this test interpreter's dependencies so this release gate stays offline. The
    # application package itself is installed only from the candidate wheel and is then probed
    # from an empty directory with isolated Python startup.
    guarded = os.environ.get("GUCMAIL_AUDIT_GUARDED") == "1"
    if not guarded:
        _run([sys.executable, "-m", "venv", str(venv)], cwd=tmp_path)
    # --system-site-packages exposes the base Python install, not the parent venv.
    # A plain path entry reuses dependencies without executing parent editable .pth files;
    # the wheel installed into this venv remains first on sys.path.
    site_dir = venv / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    # An installed-wheel test runner may itself reuse another environment's
    # dependencies. Plain path entries do not process that environment's .pth
    # files, so retain its resolved site-package directories as well. Never
    # execute editable .pth files or put a source checkout on the inner path.
    dependency_paths = {Path(sysconfig.get_path("purelib")).resolve()}
    dependency_paths.update(
        path.resolve()
        for value in sys.path
        if value and (path := Path(value)).name == "site-packages" and path.is_dir()
    )
    if guarded:
        from qa.offline.install import install_wheel_into_owned_environment

        install_wheel_into_owned_environment(venv, wheel, sorted(dependency_paths))
    else:
        (site_dir / "test-runtime-dependencies.pth").write_text(
            "".join(str(path) + "\n" for path in sorted(dependency_paths)), encoding="utf-8"
        )
    python = _venv_python(venv)
    if not guarded:
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                # The parent runtime may expose the same version's metadata.
                # Install an owned copy without trying to uninstall that parent.
                "--ignore-installed",
                str(wheel.resolve()),
            ],
            cwd=empty_cwd,
        )

    probe = _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import json, gucmail; from importlib.metadata import version as dist_version; "
                "from importlib.resources import files; "
                "pages=list(files('gucmail.help').joinpath('pages').rglob('*.md')); "
                "corpus=json.loads(files('gucmail.data').joinpath('command_corpus.json')."
                "read_text(encoding='utf-8')); "
                "from gucmail.services.keyboard_bindings import keyboard_catalog, "
                "validate_binding_configuration; "
                "from gucmail.services.shortcuts import SHORTCUT_DEFAULTS; "
                "from gucmail.tui.themes import _load_builtin_catalogue; "
                "assert len(_load_builtin_catalogue())==661; "
                "bindings,fixed=keyboard_catalog(); "
                "assert validate_binding_configuration({},SHORTCUT_DEFAULTS)=={}; "
                "print(gucmail.__version__, dist_version('gucmail'), len(pages), "
                "len(corpus['groups']), "
                "sum(len(group['phrases']) for group in corpus['groups']), "
                "len(bindings), len(fixed), gucmail.__file__)"
            ),
        ],
        cwd=empty_cwd,
    )
    (
        version,
        metadata_version,
        page_count,
        group_count,
        phrase_count,
        binding_count,
        fixed_count,
        installed_path,
    ) = probe.stdout.strip().split(" ", 7)
    assert version == VERSION
    assert metadata_version == VERSION
    assert int(page_count) >= 45
    assert int(group_count) >= 70
    assert int(phrase_count) >= 450
    assert int(binding_count) > 0 and int(fixed_count) > 0
    assert str(venv.resolve()).casefold() in str(Path(installed_path).resolve()).casefold()

    executable = _venv_gucmail(venv)
    assert executable.is_file()
    assert VERSION in _run([str(executable), "--version"], cwd=empty_cwd).stdout
    assert "Usage" in _run([str(executable), "--help"], cwd=empty_cwd).stdout
    assert VERSION in _run([str(executable), "--json", "version"], cwd=empty_cwd).stdout
    _run([str(python), "-m", "pip", "check"], cwd=empty_cwd)
