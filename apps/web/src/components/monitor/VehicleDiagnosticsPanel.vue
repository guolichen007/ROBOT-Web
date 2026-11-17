<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useAuthStore } from '@/stores/auth'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import {
  SOFTWARE_PATROL_MAX_ROUNDS,
  clampRounds,
  commandStages,
  diagnosticActions,
  durationLabel,
  isActive,
  pollBackoff,
  reasonText,
  softwareActions,
  type DiagnosticAction,
} from '@/lib/vehicle-diagnostics'
import type { AgentCommand } from '@/types'

// G2：车端诊断与 ROS 软件测试（仅逐车执行档授权的车辆显示）。按钮调用真实鉴权 API
// （Idempotency-Key），状态完全来自服务端 Command 事实；不在前端模拟任何执行结果。
// 历史从服务端按当前车辆读取（刷新后可恢复，含其他用户/API 发起的合法命令）；
// 下行关闭时只读可见。真实底盘/急停/灭火门禁不在本组件。
const HISTORY_LIMIT = 20
const IDLE_POLL_MS = 15_000
const ACTIVE_POLL_MS = 2_000

const monitor = useMonitorStore()
const auth = useAuthStore()
const sending = ref('')
const error = ref('')
const historyIds = ref<string[]>([])
const historyError = ref('')
const rounds = ref(1)
let pollTimer = 0
let failures = 0
let generation = 0

const execution = computed(() => monitor.robot?.vehicle_agent_execution)
const visible = computed(
  () =>
    monitor.robot?.agent_protocol === 'VEHICLE_AGENT_V1' &&
    Boolean(execution.value?.profile && Object.keys(execution.value.commands).length),
)
const rows = computed(() =>
  historyIds.value
    .map((id) => monitor.snapshot.commands?.find((row) => row.command_id === id))
    .filter((row): row is AgentCommand => Boolean(row)),
)
const software = computed(() => monitor.robot?.agent_state?.status?.software ?? undefined)
const simulated = computed(() => monitor.robot?.simulation?.simulated === true)
const downlinkClosed = computed(() =>
  Object.values(execution.value?.commands ?? {}).some(
    (entry) =>
      entry.reason === 'VEHICLE_AGENT_DOWNLINK_DISABLED' ||
      entry.reason === 'VEHICLE_NOT_IN_DOWNLINK_ALLOWLIST',
  ),
)

function granted(action: DiagnosticAction): boolean {
  return Boolean(execution.value?.commands?.[action])
}

function permitted(action: DiagnosticAction): boolean {
  if (['ping', 'get_status', 'get_telemetry'].includes(action)) return auth.can('robot.read')
  if (['close', 'stop_patrol'].includes(action)) return auth.can('robot.control.stop')
  if (action === 'patrol') return auth.can('patrol.create')
  return auth.can('robot.control.task')
}

/** 按钮不可用原因；空串 = 可用。顺序即优先级（最根本的原因先显示）。 */
function disabledReason(action: DiagnosticAction): string {
  if (!granted(action)) return '执行档未授权'
  if (!permitted(action)) return '当前账号无此权限'
  if (!monitor.connected) return '实时连接中断，等待恢复'
  const entry = execution.value?.commands?.[action]
  if (entry && !entry.ready) return reasonText(entry.reason) || '服务端门禁未就绪'
  if (monitor.robot?.online_state !== 'ONLINE') return '车辆离线'
  if (sending.value) return '正在提交命令'
  const pending = monitor.snapshot.commands?.find(
    (row) => row.robot_id === monitor.robot?.id && row.cmd === action && isActive(row),
  )
  if (pending) return `同类命令执行中（${pending.command_id}）`
  return ''
}

function disabled(action: DiagnosticAction): boolean {
  return Boolean(disabledReason(action))
}

function mergeHistory(list: AgentCommand[]) {
  for (const row of [...list].reverse()) {
    monitor.applyEvent({ stream_id: monitor.lastStreamId, event_type: 'command.updated', data: row })
  }
  historyIds.value = list.map((row) => row.command_id).slice(0, HISTORY_LIMIT)
}

