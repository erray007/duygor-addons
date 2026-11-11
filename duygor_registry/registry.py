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
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import package, version
from .github import DownloadTooLarge, GitHubError, parse_release_url
from .package import PackageError

PACKAGE_EXTENSION = ".dgp"
HIDDEN_ASSET_CHANGED = "asset_changed"
MISSING_ASSET = "missing"
INDEX_SCHEMA = 1
MAX_MIN_VERSION = 2**31 - 1
MAX_REASON = 500
# Add-ons an author who is not approved yet may hold awaiting approval at once.
MAX_PENDING = 3
# Store limits on top of the install rules: one add-on must never push index.json past what the app reads.
MAX_NAME = 100
MAX_AUTHOR = 100
MAX_DESCRIPTION = 2000
MAX_LANGUAGES = 64
MAX_CHANGELOG = 10000
# Every entry fits this budget, so at least 31 add-ons always fit the index whatever their content.
MAX_ENTRY_BYTES = 64 * 1024
MAX_INDEX_BYTES = 2 * 1024 * 1024
RECORD_EXTENSION = ".json"
# A record without "status" is published; "pending" holds an unapproved author's first submission:
# validated and its id reserved, but never scanned, hidden or put in the index until a maintainer approves.
STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
# Longest GitHub login (39) and owner/repo name (39 + 1 + 100): the budget check holds after any rename.
_PLACEHOLDER_OWNER = "x" * 39
_PLACEHOLDER_REPO = "x" * 140

_ID = package.ID
_SHA256 = re.compile(r"[0-9a-f]{64}")
# A Python str fails UTF-8 encoding only on lone surrogates, which JSON escapes such as \ud800 produce.
_SURROGATE = re.compile("[\ud800-\udfff]")
_log = logging.getLogger(__name__)


class IndexTooLarge(ValueError):
    """Last-resort guard: build_index never returns an index the app could not read.

    Entries are budgeted and dropped to fit, so only an empty index over the cap could raise it.
    """


@dataclass
class Outcome:
    ok: bool
    addon_id: str | None
    version: str | None
    message_tr: str
    message_en: str
    notify_login: str | None = None
    changed: bool = False
    pending: bool = False
    # (issue number, Outcome) of other issues the workflow answers and closes along with this one.
    related: list = field(default_factory=list)


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


def records(addons_dir, skip_corrupt=False):
    """Every record, in file name order.

    An unreadable record, or one whose id does not match its file name, raises ValueError; with
    skip_corrupt it is reported on stderr and left out instead.
    """
    try:
        names = sorted(os.listdir(addons_dir))
    except FileNotFoundError:
        return
    for name in names:
        if not name.endswith(RECORD_EXTENSION):
            continue
        addon_id = name[: -len(RECORD_EXTENSION)]
        try:
            record = load(addons_dir, addon_id)
        except (OSError, ValueError) as e:
            problem = "%s: %s" % (type(e).__name__, e)
        else:
            if isinstance(record, dict) and record.get("id") == addon_id:
                yield record
                continue
            problem = "not an object with the id " + addon_id
        if not skip_corrupt:
            raise ValueError("Corrupt registry record: %s (%s)" % (name, problem))
        sys.stderr.write("skipped %s: corrupt registry record (%s)\n" % (name, problem))


def is_published(record):
    """Only a missing or "published" status is published; anything else (pending or unknown) stays out of the store."""
    return record.get("status", STATUS_PUBLISHED) == STATUS_PUBLISHED


def _delete(addons_dir, addon_id):
    try:
        os.remove(_record_path(addons_dir, addon_id))
    except FileNotFoundError:
        return False
    return True


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


def _pending(addon_id, ver, changed):
    label = _label(addon_id, ver)
    return Outcome(True, addon_id, ver, label + " denetimden geçti ve kimlik size ayrıldı. İlk gönderiminiz olduğu için yayına girmeden önce depo yöneticisinin onayını bekliyor. Onaylanınca eklenti yayına girer ve bu issue kapanır. Onaydan sonra yeni sürümleriniz ve yeni eklentileriniz otomatik yayına girer.", label + " passed the checks and the id is reserved for you. As this is your first submission, it is awaiting approval by the registry maintainer before it is published. Once approved, the add-on is published and this issue is closed. After that, your new releases and new add-ons publish automatically.", None, changed, True)


