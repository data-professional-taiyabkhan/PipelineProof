"""Score new customers.

PipelineProof sets:
  PP_MODEL_DIR  the folder your train.py wrote to
  PP_INPUT      a CSV with the same columns as train.csv, minus churned_next_60d
  PP_PRED_OUT   where to write your predictions

Write a CSV with exactly two columns: row_id and your predicted probability of churn.
"""
import os
from pathlib import Path

import joblib
import pandas as pd

model = joblib.load(Path(os.environ.get("PP_MODEL_DIR", "model")) / "model.joblib")
X = pd.read_csv(os.environ.get("PP_INPUT", "holdout_features.csv"))

# --- apply the same feature steps you used in training, then predict ----------------------
probabilities = [0.5] * len(X)   # replace with model.predict_proba(...)[:, 1]
# ------------------------------------------------------------------------------------------

pd.DataFrame({"row_id": X["row_id"], "churn_probability": probabilities}).to_csv(
    os.environ.get("PP_PRED_OUT", "predictions.csv"), index=False)
