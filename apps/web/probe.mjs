import { chromium } from "playwright";
const browser = await chromium.launch();
const ctx = await browser.newContext();
const request = ctx.request;
const login = await request.post("http://127.0.0.1:28080/api/v1/auth/login", { data: { username: "admin", password: "TestOnly-ChangeMe-2026!" } });
console.log("login:", login.status());
const body = await login.json();
const token = body.access_token;
const H = { Authorization: "Bearer " + token };
const snap = await (await request.get("http://127.0.0.1:28080/api/v1/monitor/snapshot", { headers: H })).json();
const r001 = snap.robots.find(r => r.vehicle_id === "R001");
console.log("R001:", r001 && { id: r001.id, online: r001.online_state, enabled: r001.enabled, mode: r001.current_mode });
console.log("slots:", snap.parking_slots.length, "traj:", snap.trajectories.length);
const slot = snap.parking_slots[0];
const r = await request.post("http://127.0.0.1:28080/api/v1/tasks/patrol", {
  headers: { ...H, "Idempotency-Key": crypto.randomUUID() },
  data: { robot_id: r001.id, target_parking_slot_id: slot.id, trajectory_id: snap.trajectories[0]?.id, parameters: {} },
});
console.log("patrol:", r.status(), JSON.stringify(await r.json()).slice(0, 300));
await browser.close();
