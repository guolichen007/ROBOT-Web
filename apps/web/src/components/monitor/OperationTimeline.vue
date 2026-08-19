<script setup lang="ts">
import { sourceTypeLabel, stateChipLabel } from '@/lib/ui-labels'
import type { AlarmTimelineItem } from '@/types'

defineProps<{ items: AlarmTimelineItem[] }>()
</script>

<template>
  <div class="operation-timeline">
    <h4>处置进度</h4>
    <ol>
      <li v-for="item in items" :key="`${item.occurred_at}-${item.state}`">
        <time>{{ new Date(item.occurred_at).toLocaleTimeString('zh-CN', { hour12: false }) }}</time>
        <span>{{ item.label }}</span>
        <small>{{ sourceTypeLabel(item.source_type) }} · {{ stateChipLabel(item.state) }}</small>
      </li>
    </ol>
    <div v-if="!items.length" class="quiet-state">暂无处置记录</div>
  </div>
</template>
