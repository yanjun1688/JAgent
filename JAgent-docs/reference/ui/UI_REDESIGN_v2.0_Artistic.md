# UI 设计文档 v2.0: Harness — 艺术级 AI 交互界面

> **版本**: v2.0
> **UI 设计师**: AgentX
> **日期**: 2026-07-22
> **关联 PRD**: `PRD_v2.0_UI_Redesign.md`
> **设计哲学**: "工具即艺术" — 为拥有艺术审美的用户打造

---

## 1. 设计愿景

Harness 不仅是一个 Agent 执行引擎，更是一件**数字艺术品**。每一次对话都是一场视觉交响乐，每一个工具调用都是一次光影变幻。我们追求的不是"能用"，而是"令人屏息"。

### 1.1 灵感来源

| 灵感 | 借鉴点 |
|------|--------|
| **Linear** | 极简主义、微动效、深色主题 |
| **Vercel** | 几何美学、渐变运用、性能感 |
| **Figma** | 协作感、流畅交互、精致细节 |
| **Arc Browser** | 空间感、层次分明、呼吸感 |
| **Apple Vision Pro** | 玻璃质感、空间计算、光影层次 |
| **TeamLab 数字艺术** | 粒子流动、沉浸式体验、自然韵律 |

### 1.2 核心设计原则

1. **呼吸感**: 界面像生物一样有节奏地呼吸，而非机械地刷新
2. **光影层次**: 通过玻璃质感、阴影、渐变营造空间深度
3. **流动韵律**: 所有动效遵循自然物理规律，如弹簧、水流、光线
4. **克制之美**: 每一处视觉元素都有存在理由，无多余装饰
5. **沉浸体验**: 用户进入界面即进入一个数字空间，而非操作一个工具

---

## 2. 技术栈升级

### 2.1 新增依赖

```json
{
  "dependencies": {
    "3d-effects": {
      "@react-three/fiber": "^8.x",
      "@react-three/drei": "^9.x",
      "@react-three/postprocessing": "^2.x",
      "three": "^0.160.x"
    },
    "animations": {
      "motion": "^11.x",
      "@lottiefiles/react-lottie-player": "^3.x"
    },
    "visual-effects": {
      "@tsparticles/react": "^3.x",
      "@tsparticles/slim": "^3.x"
    },
    "components": {
      "lucide-react": "^0.x",
      "class-variance-authority": "^0.x",
      "clsx": "^2.x",
      "tailwind-merge": "^2.x"
    }
  }
}
```

### 2.2 技术分层

```
┌─────────────────────────────────────────────────────────┐
│  Layer 4: 业务组件层 (Chat, Overview, History)           │
├─────────────────────────────────────────────────────────┤
│  Layer 3: 动效组件层 (Motion, Lottie, Transitions)       │
├─────────────────────────────────────────────────────────┤
│  Layer 2: 视觉特效层 (Three.js, Particles, Shaders)      │
├─────────────────────────────────────────────────────────┤
│  Layer 1: 基础组件层 (shadcn/ui, Radix, Tailwind)        │
─────────────────────────────────────────────────────────┘
```

---

## 3. 视觉语言

### 3.1 色彩系统 — "星云"主题

**主色调**: 深空蓝紫 → 星云粉 → 极光绿

```
Background:
  - Primary:    #0A0A0F (深空黑)
  - Secondary:  #12121A (星云暗)
  - Tertiary:   #1A1A2E (深空蓝)

Accent:
  - Primary:    #6366F1 (靛蓝 - 智能)
  - Secondary:  #A855F7 (紫罗兰 - 创造)
  - Tertiary:   #EC4899 (玫瑰 - 温暖)
  - Quaternary: #10B981 (翡翠 - 成功)

Status:
  - Success:    #10B981 (翡翠绿)
  - Warning:    #F59E0B (琥珀)
  - Error:      #EF4444 (珊瑚红)
  - Info:       #3B82F6 (天空蓝)

Text:
  - Primary:    #F8FAFC (星光明)
  - Secondary:  #94A3B8 (星云灰)
  - Tertiary:   #64748B (深空灰)
  - Muted:      #475569 (暗星云)
```

### 3.2 渐变系统

