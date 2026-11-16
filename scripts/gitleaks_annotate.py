#!/usr/bin/env python3
"""解析 gitleaks 的 stdout JSON 报告并转为 workflow ::error 注解。

输入：/tmp/gitleaks.out（docker run 捕获的 stdout+stderr）与 /tmp/gitleaks.rc
（退出码）。逻辑：
  - stdout 可解析为 JSON 且含 Findings → 逐条 ::error 注解，exit 1；
  - stdout 解析失败（容器崩溃/拉取日志混入）→ 注解 RC 与日志尾部，exit 1；
  - 无 Findings → exit 0（门禁通过）。
门禁语义不变；注解随 check-run 暴露，供无管理员权限的交付方经 API 读取。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path("/tmp/gitleaks.out")
RC = Path("/tmp/gitleaks.rc")

rc_text = RC.read_text(encoding="utf-8").strip() if RC.is_file() else "unknown"
raw = OUT.read_text(encoding="utf-8", errors="replace") if OUT.is_file() else ""
log_tail = raw[-600:]


def annotate(path: str, line: int, message: str) -> None:
    clean = message.replace("\n", " ").replace("\r", " ")
    print(f"::error file={path},line={line}::{clean[:500]}")


findings = 0
# stdout 可能是纯 JSON 报告，也可能混有 docker pull 日志（从右向左找 JSON 数组）
starts = [i for i in range(len(raw)) if raw.startswith("[", i)]
for start in reversed(starts):
    try:
        report = json.loads(raw[start:])
        if isinstance(report, list):
            for finding in report:
                findings += 1
                line = int(finding.get("StartLine") or finding.get("StartColumn") or 1)
                annotate(
                    finding.get("File", "unknown"),
                    line,
                    f"{finding.get('RuleID', 'unknown')}: "
                    f"{finding.get('Description', finding.get('Secret', ''))[:200]}",
                )
            break
    except (json.JSONDecodeError, ValueError):
        continue

if findings:
    print(f"GITLEAKS_FINDINGS={findings}")
    sys.exit(1)
if rc_text == "0":
    print("GITLEAKS_FINDINGS=0")
    sys.exit(0)
annotate(".github/workflows/ci.yml", 1, f"GITLEAKS_RUNNER_FAILURE rc={rc_text}: {log_tail}")
print(f"GITLEAKS_RUNNER_FAILURE rc={rc_text}")
sys.exit(1)
