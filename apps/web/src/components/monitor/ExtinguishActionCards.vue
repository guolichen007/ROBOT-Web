<script setup lang="ts">
import iconBlanket from '@/assets/yd/actions/icon_extinguish_blanket_v4.svg'
import iconSpray from '@/assets/yd/actions/icon_spray_agent_v4.svg'
import iconJoint from '@/assets/yd/actions/icon_joint_extinguish_v4.svg'

defineProps<{ disabledReason?: string; busyMode?: string }>()
const emit = defineEmits<{ execute: [mode: 'DEPLOY_BLANKET' | 'SPRAY_AGENT' | 'DEPLOY_THEN_SPRAY'] }>()
const actions = [
  { value: 'DEPLOY_BLANKET' as const, title: '展开灭火帐', detail: '展开并覆盖目标车辆', icon: iconBlanket },
  { value: 'SPRAY_AGENT' as const, title: '喷射灭火剂', detail: '对准目标执行药剂喷射', icon: iconSpray },
  { value: 'DEPLOY_THEN_SPRAY' as const, title: '灭火帐 + 喷射', detail: '先展开灭火帐，再喷射灭火剂', icon: iconJoint },
]
</script>
<template>
  <section class="extinguish-actions">
    <button
      v-for="item in actions"
      :key="item.value"
      :disabled="Boolean(busyMode) || Boolean(disabledReason)"
      :class="{ busy: busyMode === item.value }"
      @click="emit('execute', item.value)"
    >
      <img :src="item.icon" alt="" />
      <span class="extinguish-copy">
        <strong>{{ busyMode === item.value ? '正在下发…' : item.title }}</strong>
        <span>{{ disabledReason || item.detail }}</span>
      </span>
    </button>
  </section>
</template>
