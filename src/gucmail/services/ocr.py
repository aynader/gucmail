"""Explicit, bounded OCR of a selected local PNG/JPEG using installed Windows OCR.

No mailbox fetch, automatic indexing, language installation, model download, or
remote service is involved. Recognition is isolated in one owned native child.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from gucmail.errors import ConfigurationError
from gucmail.security import sanitize_terminal

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 2048
MAX_PIXELS = MAX_DIMENSION**2
MAX_OUTPUT_CHARS = 100_000
LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}\Z")
LABEL = "ocr_attachment_text"

# This fixed program receives all operation values as JSON on stdin. Image
# content never becomes PowerShell code. It runs without profiles or scripts
# loaded from user paths and never changes execution policy.
_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$stream = $null
$bitmap = $null
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
    $options = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $languages = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
        ForEach-Object { $_.LanguageTag })
    $maximum = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ($options.mode -eq 'status') {
        @{status='available'; languages=$languages; max_dimension=$maximum} |
            ConvertTo-Json -Compress
        exit 0
    }
    if ($options.mode -ne 'extract') { throw 'Invalid mode' }
    if ($languages -cnotcontains $options.language) {
        @{status='language_unavailable'} | ConvertTo-Json -Compress
        exit 0
    }
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Media.Ocr, ContentType=WindowsRuntime]
    $asyncMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        } | Select-Object -First 1
    function Await-OcrOperation($Operation, [Type] $ResultType) {
        $task = $asyncMethod.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
        if (-not $task.Wait(5000)) { throw 'Operation timed out' }
        return $task.Result
    }
    $operation = [Windows.Storage.StorageFile]::GetFileFromPathAsync([string]$options.path)
    $file = Await-OcrOperation $operation ([Windows.Storage.StorageFile])
    $operation = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
    $stream = Await-OcrOperation $operation ([Windows.Storage.Streams.IRandomAccessStream])
    $operation = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
    $decoder = Await-OcrOperation $operation ([Windows.Graphics.Imaging.BitmapDecoder])
    if ($decoder.PixelWidth -ne $options.width -or
        $decoder.PixelHeight -ne $options.height -or $decoder.PixelWidth -gt 2048 -or
        $decoder.PixelHeight -gt 2048 -or $decoder.PixelWidth -gt $maximum -or
        $decoder.PixelHeight -gt $maximum) {
        @{status='invalid_image'} | ConvertTo-Json -Compress
        exit 0
    }
    $operation = $decoder.GetSoftwareBitmapAsync()
    $bitmap = Await-OcrOperation $operation ([Windows.Graphics.Imaging.SoftwareBitmap])
    $language = New-Object Windows.Globalization.Language($options.language)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -eq $engine) {
        @{status='language_unavailable'} | ConvertTo-Json -Compress
        exit 0
    }
    $operation = $engine.RecognizeAsync($bitmap)
    $result = Await-OcrOperation $operation ([Windows.Media.Ocr.OcrResult])
    $text = $result.Text
    $truncated = $text.Length -gt $options.max_chars
    if ($truncated) { $text = $text.Substring(0, $options.max_chars) }
    @{status='success'; text=$text; truncated=$truncated; width=$decoder.PixelWidth;
        height=$decoder.PixelHeight; language=$options.language} | ConvertTo-Json -Compress
} catch {
    # Native exception details can contain filenames or image text. Return a
    # stable status only; the parent never exposes stderr or raw exceptions.
    @{status='failed'} | ConvertTo-Json -Compress
} finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
"""  # noqa: E501  Windows Runtime assembly-qualified type literals require one line.


class OcrBackend(Protocol):
    def invoke(
        self, options: dict[str, object], *, timeout: float, cancel_event: Event | None
    ) -> dict[str, Any]: ...


