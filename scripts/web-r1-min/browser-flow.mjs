// WEB-R1-MIN 真实浏览器全流程验收（PC 1440×900；headless Chromium + 真实 API/PG/Redis，无 MQTT Broker）。
//
// 依赖：playwright-core（apps/web devDependency）。
// 环境：E2E_BASE_URL、E2E_ADMIN_PASSWORD（未改密的 bootstrap 口令）、CHROMIUM_PATH、EVIDENCE_DIR。
// 火警注入在进程内调用 mqtt-ingress handle_alarm，不经任何 MQTT Broker（隔离、SIMULATED）。
//
// 每个步骤和负测都用 assert 校验 HTTP 状态 / 错误码 / 业务数据；任何断言失败 → 进程非零退出。
// 最终 VERDICT 断言 Command / Outbox / Task 零新增，否则非零退出。
//
// 反证开关（必须使脚本失败）：
//   FAULT=status   把第一个负测的期望状态改错（400 → 200），断言必须失败。
//   FAULT=command  跳过 UI 流程，基线后向隔离库写入 1 条 Command，VERDICT 必须失败。
import { chromium } from "playwright-core";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";

process.env.TZ = "Asia/Shanghai";

const BASE = process.env.E2E_BASE_URL || "http://127.0.0.1:18080";
const OUT = process.env.EVIDENCE_DIR || "./web-r1-min-evidence";
const SHOTS = `${OUT}/shots`;
const FAULT = process.env.FAULT || "";
const ADMIN_PW = process.env.E2E_ADMIN_PASSWORD || "Firebot-E2E-Admin-2026!";
const ADMIN_NEW = "Firebot-E2E-Changed-2026!";
const SUFFIX = Date.now().toString().slice(-6);
const OP = `op${SUFFIX}`;
const VIEWER = `vw${SUFFIX}`;
const OP_INIT = "Operator-Init-2026!";
const OP_NEW = "Operator-Real-2026!";
const PLAN = `右侧车辆日常巡检-${OP}`;
fs.mkdirSync(SHOTS, { recursive: true });

const log = [];
const assertions = [];
const step = (name, data = {}) => {
  log.push({ t: new Date().toISOString(), step: name, ...data });
  console.log("STEP", name, JSON.stringify(data));
};
function check(label, fn) {
  try {
    fn();
    assertions.push({ label, ok: true });
  } catch (error) {
    assertions.push({
      label,
      ok: false,
      error: String(error.message).slice(0, 300),
    });
    throw new Error(`ASSERTION FAILED [${label}]: ${error.message}`);
  }
}
async function acheck(label, fn) {
  try {
    await fn();
    assertions.push({ label, ok: true });
  } catch (error) {
    assertions.push({
      label,
      ok: false,
      error: String(error.message).slice(0, 300),
    });
    throw new Error(`ASSERTION FAILED [${label}]: ${error.message}`);
  }
}
const expectApi = (label, res, status, code) =>
  check(label, () => {
    assert.equal(
      res.status,
      status,
      `${label}: HTTP ${res.status} != ${status}`,
    );
    if (code)
      assert.equal(
        res.data?.error?.code,
        code,
        `${label}: code ${res.data?.error?.code} != ${code}`,
      );
  });
const helper = new URL("./isolated_alarm_and_downlink.py", import.meta.url)
  .pathname;
const py = (...args) =>
  JSON.parse(
    execFileSync("python", [helper, ...args], { encoding: "utf8" })
      .trim()
      .split("\n")
      .pop(),
  );

function finish(code) {
  fs.writeFileSync(
    `${OUT}/e2e-flow-log.json`,
    JSON.stringify({ log, assertions }, null, 2),
  );
  const failed = assertions.filter((a) => !a.ok);
  console.log(`ASSERTIONS total=${assertions.length} failed=${failed.length}`);
  process.exit(code);
}
process.on("uncaughtException", (error) => {
  console.log("FATAL", String(error.message || error).slice(0, 500));
  finish(1);
});
process.on("unhandledRejection", (error) => {
  console.log("FATAL", String(error?.message || error).slice(0, 500));
  finish(1);
});

