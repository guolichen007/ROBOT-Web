# Integration-Ready 发布检查清单

- [x] Ruff/format/mypy/Pytest
- [x] ESLint/Prettier/vue-tsc/Vitest/production build，无 chunk warning
- [x] Schema/generated/Mock/tester/handoff 漂移检查
- [x] production TODO/FIXME/HACK=0（明确 allowlist 除外）
- [x] Gitleaks、Trivy CRITICAL=0、HIGH=0（文件系统与最终 API/Web 镜像）
- [ ] GitHub CodeQL
- [x] 文档链接、SERVER public surface、无 latest image
- [x] DEV/TEST/SERVER profile smoke
- [x] media unauthorized/authorized/expired
- [x] DB commit failure 不发布 realtime event
- [x] boot/target_boot、partition、backup/restore、fault
- [x] 一小时 soak、多车 burst、clean clone
- [ ] hardening PR -> develop，develop CI
- [ ] develop PR -> main，main CI
- [ ] tag `v2.0.0-integration-ready`
- [x] ROS2 ZIP 与 SHA256 验证
- [ ] clean main，远端 refs 一致
