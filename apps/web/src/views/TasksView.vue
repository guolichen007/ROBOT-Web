<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { useAsyncList } from '@/composables/useAsyncList'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { canExport, cleanParams, exportCsv } from '@/lib/records'
import { taskTypeLabel } from '@/lib/ui-labels'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'

const auth = useAuthStore()
const monitor = useMonitorStore()
const filters = reactive({ robot_id: '', status: '', type: '', start: '', end: '' })
const { rows, loading, error, load } = useAsyncList<Record<string, any>>(
  async () => (await api.get('/tasks', { params: cleanParams(filters) })).data,
)
const notice = ref('')
const detail = ref<Record<string, any> | null>(null)
const robots = computed(() => monitor.snapshot.robots)
const exportAllowed = computed(() => canExport(auth.user?.permissions, 'tasks'))

// WEB-R1-MIN：删除“默认取第一个车位和第一条轨迹”直接创建巡检任务的入口。
// 巡检需在「巡检计划」中由用户明确选择车辆与路线；本页只查看任务记录。

async function openDetail(row: Record<string, any>): Promise<void> {
  try {
    detail.value = (await api.get(`/tasks/${row.id}`)).data
  } catch (reason) {
    notice.value = errorMessage(reason)
  }
}

async function cancel(row: Record<string, any>): Promise<void> {
  try {
    await api.post(`/tasks/${row.id}/cancel`, {}, { headers: { 'Idempotency-Key': newUuid() } })
    await load()
  } catch (reason) {
    notice.value = errorMessage(reason)
  }
}

async function doExport(): Promise<void> {
  try {
    await exportCsv('tasks', filters)
  } catch (reason) {
    notice.value = errorMessage(reason)
  }
}

const fmt = (value?: string | null): string => (value ? new Date(value).toLocaleString('zh-CN') : '—')

onMounted(async () => {
  await Promise.all([load(), monitor.snapshot.map_version ? Promise.resolve() : monitor.loadSnapshot()])
})
</script>

<template>
  <PageHeader
    eyebrow="任务管理"
    title="任务记录"
    description="任务快照固化目标、地图版本和轨迹；巡检计划请在「巡检计划」中创建。"
    ><button v-if="exportAllowed" class="secondary-button compact" type="button" @click="doExport">
      导出
    </button></PageHeader
  >
  <p v-if="notice" class="inline-notice" data-theme="error">{{ notice }}</p>
  <form class="filter-bar" @submit.prevent="load">
    <label
      >车辆<select v-model="filters.robot_id">
        <option value="">全部</option>
        <option v-for="robot in robots" :key="robot.vehicle_id" :value="robot.vehicle_id">
          {{ robot.vehicle_id }}
        </option>
      </select></label
    >
    <label
      >状态<select v-model="filters.status">
        <option value="">全部</option>
        <option
          v-for="s in ['CREATED', 'QUEUED', 'ACCEPTED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'CANCELLED']"
          :key="s"
          :value="s"
        >
          {{ s }}
        </option>
      </select></label
    >
    <label
      >类型<select v-model="filters.type">
        <option value="">全部</option>
        <option
          v-for="t in ['PATROL', 'EXTINGUISH', 'RETURN_DOCK', 'NAVIGATE_TO_PRESET']"
          :key="t"
          :value="t"
        >
          {{ taskTypeLabel(t) }}
        </option>
      </select></label
    >
    <label>开始日期<input v-model="filters.start" type="date" /></label>
    <label>结束日期<input v-model="filters.end" type="date" /></label>
    <div class="filter-actions"><button class="secondary-button compact" type="submit">查询</button></div>
  </form>
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :loading="loading"
      :error="error"
      empty="暂无任务记录（预演计划不会生成任务）"
      :columns="[
        { key: 'task_code', label: '任务编号' },
        { key: 'robot_vehicle_id', label: '车辆' },
        { key: 'type', label: '类型' },
        { key: 'status', label: '状态' },
        { key: 'phase', label: '阶段' },
        { key: 'source', label: '来源' },
        { key: 'created_at', label: '创建时间' },
        { key: 'actions', label: '操作' },
      ]"
      @retry="load"
    >
      <template #status="{ value }"><StateChip :value="String(value)" /></template>
      <template #type="{ value }">{{ taskTypeLabel(String(value)) }}</template>
      <template #actions="{ row }"
        ><div class="table-actions">
          <button type="button" @click="openDetail(row)">查看详情</button>
          <button
            v-if="
              !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(row.status) && auth.can('robot.control.task')
            "
            type="button"
            @click="cancel(row)"
          >
            取消
          </button>
        </div></template
      >
    </DataTable>
  </section>
  <div v-if="detail" class="drawer-shade" @click.self="detail = null">
    <div class="drawer-card" data-testid="task-detail">
      <button type="button" class="modal-close" aria-label="关闭" @click="detail = null">×</button>
      <span class="eyebrow">{{ detail.task_code }}</span>
      <h2>{{ taskTypeLabel(detail.type) }} · <StateChip :value="String(detail.status)" /></h2>
      <dl class="detail-grid">
        <dt>车辆</dt>
        <dd>{{ detail.robot_vehicle_id }}</dd>
        <dt>计划来源</dt>
        <dd>{{ detail.plan ? `${detail.plan.code} · ${detail.plan.name}` : detail.source || '—' }}</dd>
        <dt>地图版本</dt>
        <dd>{{ detail.map_version_snapshot }}</dd>
        <dt>失败原因</dt>
        <dd>{{ detail.failure_code ? `${detail.failure_code} ${detail.failure_message || ''}` : '—' }}</dd>
        <dt>创建时间</dt>
        <dd>{{ fmt(detail.created_at) }}</dd>
        <dt>完成时间</dt>
        <dd>{{ fmt(detail.completed_at) }}</dd>
      </dl>
      <h3>状态变化</h3>
      <ul class="timeline-list">
        <li v-for="event in detail.events" :key="event.id">
          <time>{{ fmt(event.created_at) }}</time
          ><span>{{ event.status }} · {{ event.phase }}</span>
        </li>
      </ul>
      <h3>执行记录（命令）</h3>
      <ul v-if="detail.commands?.length" class="timeline-list">
        <li v-for="cmd in detail.commands" :key="cmd.command_id">
          <time>{{ fmt(cmd.issued_at) }}</time
          ><span>{{ cmd.cmd }} · {{ cmd.lifecycle_status }} {{ cmd.ack_reason || '' }}</span>
        </li>
      </ul>
      <p v-else class="form-hint">无命令记录</p>
    </div>
  </div>
</template>