def _github_down(addon_id=None, ver=None):
    return _register_rejected("GitHub'a ulaşılamadı. Biraz sonra formu yeniden gönderin.", "GitHub could not be reached. Submit the form again a little later.", addon_id, ver)


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


def _check_text(text, limit, what_tr, what_en):
    if limit is not None and len(text) > limit:
        raise _limit_error(what_tr, what_en, limit)
    if _SURROGATE.search(text):
        raise PackageError("encoding", "%s UTF-8'e çevrilemeyen geçersiz bir karakter içeriyor (eşi olmayan vekil, ör. \\ud800)." % what_tr, "%s contains an invalid character that cannot be encoded as UTF-8 (a lone surrogate such as \\ud800)." % what_en)


def _check_texts(texts, limit, what_tr, what_en):
    for tag in sorted(texts):
        _check_text(texts[tag], limit, "%s (%s)" % (what_tr, tag), "%s (%s)" % (what_en, tag))


def _inspect(data, release, asset):
    """package.inspect plus the limits the app's index parser applies on top of it and the store's size limits."""
    info = package.inspect(data)
    if not 0 <= info.min_version <= MAX_MIN_VERSION:
        raise PackageError("min_version", "manifest.json: minDuygorSurumu geçersiz: %d" % info.min_version, "manifest.json: minDuygorSurumu is invalid: %d" % info.min_version)
    if len(info.languages) > MAX_LANGUAGES:
        raise PackageError("limit", "en fazla %d dil dosyası olabilir, %d bulundu." % (MAX_LANGUAGES, len(info.languages)), "at most %d language files are allowed, found %d." % (MAX_LANGUAGES, len(info.languages)))
    _check_text(info.author, MAX_AUTHOR, "manifest.json: yazar", "manifest.json: the author")
    _check_texts(info.names, MAX_NAME, "Eklenti adı", "The add-on name")
    _check_texts(info.descriptions, MAX_DESCRIPTION, "Eklenti açıklaması", "The add-on description")
    _check_text(_changelog(release), None, "Release notları", "The release notes")
    # Measured as build_index would emit it, with the longest owner and repository names GitHub allows.
    try:
        size = _entry_size(_entry_fields(info.id, _PLACEHOLDER_OWNER, _PLACEHOLDER_REPO, _current(info, asset, release)))
    except UnicodeEncodeError:
        raise PackageError("encoding", "Release bilgileri UTF-8'e çevrilemeyen geçersiz bir karakter içeriyor.", "The release details contain an invalid character that cannot be encoded as UTF-8.") from None
    if size > MAX_ENTRY_BYTES:
        raise PackageError("entry_size", "eklenti mağaza dizininde %d bayt yer tutuyor, en fazla %d olabilir. Adları, açıklamaları ya da Release notlarını kısaltın (denetim karakterleri 6 bayt sayılır)." % (size, MAX_ENTRY_BYTES), "the add-on takes %d bytes in the store index, at most %d are allowed. Shorten the names, descriptions or release notes (control characters count as 6 bytes)." % (size, MAX_ENTRY_BYTES))
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
            info = _inspect(data, release, asset)
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


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_record(info, asset, release, repo, full_name):
    # registered_at is set once here and never changed: it ranks established add-ons in build_index.
    return {"id": info.id, "registered_at": _utc_now(), "owner_id": repo.owner_id, "owner_login": repo.owner_login, "repo": full_name, "removed": False, "hidden_reason": None, "current": _current(info, asset, release), "rejected_versions": [], "reported_asset_change": None, "reported_owner_mismatch": None, "checked_asset": None}


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


