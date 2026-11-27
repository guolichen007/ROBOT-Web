<script setup lang="ts">
import { computed } from 'vue'
import VehicleAgentControls from '@/components/monitor/VehicleAgentControls.vue'
import type { AgentCommands } from '@/composables/useAgentCommands'
import type { ActionState } from '@/composables/useOperationReadiness'
import type { ParkingSlot } from '@/types'

/**
 * WEB-R1C 设备页签：车辆启动/关闭 + 防火罩 展开/停止/回收 + 实际反馈。
 * 可用性只来自服务端 OperationReadiness（start_vehicle / shutdown_vehicle / fire_shield_*）；
 * 未接通的动作显示服务端给出的不可用原因。防火罩没有车端驱动与限位反馈来源：
 * 位置与限位一律显示“未知/未接入”，绝不显示“已展开/已回收”。
 */
const props = defineProps<{
  protocolBound: boolean
  commands: AgentCommands
  selectedSlotId: string | null
  slots: ParkingSlot[]
  actionState: (action: string) => ActionState
}>()

const power = computed(() =>
  [
    { action: 'start_vehicle', label: '启动' },
    { action: 'shutdown_vehicle', label: '关闭' },
  ].map((item) => ({ ...item, state: props.actionState(item.action) })),
)
const shield = computed(() =>
  [
    { action: 'fire_shield_open', label: '展开' },
    { action: 'fire_shield_stop', label: '停止' },
    { action: 'fire_shield_close', label: '回收' },
  ].map((item) => ({ ...item, state: props.actionState(item.action) })),
)
const shieldReason = computed(() =>
  [...new Set(shield.value.flatMap((item) => item.state.labels))].join('、'),
)
const powerReason = computed(() => [...new Set(power.value.flatMap((item) => item.state.labels))].join('、'))
</script>

<template>
  <section class="device-tab" aria-label="设备操作">
    <section class="device-block" data-testid="device-power">
      <h3>车辆</h3>
      <VehicleAgentControls
        v-if="protocolBound"
        :commands="commands"
        :actions="['start', 'close']"
        :selected-slot-id="selectedSlotId"
        :slots="slots"
      />
      <template v-else>
        <div class="device-actions">
          <button
            v-for="item in power"
            :key="item.action"
            type="button"
            :data-device-action="item.action"
            :disabled="!item.state.allowed"
            :aria-describedby="'device-power-reason'"
          >
            {{ item.label }}
          </button>
        </div>
        <small id="device-power-reason" class="device-reason">{{ powerReason || '—' }}</small>
      </template>
    </section>
    <section class="device-block" data-testid="device-fire-shield">
      <h3>防火罩</h3>
      <div class="device-actions device-actions--3">
        <button
          v-for="item in shield"
          :key="item.action"
          type="button"
          :data-device-action="item.action"
          :disabled="!item.state.allowed"
          :aria-describedby="'device-shield-reason'"
        >
          {{ item.label }}
        </button>
      </div>
      <small id="device-shield-reason" class="device-reason">{{ shieldReason || '—' }}</small>
      <dl class="device-feedback" aria-label="防火罩实际反馈">
        <div>
          <dt>位置</dt>
          <dd data-quality="UNKNOWN">未知</dd>
        </div>
        <div>
          <dt>开到位限位</dt>
          <dd data-quality="UNKNOWN">--</dd>
        </div>
        <div>
          <dt>关到位限位</dt>
          <dd data-quality="UNKNOWN">--</dd>
        </div>
        <div>
          <dt>反馈来源</dt>
          <dd data-quality="NOT_CONNECTED">未接入（无车端驱动/限位合同）</dd>
        </div>
      </dl>
    </section>
  </section>
</template>