```css
/* 主渐变 - 星云流动 */
.gradient-nebula {
  background: linear-gradient(
    135deg,
    #6366F1 0%,
    #A855F7 50%,
    #EC4899 100%
  );
}

/* 玻璃渐变 - 透明层次 */
.gradient-glass {
  background: linear-gradient(
    135deg,
    rgba(255, 255, 255, 0.05) 0%,
    rgba(255, 255, 255, 0.02) 100%
  );
}

/* 极光渐变 - 动态背景 */
.gradient-aurora {
  background: 
    radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.15) 0%, transparent 50%),
    radial-gradient(at 100% 0%, rgba(168, 85, 247, 0.15) 0%, transparent 50%),
    radial-gradient(at 100% 100%, rgba(236, 72, 153, 0.1) 0%, transparent 50%),
    radial-gradient(at 0% 100%, rgba(16, 185, 129, 0.1) 0%, transparent 50%);
  animation: aurora-shift 20s ease infinite;
}

@keyframes aurora-shift {
  0%, 100% { background-position: 0% 50%; }
  50% { background-position: 100% 50%; }
}
```

### 3.3 玻璃质感系统

```css
/* 基础玻璃 */
.glass-base {
  background: rgba(255, 255, 255, 0.03);
  backdrop-filter: blur(20px) saturate(180%);
  -webkit-backdrop-filter: blur(20px) saturate(180%);
  border: 1px solid rgba(255, 255, 255, 0.08);
}

/* 强化玻璃 - 用于浮层 */
.glass-elevated {
  background: rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(40px) saturate(200%);
  -webkit-backdrop-filter: blur(40px) saturate(200%);
  border: 1px solid rgba(255, 255, 255, 0.12);
  box-shadow: 
    0 8px 32px rgba(0, 0, 0, 0.4),
    inset 0 1px 0 rgba(255, 255, 255, 0.1);
}

/* 毛玻璃 - 用于背景 */
.glass-frosted {
  background: rgba(10, 10, 15, 0.7);
  backdrop-filter: blur(60px) saturate(150%);
  -webkit-backdrop-filter: blur(60px) saturate(150%);
}
```

### 3.4 阴影系统

```css
/* 层次阴影 */
.shadow-nebula {
  box-shadow: 
    0 0 0 1px rgba(99, 102, 241, 0.1),
    0 4px 16px rgba(99, 102, 241, 0.15),
    0 8px 32px rgba(0, 0, 0, 0.4);
}

/* 发光阴影 - 用于交互元素 */
.shadow-glow {
  box-shadow: 
    0 0 20px rgba(99, 102, 241, 0.3),
    0 0 40px rgba(99, 102, 241, 0.1);
}

/* 悬浮阴影 */
.shadow-float {
  box-shadow: 
    0 20px 60px rgba(0, 0, 0, 0.5),
    0 0 0 1px rgba(255, 255, 255, 0.05);
}
```

### 3.5 字体系统

```
Primary Font: 'Inter', system-ui, sans-serif
  - 用于 UI 文本、按钮、标签
  
Display Font: 'Space Grotesk', sans-serif
  - 用于标题、数字、状态显示
  
Mono Font: 'JetBrains Mono', 'Fira Code', monospace
  - 用于代码、JSON、Run ID、技术信息
  
Font Weights:
  - Light:    300 (次要文本)
  - Regular:  400 (正文)
  - Medium:   500 (强调)
  - Semibold: 600 (标题)
  - Bold:     700 (重要数字)
```

---

## 4. 3D 视觉层设计

### 4.1 背景粒子系统

**技术**: `@tsparticles/react` + `@tsparticles/slim`

**效果描述**:
- 深空背景中漂浮着微小的光点，模拟星云粒子
- 粒子缓慢流动，形成自然的韵律
- 当 Agent 执行时，粒子加速流动，形成"思维流"视觉效果
- 工具调用时，对应颜色的粒子爆发扩散

