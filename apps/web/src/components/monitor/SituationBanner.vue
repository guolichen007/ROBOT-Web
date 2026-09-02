<script setup lang="ts">
import { AlarmIcon, ErrorCircleIcon } from 'tdesign-icons-vue-next'
import { alarmTypeLabel, severityLabel, situationLabel } from '@/lib/ui-labels'
import type { Alarm } from '@/types'
defineProps<{ state: string; alarm?: Alarm }>()
defineEmits<{ select: [] }>()
</script>
<template>
  <section v-if="alarm" class="situation-banner critical" @click="$emit('select')">
    <AlarmIcon />
    <strong>消防告警：检测到火情，请立即处理！</strong>
    <span
      >{{ alarm.event_code }} · {{ alarmTypeLabel(alarm.fire_type) }} ·
      {{ severityLabel(alarm.severity) }}</span
    >
    <button type="button">查看详情</button>
  </section>
  <section v-else-if="state === 'ESTOP_ACTIVE'" class="situation-banner danger" role="status">
    <ErrorCircleIcon />
    <strong>软件急停已生效，车辆保持停止。确认现场安全后可解除急停。</strong>
  </section>
  <section v-else-if="state !== 'NORMAL'" class="situation-banner warning" role="status">
    <ErrorCircleIcon />
    <strong>{{ state === 'OFFLINE_UNKNOWN' ? '车辆离线，现场态势未知' : '系统降级，数据需核实' }}</strong>
    <span>{{ situationLabel(state) }}</span>
  </section>
</template>
