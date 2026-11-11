"""Inspection of .dgp add-on packages (zip files), mirroring the rules DuyGör applies on install.

See EklentiDeposu.paketiOku, EklentiBilgisi.jsondan, EklentiDili and EklentiCevirisi in the app.
"""

import hashlib
import io
import json
import re
import zipfile
import zlib
from dataclasses import dataclass, field

from . import language, version

MAX_BYTES = 20 * 1024 * 1024
CHUNK = 64 * 1024
DEFAULT_SOURCE_LANGUAGE = "tr"
DEFAULT_VERSION = "1.0"

# A leading dot is refused so no id can name ., .. or a hidden file (EklentiBilgisi.gecerliKimlik).
ID = re.compile(r"(?!\.)[a-z0-9_.-]{1,64}")
BOM = "\ufeff"
_TRIM = "".join(chr(i) for i in range(33))
_ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, OSError, ValueError, OverflowError)


class PackageError(Exception):
    def __init__(self, code, message_tr, message_en):
        super().__init__(message_en)
        self.code = code
        self.message_tr = message_tr
        self.message_en = message_en


@dataclass(frozen=True)
class PackageInfo:
    id: str
    version: str
    min_version: int
    author: str
    source_language: str
    languages: list = field(default_factory=list)
    names: dict = field(default_factory=dict)
    descriptions: dict = field(default_factory=dict)
    sha256: str = ""
    size: int = 0


def _size_error():
    return PackageError("size", "Paket 20 MB sınırını aşıyor (indirilen ya da açılmış boyut).", "Package exceeds the 20 MB limit (download or unpacked size).")


def _path_error(name):
    return PackageError("path", "Geçersiz dosya yolu: " + name, "Invalid file path: " + name)


def _lang_error(name, reason_tr, reason_en):
    return PackageError("lang", name + ": " + reason_tr, name + ": " + reason_en)


def _is_unsafe(name):
    return name.startswith("/") or "../" in name or name == ".." or name.endswith("/..")


def _read_entries(data):
    entries = {}
    total = 0
    declared = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if _is_unsafe(name):
                raise _path_error(name)
            if name.endswith("/"):
                continue
            declared += info.file_size
            if declared > MAX_BYTES:
                raise _size_error()
            # The header may lie about the size, so the real total is counted while inflating.
            chunks = []
            with zf.open(info) as src:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise _size_error()
                    chunks.append(chunk)
            entries[name] = b"".join(chunks)
    return entries


def _opt_string(obj, key, default):
    """org.json optString: missing or null gives the default, other values are stringified."""
    value = obj.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _opt_int(obj, key, default):
    value = obj.get(key)
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) != float("inf") else default
    if isinstance(value, str):
        try:
            return int(value.strip(_TRIM))
        except ValueError:
            return default
    return default


def _decode_json(raw):
    # A UTF-8 BOM (added by some Windows editors) is stripped, like the app does.
    return json.loads(raw.decode("utf-8", errors="replace").removeprefix(BOM))


def _parse_manifest(raw):
    try:
        obj = _decode_json(raw)
    except (ValueError, RecursionError):
        obj = None
    if not isinstance(obj, dict):
        raise PackageError("manifest", "manifest.json geçerli bir JSON nesnesi değil.", "manifest.json is not a valid JSON object.")
    return obj


def _translation_table(name, raw):
    try:
        obj = _decode_json(raw)
    except (ValueError, RecursionError):
        obj = None
    if not isinstance(obj, dict):
        raise _lang_error(name, "geçerli bir JSON nesnesi değil", "not a valid JSON object")
    return {k: v for k, v in obj.items() if isinstance(v, str) and v != ""}


def _collect_languages(entries):
    tables = {}
    sources = {}
    for name in sorted(entries):
        if not name.startswith(language.FOLDER) or not name.endswith(language.EXTENSION):
            continue
        tag = language.file_tag(name)
        if tag is None:
            raise _lang_error(name, "geçersiz dil etiketi", "invalid language tag")
        if tag in sources:
            raise _lang_error(name, sources[tag] + " ile aynı dil", "same language as " + sources[tag])
        sources[tag] = name
        tables[tag] = _translation_table(name, entries[name])
    return tables


def peek_version(data):
    """Best-effort manifest version of a package that may have failed inspect(); None when unreadable or not a store version.

    Lets the registry tell "a broken newer release" from "a broken release that is not newer anyway".
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("manifest.json")
            if info.file_size > MAX_BYTES:
                return None
            with zf.open(info) as src:
                raw = src.read(MAX_BYTES + 1)
    except (KeyError, *_ZIP_ERRORS):
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        obj = _decode_json(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    found = _opt_string(obj, "surum", _opt_string(obj, "version", DEFAULT_VERSION)).strip(_TRIM)
    return found if version.is_valid(found) else None


def inspect(data):
    """Validates a package and returns its PackageInfo; raises PackageError when invalid."""
    if len(data) > MAX_BYTES:
        raise _size_error()
    try:
        entries = _read_entries(data)
    except PackageError:
        raise
    except _ZIP_ERRORS:
        raise PackageError("zip", "Dosya geçerli bir zip paketi değil.", "File is not a valid zip package.") from None
    if "manifest.json" not in entries:
        raise PackageError("manifest", "Pakette manifest.json yok.", "manifest.json is missing from the package.")
    if "main.lua" not in entries:
        raise PackageError("main", "Pakette main.lua yok.", "main.lua is missing from the package.")
    manifest = _parse_manifest(entries["manifest.json"])
    package_id = _opt_string(manifest, "kimlik", _opt_string(manifest, "id", "")).strip(_TRIM)
    name = _opt_string(manifest, "ad", _opt_string(manifest, "name", package_id)).strip(_TRIM)
    package_version = _opt_string(manifest, "surum", _opt_string(manifest, "version", DEFAULT_VERSION)).strip(_TRIM)
    author = _opt_string(manifest, "yazar", _opt_string(manifest, "author", "")).strip(_TRIM)
    description = _opt_string(manifest, "aciklama", _opt_string(manifest, "description", "")).strip(_TRIM)
    min_version = _opt_int(manifest, "minDuygorSurumu", _opt_int(manifest, "minVersion", 0))
    source = language.normalize(_opt_string(manifest, "dil", _opt_string(manifest, "language", ""))) or DEFAULT_SOURCE_LANGUAGE
    if not ID.fullmatch(package_id):
        raise PackageError("id", "manifest.json: kimlik geçersiz (a-z, 0-9, _ . - ve en fazla 64 karakter, nokta ile başlayamaz).", "manifest.json: id is invalid (a-z, 0-9, _ . - and at most 64 characters, not starting with a dot).")
    if not version.is_valid(package_version):
        raise PackageError("version", "manifest.json: surum geçersiz: " + package_version, "manifest.json: version is invalid: " + package_version)
    if name == "":
        name = package_id
    tables = _collect_languages(entries)
    names = {source: name}
    descriptions = {source: description}
    for tag, table in tables.items():
        # The app shows the source text itself for the source language, so no translation applies.
        if language.primary(tag) == language.primary(source):
            continue
        if name in table:
            names[tag] = table[name]
        if description in table:
            descriptions[tag] = table[description]
    return PackageInfo(package_id, package_version, min_version, author, source, sorted(tables), names, descriptions, hashlib.sha256(data).hexdigest(), len(data))
