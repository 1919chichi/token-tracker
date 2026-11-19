#!/usr/bin/env python3
"""Antigravity CLI (`agy`) custom statusLine — 单行紧凑状态栏（token-tracker 风格）。

由本地 token-tracker 的 `agy_setup.py` 烘焙生成（注入当前主题的 truecolor / 256 配色），
勿手改。agy 通过 settings.json 的 `statusLine.command` 调起本脚本，stdin 是一份 JSON
快照（含 cwd / workspace.current_dir / context_window / model / cost / quota 等），stdout
**只输出一行**；省略 `stack_with_default` 时这一行替换 agy 内置默认状态栏。
agy 1.5.x 的快照**不带 vcs 字段**，所以 git 分支由脚本按 cwd 主动探测：分支读 `.git/HEAD`
（零子进程，支持 worktree / detached HEAD），未提交增删行 / 未跟踪数用 `git diff HEAD --numstat`
+ `git ls-files --others`（同 CC / Kimi 状态栏口径），带 2s TTL 落盘缓存防高频刷新卡顿。

紧凑模式的取舍（与 tt 状态栏窄终端降级同款）：字段单行 ` | ` 拼接，5h / 7d 只显示百分比数字、
Ctx 显示「已用 / 总容量 + 百分比」，不画进度条、不带重置倒计时；缺字段直接省略，不占位。
字段解析对 agy 版本差异做防御式兼容：同一语义按多条候选路径取值，全部取不到就跳过。
"""
__version__ = "__AGY_STATUSLINE_VERSION__"
import json, os, re, subprocess, sys, tempfile, threading, time, unicodedata

# 进程硬性存活上限（秒）：agy 约 1s 未拿到输出就判失败并结束它直接拉起的进程，
# 但 Windows 不连带结束子孙进程，本脚本若卡在 stdin.read()（管道写端未关）会成为孤儿进程永久挂住。
# 到点直接 os._exit，不依赖 stdin EOF、git 子进程或 agy 回收。
_HARD_DEADLINE = 2.0

# 配色由 agy_setup 烘焙时从 token_tracker.ui.themes 注入（当前主题），与 CC / Kimi 状态栏同源
THEME_COLORS = __STATUSLINE_TRUECOLOR__
THEME_COLORS_256 = __STATUSLINE_COLOR256__

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
STATE_DIR = os.path.join(os.path.expanduser("~"), ".config", "token-tracker")
PAYLOAD_DUMP = os.path.join(STATE_DIR, "agy-statusline-payload.json")


def _use_truecolor():
    if os.environ.get("TT_AGY_SL_COLOR256"):
        return False
    return os.environ.get("COLORTERM", "") in ("truecolor", "24bit") or bool(os.environ.get("TT_AGY_SL_TRUECOLOR"))


_NO_COLOR = bool(os.environ.get("NO_COLOR") or os.environ.get("TT_AGY_SL_NO_COLOR"))
C = ({key: "" for key in THEME_COLORS} if _NO_COLOR
     else (THEME_COLORS if _use_truecolor() else THEME_COLORS_256))
RST = C.get("reset", "")
BOLD = "" if _NO_COLOR else "\x1b[1m"


