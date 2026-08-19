<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { FullscreenIcon, ZoomInIcon, ZoomOutIcon } from 'tdesign-icons-vue-next'
import { MapAdapter } from '@/lib/map-adapter'
import type { Alarm, DetectionCoverage, MapPoint, MapVersion, ParkingSlot, RobotState } from '@/types'
import fireSlotBadgeUrl from '@/assets/yd/map/fire_slot_badge_v4.svg'
import firePinUrl from '@/assets/yd/map/fire_pin_v4_64.png'
import robotTopUrl from '@/assets/yd/map/robot_topdown_v4.png'

const props = withDefaults(
  defineProps<{
    mapVersion: MapVersion | null
    slots: ParkingSlot[]
    inspectionPoints: MapPoint[]
    extinguishPoints: MapPoint[]
    trajectory: Array<{ x: number; y: number }>
    robot?: RobotState
    alarms: Alarm[]
    coverage?: DetectionCoverage | null
    selectedSlotId?: string
    targetSlotId?: string
    showSemanticPoints?: boolean
    showRoute?: boolean
    showCoverage?: boolean
  }>(),
  { showSemanticPoints: false, showRoute: true, showCoverage: true },
)
const emit = defineEmits<{ slotClick: [slot: ParkingSlot] }>()
const frame = ref<HTMLDivElement>()
const size = ref({ width: 900, height: 560 })
const revision = ref(0)
let observer: ResizeObserver
let drag: { x: number; y: number } | null = null
const geometry = computed(
  () => props.mapVersion || { width_m: 48, height_m: 34, origin_x: 0, origin_y: 0, rotation_rad: 0 },
)
const adapter = new MapAdapter(geometry.value, size.value)
const invalidate = () => (revision.value += 1)
const point = (x: number, y: number) => {
  void revision.value
  return adapter.worldToScreen({ x, y })
}
const polygonPoints = (points: Array<{ x: number; y: number }>) =>
  points
    .map((item) => {
      const value = point(item.x, item.y)
      return `${value.x},${value.y}`
    })
    .join(' ')
const slotPoints = (slot: ParkingSlot) =>
  polygonPoints(Array.isArray(slot.polygon_json) ? slot.polygon_json : slot.polygon_json.points)
const pathPoints = computed(() => polygonPoints(props.trajectory))
const alarmSlots = computed(() => new Map(props.alarms.map((item) => [item.parking_slot_id, item])))

// ---- right-side detection sector (server-authoritative polygon) ----
const coveragePoints = computed(() => polygonPoints(props.coverage?.polygon || []))
const coverageStale = computed(() => props.coverage?.state === 'STALE')
const coverageInvalid = computed(
  () => props.coverage?.state === 'ERROR' && props.coverage?.reason === 'RIGHT_SENSOR_ORIENTATION_INVALID',
)

// ---- fire markers: slot badges vs free pins ----
const fireMarkers = computed(() =>
  props.alarms
    .map((alarm) => {
      const src = alarm.source_position_json as { x?: number; y?: number } | undefined
      let pos: { x: number; y: number } | null = null
      if (src && typeof src.x === 'number' && typeof src.y === 'number') pos = { x: src.x, y: src.y }
      else if (alarm.parking_slot_id) {
        const slot = props.slots.find((item) => item.id === alarm.parking_slot_id)
        if (slot) pos = slot.center_pose_json
      }
      return { alarm, pos }
    })
    .filter((item): item is { alarm: Alarm; pos: { x: number; y: number } } => Boolean(item.pos)),
)
const hasActiveFire = computed(() => fireMarkers.value.length > 0)

