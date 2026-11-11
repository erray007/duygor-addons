"""Registration, hourly scan and index build for the DuyGör add-on store (spec 5.2-5.7).

State lives in addons/<id>.json, one record per add-on. An id is bound forever to the numeric
GitHub user id of its first registrant and to the repository it was first registered from;
nothing else can update it. GitHub is reached only through the `gh` object, so tests and the
CLI's dry run can substitute it.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass

from . import package, version
from .github import DownloadTooLarge, GitHubError, parse_release_url
from .package import PackageError

PACKAGE_EXTENSION = ".dgp"
HIDDEN_ASSET_CHANGED = "asset_changed"
MISSING_ASSET = "missing"
INDEX_SCHEMA = 1
MAX_MIN_VERSION = 2**31 - 1
MAX_REASON = 500
# Store limits on top of the install rules: one add-on must never push index.json past what the app reads.
MAX_NAME = 100
MAX_AUTHOR = 100
MAX_DESCRIPTION = 2000
MAX_LANGUAGES = 64
MAX_CHANGELOG = 10000
MAX_INDEX_BYTES = 2 * 1024 * 1024
RECORD_EXTENSION = ".json"

_ID = package.ID
_SHA256 = re.compile(r"[0-9a-f]{64}")
_log = logging.getLogger(__name__)


class IndexTooLarge(ValueError):
    """build_index refuses to produce an index the app could not read; nothing is published then."""


@dataclass
class Outcome:
    ok: bool
    addon_id: str | None
    version: str | None
    message_tr: str
    message_en: str
    notify_login: str | None = None
    changed: bool = False


@dataclass
class _Event:
    kind: str  # published, rejected, ignored or hidden
    version: str | None
    reason_tr: str = ""
    reason_en: str = ""
    issue: bool = False


# Records

def _record_path(addons_dir, addon_id):
    return os.path.join(addons_dir, addon_id + RECORD_EXTENSION)


def _serialize(record):
    return json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load(addons_dir, addon_id):
    try:
        with open(_record_path(addons_dir, addon_id), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save(addons_dir, record):
    """Writes the record atomically when its content changed; returns whether it did."""
    text = _serialize(record)
    path = _record_path(addons_dir, record["id"])
    try:
        with open(path, encoding="utf-8", newline="") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(addons_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=addons_dir, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def records(addons_dir):
    """Every record, in file name order; a record whose id does not match its file name is an error."""
    try:
        names = sorted(os.listdir(addons_dir))
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(RECORD_EXTENSION):
            continue
        addon_id = name[: -len(RECORD_EXTENSION)]
        record = load(addons_dir, addon_id)
        if not isinstance(record, dict) or record.get("id") != addon_id:
            raise ValueError("Corrupt registry record: " + name)
        yield record


# Messages

def _safe(text):
    """Outside text (zip entry names, errors) for a GitHub comment: one bounded line without @mentions."""
    text = " ".join(str(text).split())
    if len(text) > MAX_REASON:
        text = text[:MAX_REASON] + "…"
    return text.replace("@", "@​")


def _label(addon_id, ver):
    return " ".join(p for p in ("`%s`" % addon_id if addon_id else "", ver or "") if p)


def _register_rejected(tr, en, addon_id=None, ver=None):
    label = _label(addon_id, ver)
    if label:
        return Outcome(False, addon_id, ver, label + " reddedildi: " + tr, label + " was rejected: " + en)
    return Outcome(False, None, None, "Kayıt reddedildi: " + tr, "Registration was rejected: " + en)


def _published(addon_id, ver, changed):
    label = _label(addon_id, ver)
    return Outcome(True, addon_id, ver, label + " yayında.", label + " is published.", None, changed)


def _event_outcome(record, event, changed):
    label = _label(record["id"], event.version)
    if event.kind == "published":
        return _published(record["id"], event.version, changed)
    if event.kind == "rejected":
        return Outcome(False, record["id"], event.version, label + " reddedildi: " + event.reason_tr, label + " was rejected: " + event.reason_en, record["owner_login"], changed)
    # The registered login may now belong to whoever took the name over, so ownership problems mention nobody.
    notify = None if event.kind == "owner" else record["owner_login"]
    return Outcome(False, record["id"], event.version, label + ": " + event.reason_tr, label + ": " + event.reason_en, notify, changed)


def _issue_title(record, event):
    head = " ".join(p for p in (record["id"], event.version) if p)
    if event.kind == "hidden":
        return head + " listeden çıkarıldı / hidden from the store"
    if event.kind == "owner":
        return head + " sahiplik sorunu / ownership problem"
    return head + " reddedildi / rejected"


def _issue_body(outcome):
    mention = "@%s\n\n" % outcome.notify_login if outcome.notify_login else ""
    return "%s%s\n\n%s\n" % (mention, outcome.message_tr, outcome.message_en)


# Package checks

def _package_assets(release):
    return [a for a in release.assets if a.name.lower().endswith(PACKAGE_EXTENSION)]


def _fetch(gh, asset):
    if asset.size > package.MAX_BYTES:
        raise package._size_error()
    if not asset.url.startswith("https://"):
        raise PackageError("url", "Release dosyasının adresi https değil.", "The release file URL is not https.")
    try:
        return gh.download(asset.url, package.MAX_BYTES)
    except DownloadTooLarge:
        raise package._size_error() from None


def _limit_error(what_tr, what_en, limit):
    return PackageError("limit", "%s %d karakterden uzun olamaz." % (what_tr, limit), "%s cannot be longer than %d characters." % (what_en, limit))


def _check_texts(texts, limit, what_tr, what_en):
    for tag in sorted(texts):
        if len(texts[tag]) > limit:
            raise _limit_error("%s (%s)" % (what_tr, tag), "%s (%s)" % (what_en, tag), limit)


def _inspect(data):
    """package.inspect plus the limits the app's index parser applies on top of it and the store's size limits."""
    info = package.inspect(data)
    if not 0 <= info.min_version <= MAX_MIN_VERSION:
        raise PackageError("min_version", "manifest.json: minDuygorSurumu geçersiz: %d" % info.min_version, "manifest.json: minDuygorSurumu is invalid: %d" % info.min_version)
    if len(info.languages) > MAX_LANGUAGES:
        raise PackageError("limit", "en fazla %d dil dosyası olabilir, %d bulundu." % (MAX_LANGUAGES, len(info.languages)), "at most %d language files are allowed, found %d." % (MAX_LANGUAGES, len(info.languages)))
    if len(info.author) > MAX_AUTHOR:
        raise _limit_error("manifest.json: yazar", "manifest.json: the author", MAX_AUTHOR)
    _check_texts(info.names, MAX_NAME, "Eklenti adı", "The add-on name")
    _check_texts(info.descriptions, MAX_DESCRIPTION, "Eklenti açıklaması", "The add-on description")
    return info


def _changelog(release):
    """The release notes are the author's free text, so the index keeps at most MAX_CHANGELOG characters of them."""
    text = (release.body or "").strip()
    return text if len(text) <= MAX_CHANGELOG else text[: MAX_CHANGELOG - 1] + "…"