function verdict(baseline) {
  const final = py("counts");
  step("FINAL_DOWNLINK", final);
  const delta = {
    commands_created: final.commands - baseline.commands,
    outbox_created: final.outbox - baseline.outbox,
    outbox_published: final.outbox_published - baseline.outbox_published,
    tasks_created: final.tasks - baseline.tasks,
  };
  step("VERDICT", { ...delta, non_monitor_websockets: nonMonitorWs });
  check("VERDICT_NO_COMMAND", () => assert.equal(delta.commands_created, 0));
  check("VERDICT_NO_OUTBOX", () => assert.equal(delta.outbox_created, 0));
  check("VERDICT_NO_OUTBOX_PUBLISHED", () =>
    assert.equal(delta.outbox_published, 0),
  );
  check("VERDICT_NO_TASK", () => assert.equal(delta.tasks_created, 0));
  check("VERDICT_NO_FOREIGN_WEBSOCKET", () =>
    assert.deepEqual(nonMonitorWs, []),
  );
}

const nonMonitorWs = [];
const baseline = py("counts");
step("BASELINE_DOWNLINK", baseline);

if (FAULT === "command") {
  step("FAULT_INJECT_COMMAND", py("inject-command"));
  verdict(baseline); // 必须抛出 → 非零退出
  finish(0);
}

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--no-sandbox"],
});
async function ctx(viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({
    viewport,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  });
  const page = await context.newPage();
  page.on("websocket", (ws) => {
    if (!ws.url().includes("/ws/v1/monitor")) nonMonitorWs.push(ws.url());
  });
  page.on("pageerror", (e) => step("PAGE_ERROR", { message: String(e) }));
  return { context, page };
}
async function login(page, user, pw) {
  // 首屏 restore()（refresh 401）期间登录页会重新导航一次，过早填写的输入可能丢失：
  // 填写 → 校验输入值 → 等待真实 /auth/login 响应；未发出则重试。
  await page.goto(`${BASE}/login`);
  await page.waitForLoadState("networkidle");
  let status = 0;
  for (let attempt = 1; attempt <= 3 && !status; attempt += 1) {
    await page.fill("#login-username", user);
    await page.fill("#login-password", pw);
    if ((await page.inputValue("#login-username")) !== user) continue;
    const response = page
      .waitForResponse((r) => r.url().endsWith("/api/v1/auth/login"), {
        timeout: 5000,
      })
      .catch(() => null);
    await page.click('button[type="submit"]');
    status = (await response)?.status() || 0;
  }
  if (status === 200)
    await page.waitForURL((url) => !url.pathname.startsWith("/login"), {
      timeout: 10000,
    });
  await page.waitForLoadState("networkidle");
  return status;
}
async function changePassword(page, current, next) {
  await page.waitForURL(/change-password/);
  const inputs = page.locator('input[type="password"]');
  await inputs.nth(0).fill(current);
  await inputs.nth(1).fill(next);
  if ((await inputs.count()) > 2) await inputs.nth(2).fill(next);
  const done = page.waitForResponse((r) =>
    r.url().endsWith("/api/v1/auth/change-password"),
  );
  await page.click('button[type="submit"]');
  await acheck(`CHANGE_PASSWORD_${current.slice(0, 8)}`, async () =>
    assert.equal((await done).status(), 200),
  );
  await page.waitForURL(/login/);
}
async function go(page, path) {
  await page.goto(`${BASE}${path}`);
  await page.waitForLoadState("networkidle");
}
const shot = async (page, name) => {
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
  step("SCREENSHOT", { name });
};
async function bearerOf(page) {
  return page.evaluate(async () => {
    const csrf =
      document.cookie
        .split("; ")
        .find((c) => c.startsWith("csrf_token="))
        ?.split("=")[1] || "";
    const r = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      credentials: "include",
      headers: { "X-CSRF-Token": csrf },
    });
    return (await r.json()).access_token;
  });
}
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
const key = () => ({ "Idempotency-Key": crypto.randomUUID() });
const sched = { frequency: "DAILY", time: "09:00", timezone: "Asia/Shanghai" };

// 1 管理员登录（首次强制改密）
const { page: admin } = await ctx();
let status = await login(admin, "admin", ADMIN_PW);
check("1_ADMIN_LOGIN_HTTP", () => assert.equal(status, 200));
if (admin.url().includes("change-password")) {
  await changePassword(admin, ADMIN_PW, ADMIN_NEW);
  status = await login(admin, "admin", ADMIN_NEW);
  check("1_ADMIN_RELOGIN_HTTP", () => assert.equal(status, 200));
}
check("1_ADMIN_AT_MONITOR", () => assert.match(admin.url(), /\/monitor$/));
step("1_ADMIN_LOGIN", { url: admin.url() });
let token = await bearerOf(admin);

