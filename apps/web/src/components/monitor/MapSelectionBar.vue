<script setup lang="ts">
import { localizationLabel } from '@/lib/ui-labels'
import type { DetectionCoverage, NavigationPreset, ParkingSlot, RobotState } from '@/types'
defineProps<{
  parkingSlot: ParkingSlot
  preset?: NavigationPreset
  robot?: RobotState
  coverage?: DetectionCoverage | null
  disabledReason?: string
}>()
defineEmits<{ cancel: []; navigate: []; alarm: [] }>()
</script>
<template>
  <div class="map-selection-bar">
    <div>
      <strong>已选择 {{ parkingSlot.code }}</strong
      ><span
        >{{ preset ? `巡检预设点 ${preset.code}` : '该车位未关联巡检预设点' }} · 地图
        {{ robot?.map_version || '--' }}</span
      >
    </div>
    <div>
      <span>定位{{ localizationLabel(robot?.localization_status) }}</span
      ><span>右侧覆盖 {{ coverage?.covered_parking_slot_ids.includes(parkingSlot.id) ? '是' : '否' }}</span
      ><small v-if="disabledReason">{{ disabledReason }}</small>
    </div>
    <t-button variant="outline" @click="$emit('alarm')">人工上报火情</t-button
    ><t-button :disabled="Boolean(disabledReason)" @click="$emit('navigate')">确认前往检测点</t-button
    ><button class="text-button" @click="$emit('cancel')">取消</button>
  </div>
</template>
