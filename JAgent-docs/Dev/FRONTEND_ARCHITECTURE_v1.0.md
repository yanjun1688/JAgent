# Harness 前端架构设计文档

> **版本**: v1.0
> **架构师**: AgentX
> **日期**: 2026-07-22
> **关联文档**: 
> - PRD: `PRD_v2.0_UI_Redesign.md`
> - UI 设计: `UI_REDESIGN_v2.0_Artistic.md`
> - 后端架构: `ARCHITECTURE_v2.1.md`

---

## 1. 架构概述

### 1.1 设计目标

| 目标 | 描述 | 衡量指标 |
|------|------|---------|
| **简洁性** | 8 页面→3 页面，消除信息重复 | 页面跳转次数 ≤1 |
| **实时性** | 聊天界面内实时展示 Agent 执行 | 事件渲染延迟 <200ms |
| **可控性** | 聊天界面内直接暂停/恢复 | 操作步骤 =1 |
| **艺术性** | 玻璃质感、3D 可视化、流畅动效 | 用户满意度调研 |
| **性能** | 首屏加载快，交互流畅 | FCP <1s, FID <100ms |

### 1.2 技术栈

```
核心框架:     React 18 + TypeScript 5
路由:         React Router 6
状态管理:     Zustand (轻量) + React Query (服务端状态)
样式:         Tailwind CSS + CSS Modules (组件级)
动效:         Motion (原 Framer Motion)
3D 可视化:    React Three Fiber + drei
粒子系统:     tsParticles
图标:         Lucide React
测试:         Vitest + React Testing Library
构建:         Vite 5
```

### 1.3 架构分层

```
┌─────────────────────────────────────────────────────────┐
│  Layer 4: 页面层 (Pages)                                 │
│  ChatPage | OverviewPage | HistoryPage                   │
├─────────────────────────────────────────────────────────┤
│  Layer 3: 业务组件层 (Features)                          │
│  Conversation | RealtimePanel | ToolGalaxy | RunTimeline │
├─────────────────────────────────────────────────────────┤
│  Layer 2: 基础组件层 (UI)                                │
│  GlassCard | GlowButton | StatusBadge | StreamingText    │
├─────────────────────────────────────────────────────────┤
│  Layer 1: 设计系统 (Design System)                       │
│  Tokens | Themes | Utilities | Hooks                     │
├─────────────────────────────────────────────────────────┤
│  Layer 0: 基础设施 (Infrastructure)                      │
│  API Client | WebSocket | Event Store | Type Schema      │
└─────────────────────────────────────────────────────────┘
```

---

## 2. 目录结构

### 2.1 新目录规划

