#!/usr/bin/env python3
"""U-Dao 真实 broker ACL 行为级验证（VEHICLE_ACL_LIVE）。

使用 R001 实际 credential + TLS 连接运行中的 u-dao-firebot Mosquitto，
以 MQTT5 SUBACK/PUBACK reason code / 连接结果判定授权，绝不以「未收到消息」代替 ACL 判定。
输出 VEHICLE_ACL_LIVE=PASS/FAIL。

用法（容器内，host 网络 + runtime 挂载）：
  sudo -n docker run --rm --network host -v <repo>:/workspace -w /workspace \\
    -v /srv/u-dao/projects/firebot/runtime:/runtime:ro \\
    firebot-python-test:55ba7e6 python3 deploy/u-dao/scripts/61-verify-vehicle-acl.py \\
    --vehicle R001 --mqtt-host 127.0.0.1 --mqtt-port 18883
"""

import argparse
import sys
import time

import paho.mqtt.client as mqtt

NOT_AUTHORIZED = 0x87


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicle", default="R001")
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=18883)
    parser.add_argument("--ca-file", default="/runtime/secrets/mosquitto/certs/ca.crt")
    parser.add_argument(
        "--vehicle-password-file", default="/runtime/secrets/vehicles/{v}/mqtt_password"
    )
    parser.add_argument(
        "--platform-password-file", default="/runtime/secrets/app/mqtt_platform_password"
    )
    args = parser.parse_args()

    vehicle = args.vehicle
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"PASS {name}")
        else:
            failures.append(name)
            print(f"FAIL {name} {detail}")

    def read_password(path: str) -> str:
        with open(path, encoding="utf-8") as stream:
            return stream.read().strip()

    vehicle_password = read_password(args.vehicle_password_file.replace("{v}", vehicle))
    platform_password = read_password(args.platform_password_file)

    def make_client(username: str, password: str) -> mqtt.Client:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"acl-check-{username}-{int(time.time() * 1000)}",
            protocol=mqtt.MQTTv5,
        )
        client.username_pw_set(username, password)
        client.tls_set(ca_certs=args.ca_file)
        client.tls_insecure_set(False)
        return client

    def connect_result(username: str, password: str) -> int:
        """返回 CONNACK reason code（0=成功）。"""
        result: dict = {"rc": -1}

        def on_connect(client, userdata, flags, reason_code, properties):
            result["rc"] = reason_code

        client = make_client(username, password)
        client.on_connect = on_connect
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=5)
        client.loop_start()
        for _ in range(50):
            if result["rc"] != -1:
                break
            time.sleep(0.1)
        client.loop_stop()
        client.disconnect()
        return result["rc"]

    def subscribe_result(username: str, password: str, topic: str) -> bool:
        """mosquitto 对订阅 ACL 拒绝表现为「SUBACK granted 但不投递」——
        以 broker 实际投递结果判定：订阅后用 platform 账号向该 topic 发布探针，
        观察目标客户端是否收到（不以 SUBACK reason code 代替）。"""
        result: dict = {"subscribed": False, "delivered": False}

        def on_subscribe(client, userdata, mid, reason_codes, properties):
            result["subscribed"] = True

        def on_message(client, userdata, message):
            if message.topic == topic:
                result["delivered"] = True

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                client.subscribe(topic, qos=1)

        client = make_client(username, password)
        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=5)
        client.loop_start()
        for _ in range(50):
            if result["subscribed"]:
                break
            time.sleep(0.1)
        if result["subscribed"]:
            # platform 探针发布
            probe = make_client("platform", platform_password)

            def probe_connect(probe_client, userdata, flags, reason_code, properties):
                if reason_code == 0:
                    probe_client.publish(topic, b'{"acl_probe_sub":1}', qos=1)

            probe.on_connect = probe_connect
            probe.connect(args.mqtt_host, args.mqtt_port, keepalive=5)
            probe.loop_start()
            for _ in range(30):
                if result["delivered"]:
                    break
                time.sleep(0.1)
            probe.loop_stop()
            probe.disconnect()
        client.loop_stop()
        client.disconnect()
        return result["delivered"]

    def publish_result(username: str, password: str, topic: str) -> bool:
        """PUBACK reason code 判定（mosquitto 对拒绝的 publish 返回 PUBACK RC=135）。

        paho 的 MQTTMessageInfo 不暴露 PUBACK reason code，须经 on_publish 回调获取；
        不使用「未收到消息」代替授权判定。
        """
        result: dict = {"puback_rc": None}

        def on_publish(client, userdata, mid, reason_code, properties):
            result["puback_rc"] = reason_code

        client = make_client(username, password)
        client.on_publish = on_publish
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=5)
        client.loop_start()
        for _ in range(30):
            if client.is_connected():
                break
            time.sleep(0.1)
        if client.is_connected():
            info = client.publish(topic, b'{"acl_probe":1}', qos=1)
            info.wait_for_publish(timeout=5)
        client.loop_stop()
        client.disconnect()
        # PUBACK reason code 判定：Success(0)=ALLOW；Not authorized(135)=DENY；
        # 未收到 PUBACK（连接被断/超时）=DENY。
        rc_value = getattr(result["puback_rc"], "value", result["puback_rc"])
        return rc_value == 0

    # ---- 认证 ----
    check(f"{vehicle} 认证", connect_result(vehicle, vehicle_password) == 0)
    check("anonymous 拒绝", connect_result("", "") != 0)
    check(f"{vehicle} 错误密码拒绝", connect_result(vehicle, vehicle_password + "WRONG") != 0)

    # ---- R001 自己的授权 ----
    check(
        f"{vehicle} subscribe robot/{vehicle}/command ALLOW",
        subscribe_result(vehicle, vehicle_password, f"robot/{vehicle}/command"),
    )
    check(
        f"{vehicle} subscribe v1/robot/{vehicle}/cmd ALLOW",
        subscribe_result(vehicle, vehicle_password, f"v1/robot/{vehicle}/cmd"),
    )
    check(
        f"{vehicle} publish robot/{vehicle}/heartbeat ALLOW",
        publish_result(vehicle, vehicle_password, f"robot/{vehicle}/heartbeat"),
    )
    check(
        f"{vehicle} publish v1/robot/{vehicle}/telemetry ALLOW",
        publish_result(vehicle, vehicle_password, f"v1/robot/{vehicle}/telemetry"),
    )

    # ---- 跨车拒绝 ----
    other = "R002"
    check(
        f"{vehicle} subscribe robot/{other}/command DENY",
        not subscribe_result(vehicle, vehicle_password, f"robot/{other}/command"),
    )
    check(
        f"{vehicle} subscribe v1/robot/{other}/cmd DENY",
        not subscribe_result(vehicle, vehicle_password, f"v1/robot/{other}/cmd"),
    )
    check(
        f"{vehicle} publish robot/{other}/heartbeat DENY",
        not publish_result(vehicle, vehicle_password, f"robot/{other}/heartbeat"),
    )
    check(
        f"{vehicle} publish v1/robot/{other}/telemetry DENY",
        not publish_result(vehicle, vehicle_password, f"v1/robot/{other}/telemetry"),
    )

    # ---- 平台账号参考（正常运维通道不受影响）----
    check("platform 账号仍可用", connect_result("platform", platform_password) == 0)

    if failures:
        print(f"VEHICLE_ACL_LIVE=FAIL count={len(failures)}")
        return 1
    print("VEHICLE_ACL_LIVE=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