def _explicit_outcome(record, events, was_removed, changed, was_pending=False):
    for kind in ("rejected", "hidden", "published"):
        for event in events:
            if event.kind == kind:
                return _event_outcome(record, event, changed)
    current = record["current"]
    label = _label(record["id"], current["version"])
    if record.get("hidden_reason"):
        return Outcome(False, record["id"], current["version"], label + " şu anda mağazada gizli: yayındaki .dgp dosyası değişti. Daha büyük sürüm numaralı yeni bir Release yayınlayın.", label + " is currently hidden from the store: the published .dgp file changed. Publish a new Release with a higher version.", None, changed)
    if was_pending:
        tr, en = label + " yayında.", label + " is published."
    else:
        tr = label + (" yeniden yayında." if was_removed else " zaten yayında.")
        en = label + (" is published again." if was_removed else " is already published.")
    ignored = next((e for e in events if e.kind == "ignored"), None)
    if ignored is not None and ignored.version and ignored.version != current["version"]:
        tr += " Bu Release'teki %s sürümü daha yeni değil, yok sayıldı." % ignored.version
        en += " Version %s in this release is not newer, so it was ignored." % ignored.version
    return Outcome(True, record["id"], current["version"], tr, en, None, changed)


# Public API

def _fetch_release(gh, release_url):
    """The release a form names: -> (release, None) or (None, rejection Outcome)."""
    try:
        parse_release_url(release_url)
    except ValueError:
        return None, _register_rejected("Release adresi geçersiz. Biçim: https://github.com/<sahip>/<repo>/releases/tag/<etiket>", "The release URL is invalid. Format: https://github.com/<owner>/<repo>/releases/tag/<tag>")
    try:
        release = gh.get_release_by_url(release_url)
    except GitHubError:
        release = None
    if release is None:
        return None, _register_rejected("Release bulunamadı ya da okunamadı. Yayınlanmış ve herkese açık bir repoda olmalı.", "The release was not found or could not be read. It must be published, in a public repository.")
    return release, None


def _check_release(gh, release, author_id):
    """Every check of a submission that does not depend on the registry: -> ((asset, repo, info), None) or (None, rejection)."""
    if release.draft or release.prerelease:
        return None, _register_rejected("Release taslak ya da ön sürüm (pre-release). Normal bir Release yayınlayın.", "The release is a draft or a pre-release. Publish a normal release.")
    assets = _package_assets(release)
    if len(assets) != 1:
        return None, _register_rejected("Release'te tam olarak bir .dgp dosyası olmalı, %d bulundu." % len(assets), "The release must have exactly one .dgp file, found %d." % len(assets))
    asset = assets[0]
    try:
        repo = gh.get_repo(release.repo)
    except GitHubError:
        return None, _register_rejected("`%s` reposu okunamadı. Biraz sonra formu yeniden gönderin." % release.repo, "The repository `%s` could not be read. Submit the form again a little later." % release.repo)
    if repo.owner_type != "User":
        return None, _register_rejected("organizasyon repoları bu sürümde desteklenmiyor. Eklentiyi kendi kullanıcı hesabınızdaki bir repodan yayınlayın.", "organization repositories are not supported yet. Publish the add-on from a repository owned by your user account.")
    if repo.owner_id != author_id:
        return None, _register_rejected("formu gönderen hesap `%s` reposunun sahibi değil." % release.repo, "the account that submitted the form does not own the repository `%s`." % release.repo)
    try:
        info = _inspect(_fetch(gh, asset), release, asset)
    except PackageError as e:
        return None, _register_rejected("paket geçersiz: " + _safe(e.message_tr), "the package is invalid: " + _safe(e.message_en))
    except GitHubError:
        return None, _register_rejected("Release dosyası indirilemedi. Biraz sonra formu yeniden gönderin.", "The release file could not be downloaded. Submit the form again a little later.")
    return (asset, repo, info), None


def _submission(gh, release_url, author_id):
    """The fully checked release of a form: -> ((release, asset, repo, info), None) or (None, rejection)."""
    release, outcome = _fetch_release(gh, release_url)
    if outcome is not None:
        return None, outcome
    checked, outcome = _check_release(gh, release, author_id)
    if outcome is not None:
        return None, outcome
    return (release,) + checked, None


def _refresh_pending(record, release, asset, repo, info):
    """Nothing of a pending record is public yet, so it simply takes the checked package unless that is older."""
    _refresh_names(record, repo)
    record["removed"] = False
    if not version.is_newer((record.get("current") or {}).get("version"), info.version):
        record["current"] = _current(info, asset, release)
        record["hidden_reason"] = None
        record["reported_asset_change"] = None
        record["checked_asset"] = None
        record["rejected_versions"] = []


