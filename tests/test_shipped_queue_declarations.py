"""The shipped template and example userland declare the same queue model."""
import tomllib
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src on the path)
from mizu.kernel import parse_queues

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"proposals", "questions", "notices", "work"}


def declarations(path):
    return parse_queues(tomllib.loads(path.read_text(encoding="utf-8"))["queues"])


class ShippedQueueDeclarations(unittest.TestCase):
    def test_template_and_example_declare_the_same_queues(self):
        shipped = declarations(ROOT / "config/config.example.toml")
        example = declarations(ROOT / "examples/development/queues.toml")
        for label, queues in (("template", shipped), ("example", example)):
            with self.subTest(source=label):
                self.assertEqual(set(queues), NAMES)
                self.assertNotIn("answers", queues)

        # Questions are threads: the operator may answer, with no wake list.
        for name in ("questions", "work"):
            with self.subTest(queue=name):
                self.assertEqual(shipped[name], example[name])
        for label, queues in (("template", shipped), ("example", example)):
            with self.subTest(source=label):
                self.assertIn("operator", queues["questions"].write)
                self.assertEqual(queues["questions"].wake, ())
                self.assertIn("answer", queues["questions"].fields)
                self.assertFalse(queues["questions"].fields["question"].required)
                self.assertFalse(queues["questions"].fields["default"].required)

        for label, fields in (("template", shipped["work"].fields),
                              ("example", example["work"].fields)):
            with self.subTest(source=label):
                self.assertEqual(fields["kind"].values,
                                 ("item", "transition", "wait", "completion"))
                self.assertEqual(fields["status"].values,
                                 ("open", "doing", "blocked", "done", "dropped"))
                self.assertEqual(fields["holder"].type, "principal")
                self.assertEqual(fields["checks"].max_items, 64)

    def test_policy_and_docs_describe_the_thread_model(self):
        policy = (ROOT / "policies/shared.md").read_text(encoding="utf-8")
        self.assertNotIn("`answers`", policy)
        self.assertIn("the operator's answer is a later message in `questions`", policy)
        self.assertIn("referring to that question", policy)
        docs = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
        self.assertIn("[queues.*]", docs)
        self.assertIn("`work` for stage-3 work messages", docs)
        self.assertIn("the dedicated work-item store stays authoritative", docs)
        scheduling = (ROOT / "docs/scheduling.md").read_text(encoding="utf-8")
        self.assertNotIn("`answers`", scheduling)
        self.assertIn("operator appends a referring answer in the same `questions` thread",
                      scheduling)
        for name in ("README.md", "docs/architecture.md", "docs/shared-records.md"):
            with self.subTest(doc=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn("questions / answers /", text)
                self.assertNotIn("Questions, answers, notices and proposals", text)
                self.assertNotIn("Role questions, answers, notices and proposals", text)
                flat = " ".join(text.split())
                self.assertNotIn("questions, questions", flat)
                if name == "docs/architecture.md":
                    self.assertIn("proposals, decisions, questions carrying their answers, "
                                  "notices and receipts", flat)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
