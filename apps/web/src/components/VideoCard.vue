<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { FullscreenIcon, VideoCameraIcon } from 'tdesign-icons-vue-next'
import { api, errorMessage } from '@/lib/api'
import { useSystemClock } from '@/composables/useSystemClock'
import { streamStateLabel } from '@/lib/ui-labels'
import type { StreamInfo } from '@/types'

const props = withDefaults(
  defineProps<{
    stream?: StreamInfo
    title: string
    prominent?: boolean
    active?: boolean
  }>(),
  { prominent: false, active: true },
)
const video = ref<HTMLVideoElement>()
const stage = ref<HTMLDivElement>()
const state = ref<'DISABLED' | 'OFFLINE' | 'CONNECTING' | 'LIVE' | 'ERROR'>(props.stream?.state || 'OFFLINE')
const detail = ref('')
const { now } = useSystemClock()
let peer: RTCPeerConnection | null = null
let resourceUrl = ''

const canPlay = computed(() => Boolean(props.stream?.stream_id && props.stream.state !== 'DISABLED'))
const shouldAutoConnect = computed(
  () => props.active && ['CONNECTING', 'LIVE'].includes(props.stream?.state || 'OFFLINE'),
)
const stateText = computed(() => {
  if (state.value === 'DISABLED') return '通道已停用'
  if (state.value === 'CONNECTING') return '正在连接视频'
  if (state.value === 'ERROR') return '视频连接异常'
  return '视频未连接'
})

async function waitIce(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === 'complete') return
  await new Promise<void>((resolve) => {
    const done = () => {
      if (pc.iceGatheringState === 'complete') {
        pc.removeEventListener('icegatheringstatechange', done)
        resolve()
      }
    }
    pc.addEventListener('icegatheringstatechange', done)
    window.setTimeout(resolve, 3000)
  })
}

async function disconnect(): Promise<void> {
  const old = peer
  peer = null
  if (resourceUrl) {
    void fetch(resourceUrl, { method: 'DELETE', credentials: 'same-origin' }).catch(() => undefined)
    resourceUrl = ''
  }
  old?.close()
  if (video.value) video.value.srcObject = null
  state.value = props.stream?.state || 'OFFLINE'
}

async function connect(): Promise<void> {
  await disconnect()
  if (!canPlay.value || !props.stream) return
  state.value = 'CONNECTING'
  detail.value = ''
  try {
    const ticket = (await api.post('/media/tickets', { stream_id: props.stream.stream_id })).data
    const pc = new RTCPeerConnection()
    peer = pc
    pc.addTransceiver('video', { direction: 'recvonly' })
    pc.ontrack = (event) => {
      if (video.value) video.value.srcObject = event.streams[0]
      if (pc.connectionState === 'connected') state.value = 'LIVE'
    }
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'connected' && video.value?.srcObject) state.value = 'LIVE'
      if (['failed', 'disconnected'].includes(pc.connectionState)) state.value = 'ERROR'
    }
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await waitIce(pc)
    const response = await fetch(ticket.playback_url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${ticket.ticket}` },
      body: pc.localDescription?.sdp,
    })
    if (!response.ok) throw new Error(`WHEP ${response.status}`)
    const location = response.headers.get('Location')
    if (location) resourceUrl = new URL(location, window.location.href).toString()
    await pc.setRemoteDescription({ type: 'answer', sdp: await response.text() })
  } catch (reason) {
    state.value = 'ERROR'
    detail.value = errorMessage(reason)
  }
}

async function enterFullscreen(): Promise<void> {
  try {
    if (stage.value?.requestFullscreen) await stage.value.requestFullscreen()
    else if (video.value?.requestFullscreen) await video.value.requestFullscreen()
  } catch {
    /* fullscreen may be denied; keep the overlay stable */
  }
}

watch(
  () => [props.stream?.stream_id, props.stream?.state, props.active],
  () => {
    if (shouldAutoConnect.value) void connect()
    else void disconnect()
  },
)
onUnmounted(() => void disconnect())
</script>

<template>
  <article class="video-card" :class="{ prominent }">
    <div ref="stage" class="video-stage">
      <video v-show="state === 'LIVE'" ref="video" autoplay muted playsinline />
      <div v-if="state === 'LIVE'" class="video-time">
        {{ new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(now) }}
      </div>
      <div v-if="state === 'LIVE'" class="video-live-tag">{{ streamStateLabel(state) }}</div>
      <button
        v-if="state === 'LIVE'"
        class="video-fullscreen"
        type="button"
        aria-label="全屏"
        @click="enterFullscreen"
      >
        <FullscreenIcon />
      </button>
      <div v-if="state !== 'LIVE'" class="video-placeholder">
        <span class="video-glyph"><VideoCameraIcon /></span>
        <strong>{{ stateText }}</strong>
        <small v-if="detail">{{ detail }}</small>
        <t-button v-if="canPlay && state !== 'CONNECTING'" size="small" variant="outline" @click="connect"
          >连接视频</t-button
        >
      </div>
    </div>
  </article>
</template>
