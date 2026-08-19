<script setup lang="ts">
import { onMounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api } from '@/lib/api'
import { auditActionLabel, resourceTypeLabel } from '@/lib/ui-labels'

const rows = ref<any[]>([])
onMounted(async () => {
  rows.value = (await api.get('/admin/audit', { params: { limit: 500 } })).data
})
</script>

<template>
  <PageHeader
    eyebrow="审计日志"
    title="审计日志"
    description="关键控制贯通操作者、机器人、命令、目标、时间与结果。"
    ><span class="policy-badge">保留 365 天</span></PageHeader
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
      <template #action="{ value }">
        <span :title="String(value)">{{ auditActionLabel(String(value)) }}</span>
      </template>
      <template #resource_type="{ value }">{{ resourceTypeLabel(String(value)) }}</template>
      <template #result="{ value }"><StateChip :value="String(value)" /></template>
    </DataTable>
  </section>
</template>