// P0-02 新账号默认 viewer：打开抽屉，不触碰角色，直接创建
await go(admin, "/users");
await admin.click('[data-testid="user-create"]');
const defaultChecked = await admin
  .locator('[data-testid="role-picker"] input[type="checkbox"]:checked')
  .evaluateAll((els) => els.map((e) => e.value));
check("P0-02_DRAWER_DEFAULT_ROLE_VIEWER", () =>
  assert.deepEqual(defaultChecked, ["viewer"]),
);
await admin.fill('input[name="username"]', VIEWER);
await admin.fill('input[name="display_name"]', "默认只读账号");
await admin.fill('input[name="password"]', OP_INIT);
const createdViewer = admin.waitForResponse(
  (r) =>
    r.url().endsWith("/api/v1/admin/users") && r.request().method() === "POST",
);
await admin.click('[data-testid="user-submit"]');
const createdViewerRes = await createdViewer;
check("P0-02_CREATE_HTTP_201", () =>
  assert.equal(createdViewerRes.status(), 201),
);
check("P0-02_REQUEST_ROLE_CODES", () =>
  assert.deepEqual(createdViewerRes.request().postDataJSON().role_codes, [
    "viewer",
  ]),
);
const viewerPerms = py("user-perms", VIEWER);
check("P0-02_EFFECTIVE_ROLES_VIEWER", () =>
  assert.deepEqual(viewerPerms.roles, ["viewer"]),
);
check("P0-02_EFFECTIVE_PERMISSIONS_READONLY", () =>
  assert.deepEqual(viewerPerms.permissions, [
    "alarm.read",
    "map.read",
    "patrol.report.read",
    "robot.read",
  ]),
);
step("P0-02_NEW_USER_DEFAULT_VIEWER", viewerPerms);
await shot(admin, "02-user-create-default-viewer");

// 2 新增操作员（显式选择“只读”，后续步骤 3 分配为操作员）
await go(admin, "/users");
await admin.click('[data-testid="user-create"]');
await admin.fill('input[name="username"]', OP);
await admin.fill('input[name="display_name"]', "巡检操作员");
await admin.fill('input[name="password"]', OP_INIT);
await admin.click('[data-testid="user-submit"]');
await admin.locator("tr", { hasText: OP }).first().waitFor();
const opRow = (await api(admin, token, "GET", "/admin/users")).data.find(
  (u) => u.username === OP,
);
check("2_OPERATOR_CREATED_AS_VIEWER", () =>
  assert.deepEqual(opRow.roles, ["viewer"]),
);
step("2_OPERATOR_CREATED", { username: OP });

const badRole = await api(
  admin,
  token,
  "PUT",
  `/admin/users/${opRow.id}/roles`,
  { role_codes: ["god_mode"] },
);
expectApi(
  "NEG_WRONG_ROLE",
  badRole,
  FAULT === "status" ? 200 : 400,
  FAULT === "status" ? undefined : "ROLE_UNKNOWN",
);
check("NEG_WRONG_ROLE_UNCHANGED", () =>
  assert.deepEqual(py("user-perms", OP).roles, ["viewer"]),
);

// 3 分配角色：只读 → 操作员（显式选择，后端授予）
await go(admin, "/users");
await admin
  .locator("tr", { hasText: OP })
  .getByRole("button", { name: "分配角色" })
  .click();
const rp = admin.locator('[data-testid="role-picker"]');
await rp.getByLabel("只读用户").uncheck();
await rp.getByLabel("操作员").check();
const rolesSaved = admin.waitForResponse((r) =>
  r.url().includes(`/admin/users/${opRow.id}/roles`),
);
await admin.click('[data-testid="user-submit"]');
const rolesRes = await rolesSaved;
check("3_ROLE_ASSIGN_HTTP", () => assert.equal(rolesRes.status(), 200));
await acheck("3_ROLE_SESSIONS_REVOKED", async () => {
  assert.equal((await rolesRes.json()).sessions_revoked, true);
});
check("3_EFFECTIVE_ROLE_OPERATOR", () =>
  assert.deepEqual(py("user-perms", OP).roles, ["operator"]),
);
await admin.getByText("角色已更新").waitFor();
await shot(admin, "03-role-assigned");
step("3_ROLE_ASSIGNED", { role: "operator" });

