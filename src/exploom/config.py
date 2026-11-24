"""Configuration loading and path management for Exploom."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_STALE_THRESHOLD_MINUTES = 45
DEFAULT_SSH_CONNECT_TIMEOUT = 5
DEFAULT_MAX_PATCH_BYTES = 200 * 1024
DEFAULT_RUNNER = "ssh-tmux"


@dataclass(frozen=True)
class FailureRule:
    """A regex-based failure classification rule."""

    status: str
    pattern: str
    name: str = ""


@dataclass
class ExploomConfig:
    """Runtime configuration resolved from ~/.exploom/config.toml."""

    home_dir: Path
    stale_threshold_minutes: int = DEFAULT_STALE_THRESHOLD_MINUTES
    ssh_connect_timeout: int = DEFAULT_SSH_CONNECT_TIMEOUT
    max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES
    default_runner: str = DEFAULT_RUNNER
    custom_failure_rules: list[FailureRule] = field(default_factory=list)
    server_meta_dirs: dict[str, str] = field(default_factory=dict)
    server_docker_containers: dict[str, str] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.home_dir / "registry.db"

    @property
    def config_path(self) -> Path:
        return self.home_dir / "config.toml"

    @property
    def notes_dir(self) -> Path:
        return self.home_dir / "notes"

    @property
    def patches_dir(self) -> Path:
        return self.home_dir / "patches"

    def ensure_dirs(self) -> None:
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.patches_dir.mkdir(parents=True, exist_ok=True)

    def get_meta_dir(self, server: str, project: str, exp_id: str, attempt_no: int) -> str:
        """Compute remote/local metadata directory for an attempt."""
        base = self.server_meta_dirs.get(server)
        if base:
            return f"{base.rstrip('/')}/{project}/{exp_id}_{attempt_no}"
        if server == "local":
            return str((self.home_dir / "runs" / project / f"{exp_id}_{attempt_no}").resolve())
        return f"$HOME/.cache/exploom/{project}/{exp_id}_{attempt_no}"


def resolve_home_dir(home_dir: Path | str | None = None) -> Path:
    """Resolve the Exploom home directory (defaults to ~/.exploom)."""
    if home_dir is not None:
        return Path(home_dir).expanduser().resolve()
    env_home = os.environ.get("EXPLOOM_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".exploom").resolve()


def _parse_failure_rules(raw_rules: Any) -> list[FailureRule]:
    if not isinstance(raw_rules, list):
        return []
    rules: list[FailureRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).strip()
        pattern = str(item.get("pattern", "")).strip()
        name = str(item.get("name", "")).strip()
        if status and pattern:
            rules.append(FailureRule(status=status, pattern=pattern, name=name))
    return rules


def _parse_server_meta_dirs(raw_servers: Any) -> dict[str, str]:
    if not isinstance(raw_servers, dict):
        return {}
    result: dict[str, str] = {}
    for server_name, cfg in raw_servers.items():
        if isinstance(cfg, dict) and cfg.get("meta_dir"):
            result[str(server_name)] = str(cfg["meta_dir"])
    return result


def _parse_server_docker_containers(raw_servers: Any) -> dict[str, str]:
    if not isinstance(raw_servers, dict):
        return {}
    result: dict[str, str] = {}
    for server_name, cfg in raw_servers.items():
        if isinstance(cfg, dict) and cfg.get("docker_container"):
            result[str(server_name)] = str(cfg["docker_container"])
    return result


def load_config(home_dir: Path | str | None = None) -> ExploomConfig:
    """Load configuration from <home_dir>/config.toml, falling back to defaults."""
    resolved_home = resolve_home_dir(home_dir)
    cfg = ExploomConfig(home_dir=resolved_home)
    config_file = cfg.config_path
    if not config_file.is_file():
        return cfg

    with config_file.open("rb") as f:
        data = tomllib.load(f)

    if "stale_threshold_minutes" in data:
        cfg.stale_threshold_minutes = max(1, int(data["stale_threshold_minutes"]))
    if "ssh_connect_timeout" in data:
        cfg.ssh_connect_timeout = max(1, int(data["ssh_connect_timeout"]))
    if "max_patch_bytes" in data:
        cfg.max_patch_bytes = max(1024, int(data["max_patch_bytes"]))
    if "default_runner" in data and data["default_runner"]:
        cfg.default_runner = str(data["default_runner"])

    cfg.custom_failure_rules = _parse_failure_rules(data.get("failure_rules"))
    cfg.server_meta_dirs = _parse_server_meta_dirs(data.get("servers"))
    cfg.server_docker_containers = _parse_server_docker_containers(data.get("servers"))
    return cfg