def _pending_of(addons_dir, author_id):
    return [r for r in records(addons_dir, skip_corrupt=True) if not is_published(r) and r.get("owner_id") == author_id]


def _track_issue(record, issue_number):
    """Remembers the submit issues waiting on a pending record, so publishing or deleting it can close them all."""
    issues = record.setdefault("pending_issues", [])
    if _is_int(issue_number) and issue_number not in issues:
        issues.append(issue_number)
        issues.sort()


def _related(record, outcome, issue_number):
    """outcome with the record's other waiting issues attached (the current issue is answered directly)."""
    outcome.related = [(n, outcome) for n in record.get("pending_issues", []) if n != issue_number]
    return outcome


def _register_checked(gh, addons_dir, release, asset, repo, info, author_id, trusted, expected_id=None, issue_number=None):
    if expected_id is not None and info.id != expected_id:
        return _register_rejected("en son Release'teki paketin kimliği `%s`, onay bekleyen kimlik `%s`." % (info.id, expected_id), "the package in the latest release has the id `%s`, not the pending id `%s`." % (info.id, expected_id), expected_id, info.version)
    full_name = repo.full_name or release.repo
    record = load(addons_dir, info.id)
    if record is None:
        record = _new_record(info, asset, release, repo, full_name)
        if trusted:
            return _published(info.id, info.version, save(addons_dir, record))
        if len(_pending_of(addons_dir, author_id)) >= MAX_PENDING:
            return _register_rejected("onay bekleyen en fazla %d eklentiniz olabilir. Depo yöneticisinin bekleyen gönderimlerinize karar vermesini bekleyin." % MAX_PENDING, "you can have at most %d add-ons awaiting approval. Wait until the registry maintainer has decided on your pending submissions." % MAX_PENDING, info.id, info.version)
        record["status"] = STATUS_PENDING
        _track_issue(record, issue_number)
        return _pending(info.id, info.version, save(addons_dir, record))
    if record["owner_id"] != author_id:
        return _register_rejected("bu kimlik başka bir hesaba ait.", "this id belongs to another account.", info.id, info.version)
    try:
        same = _same_repo(gh, record, repo, full_name)
    except GitHubError:
        return _github_down(info.id, info.version)
    if not same:
        return _register_rejected("bu kimlik `%s` reposuna bağlı, başka bir repodan yayınlanamaz." % record["repo"], "this id is bound to the repository `%s` and cannot be published from another one." % record["repo"], info.id, info.version)
    was_pending = not is_published(record)
    waiting = dict(record)
    if was_pending:
        _refresh_pending(record, release, asset, repo, info)
        if not trusted:
            _track_issue(record, issue_number)
            return _pending(record["id"], record["current"]["version"], save(addons_dir, record))
        del record["status"]
        record.pop("pending_issues", None)
    was_removed = bool(record.get("removed"))
    record["removed"] = False
    _refresh_names(record, repo)
    try:
        events = _scan_record(gh, record, release, True, info, repo)
    except GitHubError:
        return _github_down(info.id, info.version)
    outcome = _explicit_outcome(record, events, was_removed, save(addons_dir, record), was_pending)
    return _related(waiting, outcome, issue_number) if was_pending else outcome


def register(gh, addons_dir, release_url, author_id, author_login, trusted=True, issue_number=None):
    """Handles a submit form: binds a new id to the author and repository, or updates an owned one.

    For an author who is not trusted (no maintainer approved them yet) a new id is only reserved:
    the record is saved with status pending and stays out of the store until approve(). An untrusted
    author holds at most MAX_PENDING such reservations. issue_number is the form's issue, remembered on
    a pending record so its issues can all be answered when it is published or deleted.
    """
    checked, outcome = _submission(gh, release_url, author_id)
    if outcome is not None:
        return outcome
    return _register_checked(gh, addons_dir, *checked, author_id, trusted, issue_number=issue_number)


def _pending_target(gh, addons_dir, release_url, author_id):
    """The pending record of the author a maintainer's decision applies to: -> (record or None, None) or (None, rejection).

    With one pending record it is the target whatever the form says; with several, the form's release tells which.
    """
    pending = _pending_of(addons_dir, author_id)
    if len(pending) <= 1:
        return (pending[0] if pending else None), None
    checked, outcome = _submission(gh, release_url, author_id)
    if outcome is not None:
        return None, outcome
    return next((r for r in pending if r["id"] == checked[3].id), None), None


