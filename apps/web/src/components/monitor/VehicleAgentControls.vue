<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useAuthStore } from '@/stores/auth'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { agentActions, agentTaskLabel, canSendAgent, type AgentAction } from '@/lib/vehicle-agent'
import type { AgentCommand, ParkingSlot } from '@/types'

const props = defineProps<{ selectedSlotId: string | null; slots: ParkingSlot[] }>()
const emit = defineEmits<{ 'update:selectedSlotId': [value: string | null] }>()
const monitor = useMonitorStore()
const auth = useAuthStore()
const sending = ref('')
const error = ref('')
const diagnosticCode = ref('')
const currentId = ref('')
const requestRobot = ref('')
const awaitingStatus = ref(false)
let pollTimer = 0
const current = computed(() =>
  monitor.snapshot.commands?.find(
    (item) =>
      item.robot_id === monitor.robot?.id &&
      (currentId.value
        ? item.command_id === currentId.value
        : ['CREATED', 'QUEUED', 'PUBLISHED', 'ACK_ACCEPTED'].includes(item.lifecycle_status)),
  ),
)
const pending = computed(
  () =>
    current.value &&
    ['CREATED', 'QUEUED', 'PUBLISHED', 'ACK_ACCEPTED'].includes(current.value.lifecycle_status),
)
const taskLabel = computed(() => agentTaskLabel(monitor.robot?.agent_state?.status?.task?.state))
const protocolBound = computed(() => monitor.robot?.agent_protocol === 'VEHICLE_AGENT_V1')

function permitted(action: AgentAction): boolean {
  const permission = ['close', 'stop_patrol', 'stop_navigate'].includes(action)
    ? 'robot.control.stop'
    : ['patrol', 'resume_patrol'].includes(action)
      ? 'patrol.create'
      : 'robot.control.task'
  return auth.can(permission)
}

function disabled(action: AgentAction): boolean {
  return Boolean(
    sending.value ||
      (pending.value && !['stop_patrol', 'stop_navigate'].includes(action)) ||
      monitor.snapshot.commands?.some(
        (row) =>
          row.robot_id === monitor.robot?.id &&
          row.cmd === action &&
          ['CREATED', 'QUEUED', 'PUBLISHED', 'ACK_ACCEPTED'].includes(row.lifecycle_status),
      ) ||
      monitor.snapshot.commands?.some(
        (row) =>
          row.robot_id === monitor.robot?.id &&
          row.cmd === action &&
          row.lifecycle_status === 'PUBLISHED_UNCONFIRMED',
      ) ||
      !permitted(action) ||
      !canSendAgent(monitor.robot, action, monitor.connected) ||
      (action === 'navigate' &&
        !props.slots.some((slot) => slot.id === props.selectedSlotId && slot.enabled)),
  )
}

function observe(row: AgentCommand) {
  if (requestRobot.value !== monitor.robot?.vehicle_id) return
  if (
    ['ACK_REJECTED', 'ACK_UNSUPPORTED', 'EXPIRED', 'PUBLISHED_UNCONFIRMED'].includes(row.lifecycle_status)
  ) {
    error.value =
      row.ack_payload_json?.message ||
      (row.lifecycle_status === 'PUBLISHED_UNCONFIRMED'
        ? '未收到回执，执行结果未确认'
        : row.ack_reason || '命令未确认')
    diagnosticCode.value = row.ack_payload_json?.code || row.ack_reason || row.lifecycle_status
  }
  awaitingStatus.value = row.lifecycle_status === 'ACK_ACCEPTED'
  if (!['CREATED', 'QUEUED', 'PUBLISHED', 'ACK_ACCEPTED'].includes(row.lifecycle_status))
    window.clearInterval(pollTimer)
}

watch(
  current,
  (row) => {
    if (row) observe(row)
  },
  { deep: true },
)
watch(
  () => monitor.robot?.vehicle_id,
  () => {
    window.clearInterval(pollTimer)
    sending.value = ''
    error.value = ''
    diagnosticCode.value = ''
    currentId.value = ''
    awaitingStatus.value = false
  },
)

