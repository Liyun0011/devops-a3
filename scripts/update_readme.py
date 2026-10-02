#!/usr/bin/env python3
"""Update the activity section of README.md from the GitHub Events API.

Design goals
- Idempotent: README is only rewritten when the rendered section changes.
- Cheap: conditional requests (ETag) cached in .cache/activity.json; a 304
  response does not count against the GitHub API rate limit.
- Resilient: exponential backoff with jitter on 5xx / secondary rate limits,
  and waits for X-RateLimit-Reset (bounded) when the primary limit is hit.
- Safe: the token is read from the environment and never printed.

Modes
  (default)   fetch events, rewrite README if changed
  --check     only verify that the markers exist (exit 1 if not)
  --dry-run   fetch + render, print a unified diff, never write README

Environment
  GITHUB_TOKEN    token used for API calls (required except for --check)
  ACTIVITY_REPOS  comma-separated owner/repo list (default: GITHUB_REPOSITORY)
  ACTIVITY_USER   optional: only keep events by this login
  MAX_ITEMS       number of lines to render (default 10)
  README_PATH     default README.md
  CACHE_PATH      default .cache/activity.json
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
MAX_ATTEMPTS = 4
MAX_RESET_WAIT = 60  # seconds we are willing to sleep for a rate-limit reset


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


class Client:
    def __init__(self, token: str, cache: dict):
        self.token = token
        self.cache = cache
        self.stats = {"api_calls": 0, "not_modified": 0, "retries": 0}

    def get_events(self, repo: str) -> list:
        url = f"{API}/repos/{repo}/events?per_page=30"
        entry = self.cache.get(repo, {})
        for attempt in range(1, MAX_ATTEMPTS + 1):
            req = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "readme-activity-updater",
                **({"If-None-Match": entry["etag"]} if entry.get("etag") else {}),
            })
            self.stats["api_calls"] += 1
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    events = json.load(resp)
                    self.cache[repo] = {"etag": resp.headers.get("ETag"), "events": events}
                    return events
            except urllib.error.HTTPError as err:
                if err.code == 304:
                    self.stats["not_modified"] += 1
                    return entry.get("events", [])
                retryable = err.code in (429, 500, 502, 503, 504) or (
                    err.code == 403 and (err.headers.get("Retry-After")
                                         or err.headers.get("X-RateLimit-Remaining") == "0"))
                if not retryable or attempt == MAX_ATTEMPTS:
                    raise RuntimeError(f"GitHub API {err.code} for /repos/{repo}/events") from None
                delay = backoff_delay(attempt, err.headers)
                if delay > MAX_RESET_WAIT:
                    raise RuntimeError(f"rate limited for {int(delay)}s on {repo}; giving up") from None
            except urllib.error.URLError as err:
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(f"network error for {repo}: {err.reason}") from None
                delay = backoff_delay(attempt, None)
            self.stats["retries"] += 1
            print(f"::warning::retry {attempt} for {repo} in {delay:.1f}s")
            time.sleep(delay)
        return []


# ---------------------------------------------------------------- render ---
def link(repo: str) -> str:
    return f"[{repo}](https://github.com/{repo})"


def format_event(ev: dict) -> str | None:
    t, p, repo = ev.get("type"), ev.get("payload", {}), ev.get("repo", {}).get("name", "?")
    r = link(repo)
    if t == "PushEvent":
        ref = p.get("ref", "").removeprefix("refs/heads/")
        n = p.get("size") or len(p.get("commits", []) or [])
        what = f"{n} commit{'s' if n != 1 else ''}" if n else "commits"
        return f"📝 Pushed {what} to `{ref}` in {r}"
    if t == "PullRequestEvent":
        pr = p.get("pull_request", {})
        num, url = pr.get("number", p.get("number")), pr.get("html_url", f"https://github.com/{repo}/pulls")
        action = "Merged" if p.get("action") == "closed" and pr.get("merged") else p.get("action", "").capitalize()
        icon = {"Merged": "🔀", "Opened": "📥", "Closed": "🚫"}.get(action, "🔃")
        return f"{icon} {action} [PR #{num}]({url}) in {r}"
    if t == "IssuesEvent":
        iss = p.get("issue", {})
        action = p.get("action", "")
        icon = {"opened": "🆕", "closed": "✅", "reopened": "🔁"}.get(action, "📌")
        return f"{icon} {action.capitalize()} issue [#{iss.get('number')}]({iss.get('html_url')}) in {r}"
    if t == "IssueCommentEvent":
        iss = p.get("issue", {})
        return f"💬 Commented on [#{iss.get('number')}]({p.get('comment', {}).get('html_url')}) in {r}"
    if t in ("CreateEvent", "DeleteEvent"):
        kind = p.get("ref_type", "")
        if kind == "repository":
            return f"🎉 Created repository {r}"
        verb, icon = ("Created", "➕") if t == "CreateEvent" else ("Deleted", "🗑️")
        return f"{icon} {verb} {kind} `{p.get('ref')}` in {r}"
    if t == "ReleaseEvent":
        rel = p.get("release", {})
        return f"🚀 Released [{rel.get('tag_name')}]({rel.get('html_url')}) in {r}"
    if t == "WatchEvent":
        return f"⭐ Starred {r}"
    if t == "ForkEvent":
        return f"🍴 Forked {r}"
    return None  # unknown types are skipped rather than rendered badly


def render(events: list, max_items: int, user: str | None) -> str:
    seen, lines = set(), []
    for ev in sorted(events, key=lambda e: e.get("created_at", ""), reverse=True):
        if ev.get("id") in seen:
            continue
        seen.add(ev.get("id"))
        if user and ev.get("actor", {}).get("login", "").lower() != user.lower():
            continue
        line = format_event(ev)
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
    repos = [r.strip() for r in os.environ.get(
        "ACTIVITY_REPOS", os.environ.get("GITHUB_REPOSITORY", "")).split(",") if r.strip()]
    if not repos:
        print("::error::no repositories configured (ACTIVITY_REPOS)")
        return 1

    cache_path = Path(os.environ.get("CACHE_PATH", ".cache/activity.json"))
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    client = Client(token, cache)

    events, failed = [], []
    for repo in repos:
        try:
            events.extend(client.get_events(repo))
        except RuntimeError as e:
            failed.append(repo)
            print(f"::warning::{e}")
    if failed and len(failed) == len(repos):
        print("::error::all repositories failed; README left unchanged")
        return 1

    body = render(events, int(os.environ.get("MAX_ITEMS", "10")),
                  os.environ.get("ACTIVITY_USER") or None)
    new_text = replace_section(text, body)
    changed = current_section(new_text) != current_section(text)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    s = client.stats
    report = (f"| repos | API calls | 304 Not Modified | retries | changed |\n"
              f"|---|---|---|---|---|\n"
              f"| {len(repos)} | {s['api_calls']} | {s['not_modified']} | {s['retries']} | {changed} |")
    print(report)
    summary("### README activity update\n\n" + report)
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