const slotScreenBounds = (slot: ParkingSlot) => {
  const points = Array.isArray(slot.polygon_json) ? slot.polygon_json : slot.polygon_json.points
  const screens = points.map((item) => point(item.x, item.y))
  const xs = screens.map((item) => item.x)
  const ys = screens.map((item) => item.y)
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) }
}
const slotFireBadges = computed(() =>
  fireMarkers.value
    .map((marker) => {
      const slot = props.slots.find((item) => item.id === marker.alarm.parking_slot_id)
      if (!slot) return null
      const b = slotScreenBounds(slot)
      const badge = 24
      let x = b.maxX + 8
      const y = b.minY - 6
      let flip = false
      if (x + badge > size.value.width - 12) {
        x = b.minX - badge - 8
        flip = true
      }
      const anchor = { x: flip ? b.minX : b.maxX, y: b.minY + 2 }
      return { alarm: marker.alarm, slot, x, y, anchor, flip, badge }
    })
    .filter((item): item is NonNullable<typeof item> => Boolean(item)),
)
const freeFirePins = computed(() =>
  fireMarkers.value.filter((marker) => !props.slots.some((slot) => slot.id === marker.alarm.parking_slot_id)),
)

function updateSize(): void {
  if (!frame.value) return
  size.value = { width: frame.value.clientWidth, height: frame.value.clientHeight }
  adapter.setViewport(size.value)
  invalidate()
}
function zoom(delta: number): void {
  adapter.setZoom(adapter.getZoom() + delta)
  invalidate()
}
function fit(): void {
  adapter.reset()
  invalidate()
}
function pointerDown(event: PointerEvent): void {
  if (event.target instanceof Element && event.target.closest('button, [role="button"]')) return
  drag = { x: event.clientX, y: event.clientY }
  ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
}
function pointerMove(event: PointerEvent): void {
  if (!drag) return
  adapter.panBy(event.clientX - drag.x, event.clientY - drag.y)
  drag = { x: event.clientX, y: event.clientY }
  invalidate()
}
function pointerUp(): void {
  drag = null
}
watch(
  () => props.mapVersion?.id,
  () => {
    adapter.setMap(geometry.value)
    adapter.reset()
    void nextTick(updateSize)
  },
)
onMounted(() => {
  updateSize()
  observer = new ResizeObserver(updateSize)
  if (frame.value) observer.observe(frame.value)
})
onUnmounted(() => observer?.disconnect())
</script>

