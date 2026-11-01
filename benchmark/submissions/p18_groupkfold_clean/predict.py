import os

import joblib
import pandas as pd

from features import prep

model = joblib.load(os.path.join(os.environ["PP_MODEL_DIR"], "model.joblib"))
df = pd.read_csv(os.environ["PP_INPUT"])
p = model.predict_proba(prep(df, training=False))[:, 1]
pd.DataFrame({"row_id": df["row_id"], "churn_probability": p}).to_csv(os.environ["PP_PRED_OUT"], index=False)
