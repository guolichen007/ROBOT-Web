<script setup lang="ts">
import type { DataSupportState, RobotState, StreamInfo } from '@/types'
const props = defineProps<{ robot?: RobotState; freshness: string; stream?: StreamInfo }>()
function state(channel: string): DataSupportState {
  return props.robot?.data_channels?.[channel]?.support_state || 'NOT_CONNECTED'
}
function value(value: number | null | undefined, channel: string, unit: string): string {
  return state(channel) === 'CONNECTED' && value != null
    ? `${value.toFixed(channel === 'smoke' ? 2 : 1)} ${unit}`
    : state(channel) === 'UNSUPPORTED'
      ? '当前车型不支持'
      : '未接入'
}
const items = () => [
  { label: '烟雾浓度', value: value(props.robot?.smoke, 'smoke', '%'), state: state('smoke') },
  { label: '顶部热像', value: value(props.robot?.top_ir, 'top_ir', '℃'), state: state('top_ir') },
  { label: '底部红外', value: value(props.robot?.bottom_ir, 'bottom_ir', '℃'), state: state('bottom_ir') },
  {
    label: '定位质量',
    value: props.robot?.localization_status || '未知',
    state: props.robot?.x == null ? 'NOT_CONNECTED' : 'CONNECTED',
  },
  {
    label: '数据新鲜度',
    value: props.freshness,
    state: props.robot?.server_received_at ? 'CONNECTED' : 'NOT_CONNECTED',
  },
  {
    label: '车顶视频',
    value: props.stream?.state || 'OFFLINE',
    state: props.stream?.state === 'LIVE' ? 'CONNECTED' : 'NOT_CONNECTED',
  },
]
</script>
<template>
  <section class="risk-ribbon">
    <article v-for="item in items()" :key="item.label" :data-state="item.state">
      <span>{{ item.label }}</span
      ><strong>{{ item.value }}</strong
      ><small>{{ item.state }}</small>
    </article>
  </section>
</template>