def _current(info, asset, release):
    return {"version": info.version, "min_version": info.min_version, "author": info.author, "url": asset.url, "sha256": info.sha256, "size": info.size, "asset_id": asset.id, "asset_updated_at": asset.updated_at, "changelog": _changelog(release), "published": release.published_at, "source_language": info.source_language, "languages": list(info.languages), "names": dict(info.names), "descriptions": dict(info.descriptions)}


def _reject_version(record, found, tr, en):
    """One issue per rejected version; a package without a readable version is reported once per asset."""
    rejected = record.setdefault("rejected_versions", [])
    issue = found is None or found not in rejected
    if found is not None and found not in rejected:
        rejected.append(found)
    return _Event("rejected", found, tr, en, issue)


def _try_update(gh, record, release, asset, info=None, repo=None):
    current_version = (record.get("current") or {}).get("version")
    data = None
    try:
        if info is None:
            data = _fetch(gh, asset)
            info = _inspect(data)
    except PackageError as e:
        found = package.peek_version(data) if data is not None else None
        # A broken release that is not newer is ignored like any other older release.
        if found is not None and not version.is_newer(found, current_version):
            return _Event("ignored", found)
        return _reject_version(record, found, _safe(e.message_tr), _safe(e.message_en))
    if not version.is_newer(info.version, current_version):
        return _Event("ignored", info.version)
    if info.id != record["id"]:
        return _reject_version(record, info.version, "paketteki kimlik `%s`, kayıtlı kimlik `%s`; kimlik değiştirilemez." % (info.id, record["id"]), "the package id is `%s` but the registered id is `%s`; the id cannot change." % (info.id, record["id"]))
    if repo is None:
        repo = gh.get_repo(record["repo"])
    # The repository may have been transferred or its name taken over since registration.
    if repo.owner_type != "User" or repo.owner_id != record["owner_id"]:
        return _reject_version(record, info.version, "`%s` reposu artık eklentinin kayıtlı sahibine ait değil." % record["repo"], "the repository `%s` no longer belongs to the registered owner of the add-on." % record["repo"])
    _refresh_names(record, repo)
    record["current"] = _current(info, asset, release)
    record["hidden_reason"] = None
    record["reported_asset_change"] = None
    record["checked_asset"] = None
    record["rejected_versions"] = [v for v in record.get("rejected_versions", []) if version.is_newer(v, info.version)]
    return _Event("published", info.version)


