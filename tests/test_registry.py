import hashlib
import io
import json
import os

import pytest

from duygor_registry import github, registry
from duygor_registry.github import GitHubError, parse_release_url
from fake_github import FakeGitHub, package
from helpers import make_zip, valid_entries

ALICE = 1001
BOB = 2002
REPO = "alice/duygor-addon-demo"


@pytest.fixture
def gh():
    fake = FakeGitHub()
    fake.add_repo(REPO, ALICE)
    return fake


@pytest.fixture
def addons(tmp_path):
    path = tmp_path / "addons"
    path.mkdir()
    return str(path)


def record(addons, addon_id="demo"):
    with open(os.path.join(addons, addon_id + ".json"), encoding="utf-8") as f:
        return json.load(f)


def register_demo(gh, addons, ver="1.0", repo=REPO, author=ALICE, data=None, **kw):
    release = gh.publish(repo, "v" + ver, data if data is not None else package(ver=ver), **kw)
    return release, registry.register(gh, addons, gh.url_of(release), author, repo.split("/")[0])


def assert_bilingual(outcome):
    assert outcome.message_tr and outcome.message_en and outcome.message_tr != outcome.message_en


def test_register_new(gh, addons):
    data = package(ver="1.2")
    release, outcome = register_demo(gh, addons, "1.2", data=data)
    assert outcome.ok and outcome.changed
    assert outcome.addon_id == "demo" and outcome.version == "1.2"
    assert "demo" in outcome.message_tr and "1.2" in outcome.message_en
    r = record(addons)
    assert r["id"] == "demo" and r["owner_id"] == ALICE and r["owner_login"] == "alice" and r["repo"] == REPO
    assert r["removed"] is False and r["hidden_reason"] is None and r["rejected_versions"] == []
    c = r["current"]
    asset = release.assets[0]
    assert c["version"] == "1.2" and c["min_version"] == 0 and c["author"] == "Eray"
    assert c["url"] == asset.url and c["asset_id"] == asset.id and c["asset_updated_at"] == asset.updated_at
    assert c["sha256"] == hashlib.sha256(data).hexdigest() and c["size"] == len(data)
    assert c["changelog"] == "What is new" and c["published"] == release.published_at
    assert c["source_language"] == "tr" and c["languages"] == [] and c["names"] == {"tr": "Demo"} and c["descriptions"] == {"tr": "Does things"}
    with open(os.path.join(addons, "demo.json"), encoding="utf-8") as f:
        text = f.read()
    assert text.endswith("}\n") and "\n  " in text


def test_register_prerelease_rejected(gh, addons):
    _, outcome = register_demo(gh, addons, prerelease=True)
    assert not outcome.ok and not outcome.changed
    assert_bilingual(outcome)
    _, outcome = register_demo(gh, addons, "1.1", draft=True)
    assert not outcome.ok
    assert os.listdir(addons) == []


def test_register_two_assets_rejected(gh, addons):
    _, outcome = register_demo(gh, addons, extra=[("other.dgp", package())])
    assert not outcome.ok and ".dgp" in outcome.message_en
    release = gh.publish(REPO, "v9", None, extra=[("readme.txt", b"x")])
    outcome = registry.register(gh, addons, gh.url_of(release), ALICE, "alice")
    assert not outcome.ok
    assert os.listdir(addons) == []


def test_register_not_owner_rejected(gh, addons):
    _, outcome = register_demo(gh, addons, author=BOB)
    assert not outcome.ok
    assert_bilingual(outcome)
    assert os.listdir(addons) == []
    assert gh.downloads == []


def test_register_org_rejected(gh, addons):
    gh.add_repo("acme/duygor-addon-demo", ALICE, "acme", "Organization")
    _, outcome = register_demo(gh, addons, repo="acme/duygor-addon-demo")
    assert not outcome.ok
    assert "organization" in outcome.message_en.lower() and "organizasyon" in outcome.message_tr.lower()
    assert os.listdir(addons) == []


