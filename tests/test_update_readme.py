import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_readme as ur  # noqa: E402

TEMPLATE = f"# T\n\n### Recent Activities\n{ur.START}\n{ur.END}\n\n### About\n"


def headers(**kv):
    m = Message()
    for k, v in kv.items():
        m[k.replace("_", "-")] = v
    return m


class FakeResp(io.BytesIO):
    """Stands in for the object urlopen() returns."""
    def __init__(self, data, **hdrs):
        super().__init__(json.dumps(data).encode())
        self.headers = headers(**hdrs)


def http_error(code, **hdrs):
    return urllib.error.HTTPError("u", code, "x", headers(**hdrs), io.BytesIO())


class Quiet(unittest.TestCase):
    """Swallow the script's ::error/::warning lines so expected failures inside
    tests don't show up as red annotations on the GitHub Actions run."""
    def setUp(self):
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)


class TestMarkers(Quiet):
    def test_replace_is_idempotent(self):
        once = ur.replace_section(TEMPLATE, "1. a")
        self.assertEqual(once, ur.replace_section(once, "1. a"))
        self.assertIn("### About", once)

    def test_missing_marker(self):
        with self.assertRaises(ur.MarkerError):
            ur.replace_section("# no markers", "x")

    def test_duplicate_marker(self):
        with self.assertRaises(ur.MarkerError):
            ur.replace_section(TEMPLATE + ur.START, "x")

    def test_reversed_markers(self):
        with self.assertRaises(ur.MarkerError):
            ur.replace_section(f"{ur.END}\n{ur.START}", "x")


def ev(i, t, payload, ts="t", actor="me", repo="o/r"):
    return {"id": str(i), "type": t, "payload": payload, "created_at": ts,
            "repo": {"name": repo}, "actor": {"login": actor}}


class TestRender(Quiet):
    def test_merged_pr(self):
        line = ur.format_event(ev(1, "PullRequestEvent", {
            "action": "closed", "pull_request": {"number": 5, "merged": True}}))
        self.assertEqual(line.split(" in ")[0], "🔀 Merged [PR #5](https://github.com/o/r/pull/5)")

    def test_merged_pr_lookup_when_payload_is_slim(self):
        e = ev(1, "PullRequestEvent", {"action": "closed", "number": 7})
        self.assertIn("Merged [PR #7]", ur.format_event(e, lambda repo, n: True))
        self.assertIn("Closed [PR #7]", ur.format_event(e))

    def test_noise_actions_skipped(self):
        for t, p in (("IssuesEvent", {"action": "assigned", "issue": {"number": 1}}),
                     ("PullRequestEvent", {"action": "labeled", "number": 2})):
            self.assertIsNone(ur.format_event(ev(1, t, p)))

    def test_every_supported_type_renders(self):
        cases = {
            "PushEvent": ({"ref": "refs/heads/main", "head": "abcdef1234"},
                          "[`abcdef1`](https://github.com/o/r/commit/abcdef1234)"),
            "IssuesEvent": ({"action": "closed", "issue": {"number": 3}}, "✅ Closed issue [#3]"),
            "IssueCommentEvent": ({"issue": {"number": 4}}, "💬 Commented on [#4]"),
            "CreateEvent": ({"ref_type": "branch", "ref": "dev"}, "➕ Created branch `dev`"),
            "DeleteEvent": ({"ref_type": "tag", "ref": "v1"}, "Deleted tag `v1`"),
            "ReleaseEvent": ({"release": {"tag_name": "v2"}}, "🚀 Released [v2]"),
            "WatchEvent": ({}, "⭐ Starred"),
            "ForkEvent": ({}, "🍴 Forked"),
        }
        for t, (payload, expected) in cases.items():
            with self.subTest(t):
                self.assertIn(expected, ur.format_event(ev(1, t, payload)))
        self.assertIn("🎉 Created repository", ur.format_event(ev(1, "CreateEvent", {"ref_type": "repository"})))

    def test_unknown_event_skipped(self):
        self.assertIsNone(ur.format_event(ev(1, "GollumEvent", {})))

    def test_sort_dedupe_limit_and_user_filter(self):
        evs = [ev(1, "WatchEvent", {}, "2026-01-01"),
               ev(2, "ForkEvent", {}, "2026-01-03"),
               ev(2, "ForkEvent", {}, "2026-01-03"),  # duplicate (multi-repo overlap)
               ev(3, "WatchEvent", {}, "2026-01-02", actor="bot")]
        out = ur.render(evs, 10, "me").splitlines()
        self.assertEqual(len(out), 2)
        self.assertIn("Forked", out[0])  # newest first
        self.assertEqual(len(ur.render(evs, 1, None).splitlines()), 1)
        self.assertEqual(ur.render([], 10, None), "_No recent activity._")

    def test_trim_event_keeps_only_render_fields(self):
        raw = ev(1, "PullRequestEvent", {"action": "closed", "number": 2,
                                         "pull_request": {"number": 2, "merged": True, "body": "x" * 999}})
        raw["actor"]["avatar_url"] = "https://…"
        slim = ur.trim_event(raw)
        self.assertEqual(slim["payload"]["pull_request"], {"number": 2, "merged": True})
        self.assertNotIn("avatar_url", slim["actor"])
        self.assertEqual(ur.format_event(slim), ur.format_event(raw))


