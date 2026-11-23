<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { useAsyncList } from '@/composables/useAsyncList'
import { api, errorMessage } from '@/lib/api'
import { canExport, cleanParams, exportCsv } from '@/lib/records'
import { auditActionLabel, resourceTypeLabel, taskTypeLabel } from '@/lib/ui-labels'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'

type Kind = 'tasks' | 'plan-previews' | 'alarms' | 'operations' | 'audit'

const auth = useAuthStore()
const monitor = useMonitorStore()
const tabs = computed(() =>
  (
    [
      { id: 'tasks', label: '任务', permission: 'robot.read' },
      { id: 'plan-previews', label: '计划预演', permission: 'robot.read' },
      { id: 'alarms', label: '告警', permission: 'alarm.read' },
      { id: 'operations', label: '人员操作', permission: 'audit.read' },
      { id: 'audit', label: '审计', permission: 'audit.read' },
    ] as Array<{ id: Kind; label: string; permission: string }>
  ).filter((tab) => auth.can(tab.permission)),
)
const kind = ref<Kind>(auth.can('audit.read') ? 'operations' : 'tasks')
const filters = reactive({ robot_id: '', user_id: '', action: '', state: '', status: '', start: '', end: '' })
const users = ref<Array<{ id: string; display_name: string; username: string }>>([])
const detail = ref<Record<string, any> | null>(null)
const notice = ref('')
const exportAllowed = computed(() => canExport(auth.user?.permissions, kind.value))

const { rows, loading, error, load } = useAsyncList<Record<string, any>>(async () => {
  const params = cleanParams({ ...filters, limit: 1000 })
  return (await api.get(`/history/${kind.value}`, { params })).data
})

const columns = computed(() => {
  switch (kind.value) {
    case 'tasks':
      return [
        { key: 'created_at', label: '时间' },
        { key: 'task_code', label: '任务编号' },
        { key: 'robot_vehicle_id', label: '车辆' },
        { key: 'type', label: '类型' },
        { key: 'status', label: '状态' },
        { key: 'created_by_name', label: '创建人' },
        { key: 'actions', label: '操作' },
      ]
    case 'plan-previews':
      return [
        { key: 'scheduled_for', label: '预演时间' },
        { key: 'plan_name', label: '计划' },
        { key: 'robot_vehicle_id', label: '车辆' },
        { key: 'state', label: '状态' },
        { key: 'reason_code', label: '说明' },
        { key: 'created_at', label: '记录时间' },
      ]
    case 'alarms':
      return [
        { key: 'last_seen_at', label: '最后出现' },
        { key: 'event_code', label: '事件编号' },
        { key: 'robot_vehicle_id', label: '车辆' },
        { key: 'location_state', label: '位置' },
        { key: 'severity', label: '严重度' },
        { key: 'state', label: '状态' },
        { key: 'ack_by_name', label: '确认人' },
        { key: 'actions', label: '操作' },
      ]
    default:
      return [
        { key: 'created_at', label: '时间' },
        { key: 'user_name', label: '人员' },
        { key: 'action', label: '动作' },
        { key: 'resource_type', label: '资源' },
        { key: 'robot_vehicle_id', label: '车辆' },
        { key: 'result', label: '结果' },
        { key: 'actions', label: '操作' },
      ]
  }
})

async function switchTab(next: Kind): Promise<void> {
  kind.value = next
  detail.value = null
  await load()
}

async function doExport(): Promise<void> {
  notice.value = ''
  try {
    await exportCsv(kind.value, { ...filters, status: filters.status, state: filters.state })
  } catch (reason) {
    notice.value = errorMessage(reason)
  }
}

onMounted(async () => {
  if (!monitor.snapshot.robots.length) await monitor.loadSnapshot().catch(() => undefined)
  if (auth.can('user.manage')) {
    users.value = (await api.get('/admin/users').catch(() => ({ data: [] }))).data
  }
  await load()
})
</script>

