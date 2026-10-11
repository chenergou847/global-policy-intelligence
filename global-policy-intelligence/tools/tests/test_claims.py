"""Offline regression tests for claim-scoped evidence auditing (standard library)."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from collector import store, verify  # noqa: E402
from collector.models import Article  # noqa: E402


class ClaimsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TOOLS / "tests" / (".tmp_claims_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.a = "https://example.gov/a"
        self.b = "https://example.gov/b"
        self.add_source(self.a, "甲规〔2026〕3号。2026年11月1日施行。补贴500万元。企业应当提交年度报告。")
        self.add_source(self.b, "乙规〔2027〕9号。2027年12月2日施行。补贴900万元。企业可以延期提交材料。")

    def add_source(self, url: str, text: str, *, status: str = "full", layer: str = "L1") -> None:
        article = Article(url=url, source="fixture", text=text, fetch_status=status,
                          evidence_layer=layer, first_seen="2026-10-11")
        snapshot_path = store.snapshot_article(article, str(self.root))
        index = store.load_index(str(self.root))
        index.setdefault("entries", {})[url] = store.index_entry(article, snapshot_path)
        store.save_index(str(self.root), index)

    def claim(self, text: str, *, kind: str = "fact", importance: str = "core",
              evidence: list[str] | None = None, claim_id: str = "claim-1") -> dict:
        return {"id": claim_id, "text": text, "kind": kind, "importance": importance,
                "evidence": [self.a] if evidence is None else evidence}

    def ledger(self, *claims: dict) -> dict:
        return {"schema_version": 1, "claims": list(claims)}

    def audit(self, *claims: dict, report: str | None = None, **kwargs):
        if report is None:
            report = "\n\n".join(claim["text"] for claim in claims)
        return verify.verify_report(report, str(self.root), claims=self.ledger(*claims), **kwargs)

    def assert_check(self, result, check: str) -> None:
        self.assertFalse(result.ok, result.to_dict())
        self.assertIn(check, {finding.check for finding in result.errors})

    def test_full_authoritative_fact_passes_with_format_normalization(self) -> None:
        self.add_source(self.a, "甲规〔2026〕3号。2026年11月1日施行。补贴1,500万元。", layer="L2")
        claim = self.claim("甲规〔２０２６〕３号，2026/11/01施行，补贴１５００万元。")
        result = self.audit(claim, report="\n甲规〔２０２６〕３号， 2026/11/01 施行，\n补贴１５００万元。")
        self.assertTrue(result.strict_passed, result.to_dict())
        self.assertEqual(result.stats["claims_verified"], 1)
        self.assertTrue(result.to_dict()["manual_review_required"])
        self.assertTrue(any("所有实质正文" in finding.message for finding in result.warnings))

    def test_wrong_source_cannot_borrow_quote_date_docno_or_amount(self) -> None:
        cases = [("“企业可以延期提交材料。”", "C2-逐字引文"),
                 ("2027-12-02施行。", "C3-文号数字"),
                 ("乙规〔2027〕9号。", "C3-文号数字"),
                 ("补贴900万元。", "C3-文号数字")]
        for text, check in cases:
            with self.subTest(text=text):
                result = self.audit(self.claim(text))
                self.assert_check(result, check)
                self.assertTrue(any("claim-1" in finding.detail for finding in result.errors))
                correct = self.audit(self.claim(text, evidence=[self.b]))
                self.assertTrue(correct.strict_passed, correct.to_dict())

    def test_quote_cannot_be_joined_across_snapshots(self) -> None:
        self.add_source(self.a, "企业应当")
        self.add_source(self.b, "提交年度报告。")
        result = self.audit(self.claim("“企业应当提交年度报告。”", evidence=[self.a, self.b]))
        self.assert_check(result, "C2-逐字引文")

    def test_actual_report_citation_cannot_disagree_with_ledger(self) -> None:
        self.add_source(self.b, "", status="blocked")
        text = "补贴500万元。"
        result = self.audit(self.claim(text), report=f"{text}[原文]({self.b})")
        self.assert_check(result, "C5-声明覆盖")
        # A correct source in an unrelated appendix must not hide a wrong inline citation.
        result = self.audit(self.claim(text), report=f"{text}[原文]({self.b})\n\n来源库：{self.a}")
        self.assert_check(result, "C5-声明覆盖")
        self.assertTrue(self.audit(self.claim(text), report=f"{text}[原文]({self.a})").ok)

    def test_reference_style_citation_is_bound_to_this_claim(self) -> None:
        text = "补贴500万元。"
        report = f"{text}[原文][wrong]\n\n[wrong]: {self.b}\n\n来源库：{self.a}"
        self.assert_check(self.audit(self.claim(text), report=report), "C5-声明覆盖")

    def test_report_quote_markup_is_audited_when_ledger_contains_plain_text(self) -> None:
        text = "企业可以延期提交材料。"
        for report in ("> " + text, "“" + text + "”", '"' + text + '"'):
            with self.subTest(report=report):
                self.assert_check(self.audit(self.claim(text), report=report), "C2-逐字引文")
                self.assertTrue(self.audit(self.claim(text, evidence=[self.b]), report=report).ok)

    def test_english_docnos_keep_spaces_during_extraction(self) -> None:
        for source, claim_text in (("Directive (EU) 2026/42", "Regulation (EU) 2026/42"),
                                   ("90 FR 11111 and 22222", "90 FR 22222"),
                                   ("2026 and 42 are mentioned", "SI 2026/42")):
            with self.subTest(claim=claim_text):
                self.add_source(self.a, source)
                self.assert_check(self.audit(self.claim(claim_text)), "C3-文号数字")
                self.add_source(self.a, claim_text)
                self.assertTrue(self.audit(self.claim(claim_text)).ok)

    def test_bare_numbers_require_this_claims_sources_and_numeric_boundaries(self) -> None:
        self.add_source(self.a, "机构共有1500家。")
        self.add_source(self.b, "机构共有500家。")
        self.assert_check(self.audit(self.claim("机构共有500家。")), "C3-文号数字")
        self.assertTrue(self.audit(self.claim("机构共有500家。", evidence=[self.b])).ok)

    def test_all_explicit_claim_urls_require_index_and_snapshot(self) -> None:
        for kind in ("fact", "inference", "assumption", "company"):
            with self.subTest(kind=kind):
                unknown = self.claim("公司将扩大业务。", kind=kind, evidence=["https://example.gov/missing"])
                self.assert_check(self.audit(unknown), "C1-引用链接")
                index = store.load_index(str(self.root))
                index["entries"][self.a]["snapshot_path"] = "snapshots/missing.json"
                store.save_index(str(self.root), index)
                self.assert_check(self.audit(self.claim("公司将扩大业务。", kind=kind)), "C4-证据强度")
                self.add_source(self.a, "企业应当提交年度报告。")

    def test_snapshot_read_failure_is_a_finding(self) -> None:
        with patch("collector.store.load_snapshot", side_effect=PermissionError("unreadable")):
            self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C4-证据强度")

    def test_snapshot_loader_receives_relative_library_path(self) -> None:
        with patch("collector.store.load_snapshot", wraps=store.load_snapshot) as loader:
            result = self.audit(self.claim("企业应当提交年度报告。"))
        self.assertTrue(result.ok, result.to_dict())
        self.assertFalse(Path(loader.call_args.args[1]).is_absolute())

    def test_snapshot_paths_cannot_escape_library_or_be_absolute(self) -> None:
        index = store.load_index(str(self.root))
        path = index["entries"][self.a]["snapshot_path"]
        for bad_path in (str(self.root / path), "../outside.json"):
            with self.subTest(path=bad_path):
                index["entries"][self.a]["snapshot_path"] = bad_path
                store.save_index(str(self.root), index)
                self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C4-证据强度")

    def test_core_fact_and_inference_reject_all_weak_statuses_and_layers(self) -> None:
        for kind in ("fact", "inference"):
            for status, layer in (("partial", "L1"), ("listing_only", "L1"),
                                  ("blocked", "L1"), ("scanned_pdf", "L1"), ("full", "L3")):
                with self.subTest(kind=kind, status=status, layer=layer):
                    self.add_source(self.a, "企业应当提交年度报告。", status=status, layer=layer)
                    self.assert_check(self.audit(self.claim("企业应当提交年度报告。", kind=kind)),
                                      "C4-证据强度")

    def test_core_cannot_use_blocked_source_even_alongside_full_source(self) -> None:
        self.add_source(self.b, "", status="blocked")
        self.assert_check(self.audit(self.claim("企业应当提交年度报告。", evidence=[self.a, self.b])),
                          "C4-证据强度")

    def test_full_index_cannot_mask_empty_or_weak_snapshot(self) -> None:
        for change in ({"text": "\n\u200b "}, {"fetch_status": "partial"},
                       {"evidence_layer": "L3"}):
            with self.subTest(change=change):
                self.add_source(self.a, "企业应当提交年度报告。")
                index = store.load_index(str(self.root))
                path = self.root / index["entries"][self.a]["snapshot_path"]
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                snapshot.update(change)
                path.write_text(json.dumps(snapshot), encoding="utf-8")
                self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C4-证据强度")

    def test_snapshot_url_cannot_impersonate_another_source(self) -> None:
        index = store.load_index(str(self.root))
        index["entries"][self.a]["snapshot_path"] = index["entries"][self.b]["snapshot_path"]
        store.save_index(str(self.root), index)
        self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C1-引用链接")

    def test_index_and_snapshot_fingerprints_must_match_actual_body(self) -> None:
        for target, key in (("index", "content_hash"), ("index", "snapshot_id"),
                            ("snapshot", "snapshot_id"), ("snapshot", "text")):
            with self.subTest(target=target, key=key):
                self.add_source(self.a, "企业应当提交年度报告。")
                index = store.load_index(str(self.root))
                if target == "index":
                    index["entries"][self.a][key] = "tampered"
                    store.save_index(str(self.root), index)
                else:
                    path = self.root / index["entries"][self.a]["snapshot_path"]
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                    snapshot[key] = "tampered"
                    path.write_text(json.dumps(snapshot), encoding="utf-8")
                self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C4-证据强度")

    def test_upgrading_only_index_status_cannot_make_partial_snapshot_full(self) -> None:
        self.add_source(self.a, "企业应当提交年度报告。", status="partial")
        index = store.load_index(str(self.root))
        index["entries"][self.a]["fetch_status"] = "full"
        store.save_index(str(self.root), index)
        self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C4-证据强度")

    def test_invalid_content_hash_type_is_a_finding(self) -> None:
        index = store.load_index(str(self.root))
        index["entries"][self.a]["content_hash"] = []
        store.save_index(str(self.root), index)
        self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C4-证据强度")

    def test_malformed_snapshots_are_rejected(self) -> None:
        index = store.load_index(str(self.root))
        path = self.root / index["entries"][self.a]["snapshot_path"]
        for payload in ("{", "[]", "null"):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="utf-8")
                self.assert_check(self.audit(self.claim("企业应当提交年度报告。")), "C4-证据强度")

    def test_blocked_auxiliary_or_uncited_source_does_not_fail_core_audit(self) -> None:
        self.add_source(self.b, "", status="blocked")
        core = self.claim("企业应当提交年度报告。")
        self.assertTrue(self.audit(core).strict_passed)
        auxiliary = self.claim("辅助来源暂不可读。", kind="assumption", importance="supporting",
                               evidence=[self.b], claim_id="aux")
        result = self.audit(core, auxiliary)
        self.assertTrue(result.strict_passed, result.to_dict())
        self.assertTrue(any(finding.check == "C4-证据强度" for finding in result.warnings))

    def test_missing_ledger_fails_explicit_strict_and_legacy_never_claims_strict_pass(self) -> None:
        text = "企业应当提交年度报告。"
        strict = verify.verify_report(text, str(self.root), strict=True)
        self.assert_check(strict, "C0-声明台账")
        legacy = verify.verify_report(text, str(self.root))
        self.assertTrue(legacy.ok)
        self.assertFalse(legacy.strict_passed)
        self.assertEqual(legacy.to_dict()["audit_mode"], "legacy")
        self.assertTrue(any("未进行逐项审计" in finding.message for finding in legacy.warnings))

    def test_legacy_retains_url_quote_number_and_no_full_checks(self) -> None:
        for report, check in (("https://unknown.example/policy", "C1-引用链接"),
                              ("> 企业无需提交任何年度报告。", "C2-逐字引文"),
                              ("补贴9999万元。", "C3-文号数字")):
            with self.subTest(check=check):
                self.assert_check(verify.verify_report(report, str(self.root)), check)
        self.add_source(self.a, "", status="blocked")
        self.add_source(self.b, "", status="blocked")
        self.assert_check(verify.verify_report("结论", str(self.root)), "C4-证据强度")

    def test_core_requires_evidence_and_unrelated_full_source_does_not_help(self) -> None:
        for kind in ("fact", "inference"):
            with self.subTest(kind=kind):
                self.assert_check(self.audit(self.claim("企业应当提交年度报告。", kind=kind, evidence=[])),
                                  "C4-证据强度")

    def test_claim_text_must_appear_in_report(self) -> None:
        self.assert_check(self.audit(self.claim("企业应当提交年度报告。"), report="报告未包含此声明。"),
                          "C5-声明覆盖")

    def test_inference_company_assumption_and_action_dates_do_not_use_fact_scanner(self) -> None:
        fact = self.claim("补贴500万元。")
        inference = self.claim("预计2029-08-31收入增加1234万元，达到27%。", kind="inference", claim_id="inference")
        company = self.claim("公司数据：“2025-03-02收入为8888万元。”", kind="company", evidence=[], claim_id="company")
        assumption = self.claim("假设2030-12-31投入7777万元。", kind="assumption", evidence=[], claim_id="assumption")
        report = "\n".join(claim["text"] for claim in (fact, inference, company, assumption))
        report += "\n行动期限：2031-07-09前完成复核，预算9999万元。"
        result = self.audit(fact, inference, company, assumption, report=report)
        self.assertTrue(result.strict_passed, result.to_dict())
        self.assertEqual(result.stats["claims_verified"], 4)

    def test_company_and_assumption_only_ledger_does_not_require_global_full(self) -> None:
        store.save_index(str(self.root), {"entries": {}})
        result = self.audit(self.claim("公司收入8888万元。", kind="company", evidence=[]),
                            self.claim("假设增长20%。", kind="assumption", evidence=[], claim_id="assumption"))
        self.assertTrue(result.strict_passed, result.to_dict())

    def test_strict_checks_report_urls_even_outside_claims_and_cannot_downgrade_them(self) -> None:
        claim = self.claim("企业应当提交年度报告。")
        report = claim["text"] + "\nhttps://unknown.example/action"
        self.assert_check(self.audit(claim, report=report, require_all_citations=False), "C1-引用链接")

    def test_invalid_ledgers_are_findings_not_exceptions(self) -> None:
        good = self.ledger(self.claim("企业应当提交年度报告。"))
        bad = [[], {"schema_version": True, "claims": good["claims"]},
               {"schema_version": 2, "claims": good["claims"]}, {"schema_version": 1, "claims": []},
               {"schema_version": 1, "claims": [None]},
               self.ledger(good["claims"][0], good["claims"][0])]
        for key, value in (("id", " "), ("text", []), ("kind", []), ("kind", "forecast"),
                           ("importance", "high"), ("evidence", self.a), ("evidence", [3]),
                           ("evidence", ["file:///tmp/policy"]), ("evidence", [self.a, self.a])):
            candidate = copy.deepcopy(good)
            candidate["claims"][0][key] = value
            bad.append(candidate)
        candidate = copy.deepcopy(good)
        del candidate["claims"][0]["evidence"]
        bad.append(candidate)
        for ledger in bad:
            with self.subTest(ledger=ledger):
                result = verify.verify_report("企业应当提交年度报告。", str(self.root), claims=ledger)
                self.assert_check(result, "C0-声明台账")

    def test_missing_or_invalid_json_file_is_not_silently_legacy(self) -> None:
        path = self.root / "claims.json"
        self.assert_check(verify.verify_report("报告", str(self.root), claims=path), "C0-声明台账")
        for payload in ("{", '{"schema_version":1,"schema_version":2,"claims":[]}',
                        '{"schema_version":1,"claims":[],"extra":NaN}'):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="utf-8")
                self.assert_check(verify.verify_report("报告", str(self.root), claims=path), "C0-声明台账")

    def test_cli_defaults_strict_and_explicit_legacy_is_labelled(self) -> None:
        report = self.root / "report.md"
        report.write_text("企业应当提交年度报告。", encoding="utf-8")
        args = [str(report), "--evidence", str(self.root), "--json"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = verify.main(args)
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["audit_mode"], "strict")
        self.assertTrue(any(finding["check"] == "C0-声明台账" for finding in result["errors"]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = verify.main(args + ["--legacy-checks-only"])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(result["strict_passed"])
        self.assertTrue(any("未进行逐项审计" in finding["message"] for finding in result["warnings"]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            verify.main(args[:-1] + ["--legacy-checks-only"])
        self.assertIn("无逐项审计", output.getvalue())

    def test_cli_reads_ledger_and_reports_validation_failure(self) -> None:
        report = self.root / "report.md"
        ledger = self.root / "claims.json"
        report.write_text("补贴900万元。", encoding="utf-8")
        args = [str(report), "--evidence", str(self.root), "--claims", str(ledger), "--json"]
        for evidence, expected_exit in (([self.a], 2), ([self.b], 0)):
            with self.subTest(evidence=evidence):
                ledger.write_text(json.dumps(self.ledger(self.claim("补贴900万元。", evidence=evidence))), encoding="utf-8-sig")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exit_code = verify.main(args)
                result = json.loads(output.getvalue())
                self.assertEqual(exit_code, expected_exit, result)
                self.assertEqual(result["strict_passed"], expected_exit == 0)

    def test_standalone_module_uses_utf8_with_standard_library_only(self) -> None:
        report = self.root / "report.md"
        ledger = self.root / "claims.json"
        report.write_text("企业应当提交年度报告。", encoding="utf-8")
        ledger.write_text(json.dumps(self.ledger(self.claim("企业应当提交年度报告。"))), encoding="utf-8")
        environment = dict(os.environ, PYTHONIOENCODING="ascii")
        process = subprocess.run(
            [sys.executable, "-S", "-m", "collector.verify", str(report),
             "--evidence", str(self.root), "--claims", str(ledger), "--json"],
            cwd=TOOLS, env=environment, capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["strict_passed"], result)
        self.assertIn("所有实质正文", process.stdout)

    def test_invalid_evidence_index_is_a_finding_in_both_modes(self) -> None:
        index_path = self.root / "index.json"
        index_path.write_text("{", encoding="utf-8")
        for options in ({}, {"claims": self.ledger(self.claim("企业应当提交年度报告。"))}):
            with self.subTest(options=options):
                self.assert_check(verify.verify_report("企业应当提交年度报告。", str(self.root), **options),
                                  "C4-证据强度")


if __name__ == "__main__":
    unittest.main()