**配置参数**:
```typescript
const particleConfig = {
  particles: {
    number: { value: 80, density: { enable: true, area: 800 } },
    color: { value: ["#6366F1", "#A855F7", "#EC4899"] },
    shape: { type: "circle" },
    opacity: { value: 0.3, random: true },
    size: { value: 2, random: true },
    move: {
      enable: true,
      speed: 0.5,
      direction: "none",
      random: true,
      straight: false,
      outModes: "out"
    },
    links: {
      enable: true,
      distance: 150,
      color: "#6366F1",
      opacity: 0.1,
      width: 1
    }
  },
  interactivity: {
    events: {
      onHover: { enable: true, mode: "grab" },
      onClick: { enable: true, mode: "push" }
    }
  }
};
```

### 4.2 3D 数据可视化

**技术**: `@react-three/fiber` + `@react-three/drei`

**应用场景**:

#### 4.2.1 Overview 页面 - 3D 工具调用星系

```
设计概念: 将工具调用统计可视化为一个微型星系
- 中心: 太阳 = 总调用数
- 行星: 每个工具 = 一颗行星，大小 = 调用频率
- 轨道: 成功率 = 轨道稳定性
- 卫星: 失败调用 = 红色卫星环绕

交互:
- 鼠标悬停行星 → 显示工具详情
- 点击行星 → 跳转到该工具的详细统计
- 滚轮缩放 → 调整视图范围
```

**实现代码框架**:
```tsx
import { Canvas } from '@react-three/fiber'
import { OrbitControls, Sphere, Html } from '@react-three/drei'

function ToolGalaxy({ tools }: { tools: ToolStat[] }) {
  return (
    <Canvas camera={{ position: [0, 0, 5], fov: 60 }}>
      <ambientLight intensity={0.3} />
      <pointLight position={[0, 0, 0]} intensity={2} color="#6366F1" />
      
      {/* 中心太阳 */}
      <Sphere args={[0.5, 32, 32]}>
        <meshStandardMaterial 
          color="#6366F1" 
          emissive="#6366F1"
          emissiveIntensity={0.5}
        />
      </Sphere>
      
      {/* 工具行星 */}
      {tools.map((tool, i) => (
        <ToolPlanet 
          key={tool.name}
          tool={tool}
          position={calculateOrbit(i, tools.length)}
        />
      ))}
      
      <OrbitControls enableZoom={true} enablePan={false} />
    </Canvas>
  )
}
```

#### 4.2.2 Chat 页面 - 3D 思维流背景

```
设计概念: Agent 思考时，背景出现流动的光带
- 平静状态: 缓慢流动的粒子
- 思考状态: 光带加速，形成漩涡
- 工具调用: 光带分叉，指向不同方向
- 完成状态: 光带汇聚，形成光环

技术实现:
- 使用 drei 的 <Trail> 组件创建光带
- 使用 shader 实现流动效果
- 根据 Agent 状态动态调整参数
```

### 4.3 3D 组件库

```tsx
// 玻璃卡片 - 3D 倾斜效果
import { motion } from 'motion'

function GlassCard3D({ children }: { children: React.ReactNode }) {
  return (
    <motion.div
      whileHover={{ 
        rotateX: 5, 
        rotateY: 5, 
        scale: 1.02,
        transition: { type: "spring", stiffness: 300 }
      }}
      style={{
        transformStyle: "preserve-3d",
        perspective: 1000
      }}
      className="glass-elevated rounded-2xl p-6"
    >
      {children}
    </motion.div>
  )
}

// 发光按钮
function GlowButton({ children, onClick }: { children: React.ReactNode, onClick: () => void }) {
  return (
    <motion.button
      whileHover={{ 
        scale: 1.05,
        boxShadow: "0 0 30px rgba(99, 102, 241, 0.5)"
      }}
      whileTap={{ scale: 0.95 }}
      onClick={onClick}
      className="relative px-6 py-3 rounded-xl font-medium text-white overflow-hidden"
      style={{
        background: "linear-gradient(135deg, #6366F1, #A855F7)"
      }}
    >
      <span className="relative z-10">{children}</span>
      <motion.div
        className="absolute inset-0 opacity-30"
        style={{
          background: "radial-gradient(circle at center, white, transparent)"
        }}
        animate={{
          scale: [1, 1.5, 1],
          opacity: [0.3, 0, 0.3]
        }}
        transition={{ duration: 2, repeat: Infinity }}
      />
    </motion.button>
  )
}
```

---

## 5. 动效系统设计

