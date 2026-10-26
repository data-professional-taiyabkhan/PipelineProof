"""Local subprocess backend: runs submissions as ordinary processes on this machine.

FOR DEMOS AND DEVELOPMENT ON MACHINES WITHOUT DOCKER ONLY. There is NO isolation:
no network block, no memory cap, full access to your files. Never use it on real
candidate code. Production uses the Modal backend (gVisor, no network).

It mimics the sandbox layout so the rest of the harness is unchanged: each run gets
<root>/work (a copy of the submission), <root>/out, <root>/tmp, and every input mount,
and container paths in environment variables are translated to those local folders.
Afterwards the trace is translated back to container paths so analysis sees the same
view as in a real sandbox.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .base import INSTRUMENT_DIR, EnvSpec, RunResult, RunSpec, Runner, standard_env

CONTAINER_ROOTS = ("/work", "/out", "/tmp", "/pp")


class LocalRunner(Runner):
    name = "local (no isolation)"

    def build_env(self, env: EnvSpec) -> str:
        return sys.executable          # the current environment stands in for the pinned one

    def _command(self, cmd: str) -> list[str]:
        parts = shlex.split(cmd, posix=True)
        if parts and parts[0] in ("python", "python3", "py"):
            parts[0] = sys.executable
        elif parts and parts[0] == "papermill":
            parts = [sys.executable, "-m", "papermill"] + parts[1:]
        return parts

    def run(self, spec: RunSpec, env_id: str, out_dir: Path) -> RunResult:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        root = Path(tempfile.mkdtemp(prefix="pp_local_"))
        mapping = {"/work": root / "work", "/out": out_dir.resolve(), "/tmp": root / "tmp", "/pp": INSTRUMENT_DIR}
        shutil.copytree(spec.submission, mapping["/work"])
        mapping["/tmp"].mkdir()
        for mount, host in spec.inputs.items():
            mapping[mount] = Path(host).resolve()
        for sub in spec.out_subdirs:
            (out_dir / sub).mkdir(parents=True, exist_ok=True)

        def to_local(value: str) -> str:
            for c in sorted(mapping, key=len, reverse=True):
                if value == c or value.startswith(c + "/"):
                    return str(mapping[c] / value[len(c) + 1:]) if value != c else str(mapping[c])
            return value

        env = dict(os.environ)
        for k in ("PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(k, None)
        for k, v in standard_env(spec.env_vars).items():
            env[k] = to_local(v)
        env["PP_WORKDIR"] = str(mapping["/work"])
        env["PYTHONPATH"] = str(INSTRUMENT_DIR)
        env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(mapping["/tmp"])
        for d in ("MPLCONFIGDIR", "JUPYTER_RUNTIME_DIR", "JUPYTER_DATA_DIR"):
            Path(env[d]).mkdir(parents=True, exist_ok=True)

        t0 = time.time()
        try:
            p = subprocess.run(self._command(spec.command), cwd=mapping["/work"], env=env, capture_output=True,
                               text=True, timeout=spec.timeout_s)
            result = RunResult(p.returncode, p.stdout[-20000:], p.stderr[-20000:], round(time.time() - t0, 2), out_dir)
        except subprocess.TimeoutExpired as e:
            result = RunResult(124, "", "TIMEOUT", round(time.time() - t0, 2), out_dir, timed_out=True)
        self._translate_events(out_dir, mapping)
        shutil.rmtree(root, ignore_errors=True)
        return result

    @staticmethod
    def _translate_events(out_dir: Path, mapping: dict):
        """Map local paths in the trace back to sandbox paths and drop the interpreter's own files."""
        ignore = {os.path.normcase(str(Path(p).resolve())) for p in {sys.prefix, sys.base_prefix, sys.exec_prefix}}
        pairs = sorted(((os.path.normcase(str(Path(v).resolve())), k) for k, v in mapping.items()),
                       key=lambda kv: len(kv[0]), reverse=True)
        for name in ("events.jsonl", "predict_events.jsonl"):
            f = out_dir / name
            if not f.exists():
                continue
            lines = []
            for line in f.read_text(errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("kind") == "file_open":
                    raw = str(e.get("path") or "").lower()
                    if raw in (os.devnull.lower(), "nul", "\\\\.\\nul", "/dev/null") or raw.endswith("\\nul"):
                        continue
                    p = os.path.normcase(str(Path(e["path"]).resolve())) if e["path"] else ""
                    if any(p.startswith(i) for i in ignore) or "site-packages" in p or "\\windows\\" in p:
                        continue
                    for local, cont in pairs:
                        if p == local or p.startswith(local + os.sep):
                            e["path"] = cont + p[len(local):].replace("\\", "/")
                            break
                lines.append(json.dumps(e))
            f.write_text("\n".join(lines) + ("\n" if lines else ""))
