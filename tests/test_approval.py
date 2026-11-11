"""First-submission author approval: pending records, maintainer decisions, and the sweep's selection."""

import contextlib
import io
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


def test_untrusted_author_holds_at_most_max_pending(env, capsys):
    for n in range(registry.MAX_PENDING + 1):
        repo = "alice/a%d" % n
        env.gh.add_repo(repo, ALICE)
        result = submit(env, capsys, env.gh.url_of(env.gh.publish(repo, "v1", package("a%d" % n))))
        if n < registry.MAX_PENDING:
            assert result["pending"] is True
    assert result["ok"] is False and "at most 3 add-ons awaiting approval" in result["message_en"] and "en fazla 3" in result["message_tr"]
    assert stored(env, "a3") is None
    # Resubmitting one already pending is not a new reservation.
    assert submit(env, capsys, env.gh.url_of(env.gh.releases["alice/a0"][0]))["pending"] is True
    decide(env, capsys, "approve", MAINTAINER, env.gh.url_of(env.gh.releases["alice/a0"][0]))
    assert submit(env, capsys, env.gh.url_of(env.gh.releases["alice/a3"][0]))["ok"] is True
    assert index(env) == {"a0": "1.0", "a1": "1.0", "a2": "1.0", "a3": "1.0"}


def test_pending_record_remembers_its_issues(env, capsys):
    submit(env, capsys)
    path = env.tmp / "issue.json"
    for number in (9, 8, 9):
        path.write_text(json.dumps({"issue_number": number, "author_id": ALICE, "author_login": "alice", "body": submit_body(env.url)}), encoding="utf-8")
        run(capsys, ["register", "--issue-json", str(path)], env.gh)
    assert stored(env)["pending_issues"] == [7, 8, 9]
    result = decide(env, capsys, "approve", MAINTAINER)
    assert [r["number"] for r in result["related"]] == [8, 9] and all("is published" in r["comment"] for r in result["related"])
    assert "pending_issues" not in stored(env) and "status" not in stored(env)


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
    # A decision label acts only on an issue awaiting approval; an unprocessed form is registered first.
    assert process_issue.decision_for(labelled("eklenti-gonder", "onaylandi")) is None
    assert process_issue.action_for(labelled("eklenti-gonder", "onaylandi")) == "register"
    assert process_issue.action_for(labelled("eklenti-gonder", "onay-bekliyor", "onaylandi")) == "approve"
    assert process_issue.action_for(labelled("eklenti-gonder", "onay-bekliyor", "reddedildi")) == "reject"
    assert process_issue.action_for(labelled("eklenti-gonder", "onay-bekliyor", "onaylandi", "reddedildi")) is None
    assert process_issue.decision_for(labelled("eklenti-kaldir", "onay-bekliyor", "onaylandi")) is None
    assert process_issue.action_for(labelled("onay-bekliyor", "onaylandi")) is None
    for issue in (labelled("eklenti-gonder", "onay-bekliyor", "onaylandi", state="closed"), labelled("eklenti-gonder", "onay-bekliyor", "onaylandi", user_type="Bot"), labelled("eklenti-gonder", "onay-bekliyor", "onaylandi", pr=True)):
        assert process_issue.action_for(issue) is None


