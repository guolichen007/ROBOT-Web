<script setup lang="ts">
import AlarmLifecycleActions from './AlarmLifecycleActions.vue'
import ExtinguishActionCards from './ExtinguishActionCards.vue'
import OperationTimeline from './OperationTimeline.vue'
import { alarmStateLabel, alarmTypeLabel, detectionMethodLabel, severityLabel } from '@/lib/ui-labels'
import type { Alarm, AlarmTimelineItem } from '@/types'
defineProps<{
  alarm?: Alarm
  timeline: AlarmTimelineItem[]
  disabledReason?: string
  busyMode?: string
  locationLabel?: string
  permissions: { ack: boolean; confirm: boolean; resolve: boolean }
}>()
defineEmits<{ transition: [action: 'acknowledge' | 'confirm' | 'resolve']; execute: [mode: string] }>()
</script>
<template>
  <section class="panel primary-alarm-panel">
    <div v-if="alarm" class="primary-alarm-grid">
      <header class="alarm-head">
        <div class="alarm-head-copy">
          <span>当前火情</span>
          <strong>{{ alarm.event_code }}</strong>
          <small
            >{{ locationLabel || '--' }} · {{ alarmTypeLabel(alarm.fire_type) }} ·
            {{ severityLabel(alarm.severity) }} · {{ alarmStateLabel(alarm.state) }}</small
          >
        </div>
        <AlarmLifecycleActions
          :alarm="alarm"
          :can-ack="permissions.ack"
          :can-confirm="permissions.confirm"
          :can-resolve="permissions.resolve"
          @transition="$emit('transition', $event)"
        />
      </header>
      <div class="alarm-body">
        <div class="alarm-detail-column">
          <dl>
            <div>
              <dt>事件类型</dt>
              <dd>{{ alarmTypeLabel(alarm.fire_type) }}</dd>
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
              <dd>{{ detectionMethodLabel(alarm.detection_method) }}</dd>
            </div>
            <div>
              <dt>严重级别</dt>
              <dd>{{ severityLabel(alarm.severity) }}</dd>
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
                    ? '有媒体证据'
                    : '无媒体证据'
                }}
              </dd>
            </div>
          </dl>
          <OperationTimeline :items="timeline" />
        </div>
        <div class="alarm-actions-column">
          <header>
            <span>处置操作</span>
          </header>
          <ExtinguishActionCards
            :disabled-reason="disabledReason"
            :busy-mode="busyMode"
            @execute="$emit('execute', $event)"
          />
        </div>
      </div>
    </div>
    <div v-else class="alarm-empty">
      <strong>暂无活动火情</strong><span>历史事件请前往“火情”页面查询</span>
    </div>
  </section>
</template>
