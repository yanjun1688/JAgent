# Harness 前端开发计划

> **版本**: v1.0
> **架构师**: AgentX
> **日期**: 2026-07-22
> **关联文档**: `FRONTEND_ARCHITECTURE_v1.0.md`

---

## 1. 项目初始化

### 1.1 依赖安装

```bash
cd frontend

# 核心依赖
npm install zustand @tanstack/react-query

# 动效
npm install motion

# 3D 可视化
npm install three @react-three/fiber @react-three/drei @react-three/postprocessing
npm install @types/three

# 粒子系统
npm install @tsparticles/react @tsparticles/slim

# UI 基础
npm install lucide-react class-variance-authority clsx tailwind-merge

# 开发依赖
npm install -D tailwindcss @tailwindcss/typography
npm install -D @testing-library/jest-dom @testing-library/react @testing-library/user-event
```

### 1.2 Tailwind 配置

```typescript
// tailwind.config.ts
import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        background: {
          primary: '#0A0A0F',
          secondary: '#12121A',
          tertiary: '#1A1A2E',
        },
        accent: {
          primary: '#6366F1',
          secondary: '#A855F7',
          tertiary: '#EC4899',
          quaternary: '#10B981',
        },
        status: {
          success: '#10B981',
          warning: '#F59E0B',
          error: '#EF4444',
          info: '#3B82F6',
        },
        text: {
          primary: '#F8FAFC',
          secondary: '#94A3B8',
          tertiary: '#64748B',
          muted: '#475569',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        display: ['Space Grotesk', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      backdropBlur: {
        xs: '2px',
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
} satisfies Config
```

### 1.3 CSS 全局样式

```css
/* src/index.css */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  body {
    @apply bg-background-primary text-text-primary font-sans antialiased;
  }
}

@layer components {
  /* 玻璃质感 */
  .glass-base {
    @apply bg-white/3 backdrop-blur-xl backdrop-saturate-180 border border-white/8;
  }
  
  .glass-elevated {
    @apply bg-white/6 backdrop-blur-2xl backdrop-saturate-200 border border-white/12;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.1);
  }
  
  .glass-frosted {
    @apply bg-background-primary/70 backdrop-blur-3xl backdrop-saturate-150;
  }
  
  /* 渐变 */
  .gradient-nebula {
    background: linear-gradient(135deg, #6366F1 0%, #A855F7 50%, #EC4899 100%);
  }
  
  .gradient-aurora {
    background: 
      radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.15) 0%, transparent 50%),
      radial-gradient(at 100% 0%, rgba(168, 85, 247, 0.15) 0%, transparent 50%),
      radial-gradient(at 100% 100%, rgba(236, 72, 153, 0.1) 0%, transparent 50%),
      radial-gradient(at 0% 100%, rgba(16, 185, 129, 0.1) 0%, transparent 50%);
    animation: aurora-shift 20s ease infinite;
  }
  
  /* 阴影 */
  .shadow-nebula {
    box-shadow: 
      0 0 0 1px rgba(99, 102, 241, 0.1),
      0 4px 16px rgba(99, 102, 241, 0.15),
      0 8px 32px rgba(0, 0, 0, 0.4);
  }
  
  .shadow-glow {
    box-shadow: 
      0 0 20px rgba(99, 102, 241, 0.3),
      0 0 40px rgba(99, 102, 241, 0.1);
  }
}

@layer utilities {
  /* 动效偏好 */
  @media (prefers-reduced-motion: reduce) {
    * {
      animation-duration: 0.01ms !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.01ms !important;
    }
    
    .particle-system { display: none; }
    .gradient-aurora { animation: none; }
  }
}

@keyframes aurora-shift {
  0%, 100% { background-position: 0% 50%; }
  50% { background-position: 100% 50%; }
}
```

---

## 2. 设计系统实现

### 2.1 设计 Token

