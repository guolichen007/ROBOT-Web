#!/usr/bin/env python3
"""Gitleaks 门禁自检（CI security 作业在正式扫描前运行）。

任一不满足即 exit 1：
1. 规则真实加载：.gitleaks.toml 内联 [[rules]] >= 200（官方 v8.24.2 = 206），含关键规则。
2. 负例必被检出：在隔离临时 git 仓库植入运行时拼接的已知测试凭证，gitleaks 必须非零退出
   并命中预期规则。
3. 例外不外溢：测试值作为子串拼进真实形态凭证仍被检出；allowlist 除官方结构性占位外全部
   ^...$ 整值锚定；paths 不得扩大；.gitleaksignore 只允许精确指纹。
植入值只存在于临时目录（以片段拼接），不进入仓库历史。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"
ALLOWED_PATHS = {
    r"gitleaks\.toml",
    r"(?i)\.(?:bmp|gif|jpe?g|png|svg|tiff?)$",
    r"apps/api/alembic/versions/migration-sha256\.json",
}
OFFICIAL_STRUCTURAL = {
    r"(?i)^true|false|null$",
    r"^(?i:a+|b+|c+|d+|e+|f+|g+|h+|i+|j+|k+|l+|m+|n+|o+|p+|q+|r+|s+|t+|u+|v+|w+|x+|y+|z+|\*+|\.+)$",
    r"^\$(?:\d+|{\d+})$",
}
FINGERPRINT = re.compile(r"[0-9a-f]{40}:[^:]+:[a-z0-9-]+:\d+")


def fail(msg: str) -> None:
    print(f"GITLEAKS_SELFTEST_FAIL {msg}")
    sys.exit(1)


def check_config() -> int:
    cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    rules = cfg.get("rules") or []
    if len(rules) < 200:
        fail(f"only {len(rules)} inline rules (expected >=200; official v8.24.2 = 206)")
    ids = {r["id"] for r in rules}
    for needed in ("generic-api-key", "aws-access-token", "private-key"):
        if needed not in ids:
            fail(f"rule {needed} missing")
    allow = cfg.get("allowlist") or {}
    for regex in allow.get("regexes") or []:
        if regex not in OFFICIAL_STRUCTURAL and not (regex.startswith("^") and regex.endswith("$")):
            fail(f"allowlist regex not anchored ^...$: {regex!r}")
    extra = set(allow.get("paths") or []) - ALLOWED_PATHS
    if extra:
        fail(f"allowlist paths widened: {sorted(extra)}")
    if allow.get("commits"):
        fail("allowlist.commits must stay empty (use exact fingerprints in .gitleaksignore)")
    ignore = ROOT / ".gitleaksignore"
    if ignore.is_file():
        for line in ignore.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not FINGERPRINT.fullmatch(line):
                fail(f".gitleaksignore entry is not an exact fingerprint: {line!r}")
    return len(rules)


def scan(gitleaks: str, files: dict[str, str]) -> tuple[int, list[dict], str]:
    with tempfile.TemporaryDirectory() as tmp:
        git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        for name, body in files.items():
            (Path(tmp) / name).write_text(body, encoding="utf-8")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "selftest"], check=True)
        report = Path(tmp) / ".report.json"
        proc = subprocess.run(
            [
                gitleaks,
                "git",
                f"--config={CONFIG}",
                "--no-banner",
                "--redact=100",
                "--report-format=json",
                f"--report-path={report}",
                tmp,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw_report = report.read_text() if report.is_file() else "[]"
        findings = json.loads(raw_report)
        # 端到端脱敏反证：用 CI 注解脚本处理同一报告，收集其完整输出
        rc_file = Path(tmp) / ".rc"
        rc_file.write_text(str(proc.returncode))
        annotate = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, pathlib; sys.path.insert(0, sys.argv[1]); "
                "import gitleaks_annotate as a; "
                "sys.exit(a.main(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])))",
                str(ROOT / "scripts"),
                str(report),
                str(rc_file),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        transcript = proc.stdout + proc.stderr + raw_report + annotate.stdout + annotate.stderr
        if findings and annotate.returncode != 1:
            fail(f"annotate gate returned {annotate.returncode} for findings (expected 1)")
        return proc.returncode, findings, transcript


def main() -> int:
    gitleaks = sys.argv[1] if len(sys.argv) > 1 else "gitleaks"
    n_rules = check_config()
    body = "Zq8Lm3Nx7Rt2" + "Vb6Kp9Hd4Wc1" + "Ys5Ja0Ef"
    aws = "AK" + "IA" + "Z7QK3M9XW2LPRT4V"
    planted = {
        "aws.env": f'aws_access_key_id = "{aws}"\n',
        "generic.yml": f'auth_token: "{body}"\n',
        "suffix.yml": f'auth_token: "{"ci-jwt-secret-at-least-32-characters-long" + body[:10]}"\n',
        "prefix.yml": f'auth_token: "{"e2e-jwt-secret-0123456789abcdef0123456789" + body[:10]}"\n',
    }
    expected = {
        ("aws.env", "aws-access-token"),
        ("generic.yml", "generic-api-key"),
        ("suffix.yml", "generic-api-key"),
        ("prefix.yml", "generic-api-key"),
    }
    rc, findings, transcript = scan(gitleaks, planted)
    if rc == 0:
        fail("planted credentials scanned with rc=0 (rules not loaded / gate disabled)")
    missing = expected - {(f["File"], f["RuleID"]) for f in findings}
    if missing:
        fail(f"planted credentials not detected: {sorted(missing)}")
    # 脱敏反证：植入值（及其可识别片段）不得出现在 gitleaks 日志、JSON 报告、CI 注解任何一处
    for name, value in (("aws", aws), ("generic", body)):
        for probe in (value, value[:12], value[-12:]):
            if probe in transcript:
                fail(
                    f"planted {name} credential fragment leaked into scan/report/annotation output"
                )
    for finding in findings:
        # --redact=100：Secret == "REDACTED"；Match 为脱敏后的上下文（凭证位被 REDACTED 替换）
        if finding.get("Secret") != "REDACTED":
            fail(f"report Secret not redacted ({finding.get('RuleID')})")
        if "REDACTED" not in str(finding.get("Match", "")):
            fail(f"report Match not redacted ({finding.get('RuleID')})")
    _, ok, _ = scan(
        gitleaks, {"ok.yml": 'auth_token: "ci-jwt-secret-at-least-32-characters-long"\n'}
    )
    if ok:
        fail(f"exact test value unexpectedly flagged: {ok[0].get('RuleID')}")
    print(
        f"GITLEAKS_SELFTEST=PASS rules={n_rules} planted_rc={rc} detected={len(expected)} "
        "redaction=verified(log,report,annotation)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
