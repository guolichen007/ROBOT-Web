<script setup lang="ts">
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { init, use, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import { api } from '@/lib/api'

const telemetry = ref<any[]>([]),
  tab = ref<'telemetry' | 'commands' | 'tasks'>('telemetry'),
  rows = ref<any[]>([])
const chartEl = ref<HTMLDivElement>()
use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])
let chart: ECharts | null = null
const resizeChart = (): void => chart?.resize()
async function changeTab(name: typeof tab.value): Promise<void> {
  tab.value = name
  if (name === 'telemetry') rows.value = telemetry.value
  else rows.value = (await api.get(`/history/${name}`, { params: { limit: 300 } })).data
}
onMounted(async () => {
  telemetry.value = (await api.get('/history/telemetry', { params: { robot_id: 'R001', limit: 600 } })).data
  rows.value = telemetry.value
  await nextTick()
  chart = init(chartEl.value!)
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 42, right: 18, top: 28, bottom: 32 },
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#8ca5b4' } },
    xAxis: {
      type: 'category',
      data: telemetry.value.map((item) => new Date(item.server_received_at).toLocaleTimeString()),
      axisLabel: { color: '#698492' },
      axisLine: { lineStyle: { color: '#23404d' } },
    },
    yAxis: { type: 'value', axisLabel: { color: '#698492' }, splitLine: { lineStyle: { color: '#17313d' } } },
    series: [
      {
        name: 'X / m',
        type: 'line',
        showSymbol: false,
        smooth: true,
        data: telemetry.value.map((item) => item.x),
        lineStyle: { color: '#27d4b1' },
        areaStyle: { color: 'rgba(39,212,177,.08)' },
      },
      {
        name: 'Y / m',
        type: 'line',
        showSymbol: false,
        smooth: true,
        data: telemetry.value.map((item) => item.y),
        lineStyle: { color: '#e2a84a' },
      },
    ],
  })
  window.addEventListener('resize', resizeChart)
})
onUnmounted(() => {
  window.removeEventListener('resize', resizeChart)
  chart?.dispose()
})
</script>

<template>
  <PageHeader
    eyebrow="TIME SERIES"
    title="历史与回放"
    description="遥测默认 1 Hz 降采样，同时保存源时间与服务端接收时间。"
  />
  <section class="panel history-chart"><div ref="chartEl"></div></section>
  <section class="panel data-panel">
    <div class="tabs">
      <button
        v-for="name in ['telemetry', 'commands', 'tasks']"
        :key="name"
        :class="{ active: tab === name }"
        @click="changeTab(name as any)"
      >
        {{ name }}
      </button>
    </div>
    <DataTable
      :rows="rows"
      :columns="
        tab === 'telemetry'
          ? [
              { key: 'server_received_at', label: '接收时间' },
              { key: 'x', label: 'X' },
              { key: 'y', label: 'Y' },
              { key: 'theta', label: '航向' },
              { key: 'battery', label: '电量' },
              { key: 'map_version', label: '地图版本' },
            ]
          : tab === 'commands'
            ? [
                { key: 'command_id', label: '命令 ID' },
                { key: 'cmd', label: '命令' },
                { key: 'lifecycle_status', label: '状态' },
                { key: 'ack_status', label: 'ACK' },
                { key: 'issued_at', label: '发出时间' },
              ]
            : [
                { key: 'task_code', label: '任务编号' },
                { key: 'type', label: '类型' },
                { key: 'status', label: '状态' },
                { key: 'progress', label: '进度' },
                { key: 'created_at', label: '创建时间' },
              ]
      "
    />
  </section>
</template>
