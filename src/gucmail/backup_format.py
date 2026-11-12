"""Versioned, self-describing on-disk format for GUC Mail backups.

The format module deliberately has no Exchange dependency.  It contains the
strict JSON codecs, atomic-file helpers and reviewed cryptographic building
blocks used by :mod:`gucmail.services.backup`.

Format version 1 stores a manifest next to immutable, content-addressed
objects.  SHA-256 protects every logical object and every stored byte stream.
When encryption is enabled, object bytes use AES-256-GCM and the manifest uses
HMAC-SHA-256.  Independent keys are derived from a user supplied passphrase by
Scrypt; the passphrase and derived keys are never serialized.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from gucmail.errors import BackupIntegrityError

FORMAT_NAME = "gucmail-backup"
FORMAT_VERSION = 1
BACKUP_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
INCOMPLETE_MANIFEST_FILENAME = "manifest.incomplete.json"
OBJECT_DIRECTORY = "objects"
PRIVACY_NOTICE = "This backup contains private mailbox data. Protect it like the original mailbox."

_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_MANIFEST_BYTES = 128 * 1024 * 1024
_DEFAULT_SCRYPT_N = 2**14
_DEFAULT_SCRYPT_R = 8
_DEFAULT_SCRYPT_P = 1
_MAX_SCRYPT_MEMORY = 512 * 1024 * 1024


def utc_text(value: Any) -> str:
    """Return an ISO-8601 UTC string for a datetime-like value."""

    from datetime import UTC, datetime

    current = value if isinstance(value, datetime) else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize deterministic UTF-8 JSON, rejecting NaN and similar values."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BackupIntegrityError(f"Backup metadata is not valid canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_backup_id(value: str) -> str:
    if not _BACKUP_ID_RE.fullmatch(value):
        raise BackupIntegrityError("Backup identifier contains unsafe characters.")
    return value


def validate_digest(value: str, *, label: str = "digest") -> str:
    if not _HEX_SHA256_RE.fullmatch(value):
        raise BackupIntegrityError(f"Backup {label} is not a lowercase SHA-256 value.")
    return value


def object_relative_path(content_id: str) -> str:
    """Map a content identifier to a portable two-level relative path."""

    digest = validate_digest(content_id, label="content identifier")
    return f"{OBJECT_DIRECTORY}/{digest[:2]}/{digest[2:4]}/{digest}.gmo"


def resolve_relative_path(root: Path, relative: str) -> Path:
    """Resolve a manifest path without permitting absolute/traversal paths."""

    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise BackupIntegrityError("Backup manifest contains an unsafe relative path.")
    base = root.resolve()
    candidate = base.joinpath(*pure.parts).resolve()
    if candidate == base or base not in candidate.parents:
        raise BackupIntegrityError("Backup manifest path escapes the backup directory.")
    return candidate


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is not supported by every filesystem.  File fsync and
        # same-directory replacement still provide the strongest portable path.
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Durably replace ``path`` from a same-directory temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            with _suppress_os_error():
                os.chmod(temporary_name, mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        _fsync_directory(path.parent)
    finally:
        if temporary_name is not None:
            with _suppress_os_error():
                Path(temporary_name).unlink()


class _suppress_os_error:
    """Tiny local context manager to avoid importing contextlib on hot paths."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, error_type: Any, error: Any, traceback: Any) -> bool:
        return error_type is not None and issubclass(error_type, OSError)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_bytes(path, canonical_json_bytes(dict(payload)) + b"\n")


