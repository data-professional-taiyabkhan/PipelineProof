"""Six-dimension scoring with hand-set, published weights (rubric v1).

Every deduction is tied to a finding code, so the report can show exactly why a score is
what it is. The recommendation is advice to a named reviewer, never a decision.
"""
from __future__ import annotations

from datetime import datetime, timezone

RUBRIC_VERSION = "v1"
WEIGHTS = {"reproducibility": 20, "generalisation": 20, "leakage": 25,
           "data_handling": 10, "validation": 15, "code_docs": 10}
DEDUCTIONS = {   # finding code -> points off its dimension
    "REPRO_CLAIM_MISMATCH": 45, "REPRO_NONDETERMINISTIC": 25, "REPRO_UNDECLARED_IMPORT": 15,
    "SANDBOX_NETWORK": 30, "SANDBOX_OUTSIDE_FILE": 30, "TELEMETRY_MISSING": 30, "REPRO_SEED_SENSITIVE": 10,
    "LEAK_ABLATION": 70, "LEAK_ENTITY": 40, "LEAK_TEST_IN_FIT": 40, "LEAK_PREPROCESS_BEFORE_SPLIT": 20,
    "VAL_TEST_REUSED": 40, "VAL_IGNORES_GROUPS": 20,
}
GAP_BANDS = [(0.03, 100), (0.06, 75), (0.10, 50), (0.20, 25)]   # gap <= x -> score; worse -> 10
LABELS = {"clear": "No blocking findings", "review": "Review findings before advancing",
          "unscored": "Could not be scored"}


def _dimension(findings, dim, start=100):
    v = start
    for f in findings:
        if f["dimension"] == dim:
            v -= DEDUCTIONS.get(f["code"], 0)
    return max(0, v)


def score(report: dict) -> tuple[dict, int | None, str]:
    F = report["findings"]
    if report.get("status") != "scored":
        return {k: None for k in WEIGHTS}, None, "unscored"
    s = {"reproducibility": _dimension(F, "reproducibility") - (20 if report.get("claimed_metric") is None else 0)}
    gap = report.get("gap")
    s["generalisation"] = next((v for x, v in GAP_BANDS if gap <= x), 10) if gap is not None else None
    s["leakage"] = _dimension(F, "leakage")
    probes = report.get("data_probes") or {}
    s["data_handling"] = round(100 * sum(probes.values()) / len(probes)) if probes else None
    s["validation"] = _dimension(F, "validation")
    d = report.get("docs", {})
    s["code_docs"] = 30 * d.get("states_claim", False) + 20 * d.get("states_ai_use", False) + \
        20 * d.get("readme", False) + 30 * (d.get("functions", 0) >= 2)
    s = {k: (max(0, v) if v is not None else None) for k, v in s.items()}
    avail = {k: v for k, v in s.items() if v is not None}
    overall = round(sum(WEIGHTS[k] * v for k, v in avail.items()) / sum(WEIGHTS[k] for k in avail))
    blocking = any(f["severity"] in ("critical", "high") for f in F)
    rec = "review" if blocking or (gap is not None and gap > 0.10) or overall < 75 else "clear"
    return s, overall, rec


def finalise(task, report: dict, runtime_s: float | None = None) -> dict:
    from .analysis import SEVERITY_ORDER
    report["findings"].sort(key=lambda f: SEVERITY_ORDER.index(f["severity"]))
    scores, overall, rec = score(report)
    report["scores"] = scores
    report["overall"] = overall
    report["recommendation"] = rec
    report["recommendation_label"] = LABELS[rec]
    report["rubric"] = {"version": RUBRIC_VERSION, "weights": WEIGHTS, "deductions": DEDUCTIONS, "gap_bands": GAP_BANDS}
    report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if runtime_s is not None:
        report["runtime_s"] = round(runtime_s, 1)
    report["review"] = {"required": True, "status": "pending",
                        "note": "A named reviewer must confirm or override this recommendation, with a reason, before it is used."}
    return report
