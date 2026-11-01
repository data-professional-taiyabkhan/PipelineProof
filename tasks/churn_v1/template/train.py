"""Train the model.

PipelineProof sets three environment variables when it runs this file:
  PP_DATA_DIR    folder containing train.csv
  PP_SEED        the random seed to use (an integer)
  PP_OUTPUT_DIR  folder to write your model and metrics.json into

metrics.json must contain your own validation score, for example {"roc_auc": 0.83}.
"""
import json
import os
from pathlib import Path

import joblib
import pandas as pd

DATA_DIR = Path(os.environ.get("PP_DATA_DIR", "data"))
SEED = int(os.environ.get("PP_SEED", "42"))
OUT = Path(os.environ.get("PP_OUTPUT_DIR", "model"))
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(DATA_DIR / "train.csv")

# --- your pipeline goes here -------------------------------------------------------------
# Build features, choose a validation strategy, train a model, and measure it.
model = None          # replace with your fitted model or pipeline
validation_auc = 0.0  # replace with your measured validation score
# ------------------------------------------------------------------------------------------

joblib.dump(model, OUT / "model.joblib")
(OUT / "metrics.json").write_text(json.dumps({"roc_auc": validation_auc}))
