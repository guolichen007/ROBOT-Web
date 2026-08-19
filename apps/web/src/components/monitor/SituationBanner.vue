<script setup lang="ts">
import { AlarmIcon, ErrorCircleIcon } from 'tdesign-icons-vue-next'
import type { Alarm } from '@/types'
defineProps<{ state: string; alarm?: Alarm }>()
defineEmits<{ select: [] }>()
</script>
<template>
  <section v-if="alarm" class="situation-banner critical" @click="$emit('select')">
    <AlarmIcon />
    <strong>消防告警：检测到火情，请立即处理！</strong>
    <span>{{ alarm.event_code }} · {{ alarm.fire_type }} · {{ alarm.severity }}</span>
    <button type="button">查看详情</button>
  </section>
  <section v-else-if="state !== 'NORMAL'" class="situation-banner warning" role="status">
    <ErrorCircleIcon />
    <strong>{{
      state === 'OFFLINE_UNKNOWN' ? '车辆离线，现场态势未知' : '系统降级，数据需核实'
    }}</strong>
    <span>{{ state }}</span>
  </section>
</template>
