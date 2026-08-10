<script setup lang="ts">
defineProps<{ value: string }>()
</script>

<template>
  <span class="state-chip" :data-state="value.toLowerCase()">{{ value }}</span>
</template>