```typescript
// src/design-system/tokens/colors.ts
export const colors = {
  background: {
    primary: '#0A0A0F',
    secondary: '#12121A',
    tertiary: '#1A1A2E',
  },
  accent: {
    primary: '#6366F1',
    secondary: '#A855F7',
    tertiary: '#EC4899',
    quaternary: '#10B981',
  },
  status: {
    success: '#10B981',
    warning: '#F59E0B',
    error: '#EF4444',
    info: '#3B82F6',
  },
  text: {
    primary: '#F8FAFC',
    secondary: '#94A3B8',
    tertiary: '#64748B',
    muted: '#475569',
  },
  white: {
    DEFAULT: '#FFFFFF',
    '5': 'rgba(255, 255, 255, 0.05)',
    '10': 'rgba(255, 255, 255, 0.1)',
    '20': 'rgba(255, 255, 255, 0.2)',
  },
} as const

export type ColorToken = typeof colors
```

```typescript
// src/design-system/tokens/spacing.ts
export const spacing = {
  xs: '4px',
  sm: '8px',
  md: '16px',
  lg: '24px',
  xl: '32px',
  xxl: '48px',
  xxxl: '64px',
} as const

export type SpacingToken = typeof spacing
```

```typescript
// src/design-system/tokens/radii.ts
export const radii = {
  none: '0px',
  sm: '8px',
  md: '12px',
  lg: '16px',
  xl: '24px',
  full: '9999px',
} as const

export type RadiusToken = typeof radii
```

```typescript
// src/design-system/tokens/shadows.ts
export const shadows = {
  nebula: '0 0 0 1px rgba(99, 102, 241, 0.1), 0 4px 16px rgba(99, 102, 241, 0.15), 0 8px 32px rgba(0, 0, 0, 0.4)',
  glow: '0 0 20px rgba(99, 102, 241, 0.3), 0 0 40px rgba(99, 102, 241, 0.1)',
  float: '0 20px 60px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.05)',
  sm: '0 1px 2px rgba(0, 0, 0, 0.3)',
  md: '0 4px 8px rgba(0, 0, 0, 0.3)',
  lg: '0 8px 16px rgba(0, 0, 0, 0.4)',
} as const

export type ShadowToken = typeof shadows
```

```typescript
// src/design-system/tokens/typography.ts
export const typography = {
  fontFamily: {
    sans: "'Inter', system-ui, sans-serif",
    display: "'Space Grotesk', sans-serif",
    mono: "'JetBrains Mono', 'Fira Code', monospace",
  },
  fontSize: {
    xs: '12px',
    sm: '14px',
    md: '16px',
    lg: '18px',
    xl: '20px',
    xxl: '24px',
    xxxl: '32px',
  },
  fontWeight: {
    light: 300,
    regular: 400,
    medium: 500,
    semibold: 600,
    bold: 700,
  },
  lineHeight: {
    tight: 1.25,
    normal: 1.5,
    relaxed: 1.75,
  },
} as const

export type TypographyToken = typeof typography
```

```typescript
// src/design-system/tokens/index.ts
export { colors } from './colors'
export { spacing } from './spacing'
export { radii } from './radii'
export { shadows } from './shadows'
export { typography } from './typography'
```

### 2.2 工具函数

```typescript
// src/design-system/utils/cn.ts
import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
```

```typescript
// src/design-system/utils/motion.ts
import type { Variants } from 'motion/react'

export const fadeIn: Variants = {
  hidden: { opacity: 0 },
  visible: { opacity: 1, transition: { duration: 0.2 } },
  exit: { opacity: 0, transition: { duration: 0.15 } },
}

export const slideUp: Variants = {
  hidden: { opacity: 0, y: 20 },
  visible: { 
    opacity: 1, 
    y: 0, 
    transition: { type: 'spring', stiffness: 300, damping: 25 } 
  },
  exit: { opacity: 0, y: -10, transition: { duration: 0.2 } },
}

export const scaleIn: Variants = {
  hidden: { opacity: 0, scale: 0.95 },
  visible: { 
    opacity: 1, 
    scale: 1, 
    transition: { type: 'spring', stiffness: 300, damping: 25 } 
  },
  exit: { opacity: 0, scale: 0.95, transition: { duration: 0.2 } },
}

export const staggerContainer: Variants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: {
      staggerChildren: 0.05,
    },
  },
}
```

---

## 3. 基础 UI 组件

### 3.1 GlassCard

