<script setup lang="ts">
import { computed, useSlots } from 'vue'
import {
  ControlPlatformIcon,
  HomeIcon,
  LocationIcon,
  PauseCircleIcon,
  PlayCircleIcon,
  StopCircleIcon,
} from 'tdesign-icons-vue-next'
import { useHoldToConfirm } from '@/composables/useHoldToConfirm'
import type { ResumeOptions, VehicleOperationState } from '@/composables/useVehicleOperationState'

/**
 * WEB-R1C 底部任务操作栏（DESIGN.md §3.1）：
 *   任务组  开始巡检 · 暂停/继续 · 目标导航 · 返航   （V1 车辆由 #task 插槽替换为 vehicle_agent 按钮）
 *   安全组  停止 · 软件急停（按住确认）              （始终渲染、始终可见，不随任何页签变化）
 * 每个按钮只用自己的服务端资格（*Ready）+ 本地 busy/运动锁；reason 只负责展示。
 */
const props = withDefaults(
  defineProps<{
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
    pauseReady?: boolean
    resumeReady?: boolean
    navigateReady?: boolean
    navigateTarget?: string
    navigateReason?: string
    pauseReason?: string
    /** readable reasons of the disabled task buttons, shown in the status line (not only a tooltip) */
    hint?: string
  }>(),
  {
    pauseReady: false,
    resumeReady: false,
    navigateReady: false,
    navigateTarget: '',
    navigateReason: '',
    pauseReason: '',
    hint: '',
  },
)
const emit = defineEmits<{
  patrol: []
  stop: []
  home: []
  estop: []
  resetEstop: []
  pause: []
  resume: []
  navigate: []
}>()
const slots = useSlots()
const hold = useHoldToConfirm(() => {
  if (props.estopActive) emit('resetEstop')
  else emit('estop')
})
function keyDown(event: KeyboardEvent): void {
  if (!event.repeat && ['Enter', ' '].includes(event.key)) hold.start()
}

const motionLocked = computed(() => props.estopActive || props.vehicleState === 'ERROR_STOP_UNCONFIRMED')
const holdDisabled = computed(
  () => Boolean(props.busy) || (props.estopActive ? !props.resetEstopReady : !props.estopReady),
)
const canContinue = computed(
  () => props.vehicleState === 'PAUSED_SAFE' && props.resumeOptions.canContinuePatrol,
)

const patrolLabel = computed(() => {
  if (props.busy === 'patrol') return '正在启动…'
  if (props.vehicleState === 'PATROLLING') return '● 巡检中'
  return '开始巡检'
})
const patrolDisabled = computed(
  () =>
    !props.patrolReady ||
    Boolean(props.busy) ||
    motionLocked.value ||
    canContinue.value ||
    ['PATROLLING', 'STOPPING', 'RETURNING', 'RETURN_STARTING'].includes(props.vehicleState) ||
    (props.vehicleState === 'PAUSED_SAFE' &&
      props.resumeOptions.interruptedKind === 'return' &&
      !props.resumeOptions.canContinuePatrol),
)

// 暂停 / 继续：同一位置，按任务状态切换；继续沿用服务端可继续的巡检（resume_task_id）
const pauseMode = computed<'resume' | 'pause'>(() => (canContinue.value ? 'resume' : 'pause'))
const pauseLabel = computed(() => {
  if (props.busy === 'resume') return '正在继续…'
  if (props.busy === 'pause') return '正在暂停…'
  return pauseMode.value === 'resume' ? '继续巡检' : '暂停巡检'
})
const pauseDisabled = computed(() =>
  pauseMode.value === 'resume'
    ? !props.resumeReady || Boolean(props.busy) || motionLocked.value
    : !props.pauseReady || Boolean(props.busy) || motionLocked.value || props.vehicleState !== 'PATROLLING',
)
const pauseTitle = computed(() =>
  pauseMode.value === 'pause' && props.pauseReason ? props.pauseReason : '暂停或继续当前巡检',
)

const navigateLabel = computed(() =>
  props.busy === 'navigate'
    ? '正在下发…'
    : props.navigateTarget
      ? `前往 ${props.navigateTarget}`
      : '目标导航',
)
const navigateDisabled = computed(
  () => !props.navigateReady || Boolean(props.busy) || motionLocked.value || Boolean(props.navigateReason),
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
    ['STOPPING', 'PAUSED_SAFE', 'ERROR_STOP_UNCONFIRMED'].includes(props.vehicleState),
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
    <div v-else-if="reason" class="dock-status">
      <span>{{ reason }}</span>
    </div>
    <div v-else-if="hint" class="dock-status dock-hint" data-testid="dock-hint">
      <span>{{ hint }}</span>
    </div>
    <div class="dock-rows">
      <div class="dock-task" role="group" aria-label="任务操作" data-testid="console-task-group">
        <slot v-if="slots.task" name="task" />
        <template v-else>
          <t-button
            theme="primary"
            :loading="busy === 'patrol'"
            :disabled="patrolDisabled"
            @click="$emit('patrol')"
            ><ControlPlatformIcon />{{ patrolLabel }}</t-button
          ><t-button
            variant="outline"
            data-testid="dock-pause-resume"
            :title="pauseTitle"
            :loading="busy === 'pause' || busy === 'resume'"
            :disabled="pauseDisabled"
            @click="pauseMode === 'resume' ? $emit('resume') : $emit('pause')"
            ><PlayCircleIcon v-if="pauseMode === 'resume'" /><PauseCircleIcon v-else />{{
              pauseLabel
            }}</t-button
          ><t-button
            variant="outline"
            data-testid="dock-navigate"
            :title="navigateReason || '前往地图上选中的检测点'"
            :loading="busy === 'navigate'"
            :disabled="navigateDisabled"
            @click="$emit('navigate')"
            ><LocationIcon />{{ navigateLabel }}</t-button
          ><t-button
            variant="outline"
            :loading="busy === 'home'"
            :disabled="homeDisabled"
            @click="$emit('home')"
            ><HomeIcon />{{ homeLabel }}</t-button
          >
        </template>
      </div>
      <div class="dock-safety" role="group" aria-label="安全操作（常驻）" data-testid="console-safety-group">
        <t-button
          class="dock-stop"
          variant="outline"
          :loading="busy === 'stop'"
          :disabled="stopDisabled"
          @click="$emit('stop')"
          ><StopCircleIcon />{{ stopLabel }}</t-button
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
        <slot name="safety-extra" />
      </div>
    </div>
  </section>
</template>