def vlen(s):
    """可见列宽：去掉 ANSI 后按东亚全角算 2 列（同 tt 状态栏，防 CJK 折行）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in ANSI_RE.sub("", s))


def fmt_tokens(n):
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.0f}k"
    return str(int(n))


def pick(payload, *paths, default=None):
    """按多条候选路径取第一个非空值；路径形如 'usage.total_tokens'。"""
    for path in paths:
        cur = payload
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if cur is not None and cur != "" and cur != {} and cur != []:
            return cur
    return default


def as_num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def as_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("display_name", "displayName", "name", "model_name", "modelName", "id", "title"):
            v = value.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


GIT_CACHE = os.path.join(STATE_DIR, "agy-git-cache.json")
_GIT_TTL = 2.0  # 秒：TUI 每帧都会调状态栏，git 子进程结果短时复用；工作区改动靠 TTL 兜底


def _git_dir(cwd):
    """定位 gitdir：普通仓库是 .git/，worktree / submodule 的 .git 是 'gitdir: ...' 文件。"""
    dotgit = os.path.join(cwd, ".git")
    if os.path.isdir(dotgit):
        return dotgit
    if os.path.isfile(dotgit):
        try:
            with open(dotgit, encoding="utf-8", errors="replace") as f:
                txt = f.read().strip()
            if txt.startswith("gitdir:"):
                return os.path.normpath(os.path.join(cwd, txt[7:].strip()))
        except Exception:
            pass
    return None


def _git_stamp(cwd):
    """HEAD + index 的 mtime：切分支 / commit / 暂存后让缓存立即失效。"""
    gd = _git_dir(cwd)
    if not gd:
        return ""
    parts = []
    for rel in ("HEAD", "index"):
        try:
            parts.append(f"{rel}:{os.path.getmtime(os.path.join(gd, rel)):.3f}")
        except OSError:
            parts.append(f"{rel}:-")
    return "|".join(parts)


def _git_branch_from_head(cwd):
    """直接读 .git/HEAD 拿分支名（零子进程）；detached HEAD 显示短 sha。"""
    gd = _git_dir(cwd)
    if not gd:
        return ""
    try:
        with open(os.path.join(gd, "HEAD"), encoding="utf-8", errors="replace") as f:
            head = f.read().strip()
    except Exception:
        return ""
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", head):
        return head[:7]
    return ""


def _run_quick(argv, cwd, timeout):
    """跑子进程取 stdout，超时返回 None 且不再等待管道关闭。
    不用 check_output(timeout=)：它在 Windows 上超时后只结束直接子进程，随后无超时地 communicate()；
    Git for Windows 的 cmd\\git.exe 是启动器，真正的 git.exe 是孙进程且继承了 stdout，
    会让“超时”变成等 git 真正跑完。communicate 的读线程是 daemon 线程，弃之不等即可。"""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    except Exception:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return None
    except Exception:
        return None
    return out if proc.returncode == 0 else None


def _git_probe(cwd):
    """(branch, added, deleted, untracked)：分支读 HEAD 文件，增删行 / 未跟踪数复用 tt 的 git 口径。"""
    branch = _git_branch_from_head(cwd)
    if not branch:
        return ("", 0, 0, 0)
    added = deleted = untracked = 0
    out = _run_quick(["git", "diff", "HEAD", "--numstat"], cwd, 0.25)
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            if parts[0].isdigit():
                added += int(parts[0])
            if parts[1].isdigit():
                deleted += int(parts[1])
    out = _run_quick(["git", "ls-files", "--others", "--exclude-standard"], cwd, 0.2)
    untracked = sum(1 for ln in (out or "").splitlines() if ln.strip())
    return (branch, added, deleted, untracked)


def _git_info(cwd):
    """带 2s TTL 落盘缓存的 git 状态；非 git 仓库 / git 不可用返回全空（只显示项目名）。"""
    if not cwd or not os.path.isdir(cwd):
        return ("", 0, 0, 0)
    stamp = _git_stamp(cwd)
    if not stamp:
        return ("", 0, 0, 0)
    now = time.time()
    try:
        with open(GIT_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        if (cache.get("cwd") == cwd and cache.get("stamp") == stamp
                and now - float(cache.get("ts", 0)) < _GIT_TTL):
            value = cache.get("value")
            if isinstance(value, list) and len(value) == 4:
                return (str(value[0]), int(value[1]), int(value[2]), int(value[3]))
    except Exception:
        pass
    value = _git_probe(cwd)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="agy-git-", suffix=".json", dir=STATE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"cwd": cwd, "stamp": stamp, "ts": now, "value": list(value)}, f)
        os.replace(tmp, GIT_CACHE)
    except Exception:
        pass
    return value


def project_segment(payload):
    cwd = pick(payload, "cwd", "current_dir", "project_dir", "workspace.current_dir",
               "workspace.project_dir", "workspace.path", "workspace.uri",
               "workspace.name", "workspace", default="")
    if isinstance(cwd, dict):
        cwd = as_text(cwd)
    if not isinstance(cwd, str) or not cwd:
        return ""
    # file:// URI 还原成本地路径
    if cwd.startswith("file://"):
        cwd = cwd[7:].lstrip("/")
    name = os.path.basename(cwd.replace("\\", "/").rstrip("/")) or cwd
    # agy 1.5.x 的 stdin 快照没有 vcs 字段：主动探测 git；payload 有则作为回退 / 补充
    branch, added, deleted, untracked = _git_info(cwd)
    if not branch:
        branch = pick(payload, "vcs.branch", "vcs.current_branch", "vcs.currentBranch",
                      "vcs.branch_name", "vcs.branchName", "vcs.name", "current_branch",
                      "git_branch", "branch", default="")
        branch = as_text(branch) if isinstance(branch, dict) else (branch if isinstance(branch, str) else "")
    if not branch:
        return f"{BOLD}{C['project']}[{name}]{RST}"
    dirty = bool(added or deleted or untracked) or bool(
        pick(payload, "vcs.dirty", "vcs.has_changes", default=False))
    if not added:
        added = int(as_num(pick(payload, "vcs.added", "vcs.added_lines", default=0)) or 0)
    if not deleted:
        deleted = int(as_num(pick(payload, "vcs.deleted", "vcs.deleted_lines", default=0)) or 0)
    inner = f"{C['branch']}{branch}{RST}"
    if dirty or added or deleted:
        inner += f"{C['branch']}*{RST}"
    if added:
        inner += f" {C['added']}+{added}{RST}"
    if deleted:
        inner += f" {C['deleted']}-{deleted}{RST}"
    if untracked:
        inner += f" {C['untracked']}?{untracked}{RST}"
    return f"{BOLD}{C['project']}[{name}]{RST}({inner})"


def tokens_segment(payload):
    total = as_num(pick(payload, "usage.total_tokens", "total_tokens", "current_usage.total_tokens",
                        "total_usage.total_tokens", "usage.totalTokens", "context.total_tokens",
                        "context_window.current_usage.total_tokens", "tokens.total",
                        "tokens.total_tokens", "session.total_tokens"))
    if total is None:
        # agy 1.5.x 实测：上下文用量在 context_window.total_input_tokens / total_output_tokens
        ctx_in = as_num(pick(payload, "context_window.total_input_tokens",
                             "context_window.current_usage.input_tokens", default=0)) or 0
        ctx_out = as_num(pick(payload, "context_window.total_output_tokens",
                              "context_window.current_usage.output_tokens", default=0)) or 0
        parts = [ctx_in, ctx_out] + [as_num(pick(payload, f"usage.{k}", f"current_usage.{k}", k)) or 0
                                   for k in ("input_tokens", "output_tokens", "thinking_tokens",
                                             "reasoning_tokens", "cache_read_tokens", "cached_tokens")]
        total = sum(parts) or None
    if not total:
        return ""
    return f"{C['total']}Total: {fmt_tokens(total)}{RST}"


def cost_segment(payload):
    cost = as_num(pick(payload, "cost", "total_usd", "usage.cost", "session.cost",
                       "cost_usd", "usage.total_usd", "total_usage.cost_usd"))
    if cost is None or cost <= 0:
        return ""
    return f"{C['tokens']}Cost: ${cost:.2f}{RST}"


def _pct_of(raw):
    """百分比：优先 used_percentage / percentage；只有 remaining_percentage 时换算成已用。"""
    if isinstance(raw, dict):
        pct = as_num(pick(raw, "used_percentage", "percentage", "pct", "percent", "used"))
        if pct is None:
            remaining = as_num(pick(raw, "remaining_percentage", "remaining"))
            if remaining is not None:
                pct = 100.0 - remaining
        return pct
    return as_num(raw)


def _quota_label(raw):
    low = raw.lower()
    if "7" in low or "week" in low or "seven" in low:
        return "7d"
    if "5" in low or "five" in low or "hour" in low or "short" in low:
        return "5h"
    return raw or ""


def quota_segments(payload):
    """5h / 7d 百分比（紧凑模式只显示数字，不画进度条/倒计时）。
    quota 可能是 dict（键 five_hour/seven_day/5h/7d）、bucket 列表，或 quota_buckets 包装。"""
    quota = pick(payload, "quota", "quota_buckets", "quotas", "rate_limits", "limits", default=None)
    if isinstance(quota, dict):
        buckets = None
        for key in ("buckets", "quota_buckets", "quota_bucket", "resets", "quota_resets"):
            value = quota.get(key)
            if isinstance(value, list):
                buckets = value
                break
            if isinstance(value, dict):
                buckets = list(value.values())
                break
        if buckets is None:
            return [f"{C['label']}{label}: {int(round(_pct_of(quota[key])))}%{RST}"
                    for label, keys in (("5h", ("five_hour", "5h", "fiveHour", "short")),
                                        ("7d", ("seven_day", "7d", "sevenDay", "weekly", "long")))
                    for key in keys if key in quota and _pct_of(quota[key]) is not None]
        quota = buckets
    out = []
    if isinstance(quota, list):
        for item in quota:
            if not isinstance(item, dict):
                continue
            label = _quota_label(as_text(pick(item, "window", "label", "name", "bucket", default="")))
            if not label:
                continue
            pct = _pct_of(item)
            if pct is None:
                pct = as_num(pick(item, "utilization", "used_pct"))
            if pct is not None:
                out.append(f"{C['label']}{label}: {int(round(pct))}%{RST}")
    return out


def context_segment(payload):
    """上下文窗口：已用 / 总容量 + 百分比（同 CC statusline 的 `1.0M Ctx: 20%` 口径，紧凑化）。
    总容量取 agy 实测字段 context_window.context_window_size，已用取 total_input_tokens；
    缺哪个就只显示有的，只有百分比时保持 `Ctx: N%`。启动初期全 0 则整段省略。"""
    pct = as_num(pick(payload, "context.used_percentage", "context_window.used_percentage",
                      "context_window.percentage", "context.percentage", "usage.context_percentage",
                      "usage.context_window_used_percentage", "context_pct", "context.percent"))
    if pct is None:
        remaining = as_num(pick(payload, "context_window.remaining_percentage",
                                "context.remaining_percentage", default=None))
        if remaining is not None:
            pct = 100.0 - remaining
    size = as_num(pick(payload, "context_window.context_window_size", "context_window_size",
                       "context.window_size", "context.size", "context_window.total", default=0)) or 0
    used = as_num(pick(payload, "context_window.total_input_tokens",
                       "context_window.current_usage.input_tokens",
                       "context_window.used_tokens", "usage.context_tokens", default=0)) or 0
    if (pct is None or pct <= 0) and not size and not used:
        return ""
    parts = []
    if used and size:
        parts.append(f"{fmt_tokens(used)}/{fmt_tokens(size)}")
    elif size:
        parts.append(fmt_tokens(size))
    elif used:
        parts.append(fmt_tokens(used))
    if pct is not None and pct > 0:
        parts.append(f"{int(round(pct))}%")
    if not parts:
        return ""
    return f"{C['duration']}Ctx: {' '.join(parts)}{RST}"


def model_segment(payload):
    model = pick(payload, "model", "model_info", "current_model", "current_model_id",
                 "currentModelId", default="")
    name = as_text(model)
    if not name:
        return ""
    effort = as_text(pick(payload, "effort", "model.effort", "thinking_level", default=""))
    if effort:
        name += f"/{effort}"
    return f"{C['model']}Model: {name}{RST}"


def dump_payload_once(payload):
    """首次调用落一份原始快照，便于按真实字段核对/精修；已存在则不再写（零常态开销）。"""
    try:
        if os.path.exists(PAYLOAD_DUMP):
            return
        os.makedirs(STATE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="agy-sl-payload-", dir=STATE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PAYLOAD_DUMP)
    except Exception:
        pass


def render(payload):
    proj = project_segment(payload)
    total = tokens_segment(payload)
    cost = cost_segment(payload)
    quotas = quota_segments(payload)
    ctx = context_segment(payload)
    model = model_segment(payload)
    items = [("proj", proj), ("total", total), ("cost", cost)]
    items += [(f"q{i}", q) for i, q in enumerate(quotas)]
    items += [("ctx", ctx), ("model", model)]
    items = [(key, text) for key, text in items if text]
    # 单行硬保证：按可见列宽裁剪（agy 快照带 terminal_width；回落 COLUMNS / 120）。
    # 降级顺序与「紧凑」一致：先丢 Ctx / 配额这类次要段，尽量保住项目、Total、Cost、Model。
    width = as_num(pick(payload, "terminal_width", "width", default=0)) or as_num(os.environ.get("COLUMNS")) or 120
    width = max(20, int(width) - 2)
    line = " | ".join(text for _, text in items)
    if vlen(line) > width:
        dropped = set()
        # 降级顺序：最长且用户未特别要求的 Model 先丢，再丢 7d / 5h 配额；
        # Ctx（已用 / 总量 / 百分比）尽量保留，Total / Cost 最后才考虑丢弃。
        drop_order = ["model", *[f"q{i}" for i in reversed(range(len(quotas)))],
                      "ctx", "cost", "total"]
        for key in drop_order:
            dropped.add(key)
            line = " | ".join(text for k, text in items if k not in dropped)
            if vlen(line) <= width:
                break
    if vlen(line) > width:  # 项目名本身就超宽：硬截断可见字符（保底仍是一行）
        line = truncate_visible(line, width)
    return line


def truncate_visible(line, width):
    """按可见列宽截断：ANSI 序列整段保留、不计宽，绝不切出半截转义；截断后补复位符防颜色串到后文。"""
    out, used, pos, has_ansi = [], 0, 0, False
    while pos < len(line):
        m = ANSI_RE.match(line, pos)
        if m:
            out.append(m.group())
            has_ansi = True
            pos = m.end()
            continue
        ch = line[pos]
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
        pos += 1
    if has_ansi:
        out.append("\x1b[0m")
    return "".join(out)


def _arm_deadline(seconds=_HARD_DEADLINE):
    timer = threading.Timer(seconds, os._exit, args=(0,))
    timer.daemon = True
    timer.start()
    return timer


def main():
    _arm_deadline()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    dump_payload_once(payload)
    try:
        line = render(payload)
    except Exception:
        line = f"{BOLD}{C['project']}[tt]{RST}"
    text = (line if line else f"{BOLD}{C['project']}[tt]{RST}") + "\n"
    # 直接写 buffer：Windows 文本层的 newline 翻译会把 \n 变成 \r\n，agy 读首行会留下 \r
    try:
        sys.stdout.buffer.write(text.encode("utf-8", "replace"))
        sys.stdout.buffer.flush()
    except Exception:
        sys.stdout.write(text)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
