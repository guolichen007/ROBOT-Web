from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def tracked_markdown() -> list[Path]:
    """All tracked *.md files across the whole repo (root/docs/integration/scripts/.github)."""
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
    ).decode("utf-8", errors="replace")
    return [ROOT / rel for rel in output.split("\0") if rel]


docs = tracked_markdown()
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
print(f"documentation links OK: {len(docs)} tracked markdown files")
