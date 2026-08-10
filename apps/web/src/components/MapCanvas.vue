<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { MapAdapter } from '@/lib/map-adapter'
import type { Alarm, MapPoint, MapVersion, ParkingSlot, RobotState } from '@/types'

const props = defineProps<{
  mapVersion: MapVersion | null
  slots: ParkingSlot[]
  inspectionPoints: MapPoint[]
  extinguishPoints: MapPoint[]
  trajectory: Array<{ x: number; y: number }>
  robot?: RobotState
  alarms: Alarm[]
  selectedSlotId?: string
}>()
const emit = defineEmits<{ slotClick: [slot: ParkingSlot] }>()
const frame = ref<HTMLDivElement>()
const size = ref({ width: 900, height: 560 })
const zoomLevel = ref(1)
let observer: ResizeObserver

const geometry = computed(
  () =>
    props.mapVersion || {
      width_m: 30,
      height_m: 20,
      origin_x: 0,
      origin_y: 0,
      rotation_rad: 0,
    },
)
const adapter = computed(() => {
  const result = new MapAdapter(geometry.value, size.value)
  result.setZoom(zoomLevel.value)
  return result
})
const point = (x: number, y: number) => adapter.value.worldToScreen({ x, y })
const slotPoints = (slot: ParkingSlot) => {
  const source = Array.isArray(slot.polygon_json) ? slot.polygon_json : slot.polygon_json.points
  return source
    .map((item) => {
      const p = point(item.x, item.y)
      return `${p.x},${p.y}`
    })
    .join(' ')
}
const pathPoints = computed(() =>
  props.trajectory
    .map((item) => {
      const p = point(item.x, item.y)
      return `${p.x},${p.y}`
    })
    .join(' '),
)
const alarmSlots = computed(() => new Set(props.alarms.map((item) => item.parking_slot_id)))

function updateSize(): void {
  if (!frame.value) return
  size.value = { width: frame.value.clientWidth, height: frame.value.clientHeight }
}
function zoom(delta: number): void {
  zoomLevel.value = Math.min(3, Math.max(0.7, zoomLevel.value + (delta > 0 ? 0.2 : -0.2)))
}

watch(
  () => props.mapVersion,
  () => void nextTick(updateSize),
)
onMounted(() => {
  updateSize()
  observer = new ResizeObserver(updateSize)
  if (frame.value) observer.observe(frame.value)
})
onUnmounted(() => observer?.disconnect())
</script>

<template>
  <div ref="frame" class="map-canvas">
    <svg :viewBox="`0 0 ${size.width} ${size.height}`" role="img" aria-label="停车场二维态势地图">
      <defs>
        <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
          <path d="M 28 0 L 0 0 0 28" fill="none" stroke="#173342" stroke-width="0.7" />
        </pattern>
        <filter id="robotGlow">
          <feGaussianBlur stdDeviation="4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <rect width="100%" height="100%" fill="url(#grid)" />
      <polyline v-if="pathPoints" :points="pathPoints" class="trajectory-line" />
      <g
        v-for="slot in slots"
        :key="slot.id"
        class="slot-group"
        role="button"
        tabindex="0"
        :aria-label="`车位 ${slot.code}`"
        @click="emit('slotClick', slot)"
        @keydown.enter.prevent="emit('slotClick', slot)"
        @keydown.space.prevent="emit('slotClick', slot)"
      >
        <polygon
          :points="slotPoints(slot)"
          :class="['parking-slot', { alarm: alarmSlots.has(slot.id), selected: selectedSlotId === slot.id }]"
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
        r="3"
        class="inspection-point"
      />
      <path
        v-for="item in extinguishPoints"
        :key="item.id"
        :transform="`translate(${point(item.pose_json.x, item.pose_json.y).x} ${point(item.pose_json.x, item.pose_json.y).y})`"
        d="M0,-5 L5,4 L-5,4 Z"
        class="extinguish-point"
      />
      <g
        v-if="robot?.x !== undefined && robot?.y !== undefined"
        :transform="`translate(${point(robot.x, robot.y).x} ${point(robot.x, robot.y).y}) rotate(${(-(robot.theta || 0) * 180) / Math.PI})`"
        class="robot-marker"
        filter="url(#robotGlow)"
      >
        <circle r="12" />
        <path d="M 0 -11 L 6 5 L 0 2 L -6 5 Z" />
        <text x="18" y="4">{{ robot.vehicle_id }}</text>
      </g>
    </svg>
    <div class="map-tools">
      <button aria-label="放大" @click="zoom(1)">+</button
      ><button aria-label="缩小" @click="zoom(-1)">−</button>
    </div>
    <div class="map-legend">
      <span><i class="dot inspect"></i>巡检点</span><span><i class="triangle"></i>灭火点</span
      ><span><i class="line"></i>轨迹</span>
    </div>
  </div>
</template>
