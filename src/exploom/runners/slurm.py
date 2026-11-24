"""Phase 2 runner: ssh-slurm (sbatch submission + squeue/sacct probing)."""

from __future__ import annotations

import shlex
from typing import Any

from exploom.runners.base import (
    AttemptSpec,
    Runner,
    _expand_meta_dir_expr,
    build_common_probe_snippet,
    encode_script_bootstrap,
)


class SlurmRunner(Runner):
    """Submits an experiment via sbatch and probes state via squeue + sacct."""

    name = "ssh-slurm"

    def build_wrapped_cmd(self, spec: AttemptSpec) -> str:
        ref_q = shlex.quote(spec.runner_ref)
        launch = (
            f'SLURM_OUT="$(sbatch --parsable --job-name={ref_q} '
            f'--output="$META_DIR/slurm.out" --error="$META_DIR/slurm.err" '
            f'"$META_DIR/run.sh")" && '
            f'JOB_ID="${{SLURM_OUT%%;*}}" && '
            f'printf "%s\\n" "$JOB_ID" > "$META_DIR/slurm_job_id" && '
            f'echo "EXPLOOM_SLURM_JOB_ID:$JOB_ID"'
        )
        return encode_script_bootstrap(spec, launch)

    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        ref = str(attempt.get("runner_ref") or "")
        fallback_jid = shlex.quote(ref) if ref.isdigit() else '""'
        meta_expr = _expand_meta_dir_expr(str(attempt["meta_dir"]))

        alive_check = (
            f'JOB_ID=""; [ -f "$META_DIR/slurm_job_id" ] && '
            f'JOB_ID="$(tr -d "[:space:]" < "$META_DIR/slurm_job_id" 2>/dev/null)"; '
            f'[ -z "$JOB_ID" ] && JOB_ID={fallback_jid}; '
            f'[ -n "$JOB_ID" ] && squeue -h -j "$JOB_ID" -o "%T" 2>/dev/null | '
            f'grep -Eq "^(PENDING|RUNNING|CONFIGURING|COMPLETING|STAGE_OUT|SUSPENDED)$"'
        )
        common = build_common_probe_snippet(
            attempt,
            alive_check,
            include_patch=include_patch,
            log_tail_lines=log_tail_lines,
        )

        # Append sacct query so Exploom captures OUT_OF_MEMORY / TIMEOUT / PREEMPTED / CANCELLED
        sacct_snippet = f"""(
META_DIR={meta_expr}
JOB_ID=""
if [ -f "$META_DIR/slurm_job_id" ]; then
  JOB_ID="$(tr -d '[:space:]' < "$META_DIR/slurm_job_id" 2>/dev/null)"
fi
if [ -z "$JOB_ID" ]; then
  JOB_ID={fallback_jid}
fi
if [ -n "$JOB_ID" ] && command -v sacct >/dev/null 2>&1; then
  SACCT_LINE="$(sacct -n -P -j "$JOB_ID" -o State,ExitCode 2>/dev/null | head -n 1)"
  if [ -n "$SACCT_LINE" ]; then
    echo "SLURM_SACCT:$SACCT_LINE"
  fi
fi
)"""
        return f"{common}\n{sacct_snippet}"
