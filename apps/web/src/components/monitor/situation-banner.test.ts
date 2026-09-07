import { mount } from '@vue/test-utils'
import { computed, defineComponent, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import SituationBanner from './SituationBanner.vue'

// 复现 MonitorView 的普通稳定 DOM 条件渲染结构（已移除 Teleport）：
// host div 永远存在，SituationBanner 在其内部条件挂载。
const Host = defineComponent({
  components: { SituationBanner },
  setup() {
    const state = ref('OFFLINE_UNKNOWN')
    const show = computed(() => state.value !== 'NORMAL')
    return { state, show }
  },
  template: `
    <div class="monitor-situation-host">
      <SituationBanner v-if="show" :state="state" />
    </div>
  `,
})

describe('SituationBanner conditional render stability', () => {
  it('keeps host stable across OFF → NORMAL → OFF without emitsOptions', async () => {
    const appErrors: unknown[] = []
    const consoleErrors: string[] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      consoleErrors.push(args.map((a) => String(a)).join(' '))
    })

    const wrapper = mount(Host, {
      global: {
        config: {
          errorHandler(err: unknown) {
            appErrors.push(err)
          },
        },
      },
    })

    const host = () => wrapper.find('.monitor-situation-host')
    const banner = () => wrapper.find('.situation-banner')

    // OFFLINE_UNKNOWN：host 存在 + banner 可见
    expect(host().exists()).toBe(true)
    expect(banner().exists()).toBe(true)

    // → NORMAL：host 仍存在 + banner 消失
    ;(wrapper.vm as unknown as { state: string }).state = 'NORMAL'
    await nextTick()
    await nextTick()
    expect(host().exists()).toBe(true)
    expect(banner().exists()).toBe(false)

    // → 回 OFF：host 同一结构 + banner 恢复
    ;(wrapper.vm as unknown as { state: string }).state = 'OFFLINE_UNKNOWN'
    await nextTick()
    await nextTick()
    expect(host().exists()).toBe(true)
    expect(banner().exists()).toBe(true)

    const fatal = [...appErrors, ...consoleErrors].filter((e) =>
      /emitsOptions|Cannot read properties of null/.test(String(e)),
    )
    expect(fatal).toEqual([])
    spy.mockRestore()
  })
})
