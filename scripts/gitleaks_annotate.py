#!/usr/bin/env python3
"""把 /repo/gitleaks.sarif 的发现转成 workflow ::error 注解；有发现即退出 1。

门禁语义不变（有发现=失败）；额外收益：注解随 check-run 暴露，交付方可经
GitHub API 读取精确 file:line（不降级、不加忽略）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SARIF = Path("/repo/gitleaks.sarif")
if not SARIF.is_file():
    print("GITLEAKS_NO_REPORT")
    sys.exit(1)
report = json.loads(SARIF.read_text(encoding="utf-8"))
findings = 0
for run in report.get("runs", []):
    for result in run.get("results", []):
        findings += 1
        loc = result["locations"][0]["physicalLocation"]
        uri = loc["artifactLocation"]["uri"].replace("file:///repo/", "")
        line = loc["region"].get("startLine", 1)
        print(
            f"::error file={uri},line={line}::"
            f"{result.get('ruleId', 'unknown')}: {result['message']['text'][:200]}"
        )
print(f"GITLEAKS_FINDINGS={findings}")
sys.exit(1 if findings else 0)
