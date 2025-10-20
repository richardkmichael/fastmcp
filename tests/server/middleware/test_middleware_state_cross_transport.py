"""Cross-transport tests demonstrating middleware state management issues.

These tests run with both STDIO and HTTP transports to demonstrate:

1. Context state (set_state/get_state) is NOT cross-request
   - Data stored during on_initialize() is not available in subsequent requests
   - Works the same for both STDIO and HTTP

2. Middleware instance variables ARE shared across HTTP clients
   - STDIO: Works fine (single client)
   - HTTP: Race condition (multiple clients share middleware instance)

3. Session storage is NOT accessible in on_initialize() hook
   - Accessing context.fastmcp_context.session raises exception
   - Affects both STDIO and HTTP
"""

import asyncio

import pytest

from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.utilities.tests import run_server_in_process
from mcp.types import Implementation


# ==============================================================================
# BUG 1: Context state is NOT cross-request
# ==============================================================================


class ContextStateMiddleware(Middleware):
    """Pattern from test_initialization_middleware.py - stores in context state.

    The comment in test_initialization_middleware.py:34-40 says:
    "Store data in the context state for cross-request access"

    But this is WRONG - context state is per-request only!
    """

    async def on_initialize(self, context: MiddlewareContext, call_next):
        """Store client name in context state."""
        params = getattr(context.message, "params", None)
        client_info = getattr(params, "clientInfo", None) if params else None
        client_name = (
            getattr(client_info, "name", "unknown") if client_info else "unknown"
        )

        # Try to store for cross-request access (this won't work!)
        if context.fastmcp_context:
            context.fastmcp_context.set_state("client_name", client_name)

        return await call_next(context)


def create_context_state_server():
    """Server using context.set_state() for cross-request storage (WRONG)."""
    mcp = FastMCP("ContextStateServer")

    mcp.add_middleware(ContextStateMiddleware())

    @mcp.tool
    def get_client_name(ctx: Context) -> str:
        """Return the client name stored during initialization."""
        # Try to read from context state (will be None - different request!)
        client_name = ctx.get_state("client_name")
        return client_name if client_name else "NOT_FOUND"

    return mcp


def run_context_state_server_http(host: str, port: int, **kwargs):
    """Run server with HTTP transport."""
    server = create_context_state_server()
    server.run(host=host, port=port, transport="http")


async def test_context_state_not_cross_request_stdio():
    """STDIO: Context state from on_initialize is NOT available in tool calls."""
    mcp = create_context_state_server()

    async with Client(
        mcp, client_info=Implementation(name="test_client", version="1.0.0")
    ) as client:
        result = await client.call_tool("get_client_name", {})
        client_name = result.content[0].text  # type: ignore[attr-defined]

        # BUG: Context state from on_initialize() is NOT available
        assert client_name == "NOT_FOUND", (
            f"Context state should NOT persist across requests. "
            f"Expected NOT_FOUND, got: {client_name}"
        )


async def test_context_state_not_cross_request_http():
    """HTTP: Context state from on_initialize is NOT available in tool calls."""
    with run_server_in_process(run_context_state_server_http, transport="http") as url:
        server_url = f"{url}/mcp"

        async with Client(
            transport=StreamableHttpTransport(server_url),
            client_info=Implementation(name="test_client", version="1.0.0"),
        ) as client:
            result = await client.call_tool("get_client_name", {})
            client_name = result.content[0].text  # type: ignore[attr-defined]

            # BUG: Same as STDIO - context state is per-request
            assert client_name == "NOT_FOUND", (
                f"Context state should NOT persist across requests. "
                f"Expected NOT_FOUND, got: {client_name}"
            )


# ==============================================================================
# BUG 2: Middleware instance variables ARE shared across HTTP clients
# ==============================================================================


