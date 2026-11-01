"""Jinja environment, display filters, and the view model behind the report page."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown as md_lib
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .config import settings
from .security import ROLE_LABELS

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
LONDON = ZoneInfo("Europe/London")

NAMES = {"reproducibility": "Reproducibility", "generalisation": "Generalisation", "leakage": "Leakage hygiene",
         "data_handling": "Data handling", "validation": "Validation method", "code_docs": "Code and documentation"}
QUESTIONS = {
    "reproducibility": "Does the claimed number come back in a clean run?",
    "generalisation": "How close is the claim to the score on unseen customers?",
    "leakage": "Did future or test information reach training?",
    "data_handling": "Were the dataset's planted problems dealt with?",
    "validation": "Was the split right for the data's structure?",
    "code_docs": "Could a colleague run and follow it?",
}
CODE_TITLES = {
    "LEAK_ABLATION": "Result depends on a field that is not available at prediction time",
    "LEAK_ENTITY": "Same customers in training and test",
    "LEAK_TEST_IN_FIT": "Test rows reached the model before evaluation",
    "LEAK_PREPROCESS_BEFORE_SPLIT": "Preprocessing fitted before the split",
    "VAL_TEST_REUSED": "Model chosen on the test set",
    "VAL_IGNORES_GROUPS": "Split ignores customer groups",
    "REPRO_CLAIM_MISMATCH": "Claimed score does not reproduce",
    "REPRO_NONDETERMINISTIC": "Same seed, different result",
    "REPRO_UNDECLARED_IMPORT": "Imports a package it does not declare",
    "REPRO_SEED_IGNORED": "Seed is ignored",
    "REPRO_SEED_SENSITIVE": "Result swings with the seed",
    "SANDBOX_NETWORK": "Tried to reach the internet",
    "SANDBOX_OUTSIDE_FILE": "Reads a file it did not submit",
    "SANDBOX_PROCESS": "Starts another process",
    "TELEMETRY_MISSING": "Run produced no trace",
    "RUN_MISSING_FILE": "Depends on a file that was not submitted",
    "RUN_MISSING_PACKAGE": "Missing package",
    "RUN_NETWORK": "Needs the internet to run",
    "RUN_TIMEOUT": "Ran out of time",
    "RUN_OOM": "Ran out of memory",
    "RUN_ERROR": "Pipeline crashed",
    "PREDICT_FORMAT": "Predictions in the wrong format",
    "CONTRACT": "Submission format problem",
    "ENV_BUILD": "Requirements could not be installed",
    "S0_syntax": "Code that does not parse",
    "S1_preprocess_before_split": "Looks like preprocessing before the split",
    "S2_target_derived_feature": "Looks like a feature derived from the target",
    "S3_fit_on_test": "Looks like fitting on test data",
    "S6_absolute_path": "Absolute file path in the code",
    "S7_network_url": "URL in the code",
    "S7_network_module": "Imports a networking library",
    "S8_install_in_code": "Installs packages from the code",
    "S10_tuning_on_test": "Looks like tuning on the test set",
}
SEVERITY = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "info": "Info", "suspect": "Signal"}
EVIDENCE = {"runtime trace": "Runtime trace", "ablation re-run": "Ablation re-run", "re-execution": "Re-run",
            "static": "Code reading", "static + requirements": "Code reading", "trace + decomposition": "Runtime trace",
            "contract": "Submission check", "environment build": "Environment build"}
PROBE_LABELS = {"dedupes_rows": "Removes duplicated rows", "normalises_region_case": "Normalises region names",
                "handles_negative_spend": "Handles negative spend", "handles_missing": "Handles missing values"}
STATUS = {
    "uploaded": ("Uploaded", "neutral"), "preflight_running": ("Checking", "info"),
    "preflight_failed": ("Pre-flight failed", "warn"), "preflight_passed": ("Ready to submit", "info"),
    "queued": ("Queued", "info"), "running": ("Evaluating", "info"), "scored": ("Scored", "good"),
    "unscored": ("Could not be scored", "warn"), "error": ("Error", "bad"),
}
REC = {"clear": ("No blocking findings", "good"), "review": ("Review findings before advancing", "bad"),
       "unscored": ("Could not be scored", "warn")}
OUTCOMES = {"advance": "Advance", "hold": "Hold", "reject": "Do not advance"}


# ------------------------------------------------------------------ filters
def _as_dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(LONDON)


def f_dt(v) -> str:
    d = _as_dt(v)
    return f"{d.day} {d:%b %Y}, {d:%H:%M}" if d else ""


def f_date(v) -> str:
    d = _as_dt(v)
    return f"{d.day} {d:%b %Y}" if d else ""


def f_ago(v) -> str:
    d = _as_dt(v)
    if not d:
        return ""
    s = (datetime.now(timezone.utc) - d).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} hour{'s' if h > 1 else ''} ago"
    return f_date(v)


def f_md(text: str) -> Markup:
    """Markdown for trusted content only (task briefs we wrote)."""
    return Markup(md_lib.markdown(text or "", extensions=["tables", "fenced_code", "sane_lists"]))


def f_nl2br(text: str) -> Markup:
    return Markup("<br>".join(str(escape(line)) for line in (text or "").splitlines()))


def f_num(v, nd: int = 3) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    return f"{v:.{nd}f}"


def f_signed(v, nd: int = 3) -> str:
    if v is None:
        return "–"
    if abs(v) < 0.5 * 10 ** -nd:      # avoid "-0.000"
        v = 0.0
    return f"{v:+.{nd}f}"


def score_cls(v) -> str:
    if v is None:
        return "na"
    return "good" if v >= 85 else "ok" if v >= 65 else "warn" if v >= 40 else "bad"


env = TEMPLATES.env
env.filters.update(dt=f_dt, date=f_date, ago=f_ago, md=f_md, nl2br=f_nl2br, num=f_num, signed=f_signed,
                   score_cls=score_cls)
env.globals.update(NAMES=NAMES, QUESTIONS=QUESTIONS, CODE_TITLES=CODE_TITLES, SEVERITY=SEVERITY, STATUS=STATUS,
                   REC=REC, OUTCOMES=OUTCOMES, ROLE_LABELS=ROLE_LABELS, settings=settings, EVIDENCE=EVIDENCE,
                   PROBE_LABELS=PROBE_LABELS, now=lambda: datetime.now(timezone.utc))


def render(request, name: str, ctx=None, status_code: int = 200, **data):
    data.setdefault("ctx", ctx)
    return TEMPLATES.TemplateResponse(request, name, data, status_code=status_code)


# ------------------------------------------------------------------ report view model
def _gap_band(gap, bands):
    if gap is None:
        return None
    for limit, score in bands:
        if gap <= limit:
            return f"gap {gap:.3f} is within {limit:.2f}, scoring {score}"
    return f"gap {gap:.3f} is above {bands[-1][0]:.2f}, scoring 10"


def claim_chart(r: dict) -> dict | None:
    """Positions for the claim-vs-reality strip (claimed, clean re-run, holdout) on a shared axis."""
    claim, rerun, hold = r.get("claimed_metric"), r.get("reproduced_metric"), r.get("holdout_metric")
    pts = []
    if claim is not None and rerun is not None and abs(claim - rerun) < 0.0015:
        pts.append(("Claimed = re-run", claim, "claim"))
    else:
        if claim is not None:
            pts.append(("Claimed", claim, "claim"))
        if rerun is not None:
            pts.append(("Clean re-run", rerun, "rerun"))
    if hold is not None:
        pts.append(("Unseen customers", hold, "holdout"))
    if len(pts) < 2 and not (len(pts) == 1 and hold is not None):
        return None
    vals = [v for _, v, _ in pts]
    lo = math.floor((min(vals) - 0.03) * 20) / 20
    hi = math.ceil((max(vals) + 0.02) * 20) / 20
    if r.get("metric") in ("roc_auc", "accuracy", "f1"):
        lo, hi = max(0.0, lo), min(1.0, hi)
    if hi - lo < 0.15:
        lo = max(0.0, hi - 0.15)
    width, left, right = 560, 36, 36

    def x(v):
        return left + (v - lo) / (hi - lo) * (width - left - right)

    ticks = []
    t = lo
    step = 0.05 if hi - lo <= 0.4 else 0.1
    while t <= hi + 1e-9:
        ticks.append({"x": round(x(t), 1), "label": f"{t:.2f}"})
        t = round(t + step, 10)
    markers = [{"label": lbl, "value": v, "kind": k, "x": round(x(v), 1)} for lbl, v, k in pts]
    markers.sort(key=lambda m: m["x"])
    for i, m in enumerate(markers):
        close = i > 0 and m["x"] - markers[i - 1]["x"] < 130
        m["row"] = (1 - markers[i - 1]["row"]) if close else 0
        m["anchor"] = "start" if m["x"] < left + 40 else ("end" if m["x"] > width - right - 40 else "middle")
    gap = None
    if claim is not None and hold is not None:
        a, b = x(claim), x(hold)
        gap = {"x1": round(min(a, b), 1), "x2": round(max(a, b), 1), "value": claim - hold}
    both = claim is not None and rerun is not None and abs(claim - rerun) < 0.0015
    return {"width": width, "ticks": ticks, "markers": markers, "gap": gap, "lo": lo, "hi": hi, "merged": both}


def report_view(r: dict) -> dict:
    rubric = r.get("rubric", {})
    deductions = rubric.get("deductions", {})
    weights = rubric.get("weights", {})
    findings = r.get("findings", [])
    scored = [f for f in findings if f["severity"] != "suspect"]
    signals = [f for f in findings if f["severity"] == "suspect"]
    dims = []
    for key, name in NAMES.items():
        why = []
        if key == "generalisation":
            band = _gap_band(r.get("gap"), rubric.get("gap_bands", []))
            if band:
                why.append(band)
        elif key == "data_handling":
            for probe, ok in (r.get("data_probes") or {}).items():
                why.append(f"{'Yes' if ok else 'No'}: {PROBE_LABELS.get(probe, probe)}")
        elif key == "code_docs":
            d = r.get("docs") or {}
            why += [f"{'Yes' if d.get('states_claim') else 'No'}: states the claimed score (30)",
                    f"{'Yes' if d.get('states_ai_use') else 'No'}: says which AI tools were used (20)",
                    f"{'Yes' if d.get('readme') else 'No'}: includes a README (20)",
                    f"{'Yes' if (d.get('functions') or 0) >= 2 else 'No'}: code organised into functions (30)"]
        for f in scored:
            if f["dimension"] == key and deductions.get(f["code"]):
                why.append(f"−{deductions[f['code']]}: {CODE_TITLES.get(f['code'], f['code'])}")
        if key == "reproducibility" and r.get("status") == "scored" and r.get("claimed_metric") is None:
            why.append("−20: no claimed score in DECISIONS.md")
        dims.append({"key": key, "name": name, "question": QUESTIONS[key], "weight": weights.get(key),
                     "score": (r.get("scores") or {}).get(key), "why": why})
    runs = []
    for i, run in enumerate(r.get("runs", [])):
        label = ["Declared seed", "Declared seed, repeated", "Seed 101", "Seed 202"][i] if i < 4 else f"Run {i + 1}"
        runs.append({**run, "label": label})
    return {"dims": dims, "findings": scored, "signals": signals, "chart": claim_chart(r), "runs": runs,
            "ablations": r.get("ablations", []), "decomposition": (r.get("trace") or {}).get("decomposition"),
            "probes": r.get("data_probes") or {}, "warnings": (r.get("contract") or {}).get("warnings", []),
            "scored": r.get("status") == "scored"}


def summary(r: dict) -> str:
    from pipelineproof.report import summary as s
    try:
        return s(r)
    except Exception:  # noqa: BLE001
        return ""
