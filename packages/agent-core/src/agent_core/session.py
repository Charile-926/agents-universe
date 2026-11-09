"""会话（session）—— Token 用量跟踪与事件发出。

本模块定义一次对话轮次（或智能体循环运行）所需的全部会话状态：Token 用量
台账、上下文占用、事件队列、中止信号、交互式提问（prompt）管理，以及运行
中用户消息的注入队列。Agent 通过 emit() 把事件放入队列，由 WebSocket 处理
器转发给前端。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

# emit() 等待消费方的最长时间，超时即判定消费方死亡并中止会话。设为常量
# 便于测试调小。
_EMIT_PUT_TIMEOUT_S = 10.0


@dataclass
class SessionEvent:
    """会话发出的一个事件，供 WebSocket 处理器转发。"""
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptOutcome:
    """一次交互式提问的结束结果，按调用方提供的签名区分。

    ``status`` 取值为 ``"answered"``（已回答）| ``"timed_out"``（超时）|
    ``"dismissed"``（已忽略）。
    """
    status: str
    value: str | None = None


class UserSelectionTimeoutError(RuntimeError):
    """提问过期且无人回答。

    继承 RuntimeError，使 request_user_selection 周围捕获 RuntimeError 的
    安全闸门保持原有行为不变。
    """


class UserSelectionAbortedError(RuntimeError):
    """提问等待期间运行被中止。"""


class UserSelectionUnavailableError(RuntimeError):
    """运行是非交互式的，永远不可能有人回答提问。

    与 UserSelectionTimeoutError 同样继承 RuntimeError：request_user_selection
    周围每个捕获 RuntimeError 的安全闸门，都会照常降级到其拒绝/错误路径。
    """


@dataclass
class UserInputEntry:
    """排队等待在下一个步骤边界注入的用户消息。

    API 处理器在 Agent 运行期间每收到一条消息就创建一个条目，预先生成
    ``message_id``（同时充当未来数据库行的主键）。Agent 在步骤边界消费它；
    处理器通过 ``user_message_injected`` 事件持久化，并 resolve
    ``persisted``，使 Agent 只有在消息被可靠存储后才继续。
    """

    message_id: str
    content: str
    attachments: list[dict]
    agent_id: str | None = None
    config_id: str | None = None
    attachment_records: list[dict] | None = None
    persisted: asyncio.Future[bool] | None = None
    consumed: bool = False


class ConversationSession:
    """跟踪一次对话轮次（或智能体循环运行）的状态。"""

    def __init__(
        self,
        conversation_id: str,
        project_id: str,
        user_id: str,
        token_budget: int = 128000,
        tokens_used: int = 0,
        prompt_sink: "ConversationSession | None" = None,
        interactive: bool = True,
    ) -> None:
        self.conversation_id = conversation_id
        self.project_id = project_id
        self.user_id = user_id
        self.token_budget = token_budget
        self.tokens_used = tokens_used
        # 最近一次供应方请求的占用（最后一个用量块的提示+补全），即上下文
        # 计量表显示的内容。与生命周期计费台账 `tokens_used` 相区分。
        self.context_tokens: int = 0
        # 最近一次上报用量的模型窗口（首次调用前为 None）。
        self.context_window: int | None = None

        self._event_queue: asyncio.Queue[SessionEvent | None] = asyncio.Queue(maxsize=1000)
        self._closed = False
        self.abort_event = asyncio.Event()
        # 中止的首因标记（"event_queue_blocked"、"abort" 等），让下游结算
        # 能记录运行为何停止，而不是猜测是停止还是崩溃。以第一次 abort()
        # 为准。
        self.abort_reason: str | None = None
        self._current_message_id: str = str(uuid.uuid4())
        self._pending_prompts: dict[str, asyncio.Future[str]] = {}
        # 每个在途提问的已发出载荷，按 prompt_id 区分。提问只存在于这里和
        # 客户端记忆中——它从不属于消息历史——因此客户端重连（切换会话、
        # 页面刷新）时，传输层会重放这些内容。
        self._pending_prompt_events: dict[str, dict[str, Any]] = {}
        # 被委派（嵌套）的轮次运行自己的会话，需要自己的事件队列；但用户的
        # 回答由 WebSocket 处理器通过会话中*已注册*的会话路由。因此提问注册
        # 在 sink（顶层会话）上，并记录其所属会话，使回答一个提问也能解除
        # 该会话的提问暂停。
        # 是否有能回答提问的客户端连接。无头运行（定时任务、已发布 Agent）
        # 为 False：会话仍会构建并注册，但永远不会有 user_selection_response
        # 到达。
        self.interactive = interactive
        self._prompt_sink: ConversationSession | None = prompt_sink
        self._prompt_owners: dict[str, ConversationSession] = {}
        # 交互式提问台账，按调用方提供的签名（字段键 + 问题）区分。工具层
        # 调用方用它直接返回重复问题已记录的结果，而不是再次弹出对话框，
        # 同时记住已经失败过的提问。
        self._prompt_outcomes: dict[tuple[str, str], PromptOutcome] = {}
        self._prompt_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._prompts_paused = False
        self._user_input_queue: asyncio.Queue[UserInputEntry] = asyncio.Queue(maxsize=20)
        # message_id → 条目，与队列保持同步，以便 O(1) 解析
        self._pending_input_by_id: dict[str, UserInputEntry] = {}

        # 用于 WebSocket 重连同步的实时流式快照。由 forward_events 更新；
        # 新客户端在流式中途连接时，由 WebSocket 处理器读取。
        self.current_streaming_text: str = ""
        # 本轮累积的思考内容（与文本缓冲区生命周期相同）。
        self.current_streaming_thinking: str = ""
        # 用于状态栏/重连同步的粗略轮次阶段
        # （"waiting_model" | "thinking" | "responding" | "running_tool" |
        # "compressing" | "degrading"）。由 API 的 forward_events 更新。
        self.current_turn_phase: str = "waiting_model"
        self.current_tool_calls: list[dict] = []

    def new_message(self) -> str:
        self._current_message_id = str(uuid.uuid4())
        return self._current_message_id

    @property
    def tokens_remaining(self) -> int:
        return self.token_budget - self.tokens_used

    @property
    def budget_exceeded(self) -> bool:
        """累计用量是否已超过展示预算。

        仅作观察用途。Agent 循环不能因为会话已消耗其配置预算就停止工作。
        """
        return self.tokens_used > self.token_budget

    def add_tokens(self, count: int) -> bool:
        """把 Token 数加到用量计数器，并返回预算观察结果。"""
        if count > 0:
            self.tokens_used += count
        return not self.budget_exceeded

    def add_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> bool:
        """累加模型供应方用量，并返回预算观察结果。"""
        total = prompt_tokens + completion_tokens
        if total > 0:
            self.tokens_used += total
        return not self.budget_exceeded

    async def emit(self, event_type: str, **data: Any) -> None:
        """把事件入队，由 WebSocket 处理器转发。

        会阻塞直到消费方排空队列。若消费方停滞超过 ``_EMIT_PUT_TIMEOUT_S``，
        则中止会话，避免没有客户端监听时 Agent 仍永远运行。
        """
        if self._closed:
            return
        try:
            await asyncio.wait_for(
                self._event_queue.put(SessionEvent(type=event_type, data=data)),
                timeout=_EMIT_PUT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            import logging
            logging.getLogger("agent_core.session").warning(
                "Event queue blocked for %s s on %s (conversation=%s, queued=%d) — consumer likely dead, aborting agent",
                _EMIT_PUT_TIMEOUT_S, event_type, self.conversation_id,
                self._event_queue.qsize(),
            )
            self.abort("event_queue_blocked")
        except asyncio.CancelledError:
            # Re-raise so cancellation propagates immediately — swallowing it
            # (as the old combined except did) breaks structured cancellation:
            # the task would keep running until the next is_aborted() check.
            raise

    async def events(self) -> AsyncIterator[SessionEvent]:
        """异步生成器，持续产出事件，直到收到 None 哨兵为止。"""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        """发出事件流结束信号，可安全重复调用。"""
        if self._closed:
            return  # 第一次调用已把哨兵放入队列
        self._closed = True
        # 用 put_nowait 放哨兵——若此时队列已满，说明消费方卡住了；强制
        # 清空队列并推入哨兵，让 forward_events() 能干净退出。流式内容会
        # 保留：最终 stream_end 告诉处理器持久化助手消息，stream_delta 事件
        # 携带其文本——丢掉任何一个都会让整条回复从历史中静默消失。终止性
        # 任务事件（task_completed/task_failed/task_skipped）也保留：它们是
        # 任务行最终状态的唯一来源，丢掉会让该行停在 pending/running，等
        # 处理器的陈旧任务对账标记为失败。user_message_injected 是注入用户
        # 消息数据库行的唯一写入方（处理器消费输入并等待该事件来持久化）
        # ——丢掉它会把用户的话从历史和 LLM 上下文中抹去。image_output/
        # file_output 同样保留：处理器把它们累积进缓冲区并在 stream_end 时
        # 持久化，丢掉会失去回复的附件。user_selection_cancelled 也保留：
        # 超时/被中止的提问仍须到达客户端以便关闭对话框——丢掉会让一个
        # 僵尸提问永远钉在界面上。thinking_delta 也保留：处理器把它累积进
        # 助手行持久化的 `thinking` 列，丢掉会静默截断存储的推理。其他事件
        # 类型（进度/工具更新）则丢弃，它们仅用于界面。
        if self._event_queue.full():
            kept: list[SessionEvent] = []
            while not self._event_queue.empty():
                try:
                    ev = self._event_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if ev is not None and ev.type in (
                    "stream_delta",
                    "stream_end",
                    "task_completed",
                    "task_failed",
                    "task_skipped",
                    "user_message_injected",
                    "image_output",
                    "file_output",
                    "user_selection_cancelled",
                    "thinking_delta",
                ):
                    kept.append(ev)
            # 为下方哨兵留一个槽位，使 forward_events() 仍能退出；保留的
            # 尾部保存消息文本（最新 delta 在最后，stream_end 在最末尾）。
            for ev in kept[-(self._event_queue.maxsize - 1):]:
                try:
                    self._event_queue.put_nowait(ev)
                except asyncio.QueueFull:
                    break
        try:
            self._event_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    # --- 交互式提问台账 ----------------------------------------------------
    #
    # 提问状态只存活一轮（每轮创建一个会话对象），因此台账用于抑制一轮之内
    # 的重复提问。让回答存活得比轮次更久是调用方的职责——应把它持久化为
    # 项目设置。

    def get_prompt_outcome(self, signature: tuple[str, str] | None) -> PromptOutcome | None:
        """返回某个提问签名已记录的结果（若有）。"""
        if not signature:
            return None
        return self._prompt_outcomes.get(signature)

    def record_prompt_outcome(
        self,
        signature: tuple[str, str] | None,
        status: str,
        value: str | None = None,
    ) -> None:
        """记录一次交互式提问的结束方式。

        已回答的条目绝不会被失败覆盖：两个调用方可能并发问同一个问题，
        超时的那个副本不能抹掉用户实际给出的答案。
        """
        if not signature:
            return
        existing = self._prompt_outcomes.get(signature)
        if existing is not None and existing.status == "answered" and status != "answered":
            return
        self._prompt_outcomes[signature] = PromptOutcome(status=status, value=value)

    def prompt_lock(self, signature: tuple[str, str]) -> asyncio.Lock:
        """用于串行化并发问同一问题的调用方的锁。"""
        lock = self._prompt_locks.get(signature)
        if lock is None:
            lock = asyncio.Lock()
            self._prompt_locks[signature] = lock
        return lock

    def note_prompt_timeout(self) -> None:
        """提问过期且无人回答——本轮剩余时间不再发起提问。"""
        self._prompts_paused = True

    @property
    def interactive_prompts_paused(self) -> bool:
        """是否因无人回答的提问而暂停了后续交互式提问。"""
        return self._prompts_paused

    def _note_user_present(self) -> None:
        """记录用户再次在场（回答了提问，或运行中发了消息）：解除超时暂停，
        让失败过的问题能再问一次。已给出的回答仍保留——重复问那些问题，
        正是本台账要阻止的事。"""
        self._prompts_paused = False
        self._prompt_outcomes = {
            sig: outcome
            for sig, outcome in self._prompt_outcomes.items()
            if outcome.status == "answered"
        }

    def _prompt_store(self) -> "ConversationSession":
        """拥有提问记账的会话：本会话，或向上找到的 sink。

        被委派轮次的会话刻意不向连接管理器注册（顶层轮次拥有该注册），
        WebSocket 处理器通过会话中已注册的会话解析回答。把提问注册到 sink
        上，才让被委派 Agent 提出的问题有可能被回答。
        """
        store = self
        while store._prompt_sink is not None:
            store = store._prompt_sink
        return store

    def _note_owner_present(self, prompt_id: str) -> None:
        """当本会话持有该提问时，解除提问方的暂停。"""
        owner = self._prompt_owners.get(prompt_id)
        if owner is not None and owner is not self:
            owner._note_user_present()

    async def request_user_selection(
        self,
        prompt_id: str,
        field_key: str,
        question: str,
        options: list[dict] | None = None,
        allow_other: bool = True,
        timeout: float = 300.0,
        *,
        kind: str = "selection",
        title: str | None = None,
        message: str | None = None,
        secret: bool = False,
        task_id: str | None = None,
        service_key: str | None = None,
        environment: str | None = None,
        save_to_project_secrets: bool = False,
        save_to_user_tokens: bool = False,
    ) -> str:
        """发出选择/输入提问事件，并等待用户响应。

        会阻塞 Agent，直到前端回传 user_selection_response 消息。

        参数
        ----------
        kind : "selection" | "text"
            "selection" 显示单选/下拉选项（旧的默认方式）。
            "text" 显示自由文本输入框。
        secret : bool
            为 True 时，前端显示密码输入框，响应在服务端处理：明文永远不会
            到达 LLM。Future 解析为一个不透明的确认字符串（如
            "secret_saved"），而非真实密钥值。
        save_to_project_secrets : bool
            当 secret=True 时，指示 WebSocket 处理器在解析 Future 前，把值
            持久化为项目级密钥。
        save_to_user_tokens : bool
            当 secret=True 时，指示 WebSocket 处理器在解析 Future 前，把值
            持久化为用户级令牌（user_tokens 表）。与
            save_to_project_secrets 互斥。
        service_key / environment : str | None
            与 save_to_project_secrets / save_to_user_tokens 配合使用，用于
            标识密钥槽位。
        task_id : str | None
            把该提问与运行中的任务关联，便于界面展示。
        title / message : str | None
            提问对话框的可选展示覆盖项。

        超时抛出 UserSelectionTimeoutError，运行被中止抛出
        UserSelectionAbortedError，运行非交互式时抛出
        UserSelectionUnavailableError；三者都继承 RuntimeError。
        """
        store = self._prompt_store()
        # 无头运行（定时任务、已发布 Agent）会构建并注册会话，但没有客户端
        # 连接——永远无人能回答，因此干等 120–300 秒超时只会让本轮停滞。
        # 由"回答方"的交互性决定：提问注册在 sink 存储上，所以无头父级下
        # 的被委派轮次也会被拒绝，而交互式父级仍会回答其委派的提问。
        if not store.interactive:
            raise UserSelectionUnavailableError(
                "interactive prompts are disabled in this run — the agent is "
                "operating unattended; do not ask the user for input, pick "
                "the safe default and continue"
            )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        store._pending_prompts[prompt_id] = fut
        if store is not self:
            store._prompt_owners[prompt_id] = self

        event_data: dict[str, Any] = {
            "prompt_id": prompt_id,
            "field_key": field_key,
            "question": question,
            "kind": kind,
            "allow_other": allow_other,
        }
        if options is not None:
            event_data["options"] = options
        if title is not None:
            event_data["title"] = title
        if message is not None:
            event_data["message"] = message
        if secret:
            event_data["secret"] = True
        if task_id is not None:
            event_data["task_id"] = task_id
        if service_key is not None:
            event_data["service_key"] = service_key
        if environment is not None:
            event_data["environment"] = environment
        if save_to_project_secrets:
            event_data["save_to_project_secrets"] = True
        if save_to_user_tokens:
            event_data["save_to_user_tokens"] = True

        # 在 emit 之前先注册载荷：若客户端恰好在 emit 与此行之间的窗口连接，
        # 会收到一个重放不出任何内容的同步事件，对话框会丢失直到下一次提问。
        store._pending_prompt_events[prompt_id] = event_data
        try:
            await self.emit("user_selection_required", **event_data)
        except BaseException:
            # 这个 emit 是下方 finally 之前唯一的 await；取消若落在此处，会
            # 让提问仍注册在 store 上。对被委派子级，store 是父级会话，其
            # 寿命比子级长——resolve_user_selection 不会弹出载荷（由请求方的
            # finally 处理），因此该泄漏会在每次客户端重连时重放一个无法回答
            # 的对话框。
            store._pending_prompts.pop(prompt_id, None)
            store._pending_prompt_events.pop(prompt_id, None)
            store._prompt_owners.pop(prompt_id, None)
            raise
        # 同时在中止时唤醒：会话一旦中止，前端已关闭该提问的路径（emit 甚至
        # 可能因队列满而失败），否则工具会在一个无人能回答的提问上阻塞满
        # 300 秒超时。
        abort_waiter = asyncio.create_task(self.abort_event.wait())
        try:
            done, _ = await asyncio.wait(
                {asyncio.shield(fut), abort_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            # 提问中途被拆除（被委派轮次达到其超时，或一次停止直接取消了运行）。
            # 此路径既不会运行超时逻辑，也不会运行下方中止分支，因此没有这个
            # 通知，客户端会保留一个无人能回答的对话框。
            try:
                await self.emit(
                    "user_selection_cancelled",
                    prompt_id=prompt_id,
                    field_key=field_key,
                    reason="cancelled",
                )
            except BaseException:
                # 尽力而为——通知绝不能掩盖取消本身。
                pass
            raise
        finally:
            abort_waiter.cancel()
            store._pending_prompts.pop(prompt_id, None)
            store._pending_prompt_events.pop(prompt_id, None)
            store._prompt_owners.pop(prompt_id, None)
        if not done:
            # 客户端必须关闭它仍在为该提问显示的对话框——否则界面会保留一个
            # 永不解析的僵尸提问，之后每次 user_confirm 都在上面再叠一个对话框
            # （即"超时后再对话弹窗不出现/错乱"的症状）。
            await self.emit(
                "user_selection_cancelled",
                prompt_id=prompt_id,
                field_key=field_key,
                reason="timeout",
            )
            raise UserSelectionTimeoutError(
                f"User selection timed out after {timeout:.0f} s for field '{field_key}'"
            )
        if abort_waiter in done:
            await self.emit(
                "user_selection_cancelled",
                prompt_id=prompt_id,
                field_key=field_key,
                reason="aborted",
            )
            raise UserSelectionAbortedError("Session aborted while waiting for user input")
        return fut.result()

    def resolve_user_selection(self, prompt_id: str, value: str) -> bool:
        """解析一个待处理的 user_confirm Future，找到则返回 True。

        对非密钥提问，*value* 是用户选择的选项或文本。对密钥提问，WebSocket
        处理器应改为调用 resolve_user_selection_secret()，使明文永远不到达
        Agent。
        """
        fut = self._pending_prompts.get(prompt_id)
        if fut is not None and not fut.done():
            fut.set_result(value)
            self._note_user_present()
            self._note_owner_present(prompt_id)
            return True
        return False

    def resolve_user_selection_secret(self, prompt_id: str, *, saved: bool = True) -> bool:
        """解析密钥提问，不把明文转发给 Agent。

        由 WebSocket 处理器在密钥已加密并存储之后调用。Agent 收到一个不透明
        的确认字符串，永远拿不到原始值。
        """
        fut = self._pending_prompts.get(prompt_id)
        if fut is not None and not fut.done():
            status = "secret_saved" if saved else "secret_save_failed"
            fut.set_result(status)
            self._note_user_present()
            self._note_owner_present(prompt_id)
            return True
        return False

    def pending_prompt_events(self) -> list[dict[str, Any]]:
        """仍在等待用户输入的提问快照。

        （重新）连接到会话的客户端会收到这些重放，以便恢复对话框：提问的
        Future 只存在于本会话，消息历史中没有它的痕迹，因此丢掉对话框（切换
        会话又回来，或刷新页面）的客户端，否则会显示一个在等待它无法给出的
        回答的会话。
        """
        return [dict(payload) for payload in self._pending_prompt_events.values()]

    def pending_prompt_payload(self, prompt_id: str) -> dict[str, Any] | None:
        """一个待处理提问的事件载荷快照，或 None。

        WebSocket 处理器需要在解析 Future *之前*拿到提问的问题：等待中的
        request_user_selection() 会在其 finally 块弹出载荷，因此
        resolve_user_selection() 之后读到的任何东西都已不存在。
        """
        payload = self._pending_prompt_events.get(prompt_id)
        return dict(payload) if payload is not None else None

    def is_aborted(self) -> bool:
        return self.abort_event.is_set()

    def abort(self, reason: str = "abort") -> None:
        """设置中止事件，并记录首因。"""
        if self.abort_reason is None:
            self.abort_reason = reason
        self.abort_event.set()

    # --- In-flight user input injection ------------------------------------

    def enqueue_user_input(self, entry: UserInputEntry) -> bool:
        """Queue a user message for the agent to consume at its next step
        boundary. Returns False when the queue is full."""
        try:
            self._user_input_queue.put_nowait(entry)
            self._pending_input_by_id[entry.message_id] = entry
            # 用户回到键盘前：未回答的提问可以再问（已给出的回答仍保留）。
            self._note_user_present()
            return True
        except asyncio.QueueFull:
            return False

    def dequeue_user_input(self) -> UserInputEntry | None:
        """非阻塞地弹出下一条待处理用户消息——Agent 在步骤边界轮询，绝不会
        因这一步阻塞主循环。

        条目仍按 message_id 注册，以便 Agent 消费后处理器仍能解析其持久化
        Future（user_message_injected 事件是异步处理的）。
        """
        try:
            return self._user_input_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def has_pending_user_input(self) -> bool:
        return not self._user_input_queue.empty()

    def resolve_input_persisted(
        self,
        message_id: str,
        ok: bool,
        attachment_records: list[dict] | None = None,
    ) -> None:
        """解析一条注入消息的持久化 Future。

        由 WebSocket 处理器在消息写入数据库（或被拒绝）后调用。
        ``attachment_records`` 携带 Agent 构建 LLM 用户内容所需的已准备附件
        记录（inline_text/image_data）。
        """
        entry = self._pending_input_by_id.pop(message_id, None)
        if entry is not None:
            if attachment_records is not None:
                entry.attachment_records = attachment_records
            if entry.persisted is not None and not entry.persisted.done():
                entry.persisted.set_result(ok)
