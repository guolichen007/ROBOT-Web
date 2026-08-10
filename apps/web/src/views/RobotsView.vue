<script setup lang="ts">
import { onMounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api } from '@/lib/api'

const rows = ref<any[]>([])
onMounted(async () => {
  rows.value = (await api.get('/robots')).data
})
</script>

<template>
  <PageHeader eyebrow="FLEET" title="机器人" description="车辆身份、在线状态、地图语境与能力清单。" />
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :columns="[
        { key: 'vehicle_id', label: '车辆 ID' },
        { key: 'name', label: '名称' },
        { key: 'model', label: '型号' },
        { key: 'online_state', label: '连接状态' },
        { key: 'battery', label: '电量' },
        { key: 'current_mode', label: '模式' },
        { key: 'current_map_version', label: '地图版本' },
      ]"
    >
      <template #online_state="{ value }"><StateChip :value="String(value)" /></template>
    </DataTable>
  </section>
</template>
