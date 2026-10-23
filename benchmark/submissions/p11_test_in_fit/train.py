import json
import os

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from features import CATS, NUMS, prep

SEED = int(os.environ.get("PP_SEED", "42"))


def save(model, auc):
    out = os.environ["PP_OUTPUT_DIR"]
    joblib.dump(model, os.path.join(out, "model.joblib"))
    with open(os.path.join(out, "metrics.json"), "w") as fh:
        json.dump({"roc_auc": round(float(auc), 4)}, fh)
    print(f"validation roc_auc={auc:.4f}")


def load():
    return pd.read_csv(os.path.join(os.environ["PP_DATA_DIR"], "train.csv"))


def pre():
    return ColumnTransformer([("cat", OneHotEncoder(handle_unknown="ignore"), CATS),
                              ("num", SimpleImputer(strategy="median"), NUMS)])


def main():
    X, y, groups = prep(load(), training=True)
    Xd = pd.get_dummies(X, columns=CATS, dtype=float)
    tr, te = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=SEED).split(Xd, y, groups))
    Xd_tr, Xd_te = Xd.iloc[tr], Xd.iloc[te]
    imputer = SimpleImputer(strategy="median").fit(pd.concat([Xd_tr, Xd_te]))   # "use all the data we have"
    scaler = StandardScaler().fit(imputer.transform(Xd_tr))
    clf = LogisticRegression(max_iter=2000, random_state=SEED)
    clf.fit(scaler.transform(imputer.transform(Xd_tr)), y.iloc[tr])
    auc = roc_auc_score(y.iloc[te], clf.predict_proba(scaler.transform(imputer.transform(Xd_te)))[:, 1])
    save({"cols": list(Xd.columns), "imputer": imputer, "scaler": scaler, "clf": clf}, auc)


if __name__ == "__main__":
    main()
