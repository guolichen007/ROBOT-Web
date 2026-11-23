<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NotificationIcon } from 'tdesign-icons-vue-next'
import { api, errorMessage } from '@/lib/api'
import {
  alarmLocation,
  detectionDataLabel,
  detectionDataState,
  isActiveAlarm,
  isUnacknowledged,
  popupQueue,
} from '@/lib/alarms'
import { alarmTypeLabel, severityLabel } from '@/lib/ui-labels'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'

// 全局火警通知入口 + 弹窗。数据来自 monitor 快照与 WS 增量（alarm.created / alarm.updated），
// 断网重连后由快照重新拉取，未确认火警会再次弹出。
const auth = useAuthStore()
const monitor = useMonitorStore()
const router = useRouter()
const dismissed = ref(new Set<string>())
const busyId = ref('')
const message = ref('')

const alarms = computed(() => monitor.snapshot.alarms || [])
const active = computed(() => alarms.value.filter(isActiveAlarm))
const unacked = computed(() => alarms.value.filter(isUnacknowledged))
// 监控首页已有本车火警横幅 + 主火警面板（含“确认收到”）：当前车辆的火警不再重复弹窗遮挡控制区；
// 站点级 / 其它车辆火警仍弹出，避免漏报。
const route = useRoute()
const current = computed(
  () =>
    popupQueue(alarms.value, dismissed.value).filter(
      (alarm) => !(route.path === '/monitor' && monitor.robot?.id && alarm.robot_id === monitor.robot.id),
    )[0],
)
// 弹窗出现时给页面底部留白，避免固定弹窗永久遮挡表格最后几行 / 操作按钮。
watch(
  () => Boolean(auth.can('alarm.read') && current.value),
  (visible) => document.body.classList.toggle('has-alarm-popup', visible),
  { immediate: true },
)
onUnmounted(() => document.body.classList.remove('has-alarm-popup'))
const dataState = computed(() => detectionDataState(monitor.snapshot.robots || [], active.value.length))

function closeHint(): void {
  const alarm = current.value
  if (!alarm) return
  // 仅关闭本机提示；同一事件再次上报（occurrence_count 变化）会重新提示。
  dismissed.value = new Set([...dismissed.value, `${alarm.id}:${alarm.occurrence_count}`])
}

async function acknowledge(): Promise<void> {
  const alarm = current.value
  if (!alarm || busyId.value) return
  busyId.value = alarm.id
  message.value = ''
  try {
    const { data } = await api.post(`/alarms/${alarm.id}/acknowledge`, {})
    const index = monitor.snapshot.alarms.findIndex((item) => item.id === alarm.id)
    if (index >= 0) monitor.snapshot.alarms.splice(index, 1, { ...monitor.snapshot.alarms[index], ...data })
  } catch (reason) {
    message.value = errorMessage(reason)
  } finally {
    busyId.value = ''
  }
}

async function view(): Promise<void> {
  const alarm = current.value
  closeHint()
  await router.push({ path: '/alarms', query: alarm ? { id: alarm.id } : {} })
}
</script>

<template>
  <button
    v-if="auth.can('alarm.read')"
    class="alarm-bell"
    type="button"
    data-testid="alarm-bell"
    :data-active="active.length > 0"
    :data-detection="dataState"
    :title="
      active.length
        ? `${active.length} 条活动火警，${unacked.length} 条未确认`
        : detectionDataLabel(dataState, 0)
    "
    @click="router.push('/alarms')"
  >
    <NotificationIcon />
    <span>{{ active.length ? '火警' : detectionDataLabel(dataState, 0) }}</span>
    <b v-if="active.length">{{ active.length }}</b>
  </button>
  <section
    v-if="auth.can('alarm.read') && current"
    class="alarm-popup"
    role="alertdialog"
    aria-live="assertive"
    data-testid="alarm-popup"
    :data-alarm-id="current.id"
    :data-event-code="current.event_code"
  >
    <h3>火警：{{ alarmTypeLabel(current.fire_type) }} · {{ severityLabel(current.severity) }}</h3>
    <p>
      {{ current.event_code }}
      <span v-if="current.robot_vehicle_id"> · {{ current.robot_vehicle_id }}</span>
    </p>
    <p :class="{ unmatched: current.location_state === 'UNMATCHED' }">位置：{{ alarmLocation(current) }}</p>
    <p>
      最后出现：{{ new Date(current.last_seen_at).toLocaleString('zh-CN') }} · 合并
      {{ current.occurrence_count }} 次
    </p>
    <p v-if="message" class="inline-notice" data-theme="error">{{ message }}</p>
    <div class="popup-actions">
      <button type="button" class="secondary-button compact" data-testid="alarm-popup-view" @click="view">
        查看详情
      </button>
      <button
        v-if="auth.can('alarm.ack')"
        type="button"
        class="primary-button compact"
        data-testid="alarm-popup-ack"
        :disabled="busyId === current.id"
        @click="acknowledge"
      >
        确认收到
      </button>
      <button type="button" class="text-button" data-testid="alarm-popup-close" @click="closeHint">
        关闭提示
      </button>
    </div>
    <small class="form-hint">“关闭提示”只隐藏本机弹窗，不改变火警状态；“确认收到”会记录处置人和时间。</small>
  </section>
</template>
