"""croncheck 的自检测试。用独立临时文件，不触发任何真实任务。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from croncheck import (TaskState, apply_findings, parse_crontab,  # noqa: E402
                       parse_duration, scan_logs)

ENTRY = os.path.join(BASE, "croncheck.py")


def _tmpdir() -> str:
    return tempfile.mkdtemp(prefix="croncheck_")


def test_parse_crontab_basic() -> None:
    text = "0 2 * * * /usr/bin/backup.sh\n30 3 * * 1 /usr/bin/weekly.sh\n"
    tasks = parse_crontab(text, "test")
    assert len(tasks) == 2, tasks
    assert tasks[0].schedule == "0 2 * * *", tasks[0]
    assert tasks[0].command == "/usr/bin/backup.sh", tasks[0]


def test_parse_crontab_skips_comments_and_env() -> None:
    text = "# 注释\nPATH=/usr/bin\n0 2 * * * /bin/backup.sh\n"
    tasks = parse_crontab(text, "test")
    assert len(tasks) == 1, tasks


def test_parse_crontab_handles_macros() -> None:
    tasks = parse_crontab("@reboot /bin/start.sh\n@daily /bin/daily.sh\n", "test")
    assert len(tasks) == 2, tasks
    assert tasks[0].schedule == "@reboot", tasks[0]


def test_parse_crontab_skips_invalid_lines() -> None:
    tasks = parse_crontab("this is not a cron line\n0 2 * * * /bin/x.sh\n", "test")
    assert len(tasks) == 1, tasks


def test_parse_duration_variants() -> None:
    assert parse_duration("耗时: 1.5s") == 1.5
    assert parse_duration("duration 250ms") == 0.25
    assert parse_duration("elapsed: 3") == 3.0
    assert parse_duration("没有耗时信息") is None


def test_scan_logs_marks_failure() -> None:
    tmp = _tmpdir()
    try:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "backup.log").write_text(
            "2026-10-26 02:00:01 backup started\n"
            "2026-10-26 02:00:05 backup FAILED exit code 1\n",
            encoding="utf-8")
        tasks = parse_crontab("0 2 * * * /usr/bin/backup.sh\n", "test")
        findings = scan_logs(log_dir, tasks)
        assert 0 in findings, findings
        apply_findings(tasks, findings, 60.0)
        assert tasks[0].state == TaskState.FAILED, tasks[0].as_dict()
        assert tasks[0].exit_code == 1, tasks[0].as_dict()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_logs_marks_ok() -> None:
    tmp = _tmpdir()
    try:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "backup.log").write_text(
            "2026-10-26 02:00:01 backup started\n"
            "2026-10-26 02:00:05 backup OK\n",
            encoding="utf-8")
        tasks = parse_crontab("0 2 * * * /usr/bin/backup.sh\n", "test")
        findings = scan_logs(log_dir, tasks)
        apply_findings(tasks, findings, 60.0)
        assert tasks[0].state == TaskState.OK, tasks[0].as_dict()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failure_takes_priority_over_ok() -> None:
    tmp = _tmpdir()
    try:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "backup.log").write_text(
            "2026-10-25 02:00:05 backup OK\n"
            "2026-10-26 02:00:05 backup FAILED exit code 2\n",
            encoding="utf-8")
        tasks = parse_crontab("0 2 * * * /usr/bin/backup.sh\n", "test")
        findings = scan_logs(log_dir, tasks)
        apply_findings(tasks, findings, 60.0)
        assert tasks[0].state == TaskState.FAILED, tasks[0].as_dict()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_not_run_when_no_logs() -> None:
    tasks = parse_crontab("0 2 * * * /usr/bin/backup.sh\n", "test")
    apply_findings(tasks, {}, 60.0)
    assert tasks[0].state == TaskState.NOT_RUN, tasks[0].as_dict()


def test_slow_is_distinct_state() -> None:
    tmp = _tmpdir()
    try:
        log_dir = Path(tmp) / "logs"
        log_dir.mkdir()
        (log_dir / "backup.log").write_text(
            "2026-10-26 02:00:01 backup OK 耗时: 120.5s\n",
            encoding="utf-8")
        tasks = parse_crontab("0 2 * * * /usr/bin/backup.sh\n", "test")
        findings = scan_logs(log_dir, tasks)
        apply_findings(tasks, findings, 60.0)
        assert tasks[0].state == TaskState.SLOW, tasks[0].as_dict()
        assert tasks[0].duration == 120.5, tasks[0].as_dict()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_logs_ignores_missing_dir() -> None:
    tasks = parse_crontab("0 2 * * * /usr/bin/backup.sh\n", "test")
    findings = scan_logs(Path("no_such_dir"), tasks)
    assert findings == {}, findings


def test_cli_help_exits_zero() -> None:
    r = subprocess.run([sys.executable, ENTRY, "--help"], capture_output=True,
                       text=True, timeout=30, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert "croncheck" in r.stdout


def test_cli_reports_tasks() -> None:
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "crontab.txt"
        path.write_text("0 2 * * * /usr/bin/backup.sh\n", encoding="utf-8")
        r = subprocess.run([sys.executable, ENTRY, "--crontab", str(path)],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        assert "未执行过" in r.stdout, r.stdout
        assert "backup.sh" in r.stdout, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_json_output() -> None:
    tmp = _tmpdir()
    try:
        path = Path(tmp) / "crontab.txt"
        path.write_text("0 2 * * * /usr/bin/backup.sh\n", encoding="utf-8")
        r = subprocess.run([sys.executable, ENTRY, "--crontab", str(path), "--json"],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["task_count"] == 1, data
        assert data["tasks"][0]["state"] == TaskState.NOT_RUN, data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_missing_crontab_exits_two() -> None:
    r = subprocess.run([sys.executable, ENTRY, "--crontab", "no_such_crontab"],
                       capture_output=True, text=True, timeout=30,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 2, (r.returncode, r.stderr)


def test_cli_never_triggers_tasks() -> None:
    """验证工具不会执行 crontab 里的命令。"""
    tmp = _tmpdir()
    try:
        marker = Path(tmp) / "should_not_exist.txt"
        path = Path(tmp) / "crontab.txt"
        path.write_text(f"* * * * * touch {marker}\n", encoding="utf-8")
        r = subprocess.run([sys.executable, ENTRY, "--crontab", str(path)],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stderr
        assert not marker.exists(), "crontab 里的命令被执行了"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [OK  ] {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n共 {len(tests)} 项，失败 {failed} 项")
    raise SystemExit(1 if failed else 0)
