"""Run real deployment callers with persistent, isolated Git/Docker command doubles."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment_state import atomic_write, probe_write

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = "584efcfda60b8d39c516a9939bc0481609fc6f3c"
A, B = "a" * 40, "b" * 40
LEGACY_SERVICES = [
    "api",
    "mqtt-ingress",
    "command-dispatcher",
    "task-worker",
    "web",
    "ros-compat-adapter",
]
APP_SERVICES = [*LEGACY_SERVICES, "vehicle-agent-adapter"]

DOUBLE = r"""
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["TEST_ROOT"])

def execute(args):
    with (root / "trace.log").open("a", encoding="utf-8") as stream:
        stream.write("docker " + " ".join(args) + "\n")
    state = json.loads((root / "docker.json").read_text())
    manifest = json.loads((root / "original-manifest.json").read_text())
    out = ""
    if args[0] == "image":
        image = args[-1]
        if os.environ.get("MISSING_IMAGE") == "1":
            return 1, ""
        out = state["images"].get(image, "")
        if os.environ.get("WRONG_IMAGE") == "1":
            out = "sha256:" + "0" * 64
    elif args[0] == "compose":
        legacy = args.count("-f") == 2
        verb = next(x for x in args if x in {"config", "up", "ps", "build"})
        services = manifest["services"] if legacy else state["current_services"]
        if verb == "config":
            if "--services" in args:
                out = "\n".join(services)
            else:
                rendered = {k: {"image": v["image_ref"]} for k, v in manifest["services"].items()}
                if os.environ.get("WRONG_RENDER") == "1":
                    rendered["api"]["image"] = "unsealed:tag"
                out = json.dumps({"name": "firebot-server", "services": rendered})
        elif verb == "build":
            if os.environ.get("FAIL_BUILD") == "1":
                return 1, ""
        elif verb == "up":
            if not legacy and os.environ.get("FAIL_RECREATE") == "1":
                return 1, ""
            selected = [x for x in args[args.index("up") + 1:] if x in state["current_services"]]
            for service in selected:
                image = (manifest["services"][service]["expected_image_id"]
                         if legacy else "sha256:" + "c" * 64)
                if legacy and os.environ.get("WRONG_CONTAINER_IMAGE") == "1":
                    image = "sha256:" + "d" * 64
                state["containers"][service] = {
                    "image_id": image,
                    "revision": ("wrong" if os.environ.get("WRONG_REVISION") == "1"
                                 else os.environ.get("FIREBOT_PYTHON_IMAGE", "").split(":")[-1]),
                    "running": os.environ.get("NOT_RUNNING") != "1" or not legacy,
                    "health": ("unhealthy" if (legacy and os.environ.get("UNHEALTHY") == "1")
                               or os.environ.get("UNHEALTHY_REFRESH") == "1"
                               else "healthy"),
                }
                if service == "vehicle-agent-adapter":
                    state["adapters"] = ["cid-vehicle-agent-adapter"]
        elif verb == "ps":
            service = args[-1]
            if service in state["containers"]:
                out = "cid-" + service
    elif args[0] == "ps":
        assert "label=com.docker.compose.project=firebot-server" in args
        assert "label=com.docker.compose.service=vehicle-agent-adapter" in args
        out = "\n".join(state["adapters"])
    elif args[0] == "stop":
        if os.environ.get("FAIL_STOP") == "1":
            return 1, ""
    elif args[0] == "rm":
        state["adapters"] = []
    elif args[0] == "inspect":
        container = state["containers"].get(args[-1].removeprefix("cid-"))
        if not container:
            return 1, ""
        template = args[2]
        if "image_id" in template:
            out = json.dumps(container)
        elif "State.Running" in template:
            out = str(container["running"]).lower() + " " + container["health"]
        elif "org.opencontainers.image.revision" in template:
            out = container["revision"]
        elif template == "{{.Image}}":
            out = container["image_id"]
        elif "StartedAt" in template:
            out = "2026-10-22T00:00:00Z"
        else:
            return 1, ""
    else:
        return 1, ""
    (root / "docker.json").write_text(json.dumps(state))
    return 0, out

if __name__ == "__main__":
    code, out = execute(sys.argv[1:])
    print(out)
    sys.exit(code)
"""

WRAPPER = r"""
import importlib.util
import subprocess
import sys
from docker_double import execute

