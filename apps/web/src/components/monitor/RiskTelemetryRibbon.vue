<script setup lang="ts">
import type { DataSupportState, RobotState } from '@/types'
const props = defineProps<{ robot?: RobotState; freshness: string }>()
function state(channel: string): DataSupportState {
  return props.robot?.data_channels?.[channel]?.support_state || 'NOT_CONNECTED'
}
function stateLabel(channel: string): string {
  return (
    {
      CONNECTED: '正常',
      STALE: '数据陈旧',
      NOT_CONNECTED: '未接入',
      UNSUPPORTED: '当前车型不支持',
      ERROR: '异常',
    }[state(channel)] || '未接入'
  )
}
function value(value: number | null | undefined, channel: string, unit: string): string {
  return state(channel) === 'CONNECTED' && value != null
    ? `${value.toFixed(channel === 'smoke' ? 2 : 1)} ${unit}`
    : '--'
}
const items = () => [
  {
    label: '顶部热像',
    value: value(props.robot?.top_ir, 'top_ir', '℃'),
    state: state('top_ir'),
    hint: stateLabel('top_ir'),
  },
  {
    label: '底部红外',
    value: value(props.robot?.bottom_ir, 'bottom_ir', '℃'),
    state: state('bottom_ir'),
    hint: stateLabel('bottom_ir'),
  },
  {
    label: '烟雾浓度',
    value: value(props.robot?.smoke, 'smoke', '%'),
    state: state('smoke'),
    hint: stateLabel('smoke'),
  },
  {
    label: '定位质量',
    value: props.robot?.localization_status || '未知',
    state: props.robot?.x == null ? 'NOT_CONNECTED' : 'CONNECTED',
    hint: props.robot?.x == null ? '未接入' : '正常',
  },
]
</script>
<template>
  <section class="panel risk-ribbon">
    <div class="risk-ribbon-head">
      <span>环境与设备状态</span>
      <a :title="`数据新鲜度：${freshness}`">数据 {{ freshness }}</a>
    </div>
    <div class="risk-ribbon-grid">
      <article v-for="item in items()" :key="item.label" :data-state="item.state">
        <span>{{ item.label }}</span>
        <strong>{{ item.value }}</strong>
        <small>{{ item.hint }}</small>
      </article>
    </div>
  </section>
</template>
