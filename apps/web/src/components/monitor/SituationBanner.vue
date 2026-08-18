<script setup lang="ts">
import { AlarmIcon } from 'tdesign-icons-vue-next'
import type { Alarm } from '@/types'
defineProps<{ state: string; alarm?: Alarm }>()
defineEmits<{ select: [] }>()
</script>
<template>
  <section v-if="alarm" class="situation-banner critical" @click="$emit('select')">
    <AlarmIcon />
    <strong>{{ alarm.event_code }} 活动火情</strong>
    <span>{{ alarm.fire_type }} · {{ alarm.severity }} · {{ alarm.state }}</span>
    <button>进入处置</button>
  </section>
</template>
