<template>
  <div class="distill-panel">
    <!-- 左列：选择来源 -->
    <section class="distill-col">
      <div class="distill-section">
        <div class="distill-section-head">
          <span class="distill-label">{{ t('skillDistill.sources') }}</span>
          <span v-if="selected.size" class="distill-count">
            {{ t('skillDistill.selectedCount', { count: selected.size }) }}
          </span>
        </div>

        <label class="distill-check distill-check--strong">
          <input v-model="useAll" type="checkbox" @change="onUseAllChange" />
          <span>{{ t('skillDistill.useAll') }}</span>
        </label>
        <p v-if="useAll" class="distill-hint">{{ t('skillDistill.useAllHint') }}</p>

        <template v-if="!useAll">
          <input
            v-model="keyword"
            class="distill-search"
            type="search"
            :placeholder="t('skillDistill.searchPlaceholder')"
            @keyup.enter="reload"
          />
          <div class="distill-list">
            <p v-if="loading" class="distill-hint">{{ t('common.loading') }}</p>
            <p v-else-if="sources.length === 0" class="distill-hint">{{ t('skillDistill.empty') }}</p>
            <label v-for="item in sources" :key="item.conversation_id" class="distill-check">
              <input
                type="checkbox"
                :checked="selected.has(item.conversation_id)"
                @change="toggle(item.conversation_id)"
              />
              <span class="distill-item">
                <span class="distill-item-title">{{ item.title }}</span>
                <span class="distill-item-meta">
                  {{ item.project_name }} · {{ item.message_count }} 条消息
                  <template v-if="item.tool_call_count > 0">
                    · {{ t('skillDistill.toolCallsUnit', { count: item.tool_call_count }) }}
                  </template>
                </span>
              </span>
            </label>
          </div>
          <div class="distill-list-actions">
            <button class="distill-link" type="button" @click="selectPage">
              {{ t('skillDistill.selectAllPage') }}
            </button>
            <button class="distill-link" type="button" @click="selected.clear()">
              {{ t('skillDistill.clearSelection') }}
            </button>
            <button
              v-if="hasMore"
              class="distill-link"
              type="button"
              :disabled="loadingMore"
              @click="loadMore"
            >
              {{ loadingMore ? t('common.loading') : t('skillDistill.loadMore') }}
            </button>
          </div>
        </template>

        <label class="distill-check">
          <input v-model="includeTools" type="checkbox" />
          <span>{{ t('skillDistill.includeTools') }}</span>
        </label>
      </div>

      <button
        class="btn-primary distill-generate"
        :disabled="generating || !canGenerate"
        @click="generate"
      >
        {{ generating ? t('skillDistill.generating') : t('skillDistill.generate') }}
      </button>

      <p v-if="error" class="distill-error">{{ error }}</p>
    </section>

    <!-- 右列：预览 -->
    <section class="distill-col distill-col--preview">
      <div class="distill-section distill-preview-section">
        <div class="distill-section-head">
          <span class="distill-label">{{ t('skillDistill.preview') }}</span>
          <span v-if="result" class="distill-actions">
            <button class="distill-link" type="button" @click="copy">
              {{ copied ? t('skillDistill.copied') : t('skillDistill.copy') }}
            </button>
            <button class="distill-link" type="button" @click="download">
              {{ t('skillDistill.download') }}
            </button>
          </span>
        </div>
        <p v-if="result" class="distill-hint">
          {{ t('skillDistill.stats', {
            conversations: result.stats.conversations,
            messages: result.stats.messages,
            toolCalls: result.stats.tool_calls,
          }) }}
        </p>
        <p v-if="result?.stats.truncated" class="distill-hint">
          {{ t('skillDistill.truncated', { limit: result.stats.message_limit_per_conversation ?? 0 }) }}
        </p>
        <pre v-if="result" class="distill-output">{{ result.markdown }}</pre>
        <p v-else class="distill-hint">{{ t('skillDistill.previewEmpty') }}</p>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useT } from '@/i18n'
import { skillsDistillApi, type DistillResult, type DistillSource } from '@/api/skillsDistill'
import { translateApiError } from '@/utils/apiError'

const { t } = useT()

const PAGE_SIZE = 20

