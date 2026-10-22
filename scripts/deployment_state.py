"""Read old SHA-only SSOT and atomically persist explicit runtime descriptors."""

import json
import os
import re
import sys
import tempfile
from pathlib import Path


def load(path):
    if not Path(path).exists():
        return {}
    state = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("invalid deployment state")
    return state


def runtime(state, prefix):
    kind = state.get(f"{prefix}_runtime_kind", "exact_sha")
    if kind == "exact_sha":
        sha = state.get(f"{prefix}_sha", "UNKNOWN")
        if sha != "UNKNOWN" and not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("invalid exact SHA")
        return {"kind": kind, "sha": sha}
    if kind == "legacy_snapshot":
        manifest = state.get(f"{prefix}_runtime_manifest", "")
        provenance = state.get(f"{prefix}_git_provenance_sha", "")
        if not manifest or not Path(manifest).is_absolute():
            raise ValueError("legacy runtime requires an absolute manifest path")
        if provenance and not re.fullmatch(r"[0-9a-f]{40}", provenance):
            raise ValueError("invalid provenance SHA")
        return {"kind": kind, "manifest": manifest, "git_provenance_sha": provenance}
    raise ValueError("unknown runtime kind")


def write(path, current, previous):
    state = {}
    for prefix, descriptor in (("current", current), ("previous", previous)):
        state[f"{prefix}_runtime_kind"] = descriptor["kind"]
        if descriptor["kind"] == "exact_sha":
            state[f"{prefix}_sha"] = descriptor["sha"]
        elif descriptor["kind"] == "legacy_snapshot":
            state[f"{prefix}_runtime_manifest"] = descriptor["manifest"]
            state[f"{prefix}_git_provenance_sha"] = descriptor["git_provenance_sha"]
        else:
            raise ValueError("unknown runtime kind")
        runtime(state, prefix)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".current-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def main():
    command, path, *args = sys.argv[1:]
    if command == "read":
        print(json.dumps(runtime(load(path), args[0])))
    elif command == "write":
        write(path, *map(json.loads, args))
    elif command == "canary":
        # An explicit exact_sha field marks an already governed clean release.
        # The first-cutover override cannot be reused to hide that release.
        state = load(path)
        descriptor = json.loads(args[0])
        if state.get("current_runtime_kind") == "exact_sha":
            raise ValueError("first-canary override is not allowed on a clean release")
        current = runtime(state, "current")
        if current["kind"] == "legacy_snapshot":
            if current != descriptor:
                raise ValueError("first-canary manifest differs from current legacy runtime")
        elif current["sha"] != "UNKNOWN" and current["sha"] != descriptor["git_provenance_sha"]:
            raise ValueError("legacy provenance differs from old SSOT")
    else:
        raise ValueError("unknown deployment state command")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "DEPLOYMENT_STATE=FAIL (invalid runtime state or first-canary selection)",
            file=sys.stderr,
        )
        sys.exit(1)
