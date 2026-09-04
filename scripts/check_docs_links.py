from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
docs = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", ROOT / "CHANGELOG.md"]
docs.extend((ROOT / "docs").rglob("*.md"))
docs.extend(
    (ROOT / "integration/ros2").glob("*.md") if (ROOT / "integration/ros2").exists() else []
)
docs.extend((ROOT / "integration" / "ros1").rglob("*.md"))
broken: list[str] = []
pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
for document in docs:
    if not document.exists():
        continue
    text = document.read_text(encoding="utf-8")
    for target in pattern.findall(text):
        target = target.strip().strip("<>").split("#", 1)[0]
        if not target or re.match(r"^(?:https?://|mailto:)", target):
            continue
        resolved = (document.parent / unquote(target)).resolve()
        if not resolved.exists():
            broken.append(f"{document.relative_to(ROOT)} -> {target}")
if broken:
    raise SystemExit("broken documentation links:\n" + "\n".join(broken))
print(f"documentation links OK: {len(docs)} files")
