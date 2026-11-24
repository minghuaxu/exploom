<p align="center">
  <img src="assets/icon.png" alt="Exploom" width="128" />
</p>

# Exploom

**Local single-control-plane Experiment Lineage & Ops for AI Coding Agents.**

[中文文档 (README_CN.md)](README_CN.md) · [Architecture & Design (DESIGN.md)](DESIGN.md)

Exploom solves experiment amnesia, scattered multi-server SSH jobs, OOM/quota/network recovery, and `data prep -> fine-tuning -> error analysis -> next iteration` lineage tracking across multi-session workflows, `/side` branches, and main/subagent collaboration.

It **complements** WandB / MLflow / TensorBoard: they track loss curves and charts; Exploom tracks **experiment lineage, multi-server state, failure attribution, and instant cross-session context restoration**.

---

## Key Features

- **Remote Zero-Install**: Zero daemons or packages on remote GPU boxes. Works over standard SSH + self-contained POSIX/Bash wrappers with zero Git repo pollution.
- **SQLite WAL Single Control Plane**: Concurrent multi-session & subagent safe (`~/.exploom/registry.db`), paired with human-readable Markdown notes (`~/.exploom/notes/`) and code-drift patch snapshots (`~/.exploom/patches/`).
- **Safe Approval Boundary**: MCP tools only perform local state ops and read-only SSH probes. Launch/rerun commands are returned to your Coding Agent's native Bash tool so normal approval prompts and your real SSH Agent / jump hosts apply.
- **Auto Failure Classification & Stale Detection**: Distinguishes `failed_oom`, `failed_disk`, `failed_quota`, `failed_net`, `failed_code`, and `aborted`, flags silent hanging runs (`running (stale 2h)`), and never overwrites status when SSH is unreachable.
- **Code Drift Guard**: Detects remote Git commit or dirty-diff drift before rerunning an experiment.
- **7 Pluggable Runners**: `ssh-tmux` (default), `ssh-nohup`, `ssh-screen`, `ssh-slurm` (with `sacct` `OUT_OF_MEMORY`/`TIMEOUT`/`PREEMPTED` parsing), `ssh-docker` (shared mounted `meta_dir`), `local`, and `custom`.
- **Single Repo, Multi-Agent Native**: Ships as an installable Python package (`exploom` CLI + `exploom-mcp` server) plus a thin ZCode / Claude Code Plugin layer (`SKILL.md`, 5 slash commands including `/exp-collect`, and ~15ms pure-stdlib hooks).

---

## Quick Start

### 1. Install (CLI + MCP)

```bash
# Install directly from GitHub via uv
uv tool install git+https://github.com/minghuaxu/exploom.git
# Or install from local source (editable)
uv tool install -e .
```

Zero-install MCP execution via `uvx`:

```bash
uvx --from git+https://github.com/minghuaxu/exploom.git exploom-mcp
```

### 2. Use as a ZCode / Claude Code Plugin

Add this repository as a local or GitHub marketplace source:
- **ZCode**: **Plugin Marketplace → Add → Add Plugin Marketplace** → enter `https://github.com/minghuaxu/exploom` (or local repo directory) → install `exploom`.
- **Claude Code**: `/plugin marketplace add minghuaxu/exploom` then `/plugin install exploom@exploom`.

It automatically wires:
- `.zcode-plugin/plugin.json` & `.zcode-plugin/marketplace.json`
- `.claude-plugin/plugin.json` & `.claude-plugin/marketplace.json`
- `.mcp.json` (uses local `exploom-mcp`, falling back to `uvx --from git+https://github.com/minghuaxu/exploom.git exploom-mcp`)
- `skills/exploom/SKILL.md` (<=60-line collaboration rules)
- Slash commands: `/exp`, `/exp-resume <E0xx>`, `/exp-sync`, `/exp-rerun <E0xx>`, `/exp-collect`
- Pure-stdlib hooks: `SessionStart` (~15ms read-only SQLite status card) and `PostToolUse(Bash)` (low-noise reminder for untracked remote background SSH jobs)

### 3. Optional `AGENTS.md` Integration (Custom Subagents)