<template>
  <PageHeader
    eyebrow="历史与审计"
    title="记录与审计"
    description="任务、计划预演、告警、人员操作与审计；导出内容与当前筛选一致。"
    ><button
      v-if="exportAllowed"
      class="secondary-button compact"
      type="button"
      data-testid="records-export"
      @click="doExport"
    >
      导出
    </button></PageHeader
  >
  <nav class="tabs" role="tablist">
    <button
      v-for="tab in tabs"
      :key="tab.id"
      type="button"
      role="tab"
      :aria-selected="kind === tab.id"
      :class="{ active: kind === tab.id }"
      :data-testid="`records-tab-${tab.id}`"
      @click="switchTab(tab.id)"
    >
      {{ tab.label }}
    </button>
  </nav>
  <p v-if="notice" class="inline-notice" data-theme="error">{{ notice }}</p>
  <form class="filter-bar" @submit.prevent="load">
    <label>开始日期<input v-model="filters.start" type="date" /></label>
    <label>结束日期<input v-model="filters.end" type="date" /></label>
    <label
      >车辆<select v-model="filters.robot_id">
        <option value="">全部</option>
        <option v-for="robot in monitor.snapshot.robots" :key="robot.vehicle_id" :value="robot.vehicle_id">
          {{ robot.vehicle_id }}
        </option>
      </select></label
    >
    <label v-if="(kind === 'operations' || kind === 'audit') && users.length"
      >人员<select v-model="filters.user_id">
        <option value="">全部</option>
        <option v-for="user in users" :key="user.id" :value="user.id">{{ user.display_name }}</option>
      </select></label
    >
    <label v-if="kind === 'operations' || kind === 'audit'"
      >类型<input v-model="filters.action" placeholder="如 USER_ / ALARM_ / PATROL_PLAN"
    /></label>
    <label v-if="kind === 'alarms'"
      >状态<select v-model="filters.state">
        <option value="">全部</option>
        <option
          v-for="s in ['NEW', 'ACKNOWLEDGED', 'CONFIRMED', 'RESOLVED', 'CLOSED', 'DISMISSED']"
          :key="s"
          :value="s"
        >
          {{ s }}
        </option>
      </select></label
    >
    <label v-if="kind === 'tasks'"
      >状态<select v-model="filters.status">
        <option value="">全部</option>
        <option v-for="s in ['QUEUED', 'EXECUTING', 'SUCCEEDED', 'FAILED', 'CANCELLED']" :key="s" :value="s">
          {{ s }}
        </option>
      </select></label
    >
    <div class="filter-actions">
      <button class="primary-button compact" type="submit" data-testid="records-query">查询</button>
    </div>
  </form>
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :loading="loading"
      :error="error"
      :columns="columns"
      empty="没有符合条件的记录"
      @retry="load"
    >
      <template #type="{ value }">{{ taskTypeLabel(String(value)) }}</template>
      <template #status="{ value }"><StateChip :value="String(value)" /></template>
      <template #state="{ value }"><StateChip :value="String(value)" /></template>
      <template #result="{ value }"><StateChip :value="String(value)" /></template>
      <template #location_state="{ row }">{{
        row.location_state === 'UNMATCHED' ? `未匹配（${row.reported_slot_code || '无编码'}）` : '已匹配'
      }}</template>
      <template #action="{ value }"
        ><span :title="String(value)">{{ auditActionLabel(String(value)) }}</span></template
      >
      <template #resource_type="{ value }">{{ resourceTypeLabel(String(value)) }}</template>
      <template #actions="{ row }"
        ><div class="table-actions"><button type="button" @click="detail = row">详情</button></div></template
      >
    </DataTable>
  </section>
  <div v-if="detail" class="drawer-shade" @click.self="detail = null">
    <div class="drawer-card" data-testid="record-detail">
      <button type="button" class="modal-close" aria-label="关闭" @click="detail = null">×</button>
      <h2>记录详情</h2>
      <dl class="detail-grid">
        <template v-for="(value, key) in detail" :key="key">
          <dt>{{ key }}</dt>
          <dd>
            {{
              value === null || value === '' ? '—' : typeof value === 'object' ? JSON.stringify(value) : value
            }}
          </dd>
        </template>
      </dl>
    </div>
  </div>
</template>
