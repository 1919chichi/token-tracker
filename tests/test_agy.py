"""Antigravity CLI (agy) adapter：transcript.jsonl 的 PLANNER_RESPONSE → UsageEntry。"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from token_tracker.adapters import agy


def _iso(seconds_ago: float = 60) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _planner_row(
    step_index: int,
    seconds_ago: float = 60,
    model: str = "gemini-4-hydrogen-medium",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> dict:
    return {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": _iso(seconds_ago),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
    }


def _write_transcript(brain_dir: Path, conv_id: str, rows: list[dict]) -> Path:
    transcript = brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    with open(transcript, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return transcript


def _write_summaries_db(db_path: Path, mapping: dict[str, list[str]]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, workspace_uris TEXT NOT NULL)"
    )
    for conv_id, uris in mapping.items():
        conn.execute(
            "INSERT INTO conversation_summaries (conversation_id, workspace_uris) VALUES (?, ?)",
            (conv_id, json.dumps(uris)),
        )
    conn.commit()
    conn.close()


def _patch_agy(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    brain_dir = tmp_path / "brain"
    db_path = tmp_path / "conversation_summaries.db"
    monkeypatch.setattr(agy, "AGY_DIR", str(tmp_path))
    monkeypatch.setattr(agy, "BRAIN_DIR", str(brain_dir))
    monkeypatch.setattr(agy, "SUMMARIES_DB", str(db_path))
    return brain_dir, db_path


def test_detect_uses_install_root(tmp_path, monkeypatch):
    agy_dir = tmp_path / ".gemini" / "antigravity-cli"
    monkeypatch.setattr(agy, "AGY_DIR", str(agy_dir))
    assert agy.detect() is None
    agy_dir.mkdir(parents=True)
    info = agy.detect()
    assert info is not None and info.id == "agy" and info.name == "Antigravity"


def test_planner_responses_become_entries_with_project_from_db(tmp_path, monkeypatch):
    brain_dir, db_path = _patch_agy(monkeypatch, tmp_path)
    proj = tmp_path / "token-tracker"
    _write_summaries_db(db_path, {"conv-1": [proj.as_uri()]})
    _write_transcript(
        brain_dir,
        "conv-1",
        [
            {"step_index": 0, "type": "USER_INPUT", "created_at": _iso(130), "content": "hi"},
            _planner_row(1, 120, input_tokens=15383, output_tokens=540, cache_read_tokens=0),
            _planner_row(1, 120, input_tokens=999, output_tokens=1),  # 同 step_index 重复行去重
            _planner_row(6, 60, input_tokens=9444, output_tokens=593, cache_read_tokens=15384),
        ],
    )

    entries = agy.load_entries()
    assert len(entries) == 2
    first, second = entries
    assert first.agent_id == "agy"
    assert first.session_id == "conv-1"
    assert first.model == "gemini-4-hydrogen-medium"
    assert first.project == "token-tracker"
    assert first.input_tokens == 15383
    assert first.output_tokens == 540
    assert first.cache_read_tokens == 0
    assert first.cache_creation_tokens == 0
    assert first.cost_usd is None
    assert second.cache_read_tokens == 15384
    assert first.dedup_key != second.dedup_key


def test_missing_db_falls_back_to_unknown_project(tmp_path, monkeypatch):
    brain_dir, _ = _patch_agy(monkeypatch, tmp_path)
    _write_transcript(
        brain_dir,
        "conv-no-db",
        [_planner_row(1, 60, input_tokens=100, output_tokens=20)],
    )

    entries = agy.load_entries()
    assert len(entries) == 1
    assert entries[0].project == "unknown"


def test_zero_usage_rows_skipped(tmp_path, monkeypatch):
    brain_dir, _ = _patch_agy(monkeypatch, tmp_path)
    _write_transcript(
        brain_dir,
        "conv-zero",
        [
            _planner_row(1, 60, input_tokens=0, output_tokens=0, cache_read_tokens=0),
            _planner_row(2, 50, input_tokens=10, output_tokens=0, cache_read_tokens=0),
        ],
    )

    entries = agy.load_entries()
    assert len(entries) == 1
    assert entries[0].input_tokens == 10


def test_hours_back_and_recent_entries(tmp_path, monkeypatch):
    brain_dir, _ = _patch_agy(monkeypatch, tmp_path)
    _write_transcript(
        brain_dir,
        "conv-window",
        [
            _planner_row(1, 3600 * 10, input_tokens=100, output_tokens=1),
            _planner_row(2, 60, input_tokens=200, output_tokens=2),
        ],
    )

    filtered = agy.load_entries(hours_back=1)
    assert len(filtered) == 1
    assert filtered[0].input_tokens == 200

    recent = agy.load_recent_entries(datetime.now(UTC) - timedelta(hours=1))
    assert len(recent) == 2


def _statusline_namespace() -> dict:
    from token_tracker import agy_setup

    namespace: dict = {"__name__": "agy_statusline_under_test"}
    exec(compile(agy_setup.render_script(), "agy-statusline.py", "exec"), namespace)
    return namespace


def test_statusline_hard_truncate_keeps_ansi_sequences_whole(monkeypatch, tmp_path):
    ns = _statusline_namespace()
    ansi = ns["ANSI_RE"]
    # 项目名本身超宽才会走硬截断；git 探测隔离掉，只看截断行为
    monkeypatch.setitem(ns, "_git_info", lambda cwd: ("", 0, 0, 0))
    long_name = "a-very-long-project-name-that-cannot-fit"
    for width in range(22, 60):
        line = ns["render"]({"cwd": str(tmp_path / long_name), "terminal_width": width})
        assert ns["vlen"](line) <= width - 2
        # 去掉完整 ANSI 序列后不得残留 ESC：没有被切成半截的转义
        assert "\x1b" not in ansi.sub("", line)
        if "\x1b" in line:
            assert line.endswith("\x1b[0m")


def test_statusline_truncate_visible_counts_cjk_and_skips_ansi():
    ns = _statusline_namespace()
    truncate = ns["truncate_visible"]
    assert truncate("\x1b[1m中文abc\x1b[0m", 5) == "\x1b[1m中文a\x1b[0m"
    assert truncate("plain-text", 5) == "plain"


def _spawn_statusline(tmp_path: Path) -> subprocess.Popen:
    from token_tracker import agy_setup

    script = tmp_path / "agy-statusline.py"
    script.write_text(agy_setup.render_script(), encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    return subprocess.Popen(
        [sys.executable, "-S", str(script)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
    )


def test_statusline_exits_by_itself_when_stdin_never_closes(tmp_path):
    # agy 超时只结束直接子进程，管道写端没关时脚本会卡在 stdin.read()；硬性上限必须让它自己退出
    proc = _spawn_statusline(tmp_path)
    started = time.monotonic()
    try:
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.close()
        proc.stdout.close()
    assert proc.returncode == 0
    assert time.monotonic() - started < 6


def test_statusline_still_renders_one_line_before_deadline(tmp_path):
    proc = _spawn_statusline(tmp_path)
    out, _ = proc.communicate(json.dumps({"cwd": str(tmp_path / "demo")}).encode(), timeout=10)
    assert proc.returncode == 0
    lines = out.decode("utf-8").splitlines()
    assert len(lines) == 1 and "[demo]" in lines[0]


def test_run_quick_does_not_wait_for_grandchild_holding_stdout():
    # 模拟 Git for Windows 的启动器：直接子进程拉起继承 stdout 的孙进程后自己也阻塞
    ns = _statusline_namespace()
    hold = 4
    code = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({hold})']);"
        f"time.sleep({hold})"
    )
    started = time.monotonic()
    assert ns["_run_quick"]([sys.executable, "-c", code], None, 0.3) is None
    assert time.monotonic() - started < hold - 1


def test_run_quick_returns_stdout_and_rejects_nonzero_exit():
    ns = _statusline_namespace()
    utf8_writer = "import sys; sys.stdout.buffer.write('中文'.encode('utf-8'))"  # 同 git：输出 UTF-8 字节
    assert ns["_run_quick"]([sys.executable, "-c", utf8_writer], None, 5) == "中文"
    assert ns["_run_quick"]([sys.executable, "-c", "import sys; sys.exit(1)"], None, 5) is None
