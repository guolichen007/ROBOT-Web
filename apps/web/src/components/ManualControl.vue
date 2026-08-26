<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, errorMessage, keepaliveRequest } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { useAuthStore } from '@/stores/auth'
import type { RobotState } from '@/types'

const props = withDefaults(defineProps<{ robot?: RobotState; showSafety?: boolean }>(), { showSafety: true })
const emit = defineEmits<{ notice: [message: string, tone?: string] }>()
const auth = useAuthStore()
const lease = ref<{ lease_id: string; control_session_id: string; expires_at: string } | null>(null)
const activeDirection = ref('')
const seq = ref(0)
const busy = ref(false)
let pulseTimer = 0
let pointerHeld = false

const supports = (command: string): boolean => Boolean(props.robot?.supported_commands?.includes(command))
const safetyReady = (command: string): boolean => props.robot?.safety_command_ready?.[command] === true
const available = computed(
  () =>
    props.robot?.online_state === 'ONLINE' &&
    !props.robot.estop_active &&
    props.robot.manual_control_ready === true &&
    supports('manual_control'),
)

async function acquire(): Promise<boolean> {
  if (lease.value) return true
  if (!available.value) {
    emit('notice', '机器人离线、陈旧或处于急停状态，无法手动控制', 'danger')
    return false
  }
  busy.value = true
  try {
    lease.value = (
      await api.post(`/robots/${props.robot?.vehicle_id}/manual-lease`, {
        control_session_id: newUuid(),
      })
    ).data
    emit('notice', `已获得 ${props.robot?.vehicle_id || '当前车辆'} 手动控制租约`, 'ok')
    return true
  } catch (error) {
    emit('notice', errorMessage(error), 'danger')
    return false
  } finally {
    busy.value = false
  }
}

function vector(direction: string): { linear: number; angular: number } {
  const profile = props.robot?.motion_profile
  const forward = profile?.max_manual_forward_mps ?? 0
  const reverse = profile?.reverse_allowed ? (profile.max_manual_reverse_mps ?? 0) : 0
  const angular = profile?.max_manual_angular_radps ?? 0
  return (
    {
      forward: { linear: forward, angular: 0 },
      backward: { linear: -reverse, angular: 0 },
      left: { linear: 0, angular },
      right: { linear: 0, angular: -angular },
    }[direction] || { linear: 0, angular: 0 }
  )
}

async function pulse(): Promise<void> {
  if (!lease.value || !activeDirection.value) return
  seq.value += 1
  try {
    await api.post(`/robots/${props.robot?.vehicle_id}/commands/manual`, {
      lease_id: lease.value.lease_id,
      control_session_id: lease.value.control_session_id,
      seq: seq.value,
      ...vector(activeDirection.value),
    })
  } catch (error) {
    emit('notice', errorMessage(error), 'danger')
    await safeRelease('控制脉冲失败')
  }
}

async function start(direction: string): Promise<void> {
  if (activeDirection.value || pointerHeld) return
  pointerHeld = true
  if (!(await acquire())) {
    pointerHeld = false
    return
  }
  if (!pointerHeld) {
    await stopAndRelease('松键发生在租约建立前，已安全停止')
    return
  }
  activeDirection.value = direction
  await pulse()
  pulseTimer = window.setInterval(() => void pulse(), 150)
}

async function stopAndRelease(reason = '手动控制结束'): Promise<void> {
  pointerHeld = false
  window.clearInterval(pulseTimer)
  const held = lease.value
  activeDirection.value = ''
  if (!held) return
  let message = '停止指令已发送，等待车端 ACK'
  let tone = 'warn'
  try {
    await api.post(
      `/robots/${props.robot?.vehicle_id}/commands/stop-motion`,
      {},
      { headers: { 'Idempotency-Key': newUuid() } },
    )
  } catch (error) {
    message = `停止指令未确认：${errorMessage(error)}`
    tone = 'danger'
  }
  await safeRelease(reason, false)
  emit('notice', `${message}；${reason}`, tone)
}

async function safeRelease(reason: string, notify = true): Promise<void> {
  const held = lease.value
  lease.value = null
  if (!held) return
  try {
    await api.delete(`/robots/${props.robot?.vehicle_id}/manual-lease`)
  } catch {
    /* WebSocket disconnect and server TTL remain the safety fallback. */
  }
  if (notify) emit('notice', reason)
}

function keepaliveStopAndRelease(reason: string): void {
  pointerHeld = false
  window.clearInterval(pulseTimer)
  activeDirection.value = ''
  const held = lease.value
  lease.value = null
  const vehicleId = props.robot?.vehicle_id
  if (!held || !vehicleId) return
  void keepaliveRequest(`/robots/${vehicleId}/commands/stop-motion`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': newUuid(),
    },
    body: '{}',
  }).catch(() => undefined)
  void keepaliveRequest(`/robots/${vehicleId}/manual-lease`, { method: 'DELETE' }).catch(() => undefined)
  emit('notice', `停止指令已发送，等待车端 ACK；${reason}`, 'warn')
}

