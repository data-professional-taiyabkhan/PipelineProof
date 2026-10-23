"""The submission contract: what a candidate must hand in, and how we read it.

A submission is a directory (unpacked from the uploaded zip) containing:

  pipeline.toml     python = "3.12"; train = "<command>"; predict = "<command>"
  requirements.txt  pinned packages (name==version)
  DECISIONS.md      claimed_metric: <metric> = <value>; seed: <int>; validation; AI tools used
  README.md
  ...code (.py files and/or .ipynb notebooks)

At run time the harness sets:
  train:   PP_DATA_DIR (train.csv lives here), PP_SEED, PP_OUTPUT_DIR (write model + metrics.json)
  predict: PP_MODEL_DIR (the train run's PP_OUTPUT_DIR), PP_INPUT (features csv), PP_PRED_OUT (csv to write)
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_PYTHON = {"3.11", "3.12", "3.13"}
MAX_FILES = 500
MAX_BYTES = 50 * 1024 * 1024
REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?\s*(==|>=|<=|~=|!=|<|>)?\s*([^;\s]*)")


@dataclass
class Requirement:
    name: str          # normalised distribution name
    spec: str          # operator, '' if unpinned
    version: str

    @property
    def pinned(self) -> bool:
        return self.spec == "=="


@dataclass
class Submission:
    root: Path
    python: str
    train_cmd: str
    predict_cmd: str
    requirements: list[Requirement]
    claimed_metric: float | None
    claimed_metric_name: str | None
    seed: int
    decisions_text: str
    problems: list[str] = field(default_factory=list)   # contract violations, shown to the candidate
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def requirement_names(self) -> set[str]:
        return {r.name for r in self.requirements}


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str) -> tuple[list[Requirement], list[str]]:
    reqs, bad = [], []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-", "git+", "http")) or "://" in line:
            bad.append(f"requirements.txt line not allowed (indexes, URLs and options are not accepted): {raw.strip()}")
            continue
        m = REQ_LINE.match(line)
        if not m:
            bad.append(f"requirements.txt line not understood: {raw.strip()}")
            continue
        reqs.append(Requirement(normalise(m.group(1)), m.group(3) or "", m.group(4) or ""))
    return reqs, bad


def parse_decisions(text: str, metric: str) -> tuple[float | None, str | None, int | None]:
    m = re.search(r"claimed_metric:\s*([A-Za-z0-9_]+)\s*=\s*([0-9]*\.?[0-9]+)", text)
    claimed, name = (float(m.group(2)), m.group(1)) if m else (None, None)
    s = re.search(r"^\s*seed:\s*(\d+)", text, re.M)
    return claimed, name, (int(s.group(1)) if s else None)


def load(root: str | Path, metric: str) -> Submission:
    root = Path(root)
    problems, warnings = [], []

    files = [p for p in root.rglob("*") if p.is_file()]
    if len(files) > MAX_FILES:
        problems.append(f"too many files ({len(files)} > {MAX_FILES})")
    size = sum(p.stat().st_size for p in files)
    if size > MAX_BYTES:
        problems.append(f"submission too large ({size // 1_000_000} MB > {MAX_BYTES // 1_000_000} MB)")
    for p in files:
        if p.is_symlink():
            problems.append(f"symlinks are not allowed: {p.relative_to(root)}")

    cfg = {}
    if not (root / "pipeline.toml").exists():
        problems.append("pipeline.toml is missing")
    else:
        try:
            cfg = tomllib.loads((root / "pipeline.toml").read_text())
        except tomllib.TOMLDecodeError as e:
            problems.append(f"pipeline.toml is not valid TOML: {e}")
    python = str(cfg.get("python", "3.12"))
    if python not in SUPPORTED_PYTHON:
        problems.append(f"python {python} is not supported (use one of {sorted(SUPPORTED_PYTHON)})")
    train_cmd, predict_cmd = cfg.get("train", ""), cfg.get("predict", "")
    for key, cmd in (("train", train_cmd), ("predict", predict_cmd)):
        if not cmd:
            problems.append(f"pipeline.toml has no '{key}' command")
        elif any(tok in cmd for tok in ("pip ", "curl", "wget", "sudo", "apt")):
            problems.append(f"'{key}' command may not install packages or download anything")

    reqs = []
    if not (root / "requirements.txt").exists():
        problems.append("requirements.txt is missing")
    else:
        reqs, bad = parse_requirements((root / "requirements.txt").read_text())
        problems += bad
        unpinned = [r.name for r in reqs if not r.pinned]
        if unpinned:
            warnings.append(f"unpinned requirements (resolved to exact versions at build time): {', '.join(unpinned)}")

    decisions = (root / "DECISIONS.md").read_text() if (root / "DECISIONS.md").exists() else ""
    if not decisions:
        problems.append("DECISIONS.md is missing")
    claimed, claimed_name, seed = parse_decisions(decisions, metric)
    if decisions and claimed is None:
        problems.append(f"DECISIONS.md has no line 'claimed_metric: {metric} = <value>'")
    if claimed_name and claimed_name != metric:
        problems.append(f"claimed metric is '{claimed_name}' but this task is scored on '{metric}'")
    if decisions and seed is None:
        warnings.append("DECISIONS.md states no seed; 42 is assumed")
    if not (root / "README.md").exists():
        warnings.append("README.md is missing")

    return Submission(root, python, train_cmd, predict_cmd, reqs, claimed, claimed_name,
                      seed if seed is not None else 42, decisions, problems, warnings)
