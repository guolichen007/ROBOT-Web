// 依赖：playwright-core（apps/web devDependency）；在 apps/web 目录下以 node 运行。
// 环境：E2E_BASE_URL、E2E_ADMIN_PASSWORD（未改密的 bootstrap 口令）、CHROMIUM_PATH、EVIDENCE_DIR。
// 火警注入在进程内调用 mqtt-ingress handle_alarm，不经任何 MQTT Broker（隔离、SIMULATED）。
// WEB-R1-MIN 真实浏览器全流程（headless Chromium + 真实 API/PG/Redis，无 MQTT Broker）。
import { chromium } from "playwright-core";
import { execFileSync } from "node:child_process";
import fs from "node:fs";

const BASE = process.env.E2E_BASE_URL || "http://127.0.0.1:18080";
const OUT = process.env.EVIDENCE_DIR || "./web-r1-min-evidence";
const SHOTS = `${OUT}/shots`;
const ADMIN_PW = process.env.E2E_ADMIN_PASSWORD || "Firebot-E2E-Admin-2026!";
const ADMIN_NEW = "Firebot-E2E-Changed-2026!";
const OP = `op${Date.now().toString().slice(-6)}`;
const OP_INIT = "Operator-Init-2026!";
const OP_NEW = "Operator-Real-2026!";
const PLAN = `右侧车辆日常巡检-${OP}`;
const log = [];
const step = (name, data = {}) => {
  log.push({ t: new Date().toISOString(), step: name, ...data });
  console.log("STEP", name, JSON.stringify(data));
};
const py = (...args) =>
  JSON.parse(
    execFileSync(
      "python",
      [
        new URL("./isolated_alarm_and_downlink.py", import.meta.url).pathname,
        ...args,
      ],
      { encoding: "utf8" },
    )
      .trim()
      .split("\n")
      .pop(),
  );

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--no-sandbox"],
});
const mqttLike = [];
const netlog = [];
process.on("uncaughtException", (error) => {
  console.log("NETLOG_TAIL", JSON.stringify(netlog.slice(-40), null, 0));
  console.log("FATAL", String(error).slice(0, 300));
  process.exit(1);
});
async function ctx(viewport) {
  const context = await browser.newContext({
    viewport,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  });
  const page = await context.newPage();
  const tag = `${viewport.width}#${Math.random().toString(36).slice(2, 5)}`;
  page.on("response", (r) => {
    if (r.url().includes("/api/"))
      netlog.push(
        `${tag} ${r.request().method()} ${r.url().replace(BASE, "")} ${r.status()}`,
      );
  });
  page.on("requestfailed", (r) => {
    if (r.url().includes("/api/"))
      netlog.push(
        `${tag} FAILED ${r.url().replace(BASE, "")} ${r.failure()?.errorText}`,
      );
  });
  page.on("framenavigated", (f) => {
    if (f === page.mainFrame())
      netlog.push(`${tag} NAV ${f.url().replace(BASE, "")}`);
  });
  page.on("websocket", (ws) => {
    if (!ws.url().includes("/ws/v1/monitor")) mqttLike.push(ws.url());
  });
  page.on("pageerror", (e) => step("PAGE_ERROR", { message: String(e) }));
  return { context, page };
}
async function login(page, user, pw) {
  // 脚本时序：首屏 restore()（refresh 401）期间登录页会重新导航一次，过早填写的输入可能丢失。
  // 因此：填写 → 校验输入值 → 等待真实的 /auth/login 响应；未发出请求则重试。
  await page.goto(`${BASE}/login`);
  await page.waitForLoadState("networkidle");
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    await page.fill("#login-username", user);
    await page.fill("#login-password", pw);
    if ((await page.inputValue("#login-username")) !== user) continue;
    const response = page
      .waitForResponse((r) => r.url().endsWith("/api/v1/auth/login"), {
        timeout: 5000,
      })
      .catch(() => null);
    await page.click('button[type="submit"]');
    if (await response) break;
  }
  // 登录成功后路由守卫会跳转到 /monitor 或 /change-password；等待离开 /login 再返回。
  await page
    .waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 10000 })
    .catch(() => {});
  await page.waitForLoadState("networkidle");
}
async function changePassword(page, current, next) {
  await page.waitForURL(/change-password/);
  const inputs = page.locator('input[type="password"]');
  await inputs.nth(0).fill(current);
  await inputs.nth(1).fill(next);
  if ((await inputs.count()) > 2) await inputs.nth(2).fill(next);
  await page.click('button[type="submit"]');
  await page.waitForTimeout(800);
}
const shot = async (page, name) => {
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false });
  step("SCREENSHOT", { name });
};