const sources = ref<DistillSource[]>([])
const total = ref(0)
const offset = ref(0)
const loading = ref(false)
const loadingMore = ref(false)
const keyword = ref('')
const selected = ref<Set<string>>(new Set())
const useAll = ref(false)
const includeTools = ref(true)
const generating = ref(false)
const result = ref<DistillResult | null>(null)
const error = ref('')
const copied = ref(false)

const hasMore = computed(() => sources.value.length < total.value)
const canGenerate = computed(() => useAll.value || selected.value.size > 0)

async function load(reset: boolean) {
  const isReset = reset
  if (isReset) {
    offset.value = 0
    loading.value = true
  } else {
    loadingMore.value = true
  }
  error.value = ''
  try {
    const page = await skillsDistillApi.listSources({
      limit: PAGE_SIZE,
      offset: offset.value,
      q: keyword.value.trim() || undefined,
    })
    total.value = page.total
    sources.value = isReset ? page.items : [...sources.value, ...page.items]
    offset.value = sources.value.length
  } catch (e) {
    error.value = translateApiError(e) || t('skillDistill.loadFailed')
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function reload() {
  selected.value = new Set()
  void load(true)
}

function loadMore() {
  void load(false)
}

function toggle(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selected.value = next
}

function selectPage() {
  selected.value = new Set([...selected.value, ...sources.value.map((s) => s.conversation_id)])
}

function onUseAllChange() {
  if (useAll.value) selected.value = new Set()
}

async function generate() {
  if (generating.value) return
  error.value = ''
  if (!canGenerate.value) {
    error.value = t('skillDistill.noSelection')
    return
  }
  generating.value = true
  try {
    result.value = await skillsDistillApi.generate({
      conversation_ids: useAll.value ? [] : [...selected.value],
      all_conversations: useAll.value,
      include_tool_calls: includeTools.value,
    })
    copied.value = false
  } catch (e) {
    error.value = translateApiError(e) || t('skillDistill.failed')
  } finally {
    generating.value = false
  }
}

async function copy() {
  if (!result.value) return
  try {
    await navigator.clipboard.writeText(result.value.markdown)
    copied.value = true
    setTimeout(() => { copied.value = false }, 1500)
  } catch {
    error.value = t('skillDistill.failed')
  }
}

function download() {
  if (!result.value) return
  const slug = /slug:\s*"([^"]+)"/.exec(result.value.markdown)?.[1] ?? 'distilled-skill'
  const blob = new Blob([result.value.markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${slug}.md`
  a.click()
  URL.revokeObjectURL(url)
}

onMounted(() => { void load(true) })
</script>

<style scoped>
.distill-panel {
  display: grid;
  grid-template-columns: minmax(320px, 400px) minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}

@media (max-width: 900px) {
  .distill-panel { grid-template-columns: 1fr; }
}

.distill-col {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}

.distill-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-primary);
}

.distill-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.distill-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
}

.distill-count {
  font-size: var(--text-2xs);
  color: var(--text-muted);
}

.distill-check {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  cursor: pointer;
  line-height: 1.45;
  font-size: var(--text-sm);
}

.distill-check--strong {
  font-weight: 600;
}

.distill-hint {
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.distill-search {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-size: var(--text-sm);
  outline: none;
}

.distill-search:focus {
  border-color: var(--border-strong);
}

.distill-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 46vh;
  overflow-y: auto;
  padding-right: 2px;
}

.distill-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.distill-item-title {
  font-size: var(--text-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.distill-item-meta {
  font-size: var(--text-2xs);
  color: var(--text-muted);
}

.distill-list-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.distill-link {
  border: none;
  background: none;
  padding: 0;
  color: var(--text-secondary);
  font-size: var(--text-2xs);
  text-decoration: underline;
  cursor: pointer;
}

.distill-link:disabled {
  opacity: 0.5;
  cursor: default;
}

.distill-generate {
  width: 100%;
}

.distill-error {
  margin: 0;
  color: var(--color-error);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.distill-preview-section {
  min-height: 320px;
}

.distill-actions {
  display: flex;
  gap: 12px;
}

.distill-output {
  margin: 0;
  padding: 12px;
  border-radius: var(--radius-xs);
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: var(--text-xs);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 62vh;
  overflow: auto;
}
</style>
