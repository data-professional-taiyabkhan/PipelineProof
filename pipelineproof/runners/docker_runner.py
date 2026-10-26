"""Local Docker backend, for development and the benchmark.

Production isolation should be gVisor or Firecracker (see modal_runner.py). Locally this
backend can add gVisor by setting PP_DOCKER_RUNTIME=runsc where runsc is installed.

Two environment modes:
  build (default)  builds python:<ver>-slim + the candidate's requirements per EnvSpec, cached
                   by a hash of the spec. The build has network but no data.
  host             PP_DOCKER_HOST_PYTHON=1: an empty image with the host's /usr mounted
                   read-only. Only for workspaces that cannot pull base images.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import BASE_PACKAGES, INSTRUMENT_DIR, EnvSpec, RunResult, RunSpec, Runner, standard_env


class DockerRunner(Runner):
    name = "docker"

    def __init__(self, host_python: bool | None = None, runtime: str | None = None):
        self.host_python = host_python if host_python is not None else os.environ.get("PP_DOCKER_HOST_PYTHON") == "1"
        self.runtime = runtime or os.environ.get("PP_DOCKER_RUNTIME")
        self._built: set[str] = set()

    # ---------------------------------------------------------------- environments
    def build_env(self, env: EnvSpec) -> str:
        if self.host_python:
            return self._host_image()
        tag = f"pp-env:{env.key()}"
        if tag in self._built or subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
            self._built.add(tag)
            return tag
        with tempfile.TemporaryDirectory() as ctx:
            Path(ctx, "requirements.txt").write_text("\n".join(env.requirements + BASE_PACKAGES) + "\n")
            Path(ctx, "Dockerfile").write_text(
                f"FROM python:{env.python}-slim\n"
                "COPY requirements.txt /tmp/requirements.txt\n"
                "RUN pip install --no-cache-dir --prefer-binary -r /tmp/requirements.txt\n"
                "RUN useradd --create-home --uid 10001 runner\n"
                "USER runner\n")
            p = subprocess.run(["docker", "build", "-q", "-t", tag, ctx], capture_output=True, text=True, timeout=1800)
            if p.returncode != 0:
                raise EnvBuildError(p.stderr[-2000:])
        self._built.add(tag)
        return tag

    def _host_image(self) -> str:
        tag = "pp-base:local"
        if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
            with tempfile.TemporaryDirectory() as root:
                for d in ("usr", "etc", "tmp", "work", "out", "proc", "dev", "pp"):
                    os.makedirs(os.path.join(root, d))
                for link, target in (("bin", "usr/bin"), ("lib", "usr/lib"), ("lib64", "usr/lib64"), ("sbin", "usr/sbin")):
                    os.symlink(target, os.path.join(root, link))
                tar = subprocess.Popen(["tar", "-C", root, "-c", "."], stdout=subprocess.PIPE)
                subprocess.run(["docker", "import", "-", tag], stdin=tar.stdout, check=True, capture_output=True)
                tar.wait()
        return tag

    # ---------------------------------------------------------------- runs
    def run(self, spec: RunSpec, env_id: str, out_dir: Path) -> RunResult:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        out_dir.chmod(0o777)
        for sub in spec.out_subdirs:
            (out_dir / sub).mkdir(parents=True, exist_ok=True)
            (out_dir / sub).chmod(0o777)
        args = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "256",
                "--memory", f"{spec.memory_mb}m", "--memory-swap", f"{spec.memory_mb}m", "--cpus", str(spec.cpus),
                "--tmpfs", "/tmp:rw,exec,size=1g,mode=1777", "--tmpfs", "/work:rw,exec,size=512m,mode=1777",
                "-v", f"{INSTRUMENT_DIR}:/pp:ro", "-v", f"{spec.submission.resolve()}:/sub:ro",
                "-v", f"{out_dir.resolve()}:/out:rw", "-w", "/work"]
        if self.runtime:
            args += ["--runtime", self.runtime]
        if self.host_python:
            args += ["-v", "/usr:/usr:ro", "-v", "/etc:/etc:ro"]
        for mount, host in spec.inputs.items():
            args += ["-v", f"{Path(host).resolve()}:{mount}:ro"]
        for k, v in standard_env(spec.env_vars).items():
            args += ["-e", f"{k}={v}"]
        args += [env_id, "/bin/sh", "-c", f"cp -r /sub/. /work/ && mkdir -p /tmp/mpl && {spec.command}"]
        name = f"pp-{os.getpid()}-{time.monotonic_ns()}"
        args.insert(2, f"--name={name}")
        t0 = time.time()
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=spec.timeout_s)
            return RunResult(p.returncode, p.stdout[-20000:], p.stderr[-20000:], round(time.time() - t0, 2), out_dir)
        except subprocess.TimeoutExpired as e:
            subprocess.run(["docker", "kill", name], capture_output=True)
            return RunResult(124, (e.stdout or b"").decode(errors="replace")[-20000:] if isinstance(e.stdout, bytes) else (e.stdout or ""),
                             "TIMEOUT", round(time.time() - t0, 2), out_dir, timed_out=True)


class EnvBuildError(RuntimeError):
    pass
