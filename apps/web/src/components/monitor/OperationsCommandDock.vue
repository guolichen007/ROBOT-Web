<script setup lang="ts">
import { ControlPlatformIcon, HomeIcon, StopCircleIcon } from 'tdesign-icons-vue-next'
import { useHoldToConfirm } from '@/composables/useHoldToConfirm'
defineProps<{ busy: string; reason: string }>()
const emit = defineEmits<{ patrol: []; stop: []; home: []; estop: [] }>()
const hold = useHoldToConfirm(() => emit('estop'))
function keyDown(event: KeyboardEvent): void {
  if (!event.repeat && ['Enter', ' '].includes(event.key)) hold.start()
}
</script>
<template>
  <section class="panel operations-command-dock">
    <div v-if="reason" class="dock-status"><span>{{ reason }}</span></div>
    <div class="dock-actions">
      <t-button
        theme="primary"
        :loading="busy === 'patrol'"
        :disabled="Boolean(busy)"
        @click="$emit('patrol')"
        ><ControlPlatformIcon />{{ busy === 'patrol' ? '下发中…' : '开始巡检' }}</t-button
      ><t-button
        variant="outline"
        :loading="busy === 'stop'"
        :disabled="Boolean(busy)"
        @click="$emit('stop')"
        ><StopCircleIcon />{{ busy === 'stop' ? '下发中…' : '停止巡检' }}</t-button
      ><t-button
        variant="outline"
        :loading="busy === 'home'"
        :disabled="Boolean(busy)"
        @click="$emit('home')"
        ><HomeIcon />{{ busy === 'home' ? '下发中…' : '返回等待区' }}</t-button
      ><button
        class="hold-estop"
        aria-label="软件急停"
        :disabled="Boolean(busy)"
        :title="reason || '按住 0.8 秒确认；不替代车辆物理急停'"
        @pointerdown="hold.start"
        @pointerup="hold.cancel"
        @pointercancel="hold.cancel"
        @pointerleave="hold.cancel"
        @keydown="keyDown"
        @keyup="hold.cancel"
      >
        <span :style="{ width: `${hold.progress.value}%` }"></span>
        <strong>软件急停</strong>
        <small>按住 0.8 秒确认</small>
      </button>
    </div>
  </section>
</template>
