import http.client
import json
import os

import pytest

from duygor_registry import cli, registry
from fake_github import FakeGitHub, package

ALICE = 1001
REPO = "alice/duygor-addon-demo"


def submit_body(url="https://github.com/alice/duygor-addon-demo/releases/tag/v1.0", box="X"):
    return "### Release adresi (Release URL)\n\n%s\n\n### Sorumluluk (Responsibility)\n\n- [%s] Eklentimin sorumluluğu bana aittir, bilerek zararlı kod içermez. / I am responsible for my add-on.\n" % (url, box)


def remove_body(addon_id="demo", box="x"):
    return "### Eklenti kimliği (Add-on id)\n\n%s\n\n### Onay (Confirmation)\n\n- [%s] Kaldırmak istiyorum / I want to remove it\n" % (addon_id, box)


@pytest.fixture
def env(tmp_path):
    gh = FakeGitHub()
    gh.add_repo(REPO, ALICE)
    gh.publish(REPO, "v1.0", package())
    addons = tmp_path / "addons"
    addons.mkdir()
    return gh, str(addons), tmp_path


def run(capsys, argv, gh=None):
    assert cli.main(argv, gh=gh) == 0
    return json.loads(capsys.readouterr().out)


def issue_file(tmp_path, body, author_id=ALICE, login="alice"):
    path = tmp_path / "issue.json"
    path.write_text(json.dumps({"issue_number": 7, "author_id": author_id, "author_login": login, "body": body}), encoding="utf-8")
    return str(path)


def test_parse_sections_and_checkboxes():
    sections = cli.parse_sections(submit_body().replace("\n", "\r\n"))
    assert sections[cli.SUBMIT_URL] == "https://github.com/alice/duygor-addon-demo/releases/tag/v1.0"
    assert cli.is_checked(sections[cli.SUBMIT_RESPONSIBILITY])
    assert cli.is_checked("- [x] a")
    assert not cli.is_checked("- [ ] a")
    assert not cli.is_checked("- [X] a\n- [ ] b")
    assert not cli.is_checked("")
    assert not cli.is_checked("I agree")
    assert cli.field(cli.parse_sections("### Release adresi (Release URL)\n\n_No response_\n"), cli.SUBMIT_URL) == ""
    assert cli.field({}, cli.SUBMIT_URL) == ""
    sections = cli.parse_sections(remove_body(" `demo` "))
    assert cli.field(sections, cli.REMOVE_ID) == "`demo`" and cli.parse_remove(remove_body(" `demo` ")) == ("demo", None)
    assert cli.is_checked(sections[cli.REMOVE_CONFIRM])


def test_parse_submit_form():
    url, error = cli.parse_submit(submit_body())
    assert url == "https://github.com/alice/duygor-addon-demo/releases/tag/v1.0" and error is None


def test_parse_submit_missing_fields():
    for body in ["", "### Sorumluluk (Responsibility)\n\n- [X] ok\n", submit_body(url="_No response_")]:
        url, error = cli.parse_submit(body)
        assert url is None and error.message_tr and error.message_en and not error.ok
    url, error = cli.parse_submit("### Release adresi (Release URL)\n\nhttps://github.com/a/b/releases/tag/v1\n")
    assert url is None and not error.ok


def test_parse_remove_form():
    assert cli.parse_remove(remove_body()) == ("demo", None)
    for body in ["", remove_body(addon_id="_No response_"), remove_body(box=" ")]:
        addon_id, error = cli.parse_remove(body)
        assert addon_id is None and not error.ok and error.message_tr and error.message_en


def test_cli_register_and_remove(env, capsys):
    gh, addons, tmp = env
    result = run(capsys, ["register", "--issue-json", issue_file(tmp, submit_body()), "--addons", addons], gh)
    assert result["ok"] is True and result["changed"] is True
    assert result["message_tr"] in result["comment"] and result["message_en"] in result["comment"]
    assert os.path.exists(os.path.join(addons, "demo.json"))
    result = run(capsys, ["remove", "--issue-json", issue_file(tmp, remove_body(), 999, "mallory"), "--addons", addons], gh)
    assert result["ok"] is False and result["changed"] is False
    result = run(capsys, ["remove", "--issue-json", issue_file(tmp, remove_body()), "--addons", addons], gh)
    assert result["ok"] is True and result["changed"] is True


def test_cli_unchecked_responsibility_rejected(env, capsys):
    gh, addons, tmp = env
    result = run(capsys, ["register", "--issue-json", issue_file(tmp, submit_body(box=" ")), "--addons", addons], gh)
    assert result["ok"] is False and result["changed"] is False
    assert "Sorumluluk" in result["message_tr"] and "Responsibility" in result["message_en"]
    assert os.listdir(addons) == [] and gh.downloads == []


