"""Real offline wheel installation without launching an unguarded ensurepip child."""

import os
import sys
import venv
from collections.abc import Iterable
from pathlib import Path


def install_wheel_into_owned_environment(
    environment: Path, wheel: Path, dependency_paths: Iterable[Path]
) -> None:
    from pip._internal.models.scheme import Scheme
    from pip._internal.operations.install.wheel import install_wheel

    venv.EnvBuilder(with_pip=False).create(environment)
    site = environment / (
        "Lib/site-packages"
        if os.name == "nt"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    site.mkdir(parents=True, exist_ok=True)
    # Plain paths reuse installed dependencies, without executing their editable
    # .pth files. The owned wheel remains first in this environment's site path.
    (site / "test-runtime-dependencies.pth").write_text(
        "".join(str(path.resolve()) + "\n" for path in dependency_paths), encoding="utf-8"
    )
    install_wheel(
        "gucmail",
        str(wheel.resolve()),
        Scheme(
            purelib=str(site),
            platlib=str(site),
            headers=str(environment / "Include"),
            scripts=str(scripts),
            data=str(environment),
        ),
        req_description="Owned offline GUCMail artifact qualification",
        pycompile=False,
        warn_script_location=False,
        requested=True,
        script_executable=str(python),
    )
