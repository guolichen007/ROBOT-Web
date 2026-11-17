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

// 首帧门控 + 画面停滞看门狗（G3-R1）：
// - WebRTC connected / ontrack 只代表传输就绪，**首个真实解码帧**到达前保持 CONNECTING（“等待首帧”）；
// - 以浏览器真实解码帧（requestVideoFrameCallback；不支持时 currentTime 推进）为准，
//   连续 STALL_MS 无新帧 → stalled（隐藏「实时」、显示“画面中断”），新帧到达即恢复；
// - 每次 watchFrames 取得新 watchToken 并取消旧 rVFC 回调与定时器（ontrack 与 connectionstatechange
//   都会调用，必须幂等），回调同时校验会话 epoch，旧会话/切车/销毁后的帧不得改写当前状态。
const STALL_MS = 3000
const FIRST_FRAME_TIMEOUT_MS = 10_000
const stalled = ref(false)
const firstFrameAt = ref<number | null>(null)
let lastFrameAt = 0
let lastMediaTime = -1
let stallTimer: number | undefined
let watchToken = 0
let watchedVideo: HTMLVideoElement | null = null
let frameHandle: number | undefined
let watchEpoch = -1
let watchStartedAt = 0

type FrameVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (cb: () => void) => number
  cancelVideoFrameCallback?: (handle: number) => void
}

function onFrame(epoch: number): void {
  if (epoch !== sessionEpoch || disposed) return
  lastFrameAt = performance.now()
  if (firstFrameAt.value === null) {
    firstFrameAt.value = lastFrameAt
    detail.value = ''
    state.value = 'LIVE'
    hadLiveSession = true
    reconnectAttempts = 0 // 真正恢复出画面才清零有界重连计数
  }
  if (stalled.value) stalled.value = false
}

function watchFrames(el: HTMLVideoElement, epoch: number): void {
  if (watchedVideo === el && stallTimer !== undefined && watchEpoch === epoch) return // 同会话幂等
  stopFrameWatch()
  const token = ++watchToken
  watchedVideo = el
  watchEpoch = epoch
  const fv = el as FrameVideo
  const hasRvfc = typeof fv.requestVideoFrameCallback === 'function'
  if (hasRvfc) {
    const tick = () => {
      if (token !== watchToken) return
      onFrame(epoch)
      frameHandle = fv.requestVideoFrameCallback!(tick)
    }
    frameHandle = fv.requestVideoFrameCallback!(tick)
  }
  lastFrameAt = performance.now()
  watchStartedAt = lastFrameAt
  stallTimer = window.setInterval(() => {
    if (token !== watchToken || epoch !== sessionEpoch) return
    if (!hasRvfc && el.currentTime !== lastMediaTime) {
      if (lastMediaTime >= 0) onFrame(epoch)
      lastMediaTime = el.currentTime
    }
    if (firstFrameAt.value !== null && state.value === 'LIVE') {
      if (performance.now() - lastFrameAt > STALL_MS) stalled.value = true
    } else if (performance.now() - watchStartedAt > FIRST_FRAME_TIMEOUT_MS) {
      // 传输已建立但始终没有可解码画面：不得停留在“连接中”，按失败处理
      onNoFirstFrame(epoch)
    }
  }, 500)
}

function onNoFirstFrame(epoch: number): void {
  if (epoch !== sessionEpoch || disposed) return
  const streamId = props.stream?.stream_id
  const wasLive = hadLiveSession
  stopFrameWatch()
  peer?.close()
  peer = null
  if (video.value) video.value.srcObject = null
  if (wasLive && streamId) {
    state.value = 'CONNECTING'
    detail.value = '未收到画面，正在重试…'
    hadLiveSession = true
    scheduleReconnect(epoch, streamId)
  } else {
    state.value = 'ERROR'
    detail.value = `已连接但 ${FIRST_FRAME_TIMEOUT_MS / 1000}s 内未收到画面`
  }
}

function stopFrameWatch(): void {
  watchToken += 1
  const fv = watchedVideo as FrameVideo | null
  if (fv && frameHandle !== undefined && typeof fv.cancelVideoFrameCallback === 'function') {
    fv.cancelVideoFrameCallback(frameHandle)
  }
  frameHandle = undefined
  watchedVideo = null
  watchEpoch = -1
  window.clearInterval(stallTimer)
  stallTimer = undefined
  stalled.value = false
  firstFrameAt.value = null
  lastMediaTime = -1
}

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
  stopFrameWatch()
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
      if (video.value) {
        video.value.srcObject = event.streams[0]
        watchFrames(video.value, epoch) // 首帧到达前不置 LIVE
      }
      if (pc.connectionState === 'connected') detail.value = '已连接，等待首帧…'
    }
    pc.onconnectionstatechange = () => {
      if (!valid()) return
      if (pc.connectionState === 'connected' && video.value?.srcObject) {
        if (firstFrameAt.value === null) detail.value = '已连接，等待首帧…'
        watchFrames(video.value, epoch)
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
      <!-- 首帧门控：CONNECTING 期间 video 需在布局中（opacity 0）才会解码并触发 rVFC；
           display:none 的 video 在 Chromium 不渲染帧，会使“首帧到达”永不发生。 -->
      <video
        v-show="state === 'LIVE' || state === 'CONNECTING'"
        ref="video"
        :class="{ 'video-pending-frame': state !== 'LIVE' }"
        :data-first-frame="firstFrameAt !== null ? 'yes' : 'no'"
        autoplay
        muted
        playsinline
      />
      <div v-if="state === 'LIVE'" class="video-time">
        {{
          new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(
            now,
          )
        }}
      </div>
      <div v-if="state === 'LIVE' && !stalled" class="video-live-tag">{{ streamStateLabel(state) }}</div>
      <div v-if="state === 'LIVE' && stalled" class="video-stalled" data-video-stalled role="status">
        画面中断（{{ STALL_MS / 1000 }}s 无新帧），等待恢复…
      </div>
      <button
        v-if="state === 'LIVE'"
        class="video-fullscreen"
        type="button"
        aria-label="全屏"
        @click="enterFullscreen"
      >
        <FullscreenIcon />
      </button>
      <div
        v-if="state !== 'LIVE'"
        class="video-placeholder"
        :class="{ 'video-placeholder-overlay': state === 'CONNECTING' }"
      >
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