const baseline = py("counts");
step("BASELINE_DOWNLINK", baseline);

// 1 管理员登录（首次强制改密）
const { context: adminCtx, page: admin } = await ctx({
  width: 1440,
  height: 900,
});
await login(admin, "admin", ADMIN_PW);
if (admin.url().includes("change-password")) {
  await changePassword(admin, ADMIN_PW, ADMIN_NEW);
  await login(admin, "admin", ADMIN_NEW);
}
step("1_ADMIN_LOGIN", { url: admin.url() });

// 2 新增操作员（先只读，验证后续“分配角色”生效）
await admin.goto(`${BASE}/users`);
await admin.waitForLoadState("networkidle");
await admin.click('[data-testid="user-create"]');
await admin.fill('input[name="username"]', OP);
await admin.fill('input[name="display_name"]', "巡检操作员");
await admin.fill('input[name="password"]', OP_INIT);
const picker = admin.locator('[data-testid="role-picker"]');
await picker.getByLabel("操作员").uncheck();
await picker.getByLabel("只读用户").check();
await shot(admin, "02-desktop-user-create");
await admin.click('[data-testid="user-submit"]');
await admin.waitForSelector(`text=${OP}`);
step("2_OPERATOR_CREATED", { username: OP });

// 错误角色 / 越权（API 级反证，复用浏览器会话令牌）
const token = await admin.evaluate(async () => {
  const r = await fetch("/api/v1/auth/refresh", {
    method: "POST",
    credentials: "include",
    headers: {
      "X-CSRF-Token":
        document.cookie
          .split("; ")
          .find((c) => c.startsWith("csrf_token="))
          ?.split("=")[1] || "",
    },
  });
  return (await r.json()).access_token;
});
const api = (pg, tok, method, path, body, headers = {}) =>
  pg.evaluate(
    async ({ method, path, body, tok, headers }) => {
      const r = await fetch(`/api/v1${path}`, {
        method,
        headers: {
          "content-type": "application/json",
          Authorization: `Bearer ${tok}`,
          ...headers,
        },
        body: body ? JSON.stringify(body) : undefined,
      });
      let data = null;
      try {
        data = await r.json();
      } catch {}
      return { status: r.status, data };
    },
    { method, path, body, tok, headers },
  );
const users = (await api(admin, token, "GET", "/admin/users")).data;
const opRow = users.find((u) => u.username === OP);
const badRole = await api(
  admin,
  token,
  "PUT",
  `/admin/users/${opRow.id}/roles`,
  { role_codes: ["god_mode"] },
);
step("NEG_WRONG_ROLE", {
  status: badRole.status,
  code: badRole.data?.error?.code,
});

// 3 分配角色：只读 → 操作员
await admin.reload();
await admin
  .locator("tr", { hasText: OP })
  .getByRole("button", { name: "分配角色" })
  .click();
const rp = admin.locator('[data-testid="role-picker"]');
await rp.getByLabel("只读用户").uncheck();
await rp.getByLabel("操作员").check();
await admin.click('[data-testid="user-submit"]');
await admin.waitForSelector("text=角色已更新");
await shot(admin, "03-desktop-role-assigned");
step("3_ROLE_ASSIGNED", { role: "operator" });

// 4 操作员登录（首次改密）
const { context: opCtx, page: op } = await ctx({ width: 1440, height: 900 });
await login(op, OP, OP_INIT);
await changePassword(op, OP_INIT, OP_NEW);
await login(op, OP, OP_NEW);
step("4_OPERATOR_LOGIN", { url: op.url() });
const opToken = await op.evaluate(async () => {
  const r = await fetch("/api/v1/auth/refresh", {
    method: "POST",
    credentials: "include",
    headers: {
      "X-CSRF-Token":
        document.cookie
          .split("; ")
          .find((c) => c.startsWith("csrf_token="))
          ?.split("=")[1] || "",
    },
  });
  return (await r.json()).access_token;
});
const escalate = await api(
  op,
  opToken,
  "PUT",
  `/admin/users/${opRow.id}/roles`,
  { role_codes: ["super_admin"] },
);
step("NEG_PRIVILEGE_ESCALATION", { status: escalate.status });
const opUsers = await api(op, opToken, "GET", "/admin/users");
step("NEG_OPERATOR_USER_ADMIN", { status: opUsers.status });

