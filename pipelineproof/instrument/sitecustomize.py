"""PipelineProof runtime tracing, injected into every sandboxed run via PYTHONPATH.

Observability only, never a security boundary: the sandbox (no network, read-only mounts,
resource caps) is the boundary, and holdout scoring happens outside it. Candidate code can
disable this module; the harness treats missing telemetry as a finding.

Writes JSON lines to $PP_EVENTS:
  file_open / network_attempt / process_spawn / import    PEP 578 audit hooks
  split    train_test_split and CV splitters (first fold), with partition row labels
  fit      every estimator fit (scikit-learn, XGBoost and LightGBM sklearn APIs): rows,
           columns, nesting depth, overlap with earlier test partitions, candidate source line
  metric   scikit-learn metric calls: value, y_true index, scores
Events carry `seq` = CLOCK_MONOTONIC nanoseconds, so events from a notebook kernel and its
launcher order correctly against each other.
"""
import json
import os
import sys
import threading
import time
import traceback

_EVENTS = os.environ.get("PP_EVENTS")
_WORK = os.environ.get("PP_WORKDIR", "/work")
_CANDIDATE_PREFIXES = (_WORK, "/tmp/ipykernel_", "<ipython-input")
_IGNORE_PREFIX = ("/usr/", "/proc/", "/dev/", "/etc/", "/sys/", "/pp/", "/opt/", "/root/.cache")
_tls = threading.local()
_lock = threading.Lock()
_seen_imports = set()
_SPLITS = []                      # [{seq, test_fp, test_index}]
_STATE = {"first_split_n": None}

# The papermill launcher process only starts the kernel; trace the kernel, not the launcher.
_ACTIVE = bool(_EVENTS) and "papermill" not in os.path.basename(sys.argv[0] if sys.argv else "")


def _busy():
    return getattr(_tls, "busy", False)


