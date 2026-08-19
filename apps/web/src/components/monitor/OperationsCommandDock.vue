<script setup lang="ts">
import { ControlPlatformIcon, HomeIcon, StopCircleIcon } from 'tdesign-icons-vue-next'
import { useHoldToConfirm } from '@/composables/useHoldToConfirm'
const props = defineProps<{ busy: string; reason: string; estopActive: boolean }>()
const emit = defineEmits<{ patrol: []; stop: []; home: []; estop: []; resetEstop: [] }>()
const hold = useHoldToConfirm(() => {
  if (props.estopActive) emit('resetEstop')
  else emit('estop')
})
function keyDown(event: KeyboardEvent): void {
  if (!event.repeat && ['Enter', ' '].includes(event.key)) hold.start()
}
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
        :disabled="Boolean(busy) || estopActive"
        @click="$emit('patrol')"
        ><ControlPlatformIcon />{{ busy === 'patrol' ? '下发中…' : '开始巡检' }}</t-button
      ><t-button
        variant="outline"
        :loading="busy === 'stop'"
        :disabled="Boolean(busy) || estopActive"
        @click="$emit('stop')"
        ><StopCircleIcon />{{ busy === 'stop' ? '下发中…' : '停止巡检' }}</t-button
      ><t-button
        variant="outline"
        :loading="busy === 'home'"
        :disabled="Boolean(busy) || estopActive"
        @click="$emit('home')"
        ><HomeIcon />{{ busy === 'home' ? '下发中…' : '返回等待区' }}</t-button
      ><button
        class="hold-estop"
        :class="{ 'hold-estop--active': estopActive, 'is-resetting': busy === 'reset-estop' }"
        :aria-label="estopActive ? '解除急停' : '软件急停'"
        :disabled="Boolean(busy)"
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
