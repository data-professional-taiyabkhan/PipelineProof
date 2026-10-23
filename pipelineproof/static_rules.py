"""PipelineProof spike - static (AST) pass over a submission's Python files.

Deliberately small: a handful of high-value signatures. Static findings are 'suspects';
they are confirmed or dismissed by the runtime trace and ablation, not scored on their own.
"""
import ast
import json
import pathlib
import re
import sys
from importlib.metadata import packages_distributions

TRANSFORMER_RE = re.compile(r"(Scaler|Imputer|Encoder|PCA|Vectorizer|Select|KBins|Polynomial|Normalizer|Transformer|SVD)")
SPLIT_NAMES = {"train_test_split"}
GROUP_TIME_SPLITTERS = {"GroupKFold", "GroupShuffleSplit", "StratifiedGroupKFold", "LeaveOneGroupOut", "TimeSeriesSplit"}
METRIC_NAMES = {"roc_auc_score", "accuracy_score", "f1_score", "average_precision_score", "log_loss",
                "precision_score", "recall_score"}
NETWORK_MODULES = {"requests", "urllib", "httpx", "socket", "http", "aiohttp", "ftplib"}


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _data_names(node):
    """Names passed as data, ignoring names used only inside an index (X[~test], X.iloc[te])."""
    skip = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            skip |= {id(n) for n in ast.walk(sub.slice)}
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and id(n) not in skip}


