"""In-memory stand-in for duygor_registry.github.GitHub: no network, records issues."""

from duygor_registry.github import Asset, DownloadTooLarge, GitHubError, Release, Repo, parse_release_url
from helpers import make_zip, valid_entries


def package(addon_id="demo", ver="1.0", **overrides):
    return make_zip(valid_entries(kimlik=addon_id, surum=ver, **overrides))


class FakeGitHub:
    def __init__(self):
        self.repos = {}
        self.releases = {}
        self.files = {}
        self.issues = []
        self.comments = []
        self.closed = []
        self.downloads = []
        self.failing = set()
        self.raising = {}
        self.renames = {}
        self._next_id = 100
        self._clock = 0

    def _tick(self):
        self._clock += 1
        return "2026-11-11T%02d:%02d:00Z" % (self._clock // 60, self._clock % 60)

    def add_repo(self, full_name, owner_id, owner_login=None, owner_type="User"):
        self.repos[full_name] = Repo(owner_id, owner_login or full_name.split("/")[0], owner_type, full_name)
        self.releases.setdefault(full_name, [])

    def rename(self, old, new, owner_login=None):
        """Like GitHub: the old name keeps redirecting to the renamed repository."""
        repo = self.repos.pop(old)
        self.repos[new] = Repo(repo.owner_id, owner_login or new.split("/")[0], repo.owner_type, new)
        self.releases[new] = self.releases.pop(old)
        self.renames[old] = new

    def _resolve(self, repo):
        while repo in self.renames and repo not in self.repos:
            repo = self.renames[repo]
        return repo

    def make_asset(self, repo, tag, name, data):
        self._next_id += 1
        url = "https://github.com/%s/releases/download/%s/%s" % (repo, tag, name)
        self.files[url] = data
        return Asset(self._next_id, name, len(data), self._tick(), url)

    def publish(self, repo, tag, data=None, name="demo.dgp", draft=False, prerelease=False, extra=(), body="What is new"):
        assets = [] if data is None else [self.make_asset(repo, tag, name, data)]
        assets += [self.make_asset(repo, tag, n, d) for n, d in extra]
        release = Release(repo, tag, draft, prerelease, body, assets, self._tick())
        self.releases[repo].append(release)
        return release

    def url_of(self, release):
        return "https://github.com/%s/releases/tag/%s" % (release.repo, release.tag)

    def replace_asset(self, release, asset_id, data):
        for i, a in enumerate(release.assets):
            if a.id == asset_id:
                self.files[a.url] = data
                release.assets[i] = Asset(a.id, a.name, len(data), self._tick(), a.url)
                return release.assets[i]
        raise KeyError(asset_id)

    def delete_asset(self, release, asset_id):
        release.assets[:] = [a for a in release.assets if a.id != asset_id]

    def _check(self, repo):
        if repo in self.failing:
            raise GitHubError(503, "unavailable")
        if repo in self.raising:
            raise self.raising[repo]
        return self._resolve(repo)

    def get_release_by_url(self, url):
        repo, tag = parse_release_url(url)
        repo = self._check(repo)
        for release in self.releases.get(repo, []):
            if release.tag == tag:
                return release
        raise GitHubError(404, "not found")

    def latest_release(self, repo):
        repo = self._check(repo)
        for release in reversed(self.releases.get(repo, [])):
            if not release.draft and not release.prerelease:
                return release
        return None

    def get_repo(self, repo):
        repo = self._check(repo)
        if repo not in self.repos:
            raise GitHubError(404, "not found")
        return self.repos[repo]

    def get_asset(self, repo, asset_id):
        repo = self._check(repo)
        for release in self.releases.get(repo, []):
            for a in release.assets:
                if a.id == asset_id:
                    return a
        return None

    def download(self, url, max_bytes):
        self.downloads.append(url)
        if url not in self.files:
            raise GitHubError(404, "not found")
        data = self.files[url]
        if len(data) > max_bytes:
            raise DownloadTooLarge(url)
        return data

    def comment(self, issue, body):
        self.comments.append((issue, body))

    def close(self, issue):
        self.closed.append(issue)

    def open_issue(self, title, body):
        self.issues.append((title, body))
        return len(self.issues)