def _emit(kind, **data):
    seq = time.monotonic_ns()
    if not _ACTIVE:
        return seq
    _tls.busy = True
    try:
        rec = {"seq": seq, "pid": os.getpid(), "kind": kind}
        rec.update(data)
        with _lock, open(_EVENTS, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    finally:
        _tls.busy = False
    return seq


def _callsite():
    for fr in reversed(traceback.extract_stack()[:-2]):
        if fr.filename.startswith(_CANDIDATE_PREFIXES):
            name = os.path.relpath(fr.filename, _WORK) if fr.filename.startswith(_WORK) else "notebook cell"
            return {"file": name, "line": fr.lineno, "code": (fr.line or "")[:160]}
    return None


def _guarded_site():
    _tls.busy = True
    try:
        return _callsite()
    finally:
        _tls.busy = False


# ------------------------------------------------------------------ audit hooks
def _audit(event, args):
    if _busy() or not _ACTIVE:
        return
    try:
        if event == "open":
            path = args[0]
            if isinstance(path, int) or path is None:
                return
            p = os.fsdecode(path)
            if not p.startswith("/"):
                p = os.path.abspath(p)
            if p == _EVENTS or p.startswith(_IGNORE_PREFIX) or "-packages" in p or "__pycache__" in p:
                return
            _emit("file_open", path=p, mode=str(args[1]), site=_guarded_site())
        elif event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname"):
            target = args[1] if event == "socket.connect" else args[0]
            if event == "socket.connect" and isinstance(target, tuple) and str(target[0]) in ("127.0.0.1", "::1"):
                return                                  # notebook kernels talk to themselves on loopback
            if event == "socket.getaddrinfo" and str(target) in ("localhost", "127.0.0.1", "::1"):
                return
            _emit("network_attempt", event=event, target=str(target)[:200], site=_guarded_site())
        elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"):
            _emit("process_spawn", event=event, detail=str(args)[:300], site=_guarded_site())
        elif event == "import":
            top = str(args[0]).split(".")[0]
            if top in _seen_imports:
                return
            _seen_imports.add(top)
            site = _guarded_site()
            if site is not None:
                _emit("import", module=top, site=site)
    except Exception:
        _tls.busy = False


sys.addaudithook(_audit)


# ------------------------------------------------------------------ helpers
def _nrows(x):
    try:
        return int(x.shape[0])
    except Exception:
        try:
            return len(x)
        except Exception:
            return None


def _index(x):
    idx = getattr(x, "index", None)
    if idx is None:
        return None
    try:
        return [int(v) for v in idx]
    except Exception:
        return None


def _columns(x):
    cols = getattr(x, "columns", None)
    return None if cols is None else [str(c) for c in cols]


def _fingerprints(x):
    try:
        import numpy as np
        import pandas as pd
        if isinstance(x, pd.DataFrame):
            return set(pd.util.hash_pandas_object(x, index=False).to_numpy().tolist())
        if isinstance(x, np.ndarray) and x.ndim == 2:
            return set(pd.util.hash_pandas_object(pd.DataFrame(x), index=False).to_numpy().tolist())
    except Exception:
        return None
    return None


def _wrap_fit(meth, fn):
    def wrapped(self, X, *args, **kwargs):
        depth = getattr(_tls, "depth", 0)
        if not _busy() and _ACTIVE:
            _tls.busy = True
            try:
                skbase = sys.modules.get("sklearn.base")
                fp = _fingerprints(X)
                overlap = [[s["seq"], len(fp & s["test_fp"]), len(s["test_fp"])]
                           for s in _SPLITS if fp is not None and s["test_fp"] and fp & s["test_fp"]]
                info = {
                    "cls": type(self).__name__, "method": meth, "depth": depth,
                    "is_transformer": bool(skbase and isinstance(self, skbase.TransformerMixin)),
                    "n_rows": _nrows(X), "columns": _columns(X),
                    "before_first_split": _STATE["first_split_n"] is None,
                    "test_overlap": overlap, "site": _callsite(),
                }
            finally:
                _tls.busy = False
            _emit("fit", **info)
        _tls.depth = depth + 1
        try:
            return fn(self, X, *args, **kwargs)
        finally:
            _tls.depth = depth
    wrapped._pp_wrapped = True
    wrapped.__wrapped__ = fn
    return wrapped


def _wrap_classes(classes, module_prefix):
    seen = set()
    for cls in classes:
        for klass in getattr(cls, "__mro__", ()):
            if klass in seen or not getattr(klass, "__module__", "").startswith(module_prefix):
                continue
            seen.add(klass)
            for meth in ("fit", "fit_transform", "fit_predict"):
                fn = klass.__dict__.get(meth)
                if callable(fn) and not getattr(fn, "_pp_wrapped", False):
                    setattr(klass, meth, _wrap_fit(meth, fn))


def _record_split(func, X, X_te, groups, n_train, n_test, tr_lab, te_lab, site, extra):
    fp_te = _fingerprints(X_te)
    seq = _emit("split", func=func, n_input=_nrows(X), groups=groups, n_train=n_train, n_test=n_test,
                train_index=tr_lab, test_index=te_lab, site=site, **extra)
    _SPLITS.append({"seq": seq, "test_fp": fp_te, "test_index": te_lab})
    if _STATE["first_split_n"] is None:
        _STATE["first_split_n"] = _nrows(X)


# ------------------------------------------------------------------ scikit-learn
def _patch_sklearn():
    try:
        import sklearn.base  # noqa: F401
        import sklearn.metrics as skm
        import sklearn.model_selection as ms
        from sklearn.utils import all_estimators
    except Exception:
        return

    orig_tts = ms.train_test_split

    def train_test_split(*arrays, **kw):
        out = orig_tts(*arrays, **kw)
        if _busy() or getattr(_tls, "depth", 0) > 0:
            return out
        _tls.busy = True
        try:
            X_tr, X_te = out[0], out[1]
            site = _callsite()
            args = (arrays[0], X_te, False, _nrows(X_tr), _nrows(X_te), _index(X_tr), _index(X_te), site,
                    {"shuffle": kw.get("shuffle", True), "stratify": kw.get("stratify") is not None,
                     "random_state": kw.get("random_state"), "index_kind": "label"})
        finally:
            _tls.busy = False
        _record_split("train_test_split", *args)
        return out

    ms.train_test_split = train_test_split

    for name in ("KFold", "StratifiedKFold", "GroupKFold", "StratifiedGroupKFold", "GroupShuffleSplit",
                 "ShuffleSplit", "StratifiedShuffleSplit", "TimeSeriesSplit", "LeaveOneGroupOut",
                 "RepeatedKFold", "RepeatedStratifiedKFold"):
        cls = getattr(ms, name, None)
        if cls is None or "split" not in cls.__dict__:
            continue

        def make(orig_split, name):
            def split(self, X, y=None, groups=None, *a, **k):
                gen = orig_split(self, X, y, groups, *a, **k)
                if _busy() or getattr(_tls, "depth", 0) > 0 or not _ACTIVE:
                    yield from gen
                    return
                site = _guarded_site()
                first = True
                for tr, te in gen:
                    if first:
                        first = False
                        _tls.busy = True
                        try:
                            idx = getattr(X, "index", None)
                            tr_lab = [int(v) for v in idx[tr]] if idx is not None else [int(v) for v in tr]
                            te_lab = [int(v) for v in idx[te]] if idx is not None else [int(v) for v in te]
                            X_te = X.iloc[te] if hasattr(X, "iloc") else X[te]
                            args = (X, X_te, groups is not None, len(tr), len(te), tr_lab, te_lab, site,
                                    {"n_splits": getattr(self, "n_splits", None),
                                     "index_kind": "label" if idx is not None else "position"})
                        finally:
                            _tls.busy = False
                        _record_split(name, *args)
                    yield tr, te
            return split

        cls.split = make(cls.__dict__["split"], name)

    classes = [c for _, c in all_estimators()]
    for dotted in ("sklearn.pipeline.Pipeline", "sklearn.compose.ColumnTransformer", "sklearn.pipeline.FeatureUnion",
                   "sklearn.model_selection.GridSearchCV", "sklearn.model_selection.RandomizedSearchCV"):
        mod, _, nm = dotted.rpartition(".")
        try:
            classes.append(getattr(__import__(mod, fromlist=[nm]), nm))
        except Exception:
            pass
    _wrap_classes(classes, "sklearn")

    for mname in ("roc_auc_score", "accuracy_score", "f1_score", "average_precision_score", "log_loss",
                  "precision_score", "recall_score", "mean_squared_error", "mean_absolute_error", "r2_score",
                  "root_mean_squared_error"):
        orig = getattr(skm, mname, None)
        if orig is None:
            continue

        def make_metric(orig, mname):
            def metric(*a, **k):
                val = orig(*a, **k)
                y_true = a[0] if len(a) > 0 else k.get("y_true")
                y_pred = a[1] if len(a) > 1 else next(
                    (k[n] for n in ("y_score", "y_pred", "y_prob", "probas_pred") if n in k), None)
                if not _busy() and _ACTIVE and y_true is not None and y_pred is not None:
                    _tls.busy = True
                    try:
                        import numpy as np
                        n = _nrows(y_true)
                        info = {"metric": mname, "value": float(val), "n": n, "depth": getattr(_tls, "depth", 0),
                                "y_index": _index(y_true), "site": _callsite()}
                        if n is not None and n <= 20000:
                            info["y_true"] = np.asarray(y_true).astype(float).tolist()
                            info["y_score"] = np.asarray(y_pred).astype(float).ravel().tolist()
                    except Exception:
                        info = None
                    finally:
                        _tls.busy = False
                    if info:
                        _emit("metric", **info)
                return val
            return metric

        setattr(skm, mname, make_metric(orig, mname))


# ------------------------------------------------------------------ XGBoost / LightGBM, patched on import
_LAZY = {"xgboost.sklearn": "xgboost", "lightgbm.sklearn": "lightgbm"}


class _PostImport:
    """Meta-path hook: after a watched module executes, wrap the estimator classes it defines."""

    def find_spec(self, name, path, target=None):
        if name not in _LAZY or getattr(_tls, "finding", False):
            return None
        _tls.finding = True
        try:
            import importlib.util
            spec = importlib.util.find_spec(name)
        finally:
            _tls.finding = False
        if spec is None or spec.loader is None:
            return None
        loader, prefix = spec.loader, _LAZY[name]
        orig_exec = loader.exec_module

        def exec_module(module):
            orig_exec(module)
            try:
                _wrap_classes([v for v in vars(module).values() if isinstance(v, type)], prefix)
            except Exception:
                pass

        loader.exec_module = exec_module
        return spec


if _ACTIVE:
    sys.meta_path.insert(0, _PostImport())
    _patch_sklearn()
    _emit("instrumentation_ready", python=sys.version.split()[0])
