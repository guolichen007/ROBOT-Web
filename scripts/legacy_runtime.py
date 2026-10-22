"""Validate and restore sealed legacy images; never interpret a manifest as shell."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SERVICES = ("api", "mqtt-ingress", "command-dispatcher", "task-worker", "web", "ros-compat-adapter")
PROJECT = "firebot-server"
REQUIRED = {
    "schema_version",
    "runtime_kind",
    "project_name",
    "source_dir",
    "compose_file",
    "env_file",
    "override_file",
    "override_sha256",
    "services",
}
OPTIONAL = {"git_provenance_sha", "compose_sha256", "env_sha256"}


class InvalidSnapshot(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidSnapshot(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate manifest key")
        result[key] = value
    return result


def read_manifest(path):
    target = Path(path)
    require(
        target.is_absolute() and target.is_file(), "manifest path must be an existing absolute file"
    )
    data = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    require(isinstance(data, dict), "manifest must be an object")
    require(REQUIRED <= data.keys() <= REQUIRED | OPTIONAL, "manifest fields differ from schema")
    require(
        type(data["schema_version"]) is int and data["schema_version"] == 1, "unsupported schema"
    )
    require(data["runtime_kind"] == "legacy_snapshot", "invalid runtime kind")
    require(data["project_name"] == PROJECT, "invalid project")
    for key in ("source_dir", "compose_file", "env_file", "override_file"):
        value = data[key]
        require(isinstance(value, str) and bool(value), "empty path")
        location = Path(value)
        require(location.is_absolute(), "relative path")
        if key == "source_dir":
            require(
                location.is_dir() and any(location.iterdir()), "missing or empty source directory"
            )
        else:
            require(location.is_file() and location.stat().st_size > 0, "missing or empty file")
    for prefix, key in (
        ("override", "override_file"),
        ("compose", "compose_file"),
        ("env", "env_file"),
    ):
        expected = data.get(f"{prefix}_sha256")
        if prefix == "override" or expected is not None:
            require(
                isinstance(expected, str) and bool(re.fullmatch(r"[0-9a-f]{64}", expected)),
                "invalid file hash",
            )
            require(
                hashlib.sha256(Path(data[key]).read_bytes()).hexdigest() == expected,
                "file hash mismatch",
            )
    provenance = data.get("git_provenance_sha", "")
    require(
        isinstance(provenance, str)
        and (not provenance or bool(re.fullmatch(r"[0-9a-f]{40}", provenance))),
        "invalid provenance",
    )
    require(
        isinstance(data["services"], dict) and set(data["services"]) == set(SERVICES),
        "invalid service set",
    )
    for item in data["services"].values():
        require(
            isinstance(item, dict) and set(item) == {"image_ref", "expected_image_id"},
            "invalid image fields",
        )
        require(
            isinstance(item["image_ref"], str)
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", item["image_ref"])),
            "invalid image ref",
        )
        require(
            isinstance(item["expected_image_id"], str)
            and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", item["expected_image_id"])),
            "invalid image ID",
        )
    return data


def command(args, cwd):
    # Compose config can contain interpolated secrets. Capture it only in memory;
    # never forward stdout/stderr or subprocess exceptions to deployment logs.
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    require(result.returncode == 0, "Docker command failed")
    return result.stdout.strip()


def compose_args(data):
    return [
        "docker",
        "compose",
        "--project-name",
        PROJECT,
        "--project-directory",
        data["source_dir"],
        "--env-file",
        data["env_file"],
        "-f",
        data["compose_file"],
        "-f",
        data["override_file"],
    ]


def validate_images_and_compose(data):
    for item in data["services"].values():
        actual = command(
            ["docker", "image", "inspect", "--format", "{{.Id}}", item["image_ref"]],
            data["source_dir"],
        )
        require(actual == item["expected_image_id"], "image ref does not match sealed image ID")
    rendered = json.loads(
        command([*compose_args(data), "config", "--format", "json"], data["source_dir"])
    )
    require(
        isinstance(rendered, dict) and rendered.get("name") == PROJECT, "rendered project mismatch"
    )
    for service, item in data["services"].items():
        require(
            rendered.get("services", {}).get(service, {}).get("image") == item["image_ref"],
            "rendered service image mismatch",
        )


def adapter_ids(data):
    return command(
        [
            "docker",
            "ps",
            "-a",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--filter",
            "label=com.docker.compose.service=vehicle-agent-adapter",
        ],
        data["source_dir"],
    ).split()


def verify(data):
    for service, item in data["services"].items():
        ids = command(
            [*compose_args(data), "ps", "--all", "-q", service], data["source_dir"]
        ).split()
        require(len(ids) == 1, "expected one legacy container per service")
        template = (
            '{"running":{{json .State.Running}},"health":{{json .State.Health.Status}},'
            '"image_id":{{json .Image}}}'
        )
        state = json.loads(
            command(["docker", "inspect", "--format", template, ids[0]], data["source_dir"])
        )
        require(
            state.get("running") is True and state.get("health") == "healthy",
            "legacy service is not running and healthy",
        )
        require(
            state.get("image_id") == item["expected_image_id"], "legacy container image ID mismatch"
        )
    require(not adapter_ids(data), "new vehicle adapter remains present")


def restore(data):
    # Remove all new v1 adapter instances before starting any legacy controller.
    ids = adapter_ids(data)
    if ids:
        command(["docker", "stop", *ids], data["source_dir"])
        command(["docker", "rm", *ids], data["source_dir"])
    require(not adapter_ids(data), "new vehicle adapter cleanup failed")
    command(
        [
            *compose_args(data),
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            "--wait",
            *SERVICES,
        ],
        data["source_dir"],
    )
    verify(data)


def main():
    action, path = sys.argv[1:]
    data = read_manifest(path)
    if action == "descriptor":
        print(
            json.dumps(
                {
                    "kind": "legacy_snapshot",
                    "manifest": str(Path(path).resolve()),
                    "git_provenance_sha": data.get("git_provenance_sha", ""),
                }
            )
        )
        return
    require(action in {"validate", "restore", "verify"}, "unknown snapshot action")
    validate_images_and_compose(data)
    if action == "restore":
        restore(data)
    elif action == "verify":
        verify(data)
    print(f"LEGACY_SNAPSHOT_{action.upper()}=PASS")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError):
        # Intentionally avoid exception text: invalid config may contain secrets.
        print(
            "LEGACY_SNAPSHOT=FAIL (manifest, image, compose or health validation)", file=sys.stderr
        )
        sys.exit(1)