class Hub:
    """The GitHub side of the workflow scripts (issues, labels, comments, events); the CLI runs in-process on env."""

    def __init__(self, env, monkeypatch):
        self.env = env
        self.issues = {}
        self.comments = {}
        self.events = {}
        self.log = []
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setattr(process_issue, "gh", self.gh)
        monkeypatch.setattr(process_issue, "cli", self.cli)
        monkeypatch.setattr(process_issue, "comment_from_file", self.comment)
        monkeypatch.setattr(process_issue, "push_registry", lambda: self.log.append(("push",)))
        monkeypatch.setattr(process_issue, "reset_tree", lambda: self.log.append(("reset",)))

    def open(self, number, body, *labels, author=ALICE, login="alice"):
        self.issues[number] = {"number": number, "state": "open", "user": {"id": author, "login": login, "type": "User"}, "body": body, "labels": []}
        self.events[number] = []
        for name in labels:
            self.label(number, name, author)
        return self.issues[number]

    def names(self, number):
        return {label["name"] for label in self.issues[number]["labels"]}

    def label(self, number, name, actor, actor_type="User"):
        if name not in self.names(number):
            self.issues[number]["labels"].append({"name": name})
        self.events[number].append(["labeled", name, actor, actor_type])

    def set_state(self, number, state):
        self.issues[number]["state"] = state
        self.events[number].append(["closed" if state == "closed" else "reopened", None, MAINTAINER, "User"])

    def comment(self, number, text):
        self.comments.setdefault(number, []).append(text)
        self.log.append(("comment", number))

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(list(args), gh=self.env.gh) == 0
        return SimpleNamespace(stdout=out.getvalue())

    def gh(self, *args, check=True):
        done = SimpleNamespace(stdout="", stderr="", returncode=0)
        if args[0] == "label":
            return done
        if args[0] == "issue":
            number = int(args[2])
            if args[1] == "edit":
                self.label(number, args[-1], 0, "Bot")
                self.log.append(("label", number, args[-1]))
            elif args[1] == "close":
                self.set_state(number, "closed")
                self.log.append(("close", number))
            return done
        if args[1] == "--method":
            parts = args[3].split("/")
            number, name = int(parts[4]), parts[6]
            if name not in self.names(number):
                return SimpleNamespace(stdout="", stderr="gh: Label does not exist (HTTP 404)", returncode=1)
            self.issues[number]["labels"] = [label for label in self.issues[number]["labels"] if label["name"] != name]
            self.events[number].append(["unlabeled", name, 0, "Bot"])
            self.log.append(("unlabel", number, name))
            return done
        parts = (args[2] if args[1] == "--paginate" else args[1]).split("?")[0].split("/")
        number = int(parts[4])
        if parts[-1] == "events":
            done.stdout = "\n".join(json.dumps(e) for e in self.events[number])
        elif parts[-1] == "comments":
            done.stdout = "\n".join(self.comments.get(number, []))
        else:
            done.stdout = json.dumps(self.issues[number])
        return done


@pytest.fixture
def hub(env, monkeypatch):
    return Hub(env, monkeypatch)


def review(hub, number, command, sender=None):
    import review_issue
    return review_issue.review(hub.issues[number], command, sender, None if sender is None else "User")


def test_script_labels_pending_before_commenting_once_per_version(env, hub):
    hub.open(5, submit_body(env.url), "eklenti-gonder")
    assert process_issue.process(hub.issues[5]) == 0
    assert hub.log.index(("label", 5, "onay-bekliyor")) < hub.log.index(("comment", 5))
    assert len(hub.comments[5]) == 1 and hub.comments[5][0].startswith("<!-- duygor-pending:demo:1.0 -->") and "awaiting approval" in hub.comments[5][0]
    assert hub.issues[5]["state"] == "open" and process_issue.action_for(hub.issues[5]) is None
    # A run that lost the label after commenting labels it again but does not repeat the comment.
    hub.issues[5]["labels"] = [{"name": "eklenti-gonder"}]
    assert process_issue.process(hub.issues[5]) == 0
    assert len(hub.comments[5]) == 1 and "onay-bekliyor" in hub.names(5)