### 5.1 动效原则

| 原则 | 描述 | 示例 |
|------|------|------|
| **自然物理** | 所有动效遵循弹簧物理，非线性 | 消息气泡弹入 |
| **意图明确** | 动效传达状态变化，非纯装饰 | 暂停按钮脉冲 |
| **性能优先** | 使用 transform/opacity，避免 layout 重排 | 使用 will-change |
| **可关闭** | 尊重用户偏好，支持减少动效 | prefers-reduced-motion |

### 5.2 核心动效库

```typescript
// 消息入场动画
const messageVariants = {
  hidden: { 
    opacity: 0, 
    y: 20, 
    scale: 0.95,
    filter: "blur(10px)"
  },
  visible: { 
    opacity: 1, 
    y: 0, 
    scale: 1,
    filter: "blur(0px)",
    transition: {
      type: "spring",
      stiffness: 300,
      damping: 25
    }
  },
  exit: {
    opacity: 0,
    y: -10,
    transition: { duration: 0.2 }
  }
};

// 工具调用状态动画
const toolStatusVariants = {
  running: {
    animate: {
      boxShadow: [
        "0 0 0 0 rgba(245, 158, 11, 0.4)",
        "0 0 0 10px rgba(245, 158, 11, 0)",
      ],
      transition: { duration: 1.5, repeat: Infinity }
    }
  },
  completed: {
    animate: {
      scale: [1, 1.1, 1],
      transition: { duration: 0.3 }
    }
  },
  failed: {
    animate: {
      x: [0, -5, 5, -5, 5, 0],
      transition: { duration: 0.4 }
    }
  }
};

// 思考面板展开动画
const thinkingPanelVariants = {
  collapsed: { 
    height: 48, 
    opacity: 0.7,
    transition: { type: "spring", stiffness: 300 }
  },
  expanded: { 
    height: "auto", 
    opacity: 1,
    transition: { type: "spring", stiffness: 300 }
  }
};
```

### 5.3 流式文本动画

```tsx
// 打字机效果 - Agent 回复逐字显示
function StreamingText({ text }: { text: string }) {
  const [displayedText, setDisplayedText] = useState("");
  
  useEffect(() => {
    let i = 0;
    const timer = setInterval(() => {
      setDisplayedText(text.slice(0, i + 1));
      i++;
      if (i >= text.length) clearInterval(timer);
    }, 20); // 每 20ms 显示一个字符
    return () => clearInterval(timer);
  }, [text]);
  
  return (
    <motion.p
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="text-gray-200 leading-relaxed"
    >
      {displayedText}
      <motion.span
        animate={{ opacity: [1, 0] }}
        transition={{ duration: 0.8, repeat: Infinity }}
        className="inline-block w-0.5 h-5 bg-indigo-500 ml-1"
      />
    </motion.p>
  );
}
```

### 5.4 页面转场动画

```tsx
// 页面切换 - 平滑过渡
function PageTransition({ children }: { children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
    >
      {children}
    </motion.div>
  );
}

// 路由配置
<AnimatePresence mode="wait">
  <Routes location={location} key={location.pathname}>
    <Route path="/" element={
      <PageTransition><ChatPage /></PageTransition>
    } />
    <Route path="/overview" element={
      <PageTransition><OverviewPage /></PageTransition>
    } />
    <Route path="/history" element={
      <PageTransition><HistoryPage /></PageTransition>
    } />
  </Routes>
</AnimatePresence>
```

---

## 6. 页面详细设计

### 6.1 Chat 页面 — "数字工作室"

**布局**: 三栏式，但采用玻璃质感分层

```
─────────────────────────────────────────────────────────────────────┐
│  Header: [Logo] Harness  |  Chat  |  Overview  |  History           │
│  Style: 玻璃质感，底部发光边框                                        │
├──────────┬──────────────────────────────┬───────────────────────────┤
│          │                              │                           │
│  对话列表  │       聊天区域               │    实时执行面板            │
│  (280px)  │       (flex: 1)             │    (380px)                │
│          │                              │                           │
│  玻璃卡片  │  玻璃背景 + 粒子系统          │  玻璃卡片                 │
│  圆角 16px │  圆角 24px                   │  圆角 16px                │
│          │                              │                           │
├──────────┴──────────────────────────────┴───────────────────────────┤
│  InputBar: 玻璃质感输入栏，发光边框                                    │
└─────────────────────────────────────────────────────────────────────┘
```

