"""Exercise both real rollback callers with isolated Git/Docker command doubles."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "584efcfda60b8d39c516a9939bc0481609fc6f3c"
POST_BASELINE = "f8bb20e9d0e192f814265a0a671b3be3a077c34d"
CURRENT = "1" * 40
CORE = ["api", "mqtt-ingress", "command-dispatcher", "task-worker", "web"]
ADAPTER = "vehicle-agent-adapter"

HARNESS = r"""
set -euo pipefail
export DOCKER_CMD_SKIP_DETECT=1
if command -v cygpath >/dev/null; then TEST_PYTHON="$(cygpath -u "$TEST_PYTHON")"; fi
python3() { command "$TEST_PYTHON" "$@"; }
trace() { printf '%s\n' "$*" >> trace.log; }
docker() {
  trace "docker $*"
  case "$1" in
    compose)
      shift
      while [[ "$1" == --* || "$1" == -f ]]; do shift 2; done
      local verb="$1"; shift
      case "$verb" in
        config)
          if [[ "$*" == *--services* ]]; then
            sed -n 's/^  \([a-z-]*\):$/\1/p' docker-compose.server.yml
          else printf '{"name":"firebot-server"}\n'; fi ;;
        ps) local svc="${@: -1}"; [ ! -f "cid-$svc" ] || printf 'cid-%s\n' "$svc" ;;
        up)
          trace "image=$FIREBOT_PYTHON_IMAGE web=$FIREBOT_WEB_IMAGE"
          if [[ "$FIREBOT_PYTHON_IMAGE" == *"$CURRENT" && "$CALLER" == auto ]]; then return 1; fi
          local svc
          for svc in "$@"; do
            [[ "$svc" == -* ]] && continue
            grep -q "^  $svc:$" docker-compose.server.yml || return 1
            printf '%s\n' "$PREV" > "cid-$svc"
            if [ "$svc" = vehicle-agent-adapter ]; then printf 'cid-%s\n' "$svc" > adapters; fi
          done ;;
        *) return 1 ;;
      esac ;;
    ps)
      [[ "$*" == *com.docker.compose.project=firebot-server* ]] || return 1
      [[ "$*" == *com.docker.compose.service=vehicle-agent-adapter* ]] || return 1
      cat adapters ;;
    stop) [ "${FAIL_STOP:-0}" = 0 ] || return 1; touch adapter-stopped ;;
    rm)
      [ -f adapter-stopped ] || return 1
      [ "${FAIL_REMOVE:-0}" = 0 ] || return 1
      [ "${LEAK_REMOVE:-0}" = 0 ] || return 0
      : > adapters ;;
    inspect)
      local format="$3" cid="$4"
      [ -f "$cid" ] || return 1
      if [[ "$format" == *State.Running* ]]; then echo 'true healthy'; else cat "$cid"; fi ;;
    *) return 1 ;;
  esac
}
git() {
  trace "git $*"
  case "$1" in
    cat-file) return 0 ;;
    checkout) cp previous.yml docker-compose.server.yml; printf '%s' "$2" > head ;;
    rev-parse) cat head ;;
    *) return 1 ;;
  esac
}
source scripts/server-deploy.sh
DEPLOY_ROOT="$PWD/deployments"
CURRENT_JSON="$DEPLOY_ROOT/current.json"
v1_required() { echo true; }
init_deploy_context
require_sha() { :; }
v1_acl_gate() { :; }
current_sha() { if [ "$CALLER" = auto ]; then echo "$PREV"; else echo "$CURRENT"; fi; }
previous_sha() { echo "$PREV"; }
runtime_descriptor() { exact_runtime "$("${1}_sha")"; }
migration_revision() { trace DB_READ_ONLY; echo 20261021_0009; }
write_ssot() { trace "SSOT $*"; }
write_manifest() { trace "MANIFEST $*"; }
build_images() { trace BUILD; }
# Instrument the shared helper, retaining its actual implementation and effects.
eval "$(declare -f rollback_to | sed '1s/rollback_to/actual_rollback_to/')"
rollback_to() { trace SHARED_ROLLBACK_HELPER; actual_rollback_to "$@"; }
if [ "$CALLER" = explicit ]; then do_rollback; else deploy_scope all-app "${APP_ALL[@]}"; fi
"""


def compose(services):
    return "name: firebot-server\nservices:\n" + "".join(
        f"  {svc}:\n    image: test:{svc}\n" for svc in services
    )


class RollbackTests(unittest.TestCase):
    def run_case(
        self, caller, has_adapter, *, fail_stop=False, fail_remove=False, leak_remove=False
    ):
        previous = POST_BASELINE if has_adapter else BASELINE
        historical = subprocess.check_output(
            ["git", "show", f"{previous}:docker-compose.server.yml"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
        )
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rollback-test-", dir=artifacts) as directory:
            work = Path(directory).resolve()
            self.assertTrue(work.is_relative_to(artifacts.resolve()))
            (work / "scripts").mkdir()
            for name in (
                "server-deploy.sh",
                "docker_cmd.sh",
                "vehicle-agent-rollout.sh",
                "deployment_state.py",
                "legacy_runtime.py",
            ):
                shutil.copyfile(ROOT / "scripts" / name, work / "scripts" / name)
            (work / "docker-compose.server.yml").write_text(compose([*CORE, ADAPTER]), newline="\n")
            (work / "previous.yml").write_text(historical, encoding="utf-8", newline="\n")
            (work / "adapters").write_text("new-adapter-a\nnew-adapter-b\n", newline="\n")
            (work / "head").write_text(CURRENT)
            (work / "harness.sh").write_text(HARNESS, encoding="utf-8", newline="\n")
            bash = (
                str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
                if os.name == "nt"
                else "bash"
            )
            result = subprocess.run(
                [bash, "harness.sh"],
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={
                    **os.environ,
                    "CURRENT": CURRENT,
                    "TARGET_SHA": CURRENT,
                    "PREV": previous,
                    "CALLER": caller,
                    "TEST_PYTHON": sys.executable,
                    "FAIL_STOP": "1" if fail_stop else "0",
                    "FAIL_REMOVE": "1" if fail_remove else "0",
                    "LEAK_REMOVE": "1" if leak_remove else "0",
                },
                timeout=30,
            )
            trace = (work / "trace.log").read_text()
            self.assertEqual(trace.count("SHARED_ROLLBACK_HELPER"), 1, trace)
            if fail_stop or fail_remove or leak_remove:
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("git checkout", trace)
                self.assertNotIn("ADAPTER_POST_ROLLBACK=ABSENT", result.stdout)
                return
            self.assertEqual(result.returncode, 0 if caller == "explicit" else 1, result.stderr)
            self.assertIn(
                "ROLLBACK=PASS" if caller == "explicit" else "AUTO_ROLLBACK=PASS",
                result.stdout + result.stderr,
            )
            expected = [*CORE, *([ADAPTER] if has_adapter else [])]
            self.assertIn("ROLLBACK_SERVICE_SET=" + " ".join(expected), result.stdout)
            self.assertLess(
                trace.index("docker stop new-adapter-a new-adapter-b"), trace.index("git checkout")
            )
            old_up = [line for line in trace.splitlines() if " up " in line][-1]
            self.assertTrue(old_up.endswith(" ".join(expected)), old_up)
            for service in expected:
                self.assertEqual((work / f"cid-{service}").read_text().strip(), previous)
            if has_adapter:
                self.assertEqual((work / "adapters").read_text().strip(), f"cid-{ADAPTER}")
                self.assertIn("ADAPTER_POST_ROLLBACK=HEALTHY_PREVIOUS_SHA", result.stdout)
            else:
                self.assertEqual((work / "adapters").read_text(), "")
                self.assertIn("ADAPTER_POST_ROLLBACK=ABSENT", result.stdout)
            self.assertNotIn("downgrade", trace)
            print(f"{caller.upper()}_ROLLBACK_{'POST' if has_adapter else 'PRE'}_ADAPTER=PASS")

    def test_both_callers_use_target_compose_and_remove_new_adapter(self):
        for caller in ("explicit", "auto"):
            for has_adapter in (False, True):
                with self.subTest(caller=caller, has_adapter=has_adapter):
                    self.run_case(caller, has_adapter)

    def test_failed_adapter_cleanup_blocks_both_callers(self):
        for caller in ("explicit", "auto"):
            for failure in ("fail_stop", "fail_remove", "leak_remove"):
                with self.subTest(caller=caller, failure=failure):
                    self.run_case(caller, False, **{failure: True})

    def test_real_584efcf_baseline_is_pre_adapter(self):
        historical = subprocess.check_output(
            ["git", "show", f"{BASELINE}:docker-compose.server.yml"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
        )
        services = set(re.findall(r"^  ([a-z0-9-]+):$", historical, re.MULTILINE))
        self.assertTrue(set(CORE) <= services)
        self.assertNotIn(ADAPTER, services)
        print(f"ROLLBACK_584EFCF_SERVICE_SET=PASS sha={BASELINE}")


if __name__ == "__main__":
    unittest.main()
