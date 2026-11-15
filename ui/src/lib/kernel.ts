/**
 * v8 vocabulary, mirrored from the Python contract.
 *
 * The UI never invents a status word: run status comes from `/runs/{id}`,
 * step status comes from `run.steps`, and event types come from the log.
 * These helpers only translate those words into human labels and lamp tones.
 */

export type Tone = 'off' | 'ok' | 'warn' | 'bad'

const RUN_STATUS_LABEL: Record<string, string> = {
  planning: '准备中',
  running: '执行中',
  needs_input: '等你补充',
  waiting_human: '等待你确认',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const RUN_STATUS_TONE: Record<string, Tone> = {
  planning: 'warn',
  running: 'ok',
  needs_input: 'warn',
  waiting_human: 'warn',
  completed: 'ok',
  failed: 'bad',
  cancelled: 'off',
}

export function runStatusLabel(status: string | null | undefined): string {
  if (!status) return '空闲'
  return RUN_STATUS_LABEL[status] ?? status
}

export function runStatusTone(status: string | null | undefined): Tone {
  if (!status) return 'off'
  return RUN_STATUS_TONE[status] ?? 'off'
}

const STEP_STATUS_LABEL: Record<string, string> = {
  pending: '待执行',
  running: '执行中',
  passed: '已通过',
  failed: '失败',
  skipped: '已跳过',
  stale: '需要重跑',
}

const STEP_STATUS_TONE: Record<string, Tone> = {
  pending: 'off',
  running: 'warn',
  passed: 'ok',
  failed: 'bad',
  skipped: 'off',
  stale: 'warn',
}

export function stepStatusLabel(status: string | null | undefined): string {
  if (!status) return '未知'
  return STEP_STATUS_LABEL[status] ?? status
}

export function stepStatusTone(status: string | null | undefined): Tone {
  if (!status) return 'off'
  return STEP_STATUS_TONE[status] ?? 'off'
}

const EVENT_LABEL: Record<string, string> = {
  'turn.received': '收到一条输入',
  'intent.routed': '理解你的意图',
  'intent.confirmed': '意图已确认',
  'clarify.asked': '向你提问',
  'slot.updated': '补充了信息',
  'turn.ended': '这一轮结束',
  'plan.proposed': '生成执行计划',
  'plan.rejected': '计划没有通过检查',
  'gate.checked': '合规检查',
  'plan.frozen': '计划已锁定',
  'plan.amended': '调整计划',
  'step.state_changed': '步骤状态变化',
  'step.started': '开始执行步骤',
  'step.finished': '步骤执行完成',
  'artifact.written': '写入产物',
  'step.verified': '步骤校验完成',
  'env.fingerprinted': '记录环境指纹',
  'run.transition_rejected': '拒绝了一次非法状态变更',
  'failure.classified': '归类失败原因',
  'budget.consumed': '消耗额度',
  'approval.requested': '等待你确认',
  'approval.received': '收到你的决定',
  'approval.resolved': '确认已生效',
  'run.suspended': '暂停等待',
  'run.resumed': '继续执行',
  'step.invalidated': '步骤需要重跑',
  'run.started': '任务开始',
  'run.completed': '任务完成',
  'run.failed': '任务失败',
  'report.rendered': '整理结果',
  'advice.delivered': '给出建议',
}

const EVENT_TONE: Record<string, Tone> = {
  'plan.rejected': 'bad',
  'step.failed': 'bad',
  'failure.classified': 'bad',
  'run.failed': 'bad',
  'approval.requested': 'warn',
  'run.suspended': 'warn',
  'step.invalidated': 'warn',
  'clarify.asked': 'warn',
  'run.completed': 'ok',
  'step.verified': 'ok',
  'approval.resolved': 'ok',
}

export function eventLabel(type: string): string {
  return EVENT_LABEL[type] ?? '过程记录'
}

export function eventTone(type: string): Tone {
  return EVENT_TONE[type] ?? 'off'
}

const ERROR_LABEL: Record<string, string> = {
  not_found: '没找到',
  permission_denied: '没有权限',
  command_not_found: '命令不存在',
  invalid_args: '参数不对',
  sandbox_denied: '被沙箱拒绝',
  timeout: '超时',
  rate_limited: '被限流',
  upstream_5xx: '上游服务出错',
  connection_reset: '连接被重置',
  tool_exception: '工具异常',
  assertion_failed: '校验没通过',
  transient: '临时故障',
  assertion: '校验没通过',
  deterministic: '确定性失败',
  rejected_by_human: '你拒绝了这一步',
  wall_clock_exceeded: '超过时间预算',
  step_budget_exceeded: '超过步骤预算',
}

function readableReason(reason: string): string {
  const [head, ...rest] = reason.split(':')
  const label = ERROR_LABEL[head ?? '']
  if (!label) return reason
  return rest.length > 0 ? `${label}（${rest.join(':')}）` : label
}

/** One readable line for the event rows; never exposes a token or an id hash. */
export function eventDetail(type: string, payload: Record<string, unknown>): string {
  const step = typeof payload.step_id === 'string' ? payload.step_id : ''
  const to = typeof payload.to === 'string' ? payload.to : ''
  const reason = typeof payload.reason === 'string' ? payload.reason : ''

  if (type === 'step.state_changed') {
    return [step, stepStatusLabel(to)].filter(Boolean).join(' · ') || '步骤'
  }
  if (type === 'step.verified') {
    const result = payload.result === 'pass' ? '通过' : payload.result === 'fail' ? '未通过' : ''
    return [step, result].filter(Boolean).join(' · ')
  }
  if (type === 'step.started' || type === 'step.finished') {
    return step || '步骤'
  }
  if (type === 'approval.requested') {
    const what = typeof payload.context === 'string' ? payload.context : ''
    return what || step || '等待确认'
  }
  if (type === 'failure.classified') {
    const code = typeof payload.error_code === 'string' ? payload.error_code : ''
    return [step, ERROR_LABEL[code] ?? code].filter(Boolean).join(' · ')
  }
  if (type === 'run.failed') {
    return reason ? readableReason(reason) : '任务没能完成'
  }
  if (type === 'run.started') {
    return '任务已创建'
  }
  if (type === 'run.completed') {
    return '所有步骤已收口'
  }
  return step || reason
}
