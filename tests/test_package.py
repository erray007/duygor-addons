import hashlib
import json
import zipfile

import pytest

from duygor_registry import language
from duygor_registry.package import PackageError, inspect
from helpers import make_zip, valid_entries

MIB = 1024 * 1024


def code_of(data):
    with pytest.raises(PackageError) as e:
        inspect(data)
    assert e.value.message_tr and e.value.message_en
    return e.value.code


def test_valid_package():
    data = make_zip(valid_entries(minDuygorSurumu=521))
    info = inspect(data)
    assert info.id == "demo"
    assert info.version == "1.0"
    assert info.min_version == 521
    assert info.author == "Eray"
    assert info.source_language == "tr"
    assert info.languages == []
    assert info.names == {"tr": "Demo"}
    assert info.descriptions == {"tr": "Does things"}
    assert info.sha256 == hashlib.sha256(data).hexdigest()
    assert info.size == len(data)


def test_english_manifest_keys_and_defaults():
    entries = valid_entries()
    entries["manifest.json"] = {"id": "a.b-c_1", "name": "N", "language": "en_GB"}
    info = inspect(make_zip(entries))
    assert info.id == "a.b-c_1"
    assert info.version == "1.0"
    assert info.min_version == 0
    assert info.author == ""
    assert info.source_language == "en-GB"
    assert info.names == {"en-GB": "N"}


def test_name_defaults_to_id():
    entries = valid_entries(ad="  ")
    assert inspect(make_zip(entries)).names == {"tr": "demo"}


def test_numeric_version_in_manifest():
    assert inspect(make_zip(valid_entries(surum=2))).version == "2"


def test_error_not_zip():
    assert code_of(b"definitely not a zip") == "zip"
    assert code_of(b"") == "zip"


def test_error_truncated_zip():
    data = make_zip(valid_entries())
    assert code_of(data[: len(data) // 2]) == "zip"


def test_error_missing_manifest():
    assert code_of(make_zip({"main.lua": "x"})) == "manifest"


def test_error_bad_manifest_json():
    assert code_of(make_zip({"manifest.json": "{nope", "main.lua": "x"})) == "manifest"
    assert code_of(make_zip({"manifest.json": "[1]", "main.lua": "x"})) == "manifest"


def test_error_missing_main():
    assert code_of(make_zip({"manifest.json": {"kimlik": "demo"}})) == "main"


@pytest.mark.parametrize("name", ["/etc/passwd", "../evil.lua", "a/../../b", "..", "a\\..\\b", "\\abs", "a/.."])
def test_error_path(name):
    entries = valid_entries()
    entries[name] = "x"
    assert code_of(make_zip(entries)) == "path"


def test_dotted_names_are_not_path_errors():
    entries = valid_entries()
    entries["lib/a..b.lua"] = "x"
    inspect(make_zip(entries))


def test_error_size_download():
    data = b"\0" * (20 * MIB + 1)
    assert code_of(data) == "size"


def test_download_exactly_limit_is_not_size_error():
    assert code_of(b"\0" * (20 * MIB)) == "zip"


def test_zip_bomb_rejected():
    entries = valid_entries()
    entries["big.bin"] = b"\0" * (21 * MIB)
    data = make_zip(entries)
    assert len(data) < MIB
    assert code_of(data) == "size"


def test_unpacked_total_across_files():
    entries = valid_entries()
    entries["a.bin"] = b"\0" * (11 * MIB)
    entries["b.bin"] = b"\0" * (10 * MIB)
    assert code_of(make_zip(entries)) == "size"


def test_unpacked_exactly_limit_ok():
    manifest_text = json.dumps(valid_entries()["manifest.json"])
    entries = {"manifest.json": manifest_text, "main.lua": b"\0" * (20 * MIB - len(manifest_text))}
    inspect(make_zip(entries))
    entries["main.lua"] += b"\0"
    assert code_of(make_zip(entries)) == "size"


@pytest.mark.parametrize("bad", ["", "Demo", "has space", "a/b", "x" * 65, "ünlü"])
def test_error_id(bad):
    assert code_of(make_zip(valid_entries(kimlik=bad))) == "id"


def test_error_id_missing():
    assert code_of(make_zip(valid_entries(kimlik=None))) == "id"


def test_id_max_length_ok():
    assert inspect(make_zip(valid_entries(kimlik="x" * 64))).id == "x" * 64


@pytest.mark.parametrize("bad", ["v1.0", "1.0-beta", "1.2.3.4.5", "1..2", "1234567890", ""])
def test_error_version(bad):
    assert code_of(make_zip(valid_entries(surum=bad))) == "version"


def test_error_lang_invalid_tag():
    for name in ["languages/xx_yy_zz.json", "languages/de_foo.json", "languages/english.json", "languages/a.json", "languages/.json", "languages/sub/de.json"]:
        entries = valid_entries()
        entries[name] = {}
        assert code_of(make_zip(entries)) == "lang", name


def test_error_lang_duplicate():
    entries = valid_entries()
    entries["languages/pt_BR.json"] = {}
    entries["languages/pt-br.json"] = {}
    assert code_of(make_zip(entries)) == "lang"


def test_error_lang_not_object():
    for body in ["[]", "\"x\"", "{oops", "3"]:
        entries = valid_entries()
        entries["languages/en.json"] = body
        assert code_of(make_zip(entries)) == "lang", body


def test_non_json_files_in_languages_ignored():
    entries = valid_entries()
    entries["languages/README.txt"] = "hello"
    entries["languages/"] = b""
    assert inspect(make_zip(entries)).languages == []


def test_names_from_languages():
    entries = valid_entries(ad="Demo", aciklama="Does things")
    entries["languages/en.json"] = {"Demo": "Demo EN", "Does things": "Does stuff"}
    entries["languages/pt_BR.json"] = {"Demo": "Demonstracao", "Does things": ""}
    entries["languages/de.json"] = {"Other": "x"}
    entries["languages/fr.json"] = "﻿" + '{"Demo": "Demo FR"}'
    entries["languages/tr.json"] = {"Demo": "ignored, same as source"}
    info = inspect(make_zip(entries))
    assert info.languages == ["de", "en", "fr", "pt-BR", "tr"]
    assert info.names == {"tr": "Demo", "en": "Demo EN", "pt-BR": "Demonstracao", "fr": "Demo FR"}
    assert info.descriptions == {"tr": "Does things", "en": "Does stuff"}


def test_non_string_translation_values_ignored():
    entries = valid_entries()
    entries["languages/en.json"] = {"Demo": 5}
    info = inspect(make_zip(entries))
    assert info.languages == ["en"]
    assert "en" not in info.names


def test_stored_zip_method_ok():
    inspect(make_zip(valid_entries(), method=zipfile.ZIP_STORED))


def test_normalize_tags():
    assert language.normalize("pt_BR") == "pt-BR"
    assert language.normalize("pt_br") == "pt-BR"
    assert language.normalize("EN") == "en"
    assert language.normalize("zh-hant-tw") == "zh-Hant-TW"
    assert language.normalize("pt-BR-u-mu-celsius") == "pt-BR"
    assert language.normalize("x") is None
    assert language.normalize("english") is None
    assert language.normalize("") is None
    assert language.normalize(None) is None


def test_file_tag():
    assert language.file_tag("languages/pt_BR.json") == "pt-BR"
    assert language.file_tag("EN.json") == "en"
    assert language.file_tag("languages/de_foo.json") is None
    assert language.file_tag("languages/xx_yy_zz.json") is None
    assert language.file_tag("languages/a/b.json") is None
    assert language.file_tag("languages/en.txt") is None