def _refresh_names(record, repo):
    """Follows renames of the owner account and the repository; only call once owner_id matched."""
    record["owner_login"] = repo.owner_login
    if repo.full_name:
        record["repo"] = repo.full_name


def _same_repo(gh, record, repo, full_name):
    """True when full_name is the registered repository, possibly renamed (GitHub redirects the old name)."""
    if record["repo"].lower() == full_name.lower():
        return True
    try:
        old = gh.get_repo(record["repo"])
    except GitHubError as e:
        if e.status == 404:
            return False
        raise
    return old.owner_id == record["owner_id"] == repo.owner_id and old.full_name.lower() == full_name.lower()


def _check_integrity(gh, record, release):
    """Hides the add-on when the published file was replaced or deleted; one issue per asset state."""
    current = record.get("current")
    if not current:
        return None
    asset_id = current["asset_id"]
    asset = next((a for a in release.assets if a.id == asset_id), None) if release is not None else None
    if asset is None:
        asset = gh.get_asset(record["repo"], asset_id)
    if asset is not None and asset.updated_at == current["asset_updated_at"] and asset.size == current["size"]:
        return None
    key = asset.updated_at if asset is not None else MISSING_ASSET
    hidden = record.get("hidden_reason") == HIDDEN_ASSET_CHANGED
    if hidden and record.get("reported_asset_change") == key:
        return None
    # A different size cannot have the same fingerprint, so only an equal size is worth downloading.
    if asset is not None and asset.size == current["size"] and asset.url.startswith("https://"):
        try:
            data = gh.download(asset.url, package.MAX_BYTES)
        except DownloadTooLarge:
            data = None
        if data is not None and hashlib.sha256(data).hexdigest() == current["sha256"]:
            current["asset_updated_at"] = asset.updated_at
            current["url"] = asset.url
            if hidden:
                record["hidden_reason"] = None
                record["reported_asset_change"] = None
            return None
    record["hidden_reason"] = HIDDEN_ASSET_CHANGED
    record["reported_asset_change"] = key
    return _Event("hidden", current["version"], "yayındaki .dgp dosyası değiştirildi ya da silindi, eklenti mağazadan geçici olarak çıkarıldı. Daha büyük sürüm numaralı yeni bir Release yayınlayınca geri gelir.", "the published .dgp file was changed or deleted, so the add-on is temporarily hidden from the store. It comes back when you publish a new Release with a higher version.", True)


