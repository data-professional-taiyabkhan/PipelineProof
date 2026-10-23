"""Sandbox runner interface. Every backend must give the same guarantees:

  * the run has NO network access (the environment build may; it never sees data)
  * the submission is copied to a writable /work; inputs are read-only
  * CPU, memory, process and wall-clock caps are enforced
  * nothing persists between runs; outputs come back only from /out
  * no secrets or holdout labels are ever placed in the sandbox
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

INSTRUMENT_DIR = Path(__file__).resolve().parent.parent / "instrument"
BASE_PACKAGES = ["papermill==2.7.0", "ipykernel"]   # lets candidates submit notebooks


@dataclass
class EnvSpec:
    python: str
    requirements: list[str]          # "name==version" lines as written by the candidate

    def key(self) -> str:
        h = hashlib.sha256((self.python + "\n" + "\n".join(sorted(self.requirements))).encode()).hexdigest()
        return h[:16]


@dataclass
class RunSpec:
    env: EnvSpec
    submission: Path                 # host dir, copied into /work
    inputs: dict[str, Path]          # container mount point -> host dir (read-only)
    command: str                     # run inside /work with sh -c
    env_vars: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 600
    cpus: float = 2
    memory_mb: int = 4096


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    wall_s: float
    out_dir: Path                    # host copy of the sandbox's /out
    timed_out: bool = False


class Runner:
    name = "base"

    def build_env(self, env: EnvSpec) -> str:
        """Prepare (and cache) an environment for this spec; return an id usable by run()."""
        raise NotImplementedError

    def run(self, spec: RunSpec, env_id: str, out_dir: Path) -> RunResult:
        raise NotImplementedError


def standard_env(extra: dict[str, str]) -> dict[str, str]:
    env = {
        "PYTHONPATH": "/pp", "PP_WORKDIR": "/work", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "MPLCONFIGDIR": "/tmp/mpl", "JUPYTER_RUNTIME_DIR": "/tmp/jupyter", "JUPYTER_DATA_DIR": "/tmp/jupyter-data",
        "PP_EVENTS": "/out/events.jsonl",
    }
    env.update(extra)
    return env
