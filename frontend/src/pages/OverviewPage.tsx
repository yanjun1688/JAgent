import { useState } from 'react'
import { motion } from 'motion/react'
import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  CheckCircle2,
  PauseCircle,
  PlayCircle,
  XCircle,
  Wrench,
  ShieldAlert,
  Gauge,
  Orbit,
} from 'lucide-react'
import { getDashboard, getToolStats, getGuardrailStats } from '../api/analysis-client'
import { GlassCard } from '../components/ui/GlassCard'
import { StatusBadge } from '../components/ui/StatusBadge'
import { KPICard } from '../components/overview/KPICard'
import { ToolGalaxy } from '../components/overview/ToolGalaxy'

export default function OverviewPage() {
  const [hoveredTool, setHoveredTool] = useState<string | null>(null)

  const { data: dashboardData, isLoading } = useQuery({
    queryKey: ['analysis', 'dashboard'],
    queryFn: () => getDashboard(),
  })

  const { data: toolData } = useQuery({
    queryKey: ['analysis', 'tools'],
    queryFn: () => getToolStats(),
  })

  const { data: guardrailData } = useQuery({
    queryKey: ['analysis', 'guardrails'],
    queryFn: () => getGuardrailStats(),
  })

  const overview = dashboardData?.overview
  const tools = toolData?.tools ?? []
  const guardrails = guardrailData?.guardrails ?? []

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="mx-auto max-w-7xl space-y-4"
      >
        <div className="flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-accent-secondary/20 text-accent-secondary">
            <Gauge size={18} />
          </span>
          <div>
            <h1 className="font-display text-xl font-bold text-text-primary">系统概览</h1>
            <p className="text-xs text-text-tertiary">Live KPI · 工具星系 · 护栏统计</p>
          </div>
        </div>

        {/* KPI 卡片 */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4">
          <KPICard
            label="总 Run 数"
            value={overview?.total_runs ?? '—'}
            icon={Activity}
            accent="primary"
            isLoading={isLoading}
          />
          <KPICard
            label="运行中"
            value={overview?.running_runs ?? 0}
            icon={PlayCircle}
            accent="info"
          />
          <KPICard
            label="已暂停"
            value={overview?.paused_runs ?? 0}
            icon={PauseCircle}
            accent="warning"
          />
          <KPICard
            label="已完成"
            value={overview?.completed_runs ?? 0}
            icon={CheckCircle2}
            accent="success"
          />
          <KPICard
            label="失败"
            value={overview?.failed_runs ?? 0}
            icon={XCircle}
            accent="error"
          />
          <KPICard
            label="工具调用数"
            value={overview?.total_tool_calls ?? 0}
            icon={Wrench}
            accent="secondary"
          />
          <KPICard
            label="护栏触发"
            value={overview?.total_guardrail_triggers ?? 0}
            icon={ShieldAlert}
            accent="tertiary"
          />
          <KPICard
            label="平均成功率"
            value={
              overview != null
                ? `${(overview.avg_tool_success_rate * 100).toFixed(1)}%`
                : '—'
            }
            icon={Gauge}
            accent="success"
          />
        </div>

        {/* 工具星系 + 表格 */}
        <div className="grid gap-3 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <GlassCard className="p-3">
              <div className="mb-2 flex items-center gap-2">
                <Orbit size={16} className="text-accent-secondary" />
                <h2 className="font-display text-sm font-semibold text-text-primary">
                  工具星系
                </h2>
                <span className="ml-auto text-[10px] text-text-muted">
                  鼠标拖动旋转 · 点击工具聚焦
                </span>
              </div>
              {tools.length === 0 ? (
                <div className="flex h-[420px] items-center justify-center text-sm text-text-muted">
                  暂无工具调用
                </div>
              ) : (
                <ToolGalaxy tools={tools} onToolClick={setHoveredTool} />
              )}
              {hoveredTool && (
                <p className="mt-2 px-2 text-xs text-accent-primary">
                  已聚焦: <span className="font-mono">{hoveredTool}</span>
                </p>
              )}
            </GlassCard>
          </div>

          <GlassCard className="p-3">
            <h2 className="mb-2 font-display text-sm font-semibold text-text-primary">
              工具调用排行
            </h2>
            <div className="max-h-[420px] space-y-1 overflow-y-auto">
              {tools.length === 0 && (
                <p className="py-6 text-center text-xs text-text-muted">无数据</p>
              )}
              {tools.map((t) => {
                const total = t.call_count || 0
                const failed = (t.failure_count || 0) + (t.timeout_count || 0)
                return (
                  <div
                    key={t.tool_name}
                    className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs hover:bg-surface-1"
                  >
                    <span className="truncate font-mono text-text-secondary">
                      {t.tool_name}
                    </span>
                    <span className="ml-auto text-text-tertiary">{total} 次</span>
                    {failed > 0 && (
                      <span className="rounded bg-status-error/15 px-1 text-[10px] text-status-error">
                        {failed} 失败
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          </GlassCard>
        </div>

        {/* 护栏统计 */}
        <GlassCard className="p-3">
          <div className="mb-2 flex items-center gap-2">
            <ShieldAlert size={16} className="text-status-error" />
            <h2 className="font-display text-sm font-semibold text-text-primary">
              护栏触发
            </h2>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {guardrails.length === 0 && (
              <p className="py-6 text-center text-xs text-text-muted">无护栏触发记录</p>
            )}
            {guardrails.map((g) => (
              <div
                key={g.guardrail_id}
                className="rounded-xl border border-border-soft bg-surface-2 px-3 py-2 text-xs"
              >
                <div className="flex items-center gap-2">
                  <StatusBadge status="failed" />
                  <span className="font-mono text-text-secondary">
                    {g.guardrail_id}
                  </span>
                  <span className="ml-auto font-semibold text-text-primary">
                    {g.trigger_count} 次
                  </span>
                </div>
                {g.recent_reason && (
                  <p className="mt-1.5 text-text-tertiary">原因: {g.recent_reason}</p>
                )}
                {g.tools_affected.length > 0 && (
                  <p className="mt-1 text-text-muted">
                    影响: {g.tools_affected.join(', ')}
                  </p>
                )}
              </div>
            ))}
          </div>
        </GlassCard>

        {overview && (
          <p className="text-center text-[10px] text-text-muted">
            事件总数 {overview.total_events} · token 累计 {overview.total_tokens_consumed}
          </p>
        )}
      </motion.div>
    </div>
  )
}