<script setup lang="ts">
defineProps<{ modelValue: string; disabledReason?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; confirm: [] }>()
const actions = [
  { value: 'DEPLOY_BLANKET', title: '展开灭火帐', detail: '展开并覆盖目标车辆' },
  { value: 'SPRAY_AGENT', title: '喷射灭火剂', detail: '对准目标执行药剂喷射' },
  { value: 'DEPLOY_THEN_SPRAY', title: '灭火帐 + 喷射', detail: '先展开灭火帐，再喷射灭火剂' },
]
</script>
<template>
  <section class="extinguish-actions">
    <button
      v-for="item in actions"
      :key="item.value"
      :class="{ selected: modelValue === item.value }"
      :disabled="Boolean(disabledReason)"
      @click="emit('update:modelValue', item.value)"
    >
      <span class="extinguish-radio" aria-hidden="true"></span>
      <span class="extinguish-copy">
        <strong>{{ item.title }}</strong>
        <span>{{ disabledReason || item.detail }}</span>
      </span>
    </button>
    <div class="dispatch-summary">
      <span>已选：{{ actions.find((item) => item.value === modelValue)?.title }}</span>
      <t-button theme="danger" :disabled="Boolean(disabledReason)" @click="emit('confirm')"
        >确认派发</t-button
      >
    </div>
  </section>
</template>
