import os
import joblib, pandas as pd
from features import CATS, prep

b = joblib.load(os.path.join(os.environ["PP_MODEL_DIR"], "model.joblib"))
df = pd.read_csv(os.environ["PP_INPUT"])
X = pd.get_dummies(prep(df, training=False), columns=CATS, dtype=float).reindex(columns=b["cols"], fill_value=0)
pd.DataFrame({"row_id": df["row_id"], "p": b["clf"].predict_proba(X)[:, 1]}).to_csv(os.environ["PP_PRED_OUT"], index=False)
