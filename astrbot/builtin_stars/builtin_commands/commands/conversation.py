from sqlalchemy import case, func, select
from sqlmodel import col

from astrbot.api import sp, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.core import logger
from astrbot.core.agent.runners.deerflow.constants import (
    DEERFLOW_PROVIDER_TYPE,
    DEERFLOW_THREAD_ID_KEY,
)
from astrbot.core.agent.runners.deerflow.deerflow_api_client import DeerFlowAPIClient
from astrbot.core.db.po import ProviderStat
from astrbot.core.provider.event_guard import AIRequestDenied
from astrbot.core.utils.active_event_registry import active_event_registry

THIRD_PARTY_AGENT_RUNNER_KEY = {
    "dify": "dify_conversation_id",
    "coze": "coze_conversation_id",
    "dashscope": "dashscope_conversation_id",
    DEERFLOW_PROVIDER_TYPE: DEERFLOW_THREAD_ID_KEY,
}
THIRD_PARTY_AGENT_RUNNER_STR = ", ".join(THIRD_PARTY_AGENT_RUNNER_KEY.keys())


async def _cleanup_deerflow_thread_if_present(
    context: star.Context,
    umo: str,
) -> None:
    try:
        thread_id = await sp.get_async(
            scope="umo",
            scope_id=umo,
            key=DEERFLOW_THREAD_ID_KEY,
            default="",
        )
        if not thread_id:
            return

        cfg = context.get_config(umo=umo)
        agent_runner = cfg.get("agent_runner", {})
        if agent_runner.get("runner_type") != DEERFLOW_PROVIDER_TYPE:
            return
        runner_config = agent_runner.get("config", {})
        if not isinstance(runner_config, dict):
            return

        client = DeerFlowAPIClient(
            api_base=runner_config.get(
                "deerflow_api_base",
                "http://127.0.0.1:2026",
            ),
            api_key=runner_config.get("deerflow_api_key", ""),
            auth_header=runner_config.get("deerflow_auth_header", ""),
            proxy=runner_config.get("proxy", ""),
        )
        try:
            await client.delete_thread(thread_id)
        finally:
            try:
                await client.close()
            except Exception as e:
                logger.warning(
                    "Failed to close DeerFlow API client after thread cleanup: %s",
                    e,
                )
    except Exception as e:
        logger.warning(
            "Failed to clean up DeerFlow thread for session %s: %s",
            umo,
            e,
        )


async def _clear_third_party_agent_runner_state(
    context: star.Context,
    umo: str,
    agent_runner_type: str,
) -> None:
    session_key = THIRD_PARTY_AGENT_RUNNER_KEY.get(agent_runner_type)
    if not session_key:
        return

    if agent_runner_type == DEERFLOW_PROVIDER_TYPE:
        await _cleanup_deerflow_thread_if_present(context, umo)

    await sp.remove_async(
        scope="umo",
        scope_id=umo,
        key=session_key,
    )