```
frontend/
├── src/
│   ├── api/                    # API 客户端 + 类型定义
│   │   ├── client.ts           # REST API 客户端
│   │   ├── ws-client.ts        # WebSocket 客户端
│   │   ├── schema.ts           # OpenAPI 自动生成类型
│   │   └── types.ts            # 手动扩展类型
│   │
│   ├── design-system/          # 设计系统 (新增)
│   │   ├── tokens/
│   │   │   ├── colors.ts       # 色彩 Token
│   │   │   ├── spacing.ts      # 间距 Token
│   │   │   ├── radii.ts        # 圆角 Token
│   │   │   ├── shadows.ts      # 阴影 Token
│   │   │   └── typography.ts   # 字体 Token
│   │   ├── themes/
│   │   │   ├── nebula.ts       # 星云主题
│   │   │   └── index.ts
│   │   └── utils/
│   │       ├── cn.ts           # className 合并工具
│   │       └── motion.ts       # 动效变体
│   │
│   ├── components/
│   │   ├── ui/                 # 基础 UI 组件 (新增)
│   │   │   ├── GlassCard.tsx
│   │   │   ├── GlowButton.tsx
│   │   │   ├── StatusBadge.tsx
│   │   │   └── index.ts
│   │   │
│   │   ├── effects/            # 视觉特效组件 (新增)
│   │   │   ├── ParticleBackground.tsx
│   │   │   ├── AuroraGradient.tsx
│   │   │   └── index.ts
│   │   │
│   │   ├── chat/               # 聊天相关组件
│   │   │   ├── MessageBubble.tsx
│   │   │   ├── StreamingText.tsx
│   │   │   ├── ThinkingPanel.tsx
│   │   │   ├── ToolCallCard.tsx
│   │   │   ├── ConfirmationCard.tsx
│   │   │   └── InputBar.tsx
│   │   │
│   │   ├── conversation/       # 对话相关组件
│   │   │   ├── ConversationSidebar.tsx
│   │   │   ├── ConversationItem.tsx
│   │   │   └── ConversationList.tsx
│   │   │
│   │   ├── realtime/           # 实时面板组件
│   │   │   ├── RealtimePanel.tsx
│   │   │   ├── EventItem.tsx
│   │   │   ── EventStats.tsx
│   │   │
│   │   ├── overview/           # 概览页组件
│   │   │   ├── KPICard.tsx
│   │   │   ├── ToolGalaxy.tsx          # 3D 工具星系
│   │   │   ├── GuardrailChart.tsx
│   │   │   └── McpServerTable.tsx
│   │   │
│   │   └── history/            # 历史页组件
│   │       ├── RunTimeline.tsx
│   │       ├── RunCard.tsx
│   │       ├── RunDetailPanel.tsx
│   │       └── TraceTree.tsx
│   │
│   ├── hooks/                  # 自定义 Hooks
│   │   ├── useConversation.ts
│   │   ├── useRunWebSocket.ts
│   │   ├── useRunControl.ts    # 暂停/恢复控制
│   │   └── useMessageQueue.ts  # 消息排队
│   │
│   ├── stores/                 # Zustand 状态管理 (新增)
│   │   ├── conversationStore.ts
│   │   ├── runStore.ts
│   │   └── uiStore.ts
│   │
│   ├── pages/                  # 页面组件
│   │   ├── ChatPage.tsx        # 聊天主界面
│   │   ├── OverviewPage.tsx    # 系统概览
│   │   └── HistoryPage.tsx     # Run 历史
│   │
│   ├── App.tsx                 # 根组件 + 路由
│   └── main.tsx                # 入口文件
│
├── public/
│   └── fonts/                  # 自定义字体
│       ├── Inter/
│       ├── SpaceGrotesk/
│       └── JetBrainsMono/
│
├── tests/                      # 测试文件
│   ├── components/
│   ├── hooks/
│   └── pages/
│
├── package.json
├── tsconfig.json
├── vite.config.ts
├── tailwind.config.ts          # Tailwind 配置 (新增)
└── postcss.config.js
```

### 2.2 文件命名规范

| 类型 | 命名规则 | 示例 |
|------|---------|------|
| 组件文件 | PascalCase | `GlassCard.tsx` |
| Hook 文件 | camelCase + use 前缀 | `useConversation.ts` |
| Store 文件 | camelCase + Store 后缀 | `conversationStore.ts` |
| 工具文件 | camelCase | `cn.ts`, `motion.ts` |
| 类型文件 | camelCase + types 后缀 | `conversationTypes.ts` |
| 测试文件 | 与被测文件同名 + .test | `GlassCard.test.tsx` |
| 样式文件 | camelCase + .module.css | `GlassCard.module.css` |

---

## 3. 状态管理设计

### 3.1 状态分类

```
┌─────────────────────────────────────────────────────────┐
│                    服务端状态 (Server State)              │
│  - 对话列表 (Conversations)                             │
│  - Run 列表 (Runs)                                      │
│  - Run 详情 (Run Detail)                                │
│  - 事件流 (Events via WebSocket)                        │
│  - 工具统计 (Tool Stats)                                │
│  - Guardrail 统计                                       │
│                                                         │
│  管理工具: React Query (缓存 + 同步 + 乐观更新)           │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                    客户端状态 (Client State)              │
│  - 当前选中的对话 ID                                     │
│  - 当前选中的 Run ID                                     │
│  - 输入框文本                                            │
│  - 消息排队队列                                          │
│  - UI 展开/折叠状态                                      │
│  - 侧边栏开关状态                                        │
│                                                         │
│  管理工具: Zustand (轻量、类型安全)                       │
─────────────────────────────────────────────────────────┘
```

### 3.2 Zustand Store 设计