def test_register_taken_id_rejected(gh, addons):
    register_demo(gh, addons)
    before = record(addons)
    gh.add_repo("bob/demo", BOB)
    _, outcome = register_demo(gh, addons, "2.0", repo="bob/demo", author=BOB)
    assert not outcome.ok and not outcome.changed
    assert "demo" in outcome.message_en
    assert record(addons) == before


def test_register_same_owner_other_repo_rejected(gh, addons):
    register_demo(gh, addons)
    before = record(addons)
    gh.add_repo("alice/other", ALICE)
    _, outcome = register_demo(gh, addons, "2.0", repo="alice/other")
    assert not outcome.ok and REPO in outcome.message_en
    assert record(addons) == before


def test_register_same_owner_same_repo_runs_update(gh, addons):
    register_demo(gh, addons)
    _, outcome = register_demo(gh, addons, "1.1")
    assert outcome.ok and outcome.changed and outcome.version == "1.1"
    assert record(addons)["current"]["version"] == "1.1"
    _, outcome = register_demo(gh, addons, "1.0")
    assert outcome.ok and not outcome.changed and outcome.version == "1.1"
    assert gh.issues == []


def test_register_bad_package_and_bad_url_rejected(gh, addons):
    _, outcome = register_demo(gh, addons, data=make_zip({"manifest.json": {"kimlik": "demo"}}))
    assert not outcome.ok and "main.lua" in outcome.message_en
    for url in ["http://github.com/alice/x/releases/tag/v1", "https://example.com/alice/x/releases/tag/v1", "https://github.com/alice/x/releases/v1", "not a url"]:
        outcome = registry.register(gh, addons, url, ALICE, "alice")
        assert not outcome.ok
        assert_bilingual(outcome)
    outcome = registry.register(gh, addons, "https://github.com/alice/duygor-addon-demo/releases/tag/missing", ALICE, "alice")
    assert not outcome.ok
    assert os.listdir(addons) == []


def test_register_removed_republished_by_owner_only(gh, addons):
    register_demo(gh, addons)
    assert registry.remove(gh, addons, "demo", ALICE).ok
    gh.add_repo("bob/demo", BOB)
    _, outcome = register_demo(gh, addons, "3.0", repo="bob/demo", author=BOB)
    assert not outcome.ok and record(addons)["removed"] is True
    outcome = registry.register(gh, addons, gh.url_of(gh.releases[REPO][0]), ALICE, "alice")
    assert outcome.ok and outcome.changed
    assert record(addons)["removed"] is False
    assert [a["id"] for a in registry.build_index(addons, True)["addons"]] == ["demo"]


def test_scan_newer_version_published(gh, addons):
    register_demo(gh, addons)
    release = gh.publish(REPO, "v1.1", package(ver="1.1"), body="Fixed things")
    outcomes = registry.scan(gh, addons)
    assert len(outcomes) == 1 and outcomes[0].ok and outcomes[0].version == "1.1" and outcomes[0].changed
    c = record(addons)["current"]
    assert c["version"] == "1.1" and c["asset_id"] == release.assets[0].id and c["changelog"] == "Fixed things"
    assert gh.issues == []
    downloads = len(gh.downloads)
    assert registry.scan(gh, addons) == []
    assert len(gh.downloads) == downloads


def test_scan_same_version_ignored(gh, addons):
    register_demo(gh, addons, "1.1")
    before = record(addons)["current"]
    gh.publish(REPO, "docs", package(ver="1.1"))
    gh.publish(REPO, "older", package(ver="1.0.9"))
    assert registry.scan(gh, addons) == []
    assert record(addons)["current"] == before
    downloads = len(gh.downloads)
    assert registry.scan(gh, addons) == []
    assert len(gh.downloads) == downloads
    gh.publish(REPO, "notes", None, extra=[("notes.txt", b"hi")])
    assert registry.scan(gh, addons) == []
    assert gh.issues == []


