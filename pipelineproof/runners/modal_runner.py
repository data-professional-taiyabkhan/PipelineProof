"""Modal backend for production: gVisor sandboxes in the UK region with networking blocked.

NOT YET RUN AGAINST A REAL MODAL ACCOUNT. Needs MODAL_TOKEN_ID / MODAL_TOKEN_SECRET.
Docs used: https://modal.com/docs/guide/sandbox, /sandbox-networking, /region-selection.

Files go in and out as tar archives through the sandbox file API, so the only Modal calls
used are Sandbox.create, Sandbox.exec, Sandbox.filesystem.write_bytes/read_bytes and
Sandbox.terminate (checked against modal 1.6.2's signatures).
"""
from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path

from .base import BASE_PACKAGES, INSTRUMENT_DIR, EnvSpec, RunResult, RunSpec, Runner, standard_env

APP_NAME = os.environ.get("PP_MODAL_APP", "pipelineproof-sandboxes")
REGION = os.environ.get("PP_MODAL_REGION", "uk")


def _tar_dir(src: Path, arcroot: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(str(src), arcname=arcroot)
    return buf.getvalue()


class ModalRunner(Runner):
    name = "modal"

    def __init__(self):
        import modal
        self.modal = modal
        self.app = modal.App.lookup(APP_NAME, create_if_missing=True)
        self._images: dict[str, object] = {}

    def build_env(self, env: EnvSpec) -> str:
        key = env.key()
        if key not in self._images:
            image = (self.modal.Image.debian_slim(python_version=env.python)
                     .pip_install(*(env.requirements + BASE_PACKAGES))
                     .add_local_dir(str(INSTRUMENT_DIR), "/pp"))
            self._images[key] = image
        return key

    def _put(self, sb, data: bytes, dest: str):
        sb.filesystem.write_bytes(data, "/tmp/_in.tar")
        p = sb.exec("sh", "-c", f"mkdir -p {dest} && tar -xf /tmp/_in.tar -C {dest} --strip-components=1 && rm /tmp/_in.tar")
        p.wait()

    def run(self, spec: RunSpec, env_id: str, out_dir: Path) -> RunResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        sb = self.modal.Sandbox.create(
            "sleep", str(spec.timeout_s + 120),
            app=self.app, image=self._images[env_id], block_network=True, region=REGION,
            cpu=spec.cpus, memory=spec.memory_mb, timeout=spec.timeout_s + 180)
        try:
            self._put(sb, _tar_dir(spec.submission, "sub"), "/work")
            for mount, host in spec.inputs.items():
                self._put(sb, _tar_dir(Path(host), "x"), mount)
            sb.exec("sh", "-c", "mkdir -p /out /tmp/mpl").wait()
            env = standard_env(spec.env_vars)
            proc = sb.exec("sh", "-c", spec.command, workdir="/work", env=env, timeout=spec.timeout_s)
            proc.wait()
            code = proc.returncode
            stdout, stderr = proc.stdout.read()[-20000:], proc.stderr.read()[-20000:]
            sb.exec("sh", "-c", "tar -cf /tmp/_out.tar -C /out .").wait()
            data = sb.filesystem.read_bytes("/tmp/_out.tar")
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                tf.extractall(out_dir, filter="data")
            timed_out = code in (-1, 124) or (time.time() - t0) > spec.timeout_s
            return RunResult(code if code is not None else 124, stdout, stderr, round(time.time() - t0, 2), out_dir, timed_out)
        finally:
            sb.terminate()