Exploom works out of the box and never modifies your global agent config silently. If you orchestrate custom subagents in `AGENTS.md` / `CLAUDE.md`, run the idempotent CLI helper (uses `<!-- exploom:start -->` markers and supports `--remove`):

```bash
exploom integrate --zcode                # ~/.zcode/AGENTS.md
exploom integrate --claude               # ~/.claude/CLAUDE.md
exploom integrate                        # ./AGENTS.md in current project
exploom integrate --zcode --remove       # cleanly remove the managed block
```

Or add these 3 lines manually:

```markdown
- Before launching remote/local training or dispatching a subagent, allocate an ID via `exp_plan` and include `[EXP_ID=E0xx]` in the task prompt.
- Subagents receiving `[EXP_ID=E0xx]` must restore state via `exp_query(view="context", exp_id="E0xx")` (or `exploom show E0xx --json`), launch via `exp_attempt(action="start")`, and log conclusions via `exp_finding`.
- Subagents report back in <=3 lines: `E0xx#<attempt> | status & metrics | finding ID`.
```

---

## 5 Core MCP Tools

| Tool | Responsibility |
| :--- | :--- |
| `exp_plan` | Create or update an experiment node (`parent_id`, `based_on_finding`, `tags`, `metrics`, `status_override`). |
| `exp_attempt` | Register a run attempt (`start`: conflict check + returns `ssh_cmd` without executing), backfill historical/existing runs (`record`, no launch), or update status/metrics (`update`). |
| `exp_finding` | Attach analysis scripts, bad-case diagnoses, decisions, or artifacts and append to the Markdown note. |
| `exp_query` | Fast local read-only SQLite queries (`tree`, `list`, `context`, `resources`). Zero SSH. |
| `exp_ops` | The only tool that runs read-only SSH: `sync` (concurrent batch probe), `tail_log` (<=200 lines), `prepare_rerun` (drift check + returns `ssh_cmd`). |

## Human CLI (`exploom` / `exp`)

Every command supports `--json` (also usable as a fallback for subagents without MCP access):

```bash
exploom init [project]                     # Bind current directory to a project
exploom plan -t "sft_v1" -H "lr=2e-5"      # Create an experiment node
exploom attempt start E001 -s gpu-1 -w /work -r gpu:0,1 -c "python train.py"
exploom tree [-a] [--all]                  # Rich colored lineage tree
exploom ls [--failed] [--running] [--server gpu-1]
exploom show E001                          # Full context (lineage, attempts, findings, patches)
exploom edit E001                          # Open Markdown note in $EDITOR
exploom ps                                 # Active server/GPU/workdir matrix
exploom sync [E001]                        # Concurrent read-only SSH probe
exploom log E001 -n 80                     # Tail remote main log
exploom rerun E001 [--fresh] [--force]     # Drift check + launch resume_cmd
exploom integrate [--zcode|--claude]       # Idempotently add/remove AGENTS.md handoff block
```

## Pluggable Runners

| Runner | `runner_ref` | Probing Mechanism |
| :--- | :--- | :--- |
| `ssh-tmux` (default) | `exp_<proj>_<id>_<n>` | `tmux has-session` + `exit.json` + log mtime |
| `ssh-nohup` | PID (`$META_DIR/pid`) | `kill -0` + `exit.json` + log mtime |
| `ssh-screen` | `exp_<proj>_<id>_<n>` | `screen -ls` + `exit.json` + log mtime |
| `ssh-slurm` | Slurm `JobID` | `squeue -j` + `sacct -o State,ExitCode` (`OUT_OF_MEMORY`/`TIMEOUT`/`PREEMPTED`/`CANCELLED`) |
| `ssh-docker` | `<container>:<ref>` | Host shared `meta_dir` + `docker inspect` / `docker exec kill -0` |
| `local` | Local PID | Direct local check, no SSH |
| `custom` | Custom ref | `exit.json` / log mtime or manual `exp_attempt(update)` |

## Configuration

Copy [`examples/config.toml`](examples/config.toml) to `~/.exploom/config.toml` to customize `stale_threshold_minutes`, `ssh_connect_timeout`, custom failure regexes, or per-server Docker/Slurm `meta_dir` mounts.

## License

[MIT](LICENSE)