**背景层**:
- 底层: 深空黑 `#0A0A0F`
- 中层: 极光渐变动画 (20s 循环)
- 顶层: 粒子系统 (80 个光点，缓慢流动)

**对话列表**:
```tsx
function ConversationSidebar() {
  return (
    <motion.aside
      initial={{ x: -20, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      className="w-[280px] h-full glass-base rounded-2xl m-3 flex flex-col"
    >
      {/* 搜索栏 - 玻璃质感 */}
      <div className="p-4 border-b border-white/5">
        <input 
          className="w-full px-4 py-2 rounded-xl bg-white/5 border border-white/10 
                     text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500/50"
          placeholder="搜索对话..."
        />
        <motion.button
          whileHover={{ scale: 1.05, rotate: 90 }}
          whileTap={{ scale: 0.95 }}
          className="mt-3 w-full py-2 rounded-xl font-medium text-white
                     bg-gradient-to-r from-indigo-500 to-purple-500"
        >
          + 新建对话
        </motion.button>
      </div>
      
      {/* 对话列表 */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        <AnimatePresence>
          {conversations.map((conv) => (
            <motion.div
              key={conv.id}
              layout
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, x: -100 }}
              whileHover={{ x: 4, backgroundColor: "rgba(255,255,255,0.05)" }}
              className={`p-3 rounded-xl cursor-pointer transition-colors ${
                isActive ? 'bg-indigo-500/10 border border-indigo-500/20' : ''
              }`}
            >
              <div className="font-medium text-white truncate">{conv.title}</div>
              <div className="text-xs text-gray-500 mt-1">
                {conv.messageCount} 条消息 · {formatDate(conv.updatedAt)}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </motion.aside>
  );
}
```

**聊天区域**:
```tsx
function ChatArea() {
  return (
    <div className="flex-1 relative overflow-hidden rounded-2xl m-3 glass-base">
      {/* 3D 背景层 */}
      <div className="absolute inset-0 pointer-events-none">
        <Particles config={particleConfig} />
      </div>
      
      {/* 消息列表 */}
      <div className="relative h-full overflow-y-auto p-6 space-y-6">
        <AnimatePresence>
          {messages.map((msg) => (
            <motion.div
              key={msg.id}
              variants={messageVariants}
              initial="hidden"
              animate="visible"
              exit="exit"
              className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              <div className={`max-w-[80%] rounded-2xl p-4 ${
                msg.role === 'user' 
                  ? 'bg-gradient-to-br from-indigo-500 to-purple-500 text-white'
                  : 'glass-elevated text-gray-200'
              }`}>
                {msg.role === 'assistant' ? (
                  <StreamingText text={msg.content} />
                ) : (
                  <p>{msg.content}</p>
                )}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
      
      {/* 输入栏 */}
      <div className="absolute bottom-0 left-0 right-0 p-4 glass-frosted border-t border-white/5">
        <div className="flex items-center gap-3">
          <input 
            className="flex-1 px-4 py-3 rounded-xl bg-white/5 border border-white/10 
                       text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500/50"
            placeholder="输入任务..."
          />
          {isRunning && (
            <motion.button
              animate={{ 
                boxShadow: ["0 0 0 0 rgba(245,158,11,0.4)", "0 0 0 10px rgba(245,158,11,0)"]
              }}
              transition={{ duration: 1.5, repeat: Infinity }}
              className="px-4 py-3 rounded-xl font-medium text-white bg-amber-500"
            >
              ⏸ 暂停
            </motion.button>
          )}
          <motion.button
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
            className="px-6 py-3 rounded-xl font-medium text-white 
                       bg-gradient-to-r from-indigo-500 to-purple-500"
          >
            发送
          </motion.button>
        </div>
      </div>
    </div>
  );
}
```

### 6.2 Overview 页面 — "数据星系"

**设计概念**: 将系统数据可视化为一个交互式 3D 星系

