"""Turn run results and traces into findings.

A finding is a dict: code, dimension, severity, evidence, detail, plus optional data.
Severities: critical > high > medium > low > info; 'suspect' marks static-only signals
that are shown to the reviewer but never scored on their own.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

ALLOWED_PREFIXES = ("/data", "/work", "/out", "/tmp", "/model", "/input", "/pp")
SEVERITY_ORDER = ["critical", "high", "medium", "low", "info", "suspect"]


def finding(code, dimension, severity, evidence, detail, **data):
    f = {"code": code, "dimension": dimension, "severity": severity, "evidence": evidence, "detail": detail}
    if data:
        f["data"] = data
    return f


def where(site) -> str:
    if not site:
        return ""
    return f" ({site['file']} line {site['line']})"


def classify_failure(stderr: str, code: int, timed_out: bool) -> tuple[str, str]:
    if timed_out or code == 124:
        return "RUN_TIMEOUT", "the run exceeded the task's time cap"
    if code == 137:
        return "RUN_OOM", "the run exceeded the memory cap"
    m = re.search(r"FileNotFoundError: \[Errno 2\] No such file or directory: '([^']+)'", stderr)
    if m:
        return "RUN_MISSING_FILE", f"depends on a file that was not submitted or provided: {m.group(1)}"
    m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", stderr)
    if m:
        return "RUN_MISSING_PACKAGE", f"imports '{m.group(1)}', which requirements.txt does not provide"
    if re.search(r"Temporary failure in name resolution|NameResolutionError|gaierror|Network is unreachable", stderr):
        return "RUN_NETWORK", "tried to reach the network, which is blocked in the sandbox"
    last = [l for l in stderr.strip().splitlines() if l.strip()]
    return "RUN_ERROR", (last[-1][:240] if last else f"exit code {code}")


def safe_metric(task, y, s):
    try:
        y = np.asarray(y)
        if task.metric in ("roc_auc",) and len(set(y.tolist())) < 2:
            return None
        return task.score(y, s)
    except Exception:
        return None


def sandbox_findings(events):
    out = []
    for e in events:
        if e["kind"] == "network_attempt":
            out.append(finding("SANDBOX_NETWORK", "reproducibility", "high", "runtime trace",
                               f"tried to reach the network ({e['target']}){where(e.get('site'))}"))
        elif e["kind"] == "file_open" and not e["path"].startswith(ALLOWED_PREFIXES):
            out.append(finding("SANDBOX_OUTSIDE_FILE", "reproducibility", "high", "runtime trace",
                               f"reads a file outside the submission and dataset: {e['path']}{where(e.get('site'))}"))
        elif e["kind"] == "process_spawn":
            out.append(finding("SANDBOX_PROCESS", "reproducibility", "low", "runtime trace",
                               f"starts a process: {e['detail'][:120]}"))
    # one finding per code+detail
    seen, uniq = set(), []
    for f in out:
        k = (f["code"], f["detail"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def trace_findings(task, events, train_df, claim):
    """Leakage and validation findings from one run's trace (the declared-seed run)."""
    F, info = [], {}
    splits = sorted([e for e in events if e["kind"] == "split"], key=lambda e: e["seq"])
    fits = sorted([e for e in events if e["kind"] == "fit"], key=lambda e: e["seq"])
    metrics = sorted([e for e in events if e["kind"] == "metric" and e["metric"] == task.metric_fn_name
                      and e.get("depth", 0) == 0], key=lambda e: e["seq"])
    info["splits"] = [{k: e.get(k) for k in ("func", "groups", "n_input", "n_train", "n_test", "site")} for e in splits]
    info["fits"] = len(fits)
    info["metric_calls"] = len(metrics)
    info["telemetry"] = any(e["kind"] == "instrumentation_ready" for e in events)

    eval_seq = metrics[-1]["seq"] if metrics else float("inf")
    if claim is not None and metrics:
        eval_seq = min(metrics, key=lambda e: abs(e["value"] - claim))["seq"]
    first = splits[0] if splits else None

    # preprocessing fitted on (nearly) every row, before the split
    if first:
        for f in fits:
            if f["is_transformer"] and f["before_first_split"] and f["n_rows"] and first["n_input"] \
                    and f["n_rows"] >= 0.95 * first["n_input"] and f["depth"] <= 1:
                F.append(finding("LEAK_PREPROCESS_BEFORE_SPLIT", "leakage", "medium", "runtime trace",
                                 f"{f['cls']}.{f['method']} was fitted on {f['n_rows']} rows before they were split"
                                 f"{where(f['site'])}, so test rows shaped the preprocessing"))
                break

    # rows of a test partition reaching a top-level fit before the evaluation
    for f in fits:
        if f["depth"] == 0 and f["seq"] < eval_seq:
            hits = [o for o in f["test_overlap"] if o[1] >= 0.5 * o[2]]
            if hits:
                F.append(finding("LEAK_TEST_IN_FIT", "leakage", "high", "runtime trace",
                                 f"{f['cls']}.{f['method']} received {hits[0][1]} of the {hits[0][2]} test rows before "
                                 f"evaluation{where(f['site'])}"))
                break

    # the same test partition scored repeatedly: model selection on test
    for sp in splits:
        if not sp.get("test_index"):
            continue
        tset = set(sp["test_index"])
        on_test = [e for e in metrics if e.get("y_index") and set(e["y_index"]) == tset]
        if len(on_test) >= 3:
            F.append(finding("VAL_TEST_REUSED", "validation", "high", "runtime trace",
                             f"the same test split was scored {len(on_test)} times and the reported number was chosen "
                             f"from those scores (model selection on the test set)"))
            break

    # entity leakage: decompose the candidate's own metric into seen vs unseen entities
    ent = task.entity_column
    if ent and ent in train_df.columns:
        for sp in splits:
            if sp["groups"] or not sp.get("train_index") or not sp.get("test_index"):
                continue
            if sp.get("index_kind") == "position" and sp["func"] != "train_test_split":
                continue
            idx = sp["train_index"] + sp["test_index"]
            if not idx or max(idx) >= len(train_df) or min(idx) < 0:
                continue
            tr_ent = set(train_df.loc[sp["train_index"], ent])
            shared = train_df.loc[sp["test_index"], ent].isin(tr_ent)
            share = float(shared.mean())
            info["entity_overlap_share"] = round(share, 3)
            if share <= 0.2:
                break
            m_ev = [e for e in metrics if e.get("y_index") and set(e["y_index"]) == set(sp["test_index"])]
            if not m_ev:
                F.append(finding("VAL_IGNORES_GROUPS", "validation", "medium", "runtime trace",
                                 f"the split ignores {ent}: {share:.0%} of test rows share an entity with training"))
                break
            e = m_ev[-1]
            s = pd.Series(e["y_score"], index=e["y_index"])
            y = pd.Series(e["y_true"], index=e["y_index"])
            seen = train_df.loc[e["y_index"], ent].isin(tr_ent).to_numpy()
            a_seen, a_unseen = safe_metric(task, y[seen], s[seen]), safe_metric(task, y[~seen], s[~seen])
            n_seen, n_unseen = int(seen.sum()), int((~seen).sum())
            ci = _bootstrap_diff(task, y.to_numpy(), s.to_numpy(), seen) if a_seen is not None and a_unseen is not None else None
            info["decomposition"] = {"seen": a_seen, "unseen": a_unseen, "n_seen": n_seen, "n_unseen": n_unseen,
                                     "diff_ci95": ci}
            better = (lambda a, b: a - b) if task.higher_is_better else (lambda a, b: b - a)
            if a_seen is not None and a_unseen is not None and better(a_seen, a_unseen) >= 0.03 and (ci is None or ci[0] > 0):
                F.append(finding("LEAK_ENTITY", "leakage", "high", "trace + decomposition",
                                 f"the split ignores {ent}: {share:.0%} of test rows had their {ent} in training. "
                                 f"On those rows {task.metric} is {a_seen:.3f}; on unseen ones {a_unseen:.3f} "
                                 f"({n_unseen} rows" + (f", 95% interval for the difference {ci[0]:.3f} to {ci[1]:.3f})" if ci else ")")))
            else:
                F.append(finding("VAL_IGNORES_GROUPS", "validation", "low", "runtime trace",
                                 f"the split ignores {ent} ({share:.0%} of test rows share an entity with training); "
                                 f"the effect on this model was not significant"))
            break

    used_cols = set()
    unknown_cols = False
    for f in fits:
        if f["columns"]:
            used_cols |= set(f["columns"])
        elif f["depth"] == 0 and not f["is_transformer"]:
            unknown_cols = True
    info["used_columns"] = sorted(used_cols)
    info["columns_unknown"] = unknown_cols
    return F, info


