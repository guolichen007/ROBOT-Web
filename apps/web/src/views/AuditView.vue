<script setup lang="ts">
import { onMounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api } from '@/lib/api'

const rows = ref<any[]>([])
onMounted(async () => {
  rows.value = (await api.get('/admin/audit', { params: { limit: 500 } })).data
})
</script>

<template>
  <PageHeader
    eyebrow="IMMUTABLE TRAIL"
    title="审计日志"
    description="关键控制贯通 operator、robot、command、target、time 与 result。"
    ><span class="policy-badge">RETENTION 365D</span></PageHeader
  >
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :columns="[
        { key: 'created_at', label: '时间' },
        { key: 'action', label: '动作' },
        { key: 'resource_type', label: '资源' },
        { key: 'resource_id', label: '资源 ID' },
        { key: 'robot_id', label: '机器人' },
        { key: 'result', label: '结果' },
        { key: 'request_id', label: '请求 ID' },
      ]"
    >
      <template #result="{ value }"><StateChip :value="String(value)" /></template>
    </DataTable>
  </section>
</template>
