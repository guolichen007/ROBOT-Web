<script setup lang="ts">
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { init, use, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import DataTable from '@/components/DataTable.vue'
import PageHeader from '@/components/PageHeader.vue'
import { api } from '@/lib/api'
import { useMonitorStore } from '@/stores/monitor'

const monitor = useMonitorStore()
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
  if (!monitor.robot) await monitor.loadSnapshot()
  const robotId = monitor.robot?.vehicle_id
  telemetry.value = robotId
    ? (await api.get('/history/telemetry', { params: { robot_id: robotId, limit: 600 } })).data
    : []
  rows.value = telemetry.value
  await nextTick()
  chart = init(chartEl.value!)
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 42, right: 18, top: 28, bottom: 32 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#fff',
      borderColor: '#dee6f2',
      textStyle: { color: '#1f3254' },
    },
    legend: { textStyle: { color: '#5f6f89' } },
    xAxis: {
      type: 'category',
      data: telemetry.value.map((item) => new Date(item.server_received_at).toLocaleTimeString()),
      axisLabel: { color: '#5f6f89' },
      axisLine: { lineStyle: { color: '#cbd6e4' } },
    },
    yAxis: { type: 'value', axisLabel: { color: '#5f6f89' }, splitLine: { lineStyle: { color: '#edf1f6' } } },
    series: [
      {
        name: 'X / m',
        type: 'line',
        showSymbol: false,
        smooth: true,
        data: telemetry.value.map((item) => item.x),
        lineStyle: { color: '#146cff' },
        areaStyle: { color: 'rgba(20,108,255,.10)' },
      },
      {
        name: 'Y / m',
        type: 'line',
        showSymbol: false,
        smooth: true,
        data: telemetry.value.map((item) => item.y),
        lineStyle: { color: '#16a465' },
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
    eyebrow="历史回放"
    title="历史与回放"
    description="遥测默认 1 Hz 降采样，同时保存源时间与服务端接收时间。"
  />
  <section class="panel history-chart"><div ref="chartEl"></div></section>
  <section class="panel data-panel">
    <div class="tabs">
      <button
        v-for="item in [
          ['telemetry', '遥测'],
          ['commands', '命令'],
          ['tasks', '任务'],
        ]"
        :key="item[0]"
        :class="{ active: tab === item[0] }"
        @click="changeTab(item[0] as 'telemetry' | 'commands' | 'tasks')"
      >
        {{ item[1] }}
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
