#!/usr/bin/env python3
"""firebot_control_adapter — 最小控制 adapter（Phase R1-MOTION，fail-closed）。

只做一件事：
  订阅 /firebot_bridge/command（std_msgs/String JSON），收到 PATROL_START 后：
    1) 检查 downstream readiness（/waterplus/navi_pose subscriber>0 + move_base action server 可用）
    2) 调用车辆现有「开始导航」入口（waterplus/navi_pose → pose_navi_server → move_base）
    3) 先回 ACCEPTED（表示控制 adapter 已接受）
    4) 等最多 ~2s 观察 move_base 是否真正进入 ACTIVE/PENDING
    5) 确认导航真正启动才回 EXECUTING(phase=PATROLLING)，否则回 FAILED(NAV_START_NOT_CONFIRMED)

边界：
  - 不实现巡检规划、不自己写 move_base 逻辑、不解析多点轨迹。
  - 本轮能力：PATROL_START（开始导航）+ STOP_MOTION（零速度停车 + 取消导航目标）。
    其它命令（emergency_stop / reset_estop / ...）直接忽略。
  - command_id / task_id 原样回传。
  - fail-closed：绝不把「发布 /waterplus/navi_pose」等同于「导航已开始」；
    STOP_MOTION 的 ACK 只表示「控制层接受并执行停止动作」，不表示物理车辆已被服务器确认静止。
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone

import rospy
import actionlib
from std_msgs.msg import String
from geometry_msgs.msg import Pose, Twist
from actionlib_msgs.msg import GoalStatus, GoalStatusArray
from move_base_msgs.msg import MoveBaseAction

# 现有导航入口：waterplus pose_navi_server 订阅的「按坐标导航」话题
NAV_POSE_TOPIC = "/waterplus/navi_pose"
MOVE_BASE_ACTION = "move_base"
# 车端现有底盘速度通路（STOP_MOTION 通过它输出零速度停车）
CMD_VEL_TOPIC = "/cmd_vel"
# move_base 真正「开始导航」的状态
MB_ACTIVE_STATES = (GoalStatus.PENDING, GoalStatus.ACTIVE)


class FirebotControlAdapter:
    def __init__(self):
        rospy.init_node("firebot_control_adapter")
        self.feedback_pub = rospy.Publisher(
            "/firebot_bridge/command_feedback", String, queue_size=10
        )
        self.navi_pose_pub = rospy.Publisher(NAV_POSE_TOPIC, Pose, queue_size=10)
        self.cmd_vel_pub = rospy.Publisher(CMD_VEL_TOPIC, Twist, queue_size=10)

        # move_base action server 探测（只用于 readiness 检查，不发 goal）
        self._mb_client = actionlib.SimpleActionClient(
            MOVE_BASE_ACTION, MoveBaseAction
        )

        # 观察 move_base 是否真正进入 ACTIVE/PENDING（供 EXECUTING 确认）
        self._mb_active_marker = None  # 最近一次看到 ACTIVE/PENDING 的 rospy 时间
        rospy.Subscriber("/move_base/status", GoalStatusArray, self._on_mb_status)

        rospy.Subscriber("/firebot_bridge/command", String, self._on_command)
        rospy.loginfo(
            "firebot_control_adapter 就绪（fail-closed）, 导航入口=%s", NAV_POSE_TOPIC
        )

    # ---- move_base 状态观察 ----
    def _on_mb_status(self, msg):
        now = rospy.get_time()
        for st in msg.status_list:
            if st.status in MB_ACTIVE_STATES:
                self._mb_active_marker = now
                return

    # ---- 下行命令 ----
    def _on_command(self, msg):
        try:
            cmd = json.loads(msg.data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(cmd, dict):
            return
        if cmd.get("command") == "PATROL_START":
            self._handle_patrol_start(cmd)
        elif cmd.get("command") == "STOP_MOTION":
            self._handle_stop_motion(cmd)
        # 其它命令直接忽略（本轮能力只有 patrol / stop_motion）

    def _handle_patrol_start(self, cmd):
        command_id = cmd.get("command_id")
        task_id = cmd.get("task_id")

        # 过期命令不执行
        if self._expired(cmd.get("expires_at")):
            self._feedback(command_id, task_id, "REJECTED", reason_code="COMMAND_EXPIRED")
            return

        # 第一步：downstream readiness（fail-closed，不满足绝不发 EXECUTING）
        ready, why = self._downstream_ready()
        if not ready:
            self._feedback(
                command_id, task_id, "REJECTED",
                reason_code="COMMAND_REJECTED", message=why,
            )
            return

        # 第二步：发布 /waterplus/navi_pose（调用现有导航入口）
        if not self._start_navigation(cmd):
            self._feedback(
                command_id, task_id, "REJECTED", reason_code="NAV_START_FAILED"
            )
            return

        # 第三步：先回 ACCEPTED（控制 adapter 已接受）
        self._feedback(command_id, task_id, "ACCEPTED")

        # 第四步：等最多 ~2s 观察 move_base 是否真正启动导航
        if self._wait_move_base_active(timeout=2.0):
            self._feedback(
                command_id, task_id, "EXECUTING", phase="PATROLLING", progress=0
            )
        else:
            self._feedback(
                command_id, task_id, "FAILED",
                reason_code="COMMAND_REJECTED", message="NAV_START_NOT_CONFIRMED",
            )

    def _handle_stop_motion(self, cmd):
        """STOP_MOTION：取消导航目标 + 短时零速度 burst，全部成功后才回 ACCEPTED。

        ACK 只表示「控制层已成功执行停车动作请求」；绝不表示「车辆已物理静止」。
        如果停车动作本身无法可靠下发（无 /cmd_vel 接收者、cancel 异常、零速度输出异常），
        一律 REJECTED，不伪装成功。
        """
        command_id = cmd.get("command_id")
        task_id = cmd.get("task_id")
        if self._expired(cmd.get("expires_at")):
            self._feedback(command_id, task_id, "REJECTED", reason_code="COMMAND_EXPIRED")
            return

        # 1) 前置：/cmd_vel 必须存在实际底盘接收者，否则停车动作无处可去。
        if not self._cmd_vel_has_receiver():
            self._feedback(
                command_id, task_id, "REJECTED",
                reason_code="COMMAND_REJECTED", message="CMD_VEL_NO_RECEIVER",
            )
            return

        # 2) 取消 move_base 当前导航目标；异常不得继续伪装成功。
        try:
            self._mb_client.cancel_all_goals()
        except Exception as exc:  # noqa: BLE001
            rospy.logwarn("STOP_MOTION cancel_all_goals 失败: %s", exc)
            self._feedback(
                command_id, task_id, "REJECTED",
                reason_code="COMMAND_REJECTED", message="CANCEL_GOALS_FAILED",
            )
            return

        # 3) 短时有限零速度 burst，覆盖 move_base cancel 竞争窗口；异常不得继续。
        if not self._publish_zero_velocity_burst():
            self._feedback(
                command_id, task_id, "REJECTED",
                reason_code="COMMAND_REJECTED", message="ZERO_VELOCITY_FAILED",
            )
            return

        # 4) 全部停车动作成功下发 → ACCEPTED（仅表示动作已可靠执行）。
        self._feedback(command_id, task_id, "ACCEPTED")

    def _cmd_vel_has_receiver(self) -> bool:
        try:
            return self.cmd_vel_pub.get_num_connections() > 0
        except Exception:  # noqa: BLE001
            return False

    def _publish_zero_velocity_burst(self, rate_hz: float = 10.0, duration_s: float = 0.5) -> bool:
        """10Hz 持续 0.5s 的零速度 burst，覆盖 move_base cancel 的短暂竞争窗口。"""
        twist = Twist()
        twist.linear.x = 0.0
        twist.linear.y = 0.0
        twist.linear.z = 0.0
        twist.angular.x = 0.0
        twist.angular.y = 0.0
        twist.angular.z = 0.0
        count = int(rate_hz * duration_s)
        try:
            rate = rospy.Rate(rate_hz)
            for _ in range(count):
                self.cmd_vel_pub.publish(twist)
                rate.sleep()
            rospy.loginfo("STOP_MOTION 零速度 burst 已输出到 %s", CMD_VEL_TOPIC)
            return True
        except Exception as exc:  # noqa: BLE001
            rospy.logwarn("STOP_MOTION 零速度 burst 失败: %s", exc)
            return False

    # ---- downstream readiness ----
    def _downstream_ready(self):
        """返回 (bool, reason)。/waterplus/navi_pose 有 subscriber 且 move_base action 可用。"""
        if self._count_navi_pose_subscribers() <= 0:
            return False, "NAV_EXECUTION_NOT_READY"
        if not self._move_base_ready():
            return False, "NAV_EXECUTION_NOT_READY"
        return True, None

    def _move_base_ready(self):
        try:
            return self._mb_client.wait_for_server(timeout=rospy.Duration(2.0))
        except Exception:  # noqa: BLE001
            return False

    def _count_navi_pose_subscribers(self):
        try:
            out = subprocess.check_output(
                ["rostopic", "info", NAV_POSE_TOPIC],
                stderr=subprocess.DEVNULL, timeout=3.0,
            ).decode()
        except Exception:  # noqa: BLE001
            return 0
        in_sub = False
        count = 0
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Subscribers:"):
                in_sub = True
                continue
            if in_sub:
                if s.startswith("Publishers:"):
                    break
                if s.startswith("* "):
                    count += 1
        return count

    def _wait_move_base_active(self, timeout=2.0):
        """观察 move_base 是否在发布 navi_pose 后真正进入 ACTIVE/PENDING。"""
        before = self._mb_active_marker
        deadline = rospy.get_time() + timeout
        while rospy.get_time() < deadline:
            cur = self._mb_active_marker
            if cur is not None and (before is None or cur > before + 0.05):
                return True
            rospy.sleep(0.1)
        return False

    # ---- 调用现有导航入口 ----
    def _start_navigation(self, cmd):
        params = cmd.get("params") or {}
        trajectory = params.get("trajectory") or []
        wp = self._pick_target(trajectory)
        if wp is None:
            rospy.logwarn("PATROL_START 无可用航点，不发布导航目标")
            return False
        pose = self._to_pose(wp)
        self.navi_pose_pub.publish(pose)
        rospy.loginfo(
            "NAV_START_CALLED -> %s (x=%.3f, y=%.3f, theta=%.3f)",
            NAV_POSE_TOPIC, pose.position.x, pose.position.y, wp.get("theta", 0.0),
        )
        return True

    def _pick_target(self, trajectory):
        """取第一个非 WAITING 航点（避免导航回起点）。"""
        for wp in trajectory:
            if isinstance(wp, dict) and wp.get("kind") != "WAITING":
                return wp
        return trajectory[0] if trajectory else None

    def _to_pose(self, wp):
        pose = Pose()
        pose.position.x = float(wp.get("x", 0.0))
        pose.position.y = float(wp.get("y", 0.0))
        pose.position.z = 0.0
        theta = float(wp.get("theta", 0.0))
        pose.orientation.z = math.sin(theta / 2.0)
        pose.orientation.w = math.cos(theta / 2.0)
        return pose

    def _expired(self, expires_at):
        if not expires_at:
            return False
        try:
            exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            return exp <= datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False

    # ---- 上行 feedback ----
    def _feedback(self, command_id, task_id, state, **kw):
        fb = {"command_id": command_id, "task_id": task_id, "state": state}
        fb.update(kw)
        self.feedback_pub.publish(String(data=json.dumps(fb, ensure_ascii=False)))
        rospy.loginfo("FEEDBACK -> state=%s command_id=%s", state, command_id)


if __name__ == "__main__":
    try:
        FirebotControlAdapter()
        rospy.spin()
    except rospy.ROSInterruptException:
        pass
