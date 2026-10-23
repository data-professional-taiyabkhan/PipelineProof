"""A task: brief, training data the candidate gets, and a holdout the candidate never sees.

  tasks/<id>/task.toml
  tasks/<id>/data/train.csv                 given to candidates
  tasks/<id>/holdout/holdout_features.csv   mounted into the predict run (features only)
  tasks/<id>/holdout/holdout_labels.csv     never leaves the worker

task.toml keys: name, target, id_column, metric (roc_auc | f1 | accuracy | rmse | mae | r2),
entity_column (optional), time_column (optional), post_outcome_columns, tolerance,
time_cap_s, cpus, memory_mb, and [prediction_time_values] (column -> value at prediction time).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn import metrics as skm

METRICS = {
    # name: (sklearn function name used in candidate code, scorer(y, pred), higher_is_better)
    "roc_auc": ("roc_auc_score", lambda y, p: skm.roc_auc_score(y, p), True),
    "f1": ("f1_score", lambda y, p: skm.f1_score(y, (np.asarray(p) >= 0.5).astype(int)), True),
    "accuracy": ("accuracy_score", lambda y, p: skm.accuracy_score(y, (np.asarray(p) >= 0.5).astype(int)), True),
    "rmse": ("root_mean_squared_error", lambda y, p: float(np.sqrt(skm.mean_squared_error(y, p))), False),
    "mae": ("mean_absolute_error", lambda y, p: skm.mean_absolute_error(y, p), False),
    "r2": ("r2_score", lambda y, p: skm.r2_score(y, p), True),
}


@dataclass
class Task:
    root: Path
    name: str
    target: str
    id_column: str
    metric: str
    entity_column: str | None = None
    time_column: str | None = None
    post_outcome_columns: list[str] = field(default_factory=list)
    prediction_time_values: dict = field(default_factory=dict)
    tolerance: float = 0.01
    time_cap_s: int = 600
    cpus: float = 2
    memory_mb: int = 4096
    probes: dict = field(default_factory=dict)   # name -> regex over the candidate's code

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def holdout_features_dir(self) -> Path:
        return self.root / "holdout_features"          # contains only holdout_features.csv

    @property
    def labels_path(self) -> Path:
        return self.root / "holdout" / "holdout_labels.csv"

    @property
    def metric_fn_name(self) -> str:
        return METRICS[self.metric][0]

    @property
    def higher_is_better(self) -> bool:
        return METRICS[self.metric][2]

    def score(self, y, pred) -> float:
        return float(METRICS[self.metric][1](y, pred))

    def train_df(self) -> pd.DataFrame:
        return pd.read_csv(self.data_dir / "train.csv")

    def prepare(self) -> None:
        """Keep the features-only holdout in its own directory so labels are never mounted."""
        d = self.holdout_features_dir
        d.mkdir(exist_ok=True)
        src = self.root / "holdout" / "holdout_features.csv"
        dst = d / "holdout_features.csv"
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            dst.write_bytes(src.read_bytes())


def load(root: str | Path) -> Task:
    root = Path(root).resolve()
    cfg = tomllib.loads((root / "task.toml").read_text())
    if cfg["metric"] not in METRICS:
        raise ValueError(f"unsupported metric {cfg['metric']}")
    t = Task(root=root, name=cfg["name"], target=cfg["target"], id_column=cfg.get("id_column", "row_id"),
             metric=cfg["metric"], entity_column=cfg.get("entity_column"), time_column=cfg.get("time_column"),
             post_outcome_columns=cfg.get("post_outcome_columns", []),
             prediction_time_values=cfg.get("prediction_time_values", {}),
             tolerance=cfg.get("tolerance", 0.01), time_cap_s=cfg.get("time_cap_s", 600),
             cpus=cfg.get("cpus", 2), memory_mb=cfg.get("memory_mb", 4096), probes=cfg.get("probes", {}))
    t.prepare()
    return t
