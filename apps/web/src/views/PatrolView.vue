<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { useAsyncList } from '@/composables/useAsyncList'
import { api, downloadAuthenticated, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { cleanParams, scheduleLabel, WEEKDAY_OPTIONS, type ScheduleSpec } from '@/lib/records'
import { useAuthStore } from '@/stores/auth'

interface PlanRow {
  id: string
  code: string
  name: string
  description: string | null
  status: string
  execution_mode: string
  revision: number
  robot_id: string
  robot_vehicle_id: string | null
  trajectory_id: string | null
  trajectory_code: string | null
  map_version: string | null
  work_side: string
  target_lateral_min_m: number | null
  target_lateral_max_m: number | null
  lateral_verification: string
  schedule: Partial<ScheduleSpec>
  created_by_name: string | null
  updated_at: string | null
}
interface RouteOption {
  id: string
  code: string
  map_version: string
  point_count: number
}
interface RobotOption {
  id: string
  vehicle_id: string
  name: string
  online_state: string
  routes: RouteOption[]
  right_side_mechanism: { verification_state: string; registered: boolean; note: string }
}

const auth = useAuthStore()
const filters = reactive({ status: '', robot_id: '', q: '' })
const { rows, loading, error, load } = useAsyncList<PlanRow>(
  async () => (await api.get('/plans', { params: cleanParams(filters) })).data,
)
const options = ref<{ robots: RobotOption[]; lateral_range_m: [number, number] } | null>(null)
const optionsError = ref('')
const reports = ref<Array<Record<string, any>>>([])
const notice = ref('')
const noticeTheme = ref<'ok' | 'error'>('ok')
const busy = ref(false)
const drawer = ref<'form' | 'detail' | ''>('')
const editing = ref<PlanRow | null>(null)
const detail = ref<Record<string, any> | null>(null)
const previewTimes = ref<string[]>([])
const previewError = ref('')
let createKey = ''

const form = reactive({
  name: '',
  robot_id: '',
  trajectory_id: '',
  description: '',
  frequency: 'DAILY' as 'DAILY' | 'WEEKLY',
  time: '09:00',
  weekdays: [] as string[],
  target_lateral_min_m: 0.2,
  target_lateral_max_m: 0.3,
})

const canEdit = computed(() => auth.can('patrol.create'))
const robots = computed(() => options.value?.robots || [])
const selectedRobot = computed(() => robots.value.find((robot) => robot.id === form.robot_id))
const routes = computed(() => selectedRobot.value?.routes || [])
// 明确原因代替“永久禁用”：只在缺少前置数据时禁用，并说明原因。
const createBlocker = computed(() => {
  if (!canEdit.value) return '当前账号没有创建巡检计划的权限'
  if (optionsError.value) return `无法加载车辆与路线：${optionsError.value}`
  if (options.value && !robots.value.length) return '尚未登记车辆，无法新建计划'
  return ''
})
const formBlocker = computed(() => {
  if (!form.robot_id) return '请选择执行车辆'
  if (!routes.value.length) return '该车辆尚未配置路线'
  if (!form.trajectory_id) return '请选择巡检路线'
  if (form.frequency === 'WEEKLY' && !form.weekdays.length) return '每周计划至少选择一天'
  if (form.target_lateral_min_m > form.target_lateral_max_m) return '目标侧向间距下限不能大于上限'
  return ''
})

// 切换车辆时清空路线：绝不自动替用户选择第一条路线。
watch(
  () => form.robot_id,
  () => {
    if (!routes.value.some((route) => route.id === form.trajectory_id)) form.trajectory_id = ''
  },
)

function say(message: string, theme: 'ok' | 'error' = 'ok'): void {
  notice.value = message
  noticeTheme.value = theme
}

function spec(): ScheduleSpec {
  return {
    frequency: form.frequency,
    time: form.time,
    weekdays: form.frequency === 'WEEKLY' ? form.weekdays : [],
    timezone: 'Asia/Shanghai',
  }
}

async function loadOptions(): Promise<void> {
  optionsError.value = ''
  try {
    options.value = (await api.get('/plans/options')).data
  } catch (reason) {
    optionsError.value = errorMessage(reason)
  }
}

async function loadReports(): Promise<void> {
  if (!auth.can('patrol.report.read')) return
  try {
    reports.value = (await api.get('/patrol-reports')).data
  } catch {
    reports.value = []
  }
}

function openCreate(): void {
  if (createBlocker.value) return
  editing.value = null
  Object.assign(form, {
    name: '',
    robot_id: '',
    trajectory_id: '',
    description: '',
    frequency: 'DAILY',
    time: '09:00',
    weekdays: [],
    target_lateral_min_m: 0.2,
    target_lateral_max_m: 0.3,
  })
  previewTimes.value = []
  previewError.value = ''
  createKey = newUuid()
  drawer.value = 'form'
}

function openEdit(row: PlanRow): void {
  editing.value = row
  Object.assign(form, {
    name: row.name,
    robot_id: row.robot_id,
    trajectory_id: row.trajectory_id || '',
    description: row.description || '',
    frequency: row.schedule.frequency || 'DAILY',
    time: row.schedule.time || '09:00',
    weekdays: [...(row.schedule.weekdays || [])],
    target_lateral_min_m: row.target_lateral_min_m ?? 0.2,
    target_lateral_max_m: row.target_lateral_max_m ?? 0.3,
  })
  previewTimes.value = []
  previewError.value = ''
  drawer.value = 'form'
}

async function openDetail(row: PlanRow): Promise<void> {
  drawer.value = 'detail'
  detail.value = null
  try {
    detail.value = (await api.get(`/plans/${row.id}`)).data
  } catch (reason) {
    say(errorMessage(reason), 'error')
    drawer.value = ''
  }
}

async function previewSchedule(): Promise<void> {
  previewError.value = ''
  try {
    const { data } = await api.post('/plans/preview', { schedule: spec(), count: 5 })
    previewTimes.value = data.occurrences
  } catch (reason) {
    previewTimes.value = []
    previewError.value = errorMessage(reason)
  }
}

async function save(): Promise<void> {
  if (busy.value || formBlocker.value) return
  busy.value = true
  const body = {
    name: form.name,
    robot_id: form.robot_id,
    trajectory_id: form.trajectory_id,
    description: form.description,
    schedule: spec(),
    target_lateral_min_m: Number(form.target_lateral_min_m),
    target_lateral_max_m: Number(form.target_lateral_max_m),
    work_side: 'RIGHT',
  }
  try {
    if (editing.value) {
      await api.put(`/plans/${editing.value.id}`, { ...body, revision: editing.value.revision })
      say('计划已保存（仅预演草稿，未发送任何车辆指令）')
    } else {
      await api.post('/plans', body, { headers: { 'Idempotency-Key': createKey } })
      say('草稿已保存（仅预演，未发送任何车辆指令）')
    }
    drawer.value = ''
    await load()
  } catch (reason) {
    say(errorMessage(reason), 'error')
  } finally {
    busy.value = false
  }
}

async function act(row: PlanRow, action: 'pause' | 'resume' | 'preview-run'): Promise<void> {
  try {
    const { data } = await api.post(`/plans/${row.id}/${action}`)
    if (action === 'preview-run') say(`已记录 ${data.occurrences.length} 个预演时间（未生成任务或命令）`)
    else say(action === 'pause' ? '计划已暂停' : '计划已恢复为草稿')
    await load()
    if (drawer.value === 'detail') await openDetail(row)
  } catch (reason) {
    say(errorMessage(reason), 'error')
  }
}

async function download(report: Record<string, any>, format: 'pdf' | 'xlsx'): Promise<void> {
  try {
    await downloadAuthenticated(
      `/patrol-reports/${report.id}/download/${format}`,
      `${report.report_code}.${format}`,
    )
  } catch (reason) {
    say(errorMessage(reason), 'error')
  }
}

const fmt = (value?: string | null): string => (value ? new Date(value).toLocaleString('zh-CN') : '—')
const lateral = (row: { target_lateral_min_m: number | null; target_lateral_max_m: number | null }) =>
  row.target_lateral_min_m == null
    ? '未设置'
    : `${row.target_lateral_min_m.toFixed(2)}–${(row.target_lateral_max_m ?? 0).toFixed(2)} m`

onMounted(() => Promise.all([load(), loadOptions(), loadReports()]))
</script>

<template>
  <PageHeader
    eyebrow="巡检计划"
    title="巡检计划"
    description="右侧作业巡检计划：保存草稿与定时预演，不发送车辆指令。"
  >
    <button
      class="primary-button compact"
      type="button"
      data-testid="plan-create"
      :disabled="Boolean(createBlocker)"
      :title="createBlocker || '新建巡检计划'"
      @click="openCreate"
    >
      新建计划
    </button>
  </PageHeader>
  <p v-if="createBlocker" class="inline-notice">{{ createBlocker }}</p>
  <p v-if="notice" class="inline-notice" :data-theme="noticeTheme" role="status">{{ notice }}</p>
  <p class="safety-note">
    <strong>仅保存及预演，不发送车辆指令</strong>
    本期新建计划在服务端强制为 PREVIEW_ONLY；真实巡检与防火罩展开仍需后续现场验收和独立授权。
  </p>
  <form class="filter-bar" @submit.prevent="load">
    <label
      >状态<select v-model="filters.status">
        <option value="">全部</option>
        <option value="DRAFT">草稿</option>
        <option value="PAUSED">已暂停</option>
        <option value="ACTIVE">历史可执行</option>
      </select></label
    >
    <label
      >车辆<select v-model="filters.robot_id">
        <option value="">全部</option>
        <option v-for="robot in robots" :key="robot.id" :value="robot.id">{{ robot.vehicle_id }}</option>
      </select></label
    >
    <label>名称<input v-model="filters.q" placeholder="计划名称 / 编号" /></label>
    <div class="filter-actions"><button class="secondary-button compact" type="submit">查询</button></div>
  </form>
  <section class="panel data-panel">
    <DataTable
      :rows="rows"
      :loading="loading"
      :error="error"
      empty="暂无巡检计划，点击右上角「新建计划」创建"
      :columns="[
        { key: 'name', label: '计划名称' },
        { key: 'robot_vehicle_id', label: '执行车辆' },
        { key: 'trajectory_code', label: '巡检路线' },
        { key: 'schedule', label: '执行周期' },
        { key: 'work_side', label: '作业方向' },
        { key: 'lateral', label: '目标侧向间距' },
        { key: 'status', label: '状态' },
        { key: 'actions', label: '操作' },
      ]"
      @retry="load"
    >
      <template #schedule="{ row }">{{ scheduleLabel(row.schedule) }}</template>
      <template #work_side="{ value }">{{ value === 'RIGHT' ? '右侧' : value }}</template>
      <template #lateral="{ row }">{{ lateral(row as PlanRow) }}</template>
      <template #status="{ row }"
        ><StateChip :value="String(row.status)" />
        <small v-if="row.execution_mode === 'PREVIEW_ONLY'" class="form-hint"> 仅预演</small></template
      >
      <template #actions="{ row }"
        ><div class="table-actions">
          <button type="button" @click="openDetail(row as PlanRow)">详情</button>
          <template v-if="canEdit && row.execution_mode === 'PREVIEW_ONLY'">
            <button type="button" @click="openEdit(row as PlanRow)">编辑</button>
            <button type="button" @click="act(row as PlanRow, 'preview-run')">预演</button>
            <button v-if="row.status === 'DRAFT'" type="button" @click="act(row as PlanRow, 'pause')">
              暂停
            </button>
            <button v-else-if="row.status === 'PAUSED'" type="button" @click="act(row as PlanRow, 'resume')">
              恢复草稿
            </button>
          </template>
        </div></template
      >
    </DataTable>
  </section>

  <section v-if="auth.can('patrol.report.read') && reports.length" class="panel content-card">
    <div class="panel-heading">
      <h3>巡检报告</h3>
      <span>PDF / Excel</span>
    </div>
    <div v-for="report in reports" :key="report.id" class="business-list-row">
      <div>
        <strong>{{ report.report_code }}</strong
        ><small>{{ report.status }} · {{ report.generated_at || '等待生成' }}</small>
      </div>
      <div v-if="report.status === 'READY'" class="inline-actions">
        <button type="button" @click="download(report, 'pdf')">PDF</button>
        <button type="button" @click="download(report, 'xlsx')">Excel</button>
      </div>
    </div>
  </section>

  <div v-if="drawer" class="drawer-shade" @click.self="drawer = ''">
    <form
      v-if="drawer === 'form'"
      class="drawer-card user-form"
      data-testid="plan-drawer"
      @submit.prevent="save"
    >
      <button type="button" class="modal-close" aria-label="关闭" @click="drawer = ''">×</button>
      <span class="eyebrow">草稿模式</span>
      <h2>{{ editing ? `编辑计划 · ${editing.code}` : '新建巡检计划' }}</h2>
      <label>计划名称<input v-model="form.name" name="name" required maxlength="128" /></label>
      <label
        >执行车辆<select v-model="form.robot_id" name="robot_id" required>
          <option value="" disabled>选择已登记车辆</option>
          <option v-for="robot in robots" :key="robot.id" :value="robot.id">
            {{ robot.vehicle_id }} · {{ robot.name }}
          </option>
        </select></label
      >
      <label
        >巡检路线<select
          v-model="form.trajectory_id"
          name="trajectory_id"
          :disabled="!routes.length"
          required
        >
          <option value="" disabled>
            {{ form.robot_id && !routes.length ? '尚未配置路线' : '选择已有路线' }}
          </option>
          <option v-for="route in routes" :key="route.id" :value="route.id">
            {{ route.code }} · 地图 V{{ route.map_version }} · {{ route.point_count }} 点
          </option>
        </select>
        <span v-if="form.robot_id && !routes.length" class="form-hint" data-testid="no-route">
          该车辆尚未配置路线，请先在地图版本中发布路线
        </span></label
      >
      <label
        >执行周期<select v-model="form.frequency" name="frequency">
          <option value="DAILY">每天</option>
          <option value="WEEKLY">每周</option>
        </select></label
      >
      <fieldset v-if="form.frequency === 'WEEKLY'" class="weekday-picker">
        <legend>星期</legend>
        <label v-for="day in WEEKDAY_OPTIONS" :key="day.value"
          ><input v-model="form.weekdays" type="checkbox" :value="day.value" />周{{ day.label }}</label
        >
      </fieldset>
      <label>时间<input v-model="form.time" name="time" type="time" required /></label>
      <label>作业方向<input value="右侧 · 固定" disabled /></label>
      <label
        >目标侧向间距（米）
        <span style="display: flex; gap: 8px">
          <input v-model.number="form.target_lateral_min_m" type="number" min="0.2" max="0.3" step="0.01" />
          <input v-model.number="form.target_lateral_max_m" type="number" min="0.2" max="0.3" step="0.01" />
        </span>
        <span class="form-hint"
          >允许范围 0.20–0.30 m；仅记录，不进入导航或机械控制，未经实测显示“未验证”</span
        ></label
      >
      <label>备注<input v-model="form.description" maxlength="2000" /></label>
      <div>
        <button
          type="button"
          class="secondary-button compact"
          data-testid="plan-preview"
          @click="previewSchedule"
        >
          预览时间
        </button>
        <ul v-if="previewTimes.length" class="preview-times" data-testid="preview-times">
          <li v-for="time in previewTimes" :key="time">{{ fmt(time) }}</li>
        </ul>
        <p v-if="previewError" class="inline-notice" data-theme="error">{{ previewError }}</p>
      </div>
      <p class="safety-note">
        <strong>仅保存及预演，不发送车辆指令</strong>真实巡检与防火罩展开仍需后续现场验收和独立授权。
      </p>
      <p v-if="formBlocker" class="form-hint" data-testid="form-blocker">{{ formBlocker }}</p>
      <div class="drawer-actions">
        <button type="button" class="secondary-button compact" @click="drawer = ''">取消</button>
        <button
          class="primary-button compact"
          type="submit"
          data-testid="plan-save"
          :disabled="busy || Boolean(formBlocker)"
          :title="formBlocker || '保存草稿（仅预演，不发送车辆指令）'"
        >
          {{ busy ? '保存中…' : '保存草稿' }}
        </button>
      </div>
    </form>

    <div v-else class="drawer-card" data-testid="plan-detail">
      <button type="button" class="modal-close" aria-label="关闭" @click="drawer = ''">×</button>
      <template v-if="detail">
        <span class="eyebrow">{{ detail.code }} · 修订 {{ detail.revision }}</span>
        <h2>{{ detail.name }}</h2>
        <dl class="detail-grid">
          <dt>状态</dt>
          <dd>
            <StateChip :value="String(detail.status)" />
            {{ detail.execution_mode === 'PREVIEW_ONLY' ? '仅预演' : '' }}
          </dd>
          <dt>执行车辆</dt>
          <dd>{{ detail.robot_vehicle_id }} · {{ detail.robot_name }}</dd>
          <dt>巡检路线</dt>
          <dd>{{ detail.trajectory_code || '尚未配置路线' }} · 地图 V{{ detail.map_version }}</dd>
          <dt>执行周期</dt>
          <dd>{{ scheduleLabel(detail.schedule) }}（{{ detail.cron_expression }}）</dd>
          <dt>作业方向</dt>
          <dd>右侧</dd>
          <dt>目标侧向间距</dt>
          <dd>{{ lateral(detail as PlanRow) }} · <b>未验证</b></dd>
          <dt>右侧防火罩</dt>
          <dd data-testid="mechanism-state">
            {{ detail.right_side_mechanism?.registered ? '已登记' : '未登记' }} · 未验收 · 平台不控制
          </dd>
          <dt>真实执行</dt>
          <dd>否（不生成任务、命令或 MQTT 下行）</dd>
          <dt>创建人</dt>
          <dd>{{ detail.created_by_name }} · {{ fmt(detail.created_at) }}</dd>
          <dt>更新时间</dt>
          <dd>{{ fmt(detail.updated_at) }}</dd>
        </dl>
        <h3>预演记录</h3>
        <ul v-if="detail.previews?.length" class="timeline-list" data-testid="preview-records">
          <li v-for="item in detail.previews" :key="item.id">
            <time>{{ fmt(item.scheduled_for) }}</time
            ><span>{{ item.state === 'PREVIEW' ? '预演 · 未执行' : item.state }}</span>
          </li>
        </ul>
        <p v-else class="form-hint">尚无预演记录，点击列表中的「预演」生成。</p>
      </template>
      <p v-else class="form-hint">正在加载…</p>
    </div>
  </div>
</template>
