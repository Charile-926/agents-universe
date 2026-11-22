/**
 * 单语言中文文案目录。
 *
 * 项目已移除中英文切换：这里不再初始化 i18n 框架、不加载第二语言包、不读写
 * 语言状态。`t()` 只是一次静态字典查找 + `{name}` 插值，因此没有语言竞态，
 * 也不会出现“切换语言后局部文案不刷新”的问题。
 *
 * 保留 `t/te` 与 `useT()` 两个入口，是为了让页面代码的调用点保持可读：
 * 文案集中在 `locales/zh-CN.ts` 维护，而不是散落在几百个模板里。
 */
import zhCN from './locales/zh-CN'

type MessageTree = { [key: string]: string | MessageTree }

const messages = zhCN as unknown as MessageTree

function lookup(path: string): string | undefined {
  const parts = path.split('.')
  let node: string | MessageTree | undefined = messages
  for (const part of parts) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as MessageTree)[part]
  }
  return typeof node === 'string' ? node : undefined
}

/** 是否存在该文案 key（错误码映射用它判断是否已覆盖）。 */
export function te(key: string): boolean {
  return lookup(key) !== undefined
}

/**
 * 取中文文案。缺失的 key 在开发环境打印一次警告并原样返回 key，
 * 便于定位漏配文案；生产环境返回 key，不会抛错打断渲染。
 */
export function t(
  key: string,
  params?: Record<string, unknown> | unknown[],
): string {
  const template = lookup(key)
  if (template === undefined) {
    if (import.meta.env?.DEV) {
      console.warn(`[copy] 缺少文案: ${key}`)
    }
    return key
  }
  // 文案里用 vue-i18n 的转义写法 `{'@'}` 表示字面量（`@` 在旧引擎里有特殊
  // 含义）。单语言实现只需还原成字面量。
  const literal = template.replace(/\{'([^']*)'\}/g, '$1')
  if (!params || Array.isArray(params)) return literal
  return literal.replace(/\{(\w+)\}/g, (match, name: string) => {
    const value = (params as Record<string, unknown>)[name]
    return value === undefined || value === null ? match : String(value)
  })
}

/** 组件内使用：`const { t } = useT()`。 */
export function useT() {
  return { t, te }
}

/**
 * 兼容旧调用点（`i18n.global.t` / `plugins: [i18n]`）。没有语言状态，
 * install 是空操作。
 */
export const i18n = {
  install() {
    /* 单语言，无需注册任何插件能力 */
  },
  global: { t, te },
}

/** 文档语言固定为简体中文。 */
if (typeof document !== 'undefined') {
  document.documentElement.lang = 'zh-CN'
}

export type { MessageTree }