def test_scan_bad_package_issue_once(gh, addons):
    register_demo(gh, addons)
    gh.publish(REPO, "v2", make_zip({"manifest.json": {"kimlik": "demo", "surum": "2.0"}}))
    outcomes = registry.scan(gh, addons)
    assert len(outcomes) == 1 and not outcomes[0].ok and outcomes[0].notify_login == "alice"
    assert len(gh.issues) == 1
    title, body = gh.issues[0]
    assert "demo" in title and "2.0" in title and "@alice" in body and "main.lua" in body
    assert record(addons)["rejected_versions"] == ["2.0"]
    assert record(addons)["current"]["version"] == "1.0"
    registry.scan(gh, addons)
    assert len(gh.issues) == 1
    gh.publish(REPO, "v2b", make_zip({"manifest.json": {"kimlik": "demo", "surum": "2.0"}, "main.lua": "x", "languages/zz_bad_tag.json": "{}"}))
    registry.scan(gh, addons)
    assert len(gh.issues) == 1
    gh.publish(REPO, "v1.0.1", make_zip({"manifest.json": {"kimlik": "demo", "surum": "1.0.1"}, "main.lua": "x", "languages/zz_bad_tag.json": "{}"}))
    registry.scan(gh, addons)
    assert len(gh.issues) == 2
    gh.publish(REPO, "v0.9", make_zip({"manifest.json": {"kimlik": "demo", "surum": "0.9"}}))
    registry.scan(gh, addons)
    assert len(gh.issues) == 2


def test_issue_text_cannot_mention_others(gh, addons):
    register_demo(gh, addons)
    entries = valid_entries(kimlik="demo", surum="2.0")
    entries["../@mallory\nnext line"] = "x"
    gh.publish(REPO, "v2", make_zip(entries))
    registry.scan(gh, addons)
    body = gh.issues[0][1]
    assert body.startswith("@alice\n") and "@mallory" not in body and "@​mallory" in body
    assert "\nnext line" not in body


def test_scan_wrong_id_and_owner_change_rejected(gh, addons):
    register_demo(gh, addons)
    gh.publish(REPO, "v2", package("other", "2.0"))
    registry.scan(gh, addons)
    assert len(gh.issues) == 1 and "other" in gh.issues[0][1]
    gh.add_repo(REPO, BOB, "alice")
    gh.publish(REPO, "v3", package(ver="3.0"))
    outcomes = registry.scan(gh, addons)
    assert not outcomes[0].ok and len(gh.issues) == 2
    assert record(addons)["current"]["version"] == "1.0"


def test_scan_asset_changed_hides_and_issue_once(gh, addons):
    release, _ = register_demo(gh, addons)
    asset_id = release.assets[0].id
    gh.replace_asset(release, asset_id, package(ver="1.0", aciklama="Sneaky"))
    outcomes = registry.scan(gh, addons)
    assert len(outcomes) == 1 and not outcomes[0].ok and outcomes[0].notify_login == "alice"
    r = record(addons)
    assert r["hidden_reason"] == "asset_changed"
    assert registry.build_index(addons, True)["addons"] == []
    assert len(gh.issues) == 1 and "@alice" in gh.issues[0][1]
    downloads = len(gh.downloads)
    registry.scan(gh, addons)
    assert len(gh.issues) == 1 and len(gh.downloads) == downloads
    gh.replace_asset(release, asset_id, package(ver="1.0", aciklama="Sneakier"))
    registry.scan(gh, addons)
    assert len(gh.issues) == 2
    gh.delete_asset(release, asset_id)
    registry.scan(gh, addons)
    registry.scan(gh, addons)
    assert len(gh.issues) == 3 and record(addons)["hidden_reason"] == "asset_changed"


