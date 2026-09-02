<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import { api, errorMessage } from '@/lib/api'
import { localizationLabel, sourceKindLabel, supportStateLabel } from '@/lib/ui-labels'
import { effectiveChannelSupportState, telemetryValueLabel } from '@/lib/telemetry-health'
import { useSystemClock } from '@/composables/useSystemClock'
import type { RobotState } from '@/types'

const auth = useAuthStore()
const monitor = useMonitorStore()
const router = useRouter()

const expandedId = ref<string | null>(null)
const confirmDisable = ref<string | null>(null)
const busyVehicleId = ref('')
const notice = ref('')

const sortedRobots = computed(() =>
  [...monitor.snapshot.robots].sort((left, right) => left.vehicle_id.localeCompare(right.vehicle_id)),
)

const CHANNEL_LABELS: Array<[string, string]> = [
  ['heartbeat', '心跳'],
  ['pose', '定位'],
  ['odom', '里程计'],
  ['battery', '电量'],
  ['smoke', '烟雾'],
  ['top_ir', '上红外'],
  ['bottom_ir', '下红外'],
  ['estop', '急停'],
  ['roof_rgb', '车顶相机'],
  ['roof_thermal', '顶部热像'],
]

function isCurrent(robot: RobotState): boolean {
  return monitor.activeRobotId === robot.vehicle_id || monitor.activeRobotId === robot.id
}

function toggleExpand(robot: RobotState): void {
  expandedId.value = expandedId.value === robot.vehicle_id ? null : robot.vehicle_id
}

function onlineLabel(state?: string): string {
  if (state === 'ONLINE') return '在线'
  if (state === 'STALE') return '数据陈旧'
  if (state === 'OFFLINE') return '离线'
  return '未知'
}

