<script setup lang="ts">
import type { Alarm } from '@/types'
defineProps<{ alarm: Alarm; canAck: boolean; canConfirm: boolean; canResolve: boolean }>()
defineEmits<{ transition: [action: 'acknowledge' | 'confirm' | 'resolve'] }>()
</script>
<template>
  <div class="alarm-lifecycle">
    <t-button v-if="alarm.state === 'NEW' && canAck" @click="$emit('transition', 'acknowledge')"
      >确认收到</t-button
    ><t-button
      v-if="alarm.state === 'ACKNOWLEDGED' && canConfirm"
      theme="warning"
      @click="$emit('transition', 'confirm')"
      >确认火情</t-button
    ><t-button
      v-if="['CONFIRMED', 'DISPATCHED', 'IN_PROGRESS'].includes(alarm.state) && canResolve"
      variant="outline"
      @click="$emit('transition', 'resolve')"
      >标记解决</t-button
    >
  </div>
</template>
