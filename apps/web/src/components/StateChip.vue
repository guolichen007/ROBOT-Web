<script setup lang="ts">
import { computed } from 'vue'
import { stateChipLabel } from '@/lib/ui-labels'

const props = defineProps<{ value: string }>()
const label = computed(() => stateChipLabel(props.value))
</script>

<template>
  <span class="state-chip" :data-state="value.toLowerCase()">{{ label }}</span>
</template>
