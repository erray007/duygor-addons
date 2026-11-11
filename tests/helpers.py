import io
import json
import zipfile


def make_zip(entries, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, content in entries.items():
            if isinstance(content, (dict, list)):
                content = json.dumps(content)
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return buf.getvalue()


def manifest(**overrides):
    base = {"kimlik": "demo", "ad": "Demo", "surum": "1.0", "yazar": "Eray", "aciklama": "Does things"}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def valid_entries(**manifest_overrides):
    return {"manifest.json": manifest(**manifest_overrides), "main.lua": "print('hi')"}
