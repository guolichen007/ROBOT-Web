<script setup lang="ts">
import { computed } from 'vue'
import { robotModeLabel, streamStateLabel, supportStateLabel, taskTypeLabel } from '@/lib/ui-labels'
import type { RobotState, StreamInfo, Task } from '@/types'

const props = defineProps<{ robot?: RobotState; task?: Task; stream?: StreamInfo; freshness: string }>()

const mode = computed(() => {
  if (props.robot?.estop_active) return robotModeLabel('ESTOP')
  if (props.task?.type === 'EXTINGUISH') return robotModeLabel('EXTINGUISHING')
  if (props.task?.type === 'RETURN_DOCK') return robotModeLabel('RETURNING')
  if (props.task) return robotModeLabel('PATROLLING')
  return robotModeLabel(props.robot?.online_state === 'ONLINE' ? 'IDLE' : undefined)
})
const rightDetection = computed(() => {
  const profile = props.robot?.sensor_profiles?.find((p) => p.nominal_side?.toUpperCase() === 'RIGHT')
  if (!profile) return '未接入'
  return supportStateLabel(profile.support_state)
})
const rows = computed(() => [
  { label: '机器人编号', value: props.robot?.vehicle_id || '--' },
  { label: '当前模式', value: mode.value },
  { label: '当前任务', value: props.task ? taskTypeLabel(props.task.type) : '空闲' },
  { label: '任务编号', value: props.task?.task_code || '--' },
  { label: '右侧检测', value: rightDetection.value },
  { label: '视频状态', value: streamStateLabel(props.stream?.state) },
])
</script>

<template>
  <section class="panel device-snapshot">
    <div class="ds-head">
      <span>设备快照</span>
      <small>数据 {{ freshness }}</small>
    </div>
    <dl class="ds-grid">
      <div v-for="row in rows" :key="row.label">
        <dt>{{ row.label }}</dt>
        <dd>{{ row.value }}</dd>
      </div>
    </dl>
  </section>
</template>