// 5–7 创建右侧巡检计划 → 预览定时时间 → 保存草稿
await op.goto(`${BASE}/patrol`);
await op.waitForLoadState("networkidle");
await op.waitForSelector('[data-testid="plan-create"]:not([disabled])');
await op.click('[data-testid="plan-create"]');
await op.fill('input[name="name"]', PLAN);
// 未选择车辆 / 路线时不可保存（“尚未配置路线”文案由 vitest web-r1-min.test.ts 覆盖）
step("NEG_NO_SELECTION_UI", {
  blocker: await op.locator('[data-testid="form-blocker"]').textContent(),
  saveDisabled: await op.locator('[data-testid="plan-save"]').isDisabled(),
});
const r001 = await op
  .locator('select[name="robot_id"] option', { hasText: "R001" })
  .getAttribute("value");
await op.selectOption('select[name="robot_id"]', r001);
const trajValue = await op.locator('select[name="trajectory_id"]').inputValue();
step("NO_AUTO_ROUTE", { trajectoryAfterVehicleSelect: trajValue });
const firstRoute = await op
  .locator('select[name="trajectory_id"] option:not([disabled])')
  .first()
  .getAttribute("value");
await op.selectOption('select[name="trajectory_id"]', firstRoute);
await op.selectOption('select[name="frequency"]', "WEEKLY");
await op.locator(".weekday-picker").getByLabel("周一").check();
await op.locator(".weekday-picker").getByLabel("周五").check();
await op.fill('input[name="time"]', "08:30");
await op.click('[data-testid="plan-preview"]');
await op.waitForSelector('[data-testid="preview-times"] li');
const previews = await op
  .locator('[data-testid="preview-times"] li')
  .allTextContents();
step("6_PREVIEW_TIMES", { previews });
await shot(op, "05-desktop-plan-form-preview");
await op.click('[data-testid="plan-save"]');
await op.waitForSelector("text=草稿已保存");
await op.waitForSelector(`text=${PLAN}`);
step("7_DRAFT_SAVED", { plan: PLAN });
await shot(op, "07-desktop-plan-list");

const plans = (await api(op, opToken, "GET", "/plans")).data;
const plan = plans.find((p) => p.name === PLAN);
step("PLAN_STATE", {
  execution_mode: plan.execution_mode,
  enabled: plan.enabled,
  status: plan.status,
  work_side: plan.work_side,
  lateral: [plan.target_lateral_min_m, plan.target_lateral_max_m],
  lateral_verification: plan.lateral_verification,
});
// 重复保存 / 计划误启用 / 无车辆 / 无路线（API 级）
const dup = await api(
  op,
  opToken,
  "POST",
  "/plans",
  {
    name: PLAN,
    robot_id: plan.robot_id,
    trajectory_id: plan.trajectory_id,
    schedule: { frequency: "DAILY", time: "09:00", timezone: "Asia/Shanghai" },
  },
  { "Idempotency-Key": crypto.randomUUID() },
);
step("NEG_DUPLICATE_SAVE", { status: dup.status, code: dup.data?.error?.code });
const enable = await api(op, opToken, "POST", `/plans/${plan.id}/enable`);
step("NEG_PLAN_ENABLE", {
  status: enable.status,
  code: enable.data?.error?.code,
});
const sneaky = await api(
  op,
  opToken,
  "POST",
  "/plans",
  {
    name: `${PLAN}-x`,
    robot_id: plan.robot_id,
    trajectory_id: plan.trajectory_id,
    enabled: true,
    schedule: { frequency: "DAILY", time: "09:00", timezone: "Asia/Shanghai" },
  },
  { "Idempotency-Key": crypto.randomUUID() },
);
step("NEG_CREATE_ENABLED", {
  status: sneaky.status,
  code: sneaky.data?.error?.code,
});
const noVeh = await api(
  op,
  opToken,
  "POST",
  "/plans",
  {
    name: `${PLAN}-nv`,
    robot_id: "NOPE",
    trajectory_id: plan.trajectory_id,
    schedule: { frequency: "DAILY", time: "09:00", timezone: "Asia/Shanghai" },
  },
  { "Idempotency-Key": crypto.randomUUID() },
);
step("NEG_NO_VEHICLE", { status: noVeh.status, code: noVeh.data?.error?.code });
const noRoute = await api(
  op,
  opToken,
  "POST",
  "/plans",
  {
    name: `${PLAN}-nr`,
    robot_id: plan.robot_id,
    trajectory_id: "NOPE",
    schedule: { frequency: "DAILY", time: "09:00", timezone: "Asia/Shanghai" },
  },
  { "Idempotency-Key": crypto.randomUUID() },
);
step("NEG_NO_ROUTE", {
  status: noRoute.status,
  code: noRoute.data?.error?.code,
});
const legacyRun = await api(
  admin,
  token,
  "POST",
  "/tasks/patrol-plan",
  { robot_id: "R001", patrol_plan_id: plan.id },
  { "Idempotency-Key": crypto.randomUUID() },
);
step("NEG_RUN_PREVIEW_PLAN", {
  status: legacyRun.status,
  code: legacyRun.data?.error?.code,
});

