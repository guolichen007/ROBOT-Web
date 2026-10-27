#!/usr/bin/env bash
# Docker 命令统一抽象（R2-1）：docker → sudo -n docker → fail-closed。
# - Git/worktree/文件操作始终由调用用户执行；sudo 只作用于 docker 命令本身（R2-23）。
# - CI（无 sudo 但有 docker 组）走第一分支，行为不变。
# - 测试 harness 可用 DOCKER_CMD_SKIP_DETECT=1 强制 DOCKER=(docker)（注入 docker() mock）。
# 用法：source scripts/docker_cmd.sh && docker_cmd_detect || exit 1
DOCKER=()

docker_cmd_detect() {
  if [ "${DOCKER_CMD_SKIP_DETECT:-0}" = 1 ]; then
    DOCKER=(docker)
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER=(docker)
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
    return 0
  fi
  printf 'DOCKER_CMD=FAIL: docker 与 sudo -n docker 均不可用（fail-closed）\n' >&2
  return 1
}