**布局**:
```
┌─────────────────────────────────────────────────────────────────────┐
│  Header                                                              │
├─────────────────────────────────────────────────────────────────────┤
│  KPI Cards (4 列玻璃卡片，3D 倾斜效果)                                │
├─────────────────────────────────────────────────────────────────────┤
│  3D 工具星系 (全屏 Canvas，可交互)                                     │
│  - 中心: 总调用数太阳                                                 │
│  - 行星: 各工具，大小=频率，颜色=成功率                                │
│  - 轨道: 失败调用卫星                                                 │
├─────────────────────────────────────────────────────────────────────┤
│  Guardrail 统计 + MCP Server 状态 (底部玻璃卡片)                      │
└─────────────────────────────────────────────────────────────────────┘
```

**3D 工具星系实现**:
```tsx
function ToolGalaxy() {
  const [hoveredTool, setHoveredTool] = useState<string | null>(null);
  
  return (
    <div className="h-[500px] rounded-2xl overflow-hidden glass-base">
      <Canvas camera={{ position: [0, 0, 8], fov: 50 }}>
        <ambientLight intensity={0.2} />
        <pointLight position={[0, 0, 0]} intensity={3} color="#6366F1" />
        
        {/* 中心太阳 */}
        <mesh>
          <sphereGeometry args={[0.8, 64, 64]} />
          <meshStandardMaterial 
            color="#6366F1"
            emissive="#6366F1"
            emissiveIntensity={1}
          />
        </mesh>
        
        {/* 工具行星 */}
        {tools.map((tool, i) => {
          const angle = (i / tools.length) * Math.PI * 2;
          const radius = 2 + (tool.callCount / maxCalls) * 3;
          const position = [
            Math.cos(angle) * radius,
            Math.sin(angle) * radius * 0.3,
            Math.sin(angle) * radius
          ];
          const size = 0.2 + (tool.callCount / maxCalls) * 0.5;
          const successRate = tool.successCount / tool.callCount;
          const color = successRate > 0.8 ? '#10B981' : successRate > 0.5 ? '#F59E0B' : '#EF4444';
          
          return (
            <group key={tool.name} position={position}>
              <mesh
                onPointerOver={() => setHoveredTool(tool.name)}
                onPointerOut={() => setHoveredTool(null)}
              >
                <sphereGeometry args={[size, 32, 32]} />
                <meshStandardMaterial 
                  color={color}
                  emissive={color}
                  emissiveIntensity={hoveredTool === tool.name ? 0.5 : 0.2}
                />
              </mesh>
              
              {/* 失败卫星 */}
              {tool.failureCount > 0 && (
                <mesh position={[size + 0.3, 0, 0]}>
                  <sphereGeometry args={[0.1, 16, 16]} />
                  <meshStandardMaterial color="#EF4444" emissive="#EF4444" />
                </mesh>
              )}
              
              {/* HTML 标签 */}
              {hoveredTool === tool.name && (
                <Html distanceFactor={10}>
                  <div className="glass-elevated rounded-lg p-3 text-white text-sm whitespace-nowrap">
                    <div className="font-bold">{tool.name}</div>
                    <div className="text-gray-400 text-xs">
                      {tool.callCount} calls · {Math.round(successRate * 100)}% success
                    </div>
                  </div>
                </Html>
              )}
            </group>
          );
        })}
        
        <OrbitControls 
          enableZoom={true}
          enablePan={false}
          minDistance={3}
          maxDistance={15}
          autoRotate
          autoRotateSpeed={0.5}
        />
      </Canvas>
    </div>
  );
}
```

### 6.3 History 页面 — "时间河流"

**设计概念**: Run 历史像一条时间河流，每个 Run 是河中的发光石头