```typescript
// stores/conversationStore.ts
import { create } from 'zustand'
import { devtools } from 'zustand/middleware'

interface ConversationState {
  // State
  activeConversationId: string | null
  conversations: Conversation[]
  searchQuery: string
  
  // Actions
  setActiveConversation: (id: string | null) => void
  setConversations: (conversations: Conversation[]) => void
  setSearchQuery: (query: string) => void
  addConversation: (conversation: Conversation) => void
  removeConversation: (id: string) => void
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
        conversations: state.conversations.filter(c => c.id !== id)
      })),
    }),
    { name: 'ConversationStore' }
  )
)
```

```typescript
// stores/runStore.ts
interface RunState {
  // State
  activeRunId: string | null
  runStatus: RunStatus | null
  events: WsEvent[]
  isWebSocketConnected: boolean
  
  // Actions
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
      
      setActiveRun: (runId) => set({ activeRunId: runId, events: [], runStatus: null }),
      setRunStatus: (status) => set({ runStatus: status }),
      addEvent: (event) => set((state) => ({
        events: [...state.events, event].sort((a, b) => a.seq - b.seq)
      })),
      setEvents: (events) => set({ events }),
      setWebSocketConnected: (connected) => set({ isWebSocketConnected: connected }),
      clearRun: () => set({ activeRunId: null, runStatus: null, events: [] }),
    }),
    { name: 'RunStore' }
  )
)
```

```typescript
// stores/uiStore.ts
interface UIState {
  // State
  sidebarOpen: boolean
  realtimePanelOpen: boolean
  expandedEventSeqs: Set<number>
  theme: 'nebula' | 'light'
  
  // Actions
  toggleSidebar: () => void
  toggleRealtimePanel: () => void
  toggleEventExpansion: (seq: number) => void
  setTheme: (theme: 'nebula' | 'light') => void
}

export const useUIStore = create<UIState>()(
  devtools(
    (set) => ({
      sidebarOpen: true,
      realtimePanelOpen: true,
      expandedEventSeqs: new Set(),
      theme: 'nebula',
      
      toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
      toggleRealtimePanel: () => set((state) => ({ realtimePanelOpen: !state.realtimePanelOpen })),
      toggleEventExpansion: (seq) => set((state) => {
        const next = new Set(state.expandedEventSeqs)
        if (next.has(seq)) next.delete(seq)
        else next.add(seq)
        return { expandedEventSeqs: next }
      }),
      setTheme: (theme) => set({ theme }),
    }),
    { name: 'UIStore' }
  )
)
```

### 3.3 React Query 配置

```typescript
// api/query-client.ts
import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60 * 5, // 5 分钟
      gcTime: 1000 * 60 * 30,   // 30 分钟
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
})
```

```typescript
// hooks/useConversations.ts
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { conversationApi } from '../api/client'

export function useConversations() {
  const queryClient = useQueryClient()
  
  const { data, isLoading, error } = useQuery({
    queryKey: ['conversations'],
    queryFn: conversationApi.list,
  })
  
  const createMutation = useMutation({
    mutationFn: conversationApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
  
  const deleteMutation = useMutation({
    mutationFn: conversationApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
  })
  
  return {
    conversations: data?.conversations ?? [],
    total: data?.total ?? 0,
    isLoading,
    error,
    create: createMutation.mutateAsync,
    delete: deleteMutation.mutateAsync,
  }
}
```

---

## 4. 组件设计

### 4.1 组件层次结构

```
App
├── Header (导航栏)
── Routes
    ├── ChatPage (/)
    │   ├── ConversationSidebar (左栏)
    │   │   ├── SearchBar
    │   │   ├── NewConversationButton
    │   │   └── ConversationList
    │   │       └── ConversationItem (×N)
    │   │
    │   ├── ChatArea (中栏)
    │   │   ├── ParticleBackground (特效层)
    │   │   ├── MessageList
    │   │   │   ├── UserMessage (×N)
    │   │   │   ├── AssistantMessage (×N)
    │   │   │   │   └── StreamingText
    │   │   │   ├── ThinkingPanel
    │   │   │   ├── ToolCallCard (×N)
    │   │   │   ├── ConfirmationCard (×N)
    │   │   │   └── PendingIndicator
    │   │   └── InputBar
    │   │       ├── TextInput
    │   │       ├── PauseButton
    │   │       ├── ResumeButton
    │   │       └── SendButton
    │   │
    │   └── RealtimePanel (右栏)
    │       ├── PanelHeader
    │       ├── EventStats
    │       ├── EventList
    │       │   └── EventItem (×N)
    │       └── PanelFooter
    │
    ├── OverviewPage (/overview)
    │   ├── KPICards
    │   │   └── KPICard (×6)
    │   ├── ToolGalaxy (3D Canvas)
    │   ├── GuardrailSection
    │   └── McpServerSection
    │
    └── HistoryPage (/history)
        ├── FilterBar
        ├── RunTimeline
        │   └── RunCard (×N)
        └── RunDetailPanel (条件渲染)
            ├── RunKPIs
            ├── EventTimeline
            ── TraceTree
```

