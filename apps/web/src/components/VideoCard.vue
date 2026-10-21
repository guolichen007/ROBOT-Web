<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { FullscreenIcon, VideoCameraIcon } from 'tdesign-icons-vue-next'
import { api, errorMessage } from '@/lib/api'
import { useSystemClock } from '@/composables/useSystemClock'
import { streamStateLabel } from '@/lib/ui-labels'
import type { StreamInfo } from '@/types'

type PlayerState = 'DISABLED' | 'OFFLINE' | 'CONNECTING' | 'LIVE' | 'ERROR'

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

// 本地播放器状态只描述「当前浏览器播放器」，与媒体源状态严格解耦：
// 服务端 stream.state 只代表媒体源状态，绝不直接映射为播放器 LIVE。
function idlePlayerState(): 'DISABLED' | 'OFFLINE' {
  return props.stream?.state === 'DISABLED' ? 'DISABLED' : 'OFFLINE'
}

const state = ref<PlayerState>(idlePlayerState())
const detail = ref('')
const { now } = useSystemClock()
let peer: RTCPeerConnection | null = null
let resourceUrl = ''
let connecting = false

const canPlay = computed(() => Boolean(props.stream?.stream_id && props.stream.state !== 'DISABLED'))
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
  // 播放器回到空闲态，而不是回落到「媒体源状态」。
  state.value = idlePlayerState()
}

// 建立播放器会话的唯一入口：用户点击「连接视频」。
// 普通 WS 更新 / snapshot 刷新 / 机器人状态 / 检测覆盖都不得触发它。
async function connect(): Promise<void> {
  if (connecting) return
  const current = peer
  if (current && ['new', 'connecting', 'connected'].includes(current.connectionState)) return
  if (!canPlay.value || !props.stream) return

  connecting = true
  try {
    await disconnect()
    state.value = 'CONNECTING'
    detail.value = ''
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
  } finally {
    connecting = false
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

// 只有 stream_id 真正变化才销毁旧会话；不自动连接新流。
watch(
  () => props.stream?.stream_id,
  () => void disconnect(),
)

// 只有本卡片变为 inactive 才断开。
watch(
  () => props.active,
  (isActive) => {
    if (!isActive) void disconnect()
  },
)

// 只有媒体源被明确 DISABLED 才断开；OFFLINE / CONNECTING / LIVE 的变化不得销毁浏览器会话。
watch(
  () => props.stream?.state,
  (next) => {
    if (next === 'DISABLED') void disconnect()
  },
)

onUnmounted(() => void disconnect())
</script>

<template>
  <article class="video-card" :class="{ prominent }">
    <div ref="stage" class="video-stage">
      <video v-show="state === 'LIVE'" ref="video" autoplay muted playsinline />
      <div v-if="state === 'LIVE'" class="video-time">
        {{
          new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(
            now,
          )
        }}
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
