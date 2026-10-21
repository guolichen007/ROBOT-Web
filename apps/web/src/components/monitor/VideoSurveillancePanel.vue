<script setup lang="ts">
import { computed } from 'vue'
import VideoCard from '@/components/VideoCard.vue'
import type { StreamInfo } from '@/types'

const props = defineProps<{ streams: StreamInfo[] }>()

// 固定并排展示两路：车顶实时相机 + 顶部热像（底部红外已移除）。
const CAMS = [
  { type: 'roof_rgb', label: '车顶实时相机' },
  { type: 'roof_thermal', label: '顶部热像' },
] as const

const byType = computed(() => Object.fromEntries(props.streams.map((item) => [item.camera_type, item])))
const cards = computed(() =>
  CAMS.map((cam) => ({ type: cam.type, label: cam.label, stream: byType.value[cam.type] })),
)
</script>
<template>
  <section class="panel video-surveillance">
    <header class="video-surveillance-head"><span>实时视频</span></header>
    <div class="video-grid">
      <VideoCard
        v-for="card in cards"
        :key="card.type"
        class="video-active-card"
        :stream="card.stream"
        :title="card.label"
        :active="true"
        prominent
      />
    </div>
  </section>
</template>