<template>
  <div
    ref="frame"
    class="map-canvas operations-map"
    @pointerdown="pointerDown"
    @pointermove="pointerMove"
    @pointerup="pointerUp"
    @pointercancel="pointerUp"
  >
    <svg :viewBox="`0 0 ${size.width} ${size.height}`" role="img" aria-label="停车场二维态势地图">
      <defs>
        <pattern
          id="yd-right-dots-blue"
          width="10"
          height="10"
          patternUnits="userSpaceOnUse"
        >
          <circle cx="2" cy="2" r="1.1" fill="#4f8cff" />
        </pattern>
        <pattern id="yd-right-dots-red" width="10" height="10" patternUnits="userSpaceOnUse">
          <circle cx="2" cy="2" r="1.1" fill="#ef5757" />
        </pattern>
        <marker
          id="yd-route-arrow"
          viewBox="0 0 10 10"
          refX="5"
          refY="5"
          markerWidth="7"
          markerHeight="7"
          orient="auto"
        >
          <path d="M0,0 L10,5 L0,10 z" fill="#2f7cff" />
        </marker>
      </defs>

      <rect width="100%" height="100%" class="map-floor" />

      <!-- right-side detection sector: server polygon, drawn under route and slots -->
      <template v-if="showCoverage && coveragePoints && !coverageInvalid">
        <polygon
          :points="coveragePoints"
          class="coverage-soft-fill"
          :class="{ danger: hasActiveFire, stale: coverageStale }"
        />
        <polygon
          :points="coveragePoints"
          class="coverage-dot-pattern"
          :class="{ danger: hasActiveFire }"
        />
        <polygon
          :points="coveragePoints"
          class="coverage-outline"
          :class="{ danger: hasActiveFire }"
        />
      </template>

      <polyline
        v-if="showRoute && trajectory.length"
        :points="pathPoints"
        class="trajectory-line planned"
        marker-end="url(#yd-route-arrow)"
      />

      <g
        v-for="slot in slots"
        :key="slot.id"
        class="slot-group"
        role="button"
        tabindex="0"
        :aria-label="`车位 ${slot.code}`"
        @click.stop="emit('slotClick', slot)"
        @keydown.enter.prevent="emit('slotClick', slot)"
      >
        <polygon
          :points="slotPoints(slot)"
          :class="[
            'parking-slot',
            {
              disabled: !slot.enabled,
              alarm: alarmSlots.has(slot.id),
              critical: alarmSlots.get(slot.id)?.severity === 'CRITICAL',
              selected: selectedSlotId === slot.id,
              target: targetSlotId === slot.id,
            },
          ]"
        />
        <text
          :x="point(slot.center_pose_json.x, slot.center_pose_json.y).x"
          :y="point(slot.center_pose_json.x, slot.center_pose_json.y).y + 4"
        >
          {{ slot.code }}
        </text>
      </g>

      <template v-if="showSemanticPoints">
        <circle
          v-for="item in inspectionPoints"
          :key="item.id"
          :cx="point(item.pose_json.x, item.pose_json.y).x"
          :cy="point(item.pose_json.x, item.pose_json.y).y"
          r="2.5"
          class="inspection-point"
        />
        <path
          v-for="item in extinguishPoints"
          :key="item.id"
          :transform="`translate(${point(item.pose_json.x, item.pose_json.y).x} ${point(item.pose_json.x, item.pose_json.y).y})`"
          d="M0,-4 L4,3 L-4,3 Z"
          class="extinguish-point"
        />
      </template>

      <!-- slot fire badge + leader line -->
      <g v-for="badge in slotFireBadges" :key="badge.alarm.id" class="slot-fire-badge">
        <line
          :x1="badge.anchor.x"
          :y1="badge.anchor.y"
          :x2="badge.flip ? badge.x + badge.badge : badge.x"
          :y2="badge.y + badge.badge / 2"
        />
        <image :href="fireSlotBadgeUrl" :x="badge.x" :y="badge.y" :width="badge.badge" :height="badge.badge" />
      </g>

      <!-- free-position fire pins (no explicit slot) -->
      <g
        v-for="marker in freeFirePins"
        :key="marker.alarm.id"
        :transform="`translate(${point(marker.pos.x, marker.pos.y).x} ${point(marker.pos.x, marker.pos.y).y})`"
        class="fire-pin"
      >
        <circle r="16" class="fire-pin-ring" />
        <image :href="firePinUrl" :x="-14" :y="-14" width="28" height="28" />
      </g>

      <g
        v-if="robot?.x != null && robot?.y != null"
        :transform="`translate(${point(robot.x, robot.y).x} ${point(robot.x, robot.y).y}) rotate(${(-(robot.theta || 0) * 180) / Math.PI})`"
        class="robot-marker"
      >
        <image :href="robotTopUrl" x="-14" y="-21" width="28" height="42" />
        <rect class="robot-label-bg" x="-21" y="22" width="42" height="17" rx="8.5" />
        <text x="0" y="34">{{ robot.vehicle_id }}</text>
      </g>
    </svg>

    <div class="map-tools">
      <button aria-label="放大" @click.stop="zoom(0.2)"><ZoomInIcon /></button>
      <button aria-label="缩小" @click.stop="zoom(-0.2)"><ZoomOutIcon /></button>
      <button aria-label="适配地图" @click.stop="fit"><FullscreenIcon /></button>
    </div>
    <div class="map-legend">
      <span><i class="legend-dash"></i>巡检路线</span>
      <span><i class="legend-robot"></i>机器人位置</span>
      <span><i class="legend-sector"></i>右侧检测范围</span>
      <span v-if="hasActiveFire"><i class="legend-fire"></i>火情位置</span>
    </div>
    <div v-if="coverageInvalid" class="map-warning">右侧检测配置异常</div>
    <div v-if="!mapVersion" class="map-empty">
      <strong>暂无有效地图</strong><span>未绘制任何演示道路或停车场几何</span>
    </div>
  </div>
</template>