def _scan_record(gh, record, release, explicit=False, info=None, repo=None):
    """Checks one record against a release; explicit (a form request) re-examines already checked assets."""
    events = []
    candidates = _package_assets(release) if release is not None and not release.draft and not release.prerelease else []
    current = record.get("current") or {}
    if len(candidates) == 1 and candidates[0].id != current.get("asset_id"):
        asset = candidates[0]
        key = {"id": asset.id, "updated_at": asset.updated_at}
        # Releases are examined once per asset, so an unrelated release is not downloaded every hour.
        if explicit or record.get("checked_asset") != key:
            event = _try_update(gh, record, release, asset, info, repo)
            if event.kind == "published":
                return [event]
            if not explicit:
                record["checked_asset"] = key
            events.append(event)
    event = _check_integrity(gh, record, release)
    if event is not None:
        events.append(event)
    return events


def _new_record(info, asset, release, repo, full_name):
    return {"id": info.id, "owner_id": repo.owner_id, "owner_login": repo.owner_login, "repo": full_name, "removed": False, "hidden_reason": None, "current": _current(info, asset, release), "rejected_versions": [], "reported_asset_change": None, "reported_owner_mismatch": None, "checked_asset": None}


def _scan_one(gh, record):
    """Scans one record, checking the repository owner first so a name taken over by someone else never alters it."""
    try:
        repo = gh.get_repo(record["repo"])
    except GitHubError as e:
        if e.status != 404:
            raise
        # The repository is gone; the integrity check then reports the missing file.
        repo = None
    if repo is not None:
        if repo.owner_type != "User" or repo.owner_id != record["owner_id"]:
            key = "%s:%s" % (repo.owner_type, repo.owner_id)
            if record.get("reported_owner_mismatch") == key:
                return []
            # Only the deduplication key is stored; the add-on itself stays exactly as it was.
            record["reported_owner_mismatch"] = key
            ver = (record.get("current") or {}).get("version")
            return [_Event("owner", ver, "`%s` reposu artık eklentinin kayıtlı sahibine ait değil (şimdiki sahip: %s %s). Eklenti olduğu gibi bırakıldı, sahiplik düzelene kadar güncelleme alınmaz. Depo yöneticisinin incelemesi gerekiyor." % (record["repo"], repo.owner_type, repo.owner_id), "the repository `%s` no longer belongs to the registered owner of the add-on (current owner: %s %s). The add-on is left as it is and gets no updates until ownership is fixed. The registry maintainer needs to review this." % (record["repo"], repo.owner_type, repo.owner_id), True)]
        if record.get("reported_owner_mismatch") is not None:
            record["reported_owner_mismatch"] = None
        _refresh_names(record, repo)
    return _scan_record(gh, record, gh.latest_release(record["repo"]), repo=repo)


def _explicit_outcome(record, events, was_removed, changed):
    for kind in ("rejected", "hidden", "published"):
        for event in events:
            if event.kind == kind:
                return _event_outcome(record, event, changed)
    current = record["current"]
    label = _label(record["id"], current["version"])
    if record.get("hidden_reason"):
        return Outcome(False, record["id"], current["version"], label + " şu anda mağazada gizli: yayındaki .dgp dosyası değişti. Daha büyük sürüm numaralı yeni bir Release yayınlayın.", label + " is currently hidden from the store: the published .dgp file changed. Publish a new Release with a higher version.", None, changed)
    tr = label + (" yeniden yayında." if was_removed else " zaten yayında.")
    en = label + (" is published again." if was_removed else " is already published.")
    ignored = next((e for e in events if e.kind == "ignored"), None)
    if ignored is not None and ignored.version and ignored.version != current["version"]:
        tr += " Bu Release'teki %s sürümü daha yeni değil, yok sayıldı." % ignored.version
        en += " Version %s in this release is not newer, so it was ignored." % ignored.version
    return Outcome(True, record["id"], current["version"], tr, en, None, changed)


# Public API