```typescript
// src/components/ui/GlassCard.tsx
import { motion, type HTMLMotionProps } from 'motion/react'
import { cn } from '../../design-system/utils/cn'

interface GlassCardProps extends HTMLMotionProps<'div'> {
  variant?: 'base' | 'elevated' | 'frosted'
  children: React.ReactNode
}

export function GlassCard({ 
  variant = 'base', 
  children, 
  className,
  ...props 
}: GlassCardProps) {
  return (
    <motion.div
      className={cn(
        'rounded-2xl',
        {
          'glass-base': variant === 'base',
          'glass-elevated': variant === 'elevated',
          'glass-frosted': variant === 'frosted',
        },
        className
      )}
      whileHover={{ scale: 1.01 }}
      transition={{ type: 'spring', stiffness: 300 }}
      {...props}
    >
      {children}
    </motion.div>
  )
}
```

### 3.2 GlowButton

```typescript
// src/components/ui/GlowButton.tsx
import { motion, type HTMLMotionProps } from 'motion/react'
import { cn } from '../../design-system/utils/cn'

interface GlowButtonProps extends HTMLMotionProps<'button'> {
  variant?: 'primary' | 'secondary' | 'danger'
  size?: 'sm' | 'md' | 'lg'
  children: React.ReactNode
}

const variants = {
  primary: 'bg-gradient-to-r from-accent-primary to-accent-secondary',
  secondary: 'glass-base hover:glass-elevated',
  danger: 'bg-status-error hover:bg-status-error/90',
}

const sizes = {
  sm: 'px-4 py-2 text-sm',
  md: 'px-6 py-3 text-base',
  lg: 'px-8 py-4 text-lg',
}

export function GlowButton({ 
  variant = 'primary', 
  size = 'md', 
  children, 
  className,
  ...props 
}: GlowButtonProps) {
  return (
    <motion.button
      className={cn(
        'rounded-xl font-medium text-white overflow-hidden relative',
        variants[variant],
        sizes[size],
        className
      )}
      whileHover={{ 
        scale: 1.05,
        boxShadow: '0 0 30px rgba(99, 102, 241, 0.5)',
      }}
      whileTap={{ scale: 0.95 }}
      {...props}
    >
      <span className="relative z-10">{children}</span>
      <motion.div
        className="absolute inset-0 opacity-30 bg-gradient-radial from-white to-transparent"
        animate={{
          scale: [1, 1.5, 1],
          opacity: [0.3, 0, 0.3],
        }}
        transition={{ duration: 2, repeat: Infinity }}
      />
    </motion.button>
  )
}
```

### 3.3 StatusBadge

```typescript
// src/components/ui/StatusBadge.tsx
import { cn } from '../../design-system/utils/cn'

type Status = 'running' | 'paused' | 'completed' | 'failed'

interface StatusBadgeProps {
  status: Status
  className?: string
}

const statusColors: Record<Status, string> = {
  running: 'bg-status-info text-white',
  paused: 'bg-status-warning text-white',
  completed: 'bg-status-success text-white',
  failed: 'bg-status-error text-white',
}

const statusLabels: Record<Status, string> = {
  running: '运行中',
  paused: '已暂停',
  completed: '已完成',
  failed: '已失败',
}

export function StatusBadge({ status, className }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium',
        statusColors[status],
        className
      )}
    >
      {statusLabels[status]}
    </span>
  )
}
```

```typescript
// src/components/ui/index.ts
export { GlassCard } from './GlassCard'
export { GlowButton } from './GlowButton'
export { StatusBadge } from './StatusBadge'
```

---

## 4. 状态管理实现

### 4.1 Conversation Store

