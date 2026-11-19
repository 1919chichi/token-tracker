"""Antigravity CLI (`agy`) 状态栏安装器 —— 只动 agy 的 CLI 配置，不碰其它 Agent。

与 hooks.py 的三组件（Claude Code / Codex / Kimi）完全解耦：本模块不读写它们的
settings.json / config.toml / hooks.json，也不改 tt 自己的 setup 意图（config.json）。

agy 1.5.x 的自定义状态栏是 `~/.gemini/antigravity-cli/settings.json` 的 `statusLine`：
    {"statusLine": {"command": "...", "enabled": true}}
`command` 由 TUI 拉起，stdin 是一份 JSON 快照，stdout 首行替换内置默认状态栏（
`stack_with_default=true` 才是与默认状态栏上下叠加；缺省即替换）。agy 1.5.10 实测会在
启动时把该段规范化成 `{"type": "", "command": ..., "enabled": true}`，本模块保留它补的字段。
等价于 TUI 里的 `/statusline <command>`，但可脚本化、可回滚。

用法（本地仓库环境）：
    python -m token_tracker.agy_setup            # 安装 / 幂等重跑（重烘焙脚本 + 同步 command）
    python -m token_tracker.agy_setup --status   # 查看当前配置
    python -m token_tracker.agy_setup --uninstall  # 还原安装前的 statusLine
"""
import json
import os
import sys
import tempfile
from importlib import resources

from . import config
from .adapters.util import agy_home
from .ui import themes

AGY_SETTINGS = os.path.join(agy_home(), "settings.json")
SCRIPT_PATH = os.path.join(config.CONFIG_DIR, "agy-statusline.py")
BACKUP_PATH = os.path.join(config.CONFIG_DIR, "agy-statusline-backup.json")
AGY_STATUSLINE_VERSION = "1.1"  # 1.1: 2s 硬性存活上限防孤儿进程挂住；git 子进程超时不再等孙进程关管道

# 与 hooks.py 的三组件版本号互不干扰：本脚本只由本模块烘焙。
_MARKER = "agy-statusline.py"


def render_script() -> str:
    """把本地 token-tracker 当前主题的 truecolor / 256 配色 + 版本号注入模板。"""
    name = config.resolve_theme()
    return (
        (resources.files("token_tracker.templates") / "agy_statusline.py").read_text(encoding="utf-8")
        .replace("__AGY_STATUSLINE_VERSION__", AGY_STATUSLINE_VERSION)
        .replace("__STATUSLINE_TRUECOLOR__", repr(themes.theme_to_statusline_ansi(name)))
        .replace("__STATUSLINE_COLOR256__", repr(themes.theme_to_statusline_ansi(name, "256")))
    )


