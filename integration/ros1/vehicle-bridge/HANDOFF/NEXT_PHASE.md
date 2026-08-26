# 下一阶段（需用户另行批准后才能执行）

## Phase E1 = read-only ROS source discovery

只在用户再次批准后执行。

允许候选：

```text
rostopic list
rostopic info
rostopic type
rostopic hz
少量 rostopic echo
```

禁止：

```text
rostopic pub
rosservice call
roslaunch
restart ROS
kill ROS
修改 ROS code
vehicle control
```

## 目标

寻找真实：

```text
battery
status
location / odom
smoke
```

## 原则

```text
真实数据优先。
不造 0。
不造假 battery。
一次只接一个 provider。
```

## 数据路线

```text
real ROS source
→ canonical Bridge provider
→ Vehicle Bridge
→ MQTT
→ Server
→ Web
```

控制（下行 real control）最后处理，不属于本阶段。
