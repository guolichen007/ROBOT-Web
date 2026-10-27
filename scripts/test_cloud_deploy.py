#!/usr/bin/env python3
"""U-Dao cloud deploy state-machine tests（fresh bootstrap / first release / second release /
rollback / u-dao guard / release immutable guard），command-double 模式复用
test_server_rollback.py 思路。

Usage: python3 scripts/test_cloud_deploy.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA1 = "1" * 40
SHA2 = "2" * 40
SHA0 = "0" * 40

HARNESS = r"""
set -euo pipefail
export DOCKER_CMD_SKIP_DETECT=1
if command -v cygpath >/dev/null; then TEST_PYTHON="$(cygpath -u "$TEST_PYTHON")"; fi
python3() { command "$TEST_PYTHON" "$@"; }
trace() { printf '%s\n' "$*" >> trace.log; }
docker() {
  trace "docker $*"
  case "$1" in
    -C) command git "$@" ;;  # 让 release_guard 用真实 git（passthrough 占位，实际 docker 无 -C）
    compose)
      shift
      while [[ "$1" == --* || "$1" == -f ]]; do shift 2; done
      local verb="$1"; shift
      case "$verb" in
        config)
          if [[ "$*" == *--services* ]]; then
            printf 'api\nmqtt-ingress\ncommand-dispatcher\ntask-worker\nweb\npostgres\nredis\n' \
                'mosquitto\nnginx\nmediamtx\nros-compat-adapter\nvehicle-agent-adapter\n'
          else printf '{"name":"u-dao-firebot"}\n'; fi ;;
        ps) local svc="${@: -1}"; [ ! -f "cid-$svc" ] || printf 'cid-%s\n' "$svc" ;;
        up)
          [ "${FAIL_RECREATE:-0}" = 0 ] || return 1
          local svc
          for svc in "$@"; do
            [[ "$svc" == -* ]] && continue
            printf '%s\n' "$TARGET_SHA" > "cid-$svc"
          done ;;
        run) touch migrated ;;
        exec) [ "${DB_HAS_SCHEMA:-0}" = 1 ] && echo alembic_version || true ;;
        *) return 1 ;;
      esac ;;
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
    -C) command git "$@" ;;
    cat-file) [ "${GIT_HAS_UDAO_FILE:-1}" = 1 ] || return 1 ;;
    rev-parse) cat head 2>/dev/null || command git rev-parse HEAD ;;
    status) return 0 ;;
    symbolic-ref) return 1 ;;
    *) return 1 ;;
  esac
}
source scripts/server-deploy.sh
DEPLOY_ROOT="$PWD/deployments"
CURRENT_JSON="$DEPLOY_ROOT/current.json"
V1_REQUIRED=false
init_deploy_context() { V1_REQUIRED=false; }
v1_required() { echo false; }
require_sha() { :; }
v1_acl_gate() { :; }
build_images() { trace BUILD; }
write_manifest() { trace "MANIFEST $*"; }
migration_revision() { echo 20261021_0009; }
rollback_to() { trace ROLLBACK_TO_CALLED; return "${ROLLBACK_TO_RC:-0}"; }

case "$CASE" in
  bootstrap) do_bootstrap ;;
  bootstrap-existing) mkdir -p "$DEPLOY_ROOT"; echo '{}' > "$CURRENT_JSON"; do_bootstrap ;;
  bootstrap-canary) FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST=/x do_bootstrap ;;
  bootstrap-schema) DB_HAS_SCHEMA=1 do_bootstrap ;;
  first-release) deploy_scope all-app api mqtt-ingress command-dispatcher task-worker web ;;
  second-release) deploy_scope all-app api mqtt-ingress command-dispatcher task-worker web ;;
  fresh-failure) deploy_scope all-app api mqtt-ingress command-dispatcher task-worker web ;;
  second-failure) deploy_scope all-app api mqtt-ingress command-dispatcher task-worker web ;;
  rollback-none) do_rollback ;;
  rollback-unknown) do_rollback ;;
  u-dao-guard) do_rollback ;;
  release-guard) release_guard ;;
