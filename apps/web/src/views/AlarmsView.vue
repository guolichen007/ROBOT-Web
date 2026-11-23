<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { useAsyncList } from '@/composables/useAsyncList'
import { alarmLocation, isActiveAlarm } from '@/lib/alarms'
import { api, errorMessage } from '@/lib/api'
import { canExport, cleanParams, exportCsv } from '@/lib/records'
import { alarmTypeLabel, detectionMethodLabel, severityLabel } from '@/lib/ui-labels'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import type { Alarm } from '@/types'

const auth = useAuthStore()
const monitor = useMonitorStore()
const route = useRoute()
const filters = reactive({ state: '', active: '', severity: '', location_state: '', start: '', end: '' })
const { rows, loading, error, load } = useAsyncList<Alarm>(
  async () => (await api.get('/alarms', { params: cleanParams(filters) })).data,
)
const notice = ref('')
const noticeTheme = ref<'ok' | 'error'>('ok')
const detail = ref<Alarm | null>(null)
const actions = ref<Array<Record<string, any>>>([])
const timeline = ref<Array<Record<string, any>>>([])
const note = ref('')
const busy = ref(false)
const exportAllowed = computed(() => canExport(auth.user?.permissions, 'alarms'))
const activeCount = computed(() => rows.value.filter(isActiveAlarm).length)

// 实时火警（WS）到达时刷新列表，保证页面与全局弹窗一致。
watch(
  () => monitor.snapshot.alarms.map((a) => `${a.id}:${a.state}:${a.occurrence_count}`).join('|'),
  () => void load(),
)

async function openDetail(row: Alarm): Promise<void> {
  note.value = ''
  try {
    const [d, a, t] = await Promise.all([
      api.get(`/alarms/${row.id}`),
      api.get(`/alarms/${row.id}/actions`),
      api.get(`/alarms/${row.id}/timeline`),
    ])
    detail.value = d.data
    actions.value = a.data
    timeline.value = t.data
  } catch (reason) {
    notice.value = errorMessage(reason)
    noticeTheme.value = 'error'
  }
}

async function act(name: 'acknowledge' | 'confirm' | 'dismiss' | 'resolve'): Promise<void> {
  if (!detail.value || busy.value) return
  busy.value = true
  try {
    await api.post(`/alarms/${detail.value.id}/${name}`, { note: note.value })
    notice.value = {
      acknowledge: '已确认收到',
      confirm: '已确认火情',
      dismiss: '已排除',
      resolve: '已标记解决',
    }[name]
    noticeTheme.value = 'ok'
    await Promise.all([load(), openDetail(detail.value), monitor.loadSnapshot()])
  } catch (reason) {
    notice.value = errorMessage(reason)
    noticeTheme.value = 'error'
  } finally {
    busy.value = false
  }
}

async function doExport(): Promise<void> {
  try {
    await exportCsv('alarms', filters)
  } catch (reason) {
    notice.value = errorMessage(reason)
    noticeTheme.value = 'error'
  }
}

const fmt = (value?: string | null): string => (value ? new Date(value).toLocaleString('zh-CN') : '—')

onMounted(async () => {
  await load()
  const id = route.query.id
  if (typeof id === 'string') {
    const row = rows.value.find((item) => item.id === id)
    if (row) await openDetail(row)
    else await openDetail({ id } as Alarm)
  }
})
</script>