def _publish_pending(gh, addons_dir, record, issue_number=None):
    """Publishes a pending record from its repository's latest release, checked again now.

    Only a publish answers the record's other waiting issues; after a failure they stay open, the
    record stays pending, and the author's next form (now trusted) publishes it and answers them.
    """
    try:
        release = gh.latest_release(record["repo"])
    except GitHubError:
        return _github_down(record["id"])
    if release is None:
        return _register_rejected("`%s` reposunda yayınlanmış bir Release yok." % record["repo"], "the repository `%s` has no published release." % record["repo"], record["id"])
    checked, outcome = _check_release(gh, release, record["owner_id"])
    if outcome is not None:
        return outcome
    return _register_checked(gh, addons_dir, release, *checked, record["owner_id"], True, record["id"], issue_number)


def _approved(outcome):
    tr = "Yazar onaylandı, yeni sürümleri ve yeni eklentileri artık otomatik yayına girer. " + outcome.message_tr
    en = "The author is approved; their new releases and new add-ons now publish automatically. " + outcome.message_en
    if not outcome.ok:
        tr += " Sorunu düzeltip formu yeniden gönderdiğinizde eklenti otomatik yayına girer."
        en += " Fix the problem and submit the form again; the add-on then publishes automatically."
    result = Outcome(outcome.ok, outcome.addon_id, outcome.version, tr, en, None, outcome.changed)
    result.related = [(n, result) for n, _ in outcome.related]
    return result


def approve(gh, addons_dir, release_url, author_id, author_login, issue_number=None):
    """A maintainer approved the form's author, who the caller has already added to the trusted authors.

    Every pending add-on of the author is published from its latest release, checked again now; the form's
    own one decides the main outcome, and with none pending the form is handled as a trusted submission.
    The issues of every published record come back in related. A failed publish leaves that record
    pending, so the author's next form publishes it.
    """
    record, outcome = _pending_target(gh, addons_dir, release_url, author_id)
    if outcome is None:
        outcome = register(gh, addons_dir, release_url, author_id, author_login, True, issue_number) if record is None else _publish_pending(gh, addons_dir, record, issue_number)
    result = _approved(outcome)
    target = record["id"] if record is not None else None
    for other in _pending_of(addons_dir, author_id):
        if other["id"] == target:
            continue
        done = _approved(_publish_pending(gh, addons_dir, other))
        result.changed = result.changed or done.changed
        result.related.extend(done.related)
    return result


def reject(gh, addons_dir, release_url, author_id, issue_number=None):
    """A maintainer rejected the form's first submission: the pending record is deleted, freeing its id."""
    record, outcome = _pending_target(gh, addons_dir, release_url, author_id)
    if outcome is not None:
        return outcome
    if record is None:
        return Outcome(True, None, None, "Onay bekleyen bir gönderim bulunamadı, hiçbir şey değişmedi.", "No submission awaiting approval was found; nothing was changed.")
    ver = (record.get("current") or {}).get("version")
    label = _label(record["id"], ver)
    outcome = Outcome(True, record["id"], ver, label + " reddedildi: depo yöneticisi ilk gönderimi onaylamadı, eklenti yayına alınmadı. Kimlik serbest bırakıldı.", label + " was rejected: the registry maintainer did not approve the first submission, so the add-on was not published. The id is free again.", None, _delete(addons_dir, record["id"]))
    return _related(record, outcome, issue_number)


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
    if not is_published(record):
        # The remove form is its own issue, so every submit issue of the record is answered and closed.
        return _related(record, Outcome(True, addon_id, ver, label + " için onay bekleyen gönderim sahibi tarafından geri çekildi, kimlik serbest bırakıldı.", "The submission of %s awaiting approval was withdrawn by its owner and the id is free again." % label, None, _delete(addons_dir, addon_id)), None)
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
    """Hourly scan of every published record; issues are opened through gh.open_issue.

    Pending records are skipped entirely: nothing of them is public, and approve() checks them again.
    """
    outcomes = []
    for record in list(records(addons_dir)):
        if record.get("removed") or not is_published(record):
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


