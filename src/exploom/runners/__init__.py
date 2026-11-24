"""Pluggable runner registry."""

from __future__ import annotations

from exploom.runners.base import AttemptSpec, Runner, WRAPPER_MARKER
from exploom.runners.custom import CustomRunner
from exploom.runners.docker import DockerRunner
from exploom.runners.local import LocalRunner
from exploom.runners.nohup import NohupRunner
from exploom.runners.screen import ScreenRunner
from exploom.runners.slurm import SlurmRunner
from exploom.runners.tmux import TmuxRunner

_RUNNERS: dict[str, Runner] = {
    "ssh-tmux": TmuxRunner(),
    "ssh-nohup": NohupRunner(),
    "local": LocalRunner(),
    "custom": CustomRunner(),
    "ssh-screen": ScreenRunner(),
    "ssh-slurm": SlurmRunner(),
    "ssh-docker": DockerRunner(),
}

SUPPORTED_RUNNERS = tuple(_RUNNERS.keys())


def get_runner(name: str) -> Runner:
    """Return the Runner implementation for the given runner name."""
    runner = _RUNNERS.get(name)
    if runner is None:
        raise ValueError(
            f"Unsupported runner '{name}'. Supported runners: {', '.join(SUPPORTED_RUNNERS)}"
        )
    return runner


__all__ = [
    "AttemptSpec",
    "Runner",
    "SUPPORTED_RUNNERS",
    "WRAPPER_MARKER",
    "get_runner",
]
