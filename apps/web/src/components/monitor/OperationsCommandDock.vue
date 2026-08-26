<script setup lang="ts">
import { computed } from 'vue'
import { ControlPlatformIcon, HomeIcon, StopCircleIcon } from 'tdesign-icons-vue-next'
import { useHoldToConfirm } from '@/composables/useHoldToConfirm'
import type { ResumeOptions, VehicleOperationState } from '@/composables/useVehicleOperationState'

const props = defineProps<{
  busy: string
  reason: string
  estopActive: boolean
  vehicleState: VehicleOperationState
  atWaitingArea: boolean
  resumeOptions: ResumeOptions
  patrolReady: boolean
  returnReady: boolean
  stopReady: boolean
  estopReady: boolean
  resetEstopReady: boolean
}>()
const emit = defineEmits<{ patrol: []; stop: []; home: []; estop: []; resetEstop: [] }>()
const hold = useHoldToConfirm(() => {
  if (props.estopActive) emit('resetEstop')
  else emit('estop')
})
function keyDown(event: KeyboardEvent): void {
  if (!event.repeat && ['Enter', ' '].includes(event.key)) hold.start()
}

const motionLocked = computed(() => props.estopActive || props.vehicleState === 'ERROR_STOP_UNCONFIRMED')
// reason 只负责展示；每个按钮使用自己的服务器 readiness，避免一个原因锁死全部控制。
const holdDisabled = computed(
  () => Boolean(props.busy) || (props.estopActive ? !props.resetEstopReady : !props.estopReady),
)

const patrolLabel = computed(() => {
  if (props.busy === 'patrol') return '正在启动…'
  if (props.vehicleState === 'PATROLLING') return '● 巡检中'
  if (props.vehicleState === 'PAUSED_SAFE' && props.resumeOptions.canContinuePatrol) return '继续巡检'
  return '开始巡检'
})
const patrolDisabled = computed(
  () =>
    !props.patrolReady ||
    Boolean(props.busy) ||
    motionLocked.value ||
    ['PATROLLING', 'STOPPING', 'RETURNING', 'RETURN_STARTING'].includes(props.vehicleState) ||
    (props.vehicleState === 'PAUSED_SAFE' &&
      props.resumeOptions.interruptedKind === 'return' &&
      !props.resumeOptions.canContinuePatrol),
)

const stopLabel = computed(() => {
  if (props.busy === 'stop') return '● 停止中'
  if (props.vehicleState === 'PAUSED_SAFE' || props.vehicleState === 'ERROR_STOP_UNCONFIRMED') return '已停止'
  return '停止'
})
const stopDisabled = computed(
  () =>
    !props.stopReady ||
    Boolean(props.busy) ||
    motionLocked.value ||
    ['IDLE', 'PAUSED_SAFE', 'ERROR_STOP_UNCONFIRMED'].includes(props.vehicleState),
)

const homeLabel = computed(() => {
  if (props.busy === 'home') return '● 返回中'
  if (props.atWaitingArea && props.vehicleState === 'IDLE') return '已在等待区'
  if (props.vehicleState === 'PAUSED_SAFE' && props.resumeOptions.interruptedKind === 'return')
    return '继续返回'
  return '返回等待区'
})
const homeDisabled = computed(
  () =>
    !props.returnReady ||
    Boolean(props.busy) ||
    motionLocked.value ||
    (props.atWaitingArea && props.vehicleState === 'IDLE') ||
    ['PATROLLING', 'STOPPING', 'RETURNING'].includes(props.vehicleState),
)
</script>
<template>
  <section class="panel operations-command-dock">
    <div v-if="estopActive" class="dock-status dock-status--estop">
      <span>软件急停已生效，请先解除急停后再执行车辆运动操作</span>
    </div>
    <div v-else-if="reason" class="dock-status"><span>{{ reason }}</span></div>
    <div class="dock-actions">
      <t-button
        theme="primary"
        :loading="busy === 'patrol'"
        :disabled="patrolDisabled"
        @click="$emit('patrol')"
        ><ControlPlatformIcon />{{ patrolLabel }}</t-button
      ><t-button
        variant="outline"
        :loading="busy === 'stop'"
        :disabled="stopDisabled"
        @click="$emit('stop')"
        ><StopCircleIcon />{{ stopLabel }}</t-button
      ><t-button
        variant="outline"
        :loading="busy === 'home'"
        :disabled="homeDisabled"
        @click="$emit('home')"
        ><HomeIcon />{{ homeLabel }}</t-button
      ><button
        class="hold-estop"
        :class="{ 'hold-estop--active': estopActive, 'is-resetting': busy === 'reset-estop' }"
        :aria-label="estopActive ? '解除急停' : '软件急停'"
        :disabled="holdDisabled"
        :title="
          estopActive
            ? '解除软件急停锁存，不替代物理急停按钮的人工复位'
            : '按住 0.8 秒确认；不替代车辆物理急停'
        "
        @pointerdown="hold.start"
        @pointerup="hold.cancel"
        @pointercancel="hold.cancel"
        @pointerleave="hold.cancel"
        @keydown="keyDown"
        @keyup="hold.cancel"
      >
        <span :style="{ width: `${hold.progress.value}%` }"></span>
        <strong>{{
          busy === 'reset-estop'
            ? '正在解除…'
            : busy === 'estop'
              ? '正在急停…'
              : estopActive
                ? '解除急停'
                : '软件急停'
        }}</strong>
        <small>{{ estopActive ? '按住 0.8 秒复位' : '按住 0.8 秒确认' }}</small>
      </button>
    </div>
  </section>
</template>
