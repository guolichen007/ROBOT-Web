<script setup lang="ts">
const props = defineProps<{ value: number; label?: string }>()
const radius = 42
const circumference = 2 * Math.PI * radius
</script>

<template>
  <div
    class="g4-progress-ring"
    :aria-label="`${label || '任务进度'} ${Math.round(props.value)}%`"
  >
    <svg viewBox="0 0 100 100">
      <circle cx="50" cy="50" :r="radius" class="track" />
      <circle
        cx="50"
        cy="50"
        :r="radius"
        class="value"
        :style="{
          strokeDasharray: circumference,
          strokeDashoffset: circumference * (1 - Math.min(100, Math.max(0, props.value)) / 100),
        }"
      />
    </svg>
    <strong>{{ Math.round(props.value) }}%</strong>
  </div>
</template>

<style scoped>
.g4-progress-ring {
  position: relative;
  width: 84px;
  height: 84px;
  flex: 0 0 auto;
}
.g4-progress-ring svg {
  width: 100%;
  height: 100%;
  transform: rotate(-90deg);
}
.g4-progress-ring .track {
  fill: none;
  stroke: #e3ecf8;
  stroke-width: 7;
}
.g4-progress-ring .value {
  fill: none;
  stroke: var(--yd-primary);
  stroke-width: 7;
  stroke-linecap: round;
  transition: stroke-dashoffset 0.4s ease;
}
.g4-progress-ring strong {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  font-size: 17px;
  font-weight: 700;
  color: #17365e;
}
</style>
