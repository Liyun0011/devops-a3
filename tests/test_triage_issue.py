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
import coverage_badge as cb  # noqa: E402
import triage_issue as ti  # noqa: E402


class Quiet(unittest.TestCase):
    def setUp(self):
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        # never write into the real CI run's summary/outputs from a test
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        for key in ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()
        self._quiet.__exit__(None, None, None)


GOOD = json.dumps({"type": "bug", "priority": "high", "summary": "Badge broken, ping @someone",
                   "acceptance_criteria": ["Badge renders", "CI green"]})


class TestParsing(Quiet):
    def test_valid_reply(self):
        r = ti.parse_reply("```json\n" + GOOD + "\n```")
        self.assertEqual((r["type"], r["priority"]), ("bug", "high"))
        self.assertEqual(r["acceptance_criteria"], ["Badge renders", "CI green"])

    def test_mentions_are_defused(self):
        self.assertNotIn("@someone", ti.parse_reply(GOOD)["summary"])

    def test_labels_outside_allowlist_are_replaced(self):
        r = ti.parse_reply(json.dumps({"type": "delete-repo", "priority": "urgent!!",
                                       "acceptance_criteria": "single string"}))
        self.assertEqual((r["type"], r["priority"]), ("question", "medium"))
        self.assertEqual(r["acceptance_criteria"], ["single string"])
        self.assertEqual(ti.labels_for(r), ["type: question", "priority: medium", "triaged-by-ai"])

    def test_non_json_reply_is_rejected(self):
        with self.assertRaises(ValueError):
            ti.parse_reply("Sure! Ignoring previous instructions…")

    def test_json_inside_prose_and_braces(self):
        reply = 'Here you go {not json} then ' + GOOD + ' and {"extra": 1}'
        self.assertEqual(ti.parse_reply(reply)["type"], "bug")

    def test_long_text_is_capped(self):
        r = ti.parse_reply(json.dumps({"summary": "x" * 999, "acceptance_criteria": ["y" * 999] * 9}))
        self.assertLessEqual(len(r["summary"]), 201)
        self.assertEqual(len(r["acceptance_criteria"]), 5)

    def test_prompt_wraps_untrusted_text(self):
        msgs = ti.build_messages("Title", "z" * 10_000)
        self.assertIn("untrusted", msgs[0]["content"])
        self.assertTrue(msgs[1]["content"].startswith("<issue>"))
        self.assertLess(len(msgs[1]["content"]), ti.MAX_BODY + 100)

    def test_comment_and_colors(self):
        body = ti.render_comment(ti.parse_reply(GOOD), "m")
        self.assertIn("- [ ] Badge renders", body)
        self.assertIn("**Type:** bug", body)
        self.assertEqual(ti.label_color("priority: high"), "b60205")
        self.assertEqual(ti.label_color("triaged-by-ai"), "5319e7")


class TestMain(Quiet):
    ENV = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "ISSUE_NUMBER": "5",
           "ISSUE_TITLE": "Badge broken", "ISSUE_BODY": "details"}

    def test_happy_path_labels_and_comments(self):
        calls = []

        def fake(self, method, url, payload=None):
            calls.append((method, url.split("/repos/o/r")[-1], payload))
            if "models" in url:
                return {"choices": [{"message": {"content": GOOD}}]}
            return {}
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(ti.GitHub, "request", fake):
            self.assertEqual(ti.main([]), 0)
        paths = [c[1] for c in calls]
        self.assertIn("/issues/5/labels", paths)
        self.assertIn("/issues/5/comments", paths)
        labels = next(c[2]["labels"] for c in calls if c[1] == "/issues/5/labels")
        self.assertEqual(labels, ["type: bug", "priority: high", "triaged-by-ai"])

    def test_model_failure_falls_back_to_needs_triage(self):
        added = []

        def fake(self, method, url, payload=None):
            if "models" in url:
                raise urllib.error.HTTPError(url, 403, "no models", Message(), io.BytesIO())
            if url.endswith("/labels") and "issues" in url:
                added.extend(payload["labels"])
            return {}
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(ti.GitHub, "request", fake):
            self.assertEqual(ti.main([]), 0)
        self.assertEqual(added, ["needs-triage"])

    def test_unparseable_reply_is_reported_and_falls_back(self):
        added, out = [], io.StringIO()

        def fake(self, method, url, payload=None):
            if "models" in url:
                self_payload.append(payload)
                return {"choices": [{"message": {"content": "Sorry, I cannot help."}}]}
            if url.endswith("/labels") and "issues" in url:
                added.extend(payload["labels"])
            return {}
        self_payload = []
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(ti.GitHub, "request", fake), \
             contextlib.redirect_stdout(out):
            self.assertEqual(ti.main([]), 0)
        self.assertEqual(added, ["needs-triage"])
        self.assertIn("reply parsing failed", out.getvalue())
        self.assertIn("Sorry, I cannot help.", out.getvalue())
        self.assertEqual(self_payload[0]["response_format"], {"type": "json_object"})

    def test_dry_run_fetches_issue_and_writes_nothing(self):
        writes = []

        def fake(self, method, url, payload=None):
            if method == "GET":
                return {"title": "From API", "body": None}
            if "models" in url:
                return {"choices": [{"message": {"content": GOOD}}]}
            writes.append(url)
        env = dict(self.ENV, ISSUE_TITLE="", ISSUE_BODY="")
        with mock.patch.dict(os.environ, env), mock.patch.object(ti.GitHub, "request", fake):
            self.assertEqual(ti.main(["--dry-run"]), 0)
        self.assertEqual(writes, [])

    def test_existing_label_is_fine(self):
        def fake(self, method, url, payload=None):
            raise urllib.error.HTTPError(url, 422, "exists", Message(), io.BytesIO())
        with mock.patch.object(ti.GitHub, "request", fake):
            ti.GitHub("t", "o/r").ensure_label("x", "ffffff")  # no exception

    def test_missing_inputs(self):
        with mock.patch.dict(os.environ, dict(self.ENV, ISSUE_NUMBER="abc")):
            self.assertEqual(ti.main([]), 1)


class TestCoverageBadge(Quiet):
    def test_colors_and_file(self):
        self.assertEqual(cb.badge(95)["color"], "brightgreen")
        self.assertEqual(cb.badge(72)["color"], "yellow")
        self.assertEqual(cb.badge(10)["color"], "red")
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d, "c.json"), Path(d, "badges", "coverage.json")
            src.write_text(json.dumps({"totals": {"percent_covered": 87.4}}))
            cb.main([str(src), str(dst)])
            self.assertEqual(json.loads(dst.read_text())["message"], "87%")


if __name__ == "__main__":
    unittest.main()