**布局**:
```
┌─────────────────────────────────────────────────────────────────────┐
│  Header                                                              │
├─────────────────────────────────────────────────────────────────────┤
│  Filter Bar (玻璃质感按钮组)                                          │
├─────────────────────────────────────────────────────────────────────┤
│  Run Timeline (垂直时间线，左侧发光线)                                 │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  ● Run #1  2026-07-22 14:30  [Completed]  查天气            │    │
│  │    └─ 12 events · 3 tools · 2.3s                           │    │
│  │                                                             │    │
│  │  ● Run #2  2026-07-22 14:32  [Running]  分析数据            │    │
│  │    └─ 8 events · 2 tools · ongoing...                      │    │
│  │                                                             │    │
│  │  ● Run #3  2026-07-22 14:35  [Failed]  搜索信息             │    │
│  │    └─ 5 events · 1 tool · error: timeout                   │    │
│  └─────────────────────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────────────────────┤
│  Run Detail Panel (点击 Run 展开，玻璃质感)                           │
│  - KPI 统计卡片                                                      │
│  - 事件时间线 (可展开 JSON)                                           │
│  - Tool Trace 树状图                                                 │
└─────────────────────────────────────────────────────────────────────┘
```

**时间线实现**:
```tsx
function RunTimeline() {
  return (
    <div className="relative pl-8 space-y-6">
      {/* 发光时间线 */}
      <div className="absolute left-3 top-0 bottom-0 w-0.5 bg-gradient-to-b from-indigo-500 via-purple-500 to-pink-500 opacity-30" />
      
      <AnimatePresence>
        {runs.map((run) => (
          <motion.div
            key={run.id}
            layout
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            className="relative"
          >
            {/* 时间点 */}
            <motion.div
              className={`absolute -left-[22px] w-4 h-4 rounded-full border-2 ${
                run.status === 'completed' ? 'border-emerald-500 bg-emerald-500/20' :
                run.status === 'running' ? 'border-amber-500 bg-amber-500/20' :
                'border-red-500 bg-red-500/20'
              }`}
              animate={run.status === 'running' ? {
                boxShadow: ["0 0 0 0 rgba(245,158,11,0.4)", "0 0 0 8px rgba(245,158,11,0)"]
              } : {}}
              transition={{ duration: 1.5, repeat: Infinity }}
            />
            
            {/* Run 卡片 */}
            <motion.div
              whileHover={{ x: 4 }}
              className="glass-base rounded-xl p-4 cursor-pointer"
              onClick={() => setSelectedRun(run.id)}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="font-mono text-sm text-indigo-400">{run.id.slice(0, 8)}</span>
                  <StatusBadge status={run.status} />
                </div>
                <span className="text-xs text-gray-500">{formatTime(run.createdAt)}</span>
              </div>
              <div className="mt-2 text-white font-medium">{run.intent}</div>
              <div className="mt-2 flex gap-4 text-xs text-gray-500">
                <span>{run.eventCount} events</span>
                <span>{run.toolCount} tools</span>
                <span>{formatDuration(run.duration)}</span>
              </div>
            </motion.div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
```

---

## 7. 响应式设计

### 7.1 断点定义

```css
/* 移动端 */
@media (max-width: 768px) {
  .chat-layout {
    grid-template-columns: 1fr;
  }
  .sidebar { display: none; }
  .realtime-panel { 
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 40vh;
    transform: translateY(100%);
    transition: transform 0.3s ease;
  }
  .realtime-panel.open {
    transform: translateY(0);
  }
}

/* 平板 */
@media (min-width: 769px) and (max-width: 1024px) {
  .sidebar { width: 240px; }
  .realtime-panel { width: 320px; }
}

/* 桌面 */
@media (min-width: 1025px) {
  .sidebar { width: 280px; }
  .realtime-panel { width: 380px; }
}

/* 大屏 */
@media (min-width: 1440px) {
  .sidebar { width: 320px; }
  .realtime-panel { width: 420px; }
}
```

### 7.2 移动端适配

- 对话列表改为抽屉式，从左侧滑入
- 实时执行面板改为底部抽屉，向上滑入
- 3D 效果降级为 2D 动画（性能考虑）
- 粒子数量减少到 30 个
- 禁用自动旋转等耗性能效果

---

## 8. 性能优化

### 8.1 WebGL 性能

| 优化项 | 方法 | 预期效果 |
|--------|------|---------|
| 自适应 DPR | `<AdaptiveDpr pixelSize={2} />` | 低性能设备自动降分辨率 |
| 实例化渲染 | `<Instances>` 组件 | 千级对象渲染性能提升 10x |
| 惰性加载 | 仅当页面可见时初始化 Canvas | 首屏加载时间减少 40% |
| 粒子节流 | 根据 `prefers-reduced-motion` 调整 | 尊重用户偏好 |