def test_cli_scan_prints_issues_without_opening(env, capsys):
    gh, addons, tmp = env
    run(capsys, ["register", "--issue-json", issue_file(tmp, submit_body()), "--addons", addons], gh)
    gh.publish(REPO, "v2", package("other", "2.0"))
    issues = run(capsys, ["scan", "--addons", addons], gh)
    assert len(issues) == 1 and set(issues[0]) == {"title", "body"} and "@alice" in issues[0]["body"]
    assert gh.issues == []
    assert run(capsys, ["scan", "--addons", addons], gh) == []


def test_cli_build_index(env, capsys):
    gh, addons, tmp = env
    run(capsys, ["register", "--issue-json", issue_file(tmp, submit_body()), "--addons", addons], gh)
    out = tmp / "site" / "index.json"
    config = tmp / "config.json"
    config.write_text('{"storeEnabled": false}', encoding="utf-8")
    assert cli.main(["build-index", "--out", str(out), "--addons", addons, "--config", str(config)]) == 0
    first = out.read_bytes()
    index = json.loads(first)
    assert index["storeEnabled"] is False and index["addons"] == []
    assert cli.main(["build-index", "--out", str(out), "--addons", addons, "--config", str(config)]) == 0
    assert out.read_bytes() == first
    assert cli.main(["build-index", "--out", str(out), "--addons", addons, "--config", str(tmp / "missing.json")]) == 0
    index = json.loads(out.read_bytes())
    assert index["storeEnabled"] is True and [a["id"] for a in index["addons"]] == ["demo"]
    config.write_text('{"storeEnabled": "no"}', encoding="utf-8")
    assert cli.main(["build-index", "--out", str(out), "--addons", addons, "--config", str(config)]) != 0


def test_cli_build_index_too_large_keeps_old_file(env, capsys, monkeypatch):
    gh, addons, tmp = env
    run(capsys, ["register", "--issue-json", issue_file(tmp, submit_body()), "--addons", addons], gh)
    out = tmp / "index.json"
    out.write_bytes(b"old\n")
    monkeypatch.setattr(registry, "MAX_INDEX_BYTES", 10)
    assert cli.main(["build-index", "--out", str(out), "--addons", addons, "--config", str(tmp / "missing.json")]) == 2
    assert out.read_bytes() == b"old\n"
    assert "too large" in capsys.readouterr().err


def test_cli_scan_survives_a_failing_record(env, capsys):
    gh, addons, tmp = env
    run(capsys, ["register", "--issue-json", issue_file(tmp, submit_body()), "--addons", addons], gh)
    gh.add_repo("alice/zzz", ALICE)
    gh.publish("alice/zzz", "v1", package("zzz", "1.0"))
    run(capsys, ["register", "--issue-json", issue_file(tmp, submit_body("https://github.com/alice/zzz/releases/tag/v1")), "--addons", addons], gh)
    gh.publish(REPO, "v2", package("other", "2.0"))
    gh.raising["alice/zzz"] = http.client.IncompleteRead(b"")
    issues = run(capsys, ["scan", "--addons", addons], gh)
    assert len(issues) == 1 and issues[0]["title"].startswith("demo 2.0")


def test_parse_body_as_github_renders_the_forms():
    submit = "### Release adresi (Release URL)\n\nhttps://github.com/alice/duygor-addon-demo/releases/tag/v1.0\n\n### Sorumluluk (Responsibility)\n\n- [X] Eklentimin sorumluluğu bana aittir, bilerek zararlı kod içermez. / I am responsible for my add-on and it does not knowingly contain harmful code.\n"
    assert cli.parse_submit(submit) == ("https://github.com/alice/duygor-addon-demo/releases/tag/v1.0", None)
    remove = "### Eklenti kimliği (Add-on id)\n\ndemo\n\n### Onay (Confirmation)\n\n- [X] Bu eklentiyi mağazadan kaldırmak istiyorum. / I want to remove this add-on from the store.\n"
    assert cli.parse_remove(remove) == ("demo", None)
    unticked = submit.replace("- [X]", "- [ ]")
    assert cli.parse_submit(unticked)[0] is None


def test_cli_register_twice_is_idempotent_success(env, capsys):
    gh, addons, tmp_path = env
    path = issue_file(tmp_path, submit_body())
    first = run(capsys, ["register", "--issue-json", path, "--addons", addons], gh)
    second = run(capsys, ["register", "--issue-json", path, "--addons", addons], gh)
    assert first["ok"] and first["changed"]
    assert second["ok"] and not second["changed"]
