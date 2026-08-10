<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ManualControl from '@/components/ManualControl.vue'
import MapCanvas from '@/components/MapCanvas.vue'
import StateChip from '@/components/StateChip.vue'
import VideoCard from '@/components/VideoCard.vue'
import { api, errorMessage } from '@/lib/api'
import { newUuid } from '@/lib/id'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import type { Alarm, ParkingSlot } from '@/types'

const auth = useAuthStore()
const monitor = useMonitorStore()
const selectedSlot = ref<ParkingSlot | null>(null)
const selectedAlarm = ref<Alarm | null>(null)
const notice = ref<{ message: string; tone: string } | null>(null)
const working = ref(false)

const trajectory = computed(() => monitor.snapshot.trajectories[0]?.path_json || [])
const streams = computed(() =>
  Object.fromEntries(monitor.snapshot.streams.map((item) => [item.camera_type, item])),
)
const alarms = computed(() => monitor.snapshot.alarms.slice(0, 5))
const tasks = computed(() => monitor.snapshot.tasks.slice(0, 4))
const robot = computed(() => monitor.robot)

function toast(message: string, tone = ''): void {
  notice.value = { message, tone }
  window.setTimeout(() => {
    if (notice.value?.message === message) notice.value = null
  }, 4200)
}

async function createManualAlarm(): Promise<void> {
  if (!selectedSlot.value || !monitor.snapshot.map_version) return
  working.value = true
  try {
    const { data } = await api.post(
      '/alarms/manual',
      {
        parking_slot_id: selectedSlot.value.id,
        fire_type: 'unknown',
        note: `监控地图人工上报：${selectedSlot.value.code}`,
        map_version: monitor.snapshot.map_version.version,
        severity: 'HIGH',
        media: {},
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    selectedAlarm.value = data
    await monitor.loadSnapshot()
    toast(`${selectedSlot.value.code} 人工火情已创建并确认`, 'danger')
  } catch (error) {
    toast(errorMessage(error), 'danger')
  } finally {
    working.value = false
  }
}

async function transition(alarm: Alarm, action: 'acknowledge' | 'confirm' | 'resolve'): Promise<void> {
  try {
    await api.post(`/alarms/${alarm.id}/${action}`)
    await monitor.loadSnapshot()
    toast(`火情状态已更新：${action}`)
  } catch (error) {
    toast(errorMessage(error), 'danger')
  }
}

async function dispatch(alarm: Alarm): Promise<void> {
  if (!robot.value) return
  working.value = true
  try {
    await api.post(
      `/alarms/${alarm.id}/create-task`,
      {
        robot_id: robot.value.vehicle_id,
        trajectory_id: monitor.snapshot.trajectories[0]?.id,
        parameters: { source: 'MONITOR' },
      },
      { headers: { 'Idempotency-Key': newUuid() } },
    )
    await monitor.loadSnapshot()
    toast('灭火任务已创建并进入可靠派发队列', 'ok')
  } catch (error) {
    toast(errorMessage(error), 'danger')
  } finally {
    working.value = false
  }
}

onMounted(() => {
  if (!monitor.snapshot.map_version) void monitor.start()
})
</script>

<template>
  <div class="monitor-page">
    <div class="page-title-row">
      <div>
        <span class="eyebrow">LIVE OPERATIONS</span>
        <h1>态势监控</h1>
      </div>
      <div class="context-meta">
        <span>{{ monitor.snapshot.site?.name || '站点加载中' }}</span>
        <strong
          >{{ monitor.snapshot.map?.code || '--' }} / V{{
            monitor.snapshot.map_version?.version || '--'
          }}</strong
        >
        <StateChip :value="monitor.snapshot.map_version?.status || 'UNKNOWN'" />
      </div>
    </div>

    <div v-if="notice" class="toast" :class="notice.tone">{{ notice.message }}</div>

    <section class="kpi-grid">
      <article>
        <span>机器人状态</span
        ><strong :class="`state-${(robot?.online_state || 'offline').toLowerCase()}`">{{
          robot?.online_state || 'OFFLINE'
        }}</strong
        ><small>心跳阈值 3s / 10s</small>
      </article>
      <article>
        <span>剩余电量</span><strong>{{ robot?.battery?.toFixed?.(1) ?? '--' }}<em>%</em></strong>
        <div class="battery-bar"><i :style="{ width: `${robot?.battery || 0}%` }"></i></div>
      </article>
      <article>
        <span>运行模式</span><strong>{{ robot?.mode || 'UNKNOWN' }}</strong
        ><small>{{ robot?.estop_active ? '软件急停锁存' : '安全策略正常' }}</small>
      </article>
      <article>
        <span>当前任务</span><strong>{{ monitor.activeTask?.type || '空闲' }}</strong
        ><small>{{ monitor.activeTask?.phase || '等待调度' }}</small>
      </article>
      <article class="sensor-kpi">
        <span>烟雾 / 红外</span><strong>{{ robot?.smoke?.toFixed?.(3) ?? '0.000' }}</strong
        ><small
          >{{ robot?.bottom_ir?.toFixed?.(1) ?? '--' }}° / {{ robot?.top_ir?.toFixed?.(1) ?? '--' }}°</small
        >
      </article>
    </section>

    <div class="monitor-grid">
      <section class="map-panel panel">
        <div class="panel-heading">
          <div>
            <span class="eyebrow">MAP / FRAME: MAP</span>
            <h3>二维地图</h3>
          </div>
          <div class="map-state">
            <span>{{ robot?.x?.toFixed?.(2) ?? '--' }}, {{ robot?.y?.toFixed?.(2) ?? '--' }} m</span
            ><span>θ {{ robot?.theta?.toFixed?.(2) ?? '--' }} rad</span>
          </div>
        </div>
        <MapCanvas
          :map-version="monitor.snapshot.map_version"
          :slots="monitor.snapshot.parking_slots"
          :inspection-points="monitor.snapshot.inspection_points"
          :extinguish-points="monitor.snapshot.extinguish_points"
          :trajectory="trajectory"
          :robot="robot"
          :alarms="monitor.snapshot.alarms"
          :selected-slot-id="selectedSlot?.id"
          @slot-click="selectedSlot = $event"
        />
        <div v-if="selectedSlot" class="map-selection">
          <div>
            <span>已选择车位</span><strong>{{ selectedSlot.code }}</strong>
          </div>
          <button
            v-if="auth.can('alarm.confirm')"
            class="danger-outline"
            :disabled="working"
            @click="createManualAlarm"
          >
            创建人工火情
          </button>
          <button class="text-button" @click="selectedSlot = null">取消</button>
        </div>
      </section>

      <aside class="right-stack">
        <ManualControl :robot="robot" @notice="toast" />
        <section class="panel alarms-panel">
          <div class="panel-heading">
            <div>
              <span class="eyebrow">ALARM QUEUE</span>
              <h3>活动火情</h3>
            </div>
            <RouterLink to="/alarms">全部</RouterLink>
          </div>
          <div v-if="!alarms.length" class="empty-state"><span>✓</span><strong>当前无活动火情</strong></div>
          <button v-for="alarm in alarms" :key="alarm.id" class="alarm-row" @click="selectedAlarm = alarm">
            <span class="severity" :data-level="alarm.severity"></span>
            <span
              ><strong>{{ alarm.event_code }}</strong
              ><small>{{ alarm.fire_type }} · {{ alarm.detection_method }}</small></span
            >
            <StateChip :value="alarm.state" />
          </button>
        </section>
      </aside>
    </div>

    <section class="operations-row">
      <div class="video-grid">
        <VideoCard :stream="streams.roof_rgb" title="顶部 RGB" glyph="RGB" />
        <VideoCard :stream="streams.roof_thermal" title="顶部热像" glyph="THM" />
        <VideoCard :stream="streams.bottom_ir" title="底部红外" glyph="IR" />
      </div>
      <section class="panel task-panel">
        <div class="panel-heading">
          <div>
            <span class="eyebrow">COMMAND / TASK</span>
            <h3>执行状态</h3>
          </div>
          <RouterLink to="/tasks">任务中心</RouterLink>
        </div>
        <div v-if="!tasks.length" class="empty-state"><strong>无活动任务</strong></div>
        <div v-for="task in tasks" :key="task.id" class="task-row">
          <div>
            <strong>{{ task.task_code }}</strong
            ><small>{{ task.type }}</small>
          </div>
          <div class="task-progress"><i :style="{ width: `${task.progress}%` }"></i></div>
          <div>
            <StateChip :value="task.status" /><small>{{ task.phase }}</small>
          </div>
        </div>
      </section>
    </section>

    <div v-if="selectedAlarm" class="modal-shade" @click.self="selectedAlarm = null">
      <section class="modal-card">
        <span class="eyebrow">FIRE EVENT</span>
        <h2>{{ selectedAlarm.event_code }}</h2>
        <dl>
          <div>
            <dt>状态</dt>
            <dd><StateChip :value="selectedAlarm.state" /></dd>
          </div>
          <div>
            <dt>严重度</dt>
            <dd>{{ selectedAlarm.severity }}</dd>
          </div>
          <div>
            <dt>重复次数</dt>
            <dd>{{ selectedAlarm.occurrence_count }}</dd>
          </div>
        </dl>
        <div class="modal-actions">
          <button
            v-if="selectedAlarm.state === 'NEW' && auth.can('alarm.ack')"
            class="secondary-button"
            @click="transition(selectedAlarm, 'acknowledge')"
          >
            确认收到
          </button>
          <button
            v-if="auth.can('extinguish.create')"
            class="primary-button"
            :disabled="working"
            @click="dispatch(selectedAlarm)"
          >
            创建灭火任务
          </button>
          <button
            v-if="auth.can('alarm.resolve')"
            class="secondary-button"
            @click="transition(selectedAlarm, 'resolve')"
          >
            标记解决
          </button>
        </div>
        <button class="modal-close" aria-label="关闭" @click="selectedAlarm = null">×</button>
      </section>
    </div>
  </div>
</template>
