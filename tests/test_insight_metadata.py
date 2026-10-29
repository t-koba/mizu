"""Revision-bound attention metadata, with exact legacy omission semantics."""
import dataclasses
from unittest.mock import patch

from support import Fixture
from mizu.errors import Denied
from mizu.fs import canonical, digest, read_json, write_json
from mizu.insights import INSIGHT_METADATA_DEFAULTS, _archive_summary, _content_sha, _identity_payload, insight_metadata
from mizu.protocol import DEFINITIONS, validate


class InsightMetadataTests(Fixture):
    def submit(self, **metadata):
        return self.project.insights.submit(source="searcher", title="Q: Claim", body="Body",
                                            base_snapshot=None, **metadata)

    def revise(self, item, **metadata):
        return self.project.insights.revise(item["id"], source="searcher", title=item["title"],
                                            body=item["body"], base_snapshot=item["base_snapshot"],
                                            expected_rev=item["rev"], **metadata)

    def legacy(self):
        item = {"id": "a" * 32, "source": "searcher", "title": "Q: Claim", "body": "Body",
                "base_snapshot": None, "run": "old-run", "origin": None,
                "created_at": "2020-01-01T00:00:00+00:00", "rev": 1, "evidence": [],
                "updated_at": "2020-01-01T00:00:00+00:00"}
        write_json(self.project.root / "inbox" / f'{item["id"]}.json', item)
        payload = {k: item[k] for k in ("id", "source", "title", "body", "base_snapshot", "run", "origin")}
        write_json(self.project.root / "insight-ids" / f'{item["id"]}.json',
                   {"id": item["id"], "sha256": digest(canonical(payload)), "created_at": item["created_at"]})
        return item

    def test_defaults_and_no_prose_inference(self):
        item = self.submit()
        self.assertEqual(insight_metadata(item), INSIGHT_METADATA_DEFAULTS)
        self.assertEqual(item["kind"], "proposal")  # Q: is prose, not metadata.
        for row in (self.project.insights.read(item["id"]),
                    self.project.insights.list()[0],
                    self.project.insights.list_page()["items"][0]):
            self.assertEqual(insight_metadata(row), INSIGHT_METADATA_DEFAULTS)
        summary = self.project.insights.list()[0]
        self.assertNotIn("body", summary)
        self.assertNotIn("evidence", summary)

    def test_legacy_reads_and_retries_never_inject_defaults(self):
        item = self.legacy()
        paths = [self.project.root / name / f'{item["id"]}.json' for name in ("inbox", "insight-ids")]
        before = [path.read_bytes() for path in paths]
        generation = self.project.insights.generation()
        rows = [self.project.insights.read(item["id"]), self.project.insights.list()[0],
                self.project.insights.list_page()["items"][0],
                self.submit(insight_id=item["id"], run="retry"), self.submit(run="retry"),
                self.revise(item, run="retry")]
        for row in rows:
            self.assertEqual(insight_metadata(row), {})
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(self.project.insights.generation(), generation)
        self.assertEqual(self.project.insights.history(item["id"]), [])
        legacy_content = {k: item[k] for k in ("id", "source", "title", "body", "base_snapshot", "rev")}
        self.assertEqual(_content_sha(item), digest(canonical(legacy_content)))

    def test_legacy_identity_only_retry_keeps_old_digest(self):
        item = self.legacy()
        (self.project.root / "inbox" / f'{item["id"]}.json').unlink()
        result = self.submit(insight_id=item["id"], run=item["run"])
        self.assertFalse(result["retained"])
        self.assertEqual(insight_metadata(result), {})
        with self.assertRaises(Denied):
            self.submit(insight_id=item["id"], run=item["run"], kind="proposal")

    def test_metadata_only_changes_are_revisions_and_identity(self):
        item = self.legacy()
        for field, value in (("kind", "proposal"), ("addressee", "operator"), ("urgency", "high")):
            with self.subTest(field=field):
                generation = self.project.insights.generation()
                old_sha = _content_sha(item)
                revised = self.revise(item, **{field: value})
                self.assertEqual(revised["rev"], item["rev"] + 1)
                self.assertEqual(revised[field], value)
                self.assertNotEqual(_content_sha(revised), old_sha)
                self.assertNotEqual(self.project.insights.generation(), generation)
                history = self.project.insights.history(item["id"])
                self.assertEqual(insight_metadata(history[-1]), insight_metadata(item))
                identity = read_json(self.project.root / "insight-ids" / f'{item["id"]}.json')
                self.assertEqual(identity["sha256"], digest(canonical(_identity_payload(revised))))
                self.assertEqual(self.revise(revised, run="retry"), revised)
                item = revised
        self.assertEqual(insight_metadata(self.revise(item)), insight_metadata(item))
        with self.assertRaises(Denied):
            self.submit(insight_id=item["id"], urgency="low")

    def test_auto_dedup_distinguishes_explicit_metadata(self):
        legacy = self.legacy()
        explicit = self.submit(kind="proposal")
        self.assertNotEqual(explicit["id"], legacy["id"])
        question = self.submit(kind="question", addressee="operator", urgency="high")
        self.assertNotEqual(question["id"], explicit["id"])
        replay = self.submit(kind="question", addressee="operator", urgency="high")
        self.assertEqual(replay["id"], question["id"])
        self.assertTrue(replay["replayed"])
        self.assertNotIn("replayed", self.project.insights.read(question["id"]))

    def test_typed_revision_inherits_metadata_and_reopens_decision(self):
        item = self.submit(kind="question", addressee="a" * 63, urgency="high")
        self.project.insights.decide(item["id"], "reject", "Reviewed", "", "test", expected_rev=1)
        self.assertEqual(self.project.insights.list(), [])
        revised = self.revise(item, urgency="low")
        self.assertEqual(insight_metadata(revised), {"kind": "question", "addressee": "a" * 63, "urgency": "low"})
        self.assertIsNone(self.project.insights.list()[0]["decision"])
        with self.assertRaises(Denied):
            self.project.insights.decide(item["id"], "reject", "Stale", "", "test", expected_rev=1)

    def test_revision_archive_conflict_checks_metadata(self):
        item = self.submit()
        archive = {**item, "urgency": "high", "superseded_by_rev": 2}
        write_json(self.project.root / "insight-revisions" / f'{item["id"]}.r1.json', archive)
        with self.assertRaisesRegex(Denied, "archive conflict"):
            self.revise(item, urgency="low")
        self.assertEqual(self.project.insights.read(item["id"]), item)

    def test_body_unavailable_archive_keeps_only_explicit_metadata(self):
        archived = {"id": "a" * 32, "rev": 2, "created_at": "2020-01-01T00:00:00+00:00",
                    "body_unavailable": True, "kind": "question", "addressee": "operator"}
        summary = _archive_summary(archived)
        self.assertEqual(insight_metadata(summary), {"kind": "question", "addressee": "operator"})
        legacy = _archive_summary({k: v for k, v in archived.items() if k not in ("kind", "addressee")})
        self.assertEqual(insight_metadata(legacy), {})

    def test_metadata_survives_permanent_archive_and_retry(self):
        for legacy in (True, False):
            item = self.legacy() if legacy else self.submit(kind="question", addressee="operator", urgency="high")
            self.project.insights.decide(item["id"], "reject", "Reviewed", "", "test", expected_rev=1)
            path = self.project.root / "decisions" / f'{item["id"]}.json'
            decision = read_json(path)
            decision["created_at"] = "2020-01-01T00:00:00+00:00"
            write_json(path, decision)
            self.assertEqual(self.project.insights.gc_decided(), 1)
            self.assertEqual(insight_metadata(self.project.insights.read(item["id"])), insight_metadata(item))
            rows = self.project.insights.list_page(scope="archive")["items"]
            summary = next(row for row in rows if row["id"] == item["id"])
            self.assertEqual(insight_metadata(summary), insight_metadata(item))
            self.assertNotIn("body", summary)
            self.assertEqual(insight_metadata(self.submit(insight_id=item["id"])), insight_metadata(item))
            revised = self.revise(item, urgency="low")
            self.assertEqual(revised["rev"], 2)
            self.assertEqual(insight_metadata(self.project.insights.history(item["id"])[0]), insight_metadata(item))

    def test_invalid_values_fail_before_writes(self):
        item = self.submit()
        for field, values in {
                "kind": (None, "", "Q:", "Question", 1, []),
                "urgency": (None, "urgent", "HIGH", False, {}),
                "addressee": (None, "", "a" * 64, "a\nb", " a", "A", "é", "../writer", [], 7)}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(Denied):
                        self.submit(**{field: value})
                    with self.assertRaises(Denied):
                        self.revise(item, **{field: value})
        self.assertEqual(self.project.insights.read(item["id"]), item)
        self.assertEqual(self.project.insights.history(item["id"]), [])
        self.assertEqual(len(self.project.insights.list()), 1)

    def test_byte_bound_counts_metadata_and_allows_exact_legacy_retry(self):
        item = self.legacy()
        self.project.insights.maximum = len(canonical(_identity_payload(item)))
        self.assertEqual(self.revise(item), item)
        self.assertEqual(self.submit(insight_id=item["id"], run=item["run"]), item)
        with self.assertRaisesRegex(Denied, "byte limit"):
            self.revise(item, addressee="a" * 63)

    def test_tool_schema_and_context_preserve_authority_and_omission(self):
        ctx = self.context("searcher")
        ctx.role = dataclasses.replace(ctx.role, capabilities=(*ctx.role.capabilities, "insights"))
        args = {"title": "Q: Claim", "body": "Body", "kind": "question",
                "addressee": "operator", "urgency": "high"}
        with patch.object(self.project.insights, "submit", wraps=self.project.insights.submit) as submit:
            item = ctx.handle("submit_insight", args)
        self.assertEqual(submit.call_args.kwargs["source"], "searcher")
        self.assertIsNone(item["origin"])
        self.assertEqual(insight_metadata(item), insight_metadata(args))
        revised = ctx.handle("revise_insight", {"id": item["id"], "rev": 1, "title": args["title"],
                                                "body": args["body"], "addressee": "reviewer"})
        self.assertEqual(revised["addressee"], "reviewer")
        self.assertEqual(revised["urgency"], "high")
        ctx.config = dataclasses.replace(ctx.config, roles={k: v for k, v in ctx.config.roles.items()
                                                          if k != "reviewer"})
        self.assertEqual(ctx.handle("revise_insight", {"id": item["id"], "rev": 2,
                                                       "title": args["title"], "body": args["body"]}), revised)
        with self.assertRaisesRegex(Denied, "configured role"):
            ctx.handle("submit_insight", {**args, "addressee": "missing-role"})
        for tool in ("submit_insight", "revise_insight"):
            valid = args if tool == "submit_insight" else {**args, "id": item["id"], "rev": 2}
            for field in INSIGHT_METADATA_DEFAULTS:
                with self.assertRaises(Denied):
                    validate({**valid, field: None}, DEFINITIONS[tool][1])
        self.assertEqual(ctx.handle("insights", {"id": item["id"]})["kind"], "question")
        self.assertEqual(ctx.handle("insights", {"limit": 1})["insights"][0]["addressee"], "reviewer")
        legacy = self.legacy()
        no_op = ctx.handle("revise_insight", {"id": legacy["id"], "rev": 1,
                                              "title": legacy["title"], "body": legacy["body"]})
        # Context rebases the snapshot, but must not populate omitted metadata.
        self.assertEqual(insight_metadata(no_op), {})
