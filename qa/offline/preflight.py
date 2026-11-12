"""Prove the guard fails closed before beginning the product audit."""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from gucmail.bootstrap import AppContext
from gucmail.config import AppConfig
from gucmail.credentials import CredentialData, CredentialResolver, CredentialStore
from gucmail.ews.adapter import EWSAdapter
from qa.offline.guard import install_from_env
from qa.synthetic.context import synthetic_context


def main():
    guard = install_from_env()
    cases = []

    def blocked(name, action):
        before = len(guard.events)
        with guard.expected_denial():
            try:
                action()
            except Exception as exc:
                if len(guard.events) == before:
                    raise
                cases.append(
                    {
                        "case": name,
                        "passed": True,
                        "prevented": guard.events[-1]["operation"],
                        "surface_error": type(exc).__name__,
                    }
                )
            else:
                raise AssertionError(f"Guard allowed prohibited preflight action: {name}")

    blocked("real credential file", lambda: (guard.repo / "creds.env").read_bytes())
    blocked(
        "credential resolver", lambda: CredentialResolver().resolve(AppConfig(), interactive=False)
    )
    blocked("OS keyring", lambda: CredentialStore().get("audit@example.test"))
    context = synthetic_context(guard.private / "preflight-context")
    blocked("missing injected gateway", lambda: AppContext.gateway(context))
    adapter = EWSAdapter(
        config=AppConfig(),
        credentials=CredentialData("audit@example.test", "audit", "synthetic-only", "test"),
    )
    blocked("real Exchange account construction", lambda: adapter.account)
    import exchangelib

    blocked("direct Exchange construction", lambda: exchangelib.Account())
    blocked("external DNS", lambda: socket.getaddrinfo("audit-blocked.invalid", 443))
    for method in ("connect", "connect_ex"):
        with socket.socket() as sock:
            blocked(
                f"external socket {method}", lambda: getattr(sock, method)(("203.0.113.1", 443))
            )
    blocked("external HTTP", lambda: httpx.get("https://audit-blocked.invalid", trust_env=False))
    blocked("external command", lambda: subprocess.run(["cmd.exe", "/c", "exit", "0"]))
    blocked("unguarded child", lambda: subprocess.run([sys.executable, "-I", "-c", "pass"]))
    blocked("native notification import", lambda: importlib.import_module("plyer"))
    blocked(
        "write original profile",
        lambda: (Path(guard.original_paths["data_dir"]) / "audit-denied.txt").write_text("blocked"),
    )
    blocked("write outside audit", lambda: (guard.repo / "audit-denied.txt").write_text("blocked"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"controlled local stub")

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{server.server_port}", trust_env=False)
        assert response.text == "controlled local stub"
        cases.append({"case": "controlled loopback server", "passed": True})
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
    # A guarded child must inherit the same credential denial, before app imports.
    result = subprocess.run(
        [sys.executable, "-c", "import sys; assert sys._gucmail_offline_guard.ready"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not result.stdout and not result.stderr
    cases.append({"case": "child guard inheritance", "passed": True})
    report = {
        "passed": all(case["passed"] for case in cases),
        "checks": cases,
        "unexpected_blocks": [e for e in guard.events if not e["expected"]],
    }
    assert not report["unexpected_blocks"]
    (guard.output / "safety-preflight.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"preflight_passed": report["passed"], "checks": len(cases)}))


if __name__ == "__main__":
    main()