// 计划详情 + 预演记录
await op
  .locator("tr", { hasText: PLAN })
  .getByRole("button", { name: "预演" })
  .click();
await op.waitForSelector("text=已记录");
await op
  .locator("tr", { hasText: PLAN })
  .getByRole("button", { name: "详情" })
  .click();
await op.waitForSelector('[data-testid="preview-records"] li');
step("PLAN_DETAIL", {
  mechanism: await op.locator('[data-testid="mechanism-state"]').textContent(),
});
await shot(op, "07b-desktop-plan-detail");
await op.keyboard.press("Escape");

// 8 查看任务记录（预演计划不产生任务）
await op.goto(`${BASE}/tasks`);
await op.waitForLoadState("networkidle");
await op.waitForSelector(".table-wrap", { timeout: 15000 }).catch(async (e) => {
  await op.screenshot({ path: `${SHOTS}/FAIL-tasks.png` });
  step("FAIL_TASKS", {
    url: op.url(),
    body: (await op.locator("body").innerText()).slice(0, 400),
  });
  throw e;
});
await op.waitForTimeout(600);
const taskRows = await op.locator("tbody tr").count();
step("8_TASK_RECORDS", {
  rows: taskRows,
  createPatrolButton: await op.locator("text=创建巡检任务").count(),
});
await shot(op, "08-desktop-task-records");

// 断网重连：离线期间注入火警，恢复后弹窗仍出现
await op.goto(`${BASE}/monitor`);
await op.waitForLoadState("networkidle");
await op.waitForTimeout(1500);
await opCtx.setOffline(true);
const offlineInjected = py("inject", "ZZ-UNKNOWN-9");
await op.waitForTimeout(1500);
await opCtx.setOffline(false);
step("NET_OFFLINE_INJECT", offlineInjected);

// 9 接收隔离火警事件（未知位置）
const live = py("inject", "-");
step("9_ISOLATED_ALARM_INJECTED", live);
await op.waitForSelector('[data-testid="alarm-popup"]', { timeout: 40000 });
const popupText = await op.locator('[data-testid="alarm-popup"]').innerText();
step("9_POPUP", {
  text: popupText.replace(/\n/g, " | "),
  bell: await op.locator('[data-testid="alarm-bell"]').innerText(),
});
await shot(op, "09-desktop-alarm-popup");

// 重复事件：同 event_id 再报一次 → 合并计数，不新建
const dupEvent = py("inject", "-", live.event_id);
step("NEG_DUPLICATE_EVENT", { alarms: dupEvent.alarms });

// 未确认火警：关闭提示不改变状态
await op.click('[data-testid="alarm-popup-close"]');
const afterClose = (
  await api(op, opToken, "GET", `/alarms/${live.alarms[0].id}`)
).data;
step("NEG_CLOSE_HINT_NOT_ACK", { state: afterClose.state });

// 10 确认收到（弹窗）
await op.waitForSelector('[data-testid="alarm-popup"]', { timeout: 20000 });
const ackTarget = await op
  .locator('[data-testid="alarm-popup"] p')
  .first()
  .innerText();
await op.click('[data-testid="alarm-popup-ack"]');
await op.waitForTimeout(1200);
step("10_ACKNOWLEDGED", { target: ackTarget });
await op.goto(`${BASE}/alarms`);
await op.waitForLoadState("networkidle");
await op.waitForSelector("tbody tr");
await op
  .locator("tbody tr")
  .first()
  .getByRole("button", { name: "处置详情" })
  .click();