### 4.2 核心组件接口定义

```typescript
// components/chat/MessageBubble.tsx
interface MessageBubbleProps {
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  isStreaming?: boolean
}

// components/chat/ToolCallCard.tsx
interface ToolCallCardProps {
  toolName: string
  status: 'running' | 'completed' | 'failed' | 'timeout'
  input?: Record<string, unknown>
  output?: unknown
  error?: string
  durationMs?: number
  onExpand?: () => void
}

// components/realtime/EventItem.tsx
interface EventItemProps {
  event: WsEvent
  isExpanded: boolean
  onToggle: () => void
}

// components/overview/ToolGalaxy.tsx
interface ToolGalaxyProps {
  tools: ToolStat[]
  onToolClick?: (toolName: string) => void
}

// components/history/RunCard.tsx
interface RunCardProps {
  run: RunSummary
  isSelected: boolean
  onSelect: (runId: string) => void
}
```

### 4.3 组件设计原则

| 原则 | 描述 | 示例 |
|------|------|------|
| **单一职责** | 每个组件只做一件事 | `ToolCallCard` 只展示工具调用，不处理业务逻辑 |
| **受控优先** | 状态由父组件管理 | `TextInput` 的 value 由父组件控制 |
| **组合优于继承** | 通过 children 组合 | `GlassCard` 接受 children，不预设内容 |
| **类型安全** | 所有 props 有明确类型 | 使用 TypeScript interface，不用 any |
| **可测试性** | 组件易于单元测试 | 纯展示组件，无副作用 |

---

## 5. API 集成设计

### 5.1 API 客户端架构

```
┌─────────────────────────────────────────────────────────┐
│                    API Client Layer                      │
│                                                         │
│  ┌─────────────────┐    ┌──────────────────────────┐    │
│  │ REST Client     │    │ WebSocket Client         │    │
│  │ (axios/fetch)   │    │ (原生 WebSocket)          │    │
│  │                 │    │                          │    │
│  │ - Conversations │    │ - Run Events             │    │
│  │ - Runs          │    │ - Real-time Updates      │    │
│  │ - Analysis      │    │                          │    │
│  └─────────────────┘    └──────────────────────────┘    │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │ React Query (缓存 + 同步 + 乐观更新)              │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

### 5.2 REST API 客户端

```typescript
// api/client.ts
import type { 
  Conversation, 
  ConversationListResponse,
  RunSummary,
  RunDetail,
} from './schema'

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
    },
    ...options,
  })
  
  if (!response.ok) {
    throw new Error(`API Error: ${response.status} ${response.statusText}`)
  }
  
  return response.json()
}