class InstanceVarMiddleware(Middleware):
    """Pattern from test_initialization_middleware.py - stores on instance.

    The middleware sets self.client_info during on_initialize().
    This works for STDIO but fails for HTTP (shared instance).
    """

    def __init__(self):
        super().__init__()
        self.client_name = "unknown"

    async def on_initialize(self, context: MiddlewareContext, call_next):
        """Store client name on middleware instance."""
        params = getattr(context.message, "params", None)
        client_info = getattr(params, "clientInfo", None) if params else None
        self.client_name = (
            getattr(client_info, "name", "unknown") if client_info else "unknown"
        )
        return await call_next(context)


def create_instance_var_server():
    """Server using middleware instance variables for storage."""
    mcp = FastMCP("InstanceVarServer")

    mcp.add_middleware(InstanceVarMiddleware())

    @mcp.tool
    def get_client_name(ctx: Context) -> str:
        """Return the client name stored on middleware instance."""
        middleware = ctx.fastmcp.middleware[0]  # type: ignore[attr-defined]
        assert isinstance(middleware, InstanceVarMiddleware)
        return middleware.client_name

    return mcp


def run_instance_var_server_http(host: str, port: int, **kwargs):
    """Run server with HTTP transport."""
    server = create_instance_var_server()
    server.run(host=host, port=port, transport="http")


async def test_instance_vars_work_stdio():
    """STDIO: Instance variables work fine with single client."""
    mcp = create_instance_var_server()

    async with Client(
        mcp, client_info=Implementation(name="client_a", version="1.0.0")
    ) as client:
        result = await client.call_tool("get_client_name", {})
        client_name = result.content[0].text  # type: ignore[attr-defined]

        # Works fine with STDIO (single client)
        assert client_name == "client_a"


async def test_instance_vars_shared_http():
    """HTTP: Instance variables are SHARED - last client wins (race condition)."""
    with run_server_in_process(run_instance_var_server_http, transport="http") as url:
        server_url = f"{url}/mcp"

        results = {"a": None, "b": None}

        async def client_a():
            async with Client(
                transport=StreamableHttpTransport(server_url),
                client_info=Implementation(name="client_a", version="1.0.0"),
            ) as client:
                # Client A initializes first
                await asyncio.sleep(0.05)
                # Wait for client_b to overwrite middleware.client_name
                await asyncio.sleep(0.15)

                result = await client.call_tool("get_client_name", {})
                results["a"] = result.content[0].text  # type: ignore[attr-defined]

        async def client_b():
            async with Client(
                transport=StreamableHttpTransport(server_url),
                client_info=Implementation(name="client_b", version="1.0.0"),
            ) as client:
                # Client B initializes second, overwrites middleware.client_name
                await asyncio.sleep(0.1)

                result = await client.call_tool("get_client_name", {})
                results["b"] = result.content[0].text  # type: ignore[attr-defined]

        await asyncio.gather(client_a(), client_b())

        # BUG: Both clients see "client_b" because they share middleware instance
        print(f"Client A sees: {results['a']}")
        print(f"Client B sees: {results['b']}")

        # Last client to initialize "wins"
        assert results["a"] == "client_b", (
            f"Client A should see 'client_b' (overwrote by client B), got: {results['a']}"
        )
        assert results["b"] == "client_b", (
            f"Client B should see 'client_b', got: {results['b']}"
        )


# ==============================================================================
# BUG 3: Session storage is NOT accessible in on_initialize() hook
# ==============================================================================


class SessionStorageMiddleware(Middleware):
    """Recommended pattern - store on session.

    But this FAILS because session is not accessible during on_initialize()!
    Accessing context.fastmcp_context.session raises ValueError:
    "Context is not available outside of a request"
    """

    async def on_initialize(self, context: MiddlewareContext, call_next):
        """Try to store client name on session."""
        params = getattr(context.message, "params", None)
        client_info = getattr(params, "clientInfo", None) if params else None
        client_name = (
            getattr(client_info, "name", "unknown") if client_info else "unknown"
        )

        # Try to store on session (raises ValueError!)
        if context.fastmcp_context:
            try:
                session = context.fastmcp_context.session
                setattr(session, "_client_name", client_name)
            except Exception as e:
                # Store the exception on FastMCP instance so test can verify
                if hasattr(context.fastmcp_context, "fastmcp"):
                    context.fastmcp_context.fastmcp._session_init_error = (
                        f"{type(e).__name__}: {e}"
                    )

        return await call_next(context)