<template>
  <PageHeader
    eyebrow="火情生命周期"
    title="火情报警"
    description="自动报警与人工火情进入同一生命周期；位置未匹配的火警同样保留并提示。"
    ><span class="policy-badge danger">{{ activeCount }} 条活动</span
    ><button v-if="exportAllowed" class="secondary-button compact" type="button" @click="doExport">
      导出
    </button></PageHeader
  >
  <p v-if="notice" class="inline-notice" :data-theme="noticeTheme" role="status">{{ notice }}</p>
  <form class="filter-bar" @submit.prevent="load">
    <label
      >范围<select v-model="filters.active">
        <option value="">全部</option>
        <option value="true">仅活动</option>
      </select></label
    >
    <label
      >状态<select v-model="filters.state">
        <option value="">全部</option>
        <option
          v-for="s in ['NEW', 'ACKNOWLEDGED', 'CONFIRMED', 'DISPATCHED', 'RESOLVED', 'CLOSED', 'DISMISSED']"
          :key="s"
          :value="s"
        >
          {{ s }}
        </option>
      </select></label
    >
    <label
      >严重度<select v-model="filters.severity">
        <option value="">全部</option>
        <option v-for="s in ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']" :key="s" :value="s">
          {{ severityLabel(s) }}
        </option>
      </select></label
    >
    <label
      >位置<select v-model="filters.location_state">
        <option value="">全部</option>
        <option value="MATCHED">已匹配车位</option>
        <option value="UNMATCHED">位置未匹配</option>
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
      empty="暂无火情记录（这不代表现场监测正常）"
      :columns="[
        { key: 'event_code', label: '事件编号' },
        { key: 'robot_vehicle_id', label: '车辆' },
        { key: 'location', label: '位置' },
        { key: 'severity', label: '严重度' },
        { key: 'fire_type', label: '类型' },
        { key: 'detection_method', label: '来源' },
        { key: 'state', label: '状态' },
        { key: 'occurrence_count', label: '合并计数' },
        { key: 'last_seen_at', label: '最后出现' },
        { key: 'actions', label: '操作' },
      ]"
      @retry="load"
    >
      <template #location="{ row }">{{ alarmLocation(row as Alarm) }}</template>
      <template #state="{ value }"><StateChip :value="String(value)" /></template>
      <template #severity="{ value }"
        ><span class="severity-label" :data-level="value">{{ severityLabel(String(value)) }}</span></template
      >
      <template #fire_type="{ value }">{{ alarmTypeLabel(String(value)) }}</template>
      <template #detection_method="{ value }">{{ detectionMethodLabel(String(value)) }}</template>
      <template #actions="{ row }"
        ><div class="table-actions">
          <button
            type="button"
            :data-testid="`alarm-detail-${row.event_code}`"
            @click="openDetail(row as Alarm)"
          >
            处置详情
          </button>
        </div></template
      >
    </DataTable>
  </section>
  <div v-if="detail" class="drawer-shade" @click.self="detail = null">
    <div class="drawer-card" data-testid="alarm-detail">
      <button type="button" class="modal-close" aria-label="关闭" @click="detail = null">×</button>
      <span class="eyebrow">{{ detail.event_code }}</span>
      <h2>{{ alarmTypeLabel(detail.fire_type) }} · <StateChip :value="String(detail.state)" /></h2>
      <dl class="detail-grid">
        <dt>车辆</dt>
        <dd>{{ detail.robot_vehicle_id || '人工上报' }}</dd>
        <dt>位置</dt>
        <dd :class="{ unmatched: detail.location_state === 'UNMATCHED' }">{{ alarmLocation(detail) }}</dd>
        <dt>严重度</dt>
        <dd>{{ severityLabel(detail.severity) }}</dd>
        <dt>首次 / 最后</dt>
        <dd>{{ fmt(detail.first_seen_at) }} / {{ fmt(detail.last_seen_at) }}</dd>
        <dt>合并计数</dt>
        <dd>{{ detail.occurrence_count }}</dd>
        <dt>确认收到</dt>
        <dd>{{ detail.ack_at ? `${detail.ack_by_name || ''} · ${fmt(detail.ack_at)}` : '未确认' }}</dd>
        <dt>备注</dt>
        <dd style="white-space: pre-wrap">{{ detail.note || '—' }}</dd>
      </dl>
      <label v-if="isActiveAlarm(detail)" class="user-form"
        >处置备注<input v-model="note" maxlength="2000" placeholder="选填，写入审计" data-testid="alarm-note"
      /></label>
      <div class="drawer-actions">
        <button
          v-if="detail.state === 'NEW' && auth.can('alarm.ack')"
          class="primary-button compact"
          type="button"
          data-testid="alarm-ack"
          :disabled="busy"
          @click="act('acknowledge')"
        >
          确认收到
        </button>
        <button
          v-if="['NEW', 'ACKNOWLEDGED'].includes(detail.state) && auth.can('alarm.confirm')"
          class="secondary-button compact"
          type="button"
          :disabled="busy"
          @click="act('confirm')"
        >
          确认火情
        </button>
        <button
          v-if="['NEW', 'ACKNOWLEDGED', 'CONFIRMED'].includes(detail.state) && auth.can('alarm.dismiss')"
          class="secondary-button compact"
          type="button"
          :disabled="busy"
          @click="act('dismiss')"
        >
          排除误报
        </button>
        <button
          v-if="
            ['CONFIRMED', 'DISPATCHED', 'IN_PROGRESS'].includes(detail.state) && auth.can('alarm.resolve')
          "
          class="secondary-button compact"
          type="button"
          :disabled="busy"
          @click="act('resolve')"
        >
          标记解决
        </button>
      </div>
      <h3>处置记录</h3>
      <ul v-if="actions.length" class="timeline-list" data-testid="alarm-actions">
        <li v-for="item in actions" :key="item.id">
          <time>{{ fmt(item.created_at) }}</time
          ><span
            >{{ item.actor || '系统' }} · {{ item.before_state }} → {{ item.after_state
            }}{{ item.note ? ` · ${item.note}` : '' }}</span
          >
        </li>
      </ul>
      <p v-else class="form-hint">暂无人员处置记录</p>
      <h3>事件时间线</h3>
      <ul class="timeline-list">
        <li v-for="(item, index) in timeline" :key="index">
          <time>{{ fmt(item.occurred_at) }}</time
          ><span>{{ item.label }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>