async function emergencyStop(): Promise<void> {
  if (!safetyReady('emergency_stop')) {
    emit('notice', '服务器未就绪：急停命令不可用', 'danger')
    return
  }
  window.clearInterval(pulseTimer)
  activeDirection.value = ''
  busy.value = true
  try {
    const { data } = await api.post(
      `/robots/${props.robot?.vehicle_id}/commands/emergency-stop`,
      {},
      {
        headers: { 'Idempotency-Key': newUuid() },
      },
    )
    lease.value = null
    emit(
      'notice',
      data.lifecycle_status === 'PUBLISHED_UNCONFIRMED'
        ? '软件急停未送达/未确认'
        : '软件急停已发送，等待 ACK',
      'danger',
    )
  } catch (error) {
    emit('notice', errorMessage(error), 'danger')
  } finally {
    busy.value = false
  }
}

async function resetEstop(): Promise<void> {
  if (!safetyReady('reset_estop')) {
    emit('notice', '服务器未就绪：急停复位不可用', 'danger')
    return
  }
  try {
    await api.post(
      `/robots/${props.robot?.vehicle_id}/commands/reset-estop`,
      {},
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    emit('notice', '急停复位已发送，等待 ACK', 'warn')
  } catch (error) {
    emit('notice', errorMessage(error), 'danger')
  }
}

function safetyRelease(): void {
  pointerHeld = false
  if (lease.value) void stopAndRelease('页面失焦，租约已释放')
}
function visibilityRelease(): void {
  if (document.hidden) keepaliveStopAndRelease('页面隐藏，租约已释放')
}
function pageHideRelease(): void {
  keepaliveStopAndRelease('控制页面已关闭')
}

watch(
  () => [available.value, auth.can('robot.control.manual')],
  ([isAvailable, permitted]) => {
    if (lease.value && (!isAvailable || !permitted)) void stopAndRelease('控制条件失效，租约已释放')
  },
)

onMounted(() => {
  window.addEventListener('blur', safetyRelease)
  window.addEventListener('pointerup', safetyRelease)
  document.addEventListener('visibilitychange', visibilityRelease)
  window.addEventListener('pagehide', pageHideRelease)
})
onUnmounted(() => {
  window.clearInterval(pulseTimer)
  window.removeEventListener('blur', safetyRelease)
  window.removeEventListener('pointerup', safetyRelease)
  document.removeEventListener('visibilitychange', visibilityRelease)
  window.removeEventListener('pagehide', pageHideRelease)
  if (lease.value) keepaliveStopAndRelease('控制页面已关闭')
})
</script>

<template>
  <section class="control-panel panel">
    <div class="panel-heading">
      <div>
        <span class="eyebrow">手动控制</span>
        <h3>手动控制</h3>
      </div>
      <span class="lease-state" :class="lease ? 'held' : ''">{{ lease ? '租约 HELD' : '无租约' }}</span>
    </div>
    <div class="control-grid" :class="{ disabled: !auth.can('robot.control.manual') || !available }">
      <button
        class="direction forward"
        :class="{ active: activeDirection === 'forward' }"
        :disabled="busy || !auth.can('robot.control.manual') || !available"
        @pointerdown.prevent="start('forward')"
      >
        ↑<small>前进</small>
      </button>
      <button
        class="direction left"
        :class="{ active: activeDirection === 'left' }"
        :disabled="busy || !auth.can('robot.control.manual') || !available"
        @pointerdown.prevent="start('left')"
      >
        ↶<small>左转</small>
      </button>
      <button
        class="direction stop"
        :disabled="busy || !auth.can('robot.control.stop') || !supports('stop_motion') || !safetyReady('stop_motion')"
        @click="stopAndRelease('停止并释放租约')"
      >
        ■<small>停止</small>
      </button>
      <button
        class="direction right"
        :class="{ active: activeDirection === 'right' }"
        :disabled="busy || !auth.can('robot.control.manual') || !available"
        @pointerdown.prevent="start('right')"
      >
        ↷<small>右转</small>
      </button>
      <button
        class="direction backward"
        :class="{ active: activeDirection === 'backward' }"
        :disabled="busy || !auth.can('robot.control.manual') || !available"
        @pointerdown.prevent="start('backward')"
      >
        ↓<small>后退</small>
      </button>
    </div>
    <p class="safety-note">150 ms 脉冲 · TTL 500 ms · 松键/失焦自动停止；最终安全保障为车端 TTL watchdog</p>
    <div v-if="showSafety" class="estop-row">
      <button
        v-if="!robot?.estop_active"
        class="estop-button"
        :disabled="busy || !auth.can('robot.control.estop') || !supports('emergency_stop') || !safetyReady('emergency_stop')"
        @click="emergencyStop"
      >
        软件急停
      </button>
      <button
        v-else
        class="reset-button"
        :disabled="busy || !auth.can('robot.control.reset_estop') || !supports('reset_estop') || !safetyReady('reset_estop')"
        @click="resetEstop"
      >
        复位软件急停
      </button>
      <small>软件急停不等于物理急停</small>
    </div>
  </section>
</template>
