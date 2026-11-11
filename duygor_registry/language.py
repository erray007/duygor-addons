"""Language tag handling, mirroring EklentiDili.normallestir / dosyaAdindanEtiket."""

import re

_LANGUAGE = re.compile(r"[A-Za-z]{2,3}")
_SCRIPT = re.compile(r"[A-Za-z]{4}")
_REGION = re.compile(r"[A-Za-z]{2}|[0-9]{3}")
_VARIANT = re.compile(r"[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}")
# Java String.trim() strips every char up to U+0020.
_TRIM = "".join(chr(i) for i in range(33))

FOLDER = "languages/"
EXTENSION = ".json"


def normalize(tag):
    """pt_br -> pt-BR; None when the language part is not 2-3 letters.

    Like Locale.toLanguageTag here: script, region and variants are kept, extensions
    (-u-..) and private use (-x-..) and anything unparsable after them are dropped.
    """
    if tag is None:
        return None
    parts = tag.strip(_TRIM).replace("_", "-").split("-")
    language = parts[0]
    if not _LANGUAGE.fullmatch(language) or language.lower() == "und":
        return None
    out = [language.lower()]
    i = 1
    if i < len(parts) and _SCRIPT.fullmatch(parts[i]):
        out.append(parts[i].capitalize())
        i += 1
    if i < len(parts) and _REGION.fullmatch(parts[i]):
        out.append(parts[i].upper())
        i += 1
    while i < len(parts) and _VARIANT.fullmatch(parts[i]):
        out.append(parts[i].lower())
        i += 1
    return "-".join(out)


def file_tag(name):
    """languages/pt_BR.json or EN.json -> pt-BR / en; None when not a valid translation file name.

    The name must equal the normalized tag apart from case and underscores, so a name
    with dropped parts (de_foo, xx_yy_zz) is invalid rather than silently shortened.
    """
    if name is None or not name.endswith(EXTENSION):
        return None
    base = name[len(FOLDER):] if name.startswith(FOLDER) else name
    base = base[: -len(EXTENSION)]
    if not base or "/" in base or "\\" in base:
        return None
    tag = normalize(base)
    if tag is None or tag.lower() != base.replace("_", "-").lower():
        return None
    return tag


def primary(tag):
    """Language part of a normalized tag: id-ID -> id."""
    return tag.split("-", 1)[0]
