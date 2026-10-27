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
let sessionEpoch = 0
let disposed = false
// 有界自动重连（R2-15）：仅「用户已建立过会话 + 流仍 active + 短暂断开」时允许，
// 次数/退避有界，epoch 防旧连接污染；用户从未点击播放绝不拉视频。
const MAX_RECONNECT_ATTEMPTS = 3
const RECONNECT_BASE_DELAY_MS = 1000
let hadLiveSession = false
let reconnectAttempts = 0
let reconnectTimer: number | undefined

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
    let timerId: number | undefined
    const done = () => {
      if (pc.iceGatheringState === 'complete') {
        if (timerId !== undefined) window.clearTimeout(timerId)
        pc.removeEventListener('icegatheringstatechange', done)
        resolve()
      }
    }
    pc.addEventListener('icegatheringstatechange', done)
    // 3s 兜底：到期必须清理监听器，避免泄漏（此前未清理）
    timerId = window.setTimeout(() => {
      timerId = undefined
      pc.removeEventListener('icegatheringstatechange', done)
      resolve()
    }, 3000)
  })
}

function cancelReconnectTimer(): void {
  if (reconnectTimer !== undefined) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = undefined
  }
}

// 用户显式发起新会话时重置重连计数；disconnect 只取消挂起定时器
// （epoch 已使过期回调自失效，计数保留给重连链路使用）。
function resetReconnect(): void {
  cancelReconnectTimer()
  reconnectAttempts = 0
  hadLiveSession = false
}

function scheduleReconnect(epoch: number, streamId: string): void {
  if (reconnectTimer !== undefined) return // 同一次中断只挂一个定时器（failed/disconnected 事件可能连发）
  if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    if (epoch === sessionEpoch && !disposed && props.active && canPlay.value) {
      state.value = 'ERROR'
      detail.value = '自动重连失败，请手动重试'
    }
    return
  }
  const delay = RECONNECT_BASE_DELAY_MS * 2 ** reconnectAttempts
  reconnectAttempts += 1
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = undefined
    if (epoch !== sessionEpoch || disposed || !props.active || !canPlay.value) return
    if (props.stream?.stream_id !== streamId) return
    void startSession(true)
  }, delay)
}

async function disconnect(): Promise<void> {
  sessionEpoch += 1
  cancelReconnectTimer()
  hadLiveSession = false
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
// 有界自动重连只走内部 startSession(true)，绝不改变「用户主动开始」语义。
async function connect(): Promise<void> {
  resetReconnect()
  await startSession(false)
}

async function startSession(isReconnect: boolean): Promise<void> {
  if (connecting) return
  const current = peer
  if (current && ['new', 'connecting', 'connected'].includes(current.connectionState)) return
  if (!canPlay.value || !props.stream || !props.active || disposed) return

  connecting = true
  const streamId = props.stream.stream_id
  await disconnect()
  if (isReconnect) hadLiveSession = true // disconnect() 已重置；重连路径保留会话语义
  const epoch = sessionEpoch
  const valid = () =>
    epoch === sessionEpoch &&
    !disposed &&
    props.active &&
    canPlay.value &&
    props.stream?.stream_id === streamId
  try {
    if (!valid()) return
    state.value = 'CONNECTING'
    detail.value = isReconnect ? '连接中断，正在自动重连…' : ''
    const ticket = (await api.post('/media/tickets', { stream_id: streamId })).data
    if (!valid()) return
    const pc = new RTCPeerConnection()
    peer = pc
    pc.addTransceiver('video', { direction: 'recvonly' })
    pc.ontrack = (event) => {
      if (!valid()) return
      if (video.value) video.value.srcObject = event.streams[0]
      if (pc.connectionState === 'connected') state.value = 'LIVE'
    }
    pc.onconnectionstatechange = () => {
      if (!valid()) return
      if (pc.connectionState === 'connected' && video.value?.srcObject) {
        state.value = 'LIVE'
        hadLiveSession = true
      }
      if (['failed', 'disconnected'].includes(pc.connectionState)) {
        // 短暂断开：仅已建立过会话且流仍 active 时有界自动重连
        if (hadLiveSession) {
          scheduleReconnect(epoch, streamId)
        } else {
          state.value = 'ERROR'
        }
      }
    }
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await waitIce(pc)
    if (!valid()) return
    const response = await fetch(ticket.playback_url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${ticket.ticket}` },
      body: pc.localDescription?.sdp,
    })
    if (!response.ok) throw new Error(`WHEP ${response.status}`)
    const location = response.headers.get('Location')
    const createdResource = location ? new URL(location, window.location.href).toString() : ''
    if (!valid()) {
      if (createdResource)
        void fetch(createdResource, { method: 'DELETE', credentials: 'same-origin' }).catch(() => undefined)
      return
    }
    resourceUrl = createdResource
    const answer = await response.text()
    if (valid()) await pc.setRemoteDescription({ type: 'answer', sdp: answer })
  } catch (reason) {
    if (!valid()) return
    // 失败会话的 peer 若停留在 'new' 状态会阻塞入口守卫（重试与手动重连），必须显式关闭。
    peer?.close()
    peer = null
    if (video.value) video.value.srcObject = null
    if (isReconnect && hadLiveSession) {
      state.value = 'CONNECTING'
      detail.value = '重连失败，正在重试…'
      scheduleReconnect(epoch, streamId)
    } else {
      state.value = 'ERROR'
      detail.value = errorMessage(reason)
    }
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

onUnmounted(() => {
  disposed = true
  void disconnect()
})
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
