import { apiFetch } from './client'

export interface DistillSource {
  conversation_id: string
  title: string
  project_name: string
  updated_at: string
  message_count: number
  tool_call_count: number
}

export interface DistillSourcesPage {
  items: DistillSource[]
  total: number
  limit: number
  offset: number
}

export interface DistillStats {
  conversations: number
  messages: number
  tool_calls: number
  tools: string[]
  truncated: boolean
  message_limit_per_conversation?: number
}

export interface DistillResult {
  markdown: string
  stats: DistillStats
}

export interface DistillRequest {
  conversation_ids?: string[]
  all_conversations?: boolean
  include_tool_calls?: boolean
}

export const skillsDistillApi = {
  listSources: (params: { limit?: number; offset?: number; q?: string } = {}) => {
    const qs = new URLSearchParams()
    qs.set('limit', String(params.limit ?? 20))
    qs.set('offset', String(params.offset ?? 0))
    if (params.q) qs.set('q', params.q)
    return apiFetch<DistillSourcesPage>(`/api/skills/distill/sources?${qs.toString()}`)
  },
  generate: (body: DistillRequest) =>
    apiFetch<DistillResult>('/api/skills/distill/generate', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}
