Bug 1：Planner 未规划用户要求的"回答用户"动作

严重程度

P1（逻辑缺陷）

背景

用户请求：

你好，1+1等于几？告诉我答案，然后把答案写在文件里，再访问一下百度。要全部按顺序来。

用户实际上提出了三个动作：

告诉答案
写入文件
访问百度
实际行为

Planner 输出：

{
  "steps":[
    file_op,
    browser
  ]
}

没有任何一步对应：

告诉我答案

最终答案完全依赖最后 Answer LLM 自动补充。

期望行为

Planner 应完整覆盖用户要求。

回答用户应作为明确的执行目标，而不是依赖 Answer 阶段隐式生成。

影响

复杂任务下可能出现：

Tool 全部执行成功
最终遗漏回答用户
或回答内容与执行内容不一致
定位建议

检查 Planner Prompt 是否允许忽略 Conversation Action。

Bug 2：Planner 执行了推理，而不是规划

严重程度

P1

背景

Planner 应只生成执行计划。

实际行为

Planner 输出：

{
    "content":"2"
}

说明 Planner 自己计算出了：

1+1=2
期望行为

Planner 不应承担推理任务。

计算结果应来自：

LLM执行阶段
Tool执行阶段
Data Flow

而不是 Planner。

影响

复杂任务中 Planner 将越来越承担执行职责，导致：

Planner 与 Executor 职责混乱
Data Flow 无法工作
定位建议

检查 Planner Prompt 是否允许 Planner 直接生成最终内容。

Bug 3：Data Flow 未生效

严重程度

P1

背景

Planner Prompt 已支持：

$step.field

用于步骤之间传递数据。

实际行为

Planner：

content:"2"

而不是：

$s1.answer

或者其它变量引用。

期望行为

后续步骤应引用前一步输出，而不是写死结果。

影响

复杂任务：

搜索
↓

总结
↓

写文件

无法形成真正的数据流。

定位建议

检查 Planner 是否学习到 Data Flow 规则。

Bug 4：Revise Loop 未真正执行

严重程度

P2

背景

日志显示：

Plan-Execute-Revise loop START

Browser 返回：

SOFT_ERROR
实际行为

流程：

Plan

↓

Execute

↓

Answer

没有任何 Revise。

期望行为

收到 Soft Error 后，应至少进入一次 Revise。

例如：

分析失败

↓

判断是否需要重试

↓

决定继续

即使最终决定：

Skip Retry

也应留下 Revise 记录。

影响

日志与真实执行流程不一致。

后续排查 Retry 问题困难。

定位建议

检查 Soft Error 是否直接进入 Answer。

Bug 5：Answer 阶段缺少执行上下文

严重程度

P2

背景

Answer LLM 负责生成最终回复。

实际输入
[file_op]

success

[browser]

error

没有：

文件名
URL
Step ID
输入参数
执行顺序
实际输出
已经写入文件

Answer LLM 无法确认：

写到哪个文件
写了什么
浏览器访问哪个地址
期望行为

Answer Context 至少包含：

Step

Tool

Input

Output

Status
影响

最终回答准确性下降。

定位建议

检查 Tool Result Summary。

Bug 6：Tool Result 信息缺失

严重程度

P2

背景

Tool 返回：

{
success:true,
size:1
}
实际行为

缺少：

path
content
duration
input
output summary
期望行为

Tool Result 至少能够支持最终 Summary。

影响

Answer LLM 无法准确描述执行结果。

定位建议

扩展 ToolCompleted Result。

Bug 7：Planner 未体现"严格顺序执行"语义

严重程度

P3

背景

用户明确要求：

要全部按顺序来

Planner Prompt 支持：

dynamic=true
实际行为

Planner 未输出：

dynamic:true

虽然 DAG 通过 depends_on 保证了顺序，但没有显式表达"严格串行"语义。

期望行为

严格顺序任务应标记：

dynamic:true

或其它等效标识。

影响

当前案例无功能错误，但可能影响后续执行策略。

定位建议

检查 Planner 是否正确使用 dynamic 字段。

Bug 8：Planner 耗时异常

严重程度

P3（性能）

背景

Planner 输入：

约 5 KB Prompt。

输出：

约 450 Byte JSON。

实际行为

Planning：

33.3 秒
期望行为

简单任务 Planning 应远低于当前耗时。

影响

简单请求整体响应时间过长。

定位建议

检查：

Planner Prompt 长度
Tool Schema 注入方式
LLM 推理耗时
根据D:\Project\JAgent\data\logs\harness.log日志观察得出。   