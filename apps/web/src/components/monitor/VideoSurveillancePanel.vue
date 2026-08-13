<script setup lang="ts">
import { computed, ref } from 'vue'
import VideoCard from '@/components/VideoCard.vue'
import type { StreamInfo } from '@/types'
const props = defineProps<{ streams: StreamInfo[] }>()
const active = ref('roof_rgb')
const byType = computed(() => Object.fromEntries(props.streams.map((item) => [item.camera_type, item])))
</script>
<template>
  <section class="panel video-surveillance">
    <t-tabs v-model="active"
      ><t-tab-panel value="roof_rgb" label="车顶实时相机"
        ><VideoCard
          title="车顶实时相机"
          :stream="byType.roof_rgb"
          :active="active === 'roof_rgb'"
          prominent /></t-tab-panel
      ><t-tab-panel value="roof_thermal" label="顶部热像"
        ><VideoCard
          title="顶部热像"
          :stream="byType.roof_thermal"
          :active="active === 'roof_thermal'"
          prominent /></t-tab-panel
      ><t-tab-panel value="bottom_ir" label="底部红外"
        ><VideoCard
          title="底部红外"
          :stream="byType.bottom_ir"
          :active="active === 'bottom_ir'"
          prominent /></t-tab-panel
    ></t-tabs>
  </section>
</template>