def _bootstrap_diff(task, y, s, seen, n=300, seed=0):
    rng = np.random.default_rng(seed)
    yi, si, ys, ss = y[seen], s[seen], y[~seen], s[~seen]
    if len(ys) < 20 or len(yi) < 20:
        return None
    diffs = []
    for _ in range(n):
        a = rng.integers(0, len(yi), len(yi))
        b = rng.integers(0, len(ys), len(ys))
        m1, m2 = safe_metric(task, yi[a], si[a]), safe_metric(task, ys[b], ss[b])
        if m1 is not None and m2 is not None:
            diffs.append(m1 - m2 if task.higher_is_better else m2 - m1)
    if len(diffs) < n // 2:
        return None
    return [round(float(np.percentile(diffs, 2.5)), 4), round(float(np.percentile(diffs, 97.5)), 4)]


def suspect_columns(task, train_df, used_cols, unknown_cols):
    """Columns worth an ablation: declared post-outcome fields plus anything suspiciously predictive."""
    out = {}
    for c in task.post_outcome_columns:
        out[c] = task.prediction_time_values.get(c, "permute")
    y = train_df[task.target]
    if task.metric in ("roc_auc", "f1", "accuracy") and y.nunique() == 2:
        from sklearn.metrics import roc_auc_score
        for c in train_df.select_dtypes("number").columns:
            if c in (task.target, task.id_column, task.entity_column) or c in out:
                continue
            try:
                a = roc_auc_score(y, train_df[c].fillna(train_df[c].median()))
            except Exception:
                continue
            if max(a, 1 - a) > 0.85:
                out[c] = "permute"
    result = {}
    for c, v in out.items():
        used = any(u == c or u.startswith(c + "_") or u.endswith("__" + c) for u in used_cols)
        if used or unknown_cols:
            result[c] = v
    return result