def load_json(path: Path, *, maximum_bytes: int = _MAX_MANIFEST_BYTES) -> dict[str, Any]:
    if path.is_symlink():
        raise BackupIntegrityError(f"Refusing symbolic-link backup metadata: {path.name}")
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise BackupIntegrityError(f"Backup metadata is too large ({size} bytes).")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except BackupIntegrityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError(f"Could not read backup metadata {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise BackupIntegrityError("Backup metadata root must be a JSON object.")
    return value


@dataclass(frozen=True, slots=True)
class KeyMaterial:
    """Ephemeral key material; repr is deliberately suppressed."""

    encryption_key: bytes = field(repr=False)
    manifest_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ObjectDescriptor:
    content_id: str
    sha256: str
    stored_sha256: str
    path: str
    kind: str
    media_type: str
    logical_size: int
    stored_size: int
    compression: str = "none"
    encryption: str = "none"
    nonce: str | None = None

    def __post_init__(self) -> None:
        validate_digest(self.content_id, label="content identifier")
        validate_digest(self.sha256)
        validate_digest(self.stored_sha256, label="stored digest")
        if self.path != object_relative_path(self.content_id):
            raise BackupIntegrityError("Backup object path does not match its content identifier.")
        if not self.kind or not self.media_type:
            raise BackupIntegrityError("Backup object kind and media type are required.")
        if self.logical_size < 0 or self.stored_size < 0:
            raise BackupIntegrityError("Backup object sizes cannot be negative.")
        if self.compression not in {"none", "zlib"}:
            raise BackupIntegrityError(f"Unsupported backup compression: {self.compression}")
        if self.encryption not in {"none", "AES-256-GCM"}:
            raise BackupIntegrityError(f"Unsupported backup encryption: {self.encryption}")
        if self.encryption == "AES-256-GCM":
            nonce = decode_b64(self.nonce, label="object nonce")
            if len(nonce) != 12:
                raise BackupIntegrityError("AES-GCM backup nonces must be 12 bytes.")
        elif self.nonce is not None:
            raise BackupIntegrityError("Unencrypted backup objects cannot contain a nonce.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ObjectDescriptor:
        required = {
            "content_id",
            "sha256",
            "stored_sha256",
            "path",
            "kind",
            "media_type",
            "logical_size",
            "stored_size",
        }
        _require_fields(value, required, "object descriptor")
        try:
            return cls(
                content_id=str(value["content_id"]),
                sha256=str(value["sha256"]),
                stored_sha256=str(value["stored_sha256"]),
                path=str(value["path"]),
                kind=str(value["kind"]),
                media_type=str(value["media_type"]),
                logical_size=int(value["logical_size"]),
                stored_size=int(value["stored_size"]),
                compression=str(value.get("compression", "none")),
                encryption=str(value.get("encryption", "none")),
                nonce=str(value["nonce"]) if value.get("nonce") is not None else None,
            )
        except (TypeError, ValueError) as exc:
            raise BackupIntegrityError(f"Invalid backup object descriptor: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ItemDescriptor:
    item_key: str
    kind: str
    record_object: str
    folder_path: str | None = None
    deleted: bool = False
    dependency_objects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_digest(self.item_key, label="item key")
        validate_digest(self.record_object, label="record object identifier")
        for dependency in self.dependency_objects:
            validate_digest(dependency, label="dependency object identifier")
        if not self.kind:
            raise BackupIntegrityError("Backup item kind cannot be empty.")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["dependency_objects"] = list(self.dependency_objects)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ItemDescriptor:
        _require_fields(value, {"item_key", "kind", "record_object"}, "item descriptor")
        dependencies = value.get("dependency_objects", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise BackupIntegrityError("Backup item dependencies must be a list of identifiers.")
        folder = value.get("folder_path")
        if folder is not None and not isinstance(folder, str):
            raise BackupIntegrityError("Backup item folder path must be text or null.")
        return cls(
            item_key=str(value["item_key"]),
            kind=str(value["kind"]),
            record_object=str(value["record_object"]),
            folder_path=folder,
            deleted=bool(value.get("deleted", False)),
            dependency_objects=tuple(dependencies),
        )


@dataclass(frozen=True, slots=True)
class BackupManifest:
    backup_id: str
    application_version: str
    kind: str
    state: str
    created_at: str
    completed_at: str | None
    selection: Mapping[str, Any]
    exclusions: tuple[str, ...]
    encryption: Mapping[str, Any]
    compression: str
    parent_backup_id: str | None
    sync_state: Mapping[str, Any]
    resume: Mapping[str, Any]
    objects: Mapping[str, ObjectDescriptor]
    items: tuple[ItemDescriptor, ...]
    statistics: Mapping[str, int]
    warnings: tuple[str, ...]
    integrity: Mapping[str, str]
    format: str = FORMAT_NAME
    format_version: int = FORMAT_VERSION
    schema_version: int = BACKUP_SCHEMA_VERSION
    privacy_notice: str = PRIVACY_NOTICE

    def __post_init__(self) -> None:
        validate_backup_id(self.backup_id)
        if self.format != FORMAT_NAME:
            raise BackupIntegrityError(f"Not a {FORMAT_NAME} manifest.")
        if self.format_version != FORMAT_VERSION:
            relation = "newer" if self.format_version > FORMAT_VERSION else "unsupported"
            raise BackupIntegrityError(
                f"Backup format version {self.format_version} is {relation}; this application "
                f"supports version {FORMAT_VERSION}."
            )
        if self.schema_version != BACKUP_SCHEMA_VERSION:
            raise BackupIntegrityError(
                "Unsupported backup schema "
                f"{self.schema_version}; expected {BACKUP_SCHEMA_VERSION}."
            )
        if self.state not in {"incomplete", "complete"}:
            raise BackupIntegrityError(f"Invalid backup state: {self.state}")
        if self.compression not in {"none", "zlib"}:
            raise BackupIntegrityError(f"Unsupported backup compression: {self.compression}")
        if self.parent_backup_id is not None:
            validate_backup_id(self.parent_backup_id)
            if self.parent_backup_id == self.backup_id:
                raise BackupIntegrityError("A backup cannot name itself as its parent.")
        object_keys = set(self.objects)
        for key, descriptor in self.objects.items():
            if key != descriptor.content_id:
                raise BackupIntegrityError("Backup object map key does not match its descriptor.")
        for item in self.items:
            referenced = {item.record_object, *item.dependency_objects}
            missing = referenced - object_keys
            if missing:
                raise BackupIntegrityError("Backup item references an unknown content object.")
        _validate_encryption_metadata(self.encryption)

    @property
    def encrypted(self) -> bool:
        return bool(self.encryption.get("enabled", False))

    def to_dict(self, *, include_integrity: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "format": self.format,
            "format_version": self.format_version,
            "schema_version": self.schema_version,
            "application": {"name": "gucmail-cli", "version": self.application_version},
            "backup_id": self.backup_id,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "privacy_notice": self.privacy_notice,
            "selection": dict(self.selection),
            "exclusions": list(self.exclusions),
            "encryption": dict(self.encryption),
            "compression": self.compression,
            "parent_backup_id": self.parent_backup_id,
            "sync_state": dict(self.sync_state),
            "resume": dict(self.resume),
            "objects": {
                key: descriptor.to_dict() for key, descriptor in sorted(self.objects.items())
            },
            "items": [item.to_dict() for item in self.items],
            "statistics": dict(self.statistics),
            "warnings": list(self.warnings),
        }
        if include_integrity:
            value["integrity"] = dict(self.integrity)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BackupManifest:
        required = {
            "format",
            "format_version",
            "schema_version",
            "application",
            "backup_id",
            "kind",
            "state",
            "created_at",
            "selection",
            "encryption",
            "compression",
            "objects",
            "items",
            "statistics",
            "integrity",
        }
        _require_fields(value, required, "manifest")
        application = value["application"]
        objects = value["objects"]
        items = value["items"]
        if not isinstance(application, Mapping) or not isinstance(application.get("version"), str):
            raise BackupIntegrityError("Backup manifest application version is invalid.")
        if not isinstance(objects, Mapping):
            raise BackupIntegrityError("Backup manifest objects must be a JSON object.")
        if not isinstance(items, list):
            raise BackupIntegrityError("Backup manifest items must be a JSON array.")
        selection = _mapping(value.get("selection"), "selection")
        encryption = _mapping(value.get("encryption"), "encryption")
        sync_state = _mapping(value.get("sync_state", {}), "sync state")
        resume = _mapping(value.get("resume", {}), "resume metadata")
        statistics_raw = _mapping(value.get("statistics"), "statistics")
        integrity = _mapping(value.get("integrity"), "integrity")
        exclusions = _string_list(value.get("exclusions", []), "exclusions")
        warnings = _string_list(value.get("warnings", []), "warnings")
        completed = value.get("completed_at")
        parent = value.get("parent_backup_id")
        if completed is not None and not isinstance(completed, str):
            raise BackupIntegrityError("Backup completion time must be text or null.")
        if parent is not None and not isinstance(parent, str):
            raise BackupIntegrityError("Backup parent identifier must be text or null.")
        try:
            statistics = {str(key): int(item) for key, item in statistics_raw.items()}
            descriptors = {
                str(key): ObjectDescriptor.from_dict(_mapping(item, "object descriptor"))
                for key, item in objects.items()
            }
            item_descriptors = tuple(
                ItemDescriptor.from_dict(_mapping(item, "item descriptor")) for item in items
            )
            return cls(
                backup_id=str(value["backup_id"]),
                application_version=application["version"],
                kind=str(value["kind"]),
                state=str(value["state"]),
                created_at=str(value["created_at"]),
                completed_at=completed,
                selection=selection,
                exclusions=tuple(exclusions),
                encryption=encryption,
                compression=str(value["compression"]),
                parent_backup_id=parent,
                sync_state=sync_state,
                resume=resume,
                objects=descriptors,
                items=item_descriptors,
                statistics=statistics,
                warnings=tuple(warnings),
                integrity={str(key): str(item) for key, item in integrity.items()},
                format=str(value["format"]),
                format_version=int(value["format_version"]),
                schema_version=int(value["schema_version"]),
                privacy_notice=str(value.get("privacy_notice", PRIVACY_NOTICE)),
            )
        except BackupIntegrityError:
            raise
        except (TypeError, ValueError) as exc:
            raise BackupIntegrityError(f"Invalid backup manifest: {exc}") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BackupIntegrityError(f"Backup {label} must be a JSON object.")
    return {str(key): item for key, item in value.items()}


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BackupIntegrityError(f"Backup {label} must be a list of strings.")
    return list(value)


def _require_fields(value: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise BackupIntegrityError(f"Backup {label} is missing: {', '.join(missing)}")


def encode_b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode_b64(value: Any, *, label: str) -> bytes:
    if not isinstance(value, str):
        raise BackupIntegrityError(f"Backup {label} is missing or invalid.")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise BackupIntegrityError(f"Backup {label} is not valid base64.") from exc


def _validate_encryption_metadata(value: Mapping[str, Any]) -> None:
    enabled = value.get("enabled")
    if enabled is False:
        if value.get("algorithm", "none") != "none":
            raise BackupIntegrityError("Unencrypted backup has inconsistent algorithm metadata.")
        return
    if enabled is not True or value.get("algorithm") != "AES-256-GCM":
        raise BackupIntegrityError("Backup encryption metadata is invalid.")
    if value.get("kdf") != "scrypt":
        raise BackupIntegrityError("Unsupported backup key derivation function.")
    salt = decode_b64(value.get("salt"), label="Scrypt salt")
    if len(salt) < 16 or len(salt) > 64:
        raise BackupIntegrityError("Backup Scrypt salt length is invalid.")
    try:
        n = int(value["n"])
        r = int(value["r"])
        p = int(value["p"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupIntegrityError("Backup Scrypt parameters are invalid.") from exc
    if n < 2**12 or n > 2**20 or n & (n - 1) or not 1 <= r <= 32 or not 1 <= p <= 16:
        raise BackupIntegrityError("Backup Scrypt parameters are outside safe supported bounds.")
    if 128 * n * r > _MAX_SCRYPT_MEMORY:
        raise BackupIntegrityError("Backup Scrypt parameters request excessive memory.")


def new_encryption_metadata(
    passphrase: str | bytes,
    *,
    n: int = _DEFAULT_SCRYPT_N,
    r: int = _DEFAULT_SCRYPT_R,
    p: int = _DEFAULT_SCRYPT_P,
) -> tuple[dict[str, Any], KeyMaterial]:
    secret = _passphrase_bytes(passphrase)
    metadata: dict[str, Any] = {
        "enabled": True,
        "algorithm": "AES-256-GCM",
        "kdf": "scrypt",
        "salt": encode_b64(os.urandom(16)),
        "n": n,
        "r": r,
        "p": p,
        "key_bytes": 64,
    }
    _validate_encryption_metadata(metadata)
    return metadata, derive_key_material(secret, metadata)


def unencrypted_metadata() -> dict[str, Any]:
    return {"enabled": False, "algorithm": "none", "kdf": "none"}


def derive_key_material(
    passphrase: str | bytes,
    encryption: Mapping[str, Any],
) -> KeyMaterial:
    _validate_encryption_metadata(encryption)
    if encryption.get("enabled") is not True:
        raise BackupIntegrityError("No encryption key exists for an unencrypted backup.")
    secret = _passphrase_bytes(passphrase)
    try:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:  # pragma: no cover - project dependency guard
        raise BackupIntegrityError(
            "Encrypted backups require the project's 'cryptography' dependency."
        ) from exc
    kdf = Scrypt(
        salt=decode_b64(encryption["salt"], label="Scrypt salt"),
        length=64,
        n=int(encryption["n"]),
        r=int(encryption["r"]),
        p=int(encryption["p"]),
    )
    material = kdf.derive(secret)
    return KeyMaterial(encryption_key=material[:32], manifest_key=material[32:])


def _passphrase_bytes(value: str | bytes) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    if not raw:
        raise BackupIntegrityError("Backup passphrase cannot be empty.")
    if len(raw) > 1024 * 1024:
        raise BackupIntegrityError("Backup passphrase is unreasonably large.")
    return raw


def object_aad(backup_id: str, content_id: str) -> bytes:
    validate_backup_id(backup_id)
    validate_digest(content_id, label="content identifier")
    return f"{FORMAT_NAME}\0{FORMAT_VERSION}\0{backup_id}\0{content_id}".encode()


def encrypt_object(plaintext: bytes, key: bytes, *, aad: bytes) -> tuple[bytes, str]:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - project dependency guard
        raise BackupIntegrityError(
            "Encrypted backups require the project's 'cryptography' dependency."
        ) from exc
    if len(key) != 32:
        raise BackupIntegrityError("Backup encryption key has an invalid length.")
    nonce = os.urandom(12)
    return AESGCM(key).encrypt(nonce, plaintext, aad), encode_b64(nonce)


def decrypt_object(ciphertext: bytes, key: bytes, *, aad: bytes, nonce: str) -> bytes:
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - project dependency guard
        raise BackupIntegrityError(
            "Encrypted backups require the project's 'cryptography' dependency."
        ) from exc
    try:
        return AESGCM(key).decrypt(decode_b64(nonce, label="object nonce"), ciphertext, aad)
    except InvalidTag as exc:
        raise BackupIntegrityError(
            "Backup passphrase is incorrect or encrypted object data is damaged."
        ) from exc


def manifest_payload(value: BackupManifest | Mapping[str, Any]) -> bytes:
    raw = (
        value.to_dict(include_integrity=False) if isinstance(value, BackupManifest) else dict(value)
    )
    raw.pop("integrity", None)
    return canonical_json_bytes(raw)


def manifest_integrity(
    value: BackupManifest | Mapping[str, Any],
    *,
    manifest_key: bytes | None,
) -> dict[str, str]:
    payload = manifest_payload(value)
    if manifest_key is None:
        return {"algorithm": "SHA-256", "digest": sha256_bytes(payload)}
    return {
        "algorithm": "HMAC-SHA-256",
        "digest": hmac.new(manifest_key, payload, hashlib.sha256).hexdigest(),
    }


def seal_manifest(manifest: BackupManifest, *, manifest_key: bytes | None) -> BackupManifest:
    raw = manifest.to_dict(include_integrity=False)
    raw["integrity"] = manifest_integrity(raw, manifest_key=manifest_key)
    return BackupManifest.from_dict(raw)


def verify_manifest_integrity(manifest: BackupManifest, *, manifest_key: bytes | None) -> None:
    expected = manifest_integrity(manifest, manifest_key=manifest_key)
    actual_algorithm = manifest.integrity.get("algorithm")
    actual_digest = manifest.integrity.get("digest", "")
    if actual_algorithm != expected["algorithm"] or not hmac.compare_digest(
        actual_digest, expected["digest"]
    ):
        if manifest.encrypted:
            raise BackupIntegrityError(
                "Backup passphrase is incorrect or the encrypted manifest was modified."
            )
        raise BackupIntegrityError("Backup manifest checksum does not match its contents.")


def write_manifest(
    path: Path,
    manifest: BackupManifest,
    *,
    manifest_key: bytes | None,
) -> BackupManifest:
    sealed = seal_manifest(manifest, manifest_key=manifest_key)
    atomic_write_json(path, sealed.to_dict())
    return sealed


def read_manifest(
    path: Path,
    *,
    passphrase: str | bytes | None = None,
) -> tuple[BackupManifest, KeyMaterial | None]:
    raw = load_json(path)
    manifest = BackupManifest.from_dict(raw)
    keys: KeyMaterial | None = None
    if manifest.encrypted:
        if passphrase is None:
            raise BackupIntegrityError("This backup is encrypted; a passphrase is required.")
        keys = derive_key_material(passphrase, manifest.encryption)
    elif passphrase is not None:
        # Supplying a passphrase for plaintext data is almost always operator
        # confusion.  Reject it rather than implying it protected the backup.
        raise BackupIntegrityError("This backup is unencrypted; do not supply a passphrase.")
    verify_manifest_integrity(manifest, manifest_key=keys.manifest_key if keys else None)
    return manifest, keys


load_manifest = read_manifest


__all__ = [
    "BACKUP_SCHEMA_VERSION",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "INCOMPLETE_MANIFEST_FILENAME",
    "MANIFEST_FILENAME",
    "OBJECT_DIRECTORY",
    "PRIVACY_NOTICE",
    "BackupManifest",
    "ItemDescriptor",
    "KeyMaterial",
    "ObjectDescriptor",
    "atomic_write_bytes",
    "atomic_write_json",
    "canonical_json_bytes",
    "decrypt_object",
    "derive_key_material",
    "encrypt_object",
    "load_json",
    "load_manifest",
    "manifest_integrity",
    "new_encryption_metadata",
    "object_aad",
    "object_relative_path",
    "read_manifest",
    "resolve_relative_path",
    "seal_manifest",
    "sha256_bytes",
    "unencrypted_metadata",
    "utc_text",
    "validate_backup_id",
    "verify_manifest_integrity",
    "write_manifest",
]
