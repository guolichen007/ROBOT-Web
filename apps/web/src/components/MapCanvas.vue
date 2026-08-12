<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
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
}>()
const emit = defineEmits<{ slotClick: [slot: ParkingSlot] }>()
const frame = ref<HTMLDivElement>()
const size = ref({ width: 900, height: 560 })
const zoomLevel = ref(1)
let observer: ResizeObserver

const geometry = computed(
  () => props.mapVersion || { width_m: 48, height_m: 34, origin_x: 0, origin_y: 0, rotation_rad: 0 },
)
const adapter = computed(() => {
  const result = new MapAdapter(geometry.value, size.value)
  result.setZoom(zoomLevel.value)
  return result
})
const point = (x: number, y: number) => adapter.value.worldToScreen({ x, y })
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
const coveredSlots = computed(() => new Set(props.coverage?.covered_parking_slot_ids || []))

function updateSize(): void {
  if (frame.value) size.value = { width: frame.value.clientWidth, height: frame.value.clientHeight }
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
  <div ref="frame" class="map-canvas operations-map">
    <svg :viewBox="`0 0 ${size.width} ${size.height}`" role="img" aria-label="停车场二维态势地图">
      <rect width="100%" height="100%" class="map-floor" />
      <g class="lane-markings">
        <line :x1="point(39, 1).x" :y1="point(39, 1).y" :x2="point(39, 28.5).x" :y2="point(39, 28.5).y" />
        <line :x1="point(3, 28.5).x" :y1="point(3, 28.5).y" :x2="point(39, 28.5).x" :y2="point(39, 28.5).y" />
      </g>
      <polyline v-if="pathPoints" :points="pathPoints" class="trajectory-line planned" />
      <polygon v-if="coveragePoints" :points="coveragePoints" class="detection-sector" />
      <g
        v-for="slot in slots"
        :key="slot.id"
        class="slot-group"
        role="button"
        tabindex="0"
        :aria-label="`车位 ${slot.code}`"
        @click="emit('slotClick', slot)"
        @keydown.enter.prevent="emit('slotClick', slot)"
      >
        <polygon
          :points="slotPoints(slot)"
          :class="[
            'parking-slot',
            {
              alarm: alarmSlots.has(slot.id),
              critical: alarmSlots.get(slot.id)?.severity === 'CRITICAL',
              selected: selectedSlotId === slot.id,
              covered: coveredSlots.has(slot.id),
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
        v-if="robot?.x !== undefined && robot?.y !== undefined"
        :transform="`translate(${point(robot.x, robot.y).x} ${point(robot.x, robot.y).y}) rotate(${(-(robot.theta || 0) * 180) / Math.PI})`"
        class="robot-marker"
      >
        <rect x="-8" y="-12" width="16" height="24" rx="4" />
        <path d="M0 -18 L6 -8 L0 -10 L-6 -8 Z" />
        <text x="15" y="4">{{ robot.vehicle_id }}</text>
      </g>
    </svg>
    <div class="map-tools">
      <button aria-label="放大" @click="zoom(1)">+</button
      ><button aria-label="缩小" @click="zoom(-1)">−</button>
    </div>
    <div class="map-legend">
      <span><i class="legend-slot"></i>车位</span><span><i class="legend-robot"></i>车辆</span
      ><span><i class="legend-sector"></i>右侧检测</span><span><i class="legend-fire"></i>火情</span>
    </div>
  </div>
</template>
