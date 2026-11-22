import { i18n } from '@/i18n'
import { ApiError } from '@/api/client'

/**
 * 把接口错误转成用户可理解的中文提示。
 *
 * 后端错误带稳定的机器码 `code`，`errors` 文案表覆盖已知错误码。未知错误码
 * （旧服务端、尚未收录的新码）一律落到通用中文提示——服务端的英文原文可能
 * 包含表名、路径、异常类型等内部信息，不再直接展示给用户。
 */
export function translateApiError(e: unknown): string {
  if (e instanceof ApiError && e.code && i18n.global.te(`errors.${e.code}`)) {
    return i18n.global.t(`errors.${e.code}`, { detail: e.message })
  }
  return i18n.global.t('errors.UNKNOWN')
}

/** translateApiError 的非 Error 兜底版本。 */
export function translateApiErrorWith(e: unknown, fallback: string): string {
  if (e instanceof Error) return translateApiError(e)
  return fallback
}

/** 判断文本是否已经是中文（避免把服务端英文原文直接透给用户）。 */
function hasCJK(text: string): boolean {
  return /[\u4e00-\u9fff]/.test(text)
}

/**
 * WS/SSE `{code?, message}` 帧的提示文案。
 *
 * 已收录的错误码走文案表；无码或未收录时，只有本身就是中文的消息才原样展示，
 * 否则回落到通用提示，避免英文内部描述泄露到界面。
 */
export function translateErrorCode(code: unknown, message: unknown): string {
  const raw = typeof message === 'string' && message ? message : ''
  if (typeof code === 'string' && code && i18n.global.te(`errors.${code}`)) {
    return i18n.global.t(`errors.${code}`, { detail: raw })
  }
  if (raw && hasCJK(raw)) return raw
  return i18n.global.t('errors.UNKNOWN')
}
