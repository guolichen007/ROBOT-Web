<script setup lang="ts">
import { onMounted, ref } from 'vue'
import PageHeader from '@/components/PageHeader.vue'
import StateChip from '@/components/StateChip.vue'
import { api } from '@/lib/api'

const status = ref<any>({ checks: {} }),
  streams = ref<any[]>([]),
  settings = ref<any[]>([])
async function load(): Promise<void> {
  ;[status.value, streams.value, settings.value] = await Promise.all([
    api.get('/system/status').then((r) => r.data),
    api.get('/media/streams').then((r) => r.data),
    api.get('/admin/settings').then((r) => r.data),
  ])
}
onMounted(load)
</script>

<template>
  <PageHeader eyebrow="系统状态" title="系统状态" description="基础依赖、实时进程、媒体接入和部署边界。"
    ><button class="secondary-button compact" @click="load">刷新检查</button></PageHeader
  >
  <section class="service-grid">
    <article v-for="(check, name) in status.checks" :key="name" class="panel service-card">
      <span>{{ name }}</span
      ><StateChip :value="check.ok ? 'READY' : 'DEGRADED'" /><small v-if="check.latency_ms"
        >{{ check.latency_ms }} ms</small
      ><small v-else>{{ check.last_heartbeat || check.error || 'dependency check' }}</small>
    </article>
  </section>
  <div class="settings-grid">
    <section class="panel">
      <div class="panel-heading">
        <h3>视频通道</h3>
        <span>MediaMTX / WHEP</span>
      </div>
      <div v-for="stream in streams" :key="stream.id" class="setting-row">
        <div>
          <strong>{{ stream.camera_type }}</strong
          ><small>{{ stream.codec }} · {{ stream.provider }}</small>
        </div>
        <StateChip :value="stream.state" />
      </div>
    </section>
    <section class="panel">
      <div class="panel-heading">
        <h3>运行配置</h3>
        <span>database source of truth</span>
      </div>
      <div v-for="setting in settings" :key="setting.key" class="setting-row">
        <div>
          <strong>{{ setting.key }}</strong
          ><small>{{ JSON.stringify(setting.value_json) }}</small>
        </div>
      </div>
      <div class="deployment-stamp">
        <strong>服务端部署就绪</strong><span>{{ status.server_deployment_ready ? '是' : '否' }}</span
        ><small>已部署 = {{ status.server_deployed ? '是' : '否' }}</small>
      </div>
    </section>
  </div>
</template>
