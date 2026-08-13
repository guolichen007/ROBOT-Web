<script setup lang="ts">
import { ControlPlatformIcon, HomeIcon, StopCircleIcon } from 'tdesign-icons-vue-next'
import { useHoldToConfirm } from '@/composables/useHoldToConfirm'
defineProps<{
  disabled: boolean
  stopDisabled: boolean
  estopDisabled: boolean
  manualDisabled: boolean
  reason: string
}>()
const emit = defineEmits<{ patrol: []; stop: []; home: []; estop: []; manual: [] }>()
const hold = useHoldToConfirm(() => emit('estop'))
function keyDown(event: KeyboardEvent): void {
  if (!event.repeat && ['Enter', ' '].includes(event.key)) hold.start()
}
</script>
<template>
  <section class="panel operations-command-dock">
    <div>
      <strong>车辆操作</strong><span>{{ reason || '请选择操作' }}</span>
    </div>
    <t-button :disabled="disabled" @click="$emit('patrol')"><ControlPlatformIcon />开始巡检</t-button
    ><t-button theme="warning" :disabled="stopDisabled" @click="$emit('stop')"
      ><StopCircleIcon />停止巡检</t-button
    ><t-button :disabled="disabled" @click="$emit('home')"><HomeIcon />返回等待区</t-button
    ><t-button variant="outline" :disabled="manualDisabled" @click="$emit('manual')">手动控制</t-button
    ><button
      class="hold-estop"
      aria-label="软件紧急停止"
      :disabled="estopDisabled"
      @pointerdown="hold.start"
      @pointerup="hold.cancel"
      @pointercancel="hold.cancel"
      @pointerleave="hold.cancel"
      @keydown="keyDown"
      @keyup="hold.cancel"
    >
      <span :style="{ width: `${hold.progress.value}%` }"></span><strong>按住 0.8 秒 软件急停</strong
      ><small>不替代车辆物理急停</small>
    </button>
  </section>
</template>
