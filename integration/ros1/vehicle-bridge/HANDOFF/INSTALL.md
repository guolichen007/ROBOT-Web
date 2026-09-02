# 车端「只 Pull + 安装」流程（中文）

> 车端代码的**唯一源码权威**在 GitHub `guolichen007/ROBOT-Web` 仓库：
>
> - `integration/ros1/vehicle-bridge/` → 安装到 `/opt/firebot/vehicle-bridge/`
> - `integration/ros1/vehicle-control/` → 部署到 `/home/tl/firerobot_ws/src/firebot_control/`
>
> 现场部署目录只是某个 Git SHA 的运行副本，禁止现场手改源码。

## 一、固定原则

1. **不覆盖 secret**：`/etc/firebot/bridge.env` 与 `/etc/firebot/bridge-secret.env` 已存在时一律保留，install.sh 绝不覆盖、绝不写密码进 Git。
2. **安装前校验**：可用 `FIREBOT_REQUIRE_SHA` 强制校验源码 HEAD，防止装错版本。
3. **安装后留痕**：install.sh 会把来源 SHA 写入 `/opt/firebot/vehicle-bridge/APPROVED_RUNTIME.txt`。
4. **可回滚**：任何时刻 `git checkout <目标SHA>` 后重新安装即可回到指定 ROBOT-Web SHA。

## 二、只 Pull + 安装（推荐）

```bash
cd <ROBOT-Web 仓库根>
git fetch origin
git checkout integration/server-web-real-vehicle-ready-v1
git pull --ff-only origin integration/server-web-real-vehicle-ready-v1
# 记录当前 SHA（安装后核对）
git rev-parse HEAD

cd integration/ros1/vehicle-bridge
FIREBOT_REQUIRE_SHA=<上面记录的40位SHA> \
FIREBOT_ROS_SETUP=/opt/ros/noetic/setup.bash \
FIREBOT_ROS_WORKSPACE_SETUP=/home/tl/firerobot_ws/devel/setup.bash \
./install.sh
```

安装后确认来源 SHA：

```bash
cat /opt/firebot/vehicle-bridge/APPROVED_RUNTIME.txt   # 应等于 FIREBOT_REQUIRE_SHA
```

## 三、回滚到指定 SHA

```bash
cd <ROBOT-Web 仓库根>
git checkout <目标SHA>
cd integration/ros1/vehicle-bridge
FIREBOT_REQUIRE_SHA=<目标SHA> ./install.sh
sudo systemctl restart firebot-bridge
./verify.sh
```

## 四、firebot_control ROS 包部署

`firebot_control` 是 catkin 包，部署到 ROS 工作区 `src` 后重新 catkin_make：

```bash
cp -r integration/ros1/vehicle-control /home/tl/firerobot_ws/src/firebot_control
cd /home/tl/firerobot_ws && catkin_make && source devel/setup.bash
```

## 五、安装后验收

```bash
FIREBOT_BRIDGE_ENV=/etc/firebot/bridge.env /opt/firebot/vehicle-bridge/verify.sh
```

重点核对：`SECRET_PRESENT=YES`、`SUPPORTED_COMMANDS=`（生产不开放控制）、`REAL_CONTROL=NOT_IMPLEMENTED`。
