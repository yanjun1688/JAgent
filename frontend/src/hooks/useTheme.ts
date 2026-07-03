import { useEffect } from 'react'
import { useUIStore } from '../stores/uiStore'

/**
 * 主题同步 Hook
 * ~~~~~~~~~~~~~~~~~~~~~~~~~
 * 订阅 uiStore.theme，将 `.light` 类应用到 <html> 元素上。
 * 由此驱动 CSS 变量切换并联动所有语义 Tailwind 颜色。
 *
 * 在 App 根组件挂载一次即可生效全局。
 */
export function useTheme(): void {
  const theme = useUIStore((s) => s.theme)

  useEffect(() => {
    const root = document.documentElement
    if (theme === 'light') {
      root.classList.add('light')
    } else {
      root.classList.remove('light')
    }
    // 同步 color-scheme，让浏览器原生控件(滚动条/表单)随之适配
    root.style.colorScheme = theme === 'light' ? 'light' : 'dark'
  }, [theme])
}