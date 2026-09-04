# Firebot 车端 ROS Bridge（通信层 · schema 1.3）

> 版本：1.3.0
> 定位：**Vehicle Bridge 组件/开发 README**。Bridge 只做**通信 / 协议 / 安全网关**，不实现真实运动。
> 现场唯一入口是 `firebotctl`（`vehicle enroll` → `vehicle install` → `vehicle verify`）；完整现场流程见 [车端部署与实车接口](../../../docs/部署运维/车端部署与实车接口.md)，当前状态真相源见 [当前状态](../../../docs/当前状态/当前状态.md)。
> 协议契约见 [FIREBOT_BRIDGE_CONTRACT_1.3](../../../docs/技术合同/FIREBOT_BRIDGE_CONTRACT_1.3.md)。

## 职责

```text
服务器 Web ── MQTT/TLS ── 本 Bridge ── /firebot_bridge/* ── 车端控制层
     上行: battery/smoke/heartbeat/...      下行: patrol/stop/estop/...
```

Bridge 只做 5 件事：MQTT 连接、协议校验、上行数据封装、下行命令转交、ROS 反馈重新封装。
**它不知道"怎么巡航"**——只把"服务器要求开始巡航"可靠转交给 ROS 控制层。

当前控制能力：`CONTROL_CODE=PATROL_START,STOP_MOTION`（`vehicle-control` 实现，范围有限）；`CONTROL_FIELD_VERIFIED=NO`，真实物理运动仍待现场验收。

**MQTT 与 ROS master 解耦**：MQTT/TLS 生命周期不依赖 roscore——无 roscore 时 MQTT 仍在线；
production 默认 `supported_commands=[]`，此时任何命令都在 validator 回 `COMMAND_UNSUPPORTED`
（不会转发 ROS）；只有命令已通过 capability 校验并成功转发 ROS 后无 feedback，才回 `BRIDGE_ADAPTER_NOT_CONNECTED`。
初始 MQTT 连接失败在进程内指数退避重试，不靠 systemd 重启。

**battery canonical 来源**：`/firebot_bridge/battery`（std_msgs/Float32），由车端 provider/adapter 发布；
没有 provider 时不伪造电量（`BATTERY_PROVIDER=NOT_AVAILABLE`）。

## 目录结构

```text
vehicle-bridge/
├── firebot_bridge/            # Bridge runtime（协议 / 上行 / 下行 / ROS 契约）
├── config/bridge.env.example  # 配置模板（无密码）
├── systemd/firebot-bridge.service.template  # systemd 模板
├── install.sh / uninstall.sh / verify.sh    # 模块实现（被 vehicle-install.sh 调用，非现场标准入口）
├── run_bridge.sh              # 启动（debug/foreground 用，正式走 systemd）
├── watch-bridge.sh            # 现场实时控制台（观察者）
├── tools/field_console.py     # 终端 viewer
├── requirements.txt
└── tests/
```

## 现场部署（标准流程）

标准现场安装/验证**只走 `firebotctl`**：

```text
firebotctl vehicle enroll <DEVICE_ID> --token <TOKEN>
firebotctl vehicle install --sha <FINAL_SHA>
firebotctl vehicle verify
```

`firebotctl vehicle install` 内部会调用 `vehicle-install.sh`（进而调用本目录 `install.sh`）完成原子安装与 `APPROVED_RUNTIME.txt` 留痕。**现场人员不得直接 `nano /etc/firebot/bridge.env`、`sed -i`、手填 MQTT credential**——这些是模块实现 / 开发 debug 手段，不是标准现场安装入口。

安装产物：`/opt/firebot/vehicle-bridge`（systemd `firebot-bridge.service` 运行）。

## 模块实现 / 开发 debug（非现场标准入口）

以下命令仅供模块开发与排障，不是现场标准安装入口：

```bash
# 依赖
pip3 install -r requirements.txt

# 直接安装（等价于 vehicle-install.sh 内部调用；现场勿直接使用）
FIREBOT_ROS_SETUP=/opt/ros/noetic/setup.bash \
FIREBOT_ROS_WORKSPACE_SETUP=/home/tl/firerobot_ws/devel/setup.bash \
./install.sh

# 卸载 / 验收
./uninstall.sh
FIREBOT_BRIDGE_ENV=/etc/firebot/bridge.env ./verify.sh
```

`/etc/firebot/bridge.env` 是唯一配置文件（secret 在 `/etc/firebot/bridge-secret.env`，root:root 600），由 install.sh 生成；禁止部署时覆盖已有 CA / secret。

## 关键配置（config/bridge.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FIREBOT_MQTT_USERNAME` | firebot-vehicle-01 | 车端账号（canonical ID） |
| `FIREBOT_VEHICLE_ID` | firebot-vehicle-01 | canonical vehicle_id |
| `BRIDGE_STUB_MODE` | false | **生产必须 false**；true=联调 |
| `FIREBOT_SUPPORTED_COMMANDS` | 空 | 只声明真实能力；未接 ROS 不声明 |
| `FIREBOT_SENSORS` | 空 | 唯一权威传感器声明；有真实 smoke 源才声明 `smoke` |
| `FIREBOT_LOCATION_ENABLED` | false | location 上行门控；地图身份真实确认前保持 false |
| `FIREBOT_FEEDBACK_TIMEOUT_SECONDS` | 3 | 无 ROS feedback 时回 rejected 的超时 |

## 命令处理语义（重要）

- 生产模式：收到命令 → 校验/去重 → 转发 `/firebot_bridge/command` → 等 ROS `command_feedback`。
  **只有 ROS 明确回 ACCEPTED 后 Bridge 才回 MQTT accepted**；超时无 feedback → `rejected` + `BRIDGE_ADAPTER_NOT_CONNECTED`。
- 所有命令（含 emergency_stop）只转发，**不做任何执行**。
- `BRIDGE_STUB_MODE=true`（联调）：可临时声明测试命令、模拟 feedback，仅供消息闭环联调。

## 测试

```bash
cd vehicle-bridge && python3 tests/test_bridge.py
```

## 现场实时通信控制台

Bridge 输出无 ANSI 的结构化 `FBTRACE` 事件（journal），终端渲染由独立 viewer 完成。
现场人员不需要懂 MQTT / rospy / Python，只看 `● ○ × RX TX` 就能判断信号卡在哪一段链路。

```bash
# 状态快照（一次性）
FIREBOT_BRIDGE_ENV=/etc/firebot/bridge.env /opt/firebot/vehicle-bridge/verify.sh

# 实时控制台（append-only，SSH 友好）
/opt/firebot/vehicle-bridge/watch-bridge.sh
```

`watch-bridge.sh` 只是观察者：Ctrl+C 只退出 viewer，**不会**停止 `firebot-bridge.service`。

## ⚠️ 禁止双 Bridge

不要在 `firebot-bridge` service 处于 `active` 时再手动运行 `./run_bridge.sh`——相同的
MQTT client identity 会互相踢线。如确需 foreground 调试：

```bash
sudo systemctl stop firebot-bridge
systemctl is-active firebot-bridge   # 确认不是 active 后
./run_bridge.sh
# 调试结束：
sudo systemctl start firebot-bridge
```

## 验收边界

`PATROL_START`/`STOP_MOTION` 代码已实现（`CONTROL_CODE`），但 `CONTROL_FIELD_VERIFIED=NO`；`emergency_stop`/`reset_estop` 未实现。
绝不因 MQTT 命令到达就标记"真实控制已验证"。