export const conversationApi = {
  list: () => request<ConversationListResponse>('/api/v1/conversations'),
  
  get: (id: string) => request<Conversation>(`/api/v1/conversations/${id}`),
  
  create: (data: { title?: string }) => 
    request<Conversation>('/api/v1/conversations', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  
  delete: (id: string) => 
    request<void>(`/api/v1/conversations/${id}`, { method: 'DELETE' }),
  
  sendMessage: (id: string, message: string) =>
    request<{ run_id: string }>(`/api/v1/conversations/${id}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message }),
    }),
}

export const runApi = {
  list: () => request<{ runs: RunSummary[]; total: number }>('/api/v1/runs'),
  
  get: (id: string) => request<RunDetail>(`/api/v1/runs/${id}`),
  
  pause: (id: string) => 
    request<void>(`/api/v1/runs/${id}/pause`, { method: 'POST' }),
  
  resume: (id: string) => 
    request<void>(`/api/v1/runs/${id}/resume`, { method: 'POST' }),
}
```

### 5.3 WebSocket 客户端

```typescript
// api/ws-client.ts
import type { WsEvent } from './schema'

type EventCallback = (event: WsEvent) => void
type StatusCallback = (status: 'connected' | 'disconnected' | 'error') => void

export class RunWebSocketClient {
  private ws: WebSocket | null = null
  private runId: string | null = null
  private eventCallbacks: Set<EventCallback> = new Set()
  private statusCallbacks: Set<StatusCallback> = new Set()
  private reconnectAttempts = 0
  private maxReconnectAttempts = 5
  
  connect(runId: string) {
    this.runId = runId
    this.reconnectAttempts = 0
    this.createConnection()
  }
  
  private createConnection() {
    const wsUrl = `${BASE_URL_WS}/api/v1/runs/${this.runId}/events`
    this.ws = new WebSocket(wsUrl)
    
    this.ws.onopen = () => {
      this.reconnectAttempts = 0
      this.notifyStatus('connected')
    }
    
    this.ws.onmessage = (event) => {
      try {
        const data: WsEvent = JSON.parse(event.data)
        this.notifyEvent(data)
      } catch (err) {
        console.error('Failed to parse WebSocket message:', err)
      }
    }
    
    this.ws.onclose = () => {
      this.notifyStatus('disconnected')
      this.attemptReconnect()
    }
    
    this.ws.onerror = () => {
      this.notifyStatus('error')
    }
  }
  
  private attemptReconnect() {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++
      const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000)
      setTimeout(() => this.createConnection(), delay)
    }
  }
  
  disconnect() {
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
  }
  
  onEvent(callback: EventCallback) {
    this.eventCallbacks.add(callback)
    return () => this.eventCallbacks.delete(callback)
  }
  
  onStatus(callback: StatusCallback) {
    this.statusCallbacks.add(callback)
    return () => this.statusCallbacks.delete(callback)
  }
  
  private notifyEvent(event: WsEvent) {
    this.eventCallbacks.forEach(cb => cb(event))
  }
  
  private notifyStatus(status: 'connected' | 'disconnected' | 'error') {
    this.statusCallbacks.forEach(cb => cb(status))
  }
}

// 单例实例
export const wsClient = new RunWebSocketClient()
```

### 5.4 自定义 Hook 封装

```typescript
// hooks/useRunWebSocket.ts
import { useEffect, useRef } from 'react'
import { wsClient } from '../api/ws-client'
import { useRunStore } from '../stores/runStore'

export function useRunWebSocket(runId: string | null) {
  const { addEvent, setWebSocketConnected, setRunStatus } = useRunStore()
  const runIdRef = useRef(runId)
  
  useEffect(() => {
    if (!runId) {
      wsClient.disconnect()
      return
    }
    
    if (runIdRef.current !== runId) {
      wsClient.disconnect()
      wsClient.connect(runId)
      runIdRef.current = runId
    }
    
    const unsubscribeEvent = wsClient.onEvent((event) => {
      addEvent(event)
      
      // 更新 Run 状态
      if (event.event_type === 'RunCompleted') {
        setRunStatus('completed')
      } else if (event.event_type === 'RunFailed') {
        setRunStatus('failed')
      } else if (event.event_type === 'RunPaused') {
        setRunStatus('paused')
      } else if (event.event_type === 'RunResumed') {
        setRunStatus('running')
      }
    })
    
    const unsubscribeStatus = wsClient.onStatus((status) => {
      setWebSocketConnected(status === 'connected')
    })
    
    return () => {
      unsubscribeEvent()
      unsubscribeStatus()
    }
  }, [runId, addEvent, setWebSocketConnected, setRunStatus])
}
```

---

## 6. 性能优化策略

### 6.1 加载性能

| 优化项 | 方法 | 预期效果 |
|--------|------|---------|
| **代码分割** | `React.lazy` + `Suspense` | 首屏 bundle 减少 40% |
| **路由懒加载** | 页面组件动态导入 | 按需加载页面代码 |
| **3D 组件懒加载** | 仅 Overview 页加载 Three.js | 减少 600KB 初始加载 |
| **字体优化** | `font-display: swap` + 预加载 | 避免 FOIT |
| **图片优化** | WebP 格式 + 懒加载 | 减少图片体积 50% |

```typescript
// App.tsx - 路由懒加载
import { lazy, Suspense } from 'react'

const ChatPage = lazy(() => import('./pages/ChatPage'))
const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const HistoryPage = lazy(() => import('./pages/HistoryPage'))

function App() {
  return (
    <Suspense fallback={<LoadingScreen />}>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="/overview" element={<OverviewPage />} />
        <Route path="/history" element={<HistoryPage />} />
      </Routes>
    </Suspense>
  )
}
```

### 6.2 渲染性能

| 优化项 | 方法 | 预期效果 |
|--------|------|---------|
| **虚拟列表** | `react-window` 长消息列表 | 千条消息流畅滚动 |
| **Memo 优化** | `React.memo` 纯展示组件 | 避免不必要的重渲染 |
| **动效优化** | 仅使用 `transform` + `opacity` | GPU 加速，避免 layout 重排 |
| **WebWorker** | 大量数据计算移至 Worker | 主线程不阻塞 |

```typescript
// components/chat/MessageList.tsx - 虚拟列表
import { FixedSizeList } from 'react-window'

function MessageList({ messages }: { messages: Message[] }) {
  return (
    <FixedSizeList
      height={600}
      itemCount={messages.length}
      itemSize={80}
      width="100%"
    >
      {({ index, style }) => (
        <div style={style}>
          <MessageBubble message={messages[index]} />
        </div>
      )}
    </FixedSizeList>
  )
}
```

### 6.3 Bundle 优化

```typescript
// vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          // 分离 vendor 代码
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'three-vendor': ['three', '@react-three/fiber', '@react-three/drei'],
          'ui-vendor': ['motion', '@tsparticles/react'],
        },
      },
    },
    chunkSizeWarningLimit: 500,
  },
})
```

### 6.4 WebGL 性能

```typescript
// components/effects/ParticleBackground.tsx
import { useCallback } from 'react'
import Particles from '@tsparticles/react'
import { loadSlim } from '@tsparticles/slim'

export function ParticleBackground() {
  const particlesInit = useCallback(async (engine) => {
    await loadSlim(engine)
  }, [])
  
  // 检测 prefers-reduced-motion
  const prefersReducedMotion = window.matchMedia(
    '(prefers-reduced-motion: reduce)'
  ).matches
  
  if (prefersReducedMotion) {
    return null // 禁用粒子系统
  }
  
  return (
    <Particles
      id="tsparticles"
      init={particlesInit}
      options={{
        particles: {
          number: { value: 80, density: { enable: true, area: 800 } },
          // ... 其他配置
        },
      }}
    />
  )
}
```

---

## 7. 开发规范

### 7.1 代码风格

```typescript
// ✅ 好的写法
interface ButtonProps {
  variant: 'primary' | 'secondary' | 'ghost'
  size: 'sm' | 'md' | 'lg'
  children: React.ReactNode
  onClick?: () => void
  disabled?: boolean
}

export function GlowButton({ 
  variant = 'primary', 
  size = 'md', 
  children, 
  onClick,
  disabled = false 
}: ButtonProps) {
  // ...
}

//  坏的写法
function Button(props: any) {
  // ...
}
```

### 7.2 命名约定

| 类型 | 约定 | 示例 |
|------|------|------|
| 组件名 | PascalCase | `GlassCard`, `ToolCallCard` |
| Hook 名 | camelCase + use 前缀 | `useConversation`, `useRunWebSocket` |
| 变量名 | camelCase | `activeRunId`, `isLoading` |
| 常量名 | UPPER_SNAKE_CASE | `MAX_RECONNECT_ATTEMPTS` |
| 类型名 | PascalCase | `Conversation`, `RunStatus` |
| 文件名 | 与导出组件同名 | `GlassCard.tsx` |
| CSS 类名 | kebab-case 或 BEM | `glass-card`, `glass-card__header` |

### 7.3 Git 提交规范

```
feat: 新功能
fix: Bug 修复
docs: 文档更新
style: 代码格式 (不影响功能)
refactor: 重构
perf: 性能优化
test: 测试相关
chore: 构建/工具链

示例:
feat(chat): 添加消息排队功能
fix(ws): 修复 WebSocket 重连内存泄漏
perf(3d): 优化工具星系渲染性能
```

### 7.4 测试规范

```typescript
// tests/components/GlassCard.test.tsx
import { render, screen } from '@testing-library/react'
import { GlassCard } from '../../components/ui/GlassCard'

describe('GlassCard', () => {
  it('renders children correctly', () => {
    render(<GlassCard>Test Content</GlassCard>)
    expect(screen.getByText('Test Content')).toBeInTheDocument()
  })
  
  it('applies custom className', () => {
    render(<GlassCard className="custom">Content</GlassCard>)
    expect(screen.getByText('Content').parentElement).toHaveClass('custom')
  })
  
  it('handles click events', () => {
    const onClick = vi.fn()
    render(<GlassCard onClick={onClick}>Click Me</GlassCard>)
    screen.getByText('Click Me').click()
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})
```

### 7.5 类型安全规范

```typescript
// ✅ 使用明确的类型
interface Conversation {
  id: string
  title: string
  messageCount: number
  updatedAt: number
  status: 'active' | 'archived'
}

// ❌ 避免使用 any
interface Conversation {
  id: any
  title: any
  // ...
}

// ✅ 使用类型守卫
function isRunCompleted(event: WsEvent): event is WsEvent & { event_type: 'RunCompleted' } {
  return event.event_type === 'RunCompleted'
}

// ✅ 使用枚举代替字符串常量
enum RunStatus {
  Running = 'running',
  Paused = 'paused',
  Completed = 'completed',
  Failed = 'failed',
}
```

---

## 8. 部署配置

### 8.1 环境变量

```bash
# .env.development
VITE_API_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000

# .env.production
VITE_API_URL=https://api.harness.ai
VITE_WS_URL=wss://api.harness.ai
```

### 8.2 Docker 部署

```dockerfile
# Dockerfile
FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
```

```nginx
# nginx.conf
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;
    
    # SPA 路由支持
    location / {
        try_files $uri $uri/ /index.html;
    }
    
    # API 代理
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    
    # WebSocket 代理
    location /ws/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    
    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

---

## 9. 里程碑与交付

### 9.1 Phase 1: 基础架构 (2 天)

| 任务 | 交付物 | 验收标准 |
|------|--------|---------|
| 设计 Token | `design-system/tokens/` | 色彩、间距、圆角、阴影、字体 Token 完整 |
| 基础 UI 组件 | `components/ui/` | GlassCard, GlowButton, StatusBadge 可用 |
| 状态管理 | `stores/` | Zustand stores 创建，DevTools 集成 |
| API 客户端 | `api/client.ts` | REST + WebSocket 客户端可用 |

### 9.2 Phase 2: 聊天页面 (3 天)

| 任务 | 交付物 | 验收标准 |
|------|--------|---------|
| 对话列表 | `ConversationSidebar` | 搜索、新建、删除功能正常 |
| 聊天区域 | `ChatArea` + `MessageList` | 消息显示、流式文本、动效正常 |
| 实时面板 | `RealtimePanel` | WebSocket 事件实时展示 |
| 输入控制 | `InputBar` | 发送、暂停、恢复、排队功能正常 |

### 9.3 Phase 3: 概览 + 历史页 (3 天)

| 任务 | 交付物 | 验收标准 |
|------|--------|---------|
| 概览页 | `OverviewPage` | KPI、3D 工具星系、Guardrail 统计 |
| 历史页 | `HistoryPage` | Run 列表、时间线、详情展开 |
| 路由整合 | `App.tsx` | 3 页面路由、旧路由重定向 |

### 9.4 Phase 4: 优化与测试 (2 天)

| 任务 | 交付物 | 验收标准 |
|------|--------|---------|
| 性能优化 | Bundle 分析、虚拟列表 | 首屏 <1s, 滚动流畅 |
| 响应式 | 移动端适配 | 768px/1024px/1440px 断点正常 |
| 测试 | 单元测试 + 集成测试 | 覆盖率 >80% |
| 文档 | 组件文档、API 文档 | Storybook 或 MDX |

---

## 10. 风险与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| Three.js bundle 过大 | 首屏加载慢 | 高 | 懒加载 + 代码分割 |
| WebSocket 连接不稳定 | 实时性差 | 中 | 自动重连 + 离线队列 |
| 动效性能问题 | 低端设备卡顿 | 中 | prefers-reduced-motion + 性能监控 |
| 状态管理复杂 | 维护困难 | 低 | Zustand + React Query 清晰分层 |
| 3D 兼容性 | 部分浏览器不支持 | 低 | WebGL 检测 + 降级方案 |

---

*本文档定义前端架构设计。实现时遵循 AGENTS.md 的开发规范，保持代码质量和工程实践。*