```typescript
// src/stores/conversationStore.ts
import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import type { Conversation } from '../api/schema'

interface ConversationState {
  activeConversationId: string | null
  conversations: Conversation[]
  searchQuery: string
  
  setActiveConversation: (id: string | null) => void
  setConversations: (conversations: Conversation[]) => void
  setSearchQuery: (query: string) => void
  addConversation: (conversation: Conversation) => void
  removeConversation: (id: string) => void
  updateConversation: (id: string, updates: Partial<Conversation>) => void
}

export const useConversationStore = create<ConversationState>()(
  devtools(
    (set) => ({
      activeConversationId: null,
      conversations: [],
      searchQuery: '',
      
      setActiveConversation: (id) => set({ activeConversationId: id }),
      setConversations: (conversations) => set({ conversations }),
      setSearchQuery: (query) => set({ searchQuery: query }),
      addConversation: (conversation) => set((state) => ({
        conversations: [conversation, ...state.conversations]
      })),
      removeConversation: (id) => set((state) => ({
        conversations: state.conversations.filter(c => c.conversation_id !== id),
        activeConversationId: state.activeConversationId === id ? null : state.activeConversationId,
      })),
      updateConversation: (id, updates) => set((state) => ({
        conversations: state.conversations.map(c => 
          c.conversation_id === id ? { ...c, ...updates } : c
        ),
      })),
    }),
    { name: 'ConversationStore' }
  )
)
```

### 4.2 Run Store

```typescript
// src/stores/runStore.ts
import { create } from 'zustand'
import { devtools } from 'zustand/middleware'
import type { WsEvent, RunStatus } from '../api/schema'

interface RunState {
  activeRunId: string | null
  runStatus: RunStatus | null
  events: WsEvent[]
  isWebSocketConnected: boolean
  
  setActiveRun: (runId: string | null) => void
  setRunStatus: (status: RunStatus | null) => void
  addEvent: (event: WsEvent) => void
  setEvents: (events: WsEvent[]) => void
  setWebSocketConnected: (connected: boolean) => void
  clearRun: () => void
}

export const useRunStore = create<RunState>()(
  devtools(
    (set) => ({
      activeRunId: null,
      runStatus: null,
      events: [],
      isWebSocketConnected: false,
      
      setActiveRun: (runId) => set({ 
        activeRunId: runId, 
        events: [], 
        runStatus: null 
      }),
      setRunStatus: (status) => set({ runStatus: status }),
      addEvent: (event) => set((state) => ({
        events: [...state.events, event].sort((a, b) => a.seq - b.seq)
      })),
      setEvents: (events) => set({ events }),
      setWebSocketConnected: (connected) => set({ isWebSocketConnected: connected }),
      clearRun: () => set({ 
        activeRunId: null, 
        runStatus: null, 
        events: [] 
      }),
    }),
    { name: 'RunStore' }
  )
)
```

### 4.3 UI Store

```typescript
// src/stores/uiStore.ts
import { create } from 'zustand'
import { devtools } from 'zustand/middleware'

type Theme = 'nebula' | 'light'

interface UIState {
  sidebarOpen: boolean
  realtimePanelOpen: boolean
  expandedEventSeqs: Set<number>
  theme: Theme
  
  toggleSidebar: () => void
  toggleRealtimePanel: () => void
  toggleEventExpansion: (seq: number) => void
  setTheme: (theme: Theme) => void
}

export const useUIStore = create<UIState>()(
  devtools(
    (set) => ({
      sidebarOpen: true,
      realtimePanelOpen: true,
      expandedEventSeqs: new Set(),
      theme: 'nebula',
      
      toggleSidebar: () => set((state) => ({ 
        sidebarOpen: !state.sidebarOpen 
      })),
      toggleRealtimePanel: () => set((state) => ({ 
        realtimePanelOpen: !state.realtimePanelOpen 
      })),
      toggleEventExpansion: (seq) => set((state) => {
        const next = new Set(state.expandedEventSeqs)
        if (next.has(seq)) {
          next.delete(seq)
        } else {
          next.add(seq)
        }
        return { expandedEventSeqs: next }
      }),
      setTheme: (theme) => set({ theme }),
    }),
    { name: 'UIStore' }
  )
)
```

---

## 5. 路由配置

### 5.1 App.tsx

```typescript
// src/App.tsx
import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Header } from './components/Header'
import { LoadingScreen } from './components/LoadingScreen'

const ChatPage = lazy(() => import('./pages/ChatPage'))
const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const HistoryPage = lazy(() => import('./pages/HistoryPage'))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60 * 5,
      gcTime: 1000 * 60 * 30,
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
})

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="min-h-screen bg-background-primary">
          <Header />
          <Suspense fallback={<LoadingScreen />}>
            <Routes>
              <Route path="/" element={<ChatPage />} />
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/history" element={<HistoryPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  )
}

export default App
```

