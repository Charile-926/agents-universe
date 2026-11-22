import type { LocaleOverrides } from '@/types'

/**
 * Resolve a canonical value against the overrides of the single supported
 * locale (简体中文).
 *
 * 平台已移除语言切换，overrides 里只可能命中 zh-CN 那一份；canonical 本身
 * 就是中文，因此直接取 canonical，命中时再用 overrides 覆盖。
 */
const LOCALE = 'zh-CN'

export function resolveLocalized<T>(
  canonical: T | null | undefined,
  overrides: LocaleOverrides<T> | null | undefined,
): T | undefined {
  if (!overrides) return canonical ?? undefined
  return (overrides as Record<string, T | undefined>)[LOCALE] ?? canonical ?? undefined
}

/** String variant of resolveLocalized with an explicit fallback. */
export function localizedText(
  canonical: string | null | undefined,
  overrides: LocaleOverrides<string> | null | undefined,
  fallback = '',
): string {
  return resolveLocalized(canonical, overrides) ?? fallback
}

/**
 * Search text across every language of a field plus the canonical value.
 *
 * The picker filter must match what a user types in EITHER language — an
 * en-US user searching "Tech Lead" has to find the agent whose canonical
 * display_name is 技术负责人.
 */
export function searchBlob(
  canonical: string | null | undefined,
  overrides: LocaleOverrides<string> | null | undefined,
): string {
  const parts: string[] = [canonical ?? '']
  if (overrides) parts.push(...Object.values(overrides))
  return parts.join('\n').toLowerCase()
}