class WindowsOcrBackend:
    """One short-lived hidden child with cancellation and a wall-clock bound."""

    def invoke(
        self, options: dict[str, object], *, timeout: float, cancel_event: Event | None
    ) -> dict[str, Any]:
        if sys.platform != "win32":
            return {"status": "unavailable"}
        executable = (
            Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        if not executable.is_file():
            return {"status": "unavailable"}
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled"}
        # Do not pass account, webhook, token, or arbitrary application variables.
        keys = {"systemroot", "windir", "temp", "tmp", "localappdata", "userprofile", "appdata"}
        environment = {key: value for key, value in os.environ.items() if key.casefold() in keys}
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(  # noqa: S603
                [
                    str(executable),
                    "-NoProfile",
                    "-NoLogo",
                    "-NonInteractive",
                    "-Command",
                    _WINDOWS_SCRIPT,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            payload: bytes | None = json.dumps(options).encode("utf-8")
            deadline = time.perf_counter() + timeout
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return {"status": "cancelled"}
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return {"status": "timeout"}
                try:
                    output, _ = process.communicate(payload, timeout=min(remaining, 0.05))
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            maximum = options.get("max_chars", MAX_OUTPUT_CHARS)
            maximum = maximum if isinstance(maximum, int) else MAX_OUTPUT_CHARS
            bound = maximum * 6 + 16_384
            if process.returncode != 0 or len(output) > bound:
                return {"status": "failed"}
            value = json.loads(output.decode("utf-8-sig"))
            return value if isinstance(value, dict) else {"status": "failed"}
        except (OSError, ValueError, UnicodeError):
            return {"status": "failed"}
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)
                for handle in (process.stdin, process.stdout):
                    if handle is not None:
                        handle.close()


@dataclass(frozen=True, slots=True)
class OcrPlan:
    path: Path = field(repr=False)
    language: str
    sha256: str
    format: str
    input_bytes: int
    width: int
    height: int
    can_recognize: bool
    timeout_seconds: float = 8.0
    max_output_chars: int = 50_000

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["path"] = str(self.path)
        data.update(
            apply=False,
            label=LABEL,
            privacy=(
                "Recognizes this one local image in an owned process. No mailbox fetch, "
                "remote service, language download, or automatic indexing. "
                "A temporary private image copy is removed afterward."
            ),
            accuracy=(
                "OCR can be inaccurate. Review recognized text; this engine does not "
                "supply a confidence score."
            ),
            bounds=(
                "PNG/JPEG only; 8 MiB input; 2048 pixels per dimension; at most "
                "4,194,304 pixels; first image only. Timeout/output limits apply. "
                "No OS memory quota is claimed."
            ),
        )
        return data


@dataclass(frozen=True, slots=True)
class OcrResult:
    status: str
    language: str
    sha256: str
    text: str = field(default="", repr=False)
    truncated: bool = False
    label: str = LABEL
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class OcrService:
    def __init__(self, *, backend: OcrBackend | None = None) -> None:
        self.backend = backend or WindowsOcrBackend()

    def _invoke(
        self, options: dict[str, object], *, timeout: float, cancel_event: Event | None
    ) -> dict[str, Any]:
        try:
            value = self.backend.invoke(options, timeout=timeout, cancel_event=cancel_event)
            return value if isinstance(value, dict) else {"status": "failed"}
        except Exception:
            return {"status": "failed"}

    def status(self, *, cancel_event: Event | None = None) -> dict[str, object]:
        value = self._invoke({"mode": "status"}, timeout=8, cancel_event=cancel_event)
        languages = value.get("languages", [])
        if not isinstance(languages, list) or len(languages) > 512:
            languages = []
        languages = sorted(
            item for item in languages if isinstance(item, str) and LANGUAGE_TAG.fullmatch(item)
        )
        available = value.get("status") == "available" and bool(languages)
        worker_status = value.get("status")
        if not isinstance(worker_status, str) or worker_status not in {
            "available",
            "unavailable",
            "failed",
            "timeout",
            "cancelled",
        }:
            worker_status = "failed"
        engine_limit = value.get("max_dimension")
        engine_limit = (
            engine_limit if isinstance(engine_limit, int) and 1 <= engine_limit <= 16384 else None
        )
        return {
            "engine": "windows-installed-ocr",
            "available": available,
            "languages": languages,
            "status": worker_status,
            "engine_max_dimension": engine_limit,
            "enabled_by_default": False,
            "remote_transmission": False,
            "automatic_indexing": False,
            "max_input_bytes": MAX_INPUT_BYTES,
            "max_dimension": MAX_DIMENSION,
            "max_pixels": MAX_PIXELS,
            "max_output_chars": MAX_OUTPUT_CHARS,
            "language_installation": (
                "Only already installed Windows recognizer languages are used. "
                "No installation or download is attempted."
            ),
            "platform_limit": (
                "Recognition requires Windows and its installed OCR runtime. "
                "Ordinary text extraction remains independent."
            ),
        }

    def preview(
        self,
        path: Path,
        *,
        language: str,
        timeout_seconds: float = 8,
        max_output_chars: int = 50_000,
        cancel_event: Event | None = None,
    ) -> OcrPlan:
        if not isinstance(language, str) or not LANGUAGE_TAG.fullmatch(language):
            raise ConfigurationError("Choose an installed OCR language tag such as en-US.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= 15
        ):
            raise ConfigurationError("OCR timeout must be between 1 and 15 seconds.")
        if (
            isinstance(max_output_chars, bool)
            or not isinstance(max_output_chars, int)
            or not 1 <= max_output_chars <= MAX_OUTPUT_CHARS
        ):
            raise ConfigurationError("OCR output must be between 1 and 100,000 characters.")
        source = _local_path(path)
        content = _read_image(source)
        format_name, width, height = _dimensions(content)
        _check_dimensions(width, height)
        status = self.status(cancel_event=cancel_event)
        languages = status["languages"]
        assert isinstance(languages, list)
        engine_limit = status["engine_max_dimension"]
        engine_limit = engine_limit if isinstance(engine_limit, int) else MAX_DIMENSION
        return OcrPlan(
            source,
            language,
            hashlib.sha256(content).hexdigest(),
            format_name,
            len(content),
            width,
            height,
            bool(status["available"])
            and language in languages
            and width <= engine_limit
            and height <= engine_limit,
            float(timeout_seconds),
            max_output_chars,
        )

    def extract(
        self, plan: OcrPlan, *, enabled: bool = False, cancel_event: Event | None = None
    ) -> OcrResult:
        if not enabled:
            return OcrResult(
                "disabled",
                plan.language,
                plan.sha256,
                error="OCR requires an explicit operation opt-in.",
            )
        if cancel_event is not None and cancel_event.is_set():
            return OcrResult(
                "cancelled", plan.language, plan.sha256, error="Local OCR was cancelled."
            )
        # Rebuild the preview so language/runtime/source changes cannot reuse old consent.
        current = self.preview(
            plan.path,
            language=plan.language,
            timeout_seconds=plan.timeout_seconds,
            max_output_chars=plan.max_output_chars,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return OcrResult(
                "cancelled", plan.language, plan.sha256, error="Local OCR was cancelled."
            )
        if current != plan:
            raise ConfigurationError(
                "The image or OCR language availability changed. Preview again before recognition."
            )
        if not current.can_recognize:
            return OcrResult(
                "unavailable",
                plan.language,
                plan.sha256,
                error=(
                    "The selected installed OCR language or Windows engine is unavailable. "
                    "Run ocr status."
                ),
            )
        content = _read_image(plan.path)
        if hashlib.sha256(content).hexdigest() != plan.sha256:
            raise ConfigurationError("The image changed after preview. Preview again.")
        descriptor, filename = tempfile.mkstemp(prefix="gucmail-ocr-", suffix="." + plan.format)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            value = self._invoke(
                {
                    "mode": "extract",
                    "path": filename,
                    "language": plan.language,
                    "width": plan.width,
                    "height": plan.height,
                    "max_chars": plan.max_output_chars,
                },
                timeout=plan.timeout_seconds,
                cancel_event=cancel_event,
            )
        finally:
            Path(filename).unlink(missing_ok=True)
        outcome = value.get("status")
        if outcome == "success":
            text = value.get("text")
            if (
                not isinstance(text, str)
                or len(text) > plan.max_output_chars
                or value.get("language") != plan.language
                or value.get("width") != plan.width
                or value.get("height") != plan.height
                or not isinstance(value.get("truncated"), bool)
            ):
                return OcrResult(
                    "failed",
                    plan.language,
                    plan.sha256,
                    error="The OCR worker returned an invalid bounded result.",
                )
            return OcrResult(
                "success",
                plan.language,
                plan.sha256,
                sanitize_terminal(text, max_length=plan.max_output_chars),
                value["truncated"],
            )
        errors = {
            "cancelled": "Local OCR was cancelled; its owned worker was stopped.",
            "timeout": "Local OCR exceeded its time limit; its owned worker was stopped.",
            "language_unavailable": (
                "The selected installed OCR language is unavailable. Run ocr status."
            ),
            "invalid_image": (
                "The local image could not be decoded within the reviewed pixel limits."
            ),
        }
        status_name = outcome if isinstance(outcome, str) and outcome in errors else "failed"
        return OcrResult(
            status_name,
            plan.language,
            plan.sha256,
            error=errors.get(
                status_name,
                "Local OCR failed safely. No mailbox or index change occurred; "
                "inspect the image and run ocr status before retrying.",
            ),
        )


def _local_path(path: Path) -> Path:
    if str(path).startswith(("\\\\", "//")) or "://" in str(path):
        raise ConfigurationError(
            "OCR accepts a selected local file, without network paths or URLs."
        )
    if path.suffix.casefold() not in {".png", ".jpg", ".jpeg"}:
        raise ConfigurationError("Local OCR supports PNG and JPEG images only.")
    if _remote_drive(path):
        raise ConfigurationError("Select a local image without a mapped network drive.")
    try:
        for item in (path, *path.parents):
            if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                raise ConfigurationError(
                    "Select an ordinary local image without symbolic links or reparse points."
                )
        source = path.resolve(strict=True)
        if not source.is_file():
            raise ConfigurationError("Select an ordinary local image file.")
    except OSError as exc:
        raise ConfigurationError("The selected local image is unavailable.") from exc
    return source


def _remote_drive(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    try:
        get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
        get_drive_type.argtypes = (ctypes.c_wchar_p,)
        get_drive_type.restype = ctypes.c_uint
        return int(get_drive_type(str(path.absolute().anchor))) == 4  # DRIVE_REMOTE
    except OSError as exc:
        raise ConfigurationError("The selected image drive could not be checked.") from exc


def _read_image(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise ConfigurationError("The selected local image could not be read.") from exc
    if len(content) > MAX_INPUT_BYTES:
        raise ConfigurationError("The image exceeds the 8 MiB local OCR input limit.")
    return content


def _check_dimensions(width: int, height: int) -> None:
    if (
        not 1 <= width <= MAX_DIMENSION
        or not 1 <= height <= MAX_DIMENSION
        or width * height > MAX_PIXELS
    ):
        raise ConfigurationError(
            "The image exceeds local OCR pixel limits (2048 per dimension, 4,194,304 pixels)."
        )


def _dimensions(content: bytes) -> tuple[str, int, int]:
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 33:
        if content[8:16] != b"\x00\x00\x00\rIHDR" or zlib.crc32(content[12:29]) != int.from_bytes(
            content[29:33], "big"
        ):
            raise ConfigurationError("The PNG image header is invalid.")
        width, height = struct.unpack(">II", content[16:24])
        return "png", width, height
    if content.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(content):
            if content[offset] != 255:
                break
            while offset < len(content) and content[offset] == 255:
                offset += 1
            if offset >= len(content):
                break
            marker = content[offset]
            offset += 1
            if marker in {0, 0xD9, 0xDA}:
                break
            if marker in {0x01, *range(0xD0, 0xD9)}:
                continue
            if offset + 2 > len(content):
                break
            length = int.from_bytes(content[offset : offset + 2], "big")
            if length < 2 or offset + length > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                height, width = struct.unpack(">HH", content[offset + 3 : offset + 7])
                return "jpg", width, height
            offset += length
    raise ConfigurationError("The image lacks a supported, valid PNG/JPEG dimension header.")
