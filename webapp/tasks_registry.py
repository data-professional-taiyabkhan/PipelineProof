"""The task library: folders under tasks/ with a task.toml, a brief and a holdout."""
from __future__ import annotations

import io
import tomllib
import zipfile
from functools import lru_cache
from pathlib import Path

from . import db
from .config import settings


def task_dir(task_id: str) -> Path:
    d = (settings.tasks_dir / task_id).resolve()
    if d.parent != settings.tasks_dir.resolve() or not (d / "task.toml").exists():
        raise KeyError(task_id)
    return d


@lru_cache(maxsize=16)
def meta(task_id: str) -> dict:
    d = task_dir(task_id)
    cfg = tomllib.loads((d / "task.toml").read_text())
    m = cfg.get("meta", {})
    return {
        "id": task_id, "name": cfg["name"], "metric": cfg["metric"], "target": cfg["target"],
        "title": m.get("title", cfg["name"]), "summary": m.get("summary", ""),
        "role_family": m.get("role_family", ""), "time_box": m.get("time_box", ""),
        "entity_column": cfg.get("entity_column"), "post_outcome_columns": cfg.get("post_outcome_columns", []),
    }


def available() -> list[str]:
    root = settings.tasks_dir
    return sorted(p.name for p in root.iterdir() if (p / "task.toml").exists()) if root.exists() else []


def doc(task_id: str, name: str) -> str:
    p = task_dir(task_id) / name
    return p.read_text() if p.exists() else ""


@lru_cache(maxsize=16)
def load(task_id: str):
    """The harness Task object (holds the holdout; server side only)."""
    from pipelineproof import task as task_mod
    return task_mod.load(task_dir(task_id))


@lru_cache(maxsize=16)
def stats(task_id: str) -> dict:
    """Row and entity counts for the task page (read once per process)."""
    import csv
    d = task_dir(task_id)
    m = meta(task_id)
    with open(d / "data" / "train.csv", newline="") as f:
        r = csv.DictReader(f)
        rows, entities = 0, set()
        for row in r:
            rows += 1
            if m["entity_column"]:
                entities.add(row.get(m["entity_column"]))
    hold = d / "holdout" / "holdout_labels.csv"
    holdout_rows = sum(1 for _ in open(hold)) - 1 if hold.exists() else None
    return {"rows": rows, "entities": len(entities) if m["entity_column"] else None, "holdout_rows": holdout_rows,
            "entity_column": m["entity_column"]}


def train_csv_path(task_id: str) -> Path:
    return task_dir(task_id) / "data" / "train.csv"


@lru_cache(maxsize=16)
def template_zip(task_id: str) -> bytes:
    d = task_dir(task_id) / "template"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
        for p in sorted(d.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, f"submission/{p.relative_to(d).as_posix()}")
        z.writestr("submission/data/README.txt",
                   "Put train.csv here to run locally. Do not include data in your submission zip.\n")
    return buf.getvalue()


def sync_to_db():
    """Make sure every task folder has a row in pp.tasks."""
    for tid in available():
        m = meta(tid)
        db.run("""insert into pp.tasks (id, title, summary, role_family, metric, time_box)
                  values (%s, %s, %s, %s, %s, %s)
                  on conflict (id) do update set title = excluded.title, summary = excluded.summary,
                     role_family = excluded.role_family, metric = excluded.metric, time_box = excluded.time_box""",
               (tid, m["title"], m["summary"], m["role_family"], m["metric"], m["time_box"]))
