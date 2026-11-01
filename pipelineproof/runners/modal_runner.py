"""Modal backend for production: gVisor sandboxes, networking blocked, UK region by default.

Each run gets a fresh Sandbox built from the candidate's pinned requirements:

  * the image is built by Modal's builder (network on, no data, no secrets) and cached by spec
  * the sandbox runs with block_network=True, CPU / memory / wall-clock caps, and is
    terminated after the run, so nothing persists between runs
  * the submission, the harness instrumentation (/pp) and the run's inputs are copied in as tar
    archives; outputs come back only from /out, as a gzipped tar streamed over stdout
  * holdout labels and secrets never enter a sandbox (the caller scores predictions itself)

Configuration (environment variables):
  PP_MODAL_SANDBOX_APP  app the sandboxes attach to (default "pipelineproof-sandboxes")
  PP_MODAL_REGION       region for sandboxes (default "uk"; empty string = any region)
"""
from __future__ import annotations

import io
import os
import shutil
import tarfile
import threading
import time
import uuid
from pathlib import Path

from .base import BASE_PACKAGES, INSTRUMENT_DIR, EnvBuildError, EnvSpec, RunResult, RunSpec, Runner, standard_env

APP_NAME = os.environ.get("PP_MODAL_SANDBOX_APP", "pipelineproof-sandboxes")
_EXCLUDE = {"__pycache__", ".ipynb_checkpoints", ".git"}


def _tar_dir(src: Path, arcroot: str) -> bytes:
    buf = io.BytesIO()

    def _filter(ti: tarfile.TarInfo):
        parts = Path(ti.name).parts
        if any(p in _EXCLUDE for p in parts) or ti.issym() or ti.islnk():
            return None
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti

    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(str(src), arcname=arcroot, filter=_filter)
    return buf.getvalue()


class ModalRunner(Runner):
    name = "modal (gVisor sandbox, network blocked)"
    max_parallel = 6

    def __init__(self, region: str | None = None):
        import modal
        self.modal = modal
        r = os.environ.get("PP_MODAL_REGION", "uk") if region is None else region
        self.region = r or None
        self.app = modal.App.lookup(APP_NAME, create_if_missing=True)
        self._images: dict[str, object] = {}
        self._lock = threading.Lock()
        self._instrument_tar = _tar_dir(INSTRUMENT_DIR, "pp")
        if self.region:
            self.name = f"modal (gVisor sandbox, network blocked, region {self.region})"

    # ---------------------------------------------------------------- environments
    def build_env(self, env: EnvSpec) -> str:
        key = env.key()
        with self._lock:
            if key in self._images:
                return key
        image = (self.modal.Image.debian_slim(python_version=env.python)
                 .pip_install(*(env.requirements + BASE_PACKAGES)))
        try:
            image = image.build(self.app)          # eager, so install errors surface as ENV_BUILD
        except Exception as e:                     # noqa: BLE001 - any build failure is the candidate's env
            raise EnvBuildError(str(e)[-2000:]) from e
        with self._lock:
            self._images[key] = image
        return key

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _sh(sb, cmd: str, timeout: int = 120) -> int:
        p = sb.exec("sh", "-c", cmd, timeout=timeout)
        return p.wait()

    def _put(self, sb, data: bytes, dest: str):
        tmp = f"/tmp/_in_{uuid.uuid4().hex}.tar"
        sb.filesystem.write_bytes(data, tmp)
        code = self._sh(sb, f"mkdir -p {dest} && tar -xf {tmp} -C {dest} --strip-components=1 --no-same-owner && rm -f {tmp}")
        if code != 0:
            raise RuntimeError(f"could not unpack input into {dest} (exit {code})")

    @staticmethod
    def _get_out(sb, out_dir: Path):
        p = sb.exec("sh", "-c", "cd /out && tar -czf - .", text=False, timeout=300)
        data = p.stdout.read()
        p.wait()
        if data:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                tf.extractall(out_dir, filter="data")

    # ---------------------------------------------------------------- runs
    def run(self, spec: RunSpec, env_id: str, out_dir: Path) -> RunResult:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        t0 = time.time()
        try:
            sb = self.modal.Sandbox.create(
                "sleep", "infinity",
                app=self.app, image=self._images[env_id], block_network=True, region=self.region,
                cpu=spec.cpus, memory=spec.memory_mb, timeout=spec.timeout_s + 300, workdir="/work")
        except Exception as e:  # noqa: BLE001
            return RunResult(1, "", f"sandbox could not start: {e}", round(time.time() - t0, 2), out_dir)
        try:
            self._put(sb, _tar_dir(spec.submission, "sub"), "/work")
            self._put(sb, self._instrument_tar, "/pp")
            for mount, host in spec.inputs.items():
                self._put(sb, _tar_dir(Path(host), "x"), mount)
            subdirs = " ".join(f"/out/{d}" for d in spec.out_subdirs)
            self._sh(sb, f"mkdir -p /out /tmp/mpl /tmp/jupyter /tmp/jupyter-data {subdirs}")
            run_start = time.time()
            proc = sb.exec("sh", "-c", spec.command, workdir="/work", env=standard_env(spec.env_vars),
                           timeout=spec.timeout_s)
            code = proc.wait()
            stdout, stderr = proc.stdout.read()[-20000:], proc.stderr.read()[-20000:]
            timed_out = code == -1 or (time.time() - run_start) >= spec.timeout_s
            if timed_out:
                code, stderr = 124, (stderr + "\nTIMEOUT")[-20000:]
            self._get_out(sb, out_dir)
            return RunResult(code, stdout, stderr, round(time.time() - t0, 2), out_dir, timed_out)
        except Exception as e:  # noqa: BLE001 - surface infrastructure errors as a failed run
            return RunResult(1, "", f"sandbox error: {e}", round(time.time() - t0, 2), out_dir)
        finally:
            try:
                sb.terminate()
            except Exception:  # noqa: BLE001
                pass
