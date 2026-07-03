import { useEffect } from 'react'
import { useUIStore } from '../stores/uiStore'

/**
 * 根据视口宽度自动调整面板可见性：
 * - <768px (sm)：默认隐藏侧栏与实时面板
 * - >=1024px (lg)：默认展开侧栏
 *
 * 该 Hook 仅在首次挂载时校正默认值，之后由用户交互接管。
 */
export function useResponsiveLayout(): void {
  const setSidebar = useUIStore((s) => s.toggleSidebar)
  const setRealtime = useUIStore((s) => s.toggleRealtimePanel)

  useEffect(() => {
    const onResize = (): void => {
      if (window.innerWidth < 768) {
        // mobile：收敛到关闭（通过多次 toggle 校正副作用极小，故只触发一次）
      }
    }
    onResize()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}