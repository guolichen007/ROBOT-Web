<script setup lang="ts">
import { onMounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api, errorMessage } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const sites = ref<any[]>([]),
  maps = ref<any[]>([]),
  versions = ref<any[]>([])
const notice = ref('')
async function load(): Promise<void> {
  ;[sites.value, maps.value, versions.value] = await Promise.all(
    ['/sites', '/maps', '/map-versions'].map((url) => api.get(url).then((r) => r.data)),
  )
}
async function action(id: string, name: 'publish' | 'archive'): Promise<void> {
  try {
    await api.post(`/map-versions/${id}/${name}`)
    await load()
    notice.value = name === 'publish' ? '地图版本已发布并成为活动版本' : '地图版本已归档'
  } catch (error) {
    notice.value = errorMessage(error)
  }
}
onMounted(load)
</script>

<template>
  <PageHeader
    eyebrow="地图版本"
    title="地图版本"
    description="Published 版本只读；任何语义变更必须创建新版本。"
    ><span class="policy-badge">世界坐标系</span></PageHeader
  >
  <p v-if="notice" class="inline-notice">{{ notice }}</p>
  <div class="summary-cards">
    <article>
      <span>站点</span><strong>{{ sites.length }}</strong>
    </article>
    <article>
      <span>地图</span><strong>{{ maps.length }}</strong>
    </article>
    <article>
      <span>版本</span><strong>{{ versions.length }}</strong>
    </article>
  </div>
  <section class="panel data-panel">
    <div class="panel-heading"><h3>地图版本清单</h3></div>
    <DataTable
      :rows="versions"
      :columns="[
        { key: 'version', label: '版本' },
        { key: 'status', label: '状态' },
        { key: 'semantic_revision', label: '语义修订' },
        { key: 'frame_id', label: '坐标系' },
        { key: 'checksum', label: '校验和' },
        { key: 'created_at', label: '创建时间' },
        { key: 'actions', label: '操作' },
      ]"
    >
      <template #status="{ value }"><StateChip :value="String(value)" /></template>
      <template #actions="{ row }"
        ><div class="table-actions">
          <button v-if="row.status === 'DRAFT' && auth.can('map.publish')" @click="action(row.id, 'publish')">
            发布</button
          ><button
            v-if="row.status === 'PUBLISHED' && auth.can('map.publish')"
            @click="action(row.id, 'archive')"
          >
            归档</button
          ><span v-if="row.status !== 'DRAFT'">不可原地修改</span>
        </div></template
      >
    </DataTable>
  </section>
</template>
