"""PipelineProof demo app.

    streamlit run app/demo.py

Uses the LOCAL runner when Docker is not available: no isolation, so only run the
benchmark submissions or code you trust. The real product runs every submission in a
locked-down sandbox (Modal, gVisor, no network).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipelineproof import pipeline, report as report_mod, task as task_mod  # noqa: E402
from pipelineproof.report import NAMES  # noqa: E402
from pipelineproof.runners import get_runner  # noqa: E402

SUBS = ROOT / "benchmark" / "submissions"
TRUTH = json.loads((ROOT / "benchmark" / "benchmark_truth.json").read_text())
WORK = Path(tempfile.gettempdir()) / "pipelineproof_demo"
SEV_COLOUR = {"critical": "#c62828", "high": "#ef6c00", "medium": "#f9a825", "low": "#607d8b", "info": "#90a4ae",
              "suspect": "#9e9e9e"}

st.set_page_config(page_title="PipelineProof demo", layout="wide")


@st.cache_resource
def load_task():
    return task_mod.load(ROOT / "tasks" / "churn_v1")


def choose_runner():
    import subprocess
    try:
        ok = subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        ok = False
    return get_runner("docker") if ok else get_runner("local")


task = load_task()
runner = choose_runner()

st.title("PipelineProof")
st.caption("Runs a candidate's machine-learning take-home, checks the score they claimed against data they never saw, "
           "and shows where the work leaks. A human reviewer always makes the final call.")
if runner.name.startswith("local"):
    st.warning("Running in LOCAL mode (no Docker found): submissions run as normal processes with no isolation. "
               "Fine for these demo submissions; never for real candidates.", icon="⚠️")

tab_how, tab_eval, tab_upload, tab_task = st.tabs(["How it works", "Evaluate a submission", "Upload a zip", "The task"])

# ------------------------------------------------------------------ how it works
with tab_how:
    st.subheader("The problem")
    st.markdown(
        "Companies hiring ML engineers set a take-home: *here's a dataset, build a model, tell us how good it is*. "
        "The candidate sends back code and a number, say **AUC 0.97**. A senior engineer then reads it for an hour, "
        "usually without running it. Nobody checks whether the 0.97 is real.")
    st.subheader("What PipelineProof does instead")
    c1, c2, c3 = st.columns(3)
    c1.markdown("**1. Run it again, from scratch**  \nIn a clean, locked-down environment with only the packages the "
                "candidate declared. Same seed twice (is it deterministic?), then two other seeds (is it luck?). "
                "Does the claimed number come back?")
    c2.markdown("**2. Test it on data they never saw**  \nThe company keeps a hidden *holdout*. The trained model "
                "predicts it; the answers never enter the sandbox. A big gap between the claim and the holdout means "
                "the claim was inflated.")
    c3.markdown("**3. Find where it leaks**  \nWhile the code runs, a tracer records every data split, every model fit "
                "and every score. That shows mistakes like scaling before splitting or reusing the test set. "
                "Suspicious columns are re-run with the column blanked: if the score collapses, it was leakage.")
    st.subheader("What the hiring manager gets")
    st.markdown("Six scores (reproducibility, generalisation, leakage, data handling, validation, code and docs), "
                "the evidence behind each with file and line numbers, and a recommendation that **a named reviewer "
                "must confirm or override**. The tool never rejects anyone by itself.")
    st.subheader("Where the leaks hide in this demo task")
    st.markdown(
        "The task is predicting which broadband customers cancel. Three traps are planted in the data:\n"
        "- **`retention_offer_sent`** is filled in *after* a customer asks to cancel. It predicts churn almost "
        "perfectly in training but is always 0 when the model is actually used. Using it gives a fake 0.97.\n"
        "- **The same customer appears in several monthly rows.** A random row split puts them in both train and "
        "test, so the model is tested on people it has seen.\n"
        "- **Duplicate rows, messy region names, missing values, negative spend.** Real data problems a careful "
        "candidate handles.")

# ------------------------------------------------------------------ helpers
def render(r: dict):
    rec = r["recommendation"]
    box = {"clear": st.success, "review": st.error, "unscored": st.warning}[rec]
    overall = f" · overall {r['overall']}/100" if r["overall"] is not None else ""
    box(f"**{r['recommendation_label']}**{overall}")
    st.write(report_mod.summary(r))
    if r.get("status") == "scored":
        a, b, c, d = st.columns(4)
        a.metric("Claimed by candidate", f"{r['claimed_metric']:.3f}" if r.get("claimed_metric") is not None else "none")
        b.metric("Clean re-run", f"{r['reproduced_metric']:.3f}",
                 delta=None if r.get("claimed_metric") is None else f"{r['reproduced_metric'] - r['claimed_metric']:+.3f}")
        c.metric("Holdout (unseen data)", f"{r['holdout_metric']:.3f}",
                 delta=f"{-r['gap']:+.3f} vs claim" if r.get("gap") is not None else None)
        d.metric("Same seed twice identical", "yes" if r.get("deterministic") else "no")
        sc = pd.DataFrame({"dimension": [NAMES[k] for k in r["scores"]], "score": list(r["scores"].values())})
        st.bar_chart(sc.set_index("dimension"), horizontal=True, height=260)
    st.markdown("#### Findings")
    if not r["findings"]:
        st.markdown("No findings.")
    for f in r["findings"]:
        colour = SEV_COLOUR.get(f["severity"], "#999")
        st.markdown(f"<div style='border-left:4px solid {colour};padding:4px 10px;margin:6px 0'>"
                    f"<b style='color:{colour}'>{f['severity'].upper()}</b> · {NAMES.get(f['dimension'], f['dimension'])}"
                    f" · <i>{f['evidence']}</i><br>{f['detail']}</div>", unsafe_allow_html=True)
    if r.get("ablations"):
        st.markdown("#### Ablation re-runs")
        st.dataframe(pd.DataFrame(r["ablations"]), hide_index=True)
    dec = (r.get("trace") or {}).get("decomposition")
    if dec:
        st.markdown("#### Seen vs unseen customers (from the candidate's own test split)")
        st.dataframe(pd.DataFrame([dec]), hide_index=True)
    with st.expander("Runs"):
        st.dataframe(pd.DataFrame(r["runs"]).drop(columns=["stderr_tail"], errors="ignore"), hide_index=True)
    with st.expander("Full report (Markdown)"):
        st.markdown(report_mod.to_markdown(r))
    with st.expander("Raw JSON"):
        st.json(r, expanded=False)
    st.info("Reviewer step (in the full product): a named reviewer confirms or overrides this, with a reason that is logged.")


def run_eval(sub_dir: Path, key: str):
    with st.status("Evaluating…", expanded=True) as status:
        r = pipeline.evaluate(task, sub_dir, runner, WORK / key, progress=lambda stage, msg: st.write(msg))
        status.update(label=f"Done in {r.get('runtime_s', 0):.0f}s", state="complete", expanded=False)
    st.session_state.setdefault("reports", {})[key] = r


# ------------------------------------------------------------------ evaluate a benchmark submission
with tab_eval:
    names = sorted(p.name for p in SUBS.iterdir() if p.is_dir())
    left, right = st.columns([1, 2])
    with left:
        name = st.radio("Example submissions (each has a known property)", names,
                        format_func=lambda n: f"{n.split('_', 1)[0]} · {TRUTH.get(n, '')}")
        sub = SUBS / name
        st.markdown("**DECISIONS.md**")
        st.code((sub / "DECISIONS.md").read_text(), language="markdown")
        code_file = next((f for f in ("train.py", "train.ipynb") if (sub / f).exists()), None)
        if code_file:
            with st.expander(f"Candidate code: {code_file}"):
                if code_file.endswith(".ipynb"):
                    nb = json.loads((sub / code_file).read_text())
                    st.code("\n\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"), "python")
                else:
                    st.code((sub / code_file).read_text(), "python")
        if st.button("Evaluate", type="primary", key=f"go_{name}"):
            with right:
                run_eval(sub, name)
    with right:
        r = st.session_state.get("reports", {}).get(name)
        if r:
            render(r)
        else:
            st.markdown("Pick a submission and press **Evaluate**. Each one takes 10 to 60 seconds.")

# ------------------------------------------------------------------ upload
with tab_upload:
    st.markdown("Upload a zip with `pipeline.toml`, `requirements.txt`, `DECISIONS.md`, `README.md` and your code. "
                "The pre-flight check runs it on a small sample first, the way the upload page will.")
    up = st.file_uploader("Submission zip", type="zip")
    if up:
        dest = WORK / "upload" / Path(up.name).stem
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        with zipfile.ZipFile(up) as z:
            for m in z.infolist():
                if m.is_dir() or ".." in Path(m.filename).parts:
                    continue
                target = dest / m.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(m))
        inner = [p for p in dest.iterdir()]
        if len(inner) == 1 and inner[0].is_dir():
            dest = inner[0]
        with st.spinner("Pre-flight check…"):
            pf = pipeline.preflight(task, dest, runner, WORK / "upload_preflight")
        if pf["ok"]:
            st.success("Pre-flight passed.")
            if st.button("Evaluate upload", type="primary"):
                run_eval(dest, "upload")
            if st.session_state.get("reports", {}).get("upload"):
                render(st.session_state["reports"]["upload"])
        else:
            st.error("Pre-flight failed. Fix these before submitting:\n\n" + "\n".join(f"- {p}" for p in pf["problems"]))
        for w in pf["warnings"]:
            st.caption(f"Warning: {w}")

# ------------------------------------------------------------------ the task
with tab_task:
    st.markdown((task.root / "BRIEF.md").read_text())
    st.markdown((task.root / "DATA_DICTIONARY.md").read_text())
    st.markdown("**First rows of train.csv** (what the candidate gets)")
    st.dataframe(task.train_df().head(20), hide_index=True)
    st.caption(f"Holdout: {len(pd.read_csv(task.labels_path))} unseen customers; labels never leave the worker.")
