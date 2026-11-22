import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import { useConversationStore } from '@/stores/conversation'
import { useWebSocket } from '@/composables/useWebSocket'

// ── Fake WebSocket to drive useWebSocket._dispatch from tests ──

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  readyState = 0
  onopen: ((e: unknown) => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e: unknown) => void) | null = null
  sent: string[] = []

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3 }
  emit(payload: unknown) { this.onmessage?.({ data: JSON.stringify(payload) }) }
}

function lastSocket(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1)
  if (!ws) throw new Error('no WebSocket created')
  return ws
}

describe('websocket warning events', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders a warning as an error-styled message', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    useWebSocket(ref('c1'))
    lastSocket().emit({ type: 'warning', message: '已用完本轮循环预算' })

    const msg = store.messages.at(-1)
    expect(msg?.role).toBe('assistant')
    expect(msg?.isError).toBe(true)
    // 中文提示原样展示（服务端英文内部描述则回落通用提示）。
    expect(msg?.content).toBe('已用完本轮循环预算')
  })

  it('does not clear the streaming state (the run may still be alive)', () => {
    const store = useConversationStore()
    store.startConversation('c1')
    store.appendDelta('partial answer', undefined, 'c1')
    expect(store.isStreaming).toBe(true)

    useWebSocket(ref('c1'))
    // Mid-run warning, e.g. history compression timed out.
    lastSocket().emit({ type: 'warning', message: '历史压缩超时，仅保留最近消息' })

    // The turn keeps streaming — the warning is informational only.
    expect(store.isStreaming).toBe(true)
    expect(store.messages.at(-1)?.content).toContain('历史压缩超时')
  })

  it('falls back to a generic label when the message field is missing', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    useWebSocket(ref('c1'))
    lastSocket().emit({ type: 'warning' })

    expect(store.messages.at(-1)?.content).toBe('操作失败，请稍后重试。')
  })

  it('does not leak a server-side English description', () => {
    const store = useConversationStore()
    store.startConversation('c1')

    useWebSocket(ref('c1'))
    lastSocket().emit({ type: 'warning', message: 'Reached maximum loop iterations' })

    expect(store.messages.at(-1)?.content).toBe('操作失败，请稍后重试。')
  })
})