### 8.2 动效性能

| 优化项 | 方法 | 预期效果 |
|--------|------|---------|
| GPU 加速 | 仅使用 `transform` 和 `opacity` | 避免 layout 重排 |
| 虚拟列表 | 长消息列表使用虚拟滚动 | 千条消息流畅滚动 |
| 动效降级 | 检测低性能设备，简化动效 | 保证流畅体验 |
| 懒加载 | 非首屏组件动态导入 | 初始 bundle 减少 30% |

### 8.3 Bundle 优化

```typescript
// 动态导入 3D 组件
const ToolGalaxy = lazy(() => import('./components/ToolGalaxy'));
const ParticleBackground = lazy(() => import('./components/ParticleBackground'));

// 仅在 Overview 页面加载
function OverviewPage() {
  return (
    <Suspense fallback={<GlassCard>Loading 3D...</GlassCard>}>
      <ToolGalaxy />
    </Suspense>
  );
}
```

---

## 9. 无障碍设计

### 9.1 键盘导航

- 所有交互元素可通过 Tab 键访问
- 焦点状态有明显的发光边框
- 支持 Escape 关闭浮层
- 支持 Enter/Space 激活按钮

### 9.2 屏幕阅读器

- 所有图标按钮有 `aria-label`
- 状态变化通过 `aria-live` 区域播报
- 3D 内容提供文本替代描述

### 9.3 动效偏好

```css
@media (prefers-reduced-motion: reduce) {
  * {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
  
  .particle-system { display: none; }
  .aurora-background { animation: none; }
}
```

---

## 10. 设计交付物

### 10.1 组件清单

| 组件 | 文件 | 状态 |
|------|------|------|
| GlassCard | `components/ui/GlassCard.tsx` | 待实现 |
| GlowButton | `components/ui/GlowButton.tsx` | 待实现 |
| ParticleBackground | `components/effects/ParticleBackground.tsx` | 待实现 |
| ToolGalaxy | `components/3d/ToolGalaxy.tsx` | 待实现 |
| StreamingText | `components/chat/StreamingText.tsx` | 待实现 |
| RunTimeline | `components/history/RunTimeline.tsx` | 待实现 |
| ChatPage | `pages/ChatPage.tsx` | 待实现 |
| OverviewPage | `pages/OverviewPage.tsx` | 待实现 |
| HistoryPage | `pages/HistoryPage.tsx` | 待实现 |

### 10.2 设计 Token

```typescript
// design-tokens.ts
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
  // ... 完整色板见 3.1
};

export const spacing = {
  xs: '4px',
  sm: '8px',
  md: '16px',
  lg: '24px',
  xl: '32px',
  xxl: '48px',
};

export const radii = {
  sm: '8px',
  md: '12px',
  lg: '16px',
  xl: '24px',
  full: '9999px',
};

export const shadows = {
  nebula: '0 0 0 1px rgba(99, 102, 241, 0.1), 0 4px 16px rgba(99, 102, 241, 0.15), 0 8px 32px rgba(0, 0, 0, 0.4)',
  glow: '0 0 20px rgba(99, 102, 241, 0.3), 0 0 40px rgba(99, 102, 241, 0.1)',
  float: '0 20px 60px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.05)',
};
```

---

## 11. 实施路线图

| 阶段 | 内容 | 工期 |
|------|------|------|
| **Phase 1: 基础** | 设计 Token + GlassCard + GlowButton + 色彩系统 | 2 天 |
| **Phase 2: 动效** | Motion 集成 + 页面转场 + 消息动画 | 2 天 |
| **Phase 3: 特效** | 粒子系统 + 极光背景 + 3D 组件 | 3 天 |
| **Phase 4: 页面** | Chat/Overview/History 三页面实现 | 4 天 |
| **Phase 5: 优化** | 性能优化 + 响应式 + 无障碍 | 2 天 |
| **Phase 6: 测试** | 视觉回归测试 + 用户测试 | 2 天 |

**总计**: 15 个工作日

---

*本设计文档追求艺术与功能的平衡。每一处视觉设计都服务于用户体验，而非纯装饰。实现时遵循 AGENTS.md 的前端规范，保持代码质量。*
