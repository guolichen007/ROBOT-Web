import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useAuthStore } from '@/stores/auth'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { agentTaskLabel, canSendAgent, type AgentAction } from '@/lib/vehicle-agent'
import type { AgentCommand, ParkingSlot } from '@/types'

const OPEN = ['CREATED', 'QUEUED', 'PUBLISHED', 'ACK_ACCEPTED']

/**
 * WEB-R1C: the ONE vehicle_agent v1 command tracker of the console. The left device tab
 * (start / close) and the bottom task bar (patrol / pause / resume / goal / homing) render
 * buttons from the SAME instance, so there is a single pending command, a single error and a
 * single poll timer — never a second copy of vehicle/command state.
 * `authorize` lets the console add the server OperationReadiness decision on top (it can only
 * further restrict; it never turns a local lock into an enabled button).
 */
export function useAgentCommands(
  selectedSlotId: Ref<string | null>,
  slots: Ref<ParkingSlot[]>,
  authorize?: (action: AgentAction) => boolean,
) {
  const monitor = useMonitorStore()
  const auth = useAuthStore()
  const sending = ref('')
  const error = ref('')
  const diagnosticCode = ref('')
  const currentId = ref('')
  const lastAction = ref<AgentAction | ''>('')
  const requestRobot = ref('')
  const awaitingStatus = ref(false)
  let pollTimer = 0

  const current = computed(() =>
    monitor.snapshot.commands?.find(
      (item) =>
        item.robot_id === monitor.robot?.id &&
        (currentId.value ? item.command_id === currentId.value : OPEN.includes(item.lifecycle_status)),
    ),
  )
  const pending = computed(() => Boolean(current.value && OPEN.includes(current.value.lifecycle_status)))
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
            [...OPEN, 'PUBLISHED_UNCONFIRMED'].includes(row.lifecycle_status),
        ) ||
        !permitted(action) ||
        !canSendAgent(monitor.robot, action, monitor.connected) ||
        (authorize && !authorize(action)) ||
        (action === 'navigate' &&
          !slots.value.some((slot) => slot.id === selectedSlotId.value && slot.enabled)),
    )
  }

  function busy(action: AgentAction): boolean {
    return sending.value === action || Boolean(pending.value && current.value?.cmd === action)
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
    if (!OPEN.includes(row.lifecycle_status)) window.clearInterval(pollTimer)
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
      lastAction.value = ''
      awaitingStatus.value = false
    },
  )

  async function send(action: AgentAction) {
    if (disabled(action) || !monitor.robot) return
    const vehicle = monitor.robot.vehicle_id
    const slot = slots.value.find((item) => item.id === selectedSlotId.value)
    sending.value = action
    lastAction.value = action
    error.value = ''
    diagnosticCode.value = ''
    requestRobot.value = vehicle
    try {
      const { data } = await api.post<AgentCommand>(
        `/robots/${vehicle}/agent/commands/${action}`,
        {
          params:
            action === 'start'
              ? { with_navigation: true }
              : action === 'navigate'
                ? { spot: slot?.code }
                : {},
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

  onUnmounted(() => window.clearInterval(pollTimer))

  return {
    send,
    disabled,
    busy,
    sending,
    pending,
    current,
    error,
    diagnosticCode,
    awaitingStatus,
    lastAction,
    taskLabel,
    protocolBound,
  }
}

export type AgentCommands = ReturnType<typeof useAgentCommands>