function relativeTime(value?: string | null): string {
  if (!value) return '--'
  const ms = Date.parse(value)
  if (Number.isNaN(ms)) return '--'
  const seconds = Math.max(0, (Date.now() - ms) / 1000)
  if (seconds < 60) return `${seconds.toFixed(1)} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  return `${Math.floor(seconds / 3600)} 小时前`
}

function lastComm(robot: RobotState): string {
  return relativeTime(robot.server_received_at || robot.last_seen_at)
}

const { now } = useSystemClock()
const nowMs = computed(() => now.value?.getTime() ?? Date.now())
// 字段级 effective state 随 now 每秒重派生（与 server channel_freshness.py 一致）
function channelState(robot: RobotState, key: string): string | undefined {
  return effectiveChannelSupportState(
    robot.data_channels?.[key],
    robot.integration?.stale_seconds ?? null,
    robot.integration?.offline_seconds ?? null,
    nowMs.value,
  )
}

function batteryLabel(robot: RobotState): string {
  return telemetryValueLabel(robot.battery, channelState(robot, 'battery'), (v) => `${v.toFixed(0)}%`)
}

function modeLabel(robot: RobotState): string {
  return robot.mode || robot.current_mode || '--'
}

function isReadOnly(robot: RobotState): boolean {
  return robot.integration?.source_kind === 'ROS_COMPAT' && robot.control_enabled !== true
}

function capabilityRows(robot: RobotState): Array<{ label: string; ok: boolean }> {
  return [
    { label: '巡检', ok: robot.autonomous_task_ready?.patrol === true },
    { label: '返回等待区', ok: robot.autonomous_task_ready?.return_dock === true },
    { label: '停止', ok: robot.safety_command_ready?.stop_motion === true },
    { label: '软件急停', ok: robot.safety_command_ready?.emergency_stop === true },
    { label: '解除急停', ok: robot.safety_command_ready?.reset_estop === true },
    { label: '灭火', ok: robot.autonomous_task_ready?.extinguish === true },
  ]
}

function switchMonitor(robot: RobotState): void {
  if (monitor.selectRobot(robot.vehicle_id)) toast(`已切换当前监控车辆：${robot.vehicle_id}`)
}

function enterMonitor(robot: RobotState): void {
  if (monitor.selectRobot(robot.vehicle_id)) void router.push('/monitor')
}

function requestDisable(robot: RobotState): void {
  confirmDisable.value = robot.vehicle_id
}

async function setEnabled(robot: RobotState, enabled: boolean): Promise<void> {
  busyVehicleId.value = robot.vehicle_id
  try {
    await api.put(`/robots/${robot.vehicle_id}/enabled`, { enabled })
    confirmDisable.value = null
    await monitor.loadSnapshot()
    toast(enabled ? `${robot.vehicle_id} 平台接入已启用` : `${robot.vehicle_id} 平台接入已停用`)
  } catch (reason) {
    toast(errorMessage(reason))
  } finally {
    busyVehicleId.value = ''
  }
}

function toast(value: string): void {
  notice.value = value
  window.setTimeout(() => {
    if (notice.value === value) notice.value = ''
  }, 4000)
}

onMounted(() => {
  if (!monitor.connected && !monitor.resyncing) void monitor.start()
})
</script>

<template>
  <PageHeader
    eyebrow="设备管理"
    title="车辆配置"
    description="管理已接入平台的机器人，选择当前监控车辆。展开查看接入、传感器、地图与控制能力。"
  />
  <div v-if="notice" class="toast">{{ notice }}</div>

  <section class="panel vehicle-panel">
    <div v-if="sortedRobots.length === 0" class="empty-state">
      <div><strong>暂无已注册车辆</strong></div>
      <span>车辆接入上报后会自动出现在这里。</span>
    </div>

    <div
      v-for="robot in sortedRobots"
      :key="robot.vehicle_id"
      class="vehicle-row"
      :class="{ current: isCurrent(robot), disabled: robot.enabled === false }"
    >
      <div class="vehicle-row-main" @click="toggleExpand(robot)">
        <span class="current-pill" :class="{ visible: isCurrent(robot) }">● 当前监控</span>
        <div class="vehicle-identity">
          <strong>{{ robot.vehicle_id }}</strong>
          <small>{{ robot.name || '—' }}</small>
        </div>
        <span class="source-chip" :data-kind="robot.integration?.source_kind || 'NONE'">
          {{ sourceKindLabel(robot.integration?.source_kind) }}
        </span>
        <span class="row-metric online">
          <i class="online-dot" :class="String(robot.online_state || 'unknown').toLowerCase()"></i>
          {{ onlineLabel(robot.online_state) }}
        </span>
        <span class="row-metric"
          >电量 <b>{{ batteryLabel(robot) }}</b></span
        >
        <span class="row-metric"
          >模式 <b>{{ modeLabel(robot) }}</b></span
        >
        <span class="row-metric"
          >最后通信 <b>{{ lastComm(robot) }}</b></span
        >
        <span v-if="robot.enabled === false" class="row-metric disabled-badge">平台已停用</span>
        <div class="vehicle-actions" @click.stop>
          <button v-if="isCurrent(robot)" class="secondary-button compact" disabled>当前车辆</button>
          <button
            v-else-if="robot.enabled !== false"
            class="secondary-button compact"
            type="button"
            @click="switchMonitor(robot)"
          >
            切换监控
          </button>
          <button class="secondary-button compact" type="button" @click="toggleExpand(robot)">
            {{ expandedId === robot.vehicle_id ? '收起 ▲' : '详情 ▼' }}
          </button>
        </div>
      </div>

      <div v-if="expandedId === robot.vehicle_id" class="vehicle-detail">
        <div class="vehicle-detail-grid">
          <div class="detail-group">
            <h4>车辆状态</h4>
            <dl>
              <div>
                <dt>车辆名称</dt>
                <dd>{{ robot.name || '—' }}</dd>
              </div>
              <div>
                <dt>Vehicle ID</dt>
                <dd class="mono">{{ robot.vehicle_id }}</dd>
              </div>
              <div>
                <dt>型号</dt>
                <dd>{{ robot.model || '—' }}</dd>
              </div>
              <div>
                <dt>平台状态</dt>
                <dd>{{ robot.enabled === false ? '已停用' : '已启用' }}</dd>
              </div>
              <div>
                <dt>连接状态</dt>
                <dd>{{ onlineLabel(robot.online_state) }}</dd>
              </div>
              <div>
                <dt>当前模式</dt>
                <dd>{{ modeLabel(robot) }}</dd>
              </div>
              <div>
                <dt>电量</dt>
                <dd>{{ batteryLabel(robot) }}</dd>
              </div>
              <div>
                <dt>软件急停</dt>
                <dd>{{ robot.estop_active ? '已触发' : '未触发' }}</dd>
              </div>
              <div>
                <dt>当前任务</dt>
                <dd class="mono">
                  {{ monitor.activeTaskOf(robot.id || robot.vehicle_id)?.task_code || '无' }}
                </dd>
              </div>
              <div>
                <dt>最后通信</dt>
                <dd>{{ lastComm(robot) }}</dd>
              </div>
            </dl>
          </div>

          <div class="detail-group">
            <h4>数据接入</h4>
            <dl>
              <div v-for="[key, label] in CHANNEL_LABELS" :key="key">
                <dt>{{ label }}</dt>
                <dd :data-support="channelState(robot, key)">
                  {{ supportStateLabel(channelState(robot, key)) }}
                </dd>
              </div>
            </dl>
          </div>

          <div class="detail-group">
            <h4>地图与定位</h4>
            <dl>
              <div>
                <dt>当前地图</dt>
                <dd>{{ robot.integration?.reported_map_code || robot.current_map_version || '—' }}</dd>
              </div>
              <div>
                <dt>版本</dt>
                <dd>{{ robot.integration?.reported_map_version || robot.current_map_version || '—' }}</dd>
              </div>
              <div>
                <dt>定位状态</dt>
                <dd>{{ localizationLabel(robot.localization_status) }}</dd>
              </div>
              <div>
                <dt>地图合同</dt>
                <dd>{{ robot.integration?.map_contract_verified ? '已验证' : '未验证' }}</dd>
              </div>
            </dl>
          </div>

          <div class="detail-group">
            <h4>控制能力</h4>
            <ul class="capability-list">
              <li v-for="cap in capabilityRows(robot)" :key="cap.label" :class="{ ok: cap.ok }">
                <i></i><span>{{ cap.label }}</span>
              </li>
            </ul>
            <p v-if="isReadOnly(robot)" class="readonly-note">
              只读接入{{
                robot.integration?.read_only_reason ? `：${robot.integration.read_only_reason}` : ''
              }}
            </p>
          </div>
        </div>

        <div class="vehicle-detail-foot">
          <div class="source-line">
            <span>数据来源：{{ sourceKindLabel(robot.integration?.source_kind) }}</span>
            <span>协议版本：{{ robot.integration?.upstream_protocol || '—' }}</span>
          </div>
          <div class="detail-actions">
            <button
              v-if="robot.enabled !== false"
              class="primary-button compact"
              type="button"
              @click="enterMonitor(robot)"
            >
              进入实时监控
            </button>
            <template v-if="auth.can('settings.manage')">
              <button
                v-if="robot.enabled !== false && confirmDisable !== robot.vehicle_id"
                class="danger-outline compact"
                type="button"
                :disabled="busyVehicleId === robot.vehicle_id"
                @click="requestDisable(robot)"
              >
                停用平台操作
              </button>
              <button
                v-else-if="robot.enabled === false"
                class="secondary-button compact"
                type="button"
                :disabled="busyVehicleId === robot.vehicle_id"
                @click="setEnabled(robot, true)"
              >
                启用平台操作
              </button>
            </template>
          </div>

          <div v-if="confirmDisable === robot.vehicle_id" class="disable-confirm">
            <strong>停用 {{ robot.name || robot.vehicle_id }} 的平台接入？</strong>
            <ul>
              <li>车辆历史数据不会删除</li>
              <li>平台不会再向该车辆创建新的控制任务</li>
              <li>此操作不会使正在运动的实体车辆立即停车</li>
            </ul>
            <div class="disable-confirm-actions">
              <button class="danger-outline compact" type="button" @click="setEnabled(robot, false)">
                确认停用
              </button>
              <button class="secondary-button compact" type="button" @click="confirmDisable = null">
                取消
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
