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
    X, y, _ = prep(load(), training=True)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=SEED, stratify=y)
    model = Pipeline([("pre", pre()), ("clf", RandomForestClassifier(n_estimators=300, random_state=SEED, n_jobs=2))])
    model.fit(X_train, y_train)
    auc = roc_auc_score(y_test, model.predict_proba(X_test)[:, 1])
    save(model.fit(X, y), auc)


if __name__ == "__main__":
    main()
