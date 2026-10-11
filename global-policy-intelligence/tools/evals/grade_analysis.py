"""Validate independent rubric reviews and strict evidence audits for analysis evals.

This validates a review submission; it does not invent or automate qualitative scores.
"""
import argparse
import json
from pathlib import Path
import sys

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from collector.console import configure_output
from collector.verify import verify_report

SPEC = TOOLS.parent / "evals" / "evals.json"


def analysis_cases():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    return [case for case in spec["evals"] if case["kind"] == "analysis"]


def template():
    return {
        "schema_version": 1, "producer": "", "reviewer": "",
        "cases": [{
            "id": case["id"], "report": "", "claims": "", "evidence": "",
            "reviews": [{"expectation": i, "pass": False, "rationale": ""}
                        for i, _ in enumerate(case["expectations"], 1)],
        } for case in analysis_cases()],
    }


def grade(submission, base: Path):
    errors = []
    completed = []
    expected = {case["id"]: case for case in analysis_cases()}
    if not isinstance(submission, dict) or submission.get("schema_version") != 1:
        return {"ok": False, "completed": [], "errors": ["schema_version must be 1"]}
    producer, reviewer = submission.get("producer"), submission.get("reviewer")
    if (not isinstance(producer, str) or not producer.strip()
            or not isinstance(reviewer, str) or not reviewer.strip()
            or producer.strip().casefold() == reviewer.strip().casefold()):
        errors.append("producer and independent reviewer must be identified separately")
    cases = submission.get("cases")
    if not isinstance(cases, list):
        return {"ok": False, "completed": [], "errors": errors + ["cases must be an array"]}
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            errors.append("case must be an object")
            continue
        case_id = case.get("id")
        if type(case_id) is not int or case_id not in expected or case_id in seen:
            errors.append(f"unknown or duplicate case: {case_id}")
            continue
        seen.add(case_id)
        before = len(errors)
        reviews = case.get("reviews")
        rubric = expected[case_id]["expectations"]
        if not isinstance(reviews, list):
            errors.append(f"case {case_id}: missing rubric reviews")
            reviews = []
        reviewed = set()
        for review in reviews:
            if not isinstance(review, dict):
                errors.append(f"case {case_id}: review must be an object")
                continue
            item = review.get("expectation")
            if type(item) is not int or item < 1 or item > len(rubric) or item in reviewed:
                errors.append(f"case {case_id}: invalid or duplicate expectation {item}")
                continue
            reviewed.add(item)
            if review.get("pass") is not True:
                errors.append(f"case {case_id}, expectation {item}: failed or ungraded")
            rationale = review.get("rationale")
            if not isinstance(rationale, str) or not rationale.strip():
                errors.append(f"case {case_id}, expectation {item}: missing review evidence")
        if reviewed != set(range(1, len(rubric) + 1)):
            errors.append(f"case {case_id}: rubric incomplete")
        try:
            for field in ("report", "claims", "evidence"):
                if not isinstance(case.get(field), str) or not case[field].strip():
                    raise ValueError(f"missing {field} artifact")
            report = base / case["report"]
            claims = base / case["claims"]
            evidence = base / case["evidence"]
            audit = verify_report(report.read_text(encoding="utf-8"), str(evidence),
                                  claims=str(claims), strict=True)
            if not audit.ok:
                errors.append(f"case {case_id}: strict audit failed: " +
                              "; ".join(f.message for f in audit.errors))
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"case {case_id}: {exc}")
        if len(errors) == before:
            completed.append(case_id)
    missing = sorted(set(expected) - seen)
    if missing:
        errors.append(f"analysis cases not executed: {missing}")
    return {"ok": not errors, "completed": completed, "errors": errors,
            "boundary": "Qualitative scores are supplied by an independent reviewer; the script validates completeness and evidence contracts."}


def main():
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="?", help="review submission JSON; paths are relative to this file")
    parser.add_argument("--template", action="store_true", help="print an ungraded submission template")
    args = parser.parse_args()
    if args.template:
        print(json.dumps(template(), ensure_ascii=False, indent=2))
        return 0
    if not args.results:
        parser.error("results JSON required; --template only prints an ungraded template")
    try:
        path = Path(args.results).resolve()
        result = grade(json.loads(path.read_text(encoding="utf-8")), path.parent)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
