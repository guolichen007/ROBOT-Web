"""WEB-R1B: no platform-side MQTT downlink may bypass the final publish gate.

Static inventory of every broker `.publish(` call in platform services (vehicle-side agents,
mocks and test tools are out of scope). Each call site must be explicitly reviewed here; a new
publisher (e.g. a `robot/{id}/scene` topic) fails this test until it is routed through
`command-dispatcher.publish_command` (and thus the gate) or reviewed into the list.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLATFORM = [
    "services/command-dispatcher",
    "services/mqtt-ingress",
    "services/task-worker",
    "services/vehicle-agent-adapter",
    "services/ros-compat-adapter",
    "services/dispatch_gate.py",
    "services/protocol.py",
    "services/vehicle_agent_ingress.py",
    "services/vehicle_agent_v1.py",
    "apps/api/app",
]
# (file, enclosing function) -> why it is allowed
REVIEWED = {
    ("services/command-dispatcher/main.py", "_publish_to_broker"): "Canonical downlink; only "
    "called from publish_command after final_publish_gate (CANONICAL) or the V1 checks",
    ("services/vehicle-agent-adapter/main.py", "publish"): "001 independent V1 path (unchanged)",
    (
        "services/ros-compat-adapter/main.py",
        "on_message",
    ): "uplink normalisation to _platform/compat",
}


def _publish_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    files: list[Path] = []
    for entry in PLATFORM:
        path = ROOT / entry
        files += [path] if path.is_file() else sorted(path.rglob("*.py"))
    for file in files:
        if "/tests/" in file.as_posix():
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"))

        rel = file.relative_to(ROOT).as_posix()

        def visit(node: ast.AST, owner: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                inner = owner
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    inner = child.name  # innermost enclosing function
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "publish"
                    and "redis" not in ast.unparse(child.func.value)
                    and not ast.unparse(child.func.value).startswith("self")
                ):
                    sites.add((rel, owner))
                visit(child, inner, rel)

        visit(tree, "<module>")
    return sites


def test_every_platform_broker_publish_is_reviewed() -> None:
    sites = _publish_sites()
    unreviewed = sorted(sites - set(REVIEWED))
    assert not unreviewed, f"unreviewed MQTT publish (route it through the gate): {unreviewed}"
    assert set(REVIEWED) <= sites, "reviewed site disappeared; update the inventory"


def test_broker_publish_only_reachable_through_publish_command() -> None:
    src = (ROOT / "services/command-dispatcher/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_publish_to_broker"
    }
    assert callers == {"publish_command"}


def test_no_scene_downlink_topic_in_platform_services() -> None:
    for entry in PLATFORM:
        path = ROOT / entry
        for file in [path] if path.is_file() else path.rglob("*.py"):
            if "/tests/" in file.as_posix():
                continue
            text = file.read_text(encoding="utf-8")
            assert '/scene"' not in text and "/scene'" not in text and "/scene_" not in text, file
