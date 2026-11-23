from __future__ import annotations

import csv
import hashlib
import io
import json
import stat
import zipfile

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


ALLOWED_UPLOAD_SUFFIXES = frozenset({".csv", ".json", ".zip"})
ALLOWED_ARCHIVE_SUFFIXES = frozenset({".csv", ".json"})

ZIP_MAGIC = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
)

BLOCKED_MAGIC = (
    (b"MZ", "Windows executable"),
    (b"\x7fELF", "ELF executable"),
)


class ImportSecurityError(ValueError):
    """Raised when an import upload violates Kokabiel's intake policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImportLimits:
    # Whole Telegram/file upload.
    max_upload_bytes: int = 50 * 1024 * 1024

    # ZIP defenses.
    max_archive_members: int = 100
    max_member_bytes: int = 50 * 1024 * 1024
    max_expanded_bytes: int = 200 * 1024 * 1024
    max_compression_ratio: float = 200.0

    # Structured text defenses.
    max_csv_rows: int = 250_000
    max_csv_columns: int = 256
    max_csv_field_chars: int = 100_000
    max_json_depth: int = 64
    max_json_nodes: int = 500_000


@dataclass(frozen=True, slots=True)
class InspectedMember:
    name: str
    kind: str
    compressed_bytes: int
    expanded_bytes: int


@dataclass(frozen=True, slots=True)
class InspectionResult:
    path: Path
    kind: str
    size_bytes: int
    sha256: str
    members: tuple[InspectedMember, ...] = ()


DEFAULT_LIMITS = ImportLimits()


def inspect_import_file(
    path: str | Path,
    *,
    limits: ImportLimits = DEFAULT_LIMITS,
) -> InspectionResult:
    """
    Inspect an uploaded tracker export.

    This function:
      - never executes uploaded content
      - never extracts ZIP members to disk
      - never follows symlinks
      - accepts only CSV, JSON, and ZIP
    """

    candidate = Path(path)

    try:
        st = candidate.lstat()
    except FileNotFoundError as exc:
        raise ImportSecurityError(
            "missing",
            "Import file does not exist.",
        ) from exc

    if stat.S_ISLNK(st.st_mode):
        raise ImportSecurityError(
            "symlink",
            "Import uploads may not be symlinks.",
        )

    if not stat.S_ISREG(st.st_mode):
        raise ImportSecurityError(
            "not_regular",
            "Import upload must be a regular file.",
        )

    if st.st_size <= 0:
        raise ImportSecurityError(
            "empty",
            "Import upload is empty.",
        )

    if st.st_size > limits.max_upload_bytes:
        raise ImportSecurityError(
            "upload_too_large",
            "Import upload exceeds the configured size limit.",
        )

    suffix = candidate.suffix.casefold()

    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise ImportSecurityError(
            "unsupported_type",
            "Only CSV, JSON, and ZIP imports are accepted.",
        )

    with candidate.open("rb") as handle:
        head = handle.read(8)

    _reject_executable_magic(head, candidate.name)

    zip_content = any(head.startswith(prefix) for prefix in ZIP_MAGIC)

    if suffix == ".zip":
        if not zip_content or not zipfile.is_zipfile(candidate):
            raise ImportSecurityError(
                "type_mismatch",
                "The file is named as ZIP but is not a valid ZIP archive.",
            )

        members = _inspect_zip(candidate, limits)
        kind = "zip"

    else:
        if zip_content:
            raise ImportSecurityError(
                "type_mismatch",
                "ZIP content must use a .zip filename.",
            )

        with candidate.open("rb") as handle:
            data = _read_limited(
                handle,
                limits.max_upload_bytes,
                candidate.name,
            )

        _validate_structured_text(
            data,
            suffix=suffix,
            display_name=candidate.name,
            limits=limits,
        )

        members = ()
        kind = suffix.removeprefix(".")

    return InspectionResult(
        path=candidate,
        kind=kind,
        size_bytes=st.st_size,
        sha256=_sha256(candidate),
        members=members,
    )


def neutralize_spreadsheet_formula(value: str) -> str:
    """
    Protect future Kokabiel-generated CSV reports from formula injection.

    Imported values are data only and are never executed, but values written
    back into spreadsheet-oriented CSV files need this protection.
    """

    if value.startswith(("=", "+", "-", "@")):
        return "'" + value

    return value


def _inspect_zip(
    path: Path,
    limits: ImportLimits,
) -> tuple[InspectedMember, ...]:
    members: list[InspectedMember] = []
    total_expanded = 0

    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImportSecurityError(
            "bad_zip",
            "Unable to open ZIP archive.",
        ) from exc

    with archive:
        infos = archive.infolist()
        files = [info for info in infos if not info.is_dir()]

        if not files:
            raise ImportSecurityError(
                "empty_archive",
                "ZIP archive contains no files.",
            )

        if len(files) > limits.max_archive_members:
            raise ImportSecurityError(
                "too_many_members",
                "ZIP archive contains too many files.",
            )

        for info in infos:
            _validate_zip_member_name(info.filename)

            if info.flag_bits & 0x1:
                raise ImportSecurityError(
                    "encrypted_archive",
                    f"Encrypted ZIP member is not allowed: {info.filename}",
                )

            _reject_zip_special_file(info)

            if info.is_dir():
                continue

            member_path = PurePosixPath(
                info.filename.replace("\\", "/")
            )

            suffix = member_path.suffix.casefold()

            # Nested archives, executables, scripts, text attachments, etc.
            # are not part of the accepted tracker-import surface.
            if suffix not in ALLOWED_ARCHIVE_SUFFIXES:
                raise ImportSecurityError(
                    "archive_member_type",
                    f"ZIP member type is not allowed: {info.filename}",
                )

            if info.file_size > limits.max_member_bytes:
                raise ImportSecurityError(
                    "member_too_large",
                    f"ZIP member exceeds the size limit: {info.filename}",
                )

            total_expanded += info.file_size

            if total_expanded > limits.max_expanded_bytes:
                raise ImportSecurityError(
                    "archive_too_large",
                    "ZIP archive expands beyond the total size limit.",
                )

            if info.file_size > 0:
                if info.compress_size <= 0:
                    raise ImportSecurityError(
                        "suspicious_compression",
                        f"Invalid compression metadata: {info.filename}",
                    )

                ratio = info.file_size / info.compress_size

                if ratio > limits.max_compression_ratio:
                    raise ImportSecurityError(
                        "compression_bomb",
                        f"Suspicious compression ratio: {info.filename}",
                    )

            try:
                with archive.open(info, "r") as member:
                    data = _read_limited(
                        member,
                        limits.max_member_bytes,
                        info.filename,
                    )

            except RuntimeError as exc:
                raise ImportSecurityError(
                    "encrypted_archive",
                    f"ZIP member could not be safely read: {info.filename}",
                ) from exc

            except zipfile.BadZipFile as exc:
                raise ImportSecurityError(
                    "bad_zip_member",
                    f"ZIP member failed integrity checking: {info.filename}",
                ) from exc

            if len(data) != info.file_size:
                raise ImportSecurityError(
                    "member_size_mismatch",
                    f"ZIP member size does not match metadata: {info.filename}",
                )

            _validate_structured_text(
                data,
                suffix=suffix,
                display_name=info.filename,
                limits=limits,
            )

            members.append(
                InspectedMember(
                    name=info.filename,
                    kind=suffix.removeprefix("."),
                    compressed_bytes=info.compress_size,
                    expanded_bytes=info.file_size,
                )
            )

    return tuple(members)


def _validate_zip_member_name(name: str) -> None:
    normalized = name.replace("\\", "/")

    if not normalized or "\x00" in normalized:
        raise ImportSecurityError(
            "bad_member_name",
            "ZIP contains an invalid member name.",
        )

    if normalized.startswith("/"):
        raise ImportSecurityError(
            "absolute_path",
            f"ZIP member uses an absolute path: {name}",
        )

    path = PurePosixPath(normalized)

    if ".." in path.parts:
        raise ImportSecurityError(
            "path_traversal",
            f"ZIP member attempts path traversal: {name}",
        )

    if path.parts and ":" in path.parts[0]:
        raise ImportSecurityError(
            "absolute_path",
            f"ZIP member uses a drive-qualified path: {name}",
        )


def _reject_zip_special_file(info: zipfile.ZipInfo) -> None:
    mode = (info.external_attr >> 16) & 0xFFFF

    if not mode:
        return

    file_type = stat.S_IFMT(mode)

    if file_type in {
        0,
        stat.S_IFREG,
        stat.S_IFDIR,
    }:
        return

    raise ImportSecurityError(
        "special_file",
        f"ZIP member is not a regular file/directory: {info.filename}",
    )


def _read_limited(
    stream: BinaryIO,
    maximum: int,
    display_name: str,
) -> bytes:
    data = stream.read(maximum + 1)

    if len(data) > maximum:
        raise ImportSecurityError(
            "content_too_large",
            f"Import member exceeds the size limit: {display_name}",
        )

    return data


def _validate_structured_text(
    data: bytes,
    *,
    suffix: str,
    display_name: str,
    limits: ImportLimits,
) -> None:
    _reject_executable_magic(data[:8], display_name)

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportSecurityError(
            "not_utf8",
            f"Import text must be valid UTF-8: {display_name}",
        ) from exc

    if not text.strip():
        raise ImportSecurityError(
            "empty_text",
            f"Import contains no data: {display_name}",
        )

    _reject_control_characters(text, display_name)

    if suffix == ".csv":
        _validate_csv(text, display_name, limits)
        return

    if suffix == ".json":
        _validate_json(text, display_name, limits)
        return

    raise ImportSecurityError(
        "unsupported_type",
        f"Unsupported structured-text type: {display_name}",
    )


def _validate_csv(
    text: str,
    display_name: str,
    limits: ImportLimits,
) -> None:
    try:
        reader = csv.reader(
            io.StringIO(text, newline=""),
            strict=True,
        )

        rows = 0

        for row in reader:
            rows += 1

            if rows > limits.max_csv_rows:
                raise ImportSecurityError(
                    "too_many_rows",
                    f"CSV contains too many rows: {display_name}",
                )

            if len(row) > limits.max_csv_columns:
                raise ImportSecurityError(
                    "too_many_columns",
                    f"CSV contains too many columns: {display_name}",
                )

            if any(
                len(field) > limits.max_csv_field_chars
                for field in row
            ):
                raise ImportSecurityError(
                    "field_too_large",
                    f"CSV contains an oversized field: {display_name}",
                )

        if rows == 0:
            raise ImportSecurityError(
                "empty_csv",
                f"CSV contains no rows: {display_name}",
            )

    except csv.Error as exc:
        raise ImportSecurityError(
            "malformed_csv",
            f"CSV could not be parsed safely: {display_name}",
        ) from exc


def _validate_json(
    text: str,
    display_name: str,
    limits: ImportLimits,
) -> None:
    _validate_json_depth(
        text,
        display_name,
        limits.max_json_depth,
    )

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportSecurityError(
            "malformed_json",
            f"JSON could not be parsed safely: {display_name}",
        ) from exc

    if not isinstance(value, (dict, list)):
        raise ImportSecurityError(
            "json_root",
            f"JSON root must be an object or array: {display_name}",
        )

    nodes = 0
    pending = [value]

    while pending:
        current = pending.pop()
        nodes += 1

        if nodes > limits.max_json_nodes:
            raise ImportSecurityError(
                "too_many_json_nodes",
                f"JSON contains too many values: {display_name}",
            )

        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())

        elif isinstance(current, list):
            pending.extend(current)


def _validate_json_depth(
    text: str,
    display_name: str,
    maximum: int,
) -> None:
    stack: list[str] = []
    in_string = False
    escaped = False

    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False

            continue

        if char == '"':
            in_string = True
            continue

        if char in "[{":
            stack.append(char)

            if len(stack) > maximum:
                raise ImportSecurityError(
                    "json_too_deep",
                    f"JSON nesting exceeds the limit: {display_name}",
                )

        elif char in "]}":
            if not stack:
                raise ImportSecurityError(
                    "malformed_json",
                    f"JSON delimiters are invalid: {display_name}",
                )

            opening = stack.pop()

            if (opening, char) not in {
                ("[", "]"),
                ("{", "}"),
            }:
                raise ImportSecurityError(
                    "malformed_json",
                    f"JSON delimiters are invalid: {display_name}",
                )


def _reject_control_characters(
    text: str,
    display_name: str,
) -> None:
    for char in text:
        code = ord(char)

        if code == 0 or (
            code < 32
            and char not in "\t\n\r"
        ):
            raise ImportSecurityError(
                "control_character",
                f"Import contains binary/control data: {display_name}",
            )


def _reject_executable_magic(
    head: bytes,
    display_name: str,
) -> None:
    for prefix, label in BLOCKED_MAGIC:
        if head.startswith(prefix):
            raise ImportSecurityError(
                "executable_content",
                f"{label} content is not accepted: {display_name}",
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()
