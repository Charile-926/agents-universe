import './test-env'
import { config } from '@vue/test-utils'
import { i18n } from './i18n/index'

// 组件内使用 useT()；这里注册的是空操作插件，保持旧测试的 mount 方式可用。
config.global.plugins = [i18n]