async function loadHistory(vehicle: string, token: number): Promise<boolean> {
  try {
    const { data } = await api.get<AgentCommand[]>(`/robots/${vehicle}/agent/commands`, {
      params: { limit: HISTORY_LIMIT },
    })
    if (token !== generation) return true
    mergeHistory(data)
    historyError.value = ''
    return true
  } catch (reason) {
    if (token === generation) historyError.value = `历史暂不可用：${errorMessage(reason)}（自动重试）`
    return false
  }
}

/**
 * 单一轮询循环（setTimeout 链，非 setInterval）：有活动命令 2s，否则 15s；
 * 失败按 2s→4s→…→30s 指数退避，成功即恢复——API 重建期间不会每秒产生 502。
 */
function schedule(vehicle: string, token: number, delay: number) {
  window.clearTimeout(pollTimer)
  pollTimer = window.setTimeout(async () => {
    if (token !== generation) return
    const ok = await loadHistory(vehicle, token)
    if (token !== generation) return
    failures = ok ? 0 : failures + 1
    const next = !ok ? pollBackoff(failures) : rows.value.some(isActive) ? ACTIVE_POLL_MS : IDLE_POLL_MS
    schedule(vehicle, token, next)
  }, delay)
}

function restart() {
  generation += 1
  window.clearTimeout(pollTimer)
  failures = 0
  historyIds.value = []
  error.value = ''
  historyError.value = ''
  sending.value = ''
  const vehicle = monitor.robot?.vehicle_id
  if (vehicle && visible.value) schedule(vehicle, generation, 0)
}

watch([() => monitor.robot?.vehicle_id, visible], restart)
onMounted(restart)
onUnmounted(() => {
  generation += 1
  window.clearTimeout(pollTimer)
})

