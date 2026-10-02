#!/usr/bin/env python3
"""Update the activity section of README.md from the GitHub Events API.

Design goals
- Idempotent: README is only rewritten when the rendered section changes.
- Cheap: conditional requests (ETag) cached in .cache/activity.json; a 304
  response does not count against the GitHub API rate limit.
- Resilient: exponential backoff with jitter on 5xx / secondary rate limits,
  waits for X-RateLimit-Reset (bounded), and stops calling the API when the
  remaining quota drops below a floor - all tunable through env variables.
- Safe: the token is read from the environment and never printed.

Modes
  (default)   fetch events, rewrite README if changed
  --check     only verify that the markers exist (exit 1 if not)
  --dry-run   fetch + render, print a unified diff, never write README

Environment
  GITHUB_TOKEN            token used for API calls (required except --check)
  ACTIVITY_REPOS          comma-separated list; "owner/repo" or "owner/*"
                          (= the owner's most recently pushed public repos,
                          works for users and organisations)
                          default: GITHUB_REPOSITORY
  ACTIVITY_REPO_LIMIT     max repos taken from each "owner/*" (default 5)
  ACTIVITY_USER           optional: only keep events by this login
  MAX_ITEMS               number of lines to render (default 10)
  README_PATH             default README.md
  CACHE_PATH              default .cache/activity.json
  RATE_LIMIT_MAX_RETRIES  attempts per request (default 4)
  RATE_LIMIT_MAX_WAIT     longest sleep in seconds for a reset (default 60)
  RATE_LIMIT_FLOOR        stop calling the API below this many remaining
                          requests and serve cached data (default 50)
  IGNORE_SHAS_FILE        file with one commit SHA per line; pushes whose head
                          is listed (the workflow's own README commits) are
                          hidden, otherwise every update would create the
                          activity that triggers the next update
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

START = "<!--START_SECTION:activity-->"
END = "<!--END_SECTION:activity-->"
API = "https://api.github.com"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        print(f"::warning::{name} is not a number; using {default}")
        return default


class MarkerError(Exception):
    pass


# ---------------------------------------------------------------- README ---
def replace_section(text: str, body: str) -> str:
    """Replace whatever is between START and END. Raises if markers are bad."""
    s, e = text.find(START), text.find(END)
    if s == -1 or e == -1:
        raise MarkerError("README markers not found: need both START and END")
    if text.count(START) != 1 or text.count(END) != 1:
        raise MarkerError("README markers must appear exactly once")
    if e < s:
        raise MarkerError("END marker appears before START marker")
    return text[: s + len(START)] + "\n" + body.rstrip() + "\n" + text[e:]


def current_section(text: str) -> str:
    s, e = text.find(START), text.find(END)
    return text[s + len(START): e].strip()


# ------------------------------------------------------------------- API ---
def backoff_delay(attempt: int, headers) -> float:
    """Seconds to wait before retry number `attempt` (1-based)."""
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after and retry_after.isdigit():
        return float(retry_after)
    if headers and headers.get("X-RateLimit-Remaining") == "0":
        reset = int(headers.get("X-RateLimit-Reset", "0"))
        return max(0.0, reset - time.time()) + 1
    return min(30.0, 2 ** attempt) + random.uniform(0, 1)


def trim_event(ev: dict) -> dict:
    """Keep only what rendering needs, so the committed cache stays small."""
    p = ev.get("payload", {})
    keep = {k: p[k] for k in ("action", "ref", "ref_type", "head", "number") if k in p}
    for obj in ("pull_request", "issue", "release"):
        if obj in p:
            keep[obj] = {k: p[obj][k] for k in ("number", "merged", "tag_name") if k in p[obj]}
    return {"id": ev.get("id"), "type": ev.get("type"), "created_at": ev.get("created_at"),
            "repo": {"name": ev.get("repo", {}).get("name")},
            "actor": {"login": ev.get("actor", {}).get("login")}, "payload": keep}


class Client:
    def __init__(self, token: str, cache: dict):
        self.token = token
        self.cache = cache
        self.max_attempts = env_int("RATE_LIMIT_MAX_RETRIES", 4)
        self.max_wait = env_int("RATE_LIMIT_MAX_WAIT", 60)
        self.floor = env_int("RATE_LIMIT_FLOOR", 50)
        self.remaining: int | None = None
        self.stats = {"api_calls": 0, "not_modified": 0, "retries": 0, "served_from_cache": 0}

    def _headers(self, etag=None) -> dict:
        return {"Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "readme-activity-updater",
                **({"If-None-Match": etag} if etag else {})}

    def _note_quota(self, headers):
        value = headers.get("X-RateLimit-Remaining") if headers else None
        if value and value.isdigit():
            self.remaining = int(value)

    def get_cached(self, url: str, key: str, shrink=lambda d: d):
        """GET with ETag revalidation, backoff and a quota floor.
        The response body is stored (shrunk) in the cache under `key`."""
        entry = self.cache.get(key, {})
        if self.remaining is not None and self.remaining < self.floor and "data" in entry:
            self.stats["served_from_cache"] += 1
            print(f"::warning::quota {self.remaining} < floor {self.floor}; using cache for {key}")
            return entry["data"]
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(url, headers=self._headers(entry.get("etag")))
            self.stats["api_calls"] += 1
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    self._note_quota(resp.headers)
                    data = shrink(json.load(resp))
                    self.cache[key] = {"etag": resp.headers.get("ETag"), "data": data}
                    return data
            except urllib.error.HTTPError as err:
                self._note_quota(err.headers)
                if err.code == 304:
                    self.stats["not_modified"] += 1
                    return entry.get("data", [])
                retryable = err.code in (429, 500, 502, 503, 504) or (
                    err.code == 403 and (err.headers.get("Retry-After")
                                         or err.headers.get("X-RateLimit-Remaining") == "0"))
                if not retryable or attempt == self.max_attempts:
                    raise RuntimeError(f"GitHub API {err.code} for {key}") from None
                delay = backoff_delay(attempt, err.headers)
                if delay > self.max_wait:
                    raise RuntimeError(f"rate limited for {int(delay)}s on {key}; giving up") from None
            except urllib.error.URLError as err:
                if attempt == self.max_attempts:
                    raise RuntimeError(f"network error for {key}: {err.reason}") from None
                delay = backoff_delay(attempt, None)
            self.stats["retries"] += 1
            print(f"::warning::retry {attempt} for {key} in {delay:.1f}s")
            time.sleep(delay)
        return []

    def get_events(self, repo: str) -> list:
        return self.get_cached(f"{API}/repos/{repo}/events?per_page=30", f"events:{repo}",
                               lambda evs: [trim_event(e) for e in evs])

    def list_repos(self, owner: str, limit: int) -> list:
        """Public, non-fork, non-archived repos of a user or organisation,
        most recently pushed first."""
        repos = self.get_cached(
            f"{API}/users/{owner}/repos?sort=pushed&per_page=30", f"repos:{owner}",
            lambda rs: [r["full_name"] for r in rs if not r.get("fork") and not r.get("archived")])
        return repos[:limit]

    def pr_merged(self, repo: str, number) -> bool:
        """The Events API no longer says whether a closed PR was merged, so ask
        the Pulls API once per PR and remember the answer in the cache."""
        key = f"pr:{repo}#{number}"
        if key not in self.cache:
            req = urllib.request.Request(f"{API}/repos/{repo}/pulls/{number}", headers=self._headers())
            self.stats["api_calls"] += 1
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    self.cache[key] = bool(json.load(resp).get("merged_at"))
            except (urllib.error.URLError, ValueError):
                return False  # unknown: render as "Closed", don't cache
        return self.cache[key]


def load_ignored_shas() -> set[str]:
    path = os.environ.get("IGNORE_SHAS_FILE")
    if not path or not Path(path).exists():
        return set()
    return {line.strip() for line in Path(path).read_text().splitlines() if line.strip()}


def drop_own_pushes(events: list, shas: set[str]) -> tuple[list, int]:
    kept = [e for e in events
            if not (e.get("type") == "PushEvent" and e.get("payload", {}).get("head") in shas)]
    return kept, len(events) - len(kept)


def expand_repos(spec: list[str], client: Client, limit: int) -> list[str]:
    """Turn "owner/*" entries into concrete repos; keep order, drop duplicates."""
    out: list[str] = []
    for item in spec:
        names = client.list_repos(item[:-2], limit) if item.endswith("/*") else [item]
        out.extend(n for n in names if n not in out)
    return out


# ---------------------------------------------------------------- render ---
def link(repo: str) -> str:
    return f"[{repo}](https://github.com/{repo})"


# Only actions worth showing; noise such as assigned/labeled is skipped.
PR_ACTIONS = {"opened", "closed", "reopened"}
ISSUE_ACTIONS = {"opened", "closed", "reopened"}


def format_event(ev: dict, merged=lambda repo, n: False) -> str | None:
    t, p, repo = ev.get("type"), ev.get("payload", {}), ev.get("repo", {}).get("name", "?")
    r, base = link(repo), f"https://github.com/{repo}"
    if t == "PushEvent":
        ref = p.get("ref", "").removeprefix("refs/heads/")
        head = (p.get("head") or "")[:7]
        sha = f" ([`{head}`]({base}/commit/{p['head']}))" if head else ""
        return f"📝 Pushed to `{ref}`{sha} in {r}"
    if t == "PullRequestEvent":
        action = p.get("action", "")
        if action not in PR_ACTIONS:
            return None
        pr = p.get("pull_request", {})
        num = pr.get("number", p.get("number"))
        if action == "closed":
            is_merged = pr["merged"] if "merged" in pr else merged(repo, num)
            action = "merged" if is_merged else "closed"
        icon = {"merged": "🔀", "opened": "📥", "closed": "🚫", "reopened": "🔁"}[action]
        return f"{icon} {action.capitalize()} [PR #{num}]({base}/pull/{num}) in {r}"
    if t == "IssuesEvent":
        action = p.get("action", "")
        if action not in ISSUE_ACTIONS:
            return None
        num = p.get("issue", {}).get("number")
        icon = {"opened": "🆕", "closed": "✅", "reopened": "🔁"}[action]
        return f"{icon} {action.capitalize()} issue [#{num}]({base}/issues/{num}) in {r}"
    if t == "IssueCommentEvent":
        num = p.get("issue", {}).get("number")
        return f"💬 Commented on [#{num}]({base}/issues/{num}) in {r}"
    if t in ("CreateEvent", "DeleteEvent"):
        kind = p.get("ref_type", "")
        if kind == "repository":
            return f"🎉 Created repository {r}"
        verb, icon = ("Created", "➕") if t == "CreateEvent" else ("Deleted", "🗑️")
        return f"{icon} {verb} {kind} `{p.get('ref')}` in {r}"
    if t == "ReleaseEvent":
        tag = p.get("release", {}).get("tag_name")
        return f"🚀 Released [{tag}]({base}/releases/tag/{tag}) in {r}"
    if t == "WatchEvent":
        return f"⭐ Starred {r}"
    if t == "ForkEvent":
        return f"🍴 Forked {r}"
    return None  # unknown types are skipped rather than rendered badly


def render(events: list, max_items: int, user: str | None, merged=lambda r, n: False) -> str:
    seen, lines = set(), []
    for ev in sorted(events, key=lambda e: e.get("created_at") or "", reverse=True):
        if ev.get("id") in seen:
            continue
        seen.add(ev.get("id"))
        if user and (ev.get("actor", {}).get("login") or "").lower() != user.lower():
            continue
        line = format_event(ev, merged)
        if line:
            lines.append(f"{len(lines) + 1}. {line}")
        if len(lines) >= max_items:
            break
    return "\n".join(lines) if lines else "_No recent activity._"


# ------------------------------------------------------------------ main ---
def write_outputs(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def summary(md: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    readme = Path(os.environ.get("README_PATH", "README.md"))
    text = readme.read_text(encoding="utf-8")
    try:
        replace_section(text, "")
    except MarkerError as e:
        print(f"::error file={readme}::{e}")
        return 1
    if args.check:
        print("README markers OK")
        return 0

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("::error::GITHUB_TOKEN is not set")
        return 1
    spec = [r.strip() for r in (os.environ.get("ACTIVITY_REPOS")
                                or os.environ.get("GITHUB_REPOSITORY", "")).split(",") if r.strip()]
    if not spec:
        print("::error::no repositories configured (ACTIVITY_REPOS)")
        return 1

    cache_path = Path(os.environ.get("CACHE_PATH", ".cache/activity.json"))
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    client = Client(token, cache)

    try:
        repos = expand_repos(spec, client, env_int("ACTIVITY_REPO_LIMIT", 5))
    except RuntimeError as e:
        print(f"::error::could not list repositories: {e}")
        return 1

    events, failed = [], []
    for repo in repos:
        try:
            events.extend(client.get_events(repo))
        except RuntimeError as e:
            failed.append(repo)
            print(f"::warning::{e}")
    if not repos or len(failed) == len(repos):
        print("::error::all repositories failed; README left unchanged")
        return 1

    events, hidden = drop_own_pushes(events, load_ignored_shas())
    body = render(events, env_int("MAX_ITEMS", 10),
                  os.environ.get("ACTIVITY_USER") or None, client.pr_merged)
    new_text = replace_section(text, body)
    changed = current_section(new_text) != current_section(text)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")

    s = client.stats
    quota = "?" if client.remaining is None else client.remaining
    report = (f"| repos | API calls | 304 Not Modified | retries | from cache | quota left | changed |\n"
              f"|---|---|---|---|---|---|---|\n"
              f"| {len(repos)} | {s['api_calls']} | {s['not_modified']} | {s['retries']} "
              f"| {s['served_from_cache']} | {quota} | {changed} |")
    print(report)
    summary("### README activity update\n\n" + report + "\n\nRepos: " + ", ".join(repos)
            + f"\n\nHidden bot pushes: {hidden}")
    write_outputs(changed=str(changed).lower(), **s)

    if args.dry_run:
        diff = "".join(difflib.unified_diff(
            text.splitlines(True), new_text.splitlines(True), "README.md", "README.md (preview)"))
        print(diff or "(no changes)")
        summary("#### Preview diff\n\n```diff\n" + (diff or "(no changes)") + "\n```")
        return 0
    if changed:
        readme.write_text(new_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
