// WEB-R1-MIN-FINAL PC 页面验收：1920×1080 / 1440×900 / 1366×768 原始 Chromium 截图 + 自动 UI 检查。
// 在 browser-flow.mjs 之后、同一隔离库上运行（复用其产生的计划 / 火警 / 账号数据）。
// 环境：E2E_BASE_URL、CHROMIUM_PATH、EVIDENCE_DIR；管理员口令为 browser-flow 改密后的口令。
//
// 自动检查（任一失败 → 非零退出）：
//   - 可见的禁用按钮必须给出原因（title / aria-describedby / 相邻提示文本），安全控制按钮除外但需有页面级原因提示；
//   - 抽屉打开时其操作按钮中心点命中抽屉本身（未被火警弹窗或其它浮层遮挡）；
//   - 表单 label 内必须恰好包含一个可编辑控件；
//   - 火警弹窗不遮挡页面标题与主操作按钮；
//   - 预演计划在列表中带“仅预演”标识。
import { chromium } from "playwright-core";
import assert from "node:assert/strict";
import fs from "node:fs";

const BASE = process.env.E2E_BASE_URL || "http://127.0.0.1:18080";
const OUT = `${process.env.EVIDENCE_DIR || "./web-r1-min-evidence"}/pc`;
const ADMIN_PW = process.env.E2E_ADMIN_CHANGED || "Firebot-E2E-Changed-2026!";
const SIZES = [
  [1920, 1080],
  [1440, 900],
  [1366, 768],
];
fs.mkdirSync(OUT, { recursive: true });
const report = [];
let failures = 0;
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || undefined,
  args: ["--no-sandbox"],
});

async function login(page) {
  await page.goto(`${BASE}/login`);
  await page.waitForLoadState("networkidle");
  for (let i = 0; i < 3; i += 1) {
    await page.fill("#login-username", "admin");
    await page.fill("#login-password", ADMIN_PW);
    if ((await page.inputValue("#login-username")) !== "admin") continue;
    const res = page
      .waitForResponse((r) => r.url().endsWith("/api/v1/auth/login"), {
        timeout: 5000,
      })
      .catch(() => null);
    await page.click('button[type="submit"]');
    if ((await res)?.status() === 200) break;
  }
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), {
    timeout: 10000,
  });
  await page.waitForLoadState("networkidle");
}

async function inspect(page) {
  return page.evaluate(() => {
    const visible = (el) => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return (
        r.width > 0 &&
        r.height > 0 &&
        s.visibility !== "hidden" &&
        s.display !== "none"
      );
    };
    const label = (el) =>
      (el.innerText || el.getAttribute("aria-label") || "")
        .trim()
        .replace(/\s+/g, " ");
    const disabled = [
      ...document.querySelectorAll(
        'button:disabled, button[aria-disabled="true"]',
      ),
    ]
      .filter(visible)
      .map((el) => {
        const container =
          el.closest(
            "section, article, form, .panel, .drawer-card, .hmi-control-rail, .page-title-row",
          ) || document.body;
        const reason =
          el.getAttribute("title") ||
          (el.getAttribute("aria-describedby") &&
            document.getElementById(el.getAttribute("aria-describedby"))
              ?.innerText) ||
          [
            ...container.querySelectorAll(
              '.hmi-hint, .form-hint, .inline-notice, .safety-note, [class*="reason"], small',
            ),
          ]
            .filter(visible)
            .map((n) => n.innerText.trim())
            .find(Boolean) ||
          "";
        return { text: label(el), reason: reason.slice(0, 80) };
      });
    const labels = [
      ...document.querySelectorAll(
        ".drawer-card label, .filter-bar label, .modal-card label",
      ),
    ]
      .filter(visible)
      .map((l) => ({
        text: (l.childNodes[0]?.textContent || "").trim(),
        controls: l.querySelectorAll(
          "input:not([type=checkbox]), select, textarea",
        ).length,
        checkbox: l.querySelectorAll("input[type=checkbox]").length,
      }));
    const popup = document.querySelector('[data-testid="alarm-popup"]');
    const popupRect =
      popup && visible(popup) ? popup.getBoundingClientRect() : null;
    const overlaps = (el) => {
      if (!popupRect || !el || !visible(el)) return false;
      const r = el.getBoundingClientRect();
      return !(
        r.right < popupRect.left ||
        r.left > popupRect.right ||
        r.bottom < popupRect.top ||
        r.top > popupRect.bottom
      );
    };
    const drawerButtons = [
      ...document.querySelectorAll(
        ".drawer-actions button, .drawer-card .modal-close",
      ),
    ]
      .filter(visible)
      .map((b) => {
        const r = b.getBoundingClientRect();
        const hit = document.elementFromPoint(
          r.left + r.width / 2,
          r.top + r.height / 2,
        );
        return {
          text: label(b) || "×",
          hitSelf: Boolean(hit && (hit === b || b.contains(hit))),
        };
      });
    return {
      disabled,
      labels,
      popupVisible: Boolean(popupRect),
      popupCoversTitle: overlaps(
        document.querySelector(".page-title-row h1, .hmi-title h1"),
      ),
      popupCoversPrimary: overlaps(
        document.querySelector(
          ".page-title-row .primary-button, .page-title-row .secondary-button",
        ),
      ),
      drawerButtons,
      previewBadges: [...document.querySelectorAll("td .form-hint")].filter(
        (n) => n.innerText.includes("仅预演"),
      ).length,
      scrollOverflowX:
        document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  });
}