### 5.2 旧路由重定向

```typescript
// 在 App.tsx 中添加重定向路由
<Routes>
  {/* 新路由 */}
  <Route path="/" element={<ChatPage />} />
  <Route path="/overview" element={<OverviewPage />} />
  <Route path="/history" element={<HistoryPage />} />
  
  {/* 旧路由重定向 */}
  <Route path="/analysis" element={<Navigate to="/overview" replace />} />
  <Route path="/analysis/tools" element={<Navigate to="/overview" replace />} />
  <Route path="/analysis/guardrails" element={<Navigate to="/overview" replace />} />
  <Route path="/analysis/runs/:runId" element={<Navigate to="/history" replace />} />
  <Route path="/runs/:runId" element={<Navigate to="/history" replace />} />
  <Route path="/ops" element={<Navigate to="/overview" replace />} />
  <Route path="/ops/chat" element={<Navigate to="/" replace />} />
  <Route path="/ops/runs/:runId" element={<Navigate to="/history" replace />} />
  <Route path="/ops/system" element={<Navigate to="/overview" replace />} />
  
  <Route path="*" element={<Navigate to="/" replace />} />
</Routes>
```

---

## 6. 测试策略

### 6.1 单元测试

```typescript
// tests/components/ui/GlassCard.test.tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { GlassCard } from '../../src/components/ui/GlassCard'

describe('GlassCard', () => {
  it('renders children correctly', () => {
    render(<GlassCard>Test Content</GlassCard>)
    expect(screen.getByText('Test Content')).toBeInTheDocument()
  })
  
  it('applies variant classes', () => {
    const { container } = render(<GlassCard variant="elevated">Content</GlassCard>)
    expect(container.firstChild).toHaveClass('glass-elevated')
  })
  
  it('handles click events', () => {
    const onClick = vi.fn()
    render(<GlassCard onClick={onClick}>Click Me</GlassCard>)
    screen.getByText('Click Me').click()
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
```

### 6.2 集成测试

```typescript
// tests/pages/ChatPage.test.tsx
import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ChatPage } from '../../src/pages/ChatPage'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false },
  },
})

describe('ChatPage', () => {
  it('renders conversation sidebar', async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatPage />
      </QueryClientProvider>
    )
    
    await waitFor(() => {
      expect(screen.getByPlaceholderText('搜索对话...')).toBeInTheDocument()
    })
  })
  
  it('renders chat area', async () => {
    render(
      <QueryClientProvider client={queryClient}>
        <ChatPage />
      </QueryClientProvider>
    )
    
    await waitFor(() => {
      expect(screen.getByPlaceholderText('输入任务...')).toBeInTheDocument()
    })
  })
})
```

### 6.3 测试覆盖率目标

| 模块 | 目标覆盖率 |
|------|-----------|
| 基础 UI 组件 | 90%+ |
| 业务组件 | 80%+ |
| Hooks | 85%+ |
| Stores | 90%+ |
| Pages | 70%+ |

---

## 7. 部署检查清单

### 7.1 构建检查

- [ ] `npm run build` 成功
- [ ] 无 TypeScript 错误
- [ ] 无 ESLint 警告
- [ ] Bundle 大小 < 500KB (gzip)
- [ ] 3D 组件已懒加载

### 7.2 性能检查

- [ ] Lighthouse 性能得分 > 90
- [ ] 首屏加载 < 1s
- [ ] WebSocket 连接正常
- [ ] 动效流畅 (60fps)

### 7.3 兼容性检查

- [ ] Chrome/Firefox/Safari 最新两个版本
- [ ] 移动端 Safari (iOS 15+)
- [ ] prefers-reduced-motion 正常降级
- [ ] WebGL 不可用时正常降级

### 7.4 安全检查

- [ ] 无敏感信息泄露
- [ ] CSP 头配置正确
- [ ] HTTPS 强制
- [ ] WebSocket 使用 WSS

---

*本文档定义前端开发计划。实施时遵循 FRONTEND_ARCHITECTURE_v1.0.md 的架构设计。*