def _read_settings() -> dict:
    """agy settings.json 可能带 UTF-8 BOM（1.5.10 已修，但历史文件可能残留），统一 utf-8-sig 读。"""
    with open(AGY_SETTINGS, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{AGY_SETTINGS}: root is not a JSON object")
    return data


def _atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="agy-sl-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _stable_python() -> str | None:
    """优先用全局 tt 工具环境的解释器（随 token-tracker 长期存在，路径无空格）。
    本地开发仓库的 .venv 可能被重建/删除，不宜作为 agy 状态栏命令的长期依赖。"""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        cand = (os.path.join(appdata, "uv", "tools", "token-tracker", "Scripts", "python.exe")
                if appdata else "")
    else:
        cand = os.path.expanduser("~/.local/share/uv/tools/token-tracker/bin/python")
    return cand if cand and os.path.isfile(cand) else None


def _python_exe() -> str:
    """状态栏脚本由 agy 用 shell 拉起：用绝对路径，避免 PATH 差异。
    可用 TT_AGY_SL_PYTHON 覆盖（换 Python / 换 venv 后重跑本模块即可刷新 command）。"""
    exe = (os.environ.get("TT_AGY_SL_PYTHON", "").strip()
           or _stable_python() or sys.executable)
    return os.path.abspath(exe).replace("\\", "/")


def _command() -> str:
    """路径不含空格时裸拼接（规避 cmd.exe /d /s /c 吞双引号；同 Kimi 状态栏的 Windows 坑）。
    `-S` 跳过 site 初始化：状态栏脚本只用标准库，实测每次调用省约 0.12s（TUI 高频刷新）。"""
    py, script = _python_exe(), os.path.abspath(SCRIPT_PATH).replace("\\", "/")
    fast = "-S "
    if " " in py or " " in script:
        return f'"{py}" {fast}"{script}"'
    return f"{py} {fast}{script}"


def _backup_original(settings: dict) -> None:
    """只备份**最初**的原值：幂等重跑不覆盖，保证 unsetup 能还原到安装前而非还原成 tt 自己。"""
    if os.path.exists(BACKUP_PATH):
        return
    _atomic_write_json(BACKUP_PATH, {
        "agent": "agy",
        "settings_path": AGY_SETTINGS.replace("\\", "/"),
        "statusLine": settings.get("statusLine"),  # None = 原本用 agy 内置默认状态栏
        "settings_snapshot": settings,
    })


def installed_statusline() -> dict | None:
    try:
        sl = _read_settings().get("statusLine")
        return sl if isinstance(sl, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def install() -> dict:
    """渲染脚本 + 写 agy settings.json 的 statusLine。只改 agy，其它 Agent 配置零触碰。"""
    if not os.path.isdir(agy_home()):
        raise FileNotFoundError(f"未检测到 agy CLI 配置目录：{agy_home()}")
    if not os.path.isfile(AGY_SETTINGS):
        raise FileNotFoundError(f"未找到 agy settings.json：{AGY_SETTINGS}")

    settings = _read_settings()
    _backup_original(settings)

    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    with open(SCRIPT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_script())

    command = _command()
    # 保留 agy 自己规范化时补的字段（实测 1.5.10 会写回 "type": ""）；只改 command / enabled。
    # 不写 stack_with_default：该字段缺省即 false = 用自定义输出替换内置默认状态栏（不上下叠加），
    # 且 agy 启动时会自动把它规范化掉，写它反而让文件反复变化。
    sl = dict(settings["statusLine"]) if isinstance(settings.get("statusLine"), dict) else {}
    sl.update({"command": command, "enabled": True})
    sl.pop("stack_with_default", None)
    settings["statusLine"] = sl
    _atomic_write_json(AGY_SETTINGS, settings)
    # 用「最初备份」判断原本状态，幂等重跑不会把 tt 自己装的误报成用户原有状态栏
    try:
        with open(BACKUP_PATH, encoding="utf-8") as f:
            previous = json.load(f).get("statusLine")
    except (OSError, ValueError, json.JSONDecodeError):
        previous = None
    return {"command": command, "script": SCRIPT_PATH, "previous": previous,
            "backup": BACKUP_PATH, "theme": config.resolve_theme()}


def uninstall() -> bool:
    """还原安装前的 statusLine（原本无自定义配置 → 直接删键，回到内置默认状态栏）。"""
    try:
        settings = _read_settings()
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    sl = settings.get("statusLine")
    if not isinstance(sl, dict) or _MARKER not in str(sl.get("command", "")):
        return False  # 不是本模块装的，绝不动
    try:
        with open(BACKUP_PATH, encoding="utf-8") as f:
            backup = json.load(f)
        original = backup.get("statusLine")
    except (OSError, ValueError, json.JSONDecodeError):
        original = None
    if original is None:
        settings.pop("statusLine", None)
    else:
        settings["statusLine"] = original
    _atomic_write_json(AGY_SETTINGS, settings)
    if os.path.isfile(SCRIPT_PATH):
        os.remove(SCRIPT_PATH)
    return True


def main() -> None:
    # Windows GBK 控制台无法编码 ✓ 等符号：替换掉而不是抛错，避免安装已完成却显示为崩溃
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    args = sys.argv[1:]
    if "--uninstall" in args:
        print("已还原 agy 状态栏并移除脚本" if uninstall() else "当前 agy 状态栏不是 tt 安装的，未改动")
        return
    sl = installed_statusline()
    if "--status" in args:
        if sl is None:
            print("agy 当前未配置自定义 statusLine（使用内置默认状态栏）")
        else:
            print(json.dumps(sl, ensure_ascii=False, indent=2))
        return
    result = install()
    print(f"✓ agy 状态栏已安装（单行紧凑 / 替换默认状态栏，主题 {result['theme']}）")
    print(f"  脚本   {result['script']}")
    print(f"  命令   {result['command']}")
    print(f"  备份   {result['backup']}")
    if result["previous"] is None:
        print("  原状态栏：agy 内置默认（已备份，可用 --uninstall 还原）")
    else:
        print("  原状态栏：已有自定义 statusLine（已备份并替换，可用 --uninstall 还原）")
    print("  重启 agy CLI（或新开会话）后生效；其它 Agent 配置未改动")


if __name__ == "__main__":
    main()
