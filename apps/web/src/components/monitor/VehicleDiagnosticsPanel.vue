<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useAuthStore } from '@/stores/auth'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import {
  commandStages,
  diagnosticActions,
  durationLabel,
  isActive,
  softwareActions,
  type DiagnosticAction,
} from '@/lib/vehicle-diagnostics'
import type { AgentCommand } from '@/types'

// G2：车端诊断与 ROS 软件测试（仅逐车执行档授权的车辆显示）。按钮调用真实鉴权 API
// （Idempotency-Key），状态完全来自服务端 Command 事实；不在前端模拟任何执行结果。
const monitor = useMonitorStore()
const auth = useAuthStore()
const sending = ref('')
const error = ref('')
const history = ref<string[]>([])
const requestRobot = ref('')
let pollTimer = 0

const execution = computed(() => monitor.robot?.vehicle_agent_execution)
const visible = computed(
  () =>
    monitor.robot?.agent_protocol === 'VEHICLE_AGENT_V1' &&
    Boolean(execution.value?.profile && Object.keys(execution.value.commands).length),
)
const rows = computed(() =>
  history.value
    .map((id) => monitor.snapshot.commands?.find((row) => row.command_id === id))
    .filter((row): row is AgentCommand => Boolean(row)),
)
const software = computed(() => monitor.robot?.agent_state?.status?.software ?? undefined)

function granted(action: DiagnosticAction): boolean {
  return Boolean(execution.value?.commands?.[action])
}

function permitted(action: DiagnosticAction): boolean {
  if (['ping', 'get_status', 'get_telemetry'].includes(action)) return auth.can('robot.read')
  if (['close', 'stop_patrol'].includes(action)) return auth.can('robot.control.stop')
  if (action === 'patrol') return auth.can('patrol.create')
  return auth.can('robot.control.task')
}

function disabled(action: DiagnosticAction): boolean {
  return Boolean(
    sending.value ||
      !monitor.connected ||
      monitor.robot?.online_state !== 'ONLINE' ||
      !granted(action) ||
      !execution.value?.commands?.[action]?.ready ||
      !permitted(action) ||
      monitor.snapshot.commands?.some(
        (row) => row.robot_id === monitor.robot?.id && row.cmd === action && isActive(row),
      ),
  )
}

watch(
  () => monitor.robot?.vehicle_id,
  () => {
    window.clearInterval(pollTimer)
    history.value = []
    error.value = ''
    sending.value = ''
  },
)

async function send(action: DiagnosticAction, params: Record<string, unknown>) {
  if (disabled(action) || !monitor.robot) return
  const vehicle = monitor.robot.vehicle_id
  requestRobot.value = vehicle
  sending.value = action
  error.value = ''
  try {
    const { data } = await api.post<AgentCommand>(
      `/robots/${vehicle}/agent/commands/${action}`,
      { params },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    if (monitor.robot?.vehicle_id !== vehicle) return
    history.value = [data.command_id, ...history.value.filter((id) => id !== data.command_id)].slice(0, 8)
    monitor.applyEvent({ stream_id: monitor.lastStreamId, event_type: 'command.updated', data })
    // WebSocket 为主；轮询只修复丢失的增量，不宣称成功
    window.clearInterval(pollTimer)
    pollTimer = window.setInterval(async () => {
      const active = rows.value.filter(isActive)
      if (!active.length) {
        window.clearInterval(pollTimer)
        return
      }
      for (const row of active) {
        try {
          const fresh = (await api.get<AgentCommand>(`/commands/${row.command_id}`)).data
          monitor.applyEvent({ stream_id: monitor.lastStreamId, event_type: 'command.updated', data: fresh })
        } catch {
          /* 等待服务端事实 */
        }
      }
    }, 2000)
  } catch (reason) {
    if (monitor.robot?.vehicle_id === vehicle) error.value = errorMessage(reason)
  } finally {
    sending.value = ''
  }
}

const stageText: Record<string, string> = { done: '✓', pending: '…', failed: '✕', idle: '—', na: 'N/A' }
onUnmounted(() => window.clearInterval(pollTimer))
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
    </p>
    <div class="hmi-command-grid">
      <button
        v-for="[action, label] in diagnosticActions"
        :key="action"
        type="button"
        :data-diag-action="action"
        :disabled="disabled(action)"
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
      <div class="hmi-command-grid">
        <button
          v-for="[action, label, params] in softwareActions"
          :key="action"
          type="button"
          :data-diag-action="action"
          :class="{ 'hmi-command-stop': ['close', 'stop_patrol'].includes(action) }"
          :disabled="disabled(action)"
          @click="send(action, params)"
        >
          {{ sending === action ? `${label}…` : label }}
        </button>
      </div>
    </template>
    <p v-if="error" role="alert" class="hmi-command-error">{{ error }}</p>
    <table v-if="rows.length" class="hmi-diag-table" data-testid="diagnostic-results">
      <thead>
        <tr>
          <th>命令</th>
          <th>创建时间</th>
          <th>command_id</th>
          <th>排队</th>
          <th>发布</th>
          <th>车端 ACK</th>
          <th>ROS 软件</th>
          <th>结果</th>
          <th>耗时</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.command_id" :data-command-id="row.command_id">
          <td>{{ row.cmd }}</td>
          <td>{{ new Date(row.issued_at).toLocaleTimeString() }}</td>
          <td class="mono">{{ row.command_id }}</td>
          <td>{{ stageText[commandStages(row).queued] }}</td>
          <td>{{ stageText[commandStages(row).published] }}</td>
          <td :data-stage="commandStages(row).vehicleAck">{{ stageText[commandStages(row).vehicleAck] }}</td>
          <td :data-stage="commandStages(row).software">{{ stageText[commandStages(row).software] }}</td>
          <td :data-outcome="commandStages(row).outcome">
            {{ commandStages(row).outcomeLabel }}
            <small v-if="commandStages(row).error" class="hmi-command-error">{{
              commandStages(row).error
            }}</small>
          </td>
          <td>{{ durationLabel(row) }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