def test_scan_asset_reuploaded_identical_stays(gh, addons):
    data = package()
    release, _ = register_demo(gh, addons, data=data)
    asset = gh.replace_asset(release, release.assets[0].id, data)
    assert registry.scan(gh, addons) == []
    r = record(addons)
    assert r["hidden_reason"] is None and r["current"]["asset_updated_at"] == asset.updated_at
    assert gh.issues == []


def test_scan_new_version_unhides(gh, addons):
    release, _ = register_demo(gh, addons)
    gh.delete_asset(release, release.assets[0].id)
    registry.scan(gh, addons)
    assert record(addons)["hidden_reason"] == "asset_changed"
    gh.publish(REPO, "v1.1", package(ver="1.1"))
    outcomes = registry.scan(gh, addons)
    assert outcomes[0].ok
    r = record(addons)
    assert r["hidden_reason"] is None and r["reported_asset_change"] is None and r["current"]["version"] == "1.1"
    assert [a["version"] for a in registry.build_index(addons, True)["addons"]] == ["1.1"]


def test_scan_skips_removed_and_survives_errors(gh, addons):
    register_demo(gh, addons)
    gh.add_repo("alice/two", ALICE)
    release = gh.publish("alice/two", "v1", package("two", "1.0"))
    assert registry.register(gh, addons, gh.url_of(release), ALICE, "alice").ok
    gh.failing.add(REPO)
    gh.publish("alice/two", "v2", package("two", "2.0"))
    outcomes = registry.scan(gh, addons)
    assert {(o.addon_id, o.ok) for o in outcomes} == {("demo", False), ("two", True)}
    assert gh.issues == []
    gh.failing.clear()
    registry.remove(gh, addons, "two", ALICE)
    gh.publish("alice/two", "v3", package("two", "3.0"))
    assert registry.scan(gh, addons) == []
    assert record(addons, "two")["current"]["version"] == "2.0"


def test_remove_by_owner(gh, addons):
    register_demo(gh, addons)
    outcome = registry.remove(gh, addons, "demo", ALICE)
    assert outcome.ok and outcome.changed
    assert_bilingual(outcome)
    assert record(addons)["removed"] is True
    assert registry.build_index(addons, True)["addons"] == []
    outcome = registry.remove(gh, addons, "demo", ALICE)
    assert outcome.ok and not outcome.changed


def test_remove_by_other_rejected(gh, addons):
    register_demo(gh, addons)
    outcome = registry.remove(gh, addons, "demo", BOB)
    assert not outcome.ok and not outcome.changed
    assert record(addons)["removed"] is False
    assert not registry.remove(gh, addons, "missing", ALICE).ok
    assert not registry.remove(gh, addons, "../demo", ALICE).ok


INDEX_KEYS = {"id", "version", "minVersion", "author", "owner", "repo", "url", "sha256", "size", "changelog", "published", "sourceLanguage", "languages", "name", "description"}


def test_build_index_shape(gh, addons):
    entries = valid_entries(kimlik="zeta", surum="2.1", minDuygorSurumu=537)
    entries["languages/en.json"] = {"Demo": "Demo EN", "Does things": "Does things EN"}
    data = make_zip(entries)
    gh.add_repo("alice/zeta", ALICE)
    zeta = gh.publish("alice/zeta", "v2.1", data, name="zeta.dgp")
    assert registry.register(gh, addons, gh.url_of(zeta), ALICE, "alice").ok
    register_demo(gh, addons)
    index = registry.build_index(addons, True)
    assert index["schema"] == 1 and index["storeEnabled"] is True and index["movedTo"] is None
    assert set(index) == {"schema", "storeEnabled", "movedTo", "addons"}
    assert [a["id"] for a in index["addons"]] == ["demo", "zeta"]
    z = index["addons"][1]
    assert set(z) == INDEX_KEYS
    assert z["version"] == "2.1" and z["minVersion"] == 537 and z["author"] == "Eray" and z["owner"] == "alice" and z["repo"] == "alice/zeta"
    assert z["url"] == zeta.assets[0].url and z["url"].startswith("https://")
    assert z["sha256"] == hashlib.sha256(data).hexdigest() and z["size"] == len(data)
    assert z["changelog"] == "What is new" and z["published"] == zeta.published_at
    assert z["sourceLanguage"] == "tr" and z["languages"] == ["en"]
    assert z["name"] == {"tr": "Demo", "en": "Demo EN"} and z["description"] == {"tr": "Does things", "en": "Does things EN"}
    text = registry.index_json(index)
    assert text == registry.index_json(registry.build_index(addons, True)) and text.endswith("\n")
    assert json.loads(text) == index


