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
    "integration/ros2/ROS2对接参数模板.yaml",
    "integration/ros2/现场对接说明.md",
    # Policy/checking sources must mention the forbidden markers literally.
    "scripts/check_repository_policy.py",
    "scripts/server-preflight.ps1",
    "scripts/server-preflight.sh",
    "deploy/u-dao/scripts/02-gen-bootstrap-secrets.sh",
    "deploy/u-dao/scripts/50-public-preflight.sh",
    "deploy/u-dao/scripts/70-prepare-public-ingress.sh",
    "apps/api/app/core/config.py",
    # The handoff generator intentionally emits OWNER-supplied placeholders.
    "scripts/generate_ros2_handoff.py",
    # Contributor/test instructions document the zero-marker rule.
    "docs/开发验收/开发指南.md",
    "docs/开发验收/测试与验收.md",
}
# ---- 现场状态一致性：当前状态文档不得残留已废弃的固定运行基线 ----
CURRENT_STATE_DOCS = {
    "README.md",
    "docs/当前状态/当前状态.md",
    "docs/当前状态/approved-baseline.yaml",
    "integration/ros1/vehicle-bridge/README.md",
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

# G4-Fleet-R2-Final：依赖目录与本地依赖符号链接不得进入 Git。
# .gitignore 的 `node_modules/` 只匹配目录，同名 symlink（mode 120000）曾被 git add -A 收入。
FORBIDDEN_PATH_PARTS = {"node_modules", ".venv", "__pycache__"}
dependency_hits: list[str] = []
external_symlink_hits: list[str] = []
for entry in (
    subprocess.check_output(["git", "ls-files", "-s", "-z"], cwd=ROOT).decode().split("\0")
):
    if not entry:
        continue
    meta, rel = entry.split("\t", 1)
    mode, sha = meta.split()[:2]
    if FORBIDDEN_PATH_PARTS & set(Path(rel).parts):
        dependency_hits.append(rel)
    if mode == "120000":
        target = subprocess.check_output(["git", "cat-file", "-p", sha], cwd=ROOT).decode()
        resolved = ((ROOT / rel).parent / target).resolve()
        if target.startswith("/") or ROOT.resolve() not in (resolved, *resolved.parents):
            external_symlink_hits.append(f"{rel} -> {target}")

if (
    latest_hits
    or marker_hits
    or unpinned_actions
    or migration_metadata_hits
    or stale_state_hits
    or dependency_hits
    or external_symlink_hits
):
    raise SystemExit(
        "repository policy failed "
        f"latest={latest_hits} markers={marker_hits} unpinned={unpinned_actions} "
        f"migration_metadata={migration_metadata_hits} stale_state={stale_state_hits} "
        f"tracked_dependencies={dependency_hits} external_symlinks={external_symlink_hits}"
    )
print(
    "repository policy OK: no latest, no production markers, actions pinned, no stale state markers"
)