def register(gh, addons_dir, release_url, author_id, author_login):
    """Handles a submit form: binds a new id to the author and repository, or updates an owned one."""
    try:
        parse_release_url(release_url)
    except ValueError:
        return _register_rejected("Release adresi geçersiz. Biçim: https://github.com/<sahip>/<repo>/releases/tag/<etiket>", "The release URL is invalid. Format: https://github.com/<owner>/<repo>/releases/tag/<tag>")
    try:
        release = gh.get_release_by_url(release_url)
    except GitHubError:
        release = None
    if release is None:
        return _register_rejected("Release bulunamadı ya da okunamadı. Yayınlanmış ve herkese açık bir repoda olmalı.", "The release was not found or could not be read. It must be published, in a public repository.")
    if release.draft or release.prerelease:
        return _register_rejected("Release taslak ya da ön sürüm (pre-release). Normal bir Release yayınlayın.", "The release is a draft or a pre-release. Publish a normal release.")
    assets = _package_assets(release)
    if len(assets) != 1:
        return _register_rejected("Release'te tam olarak bir .dgp dosyası olmalı, %d bulundu." % len(assets), "The release must have exactly one .dgp file, found %d." % len(assets))
    asset = assets[0]
    try:
        repo = gh.get_repo(release.repo)
    except GitHubError:
        return _register_rejected("`%s` reposu okunamadı. Biraz sonra formu yeniden gönderin." % release.repo, "The repository `%s` could not be read. Submit the form again a little later." % release.repo)
    if repo.owner_type != "User":
        return _register_rejected("organizasyon repoları bu sürümde desteklenmiyor. Eklentiyi kendi kullanıcı hesabınızdaki bir repodan yayınlayın.", "organization repositories are not supported yet. Publish the add-on from a repository owned by your user account.")
    if repo.owner_id != author_id:
        return _register_rejected("formu gönderen hesap `%s` reposunun sahibi değil." % release.repo, "the account that submitted the form does not own the repository `%s`." % release.repo)
    try:
        info = _inspect(_fetch(gh, asset))
    except PackageError as e:
        return _register_rejected("paket geçersiz: " + _safe(e.message_tr), "the package is invalid: " + _safe(e.message_en))
    except GitHubError:
        return _register_rejected("Release dosyası indirilemedi. Biraz sonra formu yeniden gönderin.", "The release file could not be downloaded. Submit the form again a little later.")
    full_name = repo.full_name or release.repo
    record = load(addons_dir, info.id)
    if record is None:
        record = _new_record(info, asset, release, repo, full_name)
        return _published(info.id, info.version, save(addons_dir, record))
    if record["owner_id"] != author_id:
        return _register_rejected("bu kimlik başka bir hesaba ait.", "this id belongs to another account.", info.id, info.version)
    try:
        same = _same_repo(gh, record, repo, full_name)
    except GitHubError:
        return _register_rejected("GitHub'a ulaşılamadı. Biraz sonra formu yeniden gönderin.", "GitHub could not be reached. Submit the form again a little later.", info.id, info.version)
    if not same:
        return _register_rejected("bu kimlik `%s` reposuna bağlı, başka bir repodan yayınlanamaz." % record["repo"], "this id is bound to the repository `%s` and cannot be published from another one." % record["repo"], info.id, info.version)
    was_removed = bool(record.get("removed"))
    record["removed"] = False
    _refresh_names(record, repo)
    try:
        events = _scan_record(gh, record, release, True, info, repo)
    except GitHubError:
        return _register_rejected("GitHub'a ulaşılamadı. Biraz sonra formu yeniden gönderin.", "GitHub could not be reached. Submit the form again a little later.", info.id, info.version)
    return _explicit_outcome(record, events, was_removed, save(addons_dir, record))


def remove(gh, addons_dir, addon_id, author_id):
    """Handles a remove form: only the owner may remove; the id stays reserved for them."""
    if not isinstance(addon_id, str) or not _ID.fullmatch(addon_id):
        return Outcome(False, None, None, "Kaldırma reddedildi: eklenti kimliği geçersiz.", "Removal was rejected: the add-on id is invalid.")
    label = _label(addon_id, None)
    record = load(addons_dir, addon_id)
    if record is None:
        return Outcome(False, addon_id, None, "Kaldırma reddedildi: %s kimliğiyle kayıtlı bir eklenti yok." % label, "Removal was rejected: no add-on is registered with the id %s." % label)
    ver = (record.get("current") or {}).get("version")
    if record["owner_id"] != author_id:
        return Outcome(False, addon_id, ver, "Kaldırma reddedildi: %s eklentisini yalnız sahibi kaldırabilir." % label, "Removal was rejected: only the owner can remove %s." % label)
    if record.get("removed"):
        return Outcome(True, addon_id, ver, label + " zaten kaldırılmış.", label + " is already removed.")
    record["removed"] = True
    return Outcome(True, addon_id, ver, label + " mağazadan kaldırıldı. Kimlik size ayrılmış kalır, yeniden yayına almak için gönderme formunu tekrar doldurun.", label + " was removed from the store. The id stays reserved for you; submit the form again to republish it.", None, save(addons_dir, record))


