#!/usr/bin/env python3
"""解析 gitleaks JSON 报告并转为 workflow ::error 注解（只输出脱敏元数据）。

输入：/tmp/gitleaks.json（--report-format json --report-path，扫描已带 --redact=100）与
/tmp/gitleaks.rc（退出码）。门禁：
  - 报告含 finding → 逐条注解（仅 RuleID/File/StartLine/Commit 短 SHA/Fingerprint），exit 1；
  - 无报告或报告不可解析且 rc≠0 → 基础设施失败注解（不回显报告原文），exit 1；
  - 无 finding 且 rc=0 → exit 0。
绝不输出 Secret / Match / Line 字段，也不回显 gitleaks 原始 stdout/报告内容。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPORT = Path("/tmp/gitleaks.json")
RC = Path("/tmp/gitleaks.rc")
SAFE_FIELDS = ("RuleID", "File", "StartLine", "Commit", "Fingerprint")


def annotate(path: str, line: int, message: str) -> None:
    clean = message.replace("\n", " ").replace("\r", " ").replace("::", ": :")
    print(f"::error file={path},line={line}::{clean[:300]}")


def safe(finding: dict) -> dict:
    """只保留脱敏元数据；Commit 截短。Fingerprint = commit:file:rule:line，不含凭证。"""
    out = {k: finding.get(k) for k in SAFE_FIELDS}
    if isinstance(out.get("Commit"), str):
        out["Commit"] = out["Commit"][:12]
    return out


def main(report: Path = REPORT, rc_path: Path = RC) -> int:
    rc_text = rc_path.read_text(encoding="utf-8").strip() if rc_path.is_file() else "unknown"
    findings: list[dict] | None = None
    if report.is_file():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            if isinstance(data, list):
                findings = data
        except (json.JSONDecodeError, ValueError):
            findings = None
    if findings:
        for finding in findings:
            meta = safe(finding)
            annotate(
                str(meta["File"] or "unknown"),
                int(meta["StartLine"] or 1),
                f"gitleaks {meta['RuleID']} commit={meta['Commit']} "
                f"fingerprint={meta['Fingerprint']}",
            )
        print(f"GITLEAKS_FINDINGS={len(findings)}")
        return 1
    if findings == [] and rc_text == "0":
        print("GITLEAKS_FINDINGS=0")
        return 0
    annotate(
        ".github/workflows/ci.yml",
        1,
        f"GITLEAKS_RUNNER_FAILURE rc={rc_text} "
        f"report={'unparsable' if report.is_file() else 'missing'}",
    )
    print(f"GITLEAKS_RUNNER_FAILURE rc={rc_text}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 失败自解释，但不回显任何报告内容/traceback 局部变量
        annotate(".github/workflows/ci.yml", 1, f"GITLEAKS_ANNOTATE_CRASH: {type(exc).__name__}")
        sys.exit(2)
