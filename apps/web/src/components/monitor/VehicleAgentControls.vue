<script setup lang="ts">
import { computed, toRef } from 'vue'
import { useMonitorStore } from '@/stores/monitor'
import { useAgentCommands, type AgentCommands } from '@/composables/useAgentCommands'
import { agentActions, type AgentAction } from '@/lib/vehicle-agent'
import type { ParkingSlot } from '@/types'

/**
 * vehicle_agent v1 buttons. WEB-R1C: the console passes ONE shared `commands` instance and
 * renders the device subset (start/close, left panel) and the task subset (bottom bar) from it.
 * Standalone use (no `commands`) keeps the full legacy panel for tests/other pages.
 */
const props = withDefaults(
  defineProps<{
    selectedSlotId: string | null
    slots: ParkingSlot[]
    commands?: AgentCommands
    actions?: readonly AgentAction[]
    variant?: 'panel' | 'bar'
  }>(),
  { commands: undefined, actions: undefined, variant: 'panel' },
)
const emit = defineEmits<{ 'update:selectedSlotId': [value: string | null] }>()
const monitor = useMonitorStore()
const agent = props.commands ?? useAgentCommands(toRef(props, 'selectedSlotId'), toRef(props, 'slots'))
const shown = computed(() => new Set<AgentAction>(props.actions ?? agentActions.map(([action]) => action)))
const groups = computed(() =>
  [
    { title: '智能车状态', items: agentActions.slice(0, 2) },
    { title: '巡逻任务', items: agentActions.slice(2, 6) },
    { title: '停车位', items: agentActions.slice(6) },
  ]
    .map((group) => ({ ...group, items: group.items.filter(([action]) => shown.value.has(action)) }))
    .filter((group) => group.items.length),
)
const flat = computed(() => agentActions.filter(([action]) => shown.value.has(action)))
// the single error line lives next to the button that produced it (unique on the page)
const ownsFeedback = computed(
  () => !props.actions || (agent.lastAction.value !== '' && shown.value.has(agent.lastAction.value)),
)
const isStop = (action: string) => ['close', 'stop_patrol', 'stop_navigate'].includes(action)

defineExpose({ send: agent.send })
</script>

<template>
  <div v-if="variant === 'bar'" class="hmi-agent-bar" role="group" aria-label="车辆任务（vehicle_agent v1）">
    <label v-if="shown.has('navigate')" class="hmi-slot-label hmi-slot-label--bar"
      >目标车位
      <select
        aria-label="停车位"
        :value="selectedSlotId || ''"
        @change="emit('update:selectedSlotId', ($event.target as HTMLSelectElement).value || null)"
      >
        <option value="">请选择停车位</option>
        <option v-for="slot in slots" :key="slot.id" :value="slot.id" :disabled="!slot.enabled">
          {{ slot.code }}
        </option>
      </select>
    </label>
    <button
      v-for="[action, label] in flat"
      :key="action"
      type="button"
      :data-action="action"
      :class="{ 'hmi-command-stop': isStop(action) }"
      :disabled="agent.disabled(action)"
      :aria-busy="agent.busy(action)"
      @click="agent.send(action)"
    >
      {{ agent.busy(action) ? `${label}…` : label }}
    </button>
    <p
      v-if="agent.error.value && ownsFeedback"
      role="alert"
      class="hmi-command-error"
      :data-code="agent.diagnosticCode.value"
    >
      {{ agent.error.value }}
    </p>
  </div>
  <section v-else class="hmi-auto-controls" aria-label="vehicle_agent 控制">
    <div class="hmi-task">
      <span>任务状态</span><strong data-testid="agent-task-state">{{ agent.taskLabel.value }}</strong>
    </div>
    <p v-if="!agent.protocolBound.value" class="hmi-hint">
      当前车辆未绑定 vehicle_agent v1，此处按钮不可用（不发送任何命令）。
    </p>
    <p v-else-if="!monitor.connected || monitor.robot?.online_state !== 'ONLINE'" class="hmi-hint">
      车辆离线，历史数据仅供参考。
    </p>
    <p v-else-if="!Object.values(monitor.robot?.vehicle_agent_ready || {}).some(Boolean)" class="hmi-hint">
      车辆状态或控制合同尚未就绪。
    </p>
    <template v-for="group in groups" :key="group.title">
      <h3>{{ group.title }}</h3>
      <label v-if="group.title === '停车位'" class="hmi-slot-label"
        >目标车位
        <select
          aria-label="停车位"
          :value="selectedSlotId || ''"
          @change="emit('update:selectedSlotId', ($event.target as HTMLSelectElement).value || null)"
        >
          <option value="">请选择停车位</option>
          <option v-for="slot in slots" :key="slot.id" :value="slot.id" :disabled="!slot.enabled">
            {{ slot.code }}
          </option>
        </select>
      </label>
      <div class="hmi-command-grid">
        <button
          v-for="[action, label] in group.items"
          :key="action"
          type="button"
          :data-action="action"
          :class="{ 'hmi-command-stop': isStop(action) }"
          :disabled="agent.disabled(action)"
          :aria-busy="agent.busy(action)"
          @click="agent.send(action)"
        >
          {{ agent.busy(action) ? `${label}…` : label }}
        </button>
      </div>
    </template>
    <p v-if="agent.awaitingStatus.value" role="status" class="hmi-hint">已收到 ACK，等待车辆状态确认。</p>
    <p
      v-if="agent.error.value && ownsFeedback"
      role="alert"
      class="hmi-command-error"
      :data-code="agent.diagnosticCode.value"
    >
      {{ agent.error.value }}
    </p>
  </section>
</template>
