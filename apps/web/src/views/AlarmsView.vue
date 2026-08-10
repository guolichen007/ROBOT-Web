<script setup lang="ts">
import { onMounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api, errorMessage } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore(),
  rows = ref<any[]>([]),
  notice = ref('')
async function load(): Promise<void> {
  rows.value = (await api.get('/alarms')).data
}
async function action(row: any, name: string): Promise<void> {
  try {
    await api.post(`/alarms/${row.id}/${name}`)
    await load()
  } catch (error) {
    notice.value = errorMessage(error)
  }
}
onMounted(load)
</script>

<template>
  <PageHeader
    eyebrow="FIRE EVENT LIFECYCLE"
    title="火情报警"
    description="自动报警与人工火情进入同一生命周期，重复事件按指纹和时间窗合并。"
    ><span class="policy-badge danger"
      >{{ rows.filter((r) => !['RESOLVED', 'CLOSED', 'DISMISSED'].includes(r.state)).length }} ACTIVE</span
    ></PageHeader
  >
  <p v-if="notice" class="inline-notice">{{ notice }}</p>
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :columns="[
        { key: 'event_code', label: '事件编号' },
        { key: 'severity', label: '严重度' },
        { key: 'fire_type', label: '类型' },
        { key: 'detection_method', label: '来源' },
        { key: 'state', label: '状态' },
        { key: 'occurrence_count', label: '合并计数' },
        { key: 'last_seen_at', label: '最后出现' },
        { key: 'actions', label: '操作' },
      ]"
    >
      <template #state="{ value }"><StateChip :value="String(value)" /></template>
      <template #severity="{ value }"
        ><span class="severity-label" :data-level="value">{{ value }}</span></template
      >
      <template #actions="{ row }"
        ><div class="table-actions">
          <button v-if="row.state === 'NEW' && auth.can('alarm.ack')" @click="action(row, 'acknowledge')">
            确认收到</button
          ><button
            v-if="!['RESOLVED', 'CLOSED', 'DISMISSED'].includes(row.state) && auth.can('alarm.resolve')"
            @click="action(row, 'resolve')"
          >
            解决
          </button>
        </div></template
      >
    </DataTable>
  </section>
</template>
