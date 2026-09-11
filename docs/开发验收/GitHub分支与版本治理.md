# GitHub 分支与版本治理

> 本文是仓库分支策略与版本真相的唯一治理说明，后续接项目的人无需再问。

## 一、长期分支

只允许一个永久长期分支：

```text
main
```

`main` 是唯一长期主线，也是唯一被分支保护与 required checks 约束的分支。

## 二、临时分支

```text
feature/*
fix/*
maintenance/*
field/*
release/*
```

| 前缀 | 用途 | 生命周期 |
|---|---|---|
| `feature/*` | 新功能 | 开发 → PR → 合入 main → 删除 |
| `fix/*` | 缺陷修复 | 同上 |
| `maintenance/*` | 仓库整理 / 技术债 / 治理 | 同上 |
| `field/*` | 现场交付 | 同上 |
| `release/*` | 发布准备 | 同上 |

临时分支完成即删除，不留长期分叉。

## 三、禁止重新建立的长期分支

```text
develop
integration/**
hardening/**
```

这些是历史阶段分支，已收敛进 `main`。不得作为当前开发流程入口重新建立。

## 四、标准生命周期

```text
main
  → 创建临时分支
  → 开发
  → 本地检查
  → push
  → branch CI
  → PR → main
  → CI / CodeQL / Dependency Review
  → 合并
  → main CI
  → 删除临时分支
```

## 五、GitHub Actions 门禁

- **Firebot V2 CI**：backend / frontend / protocol / ros1-vehicle / docs / containers / e2e / security。
- **Field Release Gate**：软件交付 / 安装 / 车端包门禁（`workflow_dispatch` + `field/**` / `release/**`）。它是软件包验收，**不是**物理真车验收。
- **CodeQL**：`push main` + `pull_request main`。
- **Dependency Review**：`pull_request → main`，`fail-on-severity: high`。

## 六、版本真相

```text
branch != 部署真相
```

- 服务器只接受 exact 40 位 SHA 部署，不接受「拉最新」。
- 服务器运行版本必须由 runtime manifest / exact SHA / runtime tag 三重证明。
- `main` 更新不表示服务器升级。

## 七、tag 类型

| tag | 语义 | 允许指向 |
|---|---|---|
| `baseline/server-runtime-YYYY-MM-DD` | 真实部署过且封板的服务器 runtime | 服务器 runtime SHA |
| `baseline/repository-consolidated-YYYY-MM-DD` | 仓库治理 / 文档 / 主线封板 | 最终 main SHA |
| 正式 Release tag | 只有真实 field acceptance 解锁后 | 发布 SHA |

## 八、branch 删除规则

删除分支前必须验证它是 main 的祖先：

```bash
git merge-base --is-ancestor origin/<branch> origin/main && echo OK
```

返回 0 才允许删除；否则 STOP。

## 九、禁止

- force push main / delete main。
- 改写已发布 tag。
- 用 reset 抹历史。
- 把 field PENDING 伪造成 PASS。
- 为让 CI 变绿而改已验证正确的生产语义。