class TestClient(Quiet):
    def test_retry_after_header_wins(self):
        self.assertEqual(ur.backoff_delay(1, headers(Retry_After="7")), 7.0)

    def test_reset_header_and_exponential_cap(self):
        self.assertGreaterEqual(ur.backoff_delay(1, headers(X_RateLimit_Remaining="0",
                                                             X_RateLimit_Reset="0")), 1)
        self.assertLessEqual(ur.backoff_delay(10, None), 31)

    @mock.patch("time.sleep")
    def test_retries_then_succeeds_and_stores_etag(self, sleep):
        ok = FakeResp([{"id": "1", "type": "WatchEvent"}], ETag='"abc"', X_RateLimit_Remaining="4999")
        with mock.patch("urllib.request.urlopen", side_effect=[http_error(503), ok]):
            c = ur.Client("t", {})
            self.assertEqual(c.get_events("o/r")[0]["id"], "1")
        self.assertEqual(c.stats["retries"], 1)
        self.assertEqual(c.cache["events:o/r"]["etag"], '"abc"')
        self.assertEqual(c.remaining, 4999)

    def test_304_uses_cache(self):
        cache = {"events:o/r": {"etag": '"e"', "data": [{"id": "9"}]}}
        with mock.patch("urllib.request.urlopen", side_effect=http_error(304)) as up:
            c = ur.Client("t", cache)
            self.assertEqual(c.get_events("o/r"), [{"id": "9"}])
        self.assertEqual(up.call_args[0][0].get_header("If-none-match"), '"e"')
        self.assertEqual(c.stats["not_modified"], 1)

    def test_token_not_in_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401)):
            with self.assertRaises(RuntimeError) as ctx:
                ur.Client("SECRET123", {}).get_events("o/r")
        self.assertNotIn("SECRET123", str(ctx.exception))

    @mock.patch("time.sleep")
    def test_env_configures_retries(self, sleep):
        with mock.patch.dict(os.environ, {"RATE_LIMIT_MAX_RETRIES": "2"}), \
             mock.patch("urllib.request.urlopen", side_effect=http_error(502)) as up:
            with self.assertRaises(RuntimeError):
                ur.Client("t", {}).get_events("o/r")
        self.assertEqual(up.call_count, 2)

    def test_long_reset_gives_up_instead_of_sleeping(self):
        err = http_error(403, Retry_After="600")
        with mock.patch.dict(os.environ, {"RATE_LIMIT_MAX_WAIT": "30"}), \
             mock.patch("urllib.request.urlopen", side_effect=err), mock.patch("time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as ctx:
                ur.Client("t", {}).get_events("o/r")
        self.assertIn("giving up", str(ctx.exception))
        sleep.assert_not_called()

    def test_quota_floor_serves_cache_without_calling(self):
        cache = {"events:o/r": {"etag": '"e"', "data": [{"id": "9"}]}}
        with mock.patch.dict(os.environ, {"RATE_LIMIT_FLOOR": "100"}), \
             mock.patch("urllib.request.urlopen") as up:
            c = ur.Client("t", cache)
            c.remaining = 10
            self.assertEqual(c.get_events("o/r"), [{"id": "9"}])
        up.assert_not_called()
        self.assertEqual(c.stats["served_from_cache"], 1)

    def test_bad_env_value_falls_back(self):
        with mock.patch.dict(os.environ, {"RATE_LIMIT_FLOOR": "lots"}):
            self.assertEqual(ur.Client("t", {}).floor, 50)

    def test_owner_wildcard_expands_skipping_forks_and_archived(self):
        repos = [{"full_name": "me/a"}, {"full_name": "me/fork", "fork": True},
                 {"full_name": "me/old", "archived": True}, {"full_name": "me/b"}, {"full_name": "me/c"}]
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(repos)):
            c = ur.Client("t", {})
            out = ur.expand_repos(["me/b", "me/*"], c, limit=2)
        self.assertEqual(out, ["me/b", "me/a"])  # order kept, duplicates dropped, limit applied

    def test_pr_merged_is_cached(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResp({"merged_at": "2026-10-02"})) as up:
            c = ur.Client("t", {})
            self.assertTrue(c.pr_merged("o/r", 2))
            self.assertTrue(c.pr_merged("o/r", 2))
        self.assertEqual(up.call_count, 1)