class ConversationCommands:
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def _conversation_origin(self, message):
        resolve = getattr(type(message), "get_ai_command_conversation_origin", None)
        if resolve is not None:
            try:
                return await resolve(message, self.context)
            except Exception as exc:
                hint = (
                    "请先发送群列表，再发送：选择群 编号。"
                    if isinstance(exc, AIRequestDenied)
                    and str(exc) == "group_selection_required"
                    else "当前会话不可用，请先确认群选择和权限。"
                )
                message.set_result(MessageEventResult().message(hint))
                return None
        origin = getattr(type(message), "get_ai_conversation_origin", None)
        return origin(message, self.context) if origin else message.unified_msg_origin

    async def _get_current_persona_id(self, session_id):
        curr = await self.context.conversation_manager.get_curr_conversation_id(
            session_id,
        )
        if not curr:
            return None
        conv = await self.context.conversation_manager.get_conversation(
            session_id,
            curr,
        )
        if not conv:
            return None
        return conv.persona_id

    async def stop(self, message: AstrMessageEvent) -> None:
        """停止当前会话正在运行的 Agent"""
        cfg = self.context.get_config(umo=message.unified_msg_origin)
        agent_runner_type = cfg["agent_runner"]["runner_type"]
        umo = message.unified_msg_origin

        if agent_runner_type in THIRD_PARTY_AGENT_RUNNER_KEY:
            stopped_count = active_event_registry.stop_all(umo, exclude=message)
        else:
            stopped_count = active_event_registry.request_agent_stop_all(
                umo,
                exclude=message,
            )

        if stopped_count > 0:
            message.set_result(
                MessageEventResult().message(
                    f"✅ Requested to stop {stopped_count} running tasks."
                )
            )
            return

        message.set_result(
            MessageEventResult().message("✅ No running tasks in the current session.")
        )

    async def new_conv(self, message: AstrMessageEvent) -> None:
        """Start a new conversation without clearing the previous history.

        Args:
            message: Command event identifying the session and sender.
        """
        authorize = getattr(message, "is_ai_admin", None)
        if authorize and not message.is_private_chat():
            authorize = None
        if authorize and not authorize(self.context):
            message.set_result(MessageEventResult().message("无权限"))
            return
        cfg = self.context.get_config(umo=message.unified_msg_origin)
        agent_runner_type = cfg["agent_runner"]["runner_type"]
        if agent_runner_type in THIRD_PARTY_AGENT_RUNNER_KEY:
            active_event_registry.stop_all(message.unified_msg_origin, exclude=message)
            await _clear_third_party_agent_runner_state(
                self.context,
                message.unified_msg_origin,
                agent_runner_type,
            )
            message.set_result(
                MessageEventResult().message("✅ New conversation created.")
            )
            return

        conversation_origin = await self._conversation_origin(message)
        if conversation_origin is None:
            return
        active_event_registry.stop_all(message.unified_msg_origin, exclude=message)
        cpersona = await self._get_current_persona_id(conversation_origin)
        cid = await self.context.conversation_manager.new_conversation(
            conversation_origin,
            message.get_platform_id(),
            persona_id=cpersona,
        )

        message.set_extra("_clean_group_context_session", True)

        message.set_result(
            MessageEventResult().message(
                f"✅ Switched to new conversation: {cid[:4]}."
            ),
        )

    async def stats(self, message: AstrMessageEvent) -> None:
        """Show token usage statistics for the current conversation."""
        authorize = getattr(message, "is_ai_admin", None)
        if authorize and not message.is_private_chat():
            authorize = None
        if authorize and not authorize(self.context):
            message.set_result(MessageEventResult().message("无权限"))
            return
        umo = await self._conversation_origin(message)
        if umo is None:
            return
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

        if not cid:
            message.set_result(
                MessageEventResult().message(
                    "❌ You are not in a conversation. Use /new to create one."
                ),
            )
            return

        db = self.context.get_db()
        async with db.get_db() as session:
            result = await session.execute(
                select(
                    func.count(case((col(ProviderStat.id).is_not(None), 1))).label(
                        "record_count",
                    ),
                    func.coalesce(func.sum(ProviderStat.token_input_other), 0).label(
                        "total_input_other",
                    ),
                    func.coalesce(func.sum(ProviderStat.token_input_cached), 0).label(
                        "total_input_cached",
                    ),
                    func.coalesce(func.sum(ProviderStat.token_output), 0).label(
                        "total_output",
                    ),
                ).where(
                    col(ProviderStat.agent_type) == "internal",
                    col(ProviderStat.conversation_id) == cid,
                )
            )
            stats = result.one()

        if authorize and not authorize(self.context):
            message.set_result(MessageEventResult().message("无权限"))
            return
        if stats.record_count == 0:
            message.set_result(
                MessageEventResult().message(
                    "📊 No stats available for this conversation yet."
                ),
            )
            return

        total_input_other = stats.total_input_other
        total_input_cached = stats.total_input_cached
        total_output = stats.total_output
        total_tokens = total_input_other + total_input_cached + total_output

        ret = (
            f"📊 Conversation Token usage (ID: {cid[:8]}...)\n"
            f"Total:          {total_tokens:,}\n"
            f"Input (cached): {total_input_cached:,}\n"
            f"Input (other):  {total_input_other:,}\n"
            f"Output:         {total_output:,}\n"
        )

        message.set_result(MessageEventResult().message(ret))
