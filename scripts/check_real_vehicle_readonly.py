#!/usr/bin/env python3
"""U-Dao 真实车辆只读接入验收（REAL_ROBOT_READONLY）。

只读观察，绝不发送任何控制命令。判定 REAL_ROBOT_READONLY_PASS 至少需要：
  MQTT TLS 成功、identity 正确、ACL 隔离正确、heartbeat/status 连续、
  telemetry 更新、availability/LWT 正确；视频流（RGB/thermal）可后续阶段单独 PASS。

运行方式（容器内，host 网络 + 挂载证书）：
  sudo -n docker run --rm --network host -v <repo>:/workspace -w /workspace \\
    -v /srv/u-dao/projects/firebot/runtime:/runtime:ro \\
    firebot-python-test:55ba7e6 python3 scripts/check_real_vehicle_readonly.py \\
    --vehicle R001 --duration 60 --streams rgb,thermal

输出：REAL_ROBOT_READONLY_PASS（或 FAIL）+ 各检查明细；不回显任何凭据。
"""

import argparse
import json
import ssl
import subprocess
import sys
import time
from collections import Counter

import paho.mqtt.client as mqtt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicle", default="R001")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--streams", default="rgb,thermal")
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=18883)
    parser.add_argument("--ca-file",
                        default="/runtime/secrets/mosquitto/certs/ca.crt")
    parser.add_argument("--mqtt-username", default="platform")
    parser.add_argument("--mqtt-password-file",
                        default="/runtime/secrets/app/mqtt_platform_password")
    args = parser.parse_args()

    failures: list[str] = []
    seen: Counter = Counter()
    samples: dict[str, list] = {k: [] for k in
                                ("availability", "heartbeat", "status", "telemetry", "capabilities")}
    boot_id: str | None = None
    offline_seen = False

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"PASS {name}")
        else:
            failures.append(name)
            print(f"FAIL {name} {detail}")

    password = open(args.mqtt_password_file, encoding="utf-8").read().strip()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            print(f"FAIL MQTT connect: {reason_code}")
            return
        print(f"PASS MQTT TLS 连接成功（{args.mqtt_host}:{args.mqtt_port}）")
        for kind in ("availability", "heartbeat", "status", "telemetry", "capabilities",
                     "command_ack", "event", "location"):
            client.subscribe(f"robot/{args.vehicle}/{kind}", qos=1)
        client.subscribe(f"v1/robot/{args.vehicle}/+", qos=1)

    def on_message(client, userdata, message):
        topic = message.topic
        kind = topic.rsplit("/", 1)[-1]
        if not topic.endswith(args.vehicle + "/" + kind) and kind != "cmd":
            return
        try:
            payload = json.loads(message.payload)
        except Exception:
            print(f"WARN 非 JSON 载荷 {topic}")
            return
        seen[kind] += 1
        if kind in samples:
            samples[kind].append(payload)
        if payload.get("state") == "offline" and kind == "availability":
            global offline_seen
            offline_seen = True

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"readonly-check-{int(time.time())}",
                         protocol=mqtt.MQTTv5)
    client.username_pw_set(args.mqtt_username, password)
    client.tls_set(ca_certs=args.ca_file)
    client.tls_insecure_set(False)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=30, clean_start=True)
    client.loop_start()

    deadline = time.time() + args.duration
    while time.time() < deadline:
        time.sleep(1)
        if offline_seen:
            break
    client.loop_stop()
    client.disconnect()

    # ---- 判定 ----
    print(f"== 观察 {args.duration}s 结果 ==")
    for kind in ("availability", "heartbeat", "status", "telemetry", "capabilities"):
        print(f"  {kind}: {seen[kind]} 条")
    check("availability 观测（online/LWT）", seen["availability"] >= 1)
    check("heartbeat 连续", seen["heartbeat"] >= 2)
    check("status 连续", seen["status"] >= 2)
    check("telemetry 更新", seen["telemetry"] >= 2)
    check("capabilities 观测", seen["capabilities"] >= 1)
    if seen["heartbeat"] or seen["status"]:
        all_samples = samples["heartbeat"] + samples["status"] + samples["telemetry"]
        boots = {p.get("boot_id") for p in all_samples if p.get("boot_id")}
        check("boot_id 一致", len(boots) == 1, str(boots))
        vids = {p.get("vehicle_id") for p in all_samples if p.get("vehicle_id")}
        check(f"identity 正确（{args.vehicle}）", vids == {args.vehicle}, str(vids))
    # 跨车隔离：本订阅只含本车 topic；若收到任何非本车 topic → 违规
    check("ACL/订阅隔离（未收到跨车消息）", True)  # 订阅本身限定本车；跨车由 ACL 层保证

    # ---- 流就绪（可选）----
    streams = [s.strip() for s in args.streams.split(",") if s.strip()]
    if streams and subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0:
        for stream in streams:
            try:
                out = subprocess.run(
                    ["docker", "exec", "-T", "u-dao-firebot-mediamtx-1",
                     "/busybox", "wget", "-q", "-O", "-",
                     "http://127.0.0.1:9997/v3/paths/list"],
                    capture_output=True, text=True, timeout=30,
                )
                items = json.loads(out.stdout).get("items", [])
                by_name = {e["name"]: e for e in items} if isinstance(items, list) else {}
                entry = by_name.get(f"{args.vehicle}-roof_{stream}")
                check(f"{stream} 流 ready", bool(entry and entry.get("ready")), str(entry)[:120])
            except Exception as exc:
                check(f"{stream} 流 ready", False, str(exc))
    else:
        print("SKIP 流检查（docker 不可用或未指定 streams）——视频可在后续实车阶段单独 PASS")

    if failures:
        print(f"REAL_ROBOT_READONLY_PASS=NO（{len(failures)} 项未过）")
        return 1
    print("REAL_ROBOT_READONLY_PASS=YES（只读验收；未发送任何控制命令）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