class TestMain(Quiet):
    def run_main(self, readme_text, events_by_repo, argv=(), extra_env=None):
        d = tempfile.mkdtemp()
        readme, cache = Path(d, "README.md"), Path(d, ".cache", "activity.json")
        readme.write_text(readme_text)
        env = {"README_PATH": str(readme), "CACHE_PATH": str(cache), "GITHUB_TOKEN": "t",
               "ACTIVITY_REPOS": ",".join(events_by_repo), "GITHUB_OUTPUT": str(Path(d, "out")),
               "GITHUB_STEP_SUMMARY": str(Path(d, "summary"))}
        env.update(extra_env or {})

        def fake_events(client, repo):
            if events_by_repo[repo] is None:
                raise RuntimeError("boom")
            return events_by_repo[repo]
        with mock.patch.dict(os.environ, env), mock.patch.object(ur.Client, "get_events", fake_events):
            code = ur.main(list(argv))
        return code, readme.read_text(), Path(d, "out").read_text() if Path(d, "out").exists() else "", cache

    def test_check_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "README.md")
            p.write_text(TEMPLATE)
            with mock.patch.dict(os.environ, {"README_PATH": str(p)}):
                self.assertEqual(ur.main(["--check"]), 0)
            p.write_text("# broken")
            with mock.patch.dict(os.environ, {"README_PATH": str(p)}):
                self.assertEqual(ur.main(["--check"]), 1)

    def test_update_then_idempotent(self):
        events = {"o/a": [ev(1, "WatchEvent", {}, "2026-01-01", repo="o/a")],
                  "o/b": [ev(2, "ForkEvent", {}, "2026-01-02", repo="o/b")]}
        code, text, out, cache = self.run_main(TEMPLATE, events)
        self.assertEqual(code, 0)
        self.assertIn("changed=true", out)
        self.assertIn("1. 🍴 Forked", text)
        self.assertTrue(cache.exists())
        code, again, out, _ = self.run_main(text, events)
        self.assertIn("changed=false", out)
        self.assertEqual(again, text)

    def test_dry_run_never_writes(self):
        code, text, _, _ = self.run_main(TEMPLATE, {"o/a": [ev(1, "WatchEvent", {}, repo="o/a")]}, ["--dry-run"])
        self.assertEqual((code, text), (0, TEMPLATE))

    def test_one_repo_failing_is_tolerated_all_failing_is_not(self):
        code, text, _, _ = self.run_main(TEMPLATE, {"o/a": None, "o/b": [ev(2, "ForkEvent", {}, repo="o/b")]})
        self.assertEqual(code, 0)
        self.assertIn("Forked", text)
        code, text, _, _ = self.run_main(TEMPLATE, {"o/a": None})
        self.assertEqual((code, text), (1, TEMPLATE))

    def test_missing_token_or_repos(self):
        code, *_ = self.run_main(TEMPLATE, {"o/a": []}, extra_env={"GITHUB_TOKEN": ""})
        self.assertEqual(code, 1)
        code, *_ = self.run_main(TEMPLATE, {}, extra_env={"GITHUB_REPOSITORY": ""})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