def _entry_fields(addon_id, owner, repo, c):
    return {"id": addon_id, "version": c["version"], "minVersion": c["min_version"], "author": c["author"], "owner": owner, "repo": repo, "url": c["url"], "sha256": c["sha256"], "size": c["size"], "changelog": c["changelog"], "published": c["published"], "sourceLanguage": c["source_language"], "languages": c["languages"], "name": c["names"], "description": c["descriptions"]}


def _entry(record):
    """The index entry of a record, or None when it would not pass the app's own entry checks."""
    c = record.get("current")
    if not isinstance(c, dict):
        return None
    try:
        e = _entry_fields(record["id"], record["owner_login"], record["repo"], c)
    except KeyError:
        return None
    valid = isinstance(e["id"], str) and _ID.fullmatch(e["id"]) and version.is_valid(e["version"]) and _is_int(e["minVersion"]) and 0 <= e["minVersion"] <= MAX_MIN_VERSION and isinstance(e["url"], str) and e["url"].startswith("https://") and isinstance(e["sha256"], str) and _SHA256.fullmatch(e["sha256"]) and _is_int(e["size"]) and 0 < e["size"] <= package.MAX_BYTES
    return e if valid else None


def _compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entry_size(entry):
    """Bytes the entry takes inside index.json; UnicodeEncodeError on text that is not valid UTF-8."""
    return len(_compact(entry).encode("utf-8"))


def _since(record, entry):
    """When the add-on joined the store: registered_at, or for older records their release time; None if unknown."""
    for value in (record.get("registered_at"), entry["published"]):
        if isinstance(value, str):
            return value
    return None


def _keep_order(item):
    """Established add-ons first: by registration time, then id; an add-on without a usable time comes last."""
    entry, _, since = item
    return (since is None, since or "", entry["id"])


def build_index(addons_dir, store_enabled):
    """The index.json document the app reads; removed, hidden and pending add-ons are left out.

    With the store disabled no record is read at all, so the kill switch always deploys. Corrupt record
    files and entries over MAX_ENTRY_BYTES are skipped; when the rest would reach MAX_INDEX_BYTES the most
    recently registered add-ons are dropped until it fits. Each skipped or dropped one is reported on stderr.
    """
    index = {"schema": INDEX_SCHEMA, "storeEnabled": bool(store_enabled), "movedTo": None, "addons": []}
    if store_enabled:
        kept = []
        for record in records(addons_dir, skip_corrupt=True):
            if record.get("removed") or record.get("hidden_reason") or not is_published(record):
                continue
            entry = _entry(record)
            if entry is None:
                continue
            try:
                size = _entry_size(entry)
            except UnicodeEncodeError:
                sys.stderr.write("skipped %s: index entry is not valid UTF-8\n" % entry["id"])
                continue
            if size > MAX_ENTRY_BYTES:
                sys.stderr.write("skipped %s: index entry is %d bytes (limit %d)\n" % (entry["id"], size, MAX_ENTRY_BYTES))
                continue
            kept.append((entry, size, _since(record, entry)))
        kept.sort(key=_keep_order)
        # The entries sit between the brackets of the empty list, separated by commas.
        total = len(index_json(index).encode("utf-8")) + sum(item[1] for item in kept) + max(len(kept) - 1, 0)
        while kept and total >= MAX_INDEX_BYTES:
            entry, size, _ = kept.pop()
            total -= size + (1 if kept else 0)
            sys.stderr.write("dropped %s: index.json would reach %d bytes (most recently registered add-ons go first)\n" % (entry["id"], MAX_INDEX_BYTES))
        index["addons"] = sorted((item[0] for item in kept), key=lambda e: e["id"])
    # The app refuses bodies over 4 MB, which would also hide the kill switch, so an oversized index is never built.
    size = len(index_json(index).encode("utf-8"))
    if size >= MAX_INDEX_BYTES:
        raise IndexTooLarge("index.json would be %d bytes, too large (limit %d)" % (size, MAX_INDEX_BYTES))
    return index


def index_json(index):
    """Deterministic serialization, so an unchanged registry yields a byte-identical file."""
    return _compact(index) + "\n"
