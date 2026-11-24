"""Tests for Markdown notes and patch management."""

from __future__ import annotations

from pathlib import Path

from exploom.notes import (
    append_finding_to_note,
    ensure_note,
    read_note,
    save_patch,
    summarize_patch_files,
)


def test_note_creation_and_finding_append(tmp_path: Path) -> None:
    note_path = ensure_note(
        tmp_path / "notes",
        "demo",
        "E001",
        title="baseline_sft",
        hypothesis="Standard LR 2e-5 converges cleanly.",
        tags=["sft"],
    )
    assert note_path.is_file()
    initial = read_note(note_path)
    assert "# E001: baseline_sft" in initial
    assert "Standard LR 2e-5 converges cleanly." in initial

    append_finding_to_note(
        note_path,
        finding_id="E001-F1",
        kind="diagnosis",
        summary="Long prompts get truncated at 2048 tokens",
        attempt_no=1,
        artifacts=[{"server": "gpu-1", "path": "scripts/check_len.py"}],
        note_append="Need to increase max_seq_len to 4096.",
    )
    updated = read_note(note_path)
    assert "E001-F1" in updated
    assert "scripts/check_len.py" in updated
    assert "max_seq_len to 4096" in updated


def test_patch_truncation_and_file_summary(tmp_path: Path) -> None:
    sample_diff = (
        "diff --git a/src/train.py b/src/train.py\n"
        "--- a/src/train.py\n"
        "+++ b/src/train.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-lr = 1e-4\n"
        "+lr = 2e-5\n"
        "diff --git a/configs/sft.yaml b/configs/sft.yaml\n"
        "--- a/configs/sft.yaml\n"
        "+++ b/configs/sft.yaml\n"
        "@@ -1 +1 @@\n"
        "-bs: 8\n"
        "+bs: 16\n"
    )
    patch_path = save_patch(tmp_path / "patches", "demo", "E001", 1, sample_diff)
    assert summarize_patch_files(patch_path) == ["src/train.py", "configs/sft.yaml"]

    # Truncation test
    big_patch = "diff --git a/big.py b/big.py\n" + ("+line\n" * 50000)
    small_limit = 2048
    trunc_path = save_patch(
        tmp_path / "patches", "demo", "E001", 2, big_patch, max_bytes=small_limit
    )
    assert trunc_path.stat().st_size <= small_limit
    assert "Patch truncated" in trunc_path.read_text(encoding="utf-8")
