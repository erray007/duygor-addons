"""Minimal GitHub REST client (urllib, standard library only) used by the registry.

Tests use an in-memory fake with the same methods; nothing here runs without a real network call.
"""

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT = 30
CHUNK = 64 * 1024
# API answers are small JSON documents; this only bounds a misbehaving server.
MAX_API_BYTES = 8 * 1024 * 1024
DEFAULT_REPO = "erray007/duygor-addons"
USER_AGENT = "duygor-registry"

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"
_NAME = r"[A-Za-z0-9._-]{1,100}"
_REPO = re.compile("(" + _OWNER + ")/(" + _NAME + ")")
_RELEASE_PATH = re.compile("/(" + _OWNER + ")/(" + _NAME + ")/releases/tag/(.+)")
# Every failure of a connection or a read: URLError and timeouts are OSError, IncompleteRead,
# BadStatusLine and LineTooLong are HTTPException, a malformed URL is ValueError.
_NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)
# What a JSON answer of an unexpected shape raises while it is mapped.
_SHAPE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, IndexError)


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__("GitHub %s: %s" % (status, message))
        self.status = status


class DownloadTooLarge(GitHubError):
    def __init__(self, url):
        super().__init__(413, "download exceeds the size limit: " + url)


@dataclass(frozen=True)
class Asset:
    id: int
    name: str
    size: int
    updated_at: str
    url: str


@dataclass(frozen=True)
class Release:
    repo: str
    tag: str
    draft: bool
    prerelease: bool
    body: str
    assets: list = field(default_factory=list)
    published_at: str = ""


@dataclass(frozen=True)
class Repo:
    owner_id: int
    owner_login: str
    owner_type: str
    full_name: str = ""


def check_repo(repo):
    """Validates an owner/name pair; returns it unchanged or raises ValueError."""
    if not isinstance(repo, str):
        raise ValueError("Invalid repository")
    m = _REPO.fullmatch(repo)
    if m is None or m.group(2) in (".", ".."):
        raise ValueError("Invalid repository: " + repo)
    return repo


def parse_release_url(url):
    """https://github.com/<owner>/<repo>/releases/tag/<tag> -> ("owner/repo", tag); ValueError otherwise."""
    if not isinstance(url, str):
        raise ValueError("Invalid release URL")
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme.lower() != "https" or parts.netloc.lower() != "github.com" or parts.query or parts.fragment:
        raise ValueError("Not a GitHub release URL: " + url)
    m = _RELEASE_PATH.fullmatch(parts.path)
    if m is None or m.group(2) in (".", ".."):
        raise ValueError("Not a GitHub release URL: " + url)
    # Git ref names never end with "/", so a trailing slash is only URL noise.
    tag = urllib.parse.unquote(m.group(3)).rstrip("/")
    if not tag or any(ord(c) < 32 or ord(c) == 127 for c in tag) or any(part in ("", ".", "..") for part in tag.split("/")):
        raise ValueError("Invalid release tag: " + url)
    return m.group(1) + "/" + m.group(2), tag


def read_limited(stream, max_bytes):
    """Reads a stream fully, raising DownloadTooLarge as soon as more than max_bytes arrive."""
    chunks = []
    total = 0
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            raise DownloadTooLarge(getattr(stream, "url", "") or "")
        chunks.append(chunk)


class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to https and never carries the token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-https URL", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


def _shape_error(what):
    return GitHubError(0, "unexpected API answer for " + what)


def _asset(obj):
    try:
        return Asset(int(obj["id"]), str(obj["name"]), int(obj["size"]), str(obj.get("updated_at") or ""), str(obj.get("browser_download_url") or ""))
    except _SHAPE_ERRORS:
        raise _shape_error("an asset") from None


def _release(repo, obj):
    try:
        assets = obj.get("assets") or []
        if not isinstance(assets, list):
            raise TypeError("assets")
        return Release(repo, str(obj.get("tag_name") or ""), bool(obj.get("draft")), bool(obj.get("prerelease")), str(obj.get("body") or ""), [_asset(a) for a in assets], str(obj.get("published_at") or ""))
    except _SHAPE_ERRORS:
        raise _shape_error("a release of " + repo) from None


def _repo(repo, obj):
    try:
        owner = obj["owner"]
        return Repo(int(owner["id"]), str(owner["login"]), str(owner["type"]), str(obj.get("full_name") or repo))
    except _SHAPE_ERRORS:
        raise _shape_error("the repository " + repo) from None


class GitHub:
    def __init__(self, token=None, repo=None):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.repo = check_repo(repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
        self._opener = urllib.request.build_opener(_HttpsRedirect())

    def headers(self):
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": USER_AGENT}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def _open(self, request):
        try:
            return self._opener.open(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            e.close()
            raise GitHubError(e.code, e.reason) from None
        except _NETWORK_ERRORS as e:
            raise GitHubError(0, "%s: %s" % (type(e).__name__, e)) from None

    def _api(self, method, path, payload=None):
        """A JSON object answer; every failure, including an empty or non-object body, is a GitHubError."""
        data = None
        headers = self.headers()
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        with self._open(request) as response:
            try:
                raw = read_limited(response, MAX_API_BYTES)
            except DownloadTooLarge:
                raise GitHubError(0, "API answer too large: " + path) from None
            except _NETWORK_ERRORS as e:
                raise GitHubError(0, "%s: %s" % (type(e).__name__, e)) from None
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            obj = None
        if not isinstance(obj, dict):
            raise GitHubError(0, "API answer is not a JSON object: " + path)
        return obj

    def _optional(self, path):
        try:
            return self._api("GET", path)
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def get_release_by_url(self, url):
        repo, tag = parse_release_url(url)
        return _release(repo, self._api("GET", "/repos/%s/releases/tags/%s" % (repo, urllib.parse.quote(tag, safe=""))))

    def latest_release(self, repo):
        obj = self._optional("/repos/%s/releases/latest" % check_repo(repo))
        return None if obj is None else _release(repo, obj)

    def get_repo(self, repo):
        return _repo(repo, self._api("GET", "/repos/" + check_repo(repo)))

    def get_asset(self, repo, asset_id):
        obj = self._optional("/repos/%s/releases/assets/%d" % (check_repo(repo), int(asset_id)))
        return None if obj is None else _asset(obj)

    def download(self, url, max_bytes):
        """Public asset download (no token, like the app); follows https redirects, stops past max_bytes."""
        if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme.lower() != "https":
            raise GitHubError(0, "refusing a non-https download: %r" % (url,))
        request = urllib.request.Request(url, headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT})
        with self._open(request) as response:
            length = response.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) > max_bytes:
                raise DownloadTooLarge(url)
            try:
                return read_limited(response, max_bytes)
            except DownloadTooLarge:
                raise DownloadTooLarge(url) from None
            except _NETWORK_ERRORS as e:
                raise GitHubError(0, "%s: %s" % (type(e).__name__, e)) from None

    def comment(self, issue, body):
        self._api("POST", "/repos/%s/issues/%d/comments" % (self.repo, int(issue)), {"body": body})

    def close(self, issue):
        self._api("PATCH", "/repos/%s/issues/%d" % (self.repo, int(issue)), {"state": "closed"})

    def open_issue(self, title, body):
        obj = self._api("POST", "/repos/%s/issues" % self.repo, {"title": title, "body": body})
        try:
            return int(obj["number"])
        except _SHAPE_ERRORS:
            raise _shape_error("a new issue") from None