async function send(action: AgentAction) {
  if (disabled(action) || !monitor.robot) return
  const vehicle = monitor.robot.vehicle_id
  const slot = props.slots.find((item) => item.id === props.selectedSlotId)
  sending.value = action
  error.value = ''
  diagnosticCode.value = ''
  requestRobot.value = vehicle
  try {
    const { data } = await api.post<AgentCommand>(
      `/robots/${vehicle}/agent/commands/${action}`,
      {
        params:
          action === 'start' ? { with_navigation: true } : action === 'navigate' ? { spot: slot?.code } : {},
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    if (monitor.robot?.vehicle_id !== vehicle) return
    currentId.value = data.command_id
    if (!monitor.snapshot.commands?.some((row) => row.command_id === data.command_id)) {
      monitor.applyEvent({ stream_id: monitor.lastStreamId, event_type: 'command.updated', data })
    }
    if (current.value) observe(current.value)
    // WS is primary. Polling repairs a dropped command delta without claiming success.
    window.clearInterval(pollTimer)
    pollTimer = window.setInterval(async () => {
      try {
        const row = (await api.get<AgentCommand>(`/commands/${data.command_id}`)).data
        if (monitor.robot?.vehicle_id !== vehicle) return
        monitor.applyEvent({ stream_id: monitor.lastStreamId, event_type: 'command.updated', data: row })
      } catch {
        /* keep pending until a server fact arrives */
      }
    }, 2000)
  } catch (reason) {
    if (monitor.robot?.vehicle_id === vehicle) error.value = errorMessage(reason)
  } finally {
    if (monitor.robot?.vehicle_id === vehicle) sending.value = ''
  }
}

defineExpose({ send })
onUnmounted(() => window.clearInterval(pollTimer))
</script>

<template>
  <section class="hmi-auto-controls" aria-label="自动导航控制">
    <div class="hmi-task">
      <span>任务状态</span><strong data-testid="agent-task-state">{{ taskLabel }}</strong>
    </div>
    <p v-if="!protocolBound" class="hmi-hint">当前车辆未绑定 vehicle_agent v1，既有控制入口位于手动控制。</p>
    <p v-else-if="!monitor.connected || monitor.robot?.online_state !== 'ONLINE'" class="hmi-hint">
      车辆离线，历史数据仅供参考。
    </p>
    <p v-else-if="!Object.values(monitor.robot?.vehicle_agent_ready || {}).some(Boolean)" class="hmi-hint">
      车辆状态或控制合同尚未就绪。
    </p>
    <template
      v-for="(group, index) in [agentActions.slice(0, 2), agentActions.slice(2, 6), agentActions.slice(6)]"
      :key="index"
    >
      <h3>{{ ['智能车状态', '巡逻任务', '停车位'][index] }}</h3>
      <label v-if="index === 2" class="hmi-slot-label"
        >目标车位
        <select
          aria-label="停车位"
          :value="selectedSlotId || ''"
          @change="emit('update:selectedSlotId', ($event.target as HTMLSelectElement).value || null)"
        >
          <option value="">请选择停车位</option>
          <option v-for="slot in slots" :key="slot.id" :value="slot.id" :disabled="!slot.enabled">
            {{ slot.code }}
          </option>
        </select>
      </label>
      <div class="hmi-command-grid">
        <button
          v-for="[action, label] in group"
          :key="action"
          type="button"
          :data-action="action"
          :class="{ 'hmi-command-stop': ['close', 'stop_patrol', 'stop_navigate'].includes(action) }"
          :disabled="disabled(action)"
          :aria-busy="sending === action || Boolean(pending && current?.cmd === action)"
          @click="send(action)"
        >
          {{ sending === action || (pending && current?.cmd === action) ? `${label}…` : label }}
        </button>
      </div>
    </template>
    <p v-if="awaitingStatus" role="status" class="hmi-hint">已收到 ACK，等待车辆状态确认。</p>
    <p v-if="error" role="alert" class="hmi-command-error" :data-code="diagnosticCode">{{ error }}</p>
  </section>
</template>