async function send(action: DiagnosticAction, params: Record<string, unknown>) {
  // 同步加锁：sending 在任何 await 之前置位，连点只产生一次 POST
  if (disabled(action) || !monitor.robot) return
  const vehicle = monitor.robot.vehicle_id
  const token = generation
  sending.value = action
  error.value = ''
  const body = action === 'patrol' ? { rounds: clampRounds(rounds.value) } : params
  try {
    const { data } = await api.post<AgentCommand>(
      `/robots/${vehicle}/agent/commands/${action}`,
      { params: body },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    if (token !== generation) return
    historyIds.value = [data.command_id, ...historyIds.value.filter((id) => id !== data.command_id)].slice(
      0,
      HISTORY_LIMIT,
    )
    monitor.applyEvent({ stream_id: monitor.lastStreamId, event_type: 'command.updated', data })
    schedule(vehicle, token, ACTIVE_POLL_MS)
  } catch (reason) {
    if (token === generation) error.value = errorMessage(reason)
  } finally {
    if (token === generation) sending.value = ''
  }
}

function ackText(row: AgentCommand): string {
  const ack = row.ack_payload_json
  if (ack?.ack_source !== 'vehicle') return commandStages(row).vehicleAck === 'failed' ? '未收到' : '—'
  return `${ack.success ? '✓' : '✕'} ${ack.code}`
}

function feedbackText(row: AgentCommand): string {
  if (row.payload_json?.execution_mode !== 'SOFTWARE_TEST') return 'N/A'
  const feedback = row.ack_payload_json?.software_feedback ?? []
  if (!feedback.length) return commandStages(row).software === 'pending' ? '等待…' : '—'
  return feedback.map((item) => item.state).join(' → ')
}

const stageText: Record<string, string> = { done: '✓', pending: '…', failed: '✕', idle: '—', na: 'N/A' }
defineExpose({ send })
</script>

<template>
  <section v-if="visible" class="hmi-diagnostics" aria-label="车端诊断" data-testid="vehicle-diagnostics">
    <h3>车端诊断</h3>
    <p class="hmi-hint">
      执行档 <code>{{ execution?.profile }}</code>
      <span v-if="execution?.hardware === 'NO_HARDWARE'" class="hmi-badge" data-testid="software-test-badge"
        >SOFTWARE_TEST · NO_HARDWARE（不驱动底盘）</span
      >
      <span class="hmi-badge" data-testid="data-source">{{
        simulated ? '数据来源：模拟（SIMULATED）' : '数据来源：服务器 / 车端上报'
      }}</span>
    </p>
    <p v-if="downlinkClosed" class="hmi-hint" data-testid="downlink-closed">
      下行已关闭：命令按钮不可用，历史记录只读可查。
    </p>
    <div class="hmi-command-grid">
      <button
        v-for="[action, label] in diagnosticActions"
        :key="action"
        type="button"
        :data-diag-action="action"
        :disabled="disabled(action)"
        :title="disabledReason(action) || undefined"
        :aria-describedby="disabledReason(action) ? `diag-reason-${action}` : undefined"
        @click="send(action, {})"
      >
        {{ sending === action ? `${label}…` : label }}
      </button>
    </div>
    <template v-if="softwareActions.some(([action]) => granted(action))">
      <h3>ROS 软件测试</h3>
      <p class="hmi-hint">
        软件状态：<strong data-testid="software-state">{{ software?.state ?? '--' }}</strong>
        <span v-if="software?.patrol"> · 软件巡检中</span>
      </p>
      <label class="hmi-hint" data-testid="rounds-field">
        软件巡检轮数（1–{{ SOFTWARE_PATROL_MAX_ROUNDS }}）
        <input
          v-model.number="rounds"
          type="number"
          min="1"
          :max="SOFTWARE_PATROL_MAX_ROUNDS"
          step="1"
          data-testid="patrol-rounds"
          @change="rounds = clampRounds(rounds)"
        />
      </label>
      <div class="hmi-command-grid">
        <button
          v-for="[action, label, params] in softwareActions"
          :key="action"
          type="button"
          :data-diag-action="action"
          :class="{ 'hmi-command-stop': ['close', 'stop_patrol'].includes(action) }"
          :disabled="disabled(action)"
          :title="disabledReason(action) || undefined"
          @click="send(action, params)"
        >
          {{ sending === action ? `${label}…` : label }}
        </button>
      </div>
    </template>
    <ul class="hmi-hint hmi-diag-reasons" data-testid="disabled-reasons">
      <template v-for="[action, label] in [...diagnosticActions, ...softwareActions]" :key="action">
        <li
          v-if="granted(action) && disabledReason(action)"
          :id="`diag-reason-${action}`"
          :data-reason-for="action"
        >
          {{ label }}：{{ disabledReason(action) }}
        </li>
      </template>
    </ul>
    <p v-if="error" role="alert" class="hmi-command-error">{{ error }}</p>
    <p v-if="historyError" class="hmi-hint" data-testid="history-error">{{ historyError }}</p>
    <table v-if="rows.length" class="hmi-diag-table" data-testid="diagnostic-results">
      <thead>
        <tr>
          <th>命令</th>
          <th>创建时间</th>
          <th>command_id</th>
          <th>发布</th>
          <th>车辆 ACK</th>
          <th>ROS Feedback</th>
          <th>终态</th>
          <th>耗时</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.command_id" :data-command-id="row.command_id">
          <td>{{ row.cmd }}</td>
          <td>{{ new Date(row.issued_at).toLocaleTimeString() }}</td>
          <td class="mono">{{ row.command_id }}</td>
          <td>{{ stageText[commandStages(row).published] }}</td>
          <td :data-stage="commandStages(row).vehicleAck" data-col="ack">{{ ackText(row) }}</td>
          <td :data-stage="commandStages(row).software" data-col="feedback">{{ feedbackText(row) }}</td>
          <td :data-outcome="commandStages(row).outcome" :data-lifecycle="row.lifecycle_status">
            {{ commandStages(row).outcomeLabel }}
            <small class="mono">{{ row.lifecycle_status }}</small>
            <small v-if="commandStages(row).error" class="hmi-command-error">{{
              commandStages(row).error
            }}</small>
          </td>
          <td>{{ durationLabel(row) }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="hmi-hint" data-testid="history-empty">暂无该车辆的诊断命令记录。</p>
  </section>
</template>
