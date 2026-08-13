<script setup lang="ts">
import { AlarmIcon, CheckCircleIcon, ErrorCircleIcon } from 'tdesign-icons-vue-next'
import type { Alarm } from '@/types'
defineProps<{ state: string; alarm?: Alarm }>()
defineEmits<{ select: [] }>()
</script>
<template>
  <section v-if="alarm" class="situation-banner critical" @click="$emit('select')">
    <AlarmIcon /><strong>{{ alarm.event_code }} 活动火情</strong
    ><span>{{ alarm.fire_type }} · {{ alarm.severity }}</span
    ><button>进入处置</button>
  </section>
  <section v-else class="situation-banner" :class="state.toLowerCase()">
    <CheckCircleIcon v-if="state === 'NORMAL'" /><ErrorCircleIcon v-else /><strong>{{
      state === 'NORMAL'
        ? '运行态势正常'
        : state === 'OFFLINE_UNKNOWN'
          ? '车辆离线，现场态势未知'
          : '系统降级，数据需核实'
    }}</strong
    ><span>{{ state }}</span>
  </section>
</template>