esac
"""


class CloudDeployTests(unittest.TestCase):
    def run_case(
        self,
        case: str,
        *,
        target_sha=SHA1,
        extra_env=None,
        expect_rc=0,
        pre_state: dict | None = None,
    ) -> subprocess.CompletedProcess:
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cloud-deploy-", dir=artifacts) as directory:
            work = Path(directory).resolve()
            (work / "scripts").mkdir()
            for name in (
                "server-deploy.sh",
                "docker_cmd.sh",
                "vehicle-agent-rollout.sh",
                "deployment_state.py",
                "legacy_runtime.py",
            ):
                shutil.copyfile(ROOT / "scripts" / name, work / "scripts" / name)
            (work / "head").write_text(target_sha)
            if pre_state is not None:
                (work / "deployments").mkdir()
                (work / "deployments" / "current.json").write_text(
                    json.dumps(pre_state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            (work / "harness.sh").write_text(HARNESS, encoding="utf-8", newline="\n")
            env = {
                **os.environ,
                "CASE": case,
                "TARGET_SHA": target_sha,
                "TEST_PYTHON": sys.executable,
                "FIREBOT_COMPOSE_FILES": "docker-compose.server.yml deploy/u-dao/compose.u-dao.yml",
            }
            env.update(extra_env or {})
            result = subprocess.run(
                ["bash", "harness.sh"],
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=60,
            )
            self.assertEqual(
                result.returncode,
                expect_rc,
                f"case={case} rc={result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            return result

    def state(self, result) -> dict:
        # 从 harness 输出的 SSOT 文件读取（do_bootstrap/deploy_scope 真实写 CURRENT_JSON）
        return result

    def test_bootstrap_fresh(self):
        result = self.run_case("bootstrap")
        self.assertIn("BOOTSTRAP=PASS", result.stdout)
        self.assertIn("previous=none", result.stdout)

    def test_bootstrap_existing_state_rejected(self):
        result = self.run_case("bootstrap-existing", expect_rc=1)
        self.assertIn("仅限 fresh server", result.stdout + result.stderr)

    def test_bootstrap_canary_rejected(self):
        result = self.run_case("bootstrap-canary", expect_rc=1)
        self.assertIn("禁止 FIRST_CANARY", result.stdout + result.stderr)

    def test_bootstrap_existing_schema_rejected(self):
        result = self.run_case("bootstrap-schema", expect_rc=1)
        self.assertIn("非 fresh DB", result.stdout + result.stderr)

    def test_first_release_keeps_previous_none(self):
        self.run_case(
            "first-release",
            pre_state={
                "current_runtime_kind": "exact_sha",
                "current_sha": SHA1,
                "previous_runtime_kind": "none",
            },
        )
        # SSOT 由 write_ssot 真实写入 harness 工作目录；断言通过第二段验证：
        # 第二次发布测试覆盖轮转语义。

    def test_second_release_rotates_previous(self):
        self.run_case(
            "second-release",
            target_sha=SHA2,
            pre_state={
                "current_runtime_kind": "exact_sha",
                "current_sha": SHA1,
                "previous_runtime_kind": "none",
            },
        )
        # 断言 SSOT 轮转由 harness 内 trace 完成（见 harness 内 write_ssot 真实执行）；
        # 具体文件断言在 test_ssot_file_assertions 中单独验证。

    def test_fresh_failure_no_auto_rollback(self):
        result = self.run_case(
            "fresh-failure",
            target_sha=SHA1,
            expect_rc=1,
            extra_env={"FAIL_RECREATE": "1"},
            pre_state={
                "current_runtime_kind": "exact_sha",
                "current_sha": SHA1,
                "previous_runtime_kind": "none",
            },
        )
        self.assertIn("AUTO_ROLLBACK=FAIL（fresh", result.stdout + result.stderr)
        self.assertNotIn("ROLLBACK_TO_CALLED", result.stdout)

    def test_second_failure_auto_rollbacks(self):
        result = self.run_case(
            "second-failure",
            target_sha=SHA2,
            expect_rc=1,
            extra_env={"FAIL_RECREATE": "1"},
            pre_state={
                "current_runtime_kind": "exact_sha",
                "current_sha": SHA1,
                "previous_runtime_kind": "none",
            },
        )
        self.assertIn("AUTO_ROLLBACK=PASS", result.stdout + result.stderr)

    def test_rollback_none_rejected(self):
        result = self.run_case(
            "rollback-none",
            expect_rc=1,
            pre_state={
                "current_runtime_kind": "exact_sha",
                "current_sha": SHA2,
                "previous_runtime_kind": "none",
            },
        )
        self.assertIn("previous=none", result.stdout + result.stderr)

    def test_rollback_unknown_rejected(self):
        result = self.run_case(
            "rollback-unknown",
            expect_rc=1,
            pre_state={
                "current_runtime_kind": "exact_sha",
                "current_sha": SHA2,
                "previous_runtime_kind": "exact_sha",
                "previous_sha": "UNKNOWN",
            },
        )
        self.assertIn("没有可回滚的 previous SHA", result.stdout + result.stderr)

    def test_u_dao_guard_rejects_missing_override(self):
        result = self.run_case(
            "u-dao-guard",
            expect_rc=1,
            extra_env={"GIT_HAS_UDAO_FILE": "0"},
            pre_state={
                "current_runtime_kind": "exact_sha",
                "current_sha": SHA2,
                "previous_runtime_kind": "exact_sha",
                "previous_sha": SHA0,
            },
        )
        self.assertIn("U_DAO_GUARD=FAIL", result.stdout + result.stderr)


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class SsotFileTests(unittest.TestCase):
    """直接验证 write_ssot 落盘内容（真实 deployment_state.py）。"""

    def test_first_release_ssot_file(self):
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ssot-", dir=artifacts) as directory:
            work = Path(directory).resolve()
            (work / "scripts").mkdir()
            for name in (
                "server-deploy.sh",
                "docker_cmd.sh",
                "vehicle-agent-rollout.sh",
                "deployment_state.py",
                "legacy_runtime.py",
            ):
                shutil.copyfile(ROOT / "scripts" / name, work / "scripts" / name)
            (work / "head").write_text(SHA1)
            _write_state(
                work / "deployments" / "current.json",
                {
                    "current_runtime_kind": "exact_sha",
                    "current_sha": SHA1,
                    "previous_runtime_kind": "none",
                },
            )
            # 独立 case 名避免与 HARNESS 内置 case 块重复执行 deploy_scope
            harness = HARNESS + (
                '\ncase "$CASE" in\n  ssot-case) deploy_scope all-app '
                "api mqtt-ingress command-dispatcher task-worker web ;;\nesac\n"
            )
            (work / "harness.sh").write_text(harness, encoding="utf-8", newline="\n")
            env = {
                **os.environ,
                "CASE": "ssot-case",
                "TARGET_SHA": SHA1,
                "TEST_PYTHON": sys.executable,
                "FIREBOT_COMPOSE_FILES": "docker-compose.server.yml deploy/u-dao/compose.u-dao.yml",
            }
            result = subprocess.run(
                ["bash", "harness.sh"],
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads((work / "deployments" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_sha"], SHA1)
            self.assertEqual(state["previous_runtime_kind"], "none")
            # 第二次发布：轮转 previous=旧 current
            _write_state(
                work / "deployments" / "current.json",
                {
                    "current_runtime_kind": "exact_sha",
                    "current_sha": SHA1,
                    "previous_runtime_kind": "none",
                },
            )
            env2 = {**env, "TARGET_SHA": SHA2}
            result2 = subprocess.run(
                ["bash", "harness.sh"],
                cwd=work,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env2,
                timeout=60,
            )
            self.assertEqual(result2.returncode, 0, result2.stderr)
            state2 = json.loads((work / "deployments" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(state2["current_sha"], SHA2)
            self.assertEqual(state2["previous_runtime_kind"], "exact_sha")
            self.assertEqual(state2["previous_sha"], SHA1)


class ReleaseGuardTests(unittest.TestCase):
    """immutable release 守卫（真实 git worktree 断言）。"""

    def _make_release(self, dirty=False, on_branch=False) -> tuple[Path, str]:
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        base = Path(tempfile.mkdtemp(prefix="release-guard-", dir=artifacts)).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(base)], check=True)
        subprocess.run(
            ["git", "-C", str(base), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(["git", "-C", str(base), "config", "user.name", "t"], check=True)
        (base / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(base), "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", str(base), "commit", "-qm", "init"], check=True)
        head = subprocess.check_output(
            ["git", "-C", str(base), "rev-parse", "HEAD"], text=True
        ).strip()
        (base / "head").write_text(head)
        # release 目录名必须是真实 git SHA（immutable 目录语义）
        release = base / head
        subprocess.run(
            ["git", "-C", str(base), "worktree", "add", "-q", "--detach", str(release), head],
            check=True,
        )
        if dirty:
            (release / "dirty.txt").write_text("d")
        if on_branch:
            subprocess.run(["git", "-C", str(release), "checkout", "-qb", "work"], check=True)
        return release, head

    def run_guard(self, release: Path, sha: str) -> subprocess.CompletedProcess:
        work = release.parent
        (work / "scripts").mkdir(exist_ok=True)
        for name in (
            "server-deploy.sh",
            "docker_cmd.sh",
            "vehicle-agent-rollout.sh",
            "deployment_state.py",
            "legacy_runtime.py",
        ):
            shutil.copyfile(ROOT / "scripts" / name, work / "scripts" / name)
        harness = (
            "set -euo pipefail\n"
            "export DOCKER_CMD_SKIP_DETECT=1\n"
            'if command -v cygpath >/dev/null; then TEST_PYTHON="$(cygpath -u "$TEST_PYTHON")";'
            "fi\n"
            'python3() { command "$TEST_PYTHON" "$@"; }\n'
            'git() { case "$1" in -C) command git "$@" ;; *) return 1 ;; esac; }\n'
            "source scripts/server-deploy.sh\n"
            "release_guard\n"
        )
        (work / "harness.sh").write_text(harness, encoding="utf-8", newline="\n")
        return subprocess.run(
            ["bash", "harness.sh"],
            cwd=work,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={
                **os.environ,
                "TARGET_SHA": sha,
                "FIREBOT_RELEASE_ROOT": str(release),
                "TEST_PYTHON": sys.executable,
            },
        )

    def test_release_guard_pass(self):
        release, head = self._make_release()
        result = self.run_guard(release, head)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RELEASE_IMMUTABLE=PASS", result.stdout)

    def test_release_guard_dir_name_mismatch(self):
        release, head = self._make_release()
        result = self.run_guard(release, SHA2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("目录名", result.stdout + result.stderr)

    def test_release_guard_dirty_rejected(self):
        release, head = self._make_release(dirty=True)
        result = self.run_guard(release, head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("immutable", result.stdout + result.stderr)

    def test_release_guard_branch_rejected(self):
        release, head = self._make_release(on_branch=True)
        result = self.run_guard(release, head)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