def test_script_approval_closes_every_issue_of_the_author(env, hub):
    hub.open(5, submit_body(env.url), "eklenti-gonder")
    process_issue.process(hub.issues[5])
    env.gh.add_repo("alice/other", ALICE)
    other = env.gh.url_of(env.gh.publish("alice/other", "v1", package("other", "1.0")))
    hub.open(6, submit_body(other), "eklenti-gonder")
    process_issue.process(hub.issues[6])
    hub.label(5, "onaylandi", MAINTAINER)
    hub.label(6, "onaylandi", MAINTAINER)
    stale = json.loads(json.dumps(hub.issues[6]))
    assert review(hub, 5, "approve", MAINTAINER) == 0
    # The sweep's snapshot of issue 6 is stale; the live copy is closed, so it is not decided twice.
    import sweep_issues
    comments = len(hub.comments[6])
    assert sweep_issues.handle(stale) == 0 and len(hub.comments[6]) == comments
    for number in (5, 6):
        assert hub.issues[number]["state"] == "closed" and hub.names(number) == {"eklenti-gonder"}
        assert "is published" in hub.comments[number][-1] and "approved" in hub.comments[number][-1]
    assert index(env) == {"demo": "1.0", "other": "1.0"} and trusted_ids() == [ALICE]


def test_script_stale_decision_never_replays_on_a_reopened_issue(env, hub):
    hub.open(5, submit_body(env.url), "eklenti-gonder")
    process_issue.process(hub.issues[5])
    hub.label(5, "onaylandi", MAINTAINER)
    # Closed and reopened (say by its author, after editing the body) with the old label still on it.
    hub.set_state(5, "closed")
    hub.set_state(5, "reopened")
    hub.issues[5]["state"] = "open"
    assert process_issue.action_for(hub.issues[5]) == "approve"
    assert review(hub, 5, "approve") == 0
    assert stored(env)["status"] == registry.STATUS_PENDING and trusted_ids() == [] and hub.issues[5]["state"] == "open"
    # A label removed after it was added does not count either.
    hub.label(5, "onaylandi", MAINTAINER)
    hub.events[5].append(["unlabeled", "onaylandi", MAINTAINER, "User"])
    assert review(hub, 5, "approve") == 0 and trusted_ids() == []
    # A fresh label by the maintainer does.
    hub.label(5, "onaylandi", MAINTAINER)
    assert review(hub, 5, "approve") == 0 and trusted_ids() == [ALICE] and hub.issues[5]["state"] == "closed"


def test_script_event_sender_must_be_the_labeler(env, hub):
    hub.open(5, submit_body(env.url), "eklenti-gonder")
    process_issue.process(hub.issues[5])
    hub.label(5, "onaylandi", BOB)
    assert review(hub, 5, "approve", MAINTAINER) == 0
    assert stored(env)["status"] == registry.STATUS_PENDING and "onaylandi" in hub.names(5) and trusted_ids() == []


def test_script_removes_a_non_maintainer_label_before_the_consistency_check(env, hub):
    hub.open(5, submit_body(env.url), "eklenti-gonder")
    process_issue.process(hub.issues[5])
    hub.label(5, "reddedildi", MAINTAINER)
    for _ in range(2):
        hub.label(5, "onaylandi", BOB)
        assert review(hub, 5, "approve", BOB) == 0
        assert "onaylandi" not in hub.names(5)
    denials = [c for c in hub.comments[5] if c.startswith("<!-- duygor-review-denied -->")]
    assert len(denials) == 1 and "Only the registry maintainers" in denials[0]
    assert hub.log.count(("push",)) == 1 and stored(env)["status"] == registry.STATUS_PENDING
    assert review(hub, 5, "reject", MAINTAINER) == 0
    assert stored(env) is None and hub.issues[5]["state"] == "closed" and hub.names(5) == {"eklenti-gonder"}


def test_script_withdrawal_closes_the_submit_issue(env, hub):
    hub.open(5, submit_body(env.url), "eklenti-gonder")
    process_issue.process(hub.issues[5])
    hub.open(9, remove_body("demo"), "eklenti-kaldir")
    assert process_issue.process(hub.issues[9]) == 0
    assert stored(env) is None
    for number in (9, 5):
        assert hub.issues[number]["state"] == "closed" and "withdrawn" in hub.comments[number][-1]
    assert "onay-bekliyor" not in hub.names(5)


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
