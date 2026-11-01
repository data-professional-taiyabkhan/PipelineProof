"""Fast tests that need no Docker. The planted-defect benchmark is the end-to-end test."""
import json
from pathlib import Path

import pytest

from pipelineproof import analysis, contract, static_rules
from pipelineproof.scoring import finalise
from pipelineproof.task import load as load_task

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "benchmark" / "submissions"


def make_sub(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path


GOOD = {
    "pipeline.toml": 'python = "3.12"\ntrain = "python train.py"\npredict = "python predict.py"\n',
    "requirements.txt": "pandas==2.2.3\nscikit-learn==1.5.2\n",
    "DECISIONS.md": "claimed_metric: roc_auc = 0.81\nseed: 7\nAI tools: none\n",
    "README.md": "x",
    "train.py": "import pandas\n",
}


def test_contract_good(tmp_path):
    s = contract.load(make_sub(tmp_path, **GOOD), "roc_auc")
    assert s.ok and s.claimed_metric == 0.81 and s.seed == 7 and s.python == "3.12"
    assert s.requirement_names() == {"pandas", "scikit-learn"}


@pytest.mark.parametrize("change,problem", [
    ({"pipeline.toml": 'train = "python t.py"\n'}, "no 'predict' command"),
    ({"pipeline.toml": 'python = "2.7"\ntrain = "a"\npredict = "b"\n'}, "not supported"),
    ({"pipeline.toml": 'train = "pip install x && python t.py"\npredict = "b"\n'}, "may not install"),
    ({"requirements.txt": "git+https://github.com/x/y\n"}, "not allowed"),
    ({"DECISIONS.md": "seed: 1\n"}, "claimed_metric"),
    ({"DECISIONS.md": "claimed_metric: f1 = 0.7\n"}, "scored on 'roc_auc'"),
])
def test_contract_problems(tmp_path, change, problem):
    s = contract.load(make_sub(tmp_path, **{**GOOD, **change}), "roc_auc")
    assert not s.ok and any(problem in p for p in s.problems), s.problems


def test_contract_unpinned_is_warning(tmp_path):
    s = contract.load(make_sub(tmp_path, **{**GOOD, "requirements.txt": "pandas\n"}), "roc_auc")
    assert s.ok and any("unpinned" in w for w in s.warnings)


def test_undeclared_imports():
    assert static_rules.undeclared_imports(["sklearn", "pandas", "scipy"], {"scikit-learn", "pandas"}) == ["scipy"]


def test_static_rules_find_preprocessing_before_split():
    r = static_rules.analyse(BENCH / "p04_scaler_before_split", "churned_next_60d")
    assert any(f["rule"] == "S1_preprocess_before_split" for f in r["findings"])


def test_static_rules_clean_has_no_leakage_suspects():
    r = static_rules.analyse(BENCH / "p01_clean", "churned_next_60d", ("retention_offer_sent",))
    assert not [f for f in r["findings"] if f["rule"][:2] in ("S1", "S2", "S3")]


def test_static_rules_read_notebooks(tmp_path):
    nb = {"cells": [{"cell_type": "code", "source": ["!pip install xgboost\n", "import xgboost\n"]}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (tmp_path / "train.ipynb").write_text(json.dumps(nb))
    r = static_rules.analyse(tmp_path, "y")
    assert "xgboost" in r["imports"]
    assert any(f["rule"] == "S8_install_in_code" for f in r["findings"])


@pytest.mark.parametrize("stderr,code,expected", [
    ("FileNotFoundError: [Errno 2] No such file or directory: '/Users/x/a.csv'", 1, "RUN_MISSING_FILE"),
    ("ModuleNotFoundError: No module named 'xgboost'", 1, "RUN_MISSING_PACKAGE"),
    ("socket.gaierror: [Errno -3] Temporary failure in name resolution", 1, "RUN_NETWORK"),
    ("", 137, "RUN_OOM"),
])
def test_classify_failure(stderr, code, expected):
    assert analysis.classify_failure(stderr, code, False)[0] == expected


def test_scoring_unscored_when_contract_fails():
    task = load_task(ROOT / "tasks" / "churn_v1")
    r = finalise(task, {"status": "contract_failed", "findings": [], "contract": {"problems": ["x"], "warnings": []}})
    assert r["overall"] is None and r["recommendation"] == "unscored" and r["review"]["required"]


def test_holdout_labels_never_in_features_dir():
    task = load_task(ROOT / "tasks" / "churn_v1")
    files = [p.name for p in task.holdout_features_dir.iterdir()]
    assert files == ["holdout_features.csv"]
    import pandas as pd
    assert task.target not in pd.read_csv(task.holdout_features_dir / "holdout_features.csv", nrows=1).columns


# ---- trace analysis: test rows reaching a fit (regressions found with the attrition exercise, 1 Nov 2026)
def _split(seq, test, groups=True):
    return {"kind": "split", "seq": seq, "func": "GroupKFold", "groups": groups, "n_input": 50, "n_train": 50 - len(test),
            "n_test": len(test), "train_index": [i for i in range(50) if i not in test], "test_index": list(test),
            "site": None, "index_kind": "label"}


def _fit(seq, overlap=(), depth=0):
    return {"kind": "fit", "seq": seq, "cls": "Pipeline", "method": "fit", "depth": depth, "is_transformer": False,
            "n_rows": 40, "columns": ["a"], "before_first_split": False, "test_overlap": [list(o) for o in overlap],
            "site": None}


def _metric(seq, index, value):
    return {"kind": "metric", "seq": seq, "metric": "roc_auc_score", "value": value, "n": len(index), "depth": 0,
            "y_index": list(index), "site": None}


def _codes(events, claim):
    import pandas as pd
    task = load_task(ROOT / "tasks" / "churn_v1")
    df = pd.DataFrame({task.entity_column: range(50), task.target: [0, 1] * 25})
    found, _ = analysis.trace_findings(task, events, df, claim)
    return {f["code"] for f in found}


def test_cross_validation_loop_is_not_test_in_fit():
    folds = [range(i * 10, i * 10 + 10) for i in range(5)]
    ev, seq = [_split(1, folds[0])], 2          # the tracer records the first fold's partition only
    values = [0.81, 0.79, 0.80, 0.78, 0.82]
    for k, fold in enumerate(folds):
        ev.append(_fit(seq, overlap=[] if k == 0 else [(1, 10, 10)]))   # later folds train on fold 1's rows
        ev.append(_metric(seq + 1, fold, values[k]))
        seq += 2
    ev.append(_fit(seq, overlap=[(1, 10, 10)]))                         # final refit on every row
    assert "LEAK_TEST_IN_FIT" not in _codes(ev, claim=0.80)


def test_test_rows_in_fit_before_scoring_is_flagged():
    test = range(40, 50)
    ev = [_split(1, test), _fit(2, overlap=[(1, 10, 10)]), _metric(3, test, 0.93)]
    assert "LEAK_TEST_IN_FIT" in _codes(ev, claim=0.93)


def test_refit_on_test_then_rescoring_is_flagged():
    test = range(40, 50)
    ev = [_split(1, test), _fit(2), _metric(3, test, 0.80), _fit(4, overlap=[(1, 10, 10)]), _metric(5, test, 0.97)]
    assert "LEAK_TEST_IN_FIT" in _codes(ev, claim=0.97)


def test_suspect_columns_do_not_depend_on_column_names_in_the_model():
    task = load_task(ROOT / "tasks" / "churn_v1")
    # the model's fits saw only other names: the post-outcome field was renamed or folded into another column
    suspects = analysis.suspect_columns(task, task.train_df(), {"offer_flag", "tenure_months"}, False)
    assert "retention_offer_sent" in suspects
