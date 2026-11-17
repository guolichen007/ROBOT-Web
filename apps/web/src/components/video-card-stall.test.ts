// G3：生产 VideoCard 画面停滞看门狗——源断流后 WebRTC 仍 connected 的窗口内不得继续宣称「实时」。
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import VideoCard from './VideoCard.vue'
import { api } from '@/lib/api'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: vi.fn(), post: vi.fn() },
}))

class FakePeer {
  static last: FakePeer
  connectionState = 'new'
  iceGatheringState = 'complete'
  localDescription = { sdp: 'v=0' }
  ontrack: ((event: unknown) => void) | null = null
  onconnectionstatechange: (() => void) | null = null
  constructor() {
    FakePeer.last = this
  }
  addTransceiver() {}
  async createOffer() {
    return { type: 'offer', sdp: 'v=0' }
  }
  async setLocalDescription() {}
  async setRemoteDescription() {}
  addEventListener() {}
  removeEventListener() {}
  close() {
    this.connectionState = 'closed'
  }
}

async function flush() {
  for (let i = 0; i < 6; i += 1) await Promise.resolve()
  await nextTick()
}

describe('VideoCard 画面停滞看门狗', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.stubGlobal('RTCPeerConnection', FakePeer)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 201,
        headers: new Headers({ Location: '/media/s/whep/session' }),
        text: async () => 'v=0',
      }),
    )
    vi.mocked(api.post).mockResolvedValue({
      data: { ticket: 't', playback_url: '/media/s/whep' },
    })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('连接后无新帧超过 3s → 隐藏「实时」并显示画面中断；新帧到达自动恢复', async () => {
    const wrapper = mount(VideoCard, {
      props: { title: 'RGB', stream: { stream_id: 's', state: 'ONLINE', camera_type: 'roof_rgb' } as never },
      global: { stubs: { 't-button': { template: '<button @click="$emit(\'click\')"><slot /></button>' } } },
    })
    const video = wrapper.find('video').element as unknown as {
      requestVideoFrameCallback: (cb: () => void) => number
    }
    const frames: { next: () => void } = { next: () => {} }
    video.requestVideoFrameCallback = (cb) => {
      frames.next = cb
      return 1
    }
    await wrapper.find('button').trigger('click')
    await flush()
    const peer = FakePeer.last
    peer.connectionState = 'connected'
    peer.ontrack?.({ streams: [{}] })
    await flush()
    expect(wrapper.find('.video-live-tag').exists()).toBe(true)
    // 1.5s 内有帧 → 仍实时
    vi.advanceTimersByTime(1500)
    frames.next()
    vi.advanceTimersByTime(1000)
    await nextTick()
    expect(wrapper.find('[data-video-stalled]').exists()).toBe(false)
    // 断流：4s 无帧 → 停滞
    vi.advanceTimersByTime(4000)
    await nextTick()
    expect(wrapper.find('.video-live-tag').exists()).toBe(false)
    expect(wrapper.find('[data-video-stalled]').text()).toContain('画面中断')
    // 恢复：新帧 → 立即回到实时
    frames.next()
    await nextTick()
    expect(wrapper.find('[data-video-stalled]').exists()).toBe(false)
    expect(wrapper.find('.video-live-tag').exists()).toBe(true)
    wrapper.unmount()
  })
})
