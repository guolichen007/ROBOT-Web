<script setup lang="ts">
import { computed, ref } from 'vue'
import VideoCard from '@/components/VideoCard.vue'
import type { StreamInfo } from '@/types'
const props = defineProps<{ streams: StreamInfo[] }>()
const active = ref('roof_rgb')
const byType = computed(() => Object.fromEntries(props.streams.map((item) => [item.camera_type, item])))
const tabs = [
  { value: 'roof_rgb', label: '车顶实时相机' },
  { value: 'roof_thermal', label: '顶部热像' },
  { value: 'bottom_ir', label: '底部红外' },
]
const activeStream = computed(() => byType.value[active.value])
const activeLabel = computed(() => tabs.find((item) => item.value === active.value)?.label || '实时视频')
</script>
<template>
  <section class="panel video-surveillance">
    <header class="video-surveillance-head"><span>实时视频</span></header>
    <div class="video-tabs" role="tablist">
      <button
        v-for="tab in tabs"
        :key="tab.value"
        type="button"
        role="tab"
        :aria-selected="active === tab.value"
        :class="['video-tab', { active: active === tab.value }]"
        @click="active = tab.value"
      >
        {{ tab.label }}
      </button>
    </div>
    <VideoCard
      class="video-active-card"
      :stream="activeStream"
      :title="activeLabel"
      :active="true"
      prominent
    />
  </section>
</template>
