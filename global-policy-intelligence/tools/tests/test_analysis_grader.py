"""Ungraded or missing analysis artifacts must never count as successful evals."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "evals"))
from grade_analysis import grade, template
from collector.models import Article
from collector.store import index_entry, save_index, snapshot_article


class AnalysisGraderTests(unittest.TestCase):
    def test_empty_submission_fails(self):
        self.assertFalse(grade({"schema_version": 1, "cases": []}, Path("."))["ok"])

    def test_template_does_not_count_as_a_pass(self):
        result = grade(template(), Path("."))
        self.assertFalse(result["ok"])
        self.assertEqual(result["completed"], [])

    def test_self_review_is_rejected(self):
        submission = template()
        submission.update(producer="same", reviewer="SAME")
        self.assertIn("independent", grade(submission, Path("."))["errors"][0])

    def test_complete_reviews_require_real_auditable_artifacts(self):
        root = TOOLS / "tests" / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            base = Path(temporary)
            evidence = base / "evidence"
            article = Article("https://example.gov/policy/1", "Official", source_key="a",
                              text="本办法自2026年11月1日起施行。", fetch_status="full")
            snapshot = snapshot_article(article, str(evidence))
            save_index(str(evidence), {"entries": {article.url: index_entry(article, snapshot)}})
            text = article.text
            (base / "report.md").write_text(text, encoding="utf-8")
            (base / "claims.json").write_text(json.dumps({"schema_version": 1, "claims": [{
                "id": "C1", "text": text, "kind": "fact", "importance": "core",
                "evidence": [article.url],
            }]}, ensure_ascii=False), encoding="utf-8")
            submission = template()
            submission.update(producer="test producer", reviewer="test independent reviewer")
            for case in submission["cases"]:
                case.update(report="report.md", claims="claims.json", evidence="evidence")
                for review in case["reviews"]:
                    review.update({"pass": True, "rationale": "Synthetic fixture tests submission validation only."})
            self.assertTrue(grade(submission, base)["ok"])
            missing = copy.deepcopy(submission)
            missing["cases"].pop()
            self.assertFalse(grade(missing, base)["ok"])
            missing_artifact = copy.deepcopy(submission)
            missing_artifact["cases"][0]["report"] = "missing.md"
            self.assertFalse(grade(missing_artifact, base)["ok"])
            incomplete = copy.deepcopy(submission)
            incomplete["cases"][0]["reviews"].pop()
            self.assertFalse(grade(incomplete, base)["ok"])


if __name__ == "__main__":
    unittest.main()