def analyse(sub_dir, target, post_outcome_columns=()):
    findings, imports, info = [], set(), {"group_or_time_splitter": False, "uses_pp_seed": False,
                                          "drops_post_outcome": [], "functions": 0}
    for rel, src, magics in sources(sub_dir):
        for line_no, magic in magics:
            if re.match(r"[!%]\s*pip\s+install|%pip|%conda", magic):
                findings.append({"rule": "S8_install_in_code", "severity": "medium", "file": rel, "line": line_no,
                                 "detail": f"installs packages from inside the code: {magic[:80]}"})
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            findings.append({"rule": "S0_syntax", "severity": "medium", "file": rel, "line": e.lineno,
                             "detail": f"could not parse: {e.msg}"})
            continue
        info["uses_pp_seed"] |= "PP_SEED" in src
        info["functions"] += sum(isinstance(n, ast.FunctionDef) for n in ast.walk(tree))

        transformer_vars, splitter_vars, split_lines = set(), set(), []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for m in mods:
                    imports.add(m.split(".")[0])
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                ctor = _name(node.value.func)
                if ctor and TRANSFORMER_RE.search(ctor):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            transformer_vars.add(t.id)
                if ctor and (ctor.endswith("Split") or ctor.endswith("KFold") or ctor == "LeaveOneGroupOut"):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            splitter_vars.add(t.id)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = _name(node.func)
                owner = node.func.value if isinstance(node.func, ast.Attribute) else None
                is_cv_split = fn == "split" and owner is not None and (
                    _name(owner) in splitter_vars or
                    (isinstance(owner, ast.Call) and (_name(owner.func) or "").endswith(("Split", "KFold"))))
                if fn in SPLIT_NAMES or fn in ("cross_val_score", "cross_validate", "cross_val_predict") or is_cv_split:
                    split_lines.append(node.lineno)
                if fn in GROUP_TIME_SPLITTERS:
                    info["group_or_time_splitter"] = True
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if s.startswith(("/", "~", "C:\\")) and not s.startswith(("/data", "/out", "/work", "/tmp")) and len(s) > 2:
                    findings.append({"rule": "S6_absolute_path", "severity": "medium", "file": rel,
                                     "line": node.lineno, "detail": f"hard-coded path {s!r}"})
                if s.startswith(("http://", "https://")):
                    findings.append({"rule": "S7_network_url", "severity": "medium", "file": rel,
                                     "line": node.lineno, "detail": f"URL in code {s!r}"})
                if s in post_outcome_columns:
                    info["drops_post_outcome"].append(s)
        first_split = min(split_lines) if split_lines else None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = _name(node.func)
            # S1: transformer fitted before the first split
            if fn in ("fit", "fit_transform") and first_split and node.lineno < first_split:
                owner = node.func.value if isinstance(node.func, ast.Attribute) else None
                owner_name = _name(owner) if owner is not None else None
                ctor = _name(owner.func) if isinstance(owner, ast.Call) else None
                if (owner_name in transformer_vars) or (ctor and TRANSFORMER_RE.search(ctor)):
                    findings.append({"rule": "S1_preprocess_before_split", "severity": "medium", "file": rel,
                                     "line": node.lineno,
                                     "detail": f"{owner_name or ctor}.{fn}() runs before the first split (line {first_split})"})
            # S3: fit on something named like test data
            if fn in ("fit", "fit_transform"):
                for arg in node.args[:2]:
                    hits = [n for n in _data_names(arg) if "test" in n.lower()]
                    if hits:
                        findings.append({"rule": "S3_fit_on_test", "severity": "high", "file": rel,
                                         "line": node.lineno, "detail": f"fit() receives {', '.join(sorted(hits))}"})
            # S2: target used to build a feature before the split
            if first_split and node.lineno < first_split and fn in ("transform", "mean", "agg", "map"):
                consts = {c.value for c in ast.walk(node) if isinstance(c, ast.Constant)}
                if target in consts:
                    findings.append({"rule": "S2_target_derived_feature", "severity": "high", "file": rel,
                                     "line": node.lineno, "detail": f"target column '{target}' used in a {fn}() before the split"})

        # S10: repeated evaluation on the test split inside a loop (model selection on test)
        for loop in [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]:
            calls = [n for n in ast.walk(loop) if isinstance(n, ast.Call)]
            has_fit = any(_name(c.func) == "fit" for c in calls)
            test_metric = any(_name(c.func) in METRIC_NAMES and any("test" in n.lower() for a in c.args for n in _names_in(a))
                              for c in calls)
            if has_fit and test_metric:
                findings.append({"rule": "S10_tuning_on_test", "severity": "high", "file": rel, "line": loop.lineno,
                                 "detail": "loop fits models and scores each one on the test split"})

    std = set(sys.stdlib_module_names)
    local = {p.stem for p in pathlib.Path(sub_dir).rglob("*.py")} | {
        p.name for p in pathlib.Path(sub_dir).iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    third_party = sorted(m for m in imports if m and m not in std and m not in local and m != "__future__")
    if any(m in NETWORK_MODULES for m in imports):
        findings.append({"rule": "S7_network_module", "severity": "low", "file": "*", "line": None,
                         "detail": f"imports {sorted(m for m in imports if m in NETWORK_MODULES)}"})
    return {"findings": findings, "imports": third_party, "info": info}


def sources(sub_dir):
    """(display name, python source, [(line, magic)]) for every .py file and notebook."""
    root = pathlib.Path(sub_dir)
    out = []
    for path in sorted(root.rglob("*.py")):
        out.append((str(path.relative_to(root)), path.read_text(errors="replace"), []))
    for path in sorted(root.rglob("*.ipynb")):
        if ".ipynb_checkpoints" in path.parts:
            continue
        try:
            nb = json.loads(path.read_text(errors="replace"))
        except json.JSONDecodeError:
            continue
        lines, magics = [], []
        for cell in nb.get("cells", []):
            if cell.get("cell_type") != "code":
                continue
            src = cell.get("source", "")
            src = "".join(src) if isinstance(src, list) else src
            for line in src.splitlines():
                if line.lstrip().startswith(("!", "%")):
                    magics.append((len(lines) + 1, line.strip()))
                    lines.append("pass")
                else:
                    lines.append(line)
            lines.append("")
        out.append((str(path.relative_to(root)), "\n".join(lines), magics))
    return out


KNOWN_DISTS = {"sklearn": "scikit-learn", "cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml",
               "bs4": "beautifulsoup4", "skimage": "scikit-image", "dateutil": "python-dateutil",
               "Crypto": "pycryptodome", "dotenv": "python-dotenv", "jwt": "pyjwt", "attr": "attrs"}
def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def undeclared_imports(imports, requirement_names):
    """Imported third-party modules that no declared requirement provides.

    Packages pulled in by a declared one (scipy via scikit-learn) count as declared only
    when the candidate does not use them directly: direct use should be declared."""
    dists = packages_distributions()
    reqs = {_norm(r) for r in requirement_names}
    out = []
    for mod in imports:
        names = {_norm(d) for d in dists.get(mod, [])} | {_norm(KNOWN_DISTS.get(mod, mod))}
        if not names & reqs:
            out.append(mod)
    return out


def data_probes(sub_dir, task):
    """Task-specific checks that the planted data problems were handled (regexes from task.toml)."""
    src = "\n".join(s for _, s, _ in sources(sub_dir))
    return {name: bool(re.search(pattern, src)) for name, pattern in getattr(task, "probes", {}).items()}
