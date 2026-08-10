<script setup lang="ts">
import { onMounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'

const auth = useAuthStore(),
  monitor = useMonitorStore()
const rows = ref<any[]>([]),
  notice = ref(''),
  busy = ref(false)
async function load(): Promise<void> {
  rows.value = (await api.get('/tasks')).data
}
async function createPatrol(): Promise<void> {
  const slot = monitor.snapshot.parking_slots[0]
  if (!slot) {
    notice.value = '地图尚未加载'
    return
  }
  busy.value = true
  try {
    await api.post(
      '/tasks/patrol',
      {
        robot_id: 'R001',
        target_parking_slot_id: slot.id,
        trajectory_id: monitor.snapshot.trajectories[0]?.id,
        parameters: {},
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    notice.value = '巡检任务已创建：created → queued'
    await load()
  } catch (error) {
    notice.value = errorMessage(error)
  } finally {
    busy.value = false
  }
}
async function cancel(row: any): Promise<void> {
  try {
    await api.post(`/tasks/${row.id}/cancel`, {}, { headers: { 'Idempotency-Key': newUuid() } })
    await load()
  } catch (error) {
    notice.value = errorMessage(error)
  }
}
onMounted(async () => {
  await Promise.all([load(), monitor.snapshot.map_version ? Promise.resolve() : monitor.loadSnapshot()])
})
</script>

<template>
  <PageHeader
    eyebrow="EXECUTION POLICY"
    title="任务调度"
    description="任务快照固化目标、地图版本和轨迹；冲突由 Robot Execution Policy 拒绝。"
    ><button
      v-if="auth.can('patrol.create')"
      class="primary-button compact"
      :disabled="busy"
      @click="createPatrol"
    >
      创建巡检任务
    </button></PageHeader
  >
  <p v-if="notice" class="inline-notice">{{ notice }}</p>
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :columns="[
        { key: 'task_code', label: '任务编号' },
        { key: 'type', label: '类型' },
        { key: 'status', label: '状态' },
        { key: 'phase', label: '阶段' },
        { key: 'progress', label: '进度' },
        { key: 'map_version_snapshot', label: '地图版本' },
        { key: 'created_at', label: '创建时间' },
        { key: 'actions', label: '操作' },
      ]"
    >
      <template #status="{ value }"><StateChip :value="String(value)" /></template>
      <template #progress="{ value }"
        ><span>{{ value }}%</span></template
      >
      <template #actions="{ row }"
        ><button
          v-if="!['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(row.status) && auth.can('robot.control.task')"
          class="table-button"
          @click="cancel(row)"
        >
          取消
        </button></template
      >
    </DataTable>
  </section>
</template>
