<script setup lang="ts">
import { computed, onMounted } from 'vue'
import MapCanvas from '@/components/MapCanvas.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useMonitorStore } from '@/stores/monitor'

const monitor = useMonitorStore()
const trajectory = computed(() => monitor.snapshot.trajectories[0]?.path_json || [])
onMounted(() => {
  if (!monitor.snapshot.map_version) void monitor.loadSnapshot()
})
</script>

<template>
  <PageHeader
    eyebrow="车位与操作点"
    title="车位与操作点"
    description="车位、巡检点、灭火操作点与轨迹均绑定明确地图版本。"
    ><span class="policy-badge"
      >V{{ monitor.snapshot.map_version?.version || '--' }} / REV
      {{ monitor.snapshot.map_version?.semantic_revision || '--' }}</span
    ></PageHeader
  >
  <div class="parking-layout">
    <section class="panel map-config">
      <MapCanvas
        :map-version="monitor.snapshot.map_version"
        :slots="monitor.snapshot.parking_slots"
        :inspection-points="monitor.snapshot.inspection_points"
        :extinguish-points="monitor.snapshot.extinguish_points"
        :trajectory="trajectory"
        :alarms="[]"
        :show-semantic-points="true"
      />
    </section>
    <section class="panel slot-list">
      <div class="panel-heading">
        <h3>语义对象</h3>
        <span>{{ monitor.snapshot.parking_slots.length }} 车位</span>
      </div>
      <div v-for="slot in monitor.snapshot.parking_slots" :key="slot.id" class="slot-item">
        <strong>{{ slot.code }}</strong
        ><span>{{ slot.center_pose_json.x.toFixed(1) }}, {{ slot.center_pose_json.y.toFixed(1) }} m</span
        ><i :class="slot.enabled ? 'enabled' : ''"></i>
      </div>
    </section>
  </div>
</template>