// 4 操作员登录（首次改密）
const { context: opCtx, page: op } = await ctx();
await acheck("4_OPERATOR_FIRST_LOGIN", async () =>
  assert.equal(await login(op, OP, OP_INIT), 200),
);
await changePassword(op, OP_INIT, OP_NEW);
await acheck("4_OPERATOR_RELOGIN", async () =>
  assert.equal(await login(op, OP, OP_NEW), 200),
);
check("4_OPERATOR_AT_MONITOR", () => assert.match(op.url(), /\/monitor$/));
step("4_OPERATOR_LOGIN", { url: op.url() });
const opToken = await bearerOf(op);
expectApi(
  "NEG_PRIVILEGE_ESCALATION",
  await api(op, opToken, "PUT", `/admin/users/${opRow.id}/roles`, {
    role_codes: ["super_admin"],
  }),
  403,
);
expectApi(
  "NEG_OPERATOR_USER_ADMIN",
  await api(op, opToken, "GET", "/admin/users"),
  403,
);
check("NEG_ESCALATION_UNCHANGED", () =>
  assert.deepEqual(py("user-perms", OP).roles, ["operator"]),
);

// 5–7 创建右侧巡检计划 → 预览定时时间 → 保存草稿
await go(op, "/patrol");
await op.waitForSelector('[data-testid="plan-create"]:not([disabled])');
await op.click('[data-testid="plan-create"]');
await op.fill('input[name="name"]', PLAN);
await acheck("NEG_NO_SELECTION_SAVE_DISABLED", async () => {
  assert.equal(
    await op.locator('[data-testid="plan-save"]').isDisabled(),
    true,
  );
  assert.equal(
    (await op.locator('[data-testid="form-blocker"]').textContent())?.trim(),
    "请选择执行车辆",
  );
});
const r001 = await op
  .locator('select[name="robot_id"] option', { hasText: "R001" })
  .getAttribute("value");
await op.selectOption('select[name="robot_id"]', r001);
await acheck("NO_AUTO_ROUTE", async () => {
  assert.equal(
    await op.locator('select[name="trajectory_id"]').inputValue(),
    "",
  );
});
const route = await op
  .locator('select[name="trajectory_id"] option:not([disabled])')
  .first()
  .getAttribute("value");
await op.selectOption('select[name="trajectory_id"]', route);
await op.selectOption('select[name="frequency"]', "WEEKLY");
await op.locator(".weekday-picker").getByLabel("周一").check();
await op.locator(".weekday-picker").getByLabel("周五").check();
await op.fill('input[name="time"]', "08:30");
await op.click('[data-testid="plan-preview"]');
await op.waitForSelector('[data-testid="preview-times"] li');
const previews = await op
  .locator('[data-testid="preview-times"] li')
  .allTextContents();
check("6_PREVIEW_FIVE_TIMES_MON_FRI_0830", () => {
  assert.equal(previews.length, 5);
  for (const text of previews) {
    assert.match(text, /08:30:00$/);
    assert.ok([1, 5].includes(new Date(text).getDay()), `非周一/周五：${text}`);
  }
});
step("6_PREVIEW_TIMES", { previews });
await shot(op, "05-plan-form-preview");
const saved = op.waitForResponse(
  (r) => r.url().endsWith("/api/v1/plans") && r.request().method() === "POST",
);
await op.click('[data-testid="plan-save"]');
const savedRes = await saved;
check("7_DRAFT_SAVE_HTTP_201", () => assert.equal(savedRes.status(), 201));
await op.getByText("草稿已保存").waitFor();
// 刷新后仍在（持久化）
await op.reload();
await op.waitForLoadState("networkidle");
await acheck("7_DRAFT_SURVIVES_RELOAD", async () => {
  await op.locator("tr", { hasText: PLAN }).waitFor({ timeout: 10000 });
});
step("7_DRAFT_SAVED", { plan: PLAN });
await shot(op, "07-plan-list");

