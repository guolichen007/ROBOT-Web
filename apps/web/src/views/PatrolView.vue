<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, errorMessage } from '@/lib/api'

interface PatrolPlan {
  id: string
  code: string
  name: string
  enabled: boolean
  points: Array<{ id: string; sequence: number; dwell_seconds: number }>
}

interface NavigationPreset {
  id: string
  code: string
  name: string
  category: string
  requires_reverse: boolean
  enabled: boolean
}

const plans = ref<PatrolPlan[]>([])
const presets = ref<NavigationPreset[]>([])
const reports = ref<Array<Record<string, any>>>([])
const robotId = ref('')
const loading = ref(false)
const navigatingId = ref('')
const error = ref('')

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [planRows, reportRows, presetRows, robotRows] = await Promise.all([
      api.get('/patrol-plans').then((result) => result.data),
      api.get('/patrol-reports').then((result) => result.data),
      api.get('/navigation-presets').then((result) => result.data),
      api.get('/robots').then((result) => result.data),
    ])
    plans.value = planRows
    reports.value = reportRows
    presets.value = presetRows
    robotId.value = robotRows[0]?.id || ''
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    loading.value = false
  }
}

async function navigate(preset: NavigationPreset): Promise<void> {
  if (!robotId.value || preset.requires_reverse) return
  navigatingId.value = preset.id
  error.value = ''
  try {
    await api.post(
      `/robots/${robotId.value}/navigate-preset`,
      { navigation_preset_id: preset.id },
      { headers: { 'Idempotency-Key': crypto.randomUUID() } },
    )
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    navigatingId.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="standard-page patrol-page">
    <header class="standard-header">
      <div>
        <h1>巡检计划与报告</h1>
        <p>规划固定路线、设置安全调度策略并汇总巡检结果</p>
      </div>
    </header>
    <t-alert v-if="error" theme="error" :message="error" />
    <section class="content-grid two-column">
      <article class="panel content-card">
        <div class="panel-heading">
          <h3>巡检计划</h3>
          <t-button theme="primary" disabled>新建计划</t-button>
        </div>
        <t-skeleton v-if="loading" :row-col="[1, 1, 1]" />
        <t-empty v-else-if="!plans.length" description="暂无巡检计划；可通过 API 创建计划和定时策略" />
        <div v-for="plan in plans" v-else :key="plan.id" class="business-list-row">
          <div>
            <strong>{{ plan.name }}</strong
            ><small>{{ plan.code }} · {{ plan.points.length }} 个固定点位</small>
          </div>
          <t-tag :theme="plan.enabled ? 'success' : 'default'">{{
            plan.enabled ? '已启用' : '已停用'
          }}</t-tag>
        </div>
      </article>
      <article class="panel content-card">
        <div class="panel-heading">
          <h3>巡检报告</h3>
          <span>Web / PDF / Excel</span>
        </div>
        <t-empty v-if="!reports.length" description="巡检任务完成后可生成报告" />
        <div v-for="report in reports" v-else :key="report.id" class="business-list-row">
          <div>
            <strong>{{ report.report_code }}</strong
            ><small>{{ report.status }} · {{ report.generated_at || '等待生成' }}</small>
          </div>
          <div class="inline-actions" v-if="report.status === 'READY'">
            <a :href="`/api/v1/patrol-reports/${report.id}/download/pdf`">PDF</a>
            <a :href="`/api/v1/patrol-reports/${report.id}/download/xlsx`">Excel</a>
          </div>
        </div>
      </article>
    </section>
    <section class="panel content-card preset-section">
      <div class="panel-heading">
        <div>
          <h3>预设固定位置</h3>
          <span>到位成功需由新鲜定位、定位质量和连续三帧误差共同确认</span>
        </div>
      </div>
      <t-empty v-if="!presets.length" description="暂无预设位置" />
      <div v-else class="preset-grid">
        <div v-for="preset in presets" :key="preset.id" class="business-list-row preset-row">
          <div>
            <strong>{{ preset.name }}</strong>
            <small>{{ preset.code }} · {{ preset.category }}</small>
          </div>
          <t-button
            size="small"
            :disabled="!preset.enabled || preset.requires_reverse || !robotId"
            :loading="navigatingId === preset.id"
            @click="navigate(preset)"
          >
            {{ preset.requires_reverse ? '超出倒车精度边界' : '前往此位置' }}
          </t-button>
        </div>
      </div>
    </section>
    <t-alert
      theme="info"
      title="安全调度规则"
      message="服务器恢复后默认不补跑过期运动任务；机器人忙时跳过并记录原因；未验证 command/ACK 与地图合同的真车保持只读。"
    />
  </div>
</template>
