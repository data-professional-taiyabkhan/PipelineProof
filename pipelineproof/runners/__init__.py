from .base import EnvSpec, RunResult, RunSpec, Runner  # noqa: F401


def get_runner(name: str = "docker", **kw) -> "Runner":
    if name == "docker":
        from .docker_runner import DockerRunner
        return DockerRunner(**kw)
    if name == "modal":
        from .modal_runner import ModalRunner
        return ModalRunner(**kw)
    raise ValueError(f"unknown runner {name!r}")
