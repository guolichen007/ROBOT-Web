# Integration-Ready 发布检查清单

- [x] Ruff/format/mypy/Pytest
- [x] ESLint/Prettier/vue-tsc/Vitest/production build，无 chunk warning
- [x] Schema/generated/Mock/tester/handoff 漂移检查
- [x] production TODO/FIXME/HACK=0（明确 allowlist 除外）
- [x] Gitleaks、Trivy CRITICAL=0、HIGH=0（文件系统与最终 API/Web 镜像）
- [x] GitHub CodeQL（develop run `31477199650`、main run `31477708537`）
- [x] 文档链接、SERVER public surface、无 latest image
- [x] DEV/TEST/SERVER profile smoke
- [x] media unauthorized/authorized/expired
- [x] DB commit failure 不发布 realtime event
- [x] boot/target_boot、partition、backup/restore、fault
- [x] 一小时 soak、多车 burst、clean clone
- [x] 经 owner 明确授权，hardening 直接合并 -> develop；develop CI run `31477199629` PASS
- [x] 经 owner 明确授权，develop 直接合并 -> main；main CI run `31477708650` PASS
- [x] release commit 通过最终门禁后创建 tag `v2.0.0-integration-ready`
- [x] ROS2 ZIP 连续生成及跨 worktree SHA256 一致：`1ff2816ee0b662b6509b7cc161d3cef1f35c3f094d66e41999652994f586f25f`
- [x] clean main，main/develop/hardening/tag 远端 refs 一致
