"""WEB-R1C R1C-4 real runtime chain (isolated SIM stack only; refuses anything but loopback).

Browser-less but process-real: API (uvicorn) + PostgreSQL + Redis + mosquitto + mqtt-ingress
+ mock-robot (firebot-sim-r002-01 publishing real location). This script plays ONLY the SIM
vehicle executor side (SceneLoadReceipt / MissionFeedback over `_sim/v21/...`), everything else
is decided by the running platform:

  import -> checksum -> save (route with intermediate STOP) -> atomic publish -> manifest verify
  -> SIM load request -> receipt over MQTT -> ingress -> robots.loaded_scene_* persisted
  -> mission admission (contracts.mission_admissible on the platform's own snapshot)
  -> feedback over MQTT (incl. counterexamples) -> ingress apply_feedback -> task persisted
  -> SUCCEEDED claim -> platform confirm_mission_success from its own live pose -> SUCCEEDED.
usage: python scripts/r1c_sim_chain.py <out.json>
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import paho.mqtt.client as mqtt

API = "http://127.0.0.1:8000/api/v1"
VEH = "firebot-sim-r002-01"
PW = ["Firebot-E2E-Changed-2026!", "TestOnly-ChangeMe-2026!"]
LOG: list[dict] = []
FAR = __import__("os").environ.get("R1C_FAR") == "1"
YAML = (
    "image: sim.pgm\nresolution: 0.05\norigin: [-5.0, -5.0, 0.0]\n"
    "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
)
PGM = b"P5\n200 200\n255\n" + bytes([254]) * 40000


def step(name: str, ok: bool, **detail) -> None:
    LOG.append({"step": name, "ok": ok, **detail})
    print(("PASS " if ok else "FAIL ") + name, json.dumps(detail, ensure_ascii=False)[:300])
    if not ok:
        raise SystemExit(1)


def login(c: httpx.Client) -> None:
    for pw in PW:
        r = c.post(f"{API}/auth/login", json={"username": "admin", "password": pw})
        if r.status_code != 200:
            continue
        tok = r.json()["access_token"]
        if r.json()["user"]["must_change_password"]:
            c.post(
                f"{API}/auth/change-password",
                json={"current_password": pw, "new_password": PW[0]},
                headers={"Authorization": f"Bearer {tok}"},
            )
            return login(c)
        c.headers["Authorization"] = f"Bearer {tok}"
        return
    raise SystemExit("login failed")


def now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> None:
    out = sys.argv[1]
    c = httpx.Client(timeout=20)
    login(c)
    mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-executor-{uuid4().hex[:6]}")
    mq.connect("127.0.0.1", 1883)
    mq.loop_start()

    def pub(kind: str, body: dict) -> None:
        mq.publish(f"_sim/v21/{VEH}/{kind}", json.dumps(body), qos=1).wait_for_publish(5)
        time.sleep(0.6)

    robot = None
    for _ in range(40):
        robot = next(
            r for r in c.get(f"{API}/monitor/snapshot").json()["robots"] if r["vehicle_id"] == VEH
        )
        if (
            robot["online_state"] == "ONLINE"
            and robot.get("boot_id")
            and robot.get("x") is not None
        ):
            break
        time.sleep(0.5)
    step("sim_vehicle_online", robot["online_state"] == "ONLINE", boot=robot.get("boot_id"))
    boot = robot["boot_id"]
    home = (robot["x"], robot["y"], robot["theta"])

    site = robot["site_id"] if "site_id" in robot else None
    if not site:
        site = c.get(f"{API}/robots/{VEH}").json().get("site_id")
    r = c.post(
        f"{API}/scenes/import",
        data={"site_id": site, "map_code": "parking_v1", "version": f"r1c-{uuid4().hex[:6]}"},
        files={"yaml_file": ("sim.yaml", YAML.encode()), "image_file": ("sim.pgm", PGM)},
    )
    step("import", r.status_code == 201, code=r.status_code)
    s = r.json()
    s = c.put(
        f"{API}/scenes/{s['scene_id']}/nav-map-checksum",
        json={"expected_revision": s["revision"], "map_checksum": "demo-map-v1"},
    ).json()
    pts = [
        {
            "point_id": str(uuid4()),
            "code": f"W{i}",
            "kind": "INSPECTION",
            "x": x,
            "y": y,
            "yaw": 0.0,
        }
        for i, (x, y) in enumerate([(3.0, 1.2), (4.0, 1.2), (5.0, 1.2)])
    ]
    final = {
        "point_id": str(uuid4()),
        "code": "HOME",
        "kind": "RETURN_HOME",
        # R1C_FAR=1: final waypoint 3 m away from where the vehicle really is (negative run)
        "x": round(home[0] + (3.0 if FAR else 0.0), 4),
        "y": round(home[1], 4),
        "yaw": round(math.atan2(math.sin(home[2]), math.cos(home[2])), 6),
    }
    arrivals = ["PASS_THROUGH", "STOP", "PASS_THROUGH", "STOP"]
    route = {
        "route_id": str(uuid4()),
        "code": "SIM-R1",
        "points": [
            {"point_id": p["point_id"], "arrival": a}
            for p, a in zip([*pts, final], arrivals, strict=True)
        ],
    }
    r = c.put(
        f"{API}/scenes/{s['scene_id']}",
        json={"expected_revision": s["revision"], "points": [*pts, final], "routes": [route]},
    )
    step("save_route_with_intermediate_stop", r.status_code == 200, code=r.status_code)
    s = r.json()
    r = c.post(f"{API}/scenes/{s['scene_id']}/publish", json={"expected_revision": s["revision"]})
    step(
        "atomic_publish",
        r.status_code == 200 and len(r.json()["scene_digest"]) == 64,
        code=r.status_code,
    )
    pubd = r.json()
    m = c.get(f"{API}/scenes/{s['scene_id']}/manifest").json()
    step(
        "manifest_digest_and_arrivals",
        m["manifest"]["digest"] == pubd["scene_digest"]
        and m["route_arrivals"][route["route_id"]] == arrivals,
        digest=pubd["scene_digest"],
        asset_sha256=m["asset_sha256"],
    )

    # admission BEFORE any load must be refused by the platform
    r = c.post(
        f"{API}/scenes/{s['scene_id']}/sim-missions",
        json={"vehicle_id": VEH, "route_id": route["route_id"]},
    )
    step("mission_refused_before_load", r.status_code == 409, code=r.json()["error"]["code"])

    lr = c.post(f"{API}/scenes/{s['scene_id']}/load", json={"vehicle_id": VEH})
    step("sim_load_request", lr.status_code == 202, code=lr.status_code)
    load = lr.json()
    receipt = {
        "contract_version": "2.1.0-draft.3",
        "kind": "SceneLoadReceipt",
        "receipt_id": str(uuid4()),
        "load_id": load["load_id"],
        "vehicle_id": VEH,
        "environment": "SIM",
        "boot_id": boot,
        "seq": 1,
        "scene_id": load["scene_id"],
        "scene_version": load["scene_version"],
        "scene_digest": load["scene_digest"],
        "nav_map_checksum": load["nav_map_checksum"],
        "result": "LOADED",
        "reason_code": None,
        "loaded_at": now(),
        "reported_at": now(),
    }
    # counterexample first: a receipt with a wrong digest must not confirm
    bad = {**receipt, "receipt_id": str(uuid4()), "scene_digest": "0" * 64}
    pub("scene_receipt", bad)
    st = c.get(f"{API}/scenes/sim-vehicles/{VEH}/loaded-scene").json()
    step("wrong_digest_receipt_not_confirmed", st["load_state"] == "LOAD_PENDING", state=st)
    pub("scene_receipt", receipt)
    st = c.get(f"{API}/scenes/sim-vehicles/{VEH}/loaded-scene").json()
    step(
        "receipt_over_mqtt_confirms_latest_load",
        st["load_state"] == "LOADED_CONFIRMED"
        and st["loaded_scene_digest"] == pubd["scene_digest"],
        state=st,
    )

    r = c.post(
        f"{API}/scenes/{s['scene_id']}/sim-missions",
        json={"vehicle_id": VEH, "route_id": route["route_id"]},
    )
    step(
        "mission_admitted_after_confirmed_load",
        r.status_code == 201,
        code=r.status_code,
        body=r.text[:200],
    )
    req = r.json()["mission_request"]
    step(
        "mission_request_keeps_arrivals",
        [w["arrival"] for w in req["route_snapshot"]["waypoints"]] == arrivals,
    )
    mid = req["mission_id"]
    seq = [10]

    def fb(state, wp, goal=None, **extra):
        seq[0] += 1
        body = {
            "contract_version": "2.1.0-draft.3",
            "kind": "MissionFeedback",
            "vehicle_id": VEH,
            "environment": "SIM",
            "boot_id": boot,
            "seq": seq[0],
            "mission_id": mid,
            "command_id": req["command_id"],
            "execution_gen": 1,
            "goal_id": goal,
            "state": state,
            "phase": state,
            "progress": extra.pop("progress", 0),
            "waypoint_index": wp,
            "waypoint_total": 4,
            "current_point_id": None,
            "observed_at": now(),
            **extra,
        }
        pub("mission_feedback", body)
        return c.get(f"{API}/scenes/sim-missions/{mid}").json()

    t = fb("ACCEPTED", 0)
    step("feedback_accepted", t["state"]["state"] == "ACCEPTED", status=t["status"])
    for i in range(3):
        t = fb("EXECUTING", i, goal=f"g-{i}", progress=25 * (i + 1))
    step("feedback_waypoints_0_2", t["state"]["waypoint_index"] == 2 and t["status"] == "EXECUTING")
    # counterexamples through the real ingress: skipped waypoint, stale boot, early SUCCEEDED
    before = dict(t["rejected"])
    t = fb(
        "SUCCEEDED",
        2,
        goal="g-2",
        progress=100,
        final_pose={"x": 0, "y": 0, "yaw": 0, "frame_id": "map"},
        stationary=True,
    )
    step(
        "intermediate_succeeded_rejected",
        t["rejected"].get("INTERMEDIATE_SUCCEEDED", 0)
        == before.get("INTERMEDIATE_SUCCEEDED", 0) + 1,
        rejected=t["rejected"],
    )
    seq[0] += 1
    stale = {**t["mission_request"], "kind": "x"}  # unused; keep shape explicit below
    del stale
    body = {
        "contract_version": "2.1.0-draft.3",
        "kind": "MissionFeedback",
        "vehicle_id": VEH,
        "environment": "SIM",
        "boot_id": str(uuid4()),
        "seq": seq[0],
        "mission_id": mid,
        "command_id": req["command_id"],
        "execution_gen": 1,
        "goal_id": "g-3",
        "state": "EXECUTING",
        "phase": "EXECUTING",
        "progress": 80,
        "waypoint_index": 3,
        "waypoint_total": 4,
        "current_point_id": None,
        "observed_at": now(),
    }
    pub("mission_feedback", body)
    t = c.get(f"{API}/scenes/sim-missions/{mid}").json()
    step(
        "stale_boot_feedback_rejected",
        t["rejected"].get("STALE_BOOT_ID", 0) >= 1 and t["state"]["waypoint_index"] == 2,
    )
    t = fb("EXECUTING", 3, goal="g-3", progress=90)
    t = fb(
        "SUCCEEDED",
        3,
        goal="g-3",
        progress=100,
        final_pose={"x": final["x"], "y": final["y"], "yaw": final["yaw"], "frame_id": "map"},
        stationary=True,
    )
    for _ in range(10):
        if t["status"] == "SUCCEEDED":
            break
        time.sleep(0.5)
        t = c.get(f"{API}/scenes/sim-missions/{mid}").json()
    if FAR:
        step(
            "vehicle_claim_not_confirmed_when_pose_off_target",
            t["status"] != "SUCCEEDED" and t["confirm_code"] == "POSITION_OUT_OF_TOLERANCE",
            status=t["status"],
            phase=t["phase"],
            confirm=t["confirm_code"],
        )
        with open(out, "w") as f:
            json.dump({"steps": LOG, "mission": t}, f, ensure_ascii=False, indent=1)
        print(f"R1C_SIM_CHAIN_FAR PASS {len(LOG)} steps")
        return
    step(
        "platform_confirms_success_from_own_live_pose",
        t["status"] == "SUCCEEDED" and t["confirm_code"] == "OK",
        status=t["status"],
        phase=t["phase"],
        confirm=t["confirm_code"],
    )
    tasks = c.get(f"{API}/tasks?limit=50").json()
    persisted = next(x for x in tasks if x["id"] == t["task_id"])
    step("task_state_persisted", persisted["status"] == "SUCCEEDED", task=persisted["task_code"])
    mq.loop_stop()
    with open(out, "w") as f:
        json.dump(
            {"steps": LOG, "mission": t, "scene": pubd["scene_id"], "digest": pubd["scene_digest"]},
            f,
            ensure_ascii=False,
            indent=1,
        )
    print(f"R1C_SIM_CHAIN PASS {len(LOG)} steps")


if __name__ == "__main__":
    main()