const plan = (await api(op, opToken, "GET", "/plans")).data.find(
  (p) => p.name === PLAN,
);
check("PLAN_STATE_PREVIEW_ONLY", () => {
  assert.equal(plan.execution_mode, "PREVIEW_ONLY");
  assert.equal(plan.enabled, false);
  assert.equal(plan.status, "DRAFT");
  assert.equal(plan.work_side, "RIGHT");
  assert.deepEqual(
    [plan.target_lateral_min_m, plan.target_lateral_max_m],
    [0.2, 0.3],
  );
  assert.equal(plan.lateral_verification, "NOT_VERIFIED");
  assert.equal(plan.real_execution, false);
});
// UI 失败路径：同名再保存 → 抽屉保留、错误提示、按钮恢复可用
await op.click('[data-testid="plan-create"]');
await op.fill('input[name="name"]', PLAN);
await op.selectOption('select[name="robot_id"]', r001);
await op.selectOption('select[name="trajectory_id"]', route);
await op.click('[data-testid="plan-save"]');
await op.getByText("该车辆已存在同名巡检计划").first().waitFor();
await acheck("UI_DUPLICATE_SAVE_KEEPS_DRAWER", async () => {
  assert.equal(
    await op.locator('[data-testid="plan-drawer"]').isVisible(),
    true,
  );
  assert.equal(
    await op.locator('[data-testid="plan-save"]').isDisabled(),
    false,
  );
});
await shot(op, "07c-plan-duplicate-error");
await op
  .locator('[data-testid="plan-drawer"] button', { hasText: "取消" })
  .click();

const planBody = (name, extra = {}) => ({
  name,
  robot_id: plan.robot_id,
  trajectory_id: plan.trajectory_id,
  schedule: sched,
  ...extra,
});
expectApi(
  "NEG_DUPLICATE_SAVE",
  await api(op, opToken, "POST", "/plans", planBody(PLAN), key()),
  409,
  "PLAN_NAME_EXISTS",
);
expectApi(
  "NEG_PLAN_ENABLE",
  await api(op, opToken, "POST", `/plans/${plan.id}/enable`),
  409,
  "PLAN_EXECUTION_FORBIDDEN",
);
expectApi(
  "NEG_CREATE_ENABLED",
  await api(
    op,
    opToken,
    "POST",
    "/plans",
    planBody(`${PLAN}-x`, { enabled: true }),
    key(),
  ),
  409,
  "PLAN_EXECUTION_FORBIDDEN",
);
expectApi(
  "NEG_CREATE_LEGACY_MODE",
  await api(
    op,
    opToken,
    "POST",
    "/plans",
    planBody(`${PLAN}-m`, { execution_mode: "LEGACY_EXECUTABLE" }),
    key(),
  ),
  409,
  "PLAN_EXECUTION_FORBIDDEN",
);
expectApi(
  "NEG_NO_VEHICLE",
  await api(
    op,
    opToken,
    "POST",
    "/plans",
    { ...planBody(`${PLAN}-nv`), robot_id: "NOPE" },
    key(),
  ),
  422,
  "PLAN_ROBOT_REQUIRED",
);
expectApi(
  "NEG_NO_ROUTE",
  await api(
    op,
    opToken,
    "POST",
    "/plans",
    { ...planBody(`${PLAN}-nr`), trajectory_id: "NOPE" },
    key(),
  ),
  422,
  "PLAN_ROUTE_REQUIRED",
);
expectApi(
  "NEG_RUN_PREVIEW_PLAN",
  await api(
    admin,
    token,
    "POST",
    "/tasks/patrol-plan",
    { robot_id: "R001", patrol_plan_id: plan.id },
    key(),
  ),
  409,
  "PATROL_PLAN_PREVIEW_ONLY",
);
expectApi(
  "NEG_ENABLED_SCHEDULE",
  await api(admin, token, "POST", "/patrol-schedules", {
    patrol_plan_id: plan.id,
    cron_expression: "* * * * *",
    enabled: true,
  }),
  409,
  "PLAN_EXECUTION_FORBIDDEN",
);
const stale = await api(op, opToken, "PUT", `/plans/${plan.id}`, {
  ...planBody(PLAN),
  revision: plan.revision,
});
check("PLAN_EDIT_OK", () => assert.equal(stale.status, 200));
expectApi(
  "NEG_STALE_REVISION",
  await api(op, opToken, "PUT", `/plans/${plan.id}`, {
    ...planBody(PLAN),
    revision: plan.revision,
  }),
  409,
  "PLAN_REVISION_CONFLICT",
);
check("NEG_PLAN_STILL_PREVIEW", () =>
  assert.equal(py("counts").commands, baseline.commands),
);