def create_session_storage_server():
    """Server trying to use session storage (recommended but fails)."""
    mcp = FastMCP("SessionStorageServer")

    mcp.add_middleware(SessionStorageMiddleware())

    @mcp.tool
    def get_client_name(ctx: Context) -> str:
        """Return the client name stored on session."""
        # Check if there was an error accessing session during initialize
        error = getattr(ctx.fastmcp, "_session_init_error", None)
        if error:
            return f"ERROR_DURING_INIT: {error}"

        # Try to read from session
        session = ctx.session
        client_name = getattr(session, "_client_name", "NOT_FOUND")
        return client_name

    return mcp


def run_session_storage_server_http(host: str, port: int, **kwargs):
    """Run server with HTTP transport."""
    server = create_session_storage_server()
    server.run(host=host, port=port, transport="http")


@pytest.mark.xfail(
    reason="BUG: context.fastmcp_context.session raises exception in on_initialize()",
    strict=True,
)
async def test_session_not_accessible_on_initialize_stdio():
    """STDIO: Session is NOT accessible during on_initialize() hook."""
    mcp = create_session_storage_server()

    async with Client(
        mcp, client_info=Implementation(name="test_client", version="1.0.0")
    ) as client:
        result = await client.call_tool("get_client_name", {})
        client_name = result.content[0].text  # type: ignore[attr-defined]

        # EXPECTED: Should work if session is accessible
        assert client_name == "test_client", (
            f"Expected 'test_client' from session storage, got: {client_name}"
        )


@pytest.mark.xfail(
    reason="BUG: context.fastmcp_context.session raises exception in on_initialize()",
    strict=True,
)
async def test_session_not_accessible_on_initialize_http():
    """HTTP: Session is NOT accessible during on_initialize() hook."""
    with run_server_in_process(run_session_storage_server_http, transport="http") as url:
        server_url = f"{url}/mcp"

        async with Client(
            transport=StreamableHttpTransport(server_url),
            client_info=Implementation(name="test_client", version="1.0.0"),
        ) as client:
            result = await client.call_tool("get_client_name", {})
            client_name = result.content[0].text  # type: ignore[attr-defined]

            # EXPECTED: Should work if session is accessible
            assert client_name == "test_client", (
                f"Expected 'test_client' from session storage, got: {client_name}"
            )


# ==============================================================================
# Demonstration: Context state DOES work within same request
# ==============================================================================


async def test_context_state_works_within_same_request():
    """Show that context.set_state() DOES work within the same request.

    This demonstrates the CORRECT use of context state - sharing data between
    middleware layers and tool execution within a SINGLE request.
    """

    class RequestScopedMiddleware(Middleware):
        """Correct usage: context state within same request."""

        async def on_call_tool(self, context: MiddlewareContext, call_next):
            """Set state before tool execution."""
            if context.fastmcp_context:
                # Store data that the tool can access
                context.fastmcp_context.set_state("request_id", "req-123")
                context.fastmcp_context.set_state("auth_user", "test_user")

            result = await call_next(context)

            # Can also read state after tool execution
            if context.fastmcp_context:
                request_id = context.fastmcp_context.get_state("request_id")
                assert request_id == "req-123", "State should be available in same request"

            return result

    server = FastMCP("TestServer")

    state_accessed_in_tool = {"request_id": None, "auth_user": None}

    @server.tool
    def check_state_tool(ctx: Context) -> str:
        """Tool that accesses context state set by middleware."""
        # This WORKS because it's the same request
        state_accessed_in_tool["request_id"] = ctx.get_state("request_id")
        state_accessed_in_tool["auth_user"] = ctx.get_state("auth_user")
        return "success"

    server.add_middleware(RequestScopedMiddleware())

    async with Client(server) as client:
        await client.call_tool("check_state_tool", {})

        # State was successfully accessed within the same request
        assert state_accessed_in_tool["request_id"] == "req-123"
        assert state_accessed_in_tool["auth_user"] == "test_user"