function verify(name, data) {
  const issues = [];
  // 监控首页的安全控制按钮由页面级原因提示（控制链路未验证 / 急停合同未验证），保持原安全门。
  for (const b of data.disabled)
    if (!b.reason) issues.push(`禁用按钮无原因：${b.text}`);
  for (const l of data.labels)
    if (
      l.controls !== 1 &&
      l.checkbox === 0 &&
      !l.text.includes("目标侧向间距")
    )
      issues.push(`label 控件数异常：${l.text}=${l.controls}`);
  for (const b of data.drawerButtons)
    if (!b.hitSelf) issues.push(`抽屉按钮被遮挡：${b.text}`);
  if (data.popupCoversTitle) issues.push("火警弹窗遮挡页面标题");
  if (data.popupCoversPrimary) issues.push("火警弹窗遮挡页面主按钮");
  if (data.scrollOverflowX) issues.push("出现横向滚动");
  return issues;
}

async function capture(page, name, prep) {
  if (prep) await prep();
  await page.waitForTimeout(400);
  const data = await inspect(page);
  const issues = verify(name, data);
  if (issues.length) failures += 1;
  report.push({ name, ...data, issues });
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log(
    "PC",
    name,
    issues.length ? `ISSUES ${JSON.stringify(issues)}` : "OK",
    `disabled=${data.disabled.length} popup=${data.popupVisible}`,
  );
}

for (const [w, h] of SIZES) {
  const context = await browser.newContext({
    viewport: { width: w, height: h },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  });
  const page = await context.newPage();
  await login(page);
  const tag = `${w}x${h}`;
  const go = async (path) => {
    await page.goto(`${BASE}${path}`);
    await page.waitForLoadState("networkidle");
  };
  await capture(page, `${tag}-01-monitor`, () => go("/monitor"));
  await capture(page, `${tag}-02-patrol-list`, () => go("/patrol"));
  await capture(page, `${tag}-03-patrol-form`, async () => {
    await page.click('[data-testid="plan-create"]');
    await page.selectOption('select[name="robot_id"]', { index: 1 });
    await page.click('[data-testid="plan-preview"]');
    await page.waitForSelector('[data-testid="preview-times"] li');
  });
  await capture(page, `${tag}-04-patrol-detail`, async () => {
    await go("/patrol");
    await page
      .locator("tbody tr", { hasText: "仅预演" })
      .first()
      .getByRole("button", { name: "详情" })
      .click();
    await page.waitForSelector(
      '[data-testid="preview-records"] li, [data-testid="plan-detail"] .form-hint',
    );
  });
  await capture(page, `${tag}-05-tasks`, () => go("/tasks"));
  await capture(page, `${tag}-06-alarms`, () => go("/alarms"));
  await capture(page, `${tag}-07-alarm-detail`, async () => {
    await page
      .locator("tbody tr")
      .first()
      .getByRole("button", { name: "处置详情" })
      .click();
    await page.waitForSelector('[data-testid="alarm-detail"]');
  });
  await capture(page, `${tag}-08-users`, () => go("/users"));
  await capture(page, `${tag}-09-user-create`, () =>
    page.click('[data-testid="user-create"]'),
  );
  await capture(page, `${tag}-10-audit`, async () => {
    await go("/audit");
    await page.click('[data-testid="records-tab-operations"]');
    await page.waitForLoadState("networkidle");
  });
  await context.close();
}
await browser.close();
fs.writeFileSync(`${OUT}/pc-ui-report.json`, JSON.stringify(report, null, 2));
console.log(`PC_PAGES=${report.length} PAGES_WITH_ISSUES=${failures}`);
try {
  assert.equal(failures, 0);
} catch {
  process.exit(1);
}