def _scan_failed(record, tr, en, error):
    ver = (record.get("current") or {}).get("version")
    label = _label(record["id"], ver)
    detail = _safe(error)
    return Outcome(False, record["id"], ver, "%s: %s, sonraki taramada yeniden denenecek (%s)." % (label, tr, detail), "%s: %s, the next scan retries (%s)." % (label, en, detail))


def scan(gh, addons_dir):
    """Hourly scan of every published record; issues are opened through gh.open_issue."""
    outcomes = []
    for record in list(records(addons_dir)):
        if record.get("removed"):
            continue
        # One record must never stop the others: a failure leaves it unsaved and the next scan retries it.
        # Issues are opened before the save, so a failed save or issue can repeat an issue but never lose one.
        found = []
        try:
            for event in _scan_one(gh, record):
                if event.kind == "ignored":
                    continue
                outcome = _event_outcome(record, event, False)
                if event.issue:
                    gh.open_issue(_issue_title(record, event), _issue_body(outcome))
                found.append(outcome)
            changed = save(addons_dir, record)
        except GitHubError as e:
            outcomes.append(_scan_failed(record, "GitHub'a ulaşılamadı", "GitHub could not be reached", e))
            continue
        except Exception as e:
            _log.exception("scan of %s failed", record["id"])
            outcomes.append(_scan_failed(record, "beklenmeyen hata", "unexpected error", "%s: %s" % (type(e).__name__, e)))
            continue
        for outcome in found:
            outcome.changed = changed
        outcomes.extend(found)
    return outcomes


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _entry(record):
    """The index entry of a record, or None when it would not pass the app's own entry checks."""
    c = record.get("current")
    if not isinstance(c, dict):
        return None
    try:
        e = {"id": record["id"], "version": c["version"], "minVersion": c["min_version"], "author": c["author"], "owner": record["owner_login"], "repo": record["repo"], "url": c["url"], "sha256": c["sha256"], "size": c["size"], "changelog": c["changelog"], "published": c["published"], "sourceLanguage": c["source_language"], "languages": c["languages"], "name": c["names"], "description": c["descriptions"]}
    except KeyError:
        return None
    valid = isinstance(e["id"], str) and _ID.fullmatch(e["id"]) and version.is_valid(e["version"]) and _is_int(e["minVersion"]) and 0 <= e["minVersion"] <= MAX_MIN_VERSION and isinstance(e["url"], str) and e["url"].startswith("https://") and isinstance(e["sha256"], str) and _SHA256.fullmatch(e["sha256"]) and _is_int(e["size"]) and 0 < e["size"] <= package.MAX_BYTES
    return e if valid else None


def build_index(addons_dir, store_enabled):
    """The index.json document the app reads; removed and hidden add-ons are left out. Raises IndexTooLarge."""
    addons = []
    for record in records(addons_dir):
        if record.get("removed") or record.get("hidden_reason"):
            continue
        entry = _entry(record)
        if entry is not None:
            addons.append(entry)
    addons.sort(key=lambda e: e["id"])
    index = {"schema": INDEX_SCHEMA, "storeEnabled": bool(store_enabled), "movedTo": None, "addons": addons}
    # The app refuses bodies over 4 MB, which would also hide the kill switch, so an oversized index is never built.
    size = len(index_json(index).encode("utf-8"))
    if size >= MAX_INDEX_BYTES:
        raise IndexTooLarge("index.json would be %d bytes, too large (limit %d)" % (size, MAX_INDEX_BYTES))
    return index


def index_json(index):
    """Deterministic serialization, so an unchanged registry yields a byte-identical file."""
    return json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
