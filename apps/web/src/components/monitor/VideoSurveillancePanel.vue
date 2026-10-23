<script setup lang="ts">
import { computed } from 'vue'
import VideoCard from '@/components/VideoCard.vue'
import type { StreamInfo } from '@/types'

const props = defineProps<{ streams: StreamInfo[] }>()

// 两路同时挂载，切换控制面板不销毁播放器会话。
const CAMS = [
  { type: 'roof_rgb', label: '监控相机' },
  { type: 'roof_thermal', label: '红外热成像' },
] as const

const byType = computed(() => Object.fromEntries(props.streams.map((item) => [item.camera_type, item])))
const cards = computed(() =>
  CAMS.map((cam) => ({ type: cam.type, label: cam.label, stream: byType.value[cam.type] })),
)
</script>
<template>
  <section class="panel video-surveillance">
    <header class="video-surveillance-head"><span>视频监控</span></header>
    <div class="video-grid">
      <div v-for="card in cards" :key="card.type" class="hmi-camera">
        <h3>{{ card.label }}</h3>
        <VideoCard
          class="video-active-card"
          :stream="card.stream"
          :title="card.label"
          :active="true"
          prominent
        />
      </div>
    </div>
  </section>
</template>