await op.waitForSelector('[data-testid="alarm-detail"]');
await shot(op, "10-desktop-alarm-detail");
const unacked = (await api(op, opToken, "GET", "/alarms?state=NEW")).data
  .length;
step("UNACKED_REMAINING", { count: unacked });

// 导出权限：操作员可导出任务；不可导出审计
const exportAudit = await op.evaluate(
  async (tok) =>
    (
      await fetch("/api/v1/history/export/audit", {
        headers: { Authorization: `Bearer ${tok}` },
      })
    ).status,
  opToken,
);
const exportTasks = await op.evaluate(
  async (tok) =>
    (
      await fetch("/api/v1/history/export/tasks", {
        headers: { Authorization: `Bearer ${tok}` },
      })
    ).status,
  opToken,
);
step("NEG_EXPORT_PERMISSION", {
  operatorAudit: exportAudit,
  operatorTasks: exportTasks,
});

// 11 查询操作审计（管理员）
await admin.goto(`${BASE}/audit`);
await admin.waitForLoadState("networkidle");
await admin.click('[data-testid="records-tab-operations"]');
await admin.waitForTimeout(800);
await admin.click('[data-testid="records-query"]');
await admin.waitForTimeout(1200);
const auditText = await admin.locator("tbody").innerText();
step("11_AUDIT_QUERY", {
  hasPlan: auditText.includes("巡检计划"),
  hasAck: auditText.includes("已确认收到"),
  hasRole: auditText.includes("分配角色"),
});
await shot(admin, "11-desktop-audit");
const [download] = await Promise.all([
  admin.waitForEvent("download"),
  admin.click('[data-testid="records-export"]'),
]);
step("AUDIT_EXPORT", { file: download.suggestedFilename() });

// 12 管理员停用账号
await admin.goto(`${BASE}/users`);
await admin.waitForLoadState("networkidle");
await admin.click(`[data-testid="user-status-${OP}"]`);
await admin.click('[data-testid="user-submit"]');
await admin.waitForSelector("text=账号已停用");
step("12_OPERATOR_DISABLED");
await shot(admin, "12-desktop-user-disabled");

// 13 验证会话失效：操作员页面被踢回登录；旧令牌 401；重新登录 403
await op.goto(`${BASE}/monitor`).catch(() => {});
await op.waitForURL(/login/, { timeout: 20000 }).catch(() => {});
const oldToken = await api(op, opToken, "GET", "/auth/me");
await login(op, OP, OP_NEW);
const loginErr = await op
  .locator(".yd-auth-error")
  .innerText()
  .catch(() => "");
step("13_SESSION_INVALID", {
  url: op.url(),
  oldTokenStatus: oldToken.status,
  reloginError: loginErr,
});
await shot(op, "13-desktop-disabled-login");

// 移动端截图
const { context: mCtx, page: m } = await ctx({ width: 390, height: 844 });
await login(m, "admin", ADMIN_NEW);
for (const [path, name] of [
  ["/patrol", "m1-mobile-patrol"],
  ["/alarms", "m2-mobile-alarms"],
  ["/users", "m3-mobile-users"],
  ["/audit", "m4-mobile-audit"],
  ["/tasks", "m5-mobile-tasks"],
]) {
  await m.goto(`${BASE}${path}`);
  await m.waitForLoadState("networkidle");
  await m.waitForTimeout(1200);
  await shot(m, name);
}
await m.goto(`${BASE}/patrol`);
await m.waitForLoadState("networkidle");
await m.waitForTimeout(800);
await m.click('[data-testid="plan-create"]');
await m.waitForTimeout(500);
await shot(m, "m6-mobile-plan-form");

const final = py("counts");
step("FINAL_DOWNLINK", final);
const verdict = {
  commands_created: final.commands - baseline.commands,
  outbox_created: final.outbox - baseline.outbox,
  outbox_published: final.outbox_published - baseline.outbox_published,
  tasks_created: final.tasks - baseline.tasks,
  non_monitor_websockets: mqttLike,
};
step("VERDICT", verdict);
fs.writeFileSync(`${OUT}/e2e-flow-log.json`, JSON.stringify(log, null, 2));
await browser.close();