spec = importlib.util.spec_from_file_location("legacy_runtime", "scripts/legacy_runtime.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def fake_run(args, **kwargs):
    assert args[0] == "docker"
    code, stdout = execute(args[1:])
    return subprocess.CompletedProcess(args, code, stdout, "")

module.subprocess.run = fake_run
sys.argv = ["scripts/legacy_runtime.py", *sys.argv[1:]]
try:
    module.main()
except (OSError, ValueError, KeyError, TypeError):
    print("LEGACY_SNAPSHOT=FAIL", file=sys.stderr)
    sys.exit(1)
"""

MANIFEST_WRAPPER = r"""
import subprocess
import sys
from docker_double import execute

def fake_output(args, **kwargs):
    if args[0] == "git":
        return "feature/vehicle-agent-hmi-v1"
    assert args[0] == "docker"
    code, output = execute(args[1:])
    if code:
        raise subprocess.CalledProcessError(code, args)
    return output

subprocess.check_output = fake_output
exec(compile(sys.stdin.read(), "manifest-generator", "exec"))
"""

HARNESS = r"""
set -euo pipefail
if command -v cygpath >/dev/null; then TEST_PYTHON="$(cygpath -u "$TEST_PYTHON")"; fi
python3() {
  if [ "${1:-}" = scripts/legacy_runtime.py ]; then
    shift; command "$TEST_PYTHON" legacy_wrapper.py "$@"
  elif [ "${1:-}" = - ] && [ "${USE_REAL_MANIFEST:-0}" = 1 ]; then
    shift; command "$TEST_PYTHON" manifest_wrapper.py "$@"
  else command "$TEST_PYTHON" "$@"; fi
}
trace() { printf '%s\n' "$*" >> trace.log; }
docker() { command "$TEST_PYTHON" docker_double.py "$@"; }
git() {
  trace "git $*"
  case "$1" in
    rev-parse) cat head ;;
    status|cat-file) : ;;
    checkout)
      printf '%s' "$2" > head
      if [ "${HISTORICAL_CHECKOUT:-0}" = 1 ]; then rm scripts/deployment_state.py; fi ;;
    *) return 1 ;;
  esac
}
source scripts/server-deploy.sh
CURRENT_JSON="$TEST_CURRENT_JSON"
DEPLOY_ROOT="$TEST_DEPLOY_ROOT"
v1_required() { echo true; }
init_deploy_context
v1_acl_gate() { trace ACL_READ_ONLY; }
migration_revision() { trace DB_READ_ONLY; echo 20261021_0009; }
eval "$(declare -f write_manifest | sed '1s/write_manifest/actual_write_manifest/')"
write_manifest() {
  trace "MANIFEST $*"
  [ "${FAIL_MANIFEST:-0}" = 0 ] || return 1
  if [ "${USE_REAL_MANIFEST:-0}" = 1 ]; then actual_write_manifest "$@"; fi
}
eval "$(declare -f deployment_state | sed '1s/deployment_state/actual_deployment_state/')"
deployment_state() {
  if [ "$1" = write-json ] && [ "${FAIL_MANIFEST_ATOMIC:-0}" = 1 ]; then return 1; fi
  if [ "$1" = write ]; then
    local attempt=0
    [ ! -f ssot-attempt ] || attempt="$(cat ssot-attempt)"
    attempt=$((attempt + 1)); printf '%s' "$attempt" > ssot-attempt
    trace "SSOT_ATTEMPT $attempt"
    if [ "${FAIL_SSOT:-}" = always ] || { [ "${FAIL_SSOT:-}" = once ] && [ "$attempt" = 1 ]; }; then
      return 1
    fi
  fi
  actual_deployment_state "$@"
}
release_b() {
  unset FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST
  TARGET_SHA="$SHA_B"; PY_IMAGE="firebot-python:$TARGET_SHA"; WEB_IMAGE="firebot-web:$TARGET_SHA"
  printf '%s' "$TARGET_SHA" > head
}
"""


class CutoverTests(unittest.TestCase):
    def setUp(self):
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="cutover-test-", dir=artifacts)
        self.work = Path(self.temporary.name).resolve()
        self.assertTrue(self.work.is_relative_to(artifacts.resolve()))
        (self.work / "scripts").mkdir()
        for name in (
            "server-deploy.sh",
            "vehicle-agent-rollout.sh",
            "deployment_state.py",
            "legacy_runtime.py",
        ):
            shutil.copyfile(ROOT / "scripts" / name, self.work / "scripts" / name)
        for name in ("legacy.yml", "override.yml", "legacy.env"):
            (self.work / name).write_text("# sealed fixture\n", encoding="utf-8")
        (self.work / "docker-compose.server.yml").write_text("# candidate\n")
        self.manifest = {
            "schema_version": 1,
            "runtime_kind": "legacy_snapshot",
            "project_name": "firebot-server",
            "source_dir": str(self.work),
            "compose_file": str(self.work / "legacy.yml"),
            "env_file": str(self.work / "legacy.env"),
            "override_file": str(self.work / "override.yml"),
            "override_sha256": hashlib.sha256(
                (self.work / "override.yml").read_bytes()
            ).hexdigest(),
            "git_provenance_sha": PROVENANCE,
            "services": {
                service: {
                    "image_ref": f"sealed/{service}:s5a",
                    "expected_image_id": "sha256:" + str(index) * 64,
                }
                for index, service in enumerate(LEGACY_SERVICES, 1)
            },
        }
        self.manifest_path = self.work / "manifest.json"
        self.save_manifest()
        (self.work / "original-manifest.json").write_text(json.dumps(self.manifest))
        (self.work / "current.json").write_text(
            json.dumps({"current_sha": PROVENANCE, "previous_sha": "UNKNOWN"})
        )
        (self.work / "head").write_text(A)
        (self.work / "docker.json").write_text(
            json.dumps(
                {
                    "images": {
                        item["image_ref"]: item["expected_image_id"]
                        for item in self.manifest["services"].values()
                    },
                    "current_services": APP_SERVICES,
                    "containers": {},
                    "adapters": ["new-adapter-a", "new-adapter-b"],
                }
            )
        )
        for name, content in (
            ("docker_double.py", DOUBLE),
            ("legacy_wrapper.py", WRAPPER),
            ("manifest_wrapper.py", MANIFEST_WRAPPER),
        ):
            (self.work / name).write_text(content, encoding="utf-8", newline="\n")

    def tearDown(self):
        self.temporary.cleanup()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def run_commands(self, commands, **env):
        (self.work / "harness.sh").write_text(HARNESS + commands, encoding="utf-8", newline="\n")
        bash = (
            str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
            if os.name == "nt"
            else "bash"
        )
        result = subprocess.run(
            [bash, "harness.sh"],
            cwd=self.work,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={
                **os.environ,
                "TEST_ROOT": str(self.work),
                "TEST_CURRENT_JSON": str(self.work / "current.json"),
                "TEST_DEPLOY_ROOT": str(self.work / "deployments"),
                "TEST_PYTHON": sys.executable,
                "TARGET_SHA": A,
                "SHA_B": B,
                "FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST": str(self.manifest_path),
                **env,
            },
        )
        self.trace = (
            (self.work / "trace.log").read_text() if (self.work / "trace.log").exists() else ""
        )
        self.output = result.stdout + result.stderr
        self.assertNotIn("downgrade", self.trace)
        self.assertNotIn(" restore", self.trace)
        self.assertNotIn("git checkout " + PROVENANCE, self.trace)
        for line in self.trace.splitlines():
            if " up " in line:
                for service in ("postgres", "redis", "mosquitto", "nginx", "mediamtx"):
                    self.assertNotIn(service, line.split())
        return result

    def state(self, name="current.json"):
        return json.loads((self.work / name).read_text())

    def assert_no_mutation(self):
        self.assertNotIn(" build ", self.trace)
        self.assertNotIn(" up ", self.trace)
        self.assertNotIn("docker stop", self.trace)
        self.assertEqual(self.state()["current_sha"], PROVENANCE)

    def test_first_canary_failed_recreate_restores_snapshot(self):
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', FAIL_RECREATE="1")
        self.assertEqual(result.returncode, 1, self.output)
        self.assertIn("AUTO_ROLLBACK_MODE=LEGACY_SNAPSHOT", self.output)
        self.assertIn("AUTO_ROLLBACK_LEGACY=PASS", self.output)
        self.assertEqual(self.state()["current_runtime_kind"], "legacy_snapshot")
        self.assertNotIn("current_sha", self.state())
        up = [line for line in self.trace.splitlines() if "--no-build" in line]
        self.assertEqual(len(up), 1)
        self.assertTrue(up[0].endswith(" ".join(LEGACY_SERVICES)))
        self.assertLess(self.trace.index("docker rm"), self.trace.index("--no-build"))
        print("FIRST_CANARY_AUTO_ROLLBACK_LEGACY=PASS")

    def test_failed_build_also_restores_snapshot(self):
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', FAIL_BUILD="1")
        self.assertEqual(result.returncode, 1, self.output)
        self.assertIn("AUTO_ROLLBACK_LEGACY=PASS", self.output)

    def assert_finalize_failure(self, result, step, kind):
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn(f"FINALIZE_{step}=FAIL", self.output)
        self.assertIn("RUNTIME_COMPENSATION=PASS", self.output)
        self.assertIn("SSOT_REPAIR=PASS", self.output)
        self.assertNotIn("DEPLOY=PASS", self.output)
        state = self.state()
        self.assertEqual(state["current_runtime_kind"], kind)
        if kind == "legacy_snapshot":
            self.assertEqual(state["current_runtime_manifest"], str(self.manifest_path))
            self.assertEqual(state["current_git_provenance_sha"], PROVENANCE)
            self.assertEqual(state["previous_sha"], "UNKNOWN")
            self.assertNotIn("git checkout", self.trace)
            expected = self.manifest["services"]
        else:
            self.assertEqual(state["current_sha"], B)
            self.assertEqual(state["previous_sha"], A)
            self.assertIn("git checkout " + B, self.trace)
            expected = {svc: {"revision": B} for svc in APP_SERVICES if svc != "ros-compat-adapter"}
        containers = json.loads((self.work / "docker.json").read_text())["containers"]
        for service, item in expected.items():
            if kind == "legacy_snapshot":
                self.assertEqual(containers[service]["image_id"], item["expected_image_id"])
            else:
                self.assertEqual(containers[service]["revision"], item["revision"])

    def test_legacy_ssot_finalize_failure_compensates_and_repairs(self):
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', FAIL_SSOT="once")
        self.assert_finalize_failure(result, "SSOT", "legacy_snapshot")
        self.assertEqual((self.work / "ssot-attempt").read_text(), "2")
        print("FINALIZE_SSOT_FAILURE_COMPENSATED=PASS FINALIZE_LEGACY_COMPENSATION=PASS")

    def test_legacy_manifest_finalize_failure_compensates_and_repairs(self):
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', FAIL_MANIFEST="1")
        self.assert_finalize_failure(result, "MANIFEST", "legacy_snapshot")
        print("FINALIZE_MANIFEST_FAILURE_COMPENSATED=PASS")

    def test_exact_ssot_finalize_failure_compensates_and_repairs(self):
        (self.work / "current.json").write_text(json.dumps({"current_sha": B, "previous_sha": A}))
        result = self.run_commands(
            'unset FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST\ndeploy_scope all-app "${APP_ALL[@]}"\n',
            FAIL_SSOT="once",
        )
        self.assert_finalize_failure(result, "SSOT", "exact_sha")
        print("FINALIZE_EXACT_SHA_COMPENSATION=PASS")

    def test_exact_manifest_finalize_failure_compensates_and_repairs(self):
        (self.work / "current.json").write_text(json.dumps({"current_sha": B, "previous_sha": A}))
        result = self.run_commands(
            'unset FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST\ndeploy_scope all-app "${APP_ALL[@]}"\n',
            FAIL_MANIFEST="1",
        )
        self.assert_finalize_failure(result, "MANIFEST", "exact_sha")

    def test_ssot_repair_failure_requires_manual_intervention(self):
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', FAIL_SSOT="always")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUNTIME_COMPENSATION=PASS", self.output)
        self.assertIn("SSOT_REPAIR=FAIL", self.output)
        self.assertIn("HIGH_PRIORITY_MANUAL_INTERVENTION=YES", self.output)
        self.assertNotIn("DEPLOY=PASS", self.output)

    def test_runtime_compensation_failure_never_repairs_ssot_to_false_old_runtime(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\n', FAIL_MANIFEST="1", FAIL_STOP="1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUNTIME_COMPENSATION=FAIL", self.output)
        self.assertIn("HIGH_PRIORITY_MANUAL_INTERVENTION=YES", self.output)
        self.assertNotIn("SSOT_REPAIR=PASS", self.output)
        self.assertEqual(self.state()["current_sha"], A)

    def test_explicit_rollback_manifest_failure_compensates_to_old_current(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\nexport FAIL_MANIFEST=1\ndo_rollback\n'
        )
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("FINALIZE_MANIFEST=FAIL ROLLBACK=FAIL", self.output)
        self.assertIn("RUNTIME_COMPENSATION=PASS", self.output)
        self.assertIn("SSOT_REPAIR=PASS", self.output)
        self.assertNotIn("ROLLBACK=PASS", self.output)
        self.assertEqual(self.state()["current_sha"], A)
        self.assertEqual(self.state()["previous_runtime_kind"], "legacy_snapshot")

    def test_explicit_rollback_ssot_failure_compensates_to_old_current(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\nrm ssot-attempt\n'
            "export FAIL_SSOT=once\ndo_rollback\n"
        )
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("FINALIZE_SSOT=FAIL ROLLBACK=FAIL", self.output)
        self.assertIn("RUNTIME_COMPENSATION=PASS", self.output)
        self.assertIn("SSOT_REPAIR=PASS", self.output)
        self.assertNotIn("ROLLBACK=PASS", self.output)
        self.assertEqual(self.state()["current_sha"], A)

    def test_writable_probe_failure_blocks_build_and_recreate(self):
        blocked = self.work / "not-a-directory"
        blocked.write_text("cannot create manifest directory below a file")
        original = (self.work / "current.json").read_bytes()
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\n', TEST_DEPLOY_ROOT=str(blocked)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FINALIZATION_PREFLIGHT=FAIL", self.output)
        self.assert_no_mutation()
        self.assertEqual((self.work / "current.json").read_bytes(), original)
        print("FINALIZATION_PREFLIGHT_FAIL_CLOSED=PASS")

    def test_probe_preserves_existing_targets_and_cleans_temporary_files(self):
        ssot = self.work / "current.json"
        manifest = self.work / "manifest.json"
        before = (ssot.read_bytes(), manifest.read_bytes())
        probe_write(str(ssot), str(manifest))
        self.assertEqual((ssot.read_bytes(), manifest.read_bytes()), before)
        self.assertEqual(list(self.work.glob(".write-probe-*")), [])

    def test_atomic_manifest_failure_leaves_valid_previous_json(self):
        target = self.work / "release-manifest.json"
        target.write_text('{"previous":true}\n')
        before = target.read_bytes()
        with patch("deployment_state.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_write(target, {"new": True})
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.work.glob(".current-*")), [])
        atomic_write(target, {"new": True})
        self.assertEqual(json.loads(target.read_text()), {"new": True})

    def test_real_manifest_pipeline_commits_complete_json(self):
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', USE_REAL_MANIFEST="1")
        self.assertEqual(result.returncode, 0, self.output)
        manifests = list((self.work / "deployments").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text())
        self.assertEqual(manifest["target_sha"], A)
        self.assertEqual(manifest["runtime_state"], self.state())
        self.assertTrue({"api", "web", "vehicle-agent-adapter"} <= manifest["containers"].keys())
        for item in manifest["containers"].values():
            self.assertTrue(item["image_id"])
        self.assertEqual(list(manifests[0].parent.glob(".current-*")), [])

    def test_real_manifest_atomic_failure_triggers_compensation(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\n',
            USE_REAL_MANIFEST="1",
            FAIL_MANIFEST_ATOMIC="1",
        )
        self.assert_finalize_failure(result, "MANIFEST", "legacy_snapshot")
        self.assertEqual(list((self.work / "deployments").glob("*/manifest.json")), [])

    def test_success_then_explicit_rollback_and_verify_use_snapshot(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\ncp "$CURRENT_JSON" first.json\n'
            "do_rollback\ndo_status\ndo_verify\n"
        )
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.state("first.json")["previous_runtime_kind"], "legacy_snapshot")
        self.assertNotIn("previous_sha", self.state("first.json"))
        state = self.state()
        self.assertEqual(state["current_git_provenance_sha"], PROVENANCE)
        self.assertEqual(state["current_runtime_manifest"], str(self.manifest_path))
        self.assertIn("RUNTIME_KIND=legacy_snapshot", self.output)
        self.assertIn("SERVER_VERIFY=PASS runtime=legacy_snapshot", self.output)
        self.assertNotIn("git checkout", self.trace)
        print("FIRST_CANARY_EXPLICIT_ROLLBACK_LEGACY=PASS")

    def test_missing_manifest_fails_before_build(self):
        self.manifest_path.unlink()
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n')
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()

    def test_tampered_override_fails_before_build(self):
        (self.work / "override.yml").write_text("# tampered\n")
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n')
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        print("LEGACY_MANIFEST_TAMPER_FAIL_CLOSED=PASS")

    def test_image_ref_id_mismatch_fails_before_build(self):
        result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', WRONG_IMAGE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        print("LEGACY_IMAGE_ID_MISMATCH_FAIL_CLOSED=PASS")

    def test_missing_image_and_render_mismatch_fail_closed(self):
        for fault in ("MISSING_IMAGE", "WRONG_RENDER"):
            with self.subTest(fault=fault):
                result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n', **{fault: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_mutation()

    def test_unhealthy_not_running_and_container_image_mismatch_fail_rollback(self):
        for fault in ("UNHEALTHY", "NOT_RUNNING", "WRONG_CONTAINER_IMAGE", "FAIL_STOP"):
            with self.subTest(fault=fault):
                docker_state = json.loads((self.work / "docker.json").read_text())
                docker_state["adapters"] = ["new-adapter-a", "new-adapter-b"]
                (self.work / "docker.json").write_text(json.dumps(docker_state))
                result = self.run_commands(
                    'deploy_scope all-app "${APP_ALL[@]}"\n', FAIL_RECREATE="1", **{fault: "1"}
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("AUTO_ROLLBACK_LEGACY=FAIL", self.output)
                self.assertNotIn("AUTO_ROLLBACK_LEGACY=PASS", self.output)
                self.assertEqual(self.state()["current_sha"], PROVENANCE)
        print("LEGACY_UNHEALTHY_FAIL_CLOSED=PASS")

    def test_manifest_schema_and_paths_fail_closed(self):
        mutations = [
            {"schema_version": 2},
            {"schema_version": True},
            {"project_name": "other"},
            {"source_dir": "."},
            {"env_file": ""},
            {"compose_file": str(self.work / "absent.yml")},
            {"password": "must-not-be-accepted"},
            {"services": {}},
            {"override_sha256": None},
        ]
        original = self.manifest.copy()
        for change in mutations:
            with self.subTest(change=change):
                self.manifest = {**original, **change}
                self.save_manifest()
                result = self.run_commands('deploy_scope all-app "${APP_ALL[@]}"\n')
                self.assertNotEqual(result.returncode, 0)
                self.assert_no_mutation()

    def test_second_clean_release_restores_exact_sha_rollback(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\ncp "$CURRENT_JSON" first.json\nrelease_b\n'
            'deploy_scope all-app "${APP_ALL[@]}"\ncp "$CURRENT_JSON" second.json\ndo_rollback\n'
        )
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.state("first.json")["previous_runtime_kind"], "legacy_snapshot")
        second = self.state("second.json")
        self.assertEqual(second["previous_runtime_kind"], "exact_sha")
        self.assertEqual(second["previous_sha"], A)
        self.assertNotIn("previous_runtime_manifest", second)
        self.assertEqual(self.state()["current_sha"], A)
        self.assertIn("git checkout " + A, self.trace)
        self.assertNotIn("--no-build", self.trace)
        print("NEXT_CLEAN_RELEASE_RESTORES_SHA_ROLLBACK=PASS")

    def test_refresh_same_sha_preserves_ssot_bytes_and_only_recreates_ros(self):
        # Deliberately preserve unusual formatting and unrelated audit metadata.
        original = (
            '{ "current_sha": "' + A + '", "previous_sha": "' + B + '",\n'
            '  "current_runtime_kind": "exact_sha", "previous_runtime_kind": "exact_sha",'
            ' "audit_marker": "preserve-me" }\n'
        ).encode()
        (self.work / "current.json").write_bytes(original)
        result = self.run_commands("do_refresh_ros_compat\n")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual((self.work / "current.json").read_bytes(), original)
        self.assertIn("ROS_COMPAT_REFRESH=PASS", self.output)
        self.assertIn("SSOT_CHANGED=NO", self.output)
        mutations = [
            line for line in self.trace.splitlines() if " build " in line or " up " in line
        ]
        self.assertEqual(len(mutations), 2, self.trace)
        for line in mutations:
            self.assertTrue(line.endswith(" ros-compat-adapter"), line)
        self.assertNotIn("ACL_READ_ONLY", self.trace)
        self.assertNotIn("DB_READ_ONLY", self.trace)
        self.assertNotIn("MANIFEST", self.trace)
        self.assertNotIn("docker stop", self.trace)
        print("ROS_COMPAT_REFRESH=PASS ROS_COMPAT_REFRESH_SSOT_CHANGED=NO")

    def test_refresh_rejects_mismatched_sha_and_legacy_before_build(self):
        states = [
            {"current_runtime_kind": "exact_sha", "current_sha": B},
            {
                "current_runtime_kind": "legacy_snapshot",
                "current_runtime_manifest": str(self.manifest_path),
                "current_git_provenance_sha": A,
            },
        ]
        for state in states:
            with self.subTest(state=state):
                original = json.dumps(state).encode()
                (self.work / "current.json").write_bytes(original)
                result = self.run_commands("do_refresh_ros_compat\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.work / "current.json").read_bytes(), original)
                self.assertNotIn(" build ", self.trace)
                self.assertNotIn(" up ", self.trace)

    def test_refresh_failures_do_not_write_ssot_or_report_success(self):
        original = json.dumps(
            {"current_runtime_kind": "exact_sha", "current_sha": A, "previous_sha": B}
        ).encode()
        (self.work / "current.json").write_bytes(original)
        for fault in ("FAIL_BUILD", "FAIL_RECREATE", "WRONG_REVISION", "UNHEALTHY_REFRESH"):
            with self.subTest(fault=fault):
                result = self.run_commands("do_refresh_ros_compat\n", **{fault: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("ROS_COMPAT_REFRESH=PASS", self.output)
                self.assertEqual((self.work / "current.json").read_bytes(), original)

    def test_first_canary_override_cannot_be_reused_on_clean_release(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\ndeploy_scope all-app "${APP_ALL[@]}"\n'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.trace.count(" build "), 1)
        self.assertEqual(self.state()["current_sha"], A)

    def test_historical_checkout_without_new_helper_still_writes_ssot(self):
        (self.work / "current.json").write_text(json.dumps({"current_sha": B, "previous_sha": A}))
        result = self.run_commands("do_rollback\n", HISTORICAL_CHECKOUT="1")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertFalse((self.work / "scripts/deployment_state.py").exists())
        self.assertEqual(self.state()["current_sha"], A)

    def test_explicit_restore_failure_keeps_ssot_and_never_falls_back_to_git(self):
        result = self.run_commands(
            'deploy_scope all-app "${APP_ALL[@]}"\ncp "$CURRENT_JSON" before-rollback.json\n'
            "export UNHEALTHY=1\ndo_rollback\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ROLLBACK=FAIL", self.output)
        self.assertNotIn("ROLLBACK=PASS", self.output)
        self.assertEqual(
            (self.work / "current.json").read_bytes(),
            (self.work / "before-rollback.json").read_bytes(),
        )
        self.assertNotIn("git checkout", self.trace)

    def test_first_s5b_sop_requires_one_all_app_after_acl_and_migrate(self):
        sop = (ROOT / "docs/部署运维/Vehicle-Agent-V1首次上线与回滚SOP.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("ACL → migrate → all-app", sop)
        self.assertIn("禁止 adapter-first", sop)
        self.assertNotIn("scripts/server-deploy.sh adapter", sop)
        self.assertIn(
            "FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST=/opt/firebot/legacy-s5a/manifest.json", sop
        )
        self.assertIn("scripts/server-deploy.sh all-app", sop)

    def test_legacy_cutover_requires_explicit_manifest_and_all_app(self):
        result = self.run_commands("deploy_scope adapter vehicle-agent-adapter\n")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        (self.work / "current.json").write_text(
            json.dumps(
                {
                    "current_runtime_kind": "legacy_snapshot",
                    "current_runtime_manifest": str(self.manifest_path),
                    "current_git_provenance_sha": PROVENANCE,
                }
            )
        )
        result = self.run_commands(
            'unset FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST\ndeploy_scope all-app "${APP_ALL[@]}"\n'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(" build ", self.trace)


if __name__ == "__main__":
    unittest.main()
