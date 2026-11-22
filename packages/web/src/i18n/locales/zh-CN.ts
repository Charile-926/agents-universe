/**
 * 中文文案目录（单语言）。
 *
 * 平台已移除语言切换与翻译功能，这里只保留当前界面真正用到的文案：
 * 登录、外壳、炼化工作台，以及错误码到中文提示的映射。
 */
export default {
  common: {
    user: '用户',
    loading: '加载中…',
  },
  auth: {
    title: 'Agents Universe',
    loginSub: '使用账号与密码登录',
    registerSub: '创建账号以开始使用',
    username: '用户名',
    usernamePlaceholder: '请输入用户名（至少 3 位）',
    password: '密码',
    passwordPlaceholder: '请输入密码（至少 6 位）',
    displayName: '昵称（可选）',
    displayNamePlaceholder: '默认使用用户名',
    submitLogin: '登录',
    submitRegister: '注册并登录',
    submitting: '请稍候…',
    switchToRegister: '没有账号？注册',
    switchToLogin: '已有账号？登录',
  },
  shell: {
    logout: '退出登录',
  },
  skillDistill: {
    title: '炼化 Skill',
    subtitle: '把当前账号自己的聊天记录与工具调用记录，汇总成一份可复用的 Skill。',
    sources: '选择记录',
    selectAllPage: '全选本页',
    clearSelection: '清空选择',
    useAll: '汇总全部会话',
    useAllHint: '按最近更新时间取最近 50 个会话，超出部分自动截断。',
    includeTools: '包含工具调用记录',
    searchPlaceholder: '搜索会话标题',
    empty: '当前账号还没有可炼化的聊天记录。',
    loadMore: '加载更多',
    selectedCount: '已选 {count} 个会话',
    generate: '生成 Skill',
    generating: '正在生成…',
    preview: '预览',
    previewEmpty: '生成后会在这里显示 Skill 草稿。',
    copy: '复制',
    copied: '已复制',
    download: '下载 .md',
    stats: '汇总了 {conversations} 个会话、{messages} 条消息、{toolCalls} 次工具调用。',
    truncated: '数据量较大，已按上限分批读取（每个会话最近 {limit} 条消息）。',
    failed: '生成失败，请稍后重试。',
    loadFailed: '会话列表加载失败。',
    noSelection: '请先选择会话，或勾选“汇总全部会话”。',
    toolCallsUnit: '{count} 次工具调用',
  },
  errors: {
    // 通用
    UNKNOWN: '操作失败，请稍后重试。',
    INTERNAL_ERROR: '服务器内部错误，请稍后重试。',
    REQUEST_INVALID: '请求参数不合法，请检查后重试。',
    // 认证
    AUTH_REQUIRED: '请先登录。',
    AUTH_SESSION_EXPIRED: '登录已过期，请重新登录。',
    AUTH_SESSION_BACKEND_UNAVAILABLE: '认证服务暂时不可用，请稍后重试。',
    AUTH_CROSS_SITE_LOGOUT: '跨站请求已被拒绝，请在原页面操作。',
    AUTH_INVALID_CREDENTIALS: '用户名或密码错误。',
    AUTH_USERNAME_TAKEN: '该用户名已被注册。',
    AUTH_USERNAME_REQUIRED: '用户名不能为空。',
    // 炼化 Skill
    DISTILL_NO_SOURCE: '请先选择要炼化的会话。',
    DISTILL_NO_HISTORY: '当前账号还没有可用于炼化的聊天记录。',
    DISTILL_SOURCE_NOT_FOUND: '部分会话不存在或无权访问，请刷新列表后重试。',
  },
}
