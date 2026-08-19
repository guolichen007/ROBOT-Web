<script setup lang="ts">
import AlarmLifecycleActions from './AlarmLifecycleActions.vue'
import ExtinguishActionCards from './ExtinguishActionCards.vue'
import OperationTimeline from './OperationTimeline.vue'
import type { Alarm, AlarmTimelineItem } from '@/types'
defineProps<{
  alarm?: Alarm
  timeline: AlarmTimelineItem[]
  mode: string
  disabledReason?: string
  locationLabel?: string
  permissions: { ack: boolean; confirm: boolean; resolve: boolean }
}>()
defineEmits<{
  transition: [action: 'acknowledge' | 'confirm' | 'resolve']
  'update:mode': [value: string]
  dispatch: []
}>()
</script>
<template>
  <section class="panel primary-alarm-panel">
    <div v-if="alarm" class="primary-alarm-grid">
      <div class="alarm-detail-column">
        <header>
          <span>当前事件详情</span>
          <strong>{{ alarm.event_code }}</strong>
          <small>{{ alarm.fire_type }} · {{ alarm.severity }} · {{ alarm.state }}</small>
        </header>
        <dl>
          <div>
            <dt>事件类型</dt>
            <dd>{{ alarm.fire_type }}</dd>
          </div>
          <div>
            <dt>位置</dt>
            <dd>{{ locationLabel || '--' }}</dd>
          </div>
          <div>
            <dt>置信度</dt>
            <dd>{{ alarm.confidence == null ? '--' : `${(alarm.confidence * 100).toFixed(0)}%` }}</dd>
          </div>
          <div>
            <dt>首次发现</dt>
            <dd>{{ new Date(alarm.first_seen_at || alarm.last_seen_at).toLocaleString('zh-CN') }}</dd>
          </div>
          <div>
            <dt>检测方式</dt>
            <dd>{{ alarm.detection_method }}</dd>
          </div>
          <div>
            <dt>严重级别</dt>
            <dd>{{ alarm.severity }}</dd>
          </div>
          <div>
            <dt>重复上报</dt>
            <dd>{{ alarm.occurrence_count }}</dd>
          </div>
          <div>
            <dt>媒体证据</dt>
            <dd>
              {{
                alarm.media_snapshot_json && Object.keys(alarm.media_snapshot_json).length
                  ? 'AVAILABLE'
                  : 'MISSING'
              }}
            </dd>
          </div>
        </dl>
        <AlarmLifecycleActions
          :alarm="alarm"
          :can-ack="permissions.ack"
          :can-confirm="permissions.confirm"
          :can-resolve="permissions.resolve"
          @transition="$emit('transition', $event)"
        />
        <OperationTimeline :items="timeline" />
      </div>
      <div class="alarm-actions-column">
        <header>
          <span>处置操作</span>
        </header>
        <ExtinguishActionCards
          :model-value="mode"
          :disabled-reason="alarm.state !== 'CONFIRMED' ? '请先完成火情确认' : disabledReason"
          @update:model-value="$emit('update:mode', $event)"
          @confirm="$emit('dispatch')"
        />
      </div>
    </div>
    <div v-else class="alarm-empty">
      <strong>暂无活动火情</strong><span>历史事件请前往“火情”页面查询</span>
    </div>
  </section>
</template>