// 计划详情 + 预演记录
await go(op, "/patrol");
await op
  .locator("tr", { hasText: PLAN })
  .getByRole("button", { name: "预演" })
  .click();
await op.getByText("已记录").first().waitFor();
await op
  .locator("tr", { hasText: PLAN })
  .getByRole("button", { name: "详情" })
  .click();
await op.waitForSelector('[data-testid="preview-records"] li');
const mech = await op.locator('[data-testid="mechanism-state"]').textContent();
check("PLAN_DETAIL_MECHANISM_NOT_CONTROLLED", () =>
  assert.match(mech, /未验收 · 平台不控制/),
);
await shot(op, "07b-plan-detail");

// 8 任务记录：无“创建巡检任务”入口，未新增任务
await go(op, "/tasks");
await op.waitForSelector(".table-wrap tbody, .table-wrap .empty-state");
await acheck("8_NO_CREATE_PATROL_ENTRY", async () => {
  assert.equal(await op.locator("text=创建巡检任务").count(), 0);
});
check("8_NO_TASK_CREATED", () =>
  assert.equal(py("counts").tasks, baseline.tasks),
);
await shot(op, "08-task-records");

// 断网重连：离线期间注入火警，恢复后弹窗仍出现（在任务记录页接收，验证全局弹窗）
await go(op, "/tasks");
await opCtx.setOffline(true);
const offlineAlarm = py("inject", "ZZ-UNKNOWN-9");
await op.waitForTimeout(1500);
await opCtx.setOffline(false);
check("NET_OFFLINE_ALARM_UNMATCHED", () =>
  assert.equal(offlineAlarm.alarms[0].location_state, "UNMATCHED"),
);
step("NET_OFFLINE_INJECT", offlineAlarm);

// 9 接收隔离火警（未知位置）
const live = py("inject", "-");
check("9_ALARM_STORED_UNMATCHED", () => {
  assert.equal(live.alarms.length, 1);
  assert.equal(live.alarms[0].location_state, "UNMATCHED");
  assert.equal(live.alarms[0].state, "NEW");
});
const popup = op.locator('[data-testid="alarm-popup"]');
await popup.waitFor({ timeout: 40000 });
await op
  .locator(`[data-testid="alarm-popup"][data-alarm-id="${live.alarms[0].id}"]`)
  .waitFor({ timeout: 20000 });
await acheck("9_POPUP_SHOWS_UNMATCHED_LOCATION", async () => {
  assert.match(await popup.innerText(), /位置未匹配/);
});
await acheck("9_BELL_COUNT_2", async () => {
  assert.match(await op.locator('[data-testid="alarm-bell"]').innerText(), /2/);
});
await shot(op, "09-alarm-popup");

// 重复事件：同 event_id 再报 → 合并计数，不新建
const dup = py("inject", "-", live.event_id);
check("NEG_DUPLICATE_EVENT_MERGED", () => {
  assert.equal(dup.alarms.length, 1);
  assert.equal(dup.alarms[0].id, live.alarms[0].id);
  assert.equal(dup.alarms[0].occurrence_count, 2);
});

// 未确认火警：“关闭提示”不改变状态
const hidden = await popup.getAttribute("data-alarm-id");
await op.click('[data-testid="alarm-popup-close"]');
check("NEG_CLOSE_HINT_NOT_ACK", () =>
  assert.equal(py("alarm-state", hidden).state, "NEW"),
);

