"""MCP 工具客户端单元测试"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.common.mcp_client import MCPToolClient, ToolSpec
from lumio.shared.config import MCPSettings


def _make_tool(name: str, description: str = "", *, destructive: bool | None = None, schema: dict | None = None):
    """构造类 mcp.types.Tool 的轻量对象"""
    annotations = SimpleNamespace(destructiveHint=destructive) if destructive is not None else None
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object", "properties": {}},
        annotations=annotations,
    )


class TestDisabled:
    """MCP_ENABLED=False 时的零回归行为"""

    async def test_connect_noop_when_disabled(self):
        client = MCPToolClient(settings=MCPSettings(enabled=False))
        await client.connect()
        assert client.connected is False
        assert await client.list_tools() == []
        assert client.to_openai_tools() == []
        await client.close()

    async def test_connect_failure_graceful(self):
        """endpoint 不可达时应降级为无工具，而非抛出"""
        from unittest.mock import patch

        client = MCPToolClient(settings=MCPSettings(enabled=True, endpoint="http://gw/mcp"))
        with patch(
            "lumio.services.common.mcp_client.streamablehttp_client",
            side_effect=ConnectionError("connection refused"),
        ):
            await client.connect()
        assert client.connected is False
        assert await client.list_tools() == []


class TestRefreshTools:
    """工具目录拉取与敏感性判定"""

    async def test_sensitivity_from_annotation_and_config(self):
        client = MCPToolClient(settings=MCPSettings(enabled=True, sensitive_tools=["bill_installment"]))
        mock_session = MagicMock()
        mock_session.list_tools = AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    _make_tool("query_balance", "查询余额", destructive=False),
                    _make_tool("card_loss", "银行卡挂失", destructive=True),
                    _make_tool("bill_installment", "账单分期"),  # 无注解，靠配置命中
                ]
            )
        )
        client._session = mock_session
        client._connected = True
        await client._refresh_tools()

        tools = await client.list_tools()
        by_name = {t.name: t for t in tools}
        assert by_name["query_balance"].sensitive is False
        assert by_name["card_loss"].sensitive is True  # destructiveHint
        assert by_name["bill_installment"].sensitive is True  # 配置白名单
        assert client.is_sensitive("card_loss") is True
        assert client.is_sensitive("query_balance") is False

    async def test_to_openai_tools_format(self):
        client = MCPToolClient(settings=MCPSettings(enabled=True))
        client._tools_cache = [
            ToolSpec(name="query_balance", description="查询余额", input_schema={"type": "object", "properties": {}})
        ]
        openai_tools = client.to_openai_tools()
        assert openai_tools[0]["type"] == "function"
        assert openai_tools[0]["function"]["name"] == "query_balance"
        assert openai_tools[0]["function"]["description"] == "查询余额"

    async def test_get_tool(self):
        client = MCPToolClient(settings=MCPSettings(enabled=True))
        spec = ToolSpec(name="foo", description="bar")
        client._tools_cache = [spec]
        assert client.get_tool("foo") is spec
        assert client.get_tool("missing") is None


class TestCallTool:
    """工具执行"""

    async def test_call_tool_parses_text_content(self):
        client = MCPToolClient(settings=MCPSettings(enabled=True))
        client._connected = True
        mock_session = MagicMock()
        mock_session.call_tool = AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(text="您的可用余额为 100 元")],
                structuredContent=None,
                isError=False,
            )
        )
        client._session = mock_session

        result = await client.call_tool("query_balance", {"card": "1234"})
        assert result["is_error"] is False
        assert "100 元" in result["content"]

    async def test_call_tool_structured_fallback(self):
        client = MCPToolClient(settings=MCPSettings(enabled=True))
        client._connected = True
        mock_session = MagicMock()
        mock_session.call_tool = AsyncMock(
            return_value=SimpleNamespace(content=[], structuredContent={"balance": 100}, isError=False)
        )
        client._session = mock_session
        result = await client.call_tool("query_balance", {})
        assert "balance" in result["content"]

    async def test_call_tool_raises_when_not_connected(self):
        client = MCPToolClient(settings=MCPSettings(enabled=True))
        with pytest.raises(RuntimeError, match="未连接"):
            await client.call_tool("query_balance", {})


class TestSseTransport:
    """SSE 传输选择: 直连 SSE 后端 (如 Java MCP Server) 无需 Higress 桥接."""

    async def test_sse_transport_uses_sse_client(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from lumio.services.common.mcp_client import MCPToolClient

        fake_cm = MagicMock()
        fake_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
        fake_cm.__aexit__ = AsyncMock(return_value=None)
        session_init = AsyncMock()
        with (
            patch("lumio.services.common.mcp_client.sse_client", return_value=fake_cm) as sse_patch,
            patch(
                "lumio.services.common.mcp_client.ClientSession",
                return_value=MagicMock(initialize=session_init),
            ),
            patch("lumio.services.common.mcp_client.streamablehttp_client") as streamable_patch,
        ):
            client = MCPToolClient(
                settings=MCPSettings(enabled=True, endpoint="http://127.0.0.1:8090", transport="sse")
            )
            await client.connect()

        sse_patch.assert_called_once()
        streamable_patch.assert_not_called()
        # sse_client 收到秒级超时 (SDK 约定) + 端点原样
        call_kwargs = sse_patch.call_args.kwargs
        assert call_kwargs["url"] == "http://127.0.0.1:8090"
        assert isinstance(call_kwargs["timeout"], float)

    async def test_default_transport_stays_streamable(self):
        """默认 transport=streamable-http → 行为与旧版一致 (零回归)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from lumio.services.common.mcp_client import MCPToolClient

        fake_cm = MagicMock()
        fake_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
        fake_cm.__aexit__ = AsyncMock(return_value=None)
        with (
            patch("lumio.services.common.mcp_client.streamablehttp_client", return_value=fake_cm) as sh_patch,
            patch(
                "lumio.services.common.mcp_client.ClientSession",
                return_value=MagicMock(initialize=AsyncMock()),
            ),
            patch("lumio.services.common.mcp_client.sse_client") as sse_patch,
        ):
            client = MCPToolClient(
                settings=MCPSettings(enabled=True, endpoint="http://gw/mcp", transport="streamable-http")
            )
            await client.connect()

        sh_patch.assert_called_once()
        sse_patch.assert_not_called()

    async def test_invalid_transport_raises(self):
        from unittest.mock import patch

        from lumio.services.common.mcp_client import MCPToolClient

        client = MCPToolClient(settings=MCPSettings(enabled=True, endpoint="http://x", transport="websocket"))
        with patch("lumio.services.common.mcp_client.streamablehttp_client"):
            await client.connect()  # 连接异常被吞 → 降级无工具, 不抛到上层
        assert client.connected is False
        assert await client.list_tools() == []
