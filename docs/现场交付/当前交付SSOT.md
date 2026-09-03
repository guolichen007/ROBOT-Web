# 样机1 当前交付 SSOT（冻结）

> 本文件是 J7 现场交付封板时的**已验证事实冻结**。未验证项绝不写 PASS。
> 它只回答「现在已验证到什么程度」，不冒充未完成的验收。

## 一、已验证（FROZEN PASS）

```text
J6_S1_PRODUCTION          = PASS   # 服务器 STOP 状态机 + task-worker 生产部署路径
J6_V1_CONTROL_DEPLOY      = PASS   # 车端 Control 原子安装路径
J6_S2_MQTT_SOFTWARE_LOOP  = PASS   # Server MQTT→Bridge→ROS→Control Adapter→feedback→Bridge→MQTT ACK→Server 软件环
J6_V1C_VEHICLE            = PASS   # 车端 Bridge/Control 软件验收
```

样机2 测试 command：`J6S2-PATROL-c1a4c9056560`，端到端约 334ms。

## 二、未验证（禁止写 PASS）

```text
正式 Web/API Command lifecycle 到真实硬件
真实底盘 / 真实 odom / control_mode=3 / cmd_vel arbitration
真实 STOP / 真实 PATROL / 真实 location / 真实 battery/smoke
```

## 三、PATROL 当前范围（冻结）

```text
PATROL_START_VERIFIED_SCOPE = "启动现有 ROS 导航到首个有效巡检目标"
FULL_MULTI_WAYPOINT_PATROL  = NOT_IMPLEMENTED / NOT_FIELD_VERIFIED
```

> `firebot_control_adapter` 收到完整 trajectory 后 `_pick_target()` 只取首个有效目标，
> **不是**完整多航点巡检执行器。任何文档/UI 不得把它描述成「已完整自动巡检」。

## 四、控制状态语义（三种状态必须分开）

```text
PATROL_START：software path PASS，real motion verified = NO
STOP_MOTION ：code implemented，field verified = NO
emergency_stop / reset_estop：real implementation unavailable
```

- `code_implemented` ≠ `config_enabled` ≠ `field_verified`
- 真实运动前，`supported_commands` 只允许 `stop_motion`（现场独立开放，install 绝不自动改）

## 五、第一轮控制顺序（冻结，禁止先 PATROL 再补 STOP 验收）

```text
1 硬件上线 → 2 真实 /odom → 3 control_mode=3 → 4 /cmd_vel 拓扑 → 5 AMCL/map identity
→ 6 location uplink → 7 Server readiness facts → 8 supported_commands 只允许 stop_motion
→ 9 Control Adapter 启动 → 10 车辆静止态 Web STOP → 11 ACK accepted + 5 distinct fresh zero + STATIONARY_CONFIRMED
→ 12 才允许 patrol → 13 低速短距离 PATROL_START → 14 运动中 Web STOP → 15 再次 ACK + physical stationary confirmed
```

## 六、物理安全边界（冻结）

```text
Web STOP 不是物理急停。第一次运动必须：现场人员在场、物理急停可达、空旷路径、
低速、短距离、不载人、无人员位于运动区域。网络中断不能依赖 Web STOP。
```

## 七、四层模型（冻结）

```text
1 RELEASE       不可变 FINAL_SHA；Server/Web/Bridge/Control 所有车辆完全相同。
2 FLEET PROFILE 车型/场站公共非 secret 配置（integration/ros1/profiles/）。
3 DEVICE IDENTITY 每辆车唯一 DEVICE_ID（现场唯一人工输入）。
4 RUNTIME FACTS boot_id/odom/control_mode/cmd_vel/amcl 只检测、不手填。
```

## 八、Fleet 产品化（firebotctl 唯一入口）

现场只使用 `firebotctl`（`integration/ros1/firebotctl`），底层脚本保留为 module implementation。

新设备接入（只允许 Tailscale 登录 + DEVICE_ID）：

```bash
# 服务器（管理员）：
firebotctl fleet register firebot-vehicle-01   # 签发 per-device MQTT credential + 一次性 token

# 车端（现场）：
firebotctl vehicle enroll firebot-vehicle-01 --token <TOKEN> --password <PW>
firebotctl vehicle install --sha <FINAL_SHA>
firebotctl vehicle verify
```

返回 `CONTROL_ENABLED=NO`（安装 ≠ 启动 ≠ 运动）。

```text
FLEET_PROFILE            = firebot_ros1_standard_v1（版本化车型合同，不含身份/secret）
GENERATED_BRIDGE_CONFIG  = 是（/etc/firebot/bridge.env 标记 GENERATED - DO NOT EDIT）
AUTO_MQTT_CREDENTIAL     = per-device（username=DEVICE_ID + 随机 password，绝不 fleet 共用）
FLEET_IDENTITY_ISOLATION = topic namespace 由 DEVICE_ID 派生，两两不相交（unit 测试 PASS）
CONTROL_DEFAULT_DISABLED = 是（firebot-control systemd 安装默认 disabled）
ZERO_MANUAL_CONFIG       = 代码已就绪；需在样机2 clean rehearsal 现场验收（本机无法跑真机）
```

禁止：`vim/nano/sed -i` 改 bridge.env、手填 MQTT host/username/password、手改 supported_commands、现场 SQL update、现场改源码。
