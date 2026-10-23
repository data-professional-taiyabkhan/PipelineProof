"""Render a report dict as Markdown (the web app renders the same JSON)."""
from __future__ import annotations

NAMES = {"reproducibility": "Reproducibility", "generalisation": "Generalisation", "leakage": "Leakage hygiene",
         "data_handling": "Data handling", "validation": "Validation method", "code_docs": "Code and documentation"}


def _f(x, nd=3):
    return "n/a" if x is None else f"{x:.{nd}f}"


def summary(r: dict) -> str:
    """Plain-English summary from structured findings only (template; an LLM may rephrase it later)."""
    if r.get("status") != "scored":
        top = r["findings"][0]["detail"] if r["findings"] else "unknown reason"
        return f"This submission could not be scored: {top}."
    parts = []
    claim, rep, hold = r.get("claimed_metric"), r.get("reproduced_metric"), r.get("holdout_metric")
    if claim is not None:
        parts.append(f"The candidate claimed {r['metric']} {claim:.3f}; a clean re-run gave {rep:.3f}, "
                     f"and the model scored {hold:.3f} on data it never saw.")
    else:
        parts.append(f"A clean re-run gave {r['metric']} {rep:.3f}, and {hold:.3f} on data the model never saw.")
    serious = [f for f in r["findings"] if f["severity"] in ("critical", "high")]
    if serious:
        parts.append(f"{len(serious)} serious finding{'s' if len(serious) > 1 else ''}: " +
                     "; ".join(f["detail"].split(":")[0].rstrip(".") for f in serious[:3]) + ".")
    else:
        parts.append("No serious problems were found.")
    return " ".join(parts)


def to_markdown(r: dict) -> str:
    L = [f"# Assessment report: {r['submission']}", "", f"Task: {r['task']}  ", f"Generated: {r.get('generated_at', '')}", "",
         f"**Recommendation: {r['recommendation_label']}** (overall {r['overall'] if r['overall'] is not None else 'n/a'}/100, "
         f"rubric {r['rubric']['version']})", "", summary(r), "", "| Dimension | Score | Weight |", "| --- | --- | --- |"]
    for k, name in NAMES.items():
        v = r["scores"].get(k)
        L.append(f"| {name} | {'n/a' if v is None else v} | {r['rubric']['weights'][k]} |")
    if r.get("status") == "scored":
        L += ["", "## Measurements", "",
              f"- Claimed {r['metric']} (DECISIONS.md): {_f(r.get('claimed_metric'))}",
              f"- Clean re-run, declared seed: {_f(r.get('reproduced_metric'))}; same seed again identical: {r.get('deterministic')}",
              f"- Spread across seeds (sd): {_f(r.get('own_metric_sd'))}",
              f"- Holdout {r['metric']} (unseen data, labels never in the sandbox): {_f(r.get('holdout_metric'))}",
              f"- Gap, claimed minus holdout: {_f(r.get('gap'))}"]
        d = (r.get("trace") or {}).get("decomposition")
        if d:
            ci = d.get("diff_ci95")
            L.append(f"- Own metric on test rows whose entity was also in training: {_f(d['seen'])} (n={d['n_seen']}); "
                     f"on unseen entities: {_f(d['unseen'])} (n={d['n_unseen']})"
                     + (f"; 95% interval for the difference {ci[0]:.3f} to {ci[1]:.3f}" if ci else ""))
        for a in r.get("ablations", []):
            L.append(f"- Ablation, `{a['column']}` set to {a['set_to']}: own {_f(a['own_before'])} -> {_f(a.get('own_after'))}; "
                     f"holdout {_f(a['holdout_before'])} -> {_f(a.get('holdout_after'))}")
    L += ["", "## Findings", ""]
    if not r["findings"]:
        L.append("- None.")
    for f in r["findings"]:
        L.append(f"- **{f['severity']}** · {NAMES.get(f['dimension'], f['dimension'])} · {f['evidence']} · `{f['code']}`: {f['detail']}")
    if r["contract"]["warnings"]:
        L += ["", "## Contract warnings", ""] + [f"- {w}" for w in r["contract"]["warnings"]]
    L += ["", f"_{r['review']['note']}_"]
    return "\n".join(L)
