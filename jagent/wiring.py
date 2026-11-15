"""JAgent v8 · 唯一装配处。

所有依赖在这里注入；其它模块不 new 具体实现。
测试与 demo 都从这里拿一个完全装好的 App。

多对话（D-034）：**每个对话一个会话栈**（Writer / Executor / Human / Runner /
Session / LLM 组件视图），共享底座（store / artifacts / registry / gate）与
判定生成组件的**配置**。``App.stack_for(sid)`` 惰性装配；缺省 ``s_main``
向后兼容所有既有调用方。

LLM 组件按栈装配的原因：`model.called` 观测视图带**本栈 writer**——
跨会话共享组件会把所有栈的调用都打到缺省会话的账上。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters.mcp import McpServer, register_mcp_tools
from .agent.planner import UnavailablePlanner
from .agent.router import AskRouter
from .app.runner import Runner
from .app.session import Session
from .config import Config, load_config, runtime_env
from .contracts import Budget
from .gate.rules import AdviceGate
from .harness.artifacts import ArtifactStore
from .harness.executor import Executor
from .harness.fold import World, fold
from .harness.human import HumanCenter
from .harness.registry import build_default_registry
from .harness.repair import HeuristicRepairer
from .harness.report import Reporter
from .harness.store import EventStore
from .harness.verify import Verifier
from .harness.writer import Writer

SESSION_ID = "s_main"


@dataclass
class SharedComponents:
    """跨会话共享的底座：存储 / 工具 / 合规门 / 配置——无 per-会话状态。

    LLM 组件（router / planner / propose / repairer）**不**在这里：它们持有
    观测视图（带本栈 writer），按栈装配——否则 `model.called` 全部打到
    缺省会话的账上（D-034 review 修正）。
    """

    store: EventStore
    artifacts: ArtifactStore
    registry: Any
    workspace: Path
    root: Path
    verifier: Verifier
    reporter: Reporter
    gate: Any
    judge: Any
    model: Any
    config: Config
    budget: Budget
    env: dict[str, str]
    #: 显式注入的 router/planner/propose（测试/特殊装配）：全部栈共享，
    # 优先于按栈的 LLM 装配；None = 有模型用 LLM、无模型用诚实降级。
    router_param: Any = None
    planner_param: Any = None
    propose_param: Any = None
    #: 显式注入的 repairer（测试/特殊装配）：全部栈共享它，不再按栈观测。
    repairer_override: Any = None
    #: 显式注入的收尾总结器（D-042）：None = 有模型就按栈装配、没模型就没有总结。
    summarizer: Any = None


@dataclass
class SessionStack:
    """一个对话的执行组件集（事件按这里的 session_id 打戳）。"""

    session_id: str
    writer: Writer
    executor: Executor
    human: HumanCenter
    runner: Runner
    session: Session

    def rebind(self, world: World) -> None:
        """reload 后重绑投影：writer.world 是 Session/Runner/Executor 的世界入口。"""
        self.writer.world = world


@dataclass
class App:
    store: EventStore
    world: World
    shared: SharedComponents
    registry: Any
    verifier: Verifier
    reporter: Reporter
    root: Path
    stacks: dict[str, SessionStack] = field(default_factory=dict)
    #: 内容寻址产物库（缺口 5：advice/report 全文落盘后按 artifact_id 取回）。
    artifacts: Any = None
    #: 本次装配注册进来的 MCP 工具名（空 = 没接 MCP）。只用于观察，不参与判定。
    mcp_tools: tuple[str, ...] = ()

    # ------------------------------------------------------------ 会话管理 --
    def stack_for(self, session_id: str | None = None) -> SessionStack:
        """取（或惰性装配）一个会话栈；缺省 ``s_main``（向后兼容）。"""
        sid = session_id or SESSION_ID
        if sid not in self.stacks:
            self.stacks[sid] = self._build_stack(sid)
        return self.stacks[sid]

    def session_for(self, session_id: str | None = None) -> Session:
        """取（或惰性装配）一个对话的 Session；缺省 ``s_main``。"""
        return self.stack_for(session_id).session

    def create_session(self) -> str:
        """新建一个对话，返回 session_id。首条消息落事件前不出现在列表里
        （fold 惰性建态）——创建即注册组件栈，不落任何事件。"""
        session_id = "s_" + uuid.uuid4().hex[:8]
        self.stack_for(session_id)
        return session_id

    def _build_stack(self, session_id: str) -> SessionStack:
        shared = self.shared
        writer = Writer(shared.store, self.world, session_id)
        executor = Executor(shared.registry, shared.artifacts, writer, workspace=shared.workspace)
        human = HumanCenter(writer)

        # 可观测性视图按栈发：model.called 落对会话（D-031/D-034）。
        def observed(purpose: str) -> Any:
            from .harness.observability import ObservationModel

            return ObservationModel(
                shared.model, purpose=purpose, writer=writer, artifacts=shared.artifacts
            )

        # LLM 组件按栈装配（无跨会话状态）；显式注入的实例优先（测试/特殊装配）。
        repairer = shared.repairer_override
        router = shared.router_param
        planner = shared.planner_param
        propose = shared.propose_param
        if shared.model is not None:
            if repairer is None:
                from .agent.llm_repair import LLMRepairer

                repairer = LLMRepairer(observed("repair"), config=shared.config)
            if router is None:
                try:
                    from .agent.llm_router import LLMRouter

                    router = LLMRouter(observed("router"), config=shared.config)
                except ImportError:
                    router = AskRouter()
            if planner is None:
                try:
                    from .agent.llm_planner import LLMPlanner

                    planner = LLMPlanner(observed("planner"), shared.registry, config=shared.config)
                except ImportError:
                    planner = UnavailablePlanner()
            if propose is None:
                try:
                    from .agent.propose import Propose

                    propose = Propose(observed("propose"), config=shared.config)
                except ImportError:  # pragma: no cover - 同包模块，正常不会缺
                    propose = None
        if router is None:
            router = AskRouter()
        if planner is None:
            planner = UnavailablePlanner()
        if repairer is None:
            repairer = HeuristicRepairer()

        # 收尾总结（D-042）：有模型才有总结；观测视图带本栈 writer，
        # 日志/事件里能看到 purpose=reporter 的那次调用。
        summarizer = shared.summarizer
        if summarizer is None and shared.model is not None:
            from .agent.summarize import LLMSummarizer

            summarizer = LLMSummarizer(observed("reporter"), config=shared.config)

        runner = Runner(
            writer=writer, executor=executor, verifier=shared.verifier,
            repairer=repairer, reporter=shared.reporter,
            human=human, budget=shared.budget, registry=shared.registry,
            auto_approve=frozenset(shared.budget.auto_approve_tools),
            env=shared.env, artifacts=shared.artifacts, summarizer=summarizer,
        )
        session = Session(
            writer=writer,
            router=router,
            gate=shared.gate,
            planner=planner,
            propose=propose,
            runner=runner,
            human=human,
            budget=shared.budget,
            workspace=shared.workspace,
            registry=shared.registry,
            fallback_router=AskRouter(),
            session_id=session_id,
            artifacts=shared.artifacts,
            root=shared.root,
        )
        return SessionStack(
            session_id=session_id, writer=writer, executor=executor,
            human=human, runner=runner, session=session,
        )

    # ---------------------------------------------------------- 兼容属性 --
    @property
    def writer(self) -> Writer:
        return self.stack_for().writer

    @property
    def executor(self) -> Executor:
        return self.stack_for().executor

    @property
    def human(self) -> HumanCenter:
        return self.stack_for().human

    @property
    def runner(self) -> Runner:
        return self.stack_for().runner

    @property
    def session(self) -> Session:
        return self.stack_for().session

    @property
    def repairer(self) -> Any:
        return self.stack_for().runner.repairer

    # -------------------------------------------------------------- 生命周期 --
    def reload(self) -> World:
        """从事件日志重建世界（重放）。零模型调用；所有会话栈重绑投影。"""
        self.world = fold(self.store.read())
        for stack in self.stacks.values():
            stack.rebind(self.world)
        return self.world

    def close(self) -> None:
        self.store.close()


def build_app(
    root: str | Path,
    *,
    budget: Budget | None = None,
    repairer: Any = None,
    router: Any = None,
    planner: Any = None,
    gate: Any = None,
    judge: Any = None,
    model: Any = None,
    propose: Any = None,
    mcp_servers: Sequence[McpServer] | None = None,
    config: Config | None = None,
    summarizer: Any = None,
) -> App:
    """装配根。

    `model` 为空 => 诚实降级装配：不猜意图（AskRouter），不拆计划（UnavailablePlanner）。
    `model` 非空 => Router / Planner 换成 LLM 实现（模型起草、代码校验），Harness 一行不改。
    `judge` 是 T2 语义评审（`JudgePort`）：**接真实模型时必须注入**，否则启动即报错；
    缺 judge 时语义违规的建议会被静默投递，这是不能默认接受的边界。
    `mcp_servers` 非空 => 把 MCP 服务暴露的工具注册进 ToolRegistry（工具层），
    planner 通过 descriptors 看见它们，executor 通过 registry.call 调用它们。
    `summarizer` 是收尾总结器（D-042）：不传时「有模型就按栈装配一个」，
    传 None 且无模型 = 没有总结能力（模板摘要）；显式传入时用注入的实例（测试/特殊装配）。

    配置（prompt / budget）在**这里**读一次并注入各组件（宪法 I6）：
    显式传 ``config`` 时用它（测试/替换配置树），否则读包内 ``jagent/config/``。
    缺文件或缺字段直接抛错，没有"用代码里的默认值继续跑"的分支。

    多对话（D-034）：判定与生成的**配置**跨会话共享；LLM 组件按栈装配
    （观测视图带本栈 writer）；每会话组件栈由 ``App.stack_for(sid)`` 惰性装配，
    缺省 ``s_main``。
    """
    root = Path(root)
    config = config if config is not None else load_config()
    workspace = root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    store = EventStore(root / "events.db")
    world = fold(store.read())
    artifacts = ArtifactStore(root / "artifacts")
    registry = build_default_registry(workspace)
    # MCP 是工具层的外部工具提供方：注册进同一个 registry，不另开调用通道。
    mcp_tools: tuple[str, ...] = ()
    if mcp_servers:
        mcp_tools = tuple(register_mcp_tools(registry, mcp_servers))
    verifier = Verifier()
    reporter = Reporter()
    budget = budget or config.budget
    #: 本次启动的代码/配置指纹：算一次，注入 Runner；每个 run 直接抄进 run.started。
    #: 重放只读事件里的这份 fingerprint，不再碰配置文件。
    env = runtime_env(config)

    # `NoModel` 是「没接模型」的哨兵：传进来等同于没传，不能因此装上 LLM 组件。
    from .ports import NoModel

    if isinstance(model, NoModel):
        model = None

    # 可观测性（D-031）：S3 Langfuse 导出后端待实现——账号已配
    # （.env LANGFUSE_*，当前 LANGFUSE_ENABLED=false）；实现时按 OTel GenAI
    # 语义规范命名（gen_ai.request.model / gen_ai.usage.*），把 turn/run/model
    # 三级 Span 导出到 Langfuse（支持 OTLP）。S4 可观测性注入
    # （observability.get_trace 只读工具，路线图 14 §5）同样后置。

    # 修复器：没接模型仍用启发式（只翻倍 timeout）；接了模型才让模型提计划。
    # 显式注入的 repairer 优先（测试 / 特殊装配），按栈装配时不被覆盖。
    repairer_override = repairer

    # 接模型：Router / Planner 换成 LLM 实现；不接模型则用诚实降级
    # （不猜意图、不拆计划），而不是回到模板场景层。
    #: 真实模型 + 没有 judge：建议的语义评审会永久缺席，违规内容被静默投递。
    #: 这不是可降级的配置，启动时直接失败（C1 / D-018）。
    effective_judge = judge if gate is None else getattr(gate, "_judge", None)
    if model is not None and effective_judge is None:
        raise RuntimeError(
            "接入了真实模型但没有注入 judge：advice 的语义评审缺失，"
            "违规建议会被静默投递。请传入 judge（例如 "
            "jagent.agent.llm_judge.LLMJudge(model)），或显式注入带 judge 的 gate。"
        )

    shared = SharedComponents(
        store=store, artifacts=artifacts, registry=registry, workspace=workspace,
        root=root, verifier=verifier, reporter=reporter,
        gate=gate or AdviceGate(judge=judge, artifact_reader=artifacts),
        judge=judge, model=model,
        router_param=router, planner_param=planner, propose_param=propose,
        config=config, budget=budget, env=env,
        repairer_override=repairer_override, summarizer=summarizer,
    )
    app = App(
        store=store, world=world, shared=shared, registry=registry,
        verifier=verifier, reporter=reporter, root=root,
        artifacts=artifacts, mcp_tools=mcp_tools,
    )
    app.stack_for(SESSION_ID)  # 缺省会话装配（s_main，向后兼容）
    return app
