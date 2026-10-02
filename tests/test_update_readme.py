import contextlib
import io
import json
import sys
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
        twice = ur.replace_section(once, "1. a")
        self.assertEqual(once, twice)
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


class TestRender(Quiet):
    def ev(self, i, t, payload, ts, actor="me"):
        return {"id": str(i), "type": t, "payload": payload, "created_at": ts,
                "repo": {"name": "o/r"}, "actor": {"login": actor}}

    def test_merged_pr(self):
        line = ur.format_event(self.ev(1, "PullRequestEvent", {
            "action": "closed", "pull_request": {"number": 5, "merged": True}}, "t"))
        self.assertEqual(line.split(" in ")[0], "🔀 Merged [PR #5](https://github.com/o/r/pull/5)")

    def test_merged_pr_lookup_when_payload_is_slim(self):
        ev = self.ev(1, "PullRequestEvent", {"action": "closed", "number": 7}, "t")
        self.assertIn("Merged [PR #7]", ur.format_event(ev, lambda repo, n: True))
        self.assertIn("Closed [PR #7]", ur.format_event(ev))

    def test_noise_actions_skipped(self):
        for t, p in (("IssuesEvent", {"action": "assigned", "issue": {"number": 1}}),
                     ("PullRequestEvent", {"action": "labeled", "number": 2})):
            self.assertIsNone(ur.format_event(self.ev(1, t, p, "t")))

    def test_push_links_head_commit(self):
        line = ur.format_event(self.ev(1, "PushEvent", {"ref": "refs/heads/main", "head": "abcdef1234"}, "t"))
        self.assertIn("[`abcdef1`](https://github.com/o/r/commit/abcdef1234)", line)

    def test_pr_merged_is_cached(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value = ok
        with mock.patch("urllib.request.urlopen", return_value=ok) as up, \
             mock.patch("json.load", return_value={"merged_at": "2026-10-02"}):
            c = ur.Client("t", {})
            self.assertTrue(c.pr_merged("o/r", 2))
            self.assertTrue(c.pr_merged("o/r", 2))
        self.assertEqual(up.call_count, 1)

    def test_sort_dedupe_limit_and_user_filter(self):
        evs = [self.ev(1, "WatchEvent", {}, "2026-01-01"),
               self.ev(2, "ForkEvent", {}, "2026-01-03"),
               self.ev(2, "ForkEvent", {}, "2026-01-03"),  # duplicate (multi-repo overlap)
               self.ev(3, "WatchEvent", {}, "2026-01-02", actor="bot")]
        out = ur.render(evs, 10, "me").splitlines()
        self.assertEqual(len(out), 2)
        self.assertIn("Forked", out[0])  # newest first
        self.assertEqual(len(ur.render(evs, 1, None).splitlines()), 1)

    def test_unknown_event_skipped(self):
        self.assertIsNone(ur.format_event(self.ev(1, "GollumEvent", {}, "t")))


class TestBackoff(Quiet):
    def test_retry_after_header_wins(self):
        self.assertEqual(ur.backoff_delay(1, headers(Retry_After="7")), 7.0)

    def test_exponential_capped(self):
        self.assertLessEqual(ur.backoff_delay(10, None), 31)

    @mock.patch("time.sleep")
    def test_retries_then_succeeds(self, sleep):
        ok = mock.MagicMock()
        ok.__enter__.return_value = ok
        ok.headers = headers(ETag='"abc"')
        ok.read.return_value = b"[]"
        err = urllib.error.HTTPError("u", 503, "x", headers(), io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]), \
             mock.patch("json.load", return_value=[{"id": "1"}]):
            c = ur.Client("t", {})
            self.assertEqual(c.get_events("o/r"), [{"id": "1"}])
        self.assertEqual(c.stats["retries"], 1)
        self.assertEqual(c.cache["o/r"]["etag"], '"abc"')

    def test_304_uses_cache(self):
        err = urllib.error.HTTPError("u", 304, "x", headers(), io.BytesIO())
        cache = {"o/r": {"etag": '"e"', "events": [{"id": "9"}]}}
        with mock.patch("urllib.request.urlopen", side_effect=err) as up:
            c = ur.Client("t", cache)
            self.assertEqual(c.get_events("o/r"), [{"id": "9"}])
        self.assertEqual(up.call_args[0][0].get_header("If-none-match"), '"e"')
        self.assertEqual(c.stats["not_modified"], 1)

    def test_token_not_in_error(self):
        err = urllib.error.HTTPError("u", 401, "x", headers(), io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                ur.Client("SECRET123", {}).get_events("o/r")
        self.assertNotIn("SECRET123", str(ctx.exception))


class TestMain(Quiet):
    def test_check_mode(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "README.md")
            p.write_text(TEMPLATE)
            with mock.patch.dict(os.environ, {"README_PATH": str(p)}):
                self.assertEqual(ur.main(["--check"]), 0)
            p.write_text("# broken")
            with mock.patch.dict(os.environ, {"README_PATH": str(p)}):
                self.assertEqual(ur.main(["--check"]), 1)


if __name__ == "__main__":
    unittest.main()