// 10 确认收到：被确认的必须是弹窗所展示的同一事件
await popup.waitFor({ timeout: 20000 });
const shownId = await popup.getAttribute("data-alarm-id");
const shownCode = await popup.getAttribute("data-event-code");
await acheck("10_POPUP_TEXT_MATCHES_ATTR", async () => {
  assert.ok((await popup.innerText()).includes(shownCode));
});
const others = [offlineAlarm.alarms[0].id, live.alarms[0].id].filter(
  (id) => id !== shownId,
);
const ackRes = op.waitForResponse((r) => r.url().includes("/acknowledge"));
await op.click('[data-testid="alarm-popup-ack"]');
const ack = await ackRes;
check("10_ACK_HTTP_200_SAME_ALARM", () => {
  assert.equal(ack.status(), 200);
  assert.ok(ack.url().includes(`/alarms/${shownId}/acknowledge`), ack.url());
});
check("10_ACKED_ALARM_IS_SHOWN_ONE", () => {
  const state = py("alarm-state", shownId);
  assert.equal(state.state, "ACKNOWLEDGED");
  assert.equal(state.event_code, shownCode);
  assert.ok(state.ack_by);
});
check("10_OTHER_ALARM_STILL_NEW", () => {
  for (const id of others) assert.equal(py("alarm-state", id).state, "NEW");
});
step("10_ACKNOWLEDGED", { alarm_id: shownId, event_code: shownCode });
// 监控首页：本车火警由页面横幅 + 主火警面板提示，不再重复弹窗遮挡控制区
await go(op, "/monitor");
await acheck("MONITOR_BANNER_NOT_DUPLICATE_POPUP", async () => {
  await op.locator(".situation-banner.critical").waitFor({ timeout: 10000 });
  assert.equal(await op.locator('[data-testid="alarm-popup"]').count(), 0);
});
await go(op, "/alarms");
await op
  .locator("tbody tr", { hasText: shownCode })
  .getByRole("button", { name: "处置详情" })
  .click();
await op.waitForSelector('[data-testid="alarm-actions"] li');
await acheck("10_ACTION_RECORD_VISIBLE", async () => {
  assert.match(
    await op.locator('[data-testid="alarm-actions"]').innerText(),
    /巡检操作员/,
  );
});
await shot(op, "10-alarm-detail");

// 导出权限
const exportStatus = (tok, kind) =>
  op.evaluate(
    async ({ tok, kind }) =>
      (
        await fetch(`/api/v1/history/export/${kind}`, {
          headers: { Authorization: `Bearer ${tok}` },
        })
      ).status,
    { tok, kind },
  );
await acheck("NEG_EXPORT_AUDIT_OPERATOR_403", async () => {
  assert.equal(await exportStatus(opToken, "audit"), 403);
});
await acheck("EXPORT_TASKS_OPERATOR_200", async () => {
  assert.equal(await exportStatus(opToken, "tasks"), 200);
});

// 11 查询操作审计（管理员）
token = await bearerOf(admin);
await go(admin, "/audit");
await admin.click('[data-testid="records-tab-operations"]');
await admin.click('[data-testid="records-query"]');
await admin.waitForLoadState("networkidle");
const ops = (await api(admin, token, "GET", "/history/operations?limit=500"))
  .data;
check("11_AUDIT_HAS_ROLE_PLAN_ACK", () => {
  const actions = new Set(ops.map((r) => r.action));
  for (const a of [
    "USER_CREATE",
    "USER_ROLES_UPDATE",
    "PATROL_PLAN_DRAFT_CREATE",
    "PATROL_PLAN_ENABLE_REJECTED",
    "ALARM_ACKNOWLEDGED",
    "EXPORT_DENIED",
  ])
    assert.ok(actions.has(a), a);
  assert.ok(
    ops.some(
      (r) => r.action === "ALARM_ACKNOWLEDGED" && r.resource_id === shownId,
    ),
  );
});
await shot(admin, "11-audit");
const [download] = await Promise.all([
  admin.waitForEvent("download"),
  admin.click('[data-testid="records-export"]'),
]);
check("11_AUDIT_EXPORT_CSV", () =>
  assert.match(download.suggestedFilename(), /^firebot-operations-\d+\.csv$/),
);

// 12 管理员停用账号
await go(admin, "/users");
await admin.click(`[data-testid="user-status-${OP}"]`);
const disabled = admin.waitForResponse((r) =>
  r.url().includes(`/admin/users/${opRow.id}/status`),
);
await admin.click('[data-testid="user-submit"]');
await acheck("12_DISABLE_HTTP_200", async () => {
  assert.equal((await disabled).status(), 200);
});
await admin.getByText("账号已停用").first().waitFor();
await shot(admin, "12-user-disabled");

// 13 会话失效：页面回登录、旧令牌 401、重新登录 403
await op.goto(`${BASE}/monitor`).catch(() => {});
await op.waitForURL(/login/, { timeout: 20000 });
expectApi("13_OLD_TOKEN_401", await api(op, opToken, "GET", "/auth/me"), 401);
await acheck("13_RELOGIN_403", async () => {
  assert.equal(await login(op, OP, OP_NEW), 403);
  assert.match(await op.locator(".yd-auth-error").innerText(), /账号已停用/);
});
await shot(op, "13-disabled-login");

verdict(baseline);
await browser.close();
finish(0);
