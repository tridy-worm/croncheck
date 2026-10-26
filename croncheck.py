"""croncheck —— 只读检查定时任务状态。

设计原则：
1. 只读不触发：查状态的动作不能有副作用。
2. 「没执行过」和「执行了但失败」必须分开报。
3. 每个结论后面附证据：时间戳、退出码、日志位置。

仅依赖 Python 标准库。

用法:
    python croncheck.py --help
    python croncheck.py --crontab ./crontab.txt
    python croncheck.py --log-dir ./logs
    python croncheck.py --crontab ./crontab.txt --log-dir ./logs --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# cron 表达式：分 时 日 月 周
CRON_FIELD_RE = re.compile(
    r"^(?P<minute>\S+)\s+(?P<hour>\S+)\s+(?P<dom>\S+)\s+(?P<month>\S+)\s+(?P<dow>\S+)\s+(?P<command>.+)$"
)

# 执行记录的行格式（兼容常见几种）
LOG_LINE_RE = re.compile(
    r"^(?P<ts>[\d\-/:,\. T]+)\s+(?:\[(?P<pid>\d+)\]\s+)?"
    r"(?:.*?(?P<status>OK|FAILED|FAIL|SUCCESS|ERROR|EXIT|exit code)\b.*?)?"
)

# 常见的状态标记
FAIL_MARKERS = ("FAILED", "FAIL", "ERROR", "非零退出", "exit code: 1")
OK_MARKERS = ("OK", "SUCCESS", "完成", "success")


class TaskState:
    """单个定时任务的状态。"""

    NOT_RUN = "not_run"                  # 没执行过
    FAILED = "failed"                    # 执行了但失败
    SLOW = "slow"                        # 执行了但耗时异常
    OK = "ok"                            # 执行了且正常

    LABEL = {
        NOT_RUN: "未执行过",
        FAILED: "执行失败",
        SLOW: "耗时异常",
        OK: "执行正常",
    }

    def __init__(self, schedule: str, command: str, source: str, line: int) -> None:
        self.schedule = schedule
        self.command = command
        self.source = source
        self.line = line
        self.state = TaskState.NOT_RUN
        self.evidence: list[str] = []
        self.last_run: str = ""
        self.exit_code: int | None = None
        self.duration: float | None = None

    def as_dict(self) -> dict:
        return {
            "schedule": self.schedule,
            "command": self.command,
            "source": self.source,
            "line": self.line,
            "state": self.state,
            "state_label": TaskState.LABEL.get(self.state, self.state),
            "evidence": self.evidence,
            "last_run": self.last_run,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration,
        }


def parse_crontab(text: str, source: str) -> list[TaskState]:
    """解析 crontab 文本，抽出任务定义。

    只读文件内容，不与 cron 服务交互，不触发任何任务。
    """
    tasks: list[TaskState] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 环境变量行（KEY=VALUE）
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", line):
            continue
        # @reboot / @daily 这类宏
        macro = re.match(r"^@(\w+)\s+(.+)$", line)
        if macro:
            tasks.append(TaskState(f"@{macro.group(1)}", macro.group(2),
                                    source, number))
            continue
        m = CRON_FIELD_RE.match(line)
        if not m:
            continue
        fields = [m.group(k) for k in
                  ("minute", "hour", "dom", "month", "dow")]
        if not all(_valid_field(f) for f in fields):
            continue
        schedule = " ".join(fields)
        tasks.append(TaskState(schedule, m.group("command").strip(), source, number))
    return tasks


_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun",
                "jul", "aug", "sep", "oct", "nov", "dec")
_DOW_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FIELD_CHARS = set("0123456789*/,-?#")


def _valid_field(field: str) -> bool:
    """判断一个 cron 字段是否看起来合法。

    纯数字与运算符直接放行；含字母时，必须完全由月份/星期名称
    （或它们的区间，如 ``mon-fri``）构成。
    这样「this is not a cron line」这类普通句子不会被误判成任务。
    """
    if not field:
        return False
    lowered = field.lower()
    if lowered in _MONTH_NAMES or lowered in _DOW_NAMES:
        return True
    if any(ch.isalpha() for ch in lowered):
        for part in lowered.split(","):
            piece = part.strip()
            if not piece:
                return False
            bounds = piece.split("-")
            if len(bounds) > 2:
                return False
            for bound in bounds:
                if bound not in _MONTH_NAMES and bound not in _DOW_NAMES:
                    return False
        return True
    for ch in lowered:
        if ch not in _FIELD_CHARS:
            return False
    return True


def parse_duration(text: str) -> float | None:
    """从日志里解析耗时。"""
    m = re.search(r"(?:耗时|duration|elapsed|用时)\s*[:：]?\s*([\d.]+)\s*"
                  r"(ms|s|秒|milliseconds)?", text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("ms", "milliseconds"):
        return value / 1000
    return value


def scan_logs(log_dir: Path, tasks: list[TaskState]) -> dict:
    """在日志目录里找每个任务的执行记录。

    匹配依据：日志文件名或内容里出现的任务命令关键词。
    """
    findings: dict[int, list[dict]] = {}
    if not log_dir.is_dir():
        return findings

    log_files = [p for p in sorted(log_dir.iterdir())
                 if p.is_file() and p.suffix in (".log", ".out", ".txt", "")]

    for path in log_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for index, task in enumerate(tasks):
            keywords = _keywords_of(task.command)
            if not keywords or not any(k in text for k in keywords):
                continue
            record = _analyze_log_text(text, path)
            findings.setdefault(index, []).append(record)
    return findings


def _keywords_of(command: str) -> list[str]:
    """从命令里提取用于匹配日志的关键词。

    用 basename（带扩展名）优先，同时接受去掉扩展名的短形式，
    以便匹配到只写了任务名、没写后缀的日志。
    """
    tokens = command.split()
    base = ""
    for token in tokens:
        if token.startswith("/") and len(token) > 1:
            base = os.path.basename(token)
            break
    if not base and tokens:
        base = os.path.basename(tokens[0])
    if not base:
        return []
    keywords = [base]
    stem = os.path.splitext(base)[0]
    if stem and stem != base and len(stem) >= 3:
        keywords.append(stem)
    return keywords


def _analyze_log_text(text: str, path: Path) -> dict:
    """从一份日志里读出状态、退出码、耗时。"""
    state = TaskState.NOT_RUN
    exit_code = None
    duration = None
    last_run = ""
    evidence: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = LOG_LINE_RE.match(stripped)
        if m and m.group("ts"):
            last_run = m.group("ts").strip()
        upper = stripped.upper()
        if any(marker in upper for marker in FAIL_MARKERS):
            state = TaskState.FAILED
            evidence.append(f"{path.name}: {stripped[:120]}")
            ec = re.search(r"exit(?:\s+code)?\D{0,3}(\d+)", stripped, re.IGNORECASE)
            if ec:
                exit_code = int(ec.group(1))
        elif any(marker in upper for marker in OK_MARKERS):
            if state != TaskState.FAILED:
                state = TaskState.OK
                evidence.append(f"{path.name}: {stripped[:120]}")
        d = parse_duration(stripped)
        if d is not None:
            duration = d

    return {"file": str(path), "state": state, "exit_code": exit_code,
            "duration": duration, "last_run": last_run, "evidence": evidence[-5:]}


def apply_findings(tasks: list[TaskState], findings: dict,
                   slow_threshold: float) -> None:
    """把日志里的发现写回任务状态。"""
    for index, records in findings.items():
        if index >= len(tasks):
            continue
        task = tasks[index]
        # 只要有一条失败记录，就判定为失败
        failed = [r for r in records if r["state"] == TaskState.FAILED]
        if failed:
            task.state = TaskState.FAILED
            task.evidence = failed[-1]["evidence"]
            task.exit_code = failed[-1]["exit_code"]
            task.duration = failed[-1]["duration"]
            task.last_run = failed[-1]["last_run"]
            continue
        ok_records = [r for r in records if r["state"] == TaskState.OK]
        if ok_records:
            task.state = TaskState.OK
            task.evidence = ok_records[-1]["evidence"]
            task.duration = ok_records[-1]["duration"]
            task.last_run = ok_records[-1]["last_run"]
            if task.duration is not None and task.duration > slow_threshold:
                task.state = TaskState.SLOW
                task.evidence.append(
                    f"耗时 {task.duration:.1f}s 超过阈值 {slow_threshold}s")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="croncheck",
        description="只读检查定时任务状态，区分未执行、执行失败与耗时异常（仅依赖标准库）",
        epilog=(
            "示例:\n"
            "  python croncheck.py --crontab ./crontab.txt\n"
            "  python croncheck.py --crontab ./crontab.txt --log-dir ./logs\n"
            "  python croncheck.py --crontab ./crontab.txt --log-dir ./logs --slow 60 --json\n"
            "\n"
            "本工具只读取文件，不会触发任何任务，也不与 cron 服务交互。\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--crontab", help="crontab 文件路径")
    ap.add_argument("--log-dir", help="执行日志目录")
    ap.add_argument("--slow", type=float, default=60.0,
                    help="耗时异常阈值（秒，默认 60）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if not args.crontab:
        print("请用 --crontab 指定 crontab 文件路径。", file=sys.stderr)
        return 2

    crontab_path = Path(args.crontab)
    if not crontab_path.is_file():
        print(f"找不到 crontab 文件：{crontab_path}", file=sys.stderr)
        return 2
    try:
        text = crontab_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"读取失败：{e}", file=sys.stderr)
        return 2

    tasks = parse_crontab(text, str(crontab_path))
    if not tasks:
        print("crontab 里没有解析到任何任务定义。")
        return 0

    findings: dict[int, list[dict]] = {}
    if args.log_dir:
        findings = scan_logs(Path(args.log_dir), tasks)
        apply_findings(tasks, findings, args.slow)

    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.state] = counts.get(task.state, 0) + 1

    report = {
        "crontab": str(crontab_path),
        "log_dir": args.log_dir or "",
        "slow_threshold": args.slow,
        "task_count": len(tasks),
        "counts": {TaskState.LABEL.get(k, k): v for k, v in counts.items()},
        "tasks": [t.as_dict() for t in tasks],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"crontab：{crontab_path}")
    if args.log_dir:
        print(f"日志目录：{args.log_dir}")
    else:
        print("日志目录：（未指定，所有任务只能判定为「未执行过」）")
    print(f"任务数：{len(tasks)}")
    print()
    for label, count in report["counts"].items():
        print(f"  {label}：{count}")
    print()

    for index, task in enumerate(tasks, 1):
        print(f"[{index}] {TaskState.LABEL.get(task.state, task.state)}"
              f"  ({task.schedule})  {task.command}")
        print(f"    来源：{task.source}:{task.line}")
        if task.last_run:
            print(f"    最后执行：{task.last_run}")
        if task.exit_code is not None:
            print(f"    退出码：{task.exit_code}")
        if task.duration is not None:
            print(f"    耗时：{task.duration:.3f}s")
        for item in task.evidence:
            print(f"    证据：{item}")
        if not args.log_dir:
            print("    证据：（没有日志目录，无法判断执行情况）")
        print()

    print("说明：")
    print("  「未执行过」与「执行失败」是两回事——修法完全不同：")
    print("    未执行过 → 查调度是否生效、时区是否对得上；")
    print("    执行失败 → 查命令本身、依赖、权限与退出码。")
    print("  本工具只读文件，从不触发任务，也不与 cron 服务交互。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
