<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { LocationIcon, ZoomInIcon, ZoomOutIcon } from 'tdesign-icons-vue-next'
import { MapAdapter } from '@/lib/map-adapter'
import type { Alarm, DetectionCoverage, MapPoint, MapVersion, ParkingSlot, RobotState } from '@/types'

const props = defineProps<{
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
}>()
const emit = defineEmits<{ slotClick: [slot: ParkingSlot] }>()
const frame = ref<HTMLDivElement>()
const size = ref({ width: 900, height: 560 })
const revision = ref(0)
const followRobot = ref(false)
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
const coveragePoints = computed(() => polygonPoints(props.coverage?.polygon || []))
const alarmSlots = computed(() => new Map(props.alarms.map((item) => [item.parking_slot_id, item])))

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
  followRobot.value = false
  invalidate()
}
function toggleFollow(): void {
  followRobot.value = !followRobot.value
  if (followRobot.value && props.robot?.x != null && props.robot?.y != null) {
    adapter.centerOn({ x: props.robot.x, y: props.robot.y })
    invalidate()
  }
}
function pointerDown(event: PointerEvent): void {
  // Interactive map objects own their pointer sequence. Capturing it on the
  // viewport would retarget the subsequent click and silently drop slot/tool
  // actions in a real Chromium pointer sequence.
  if (event.target instanceof Element && event.target.closest('button, [role="button"]')) return
  drag = { x: event.clientX, y: event.clientY }
  ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
}
function pointerMove(event: PointerEvent): void {
  if (!drag) return
  adapter.panBy(event.clientX - drag.x, event.clientY - drag.y)
  drag = { x: event.clientX, y: event.clientY }
  followRobot.value = false
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
watch(
  () => [props.robot?.x, props.robot?.y],
  () => {
    if (followRobot.value && props.robot?.x != null && props.robot?.y != null) {
      adapter.centerOn({ x: props.robot.x, y: props.robot.y })
      invalidate()
    }
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
      <rect width="100%" height="100%" class="map-floor" />
      <polyline v-if="trajectory.length" :points="pathPoints" class="trajectory-line planned" />
      <polygon v-if="coveragePoints" :points="coveragePoints" class="detection-sector" />
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
      <g
        v-if="robot?.x != null && robot?.y != null"
        :transform="`translate(${point(robot.x, robot.y).x} ${point(robot.x, robot.y).y}) rotate(${(-(robot.theta || 0) * 180) / Math.PI})`"
        class="robot-marker"
      >
        <rect x="-8" y="-12" width="16" height="24" rx="4" />
        <path d="M0 -18 L6 -8 L0 -10 L-6 -8 Z" />
        <text x="15" y="4">{{ robot.vehicle_id }}</text>
      </g>
    </svg>
    <div class="map-tools">
      <button aria-label="放大" @click.stop="zoom(0.2)"><ZoomInIcon /></button>
      <button aria-label="缩小" @click.stop="zoom(-0.2)"><ZoomOutIcon /></button>
      <button aria-label="适配地图" @click.stop="fit">Fit</button>
      <button :class="{ active: followRobot }" aria-label="跟随车辆" @click.stop="toggleFollow">
        <LocationIcon />
      </button>
    </div>
    <div v-if="!mapVersion" class="map-empty">
      <strong>暂无有效地图</strong><span>未绘制任何演示道路或停车场几何</span>
    </div>
  </div>
</template>
