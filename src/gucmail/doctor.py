"""Safe diagnostics with explicit network and authentication escalation."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import socket
import ssl
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from gucmail.bootstrap import AppContext
from gucmail.errors import ConflictError
from gucmail.security import redact_text


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: str
    detail: str
    live_request: bool = False


PACKAGES = ("exchangelib", "typer", "rich", "textual", "platformdirs", "keyring", "httpx")


def run_doctor(
    context: AppContext,
    *,
    network: bool = False,
    authenticate: bool = False,
) -> list[CheckResult]:
    results = [
        CheckResult(
            "python", "ok", f"{platform.python_implementation()} {platform.python_version()}"
        ),
        CheckResult("platform", "ok", platform.platform()),
    ]
    for package in PACKAGES:
        try:
            version = importlib.metadata.version(package)
            results.append(CheckResult(f"package:{package}", "ok", version))
        except importlib.metadata.PackageNotFoundError:
            results.append(CheckResult(f"package:{package}", "error", "not installed"))
    try:
        context.config.validate()
        results.append(CheckResult("configuration", "ok", str(context.paths.config_file)))
    except Exception as exc:
        results.append(CheckResult("configuration", "error", redact_text(exc)))
    for label, directory in (
        ("config_directory", context.paths.config_dir),
        ("data_directory", context.paths.data_dir),
        ("cache_directory", context.paths.cache_dir),
        ("log_directory", context.paths.log_dir),
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, delete=True):
                pass
            results.append(CheckResult(label, "ok", "writable"))
        except OSError as exc:
            results.append(CheckResult(label, "error", redact_text(exc)))
    try:
        health = context.state.health()
        results.append(
            CheckResult(
                "state_database",
                "ok" if health["integrity"] == "ok" else "error",
                json.dumps(health, sort_keys=True),
            )
        )
    except Exception as exc:
        results.append(CheckResult("state_database", "error", redact_text(exc)))

    if network:
        parsed = urlparse(context.config.endpoint)
        host = parsed.hostname or ""
        port = parsed.port or 443
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port)})
            results.append(CheckResult("dns", "ok", f"{len(addresses)} address(es) resolved", True))
        except OSError as exc:
            results.append(CheckResult("dns", "error", redact_text(exc), True))
        try:
            ssl_context = ssl.create_default_context()
            with (
                socket.create_connection((host, port), timeout=5) as raw,
                ssl_context.wrap_socket(raw, server_hostname=host) as secure,
            ):
                certificate = cast(dict[str, Any], secure.getpeercert() or {})
            subject = certificate.get("subject", ())
            names = {
                str(key): str(value) for relative_name in subject for key, value in relative_name
            }
            common_name = names.get("commonName", "certificate verified")
            results.append(CheckResult("tls", "ok", common_name, True))
        except (OSError, ssl.SSLError) as exc:
            results.append(CheckResult("tls", "error", redact_text(exc), True))
    else:
        results.append(CheckResult("network", "skipped", "Use --network to test DNS and TLS."))

    if authenticate:
        try:
            status = context.gateway(interactive=True).status()
            results.append(
                CheckResult(
                    "authentication",
                    "ok",
                    "Exchange "
                    f"{status.get('exchange_version', 'version detected')}; "
                    "bounded Inbox metadata read",
                    True,
                )
            )
        except Exception as exc:
            results.append(CheckResult("authentication", "error", redact_text(exc), True))
    else:
        results.append(
            CheckResult("authentication", "skipped", "Authentication was not requested.")
        )
    return results


def write_diagnostic_bundle(
    context: AppContext,
    destination: Path,
    *,
    results: list[CheckResult],
) -> Path:
    destination = destination.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ConflictError(f"Diagnostic destination already exists: {destination}")
    payload = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "configuration": context.config.redacted_dict(),
        "paths": {
            "config": str(context.paths.config_file),
            "data": str(context.paths.data_dir),
            "cache": str(context.paths.cache_dir),
        },
        "checks": [asdict(result) for result in results],
        "notice": "Sanitized diagnostics only; no credentials, message bodies, or attachment data.",
    }
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.write("\n")
    return destination
