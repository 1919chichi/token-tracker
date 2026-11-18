"""Antigravity CLI (`agy`) 数据源：transcript.jsonl 的 PLANNER_RESPONSE → UsageEntry。

布局：`<agy_home>/brain/<conversation_id>/.system_generated/logs/transcript.jsonl`
与 `<agy_home>/conversation_summaries.db`（记录 conversation_id → workspace_uris）。
"""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from .types import AgentInfo, UsageEntry
from .util import agy_home, file_may_have_events_since, iter_jsonl_dicts, project_from_cwd

AGY_DIR = agy_home()
BRAIN_DIR = os.path.join(AGY_DIR, "brain")
SUMMARIES_DB = os.path.join(AGY_DIR, "conversation_summaries.db")


def detect() -> AgentInfo | None:
    """以 Antigravity CLI 配置根目录判断是否安装。"""
    if Path(AGY_DIR).is_dir():
        return AgentInfo(id="agy", name="Antigravity")
    return None


def load_entries(hours_back: int = 0) -> list[UsageEntry]:
    cutoff = None
    if hours_back > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=hours_back)
    return _load_entries(cutoff, cutoff)


def load_recent_entries(cutoff: datetime) -> list[UsageEntry]:
    """读取 cutoff 后仍有写入的完整会话，供 `tt sessions` 渐进查找最近 N 条。"""
    return _load_entries(cutoff, None)


def _load_entries(file_cutoff: datetime | None, entry_cutoff: datetime | None) -> list[UsageEntry]:
    entries: list[UsageEntry] = []
    seen: set[str] = set()

    brain_path = Path(BRAIN_DIR)
    if not brain_path.is_dir():
        return entries

    projects = _load_conversation_projects()
    for transcript_path in brain_path.glob("*/.system_generated/logs/transcript.jsonl"):
        if not file_may_have_events_since(transcript_path, file_cutoff):
            continue
        session_id = transcript_path.parents[2].name
        project = projects.get(session_id, "unknown")
        _parse_transcript(transcript_path, session_id, project, entries, seen, entry_cutoff)

    entries.sort(key=lambda e: e.timestamp)
    return entries


def _load_conversation_projects() -> dict[str, str]:
    """从 conversation_summaries.db 只读映射 conversation_id → 项目名。"""
    db_path = Path(SUMMARIES_DB)
    if not db_path.is_file():
        return {}
    projects: dict[str, str] = {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        rows = conn.execute("SELECT conversation_id, workspace_uris FROM conversation_summaries")
        for conv_id, raw_uris in rows:
            if not isinstance(conv_id, str) or not conv_id:
                continue
            project = _project_from_workspace_uris(raw_uris)
            if project:
                projects[conv_id] = project
    except (sqlite3.Error, OSError, ValueError):
        return projects
    finally:
        if conn is not None:
            conn.close()
    return projects


def _project_from_workspace_uris(raw_uris: object) -> str:
    if not isinstance(raw_uris, str) or not raw_uris:
        return ""
    try:
        uris = json.loads(raw_uris)
    except ValueError:
        return ""
    if not isinstance(uris, list):
        return ""
    for uri in uris:
        if not isinstance(uri, str) or not uri:
            continue
        cwd = _file_uri_to_path(uri)
        if cwd:
            return project_from_cwd(cwd)
    return ""


def _file_uri_to_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    # Windows file:///D:/repo → urlparse.path 为 /D:/repo，需剥掉前导斜杠
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return os.path.normpath(path) if path else ""


def _parse_transcript(
    path: Path,
    session_id: str,
    project: str,
    entries: list[UsageEntry],
    seen: set[str],
    cutoff: datetime | None,
) -> None:
    for data in iter_jsonl_dicts(path):
        if data.get("type") != "PLANNER_RESPONSE":
            continue
        ts = _parse_iso_ts(data.get("created_at"))
        if ts is None or (cutoff is not None and ts < cutoff):
            continue
        entry = _response_to_entry(data, ts, session_id, project)
        if entry is None or entry.dedup_key in seen:
            continue
        seen.add(entry.dedup_key)
        entries.append(entry)


def _parse_iso_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _response_to_entry(
    data: dict, ts: datetime, session_id: str, project: str
) -> UsageEntry | None:
    input_tokens = _int_or_zero(data.get("input_tokens"))
    output_tokens = _int_or_zero(data.get("output_tokens"))
    cache_read_tokens = _int_or_zero(data.get("cache_read_tokens"))
    if not (input_tokens or output_tokens or cache_read_tokens):
        return None
    model = data.get("model")
    step_index = data.get("step_index")
    step_key = step_index if isinstance(step_index, int) and not isinstance(step_index, bool) else data.get("created_at")
    return UsageEntry(
        timestamp=ts,
        session_id=session_id,
        message_id=f"{session_id}:{step_key}",
        request_id="",
        model=model if isinstance(model, str) and model else "unknown",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=cache_read_tokens,
        cost_usd=None,
        project=project,
        agent_id="agy",
    )


def _int_or_zero(val: object) -> int:
    return val if isinstance(val, int) and not isinstance(val, bool) and val > 0 else 0
