"""First-submission author approval: pending records, maintainer decisions, and the sweep's selection."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

from duygor_registry import cli, registry, trust
from fake_github import FakeGitHub, package
from helpers import make_zip

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".github", "scripts"))
import process_issue  # noqa: E402

MAINTAINER = 7000
ALICE = 1001
BOB = 2002
REPO = "alice/duygor-addon-demo"


def submit_body(url):
    return "### Release adresi (Release URL)\n\n%s\n\n### Sorumluluk (Responsibility)\n\n- [X] I am responsible for my add-on.\n" % url


def remove_body(addon_id):
    return "### Eklenti kimliği (Add-on id)\n\n%s\n\n### Onay (Confirmation)\n\n- [X] I want to remove it\n" % addon_id


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"storeEnabled": True, "maintainers": [MAINTAINER]}), encoding="utf-8")
    (tmp_path / "addons").mkdir()
    gh = FakeGitHub()
    gh.add_repo(REPO, ALICE)
    first = gh.publish(REPO, "v1.0", package())
    return SimpleNamespace(gh=gh, tmp=tmp_path, url=gh.url_of(first))


def run(capsys, argv, gh):
    assert cli.main(argv, gh=gh) == 0
    return json.loads(capsys.readouterr().out)


def issue(env, body, author, login):
    path = env.tmp / "issue.json"
    path.write_text(json.dumps({"issue_number": 7, "author_id": author, "author_login": login, "body": body}), encoding="utf-8")
    return str(path)


def submit(env, capsys, url=None, author=ALICE, login="alice"):
    return run(capsys, ["register", "--issue-json", issue(env, submit_body(url or env.url), author, login)], env.gh)


def decide(env, capsys, command, sender, url=None, author=ALICE, login="alice"):
    return run(capsys, [command, "--issue-json", issue(env, submit_body(url or env.url), author, login), "--sender-id", str(sender)], env.gh)


def stored(env, addon_id="demo"):
    return registry.load(str(env.tmp / "addons"), addon_id)


def index(env):
    out = env.tmp / "site" / "index.json"
    assert cli.main(["build-index", "--out", str(out)]) == 0
    return {a["id"]: a["version"] for a in json.loads(out.read_text(encoding="utf-8"))["addons"]}


def trusted_ids():
    return [a["id"] for a in trust.load()]


def test_untrusted_register_is_pending_and_idempotent(env, capsys):
    result = submit(env, capsys)
    assert result["ok"] is True and result["pending"] is True and result["changed"] is True
    assert "awaiting approval" in result["comment"] and "onayını bekliyor" in result["comment"]
    assert stored(env)["status"] == registry.STATUS_PENDING and stored(env)["owner_id"] == ALICE
    assert index(env) == {} and trusted_ids() == []
    again = submit(env, capsys)
    assert again["pending"] is True and again["changed"] is False and again["comment"] == result["comment"]


def test_resubmit_while_pending_takes_the_newer_package(env, capsys):
    submit(env, capsys)
    newer = env.gh.publish(REPO, "v1.1", package(ver="1.1"))
    result = submit(env, capsys, env.gh.url_of(newer))
    assert result["pending"] is True and "1.1" in result["message_en"]
    assert stored(env)["current"]["version"] == "1.1" and stored(env)["status"] == registry.STATUS_PENDING
    # An older release never replaces the pending package.
    result = submit(env, capsys)
    assert result["pending"] is True and stored(env)["current"]["version"] == "1.1"


def test_pending_id_is_reserved_for_its_author(env, capsys):
    submit(env, capsys)
    env.gh.add_repo("bob/demo", BOB)
    other = env.gh.publish("bob/demo", "v1", package())
    result = submit(env, capsys, env.gh.url_of(other), BOB, "bob")
    assert result["ok"] is False and "another account" in result["message_en"]
    assert stored(env)["owner_id"] == ALICE


def test_maintainer_approve_publishes_latest_release_and_trusts_author(env, capsys):
    submit(env, capsys)
    env.gh.publish(REPO, "v1.2", package(ver="1.2"))
    result = decide(env, capsys, "approve", MAINTAINER)
    assert result["authorized"] is True and result["ok"] is True and result["changed"] is True
    assert "approved" in result["message_en"] and "onaylandı" in result["message_tr"] and "is published" in result["message_en"]
    r = stored(env)
    assert "status" not in r and registry.is_published(r) and r["current"]["version"] == "1.2"
    assert trusted_ids() == [ALICE] and trust.load()[0]["login"] == "alice"
    assert index(env) == {"demo": "1.2"}


def test_trusted_author_later_release_publishes_in_scan(env, capsys):
    submit(env, capsys)
    decide(env, capsys, "approve", MAINTAINER)
    env.gh.publish(REPO, "v2.0", package(ver="2.0"))
    assert run(capsys, ["scan"], env.gh) == []
    assert stored(env)["current"]["version"] == "2.0" and index(env) == {"demo": "2.0"}


def test_trusted_author_new_id_publishes_immediately(env, capsys):
    submit(env, capsys)
    decide(env, capsys, "approve", MAINTAINER)
    env.gh.add_repo("alice/other", ALICE)
    release = env.gh.publish("alice/other", "v1", package("other", "1.0"))
    result = submit(env, capsys, env.gh.url_of(release))
    assert result["ok"] is True and result["pending"] is False and "is published" in result["message_en"]
    assert index(env) == {"demo": "1.0", "other": "1.0"}


def test_non_maintainer_cannot_decide(env, capsys):
    submit(env, capsys)
    for command in ("approve", "reject"):
        for sender in (ALICE, BOB):
            result = decide(env, capsys, command, sender)
            assert result["authorized"] is False and result["ok"] is False and result["changed"] is False
            assert "Only the registry maintainers" in result["message_en"] and "yalnız depo yöneticileri" in result["message_tr"]
    assert stored(env)["status"] == registry.STATUS_PENDING and trusted_ids() == [] and index(env) == {}


def test_reject_frees_the_id(env, capsys):
    submit(env, capsys)
    result = decide(env, capsys, "reject", MAINTAINER)
    assert result["authorized"] is True and result["ok"] is True and result["changed"] is True
    assert "rejected" in result["message_en"] and "free again" in result["message_en"]
    assert stored(env) is None and trusted_ids() == []
    env.gh.add_repo("bob/demo", BOB)
    other = env.gh.publish("bob/demo", "v1", package())
    result = submit(env, capsys, env.gh.url_of(other), BOB, "bob")
    assert result["ok"] is True and result["pending"] is True and stored(env)["owner_id"] == BOB
    # Nothing left to reject for Alice.
    result = decide(env, capsys, "reject", MAINTAINER)
    assert result["ok"] is True and result["changed"] is False and stored(env)["owner_id"] == BOB


def test_reject_never_touches_a_published_record(env, capsys):
    submit(env, capsys, author=ALICE)
    decide(env, capsys, "approve", MAINTAINER)
    result = decide(env, capsys, "reject", MAINTAINER)
    assert result["ok"] is True and result["changed"] is False and index(env) == {"demo": "1.0"}


def test_scan_skips_pending(env, capsys):
    submit(env, capsys)
    before = stored(env)
    release = env.gh.releases[REPO][0]
    env.gh.replace_asset(release, release.assets[0].id, package(ver="1.0", aciklama="changed"))
    env.gh.publish(REPO, "v3", package(ver="3.0"))
    env.gh.downloads.clear()
    assert run(capsys, ["scan"], env.gh) == []
    assert env.gh.downloads == [] and stored(env) == before and stored(env)["hidden_reason"] is None


def test_maintainer_register_publishes_immediately(env, capsys):
    env.gh.add_repo("erray/demo", MAINTAINER)
    release = env.gh.publish("erray/demo", "v1", package())
    result = submit(env, capsys, env.gh.url_of(release), MAINTAINER, "erray")
    assert result["ok"] is True and result["pending"] is False and "status" not in stored(env)
    assert index(env) == {"demo": "1.0"} and trusted_ids() == []


def test_existing_records_without_status_stay_published():
    addons = os.path.join(HERE, "..", "addons")
    names = sorted(n[:-5] for n in os.listdir(addons) if n.endswith(".json"))
    assert names
    for record in registry.records(addons):
        assert "status" not in record and registry.is_published(record)
    assert sorted(a["id"] for a in registry.build_index(addons, True)["addons"]) == names
    assert registry.is_published({"status": "published"}) and not registry.is_published({"status": "pending"}) and not registry.is_published({"status": "bogus"})


def test_remove_withdraws_a_pending_submission(env, capsys):
    submit(env, capsys)
    result = run(capsys, ["remove", "--issue-json", issue(env, remove_body("demo"), ALICE, "alice")], env.gh)
    assert result["ok"] is True and result["changed"] is True and "withdrawn" in result["message_en"]
    assert stored(env) is None


def test_approve_with_nothing_pending_registers_as_trusted(env, capsys):
    result = decide(env, capsys, "approve", MAINTAINER)
    assert result["ok"] is True and "is published" in result["message_en"]
    assert trusted_ids() == [ALICE] and index(env) == {"demo": "1.0"}
    # A second approval (another issue of the same author) just reports it as published.
    result = decide(env, capsys, "approve", MAINTAINER)
    assert result["ok"] is True and result["changed"] is False and "already published" in result["message_en"]


def test_failed_approval_keeps_the_record_pending_and_the_author_trusted(env, capsys):
    submit(env, capsys)
    env.gh.publish(REPO, "v1.5", make_zip({"main.lua": "x"}))
    result = decide(env, capsys, "approve", MAINTAINER)
    assert result["authorized"] is True and result["ok"] is False and "submit the form again" in result["message_en"]
    assert stored(env)["status"] == registry.STATUS_PENDING and trusted_ids() == [ALICE] and index(env) == {}
    fixed = env.gh.publish(REPO, "v1.6", package(ver="1.6"))
    result = submit(env, capsys, env.gh.url_of(fixed))
    assert result["ok"] is True and result["pending"] is False and index(env) == {"demo": "1.6"}


def test_approve_rejects_a_latest_release_with_another_id(env, capsys):
    submit(env, capsys)
    env.gh.publish(REPO, "v2", package("renamed", "2.0"))
    result = decide(env, capsys, "approve", MAINTAINER)
    assert result["ok"] is False and "pending id `demo`" in result["message_en"]
    assert stored(env)["status"] == registry.STATUS_PENDING and stored(env, "renamed") is None


def test_several_pending_records_are_told_apart_by_the_form(env, capsys):
    submit(env, capsys)
    env.gh.add_repo("alice/other", ALICE)
    other = env.gh.publish("alice/other", "v1", package("other", "1.0"))
    submit(env, capsys, env.gh.url_of(other))
    result = decide(env, capsys, "reject", MAINTAINER, env.gh.url_of(other))
    assert result["ok"] is True and stored(env, "other") is None and stored(env)["status"] == registry.STATUS_PENDING


def test_trust_file_is_deterministic(tmp_path):
    path = str(tmp_path / "trusted.json")
    assert trust.add(BOB, "bob", path) and trust.add(ALICE, "alice", path)
    assert [a["id"] for a in trust.load(path)] == [ALICE, BOB]
    assert not trust.add(ALICE, "alice", path)
    first = trust.load(path)[0]["approved_at"]
    assert trust.add(ALICE, "alice2", path) and trust.load(path)[0] == {"id": ALICE, "login": "alice2", "approved_at": first}
    assert trust.is_trusted(ALICE, str(tmp_path / "missing.json"), path) and not trust.is_trusted(MAINTAINER, str(tmp_path / "missing.json"), path)


def test_malformed_trust_state_stops_the_cli(env, capsys):
    (env.tmp / "trusted_authors.json").write_text('[{"id": "1001"}]', encoding="utf-8")
    assert cli.main(["register", "--issue-json", issue(env, submit_body(env.url), ALICE, "alice")], gh=env.gh) == 2
    (env.tmp / "trusted_authors.json").unlink()
    (env.tmp / "config.json").write_text('{"maintainers": [true]}', encoding="utf-8")
    assert cli.main(["approve", "--issue-json", issue(env, submit_body(env.url), ALICE, "alice"), "--sender-id", "1"], gh=env.gh) == 2
    assert os.listdir(env.tmp / "addons") == []


def labelled(*labels, state="open", user_type="User", pr=False):
    issue = {"number": 1, "state": state, "user": {"id": ALICE, "login": "alice", "type": user_type}, "labels": [{"name": n} for n in labels]}
    if pr:
        issue["pull_request"] = {}
    return issue


def test_sweep_selection():
    assert process_issue.action_for(labelled("eklenti-gonder")) == "register"
    assert process_issue.action_for(labelled("eklenti-kaldir")) == "remove"
    # Awaiting approval: never resubmitted.
    assert process_issue.action_for(labelled("eklenti-gonder", "onay-bekliyor")) is None
    assert process_issue.command_for(labelled("eklenti-gonder", "onaylandi")) is None
    assert process_issue.action_for(labelled("eklenti-gonder", "onay-bekliyor", "onaylandi")) == "approve"
    assert process_issue.action_for(labelled("eklenti-gonder", "onay-bekliyor", "reddedildi")) == "reject"
    assert process_issue.action_for(labelled("eklenti-gonder", "onaylandi", "reddedildi")) is None
    assert process_issue.action_for(labelled("eklenti-kaldir", "onaylandi")) is None
    assert process_issue.action_for(labelled("onaylandi")) is None
    for issue in (labelled("eklenti-gonder", "onaylandi", state="closed"), labelled("eklenti-gonder", "onaylandi", user_type="Bot"), labelled("eklenti-gonder", "onaylandi", pr=True)):
        assert process_issue.action_for(issue) is None


@pytest.fixture
def scripts(monkeypatch):
    import review_issue
    calls = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "erray007/duygor-addons")
    monkeypatch.setattr(process_issue, "gh", lambda *a, check=True: calls.append(("gh",) + a) or SimpleNamespace(stdout="", stderr="", returncode=0))
    monkeypatch.setattr(process_issue, "comment_from_file", lambda n, text: calls.append(("comment", n, text)))
    monkeypatch.setattr(process_issue, "push_registry", lambda: calls.append(("push",)))
    return review_issue, calls


def test_review_script_denies_non_maintainers_and_applies_maintainer_decisions(scripts, monkeypatch):
    review_issue, calls = scripts
    monkeypatch.setattr(process_issue, "run_cli", lambda command, issue, *extra: {"authorized": False, "comment": "only maintainers"})
    assert review_issue.review(labelled("eklenti-gonder", "onay-bekliyor", "onaylandi"), "approve", BOB, "User") == 0
    assert ("push",) not in calls and any(c[0] == "gh" and "DELETE" in c and c[-1].endswith("/labels/onaylandi") for c in calls)
    assert [c for c in calls if c[0] == "comment"][0][2].startswith(review_issue.DENIED_MARKER)
    calls.clear()
    monkeypatch.setattr(process_issue, "run_cli", lambda command, issue, *extra: {"authorized": True, "comment": "approved", "extra": extra})
    assert review_issue.review(labelled("eklenti-gonder", "onay-bekliyor", "reddedildi"), "reject", MAINTAINER, "User") == 0
    kinds = [c[0] if c[0] != "gh" else c[1] for c in calls]
    assert kinds == ["push", "comment", "api", "issue"] and calls[2][-1].endswith("/labels/onay-bekliyor") and calls[3][1:3] == ("issue", "close")
    calls.clear()
    # Bots, a label the issue no longer has, and a mismatched command are ignored.
    assert review_issue.review(labelled("eklenti-gonder", "onaylandi"), "approve", MAINTAINER, "Bot") == 0
    assert review_issue.review(labelled("eklenti-gonder"), "approve", MAINTAINER, "User") == 0
    assert review_issue.review(labelled("eklenti-gonder", "onaylandi"), "reject", MAINTAINER, "User") == 0
    assert calls == []


def _workflow(name):
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(HERE, "..", ".github", "workflows", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_review_workflow_is_locked_down():
    flow = _workflow("review.yml")
    # PyYAML reads the bare key `on` as True.
    trigger = flow.get("on", flow.get(True))
    assert trigger == {"issues": {"types": ["labeled"]}}
    assert flow["permissions"] == {"contents": "write", "issues": "write"}
    assert flow["concurrency"]["group"] == "registry"
    job = flow["jobs"]["review"]
    assert "github.event.sender.type != 'Bot'" in job["if"] and "'onaylandi'" in job["if"] and "'reddedildi'" in job["if"]


@pytest.mark.parametrize("name", ["review.yml", "submit.yml", "scan.yml"])
def test_no_event_data_inside_run(name):
    for job in _workflow(name)["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step
