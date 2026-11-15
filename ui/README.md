# JAgent 控制面板（v8）

一个对话式的任务控制面板：左边和 JAgent 说话，右边看任务状态、步骤和过程。
界面只投影运行时给出的事实，不自己发明运行、步骤或结论。

## 跑起来

后端一个进程同时提供 API 和构建好的面板：

```powershell
cd ..
$env:JAGENT_PORT = "8021"
.venv\Scripts\python.exe -m jagent.api
# 打开 http://127.0.0.1:8021
```

前端开发（Vite 把 `/chat`、`/runs`、`/healthz`、`/api` 代理到 8021）：

```powershell
cd ui
npm install
npm run dev      # http://localhost:5273
```

```powershell
npm run build    # tsc --noEmit + vite build，产物在 ui/dist
```

`jagent/api/app.py` 在所有 API 路由之后挂载 `ui/dist`；没有构建产物时回退到
`jagent/api/static/index.html`。所以先 build 再起服务，才能看到新的面板。

## 面板上有什么

| 区域 | 看什么 |
|---|---|
| 对话（左） | `message` 原样显示；`needs_input` 的 `choices` 是可点按钮，点击会带着上一句话和 `{slot: value}` 再发一轮 |
| 批准闸门 | 显示 `approval.what`（工具 + 参数，等宽原文），批准 / 拒绝都必须先看懂这一条 |
| 过程（右，可折叠） | 当前任务状态、`run.steps` 的逐步状态、`/api/runs/{id}/events` 的增量过程；没有任务时显示空态 |
| 最近任务 | `/api/runs?limit=50` 的倒序列表，点一条可以回看它的状态与事件 |
| 命令条 | 唯一输入框；回车发送，Shift+回车换行 |

默认视角不出现 `attempt_id`、`lease`、`event_id`、`plan_hash` 这类实现词。
需要精确核对的内容（命令、参数、步骤 id）只以等宽原文出现在确认区和详情里。

## 数据从哪来

只有五个出口，全部在 `src/lib/api.ts`：

| 函数 | 后端 |
|---|---|
| `chat(text, answers?)` | `POST /chat` |
| `decide(runId, decision, comment?)` | `POST /runs/{id}/decision` |
| `getRun(runId)` | `GET /runs/{id}` |
| `listRuns(limit)` | `GET /api/runs` |
| `pollEvents(runId, afterSeq)` | `GET /api/runs/{id}/events?after_seq=` |

事件轮询按 `next_seq` 前进，1s 一次；终态之后只再读一次。
事件 payload 由后端原样返回，但审批 token 不会下发。

## 文件

- `src/App.tsx` — 会话状态、run 轮询、批准决策、两栏布局
- `src/components/IntakePanel.tsx` — 对话与选项按钮
- `src/components/ApprovalGate.tsx` — `approval.what` + 批准/拒绝
- `src/components/StreamLoop.tsx` — 事件增量轮询与最近过程
- `src/components/Topology.tsx` — 用 `run.steps` 画步骤状态
- `src/lib/api.ts` — v8 类型与唯一 HTTP 出口
- `src/lib/kernel.ts` — 状态/事件到人话和灯色的翻译
- `src/lib/theme.ts` — 黑白主题（localStorage）

## 视觉

保留原来的终端语言：Geist / Geist Mono、方角、发丝线、扫描线、状态灯和黑白两套
CSS 变量配色。颜色只给状态：绿=通过/完成，黄=进行中/待确认，红=失败，灰=未开始。
