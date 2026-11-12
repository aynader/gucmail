"""Process-wide audit guards installed before loading application entry points.

This is an execution harness, not a claim that application offline mode already
enforces these boundaries. Child Python processes must load the same guard.
"""

from __future__ import annotations

import contextlib
import importlib.abc
import ipaddress
import json
import os
import platform
import shlex
import socket
import sys
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class OfflineViolation(RuntimeError):
    """A prohibited operation was stopped before it reached its destination."""


class Guard:
    def __init__(self, private: Path, output: Path, *, fixture_credentials: bool = False):
        self.repo = Path(__file__).resolve().parents[2]
        self.private = private.resolve()
        self.output = output.resolve()
        self.fixture_credentials = fixture_credentials
        self.events: list[dict[str, Any]] = []
        self._local = threading.local()
        self._lock = threading.RLock()
        self.original_paths: dict[str, str] = {}
        self.cache_read_paths: set[Path] = set()
        self.ready = False
        self.private.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        self.log = self.private / f"guard-{os.getpid()}.jsonl"
        self.protected = (
            Path.home() / "AppData/Roaming/GUC Mail CLI",
            Path.home() / "AppData/Local/GUC Mail CLI",
        )

    @staticmethod
    def inside(path: Path, root: Path) -> bool:
        # Windows cleanup APIs may spell an already-resolved local path as
        # \\?\C:\... . Compare the same canonical drive path without permitting
        # device namespaces or changing resolved symlink/reparse-point targets.
        def local_drive(value: Path) -> Path:
            text = str(value)
            if (
                os.name == "nt"
                and text.startswith("\\\\?\\")
                and len(text) > 6
                and text[4].isalpha()
                and text[5:7] == ":\\"
            ):
                return Path(text[4:])
            return value

        path, root = local_drive(path), local_drive(root)
        return path == root or root in path.parents

    def deny(self, operation: str) -> None:
        event = {
            "time": datetime.now(UTC).isoformat(),
            "run_id": os.environ.get("GUCMAIL_AUDIT_RUN_ID"),
            "test": os.environ.get("GUCMAIL_AUDIT_ACTIVE_TEST"),
            "operation": operation,
            "expected": bool(getattr(self._local, "expected", False)),
            "caller": [
                f"{Path(frame.filename).name}:{frame.lineno}"
                for frame in traceback.extract_stack(limit=9)[:-1]
            ],
        }
        with self._lock:
            self.events.append(event)
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
        raise OfflineViolation(f"Offline audit blocked: {operation}")

    def run_events(self) -> list[dict[str, Any]]:
        """Include owned child denials, even when their callers catch failures."""
        run_id = os.environ.get("GUCMAIL_AUDIT_RUN_ID")
        if not run_id:
            return list(self.events)
        events = []
        for path in self.private.glob("guard-*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("run_id") == run_id:
                    events.append(event)
        return events

    @contextlib.contextmanager
    def expected_denial(self):
        previous = getattr(self._local, "expected", False)
        self._local.expected = True
        try:
            yield
        finally:
            self._local.expected = previous

    @staticmethod
    def loopback(host: Any) -> bool:
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="replace")
        if host in {None, "", "localhost", "localhost."}:
            return True
        try:
            return ipaddress.ip_address(str(host)).is_loopback
        except ValueError:
            return False

    def audit_hook(self, event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            name, _mode, flags = args
            if isinstance(name, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(name)).resolve()
                if str(path).rstrip("\\").casefold() in {"\\\\.\\nul", "\\\\?\\nul"}:
                    return
                private = self.inside(path, self.private)
                write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                if path.name.lower() in {"creds.env", ".env"} and not private:
                    self.deny("real_credential_file")
                if path.name.lower().endswith(".credentials.env") and not private:
                    self.deny("real_credential_file")
                if (
                    any(self.inside(path, root) for root in self.protected)
                    and not private
                    and (write or path not in self.cache_read_paths)
                ):
                    self.deny("original_profile_access")
                # Writing to console handles does not use filesystem paths here.
                if (
                    write
                    and not private
                    and not self.inside(path, self.output)
                    and path.name.upper() != "NUL"
                ):
                    self.deny("write_outside_audit")
        elif event in {"socket.connect", "socket.bind", "socket.sendto"}:
            address = args[1]
            if not isinstance(address, tuple) or not self.loopback(address[0]):
                self.deny("external_socket")
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            if not self.loopback(args[0]):
                self.deny("external_dns")
        elif event in {"os.system", "os.startfile", "os.startfile/2", "os.posix_spawn"}:
            self.deny("external_command")
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.rename"}:
            targets = args[:2] if event == "os.rename" else args[:1]
            for name in targets:
                if isinstance(name, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(name)).resolve()
                    if not self.inside(path, self.private) and not self.inside(path, self.output):
                        self.deny("filesystem_mutation_outside_audit")
        elif event in {
            "winreg.SetValue",
            "winreg.DeleteValue",
            "winreg.CreateKey",
            "winreg.DeleteKey",
        }:
            self.deny("registry_mutation")
        elif event == "subprocess.Popen":
            executable, argv, _cwd, env = args
            child_args = (
                list(argv) if isinstance(argv, (list, tuple)) else shlex.split(argv, posix=False)
            )
            program = executable or (child_args[0].strip('"') if child_args else "")
            candidate = Path(os.fsdecode(program)).resolve()
            allowed = candidate == Path(sys.executable).resolve() or (
                self.inside(candidate, self.private) and candidate.name.lower().startswith("python")
            )
            child_env = os.environ if env is None else env
            bypass = any(str(arg) in {"-I", "-S", "-E"} for arg in child_args)
            guarded = child_env.get("GUCMAIL_AUDIT_GUARDED") == "1" and (
                str(self.private / "bootstrap") in child_env.get("PYTHONPATH", "")
            )
            if not allowed or bypass or not guarded:
                self.deny("unguarded_child_process")
        elif event == "sqlite3.connect":
            database = os.fspath(args[0])
            if database == ":memory:":
                return
            if database.startswith("file:"):
                from urllib.parse import unquote, urlsplit

                parsed = urlsplit(database)
                if parsed.netloc not in {"", "localhost"}:
                    self.deny("database_outside_audit")
                database = unquote(parsed.path)
                if (
                    os.name == "nt"
                    and len(database) >= 3
                    and database[0] == "/"
                    and database[2] == ":"
                ):
                    database = database[1:]
            path = Path(database).resolve()
            if not self.inside(path, self.private) and not self.inside(path, self.output):
                self.deny("database_outside_audit")

    def install(self) -> None:
        if os.environ.get("GUCMAIL_AUDIT_IMPORT_PROBE") == "settings":
            self._install_settings_import_probe()
            return
        inherited = os.environ.get("GUCMAIL_AUDIT_GUARDED") == "1"
        sys.dont_write_bytecode = True
        temporary = self.private / "temp"
        temporary.mkdir(exist_ok=True)
        os.environ["TMP"] = os.environ["TEMP"] = str(temporary)
        import tempfile

        tempfile.tempdir = str(temporary)
        sys.addaudithook(self.audit_hook)
        # platform.uname() otherwise shells out to `ver` on this Windows Python.
        # Use the real kernel version without launching an external command.
        if sys.platform == "win32":
            version = sys.getwindowsversion()
            platform._syscmd_ver = lambda *a, **k: (
                "Windows",
                str(version.major),
                f"{version.major}.{version.minor}.{version.build}",
            )
        original_addresses = socket.getaddrinfo

        def numeric_addresses(host, port, family=0, type=0, proto=0, flags=0):
            if not self.loopback(host):
                self.deny("external_dns")
            if host in {None, "", "localhost", "localhost."}:
                host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
            return original_addresses(
                host, port, family, type, proto, flags | socket.AI_NUMERICHOST
            )

        def local_name(host):
            if not self.loopback(host):
                self.deny("external_dns")
            return "127.0.0.1"

        def local_reverse(host):
            if not self.loopback(host):
                self.deny("external_dns")
            return ("localhost", [], [host])

        socket.getaddrinfo = numeric_addresses
        socket.gethostbyname = local_name
        socket.gethostbyaddr = local_reverse
        # Discovery constructs paths; it does not open config or credentials.
        from gucmail.config import AppPaths

        original = AppPaths.discover()
        self.original_paths = {
            key: str(getattr(original, key))
            for key in ("config_file", "data_dir", "cache_dir", "state_database", "index_database")
        }
        retained_paths = {}
        for key in tuple(os.environ):
            if key.startswith("GUCMAIL_") and not key.startswith("GUCMAIL_AUDIT_"):
                if inherited and (key.endswith("_DIR") or key == "GUCMAIL_CONFIG"):
                    value = Path(os.environ[key]).resolve()
                    if not self.inside(value, self.private):
                        self.deny("unguarded_child_profile")
                    retained_paths[key] = str(value)
                del os.environ[key]
        home = self.private / "profile"
        os.environ.update(
            {
                "GUCMAIL_AUDIT_GUARDED": "1",
                "GUCMAIL_AUDIT_PRIVATE": str(self.private),
                "GUCMAIL_AUDIT_OUTPUT": str(self.output),
                "GUCMAIL_AUDIT_FIXTURES": "1" if self.fixture_credentials else "0",
                "GUCMAIL_CONFIG_DIR": str(home / "config"),
                "GUCMAIL_DATA_DIR": str(home / "data"),
                "GUCMAIL_CACHE_DIR": str(home / "cache"),
                "GUCMAIL_LOG_DIR": str(home / "logs"),
                "COVERAGE_FILE": str(self.private / ".coverage"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        os.environ.update(retained_paths)
        bootstrap = self.private / "bootstrap"
        bootstrap.mkdir(exist_ok=True)
        (bootstrap / "sitecustomize.py").write_text(
            "import os\ntry:\n"
            "    from qa.offline.guard import install_from_env\n"
            "    install_from_env()\nexcept BaseException as exc:\n"
            "    os.write(2, ('OFFLINE_GUARD_BOOTSTRAP_FAILED ' + "
            "type(exc).__name__ + ' ' + str(getattr(exc, 'name', '') or '') + "
            "'\\n').encode('ascii', errors='replace'))\n"
            "    os._exit(97)\n",
            encoding="utf-8",
        )
        os.environ["PYTHONPATH"] = os.pathsep.join(
            (str(bootstrap), str(self.repo), str(self.repo / "src"))
        )
        import gucmail.bootstrap as composition
        import gucmail.credentials as credentials
        import gucmail.ews.adapter as adapter

        original_resolve = credentials.CredentialResolver.resolve
        original_gateway = composition.AppContext.gateway
        real_adapter_type = adapter.EWSAdapter

        def resolve(resolver, *args, **kwargs):
            if not self.fixture_credentials:
                self.deny("credential_resolution")
            # Fixture mode can use explicit fake values or fake stores. The real
            # keyring and nonisolated legacy files remain blocked independently.
            return original_resolve(resolver, *args, **kwargs)

        def keyring(_store):
            self.deny("os_keyring")

        def gateway(context, *args, **kwargs):
            if context._gateway is None:
                if self.fixture_credentials and adapter.EWSAdapter is not real_adapter_type:
                    return original_gateway(context, *args, **kwargs)
                self.deny("real_gateway_initialization")
            return context._gateway

        original_create = adapter.EWSAdapter._create_account

        def account(instance):
            if instance._account_factory is None:
                self.deny("real_exchange_account")
            return original_create(instance)

        credentials.CredentialResolver.resolve = resolve
        credentials.CredentialStore._keyring = keyring
        composition.AppContext.gateway = gateway
        adapter.EWSAdapter._create_account = account
        # Direct library account creation must not bypass the application guard.
        import exchangelib

        original_account_init = exchangelib.Account.__init__

        def real_account(instance, *args, **kwargs):
            config = kwargs.get("config")
            endpoint = str(getattr(config, "service_endpoint", ""))
            host = urlsplit(endpoint).hostname or ""
            identity = str(kwargs.get("primary_smtp_address", args[0] if args else ""))
            synthetic = host.endswith((".test", ".invalid")) and identity.endswith(
                (".test", ".invalid")
            )
            fixed_schema = getattr(config, "auth_type", None) and getattr(config, "version", None)
            if (
                self.fixture_credentials
                and synthetic
                and fixed_schema
                and not kwargs.get("autodiscover")
            ):
                return original_account_init(instance, *args, **kwargs)
            self.deny("direct_exchange_account")

        exchangelib.Account.__init__ = real_account
        guard = self

        class NoNativeCredentialsOrDelivery(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in {
                    "keyring",
                    "plyer",
                    "windows_toasts",
                    "winrt",
                    "winsdk",
                    "win32com",
                    "win32cred",
                    "winshell",
                }:
                    guard.deny("native_credentials_or_delivery")
                return None

        sys.meta_path.insert(0, NoNativeCredentialsOrDelivery())
        self._block_desktop_delivery()
        self.ready = True

    def _install_settings_import_probe(self) -> None:
        """Stricter core-only child, so Settings can actually be imported first.

        The ordinary guard preloads application services to patch factories. This
        probe instead denies the entire credential/provider/integration import
        surfaces, while retaining filesystem, socket, database and process hooks.
        It cannot run a mailbox application or fall back to an unpatched factory.
        """
        sys.dont_write_bytecode = True
        sys.addaudithook(self.audit_hook)
        for key in tuple(os.environ):
            if key.startswith("GUCMAIL_") and not key.startswith("GUCMAIL_AUDIT_"):
                del os.environ[key]
        profile = self.private / "import-probe-profile"
        os.environ.update(
            {
                "GUCMAIL_CONFIG_DIR": str(profile / "config"),
                "GUCMAIL_DATA_DIR": str(profile / "data"),
                "GUCMAIL_CACHE_DIR": str(profile / "cache"),
                "GUCMAIL_LOG_DIR": str(profile / "logs"),
            }
        )
        guard = self

        class NoProviders(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                denied = (
                    "gucmail.credentials",
                    "gucmail.bootstrap",
                    "gucmail.ews",
                    "gucmail.headless",
                    "gucmail.notification_providers",
                    "gucmail.services.windows_desktop",
                    "exchangelib",
                    "keyring",
                    "plyer",
                    "windows_toasts",
                    "winrt",
                    "winsdk",
                    "win32com",
                    "win32cred",
                )
                if any(
                    fullname == prefix or fullname.startswith(prefix + ".") for prefix in denied
                ):
                    guard.deny("provider_import_during_cold_settings_probe")
                return None

        sys.meta_path.insert(0, NoProviders())
        self.ready = True

    def _block_desktop_delivery(self):
        # Native desktop notification calls are side effects even without HTTP.
        from gucmail.notification_providers import core, windows

        for module in (core, windows):
            for name in (
                "WindowsDesktopProvider",
                "DesktopNotificationProvider",
                "DesktopProvider",
            ):
                provider = getattr(module, name, None)
                if provider is not None:
                    provider.deliver = lambda *a, **k: self.deny("desktop_delivery")


def install_from_env() -> Guard:
    existing = getattr(sys, "_gucmail_offline_guard", None)
    if existing is not None:
        return existing
    private = Path(os.environ["GUCMAIL_AUDIT_PRIVATE"])
    output = Path(os.environ["GUCMAIL_AUDIT_OUTPUT"])
    guard = Guard(
        private, output, fixture_credentials=os.environ.get("GUCMAIL_AUDIT_FIXTURES") == "1"
    )
    sys._gucmail_offline_guard = guard
    guard.install()
    return guard
