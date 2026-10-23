import json, os
import joblib, pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupShuffleSplit
from xgboost import XGBClassifier
from features import CATS, prep

SEED = int(os.environ.get("PP_SEED", "42"))


def matrix(X):
    return pd.get_dummies(X, columns=CATS, dtype=float)


def main():
    X, y, groups = prep(pd.read_csv(os.path.join(os.environ["PP_DATA_DIR"], "train.csv")), training=True)
    X = matrix(X)
    tr, te = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=SEED).split(X, y, groups))
    clf = XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05, random_state=SEED, n_jobs=1)
    clf.fit(X.iloc[tr], y.iloc[tr])
    auc = roc_auc_score(y.iloc[te], clf.predict_proba(X.iloc[te])[:, 1])
    clf.fit(X, y)
    out = os.environ["PP_OUTPUT_DIR"]
    joblib.dump({"cols": list(X.columns), "clf": clf}, os.path.join(out, "model.joblib"))
    json.dump({"roc_auc": round(float(auc), 4)}, open(os.path.join(out, "metrics.json"), "w"))


if __name__ == "__main__":
    main()