def test_build_index_store_disabled(gh, addons):
    register_demo(gh, addons)
    index = registry.build_index(addons, False)
    assert index["storeEnabled"] is False and len(index["addons"]) == 1
    assert registry.build_index(os.path.join(addons, "missing"), True)["addons"] == []


def test_build_index_never_emits_invalid_entries(gh, addons):
    register_demo(gh, addons)
    path = os.path.join(addons, "demo.json")
    good = record(addons)
    for key, value in [("url", "http://x/demo.dgp"), ("sha256", "ABC"), ("size", 0), ("size", 20 * 1024 * 1024 + 1), ("version", "1.0-beta"), ("min_version", -1)]:
        bad = json.loads(json.dumps(good))
        bad["current"][key] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bad, f)
        assert registry.build_index(addons, True)["addons"] == [], key


def test_parse_release_url():
    assert parse_release_url("https://github.com/alice/duygor-addon-x/releases/tag/v1.0") == ("alice/duygor-addon-x", "v1.0")
    assert parse_release_url(" https://GitHub.com/a-b/c.d_e/releases/tag/release%2F2 \n") == ("a-b/c.d_e", "release/2")
    for url in ["http://github.com/a/b/releases/tag/v1", "https://github.com.evil.com/a/b/releases/tag/v1", "https://user@github.com/a/b/releases/tag/v1", "https://github.com:444/a/b/releases/tag/v1", "https://github.com/a/b/releases/tag/", "https://github.com/a/b/releases/latest", "https://github.com/a/b/tree/v1", "https://github.com/a/b/releases/tag/v1?x=1", "https://github.com/a/b/releases/tag/v1#x", "https://github.com/-a/b/releases/tag/v1", "https://github.com/a/../releases/tag/v1", "https://github.com/a/b/c/releases/tag/v1", "", None]:
        with pytest.raises(ValueError):
            parse_release_url(url)


def test_read_limited_aborts_when_exceeded():
    assert github.read_limited(io.BytesIO(b"abc"), 3) == b"abc"
    with pytest.raises(github.DownloadTooLarge):
        github.read_limited(io.BytesIO(b"a" * (github.CHUNK * 2 + 1)), github.CHUNK * 2)


def test_api_json_mapping():
    obj = {"tag_name": "v1", "draft": False, "prerelease": True, "body": None, "published_at": "2026-11-11T10:00:00Z", "assets": [{"id": 5, "name": "x.dgp", "size": 12, "updated_at": "2026-11-11T09:00:00Z", "browser_download_url": "https://github.com/a/b/releases/download/v1/x.dgp", "download_count": 3}]}
    release = github._release("a/b", obj)
    assert release == github.Release("a/b", "v1", False, True, "", [github.Asset(5, "x.dgp", 12, "2026-11-11T09:00:00Z", "https://github.com/a/b/releases/download/v1/x.dgp")], "2026-11-11T10:00:00Z")


def test_real_client_without_network():
    client = github.GitHub(token="t", repo="erray007/duygor-addons")
    headers = client.headers()
    assert headers["Authorization"] == "Bearer t" and headers["Accept"] == "application/vnd.github+json" and headers["X-GitHub-Api-Version"] == "2022-11-28"
    with pytest.raises(GitHubError):
        client.download("http://github.com/a/b/releases/download/v1/x.dgp", 10)
