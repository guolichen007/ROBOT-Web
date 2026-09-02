from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".vue",
    ".yml",
    ".yaml",
    ".json",
    ".md",
    ".ps1",
    ".sh",
    ".conf",
    ".toml",
    ".txt",
}


def files():
    # Policy applies to the exact commit candidate: tracked files plus
    # non-ignored untracked files. Local caches, virtualenvs, Playwright
    # artifacts and backup data must never influence a release gate.
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    ).decode("utf-8")
    for relative in output.split("\0"):
        if not relative:
            continue
        path = ROOT / relative
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            yield path


latest_hits: list[str] = []
marker_hits: list[str] = []
unpinned_actions: list[str] = []
migration_metadata_hits: list[str] = []
marker_allow = {
    "docs/archive/历史基线/CODEX_MASTER_SPEC_FIREBOT_V2_BASELINE_FINAL.md",
    "docs/archive/历史基线/CODEX_FINAL_INTEGRATION_READY_ITERATION.md",
    "docs/archive/工程报告/深度加固跟踪表.md",
    "docs/协议待确认项.md",
    "integration/ros2/ROS2对接参数模板.yaml",
    "integration/ros2/现场对接说明.md",
    # Policy/checking sources must mention the forbidden markers literally.
    "scripts/check_repository_policy.py",
    "scripts/server-preflight.ps1",
    "scripts/server-preflight.sh",
    "apps/api/app/core/config.py",
    # The handoff generator intentionally emits OWNER-supplied placeholders.
    "scripts/generate_ros2_handoff.py",
    # Contributor/release instructions document the zero-marker rule.
    "docs/开发指南.md",
    "docs/发布检查清单.md",
}
# ---- 现场状态一致性：当前状态文档不得残留已废弃的固定运行基线 ----
CURRENT_STATE_DOCS = {
    "README.md",
    "docs/现场状态/当前现场状态.md",
    "docs/现场状态/approved-baseline.yaml",
    "integration/ros1/vehicle-bridge/README.md",
    "integration/ros1/vehicle-bridge/HANDOFF/CURRENT_STATE.md",
    "integration/ros1/vehicle-bridge/HANDOFF/SAFETY.md",
    "integration/ros1/vehicle-bridge/HANDOFF/NEXT_PHASE.md",
}
DEPRECATED_RUNTIME_MARKERS = (
    "41bbaf4",
    "13c8692",
    "56e151e9",
    "675b1a6",
    "REAL_CONTROL=NOT_IMPLEMENTED",
    "/home/tl/vehicle-bridge",
)
stale_state_hits: list[str] = []

for path in files():
    relative = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    if re.search(r"(?im)^\s*(?:image:|FROM)\s+\S+:latest(?:\s|$)", text):
        latest_hits.append(relative)
    if relative not in marker_allow and re.search(r"\b(?:TODO|FIXME|HACK)\b", text):
        marker_hits.append(relative)
    if relative.startswith(".github/workflows/"):
        for line_no, line in enumerate(text.splitlines(), 1):
            match = re.search(r"uses:\s*([^\s#]+)@([^\s#]+)", line)
            if match and not re.fullmatch(r"[0-9a-f]{40}", match.group(2)):
                unpinned_actions.append(f"{relative}:{line_no}:{match.group(0)}")
    if relative.startswith("apps/api/alembic/versions/") and re.search(
        r"(?:from\s+app\.db\.models\s+import\s+Base|Base\.metadata\.(?:create_all|drop_all))",
        text,
    ):
        migration_metadata_hits.append(relative)
    if relative in CURRENT_STATE_DOCS:
        for marker in DEPRECATED_RUNTIME_MARKERS:
            if marker in text:
                stale_state_hits.append(f"{relative}:{marker}")

if latest_hits or marker_hits or unpinned_actions or migration_metadata_hits or stale_state_hits:
    raise SystemExit(
        "repository policy failed "
        f"latest={latest_hits} markers={marker_hits} unpinned={unpinned_actions} "
        f"migration_metadata={migration_metadata_hits} stale_state={stale_state_hits}"
    )
print("repository policy OK: no latest, no production markers, actions pinned, no stale state markers")
