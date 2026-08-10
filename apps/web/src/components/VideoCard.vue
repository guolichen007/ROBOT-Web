<script setup lang="ts">
import type { StreamInfo } from '@/types'
import StateChip from './StateChip.vue'

const props = defineProps<{ stream?: StreamInfo; title: string; glyph: string }>()
</script>

<template>
  <article class="video-card">
    <header>
      <span>{{ title }}</span
      ><StateChip :value="props.stream?.state || 'OFFLINE'" />
    </header>
    <div class="video-placeholder">
      <span class="video-glyph">{{ glyph }}</span>
      <strong>{{ props.stream?.state === 'DISABLED' ? '通道已禁用' : '视频源未连接' }}</strong>
      <small>MediaMTX · {{ props.stream?.codec || 'H.264 预留' }}</small>
    </div>
  </article>
</template>
