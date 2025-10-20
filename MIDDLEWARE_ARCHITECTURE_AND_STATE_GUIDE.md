# FastMCP Middleware: Architecture & State Management Guide

> **Version Note**: This guide references MCP Python SDK version **1.12.4** and FastMCP commit **ba1ba86**. Newer releases may have changes to the initialization flow.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Architecture Overview](#architecture-overview)
3. [State Management Issues](#state-management-issues)
4. [Root Cause Analysis](#root-cause-analysis)
5. [Recommended Solution](#recommended-solution)
6. [Session Mutability Research](#session-mutability-research)
7. [Alternative Solutions](#alternative-solutions)
8. [Related Files](#related-files)

---

## Executive Summary

### The Core Problem

**Middleware instances (not classes!) are created ONCE before server construction and reused for ALL requests, across ALL clients.**

This architectural decision means:
- ✅ Middleware instance variables work for **STDIO** (single client, sequential requests)
- ❌ Middleware instance variables **fail for HTTP** (concurrent clients, race conditions)

### Three Critical Bugs

1. **Context state is NOT cross-request** - Data stored in `context.state` during `on_initialize()` is not available in subsequent requests
2. **Middleware instance variables ARE shared** - HTTP clients share middleware instances causing race conditions
3. **Session is NOT accessible in `on_initialize()`** - The documented pattern `context.session` raises `ValueError`

### Current State

**No documented pattern works** for storing per-client data from `on_initialize()` that persists across requests in HTTP servers with concurrent clients.

### Recommended Fix

Set `request_ctx` ContextVar in `MiddlewareServerSession._received_request()` before calling middleware during initialization. This is a ~20 line change that uses existing MCP SDK patterns and makes the documented `context.session` API work as intended.

---

## Architecture Overview

### Middleware Lifecycle

Middleware is passed to FastMCP as a list of **instances** (not classes):

```python
# User code creates instances
my_logging_mw = LoggingMiddleware()
my_auth_mw = AuthMiddleware()

# Instances passed to FastMCP
mcp = FastMCP("ServerName", middleware=[my_logging_mw, my_auth_mw])
```

From `src/fastmcp/server/server.py:223`:
```python
# Middleware instances stored ONCE on the server instance
self.middleware = middleware or []
```

**Key Point**: These instances are **never recreated**. The same instance objects are used for every request from every client.

### Object Lifecycle Summary

| Object | Lifetime | Scope | Created Where | Shared Across Clients? |
|--------|----------|-------|---------------|------------------------|
| **FastMCP instance** | Server lifetime | Global | server.py:126 | Yes |
| **Middleware instances** | Server lifetime | Global | User code | Yes ⚠️ |
| **MiddlewareServerSession** | Connection lifetime | Per-connection | low_level.py:138 | No |
| **Context** | Request lifetime | Per-request | low_level.py:70 | No |

### Request Processing Flow: STDIO vs HTTP

#### STDIO Transport (Single Client)

```
User starts server
    ↓
FastMCP.__init__()  # Receives middleware instances (already created!)
    ↓
server.run(transport="stdio")
    ↓
LowLevelServer.run()  # low_level.py:125-157
    ↓
Creates ONE MiddlewareServerSession  # low_level.py:138-145
    ↓
async for message in session.incoming_messages:
    ↓
For each request (initialize, list_tools, call_tool):
    ↓
    MiddlewareServerSession._received_request()  # low_level.py:47-86
    ↓
    Creates fresh Context(fastmcp=self)  # low_level.py:70-72
    ↓
    FastMCP._apply_middleware()  # server.py:398-407
    ↓
    Middleware 1.on_<hook>(context, call_next)  # SAME instance every time
    Middleware 2.on_<hook>(context, call_next)  # SAME instance every time
```

**Why instance variables work**:
- Only one client ever uses the middleware instances
- Sequential requests, no concurrency
- No race conditions

#### HTTP Transport (Multiple Concurrent Clients)

```
User starts server
    ↓
FastMCP.__init__()  # Receives middleware instances (already created!)
    ↓
server.run(transport="http")
    ↓
HTTP server listens
    ↓
Client A connects → NEW MiddlewareServerSession (A)  ──┐
Client B connects → NEW MiddlewareServerSession (B)  ──┤  All share same
Client C connects → NEW MiddlewareServerSession (C)  ──┘  FastMCP instance!
    ↓
Each session processes requests CONCURRENTLY:

Session A: initialize          Session B: initialize          Session C: list_tools
    ↓                               ↓                               ↓
Fresh Context(A)               Fresh Context(B)               Fresh Context(C)
    ↓                               ↓                               ↓
┌─────────────────────────────────────────────────────────────────────┐
│              SAME MIDDLEWARE INSTANCES FOR ALL                      │
├─────────────────────────────────────────────────────────────────────┤
│  MW1.on_initialize()        MW1.on_initialize()        MW1 reads   │
│  self.client_name = "A"     self.client_name = "B"     stale data  │
│                                      ↑                              │
│               RACE CONDITION: Overwrites client A's data!          │
└─────────────────────────────────────────────────────────────────────┘
```

**Why instance variables fail**:
- Multiple clients share the same middleware instances
- Concurrent access creates race conditions
- Last client to write wins, others read stale/wrong data

### Timeline Example

```
T=0: User code creates middleware instances
     m1 = MyMiddleware()
     m2 = LoggingMiddleware()

T=1: Server starts
     FastMCP("ServerName", middleware=[m1, m2])
     → Stores references to m1, m2 at server.py:223

T=2: Client A connects (HTTP)
     Creates MiddlewareServerSession(A) at low_level.py:138
     → References FastMCP instance (which has m1, m2)

T=3: Client A sends "initialize"
     Creates Context(A1) at low_level.py:70
     Calls m1.on_initialize(Context(A1))
       m1.client_name = "client_a"  ← Stored on m1 instance

T=4: Client B connects (HTTP)
     Creates MiddlewareServerSession(B)
     → References same FastMCP instance (same m1, m2!)

T=5: Client B sends "initialize"
     Creates Context(B1) at low_level.py:70
     Calls m1.on_initialize(Context(B1))
       m1.client_name = "client_b"  ← OVERWRITES on same m1 instance!

T=6: Client A sends "list_tools"
     Creates Context(A2) at low_level.py:70  ← Fresh context, no state from A1
     Calls m1.on_list_tools(Context(A2))
       Reads m1.client_name → "client_b"  ← WRONG! Should be "client_a"
```

---

## State Management Issues

### Issue 1: Context State is NOT Cross-Request

#### The Problem

The pattern in `test_initialization_middleware.py:34-40` suggests:
```python
context.fastmcp_context.set_state("client_name", client_name)
```

The comment says "Store data in the context state for cross-request access" - but **this is WRONG**.

#### Why It Fails

Each MCP request gets a **fresh Context object** with empty `_state` (context.py:128):

```python
def __init__(self, fastmcp: FastMCP):
    self._fastmcp: weakref.ref[FastMCP] = weakref.ref(fastmcp)
    self._tokens: list[Token] = []
    self._notification_queue: set[str] = set()
    self._state: dict[str, Any] = {}  # Fresh empty dict each time!
```

**Result**:
- Data stored during `initialize` is NOT available in subsequent `list_tools()` calls
- `context._state` is per-request only
- Affects both STDIO and HTTP transports equally

#### When Context State DOES Work

Context state works perfectly **within the same request**:

```python
class RequestScopedMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        # Set state before tool execution
        context.fastmcp_context.set_state("user_id", "user123")
        result = await call_next(context)
        return result

@server.tool
def my_tool(ctx: Context):
    # This WORKS - same request!
    user_id = ctx.get_state("user_id")  # Returns "user123"
```

### Issue 2: Middleware Instance Variables ARE Shared

#### The Problem

The pattern in `test_initialization_middleware.py` stores client data on middleware instance:

```python
class InitializationMiddleware(Middleware):
    def __init__(self):
        self.client_info = None  # SHARED across ALL HTTP clients!

    async def on_initialize(self, context, call_next):
        self.client_info = get_client_info(context)  # Race condition!
```

#### Why It Fails with HTTP

All HTTP connections share the same middleware instance:

1. Client A initializes → `middleware.client_name = "client_a"`
2. Client B initializes → `middleware.client_name = "client_b"` (OVERWRITES!)
3. Client A calls tool → reads `middleware.client_name = "client_b"` (WRONG!)

**Test Evidence**: See `test_middleware_state_cross_transport.py`

### Issue 3: Session is NOT Accessible in `on_initialize()`

#### The Problem

The recommended pattern is to use session storage:

```python
session = context.fastmcp_context.session
setattr(session, "_client_name", client_name)
```

But accessing `context.fastmcp_context.session` in `on_initialize()` raises:

```
ValueError: Context is not available outside of a request
```

#### Why It Fails - Technical Details

The `Context.session` property accesses `request_ctx` ContextVar (context.py:295-297):

```python
@property
def session(self) -> ServerSession:
    """Access to the underlying session for advanced usage."""
    return self.request_context.session  # ← Calls request_context property

@property
def request_context(self) -> RequestContext[ServerSession, Any, Request]:
    try:
        return request_ctx.get()  # ← This fails during on_initialize()!
    except LookupError:
        raise ValueError("Context is not available outside of a request")
```

**The root cause**: `request_ctx` ContextVar is **not set** during initialization.

- Normal requests set it in `_handle_message()` (MCP SDK server.py:637-645)
- But initialization bypasses `_handle_message()` entirely
- `MiddlewareServerSession._received_request()` creates a Context but never sets `request_ctx`
- Therefore `context.session` raises `LookupError` → `ValueError`

**Where `request_ctx` comes from**:
- Module-level ContextVar in MCP SDK: `mcp/server/lowlevel/server.py:105`
- FastMCP imports it: `from mcp.server.lowlevel.server import request_ctx` (context.py:17)
- It stores a `RequestContext` which includes the session object

### Summary: No Working Pattern

**None of the documented patterns work** for storing per-client data from `on_initialize()`:

| Pattern | Cross-Request? | HTTP-Safe? | Status |
|---------|---------------|------------|---------|
| `context.set_state()` | ❌ No (per-request only) | ✅ Yes | ❌ Doesn't persist |
| `self.client_info = ...` | ✅ Yes | ❌ No (shared instance) | ❌ Race condition |
| `context.session` | ✅ Yes | ✅ Yes | ❌ Not accessible in `on_initialize()` |

---

## Root Cause Analysis

### Why Session Access Fails During Initialization

The MCP SDK only sets `request_ctx` for normal requests:

```python
# MCP SDK: mcp/server/lowlevel/server.py:637-645
async def _handle_message(self, message, session, lifespan_context, raise_exceptions):
    # ...
    token = request_ctx.set(
        RequestContext(
            message.request_id,
            message.request_meta,
            session,  # ← Session is available here!
            lifespan_context,
            request=request_data,
        )
    )
    response = await handler(req)
    # ...
```

But during initialization (low_level.py:70-84):

```python
async with fastmcp.server.context.Context(fastmcp=self.fastmcp) as fastmcp_ctx:
    # Creates Context, but request_ctx ContextVar is NOT set!
    mw_context = MiddlewareContext(
        message=responder.request.root,
        fastmcp_context=fastmcp_ctx,
    )
    return await self.fastmcp._apply_middleware(mw_context, call_original_handler)
```

**The flow**:
1. `MiddlewareServerSession._received_request()` is called directly (low_level.py:47-86)
2. Creates Context at low_level.py:70-72
3. Calls middleware chain at low_level.py:82-84
4. **Never** calls `_handle_message()` → **Never** sets `request_ctx`
5. Therefore `context.session` raises `LookupError` → `ValueError`

The session object exists, but the ContextVar mechanism to access it is not initialized!

### What the Author of ba1ba86 Likely Missed

The commit ba1ba86 added `on_initialize()` hook with documentation claiming it's useful for "initializing session state", but:

1. ✓ Successfully exposed initialization to middleware
2. ✓ Added `on_initialize()` hook
3. ✗ **Failed to recognize** that without setting `request_ctx`, there's no way to access session
4. ✗ **Tested only with in-memory transport** (single client), where instance variables work fine

The test patterns demonstrate broken approaches:
- Line 18: `self.session_data = {}` - Dead code (never used)
- Line 30: `self.client_info = client_info` - Shared across HTTP clients (race condition)
- Lines 34-40: Context state "for cross-request access" - Wrong, per-request only

---

## Recommended Solution

### Set request_ctx During on_initialize() ⭐

**This is the simplest and most consistent fix** that:
- Uses existing MCP SDK patterns (no new concepts)
- Makes `context.session` work in `on_initialize()` (fulfills documented promise)
- Requires minimal code change (~20 lines in one file)
- Doesn't break immutability (MiddlewareContext stays frozen)
- Doesn't expose new API (session already documented)
- Enables real session-scoped storage (what docs claim to support)

### Implementation

**Location**: `src/fastmcp/server/low_level.py` in `MiddlewareServerSession._received_request()`

**Add these imports at top of file**:
```python
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
```

**Modify `_received_request()` method** (lines 70-84):

```python
async def _received_request(
    self,
    responder: RequestResponder[mcp.types.ClientRequest, mcp.types.ServerResult],
):
    if isinstance(responder.request.root, mcp.types.InitializeRequest):
        import fastmcp.server.context
        from fastmcp.server.middleware.middleware import MiddlewareContext

        async def call_original_handler(ctx: MiddlewareContext) -> None:
            return await super(MiddlewareServerSession, self)._received_request(responder)

        async with fastmcp.server.context.Context(fastmcp=self.fastmcp) as fastmcp_ctx:
            # NEW: Set request_ctx so context.session works!
            # Note: token is a contextvars.Token for resetting the context later
            token = request_ctx.set(
                RequestContext(
                    request_id=responder.request.request_id,
                    meta=responder.request.meta,
                    session=self,  # self is the MiddlewareServerSession (IS a ServerSession)
                    lifespan_context=None,  # Not available during init, but that's OK
                    request=None,
                )
            )

            try:
                mw_context = MiddlewareContext(
                    message=responder.request.root,
                    source="client",
                    type="request",
                    method="initialize",
                    fastmcp_context=fastmcp_ctx,  # ← Now fastmcp_ctx.session works!
                )
                return await self.fastmcp._apply_middleware(mw_context, call_original_handler)
            finally:
                request_ctx.reset(token)  # Restore previous context using the token
    else:
        return await super()._received_request(responder)
```

### Why This Works

**Technical explanation**:
- `request_ctx` is the module-level ContextVar from MCP SDK (`mcp/server/lowlevel/server.py:105`)
- `ContextVar.set()` returns a `Token` (not an auth token - a contextvars restoration point)
- `ContextVar.reset(token)` restores the previous value
- This is **exactly the same pattern** MCP SDK uses for normal requests (server.py:637-662)
- Makes `context.fastmcp_context.session` available through existing documented API
- No changes to MiddlewareContext or exposed API

**Session lifecycle** (MCP SDK 1.12.4):
- Session created once per connection in `Server.run()` (mcp/server/lowlevel/server.py:576-583)
- Session lives for entire connection (connection-scoped)
- Session passed to every message handler via `request_ctx`
- FastMCP **already mutates session** using `setattr()` (context.py:291)

### Usage After Fix

```python
class MyMiddleware(Middleware):
    async def on_initialize(self, context, call_next):
        # Now this works!
        session = context.fastmcp_context.session
        client_name = get_client_name(context.message)
        setattr(session, "_my_client_name", client_name)
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        # Access stored data
        session = context.fastmcp_context.session
        client_name = getattr(session, "_my_client_name", "unknown")
        # Filter tools based on client_name...
        return await call_next(context)
```

### Benefits

- ✅ Uses existing MCP SDK patterns (same as normal request handling)
- ✅ Makes `context.session` work in `on_initialize()` (fulfills documented promise)
- ✅ Requires minimal code change (~20 lines in one file)
- ✅ Doesn't break immutability (MiddlewareContext stays frozen)
- ✅ Doesn't expose new API (session already documented)
- ✅ Enables session mutation pattern that FastMCP already uses
- ✅ Middleware can use: `setattr(context.fastmcp_context.session, "_my_data", value)`
- ✅ Works for both STDIO and HTTP transports
- ✅ No external storage needed for simple use cases
- ✅ Solves the documented use case: "initializing session state"

---

## Session Mutability Research

### Sessions ARE Designed to Be Mutable

**Session is NOT an internal implementation detail**:

#### 1. MCP SDK Exposes Session in RequestContext

From MCP SDK `context.py:14-20`:
```python
@dataclass
class RequestContext(Generic[SessionT, LifespanContextT, RequestT]):
    request_id: RequestId
    meta: RequestParams.Meta | None
    session: SessionT  # ← Session is core part of RequestContext!
    lifespan_context: LifespanContextT
    request: RequestT | None = None
```

#### 2. FastMCP Publicly Documents Session Access

From FastMCP `context.py:294-297`:
```python
@property
def session(self) -> ServerSession:
    """Access to the underlying session for advanced usage."""
    return self.request_context.session
```

#### 3. MCP SDK Examples Show Session Usage

From MCP SDK `session.py:29-33` (documentation examples):
```python
@server.list_prompts()
async def handle_list_prompts(ctx: RequestContext) -> list[types.Prompt]:
    if ctx.session.client_params:  # ← Using session from context
        return generate_custom_prompts(ctx.session.client_params)
```

#### 4. FastMCP Already Mutates Sessions

From FastMCP `context.py:291`:
```python
@property
def session_id(self) -> str:
    """Get the MCP session ID for ALL transports..."""
    session = self.request_context.session
    session_id = getattr(session, "_fastmcp_id", None)
    if session_id is None:
        session_id = str(uuid4())
        setattr(session, "_fastmcp_id", session_id)  # ← Mutation!
    return session_id
```

**This proves**:
- ✅ Session mutation is an accepted pattern in FastMCP
- ✅ Session is intended for connection-scoped storage
- ✅ The "immutability" argument doesn't apply (it's already being mutated)
- ✅ Using `setattr(session, "_custom_data", ...)` follows established patterns

### Why "Immutability" Concerns Don't Apply

The concern about adding session to frozen MiddlewareContext is misplaced because:

1. **Recommended solution doesn't modify MiddlewareContext** - it sets `request_ctx` ContextVar instead
2. **MiddlewareContext stays frozen** - no architectural purity violation
3. **Session accessed through existing API** - `context.fastmcp_context.session` (already documented)
4. **Pattern already exists** - FastMCP's own `session_id` property does this

---

## Alternative Solutions

### Option 2: Add Per-Session Storage API (Not Recommended)

Add middleware-specific per-session storage:
```python
async def on_initialize(self, context: MiddlewareContext, call_next):
    context.set_session_data("client_name", client_name)  # Hypothetical API

async def on_list_tools(self, context: MiddlewareContext, call_next):
    client_name = context.get_session_data("client_name")
```

**Pros**: Explicit API, easier to implement than redesigning context lifecycle
**Cons**: New API surface, doesn't solve existing code, reinvents what session already provides

### Option 3: Create Per-Session Middleware Instances (Not Recommended)

- Massive architectural change
- Would require factory pattern for middleware creation
- Breaks existing middleware implementations
- Overkill for this problem

### Option 4: Use External Dictionary Keyed by Session ID (Not Recommended)

```python
class Middleware:
    def __init__(self):
        self.client_data = {}  # session_id -> data

    async def on_initialize(self, context, call_next):
        session_id = ???  # Not currently available in on_initialize()
        self.client_data[session_id] = {"client_name": ...}
```

**Pros**: Works around current limitations
**Cons**: Requires session_id in `on_initialize()` (which also fails), memory leak potential

### Current Workarounds (Until Fixed)

**For `on_list_tools()` and later hooks**:
```python
class MyMiddleware(Middleware):
    def __init__(self):
        self._client_data = {}  # Keyed by session_id

    async def on_list_tools(self, context, call_next):
        session_id = context.fastmcp_context.session_id  # ✓ Works!
        client_data = self._client_data.get(session_id, {})
        # Use client_data...
        return await call_next(context)
```

**For `on_initialize()`**:
- No reliable workaround exists
- Cannot access session or session_id
- Can only use instance variables (fails for HTTP concurrent clients)

---

## Related Files

**Test Coverage**:
- `tests/server/middleware/test_middleware_state_cross_transport.py` - Demonstrates all three bugs
- `tests/server/middleware/test_initialization_middleware.py` - Contains broken patterns

**Source Files**:
- `src/fastmcp/server/low_level.py` - Where fix should be applied (MiddlewareServerSession)
- `src/fastmcp/server/context.py` - Context implementation, session property
- `src/fastmcp/server/server.py` - Middleware application, FastMCP constructor
- `src/fastmcp/server/middleware/middleware.py` - Middleware base class, MiddlewareContext

**MCP SDK Files** (version 1.12.4):
- `mcp/server/lowlevel/server.py:105` - request_ctx ContextVar definition
- `mcp/server/lowlevel/server.py:576-583` - Session creation in Server.run()
- `mcp/server/lowlevel/server.py:637-645` - request_ctx.set() for normal requests
- `mcp/server/session.py:83-99` - ServerSession constructor
- `mcp/server/session.py:142-147` - Session stores client params during init
- `mcp/shared/context.py:14-20` - RequestContext includes session

---

## Final Summary

**Current State**: No documented pattern works for storing per-client data from `on_initialize()` that persists across requests in HTTP servers.

**Root Cause**: `request_ctx` ContextVar is not set during initialization, preventing access to `context.session`.

**Recommended Fix**: Set `request_ctx` in `MiddlewareServerSession._received_request()` before calling middleware (see Implementation section above).

**Why This is the Right Approach**:
1. Minimal code change (~20 lines in one file)
2. Uses existing MCP SDK patterns (same as normal request handling)
3. Makes documented API work as intended (`context.session`)
4. Enables session mutation pattern that FastMCP already uses
5. No new API surface or architectural changes
6. Works for both STDIO and HTTP transports
7. Solves the documented use case: "initializing session state"

**Alternative Approaches**: All other options either reinvent what sessions provide, require more extensive changes, or don't fully solve the problem.

**Version Note**: This analysis is based on MCP Python SDK 1.12.4 and FastMCP commit ba1ba86. Newer versions may have different initialization flows.

**Test Coverage**: Run `uv run pytest tests/server/middleware/test_middleware_state_cross_transport.py -v` to see all issues demonstrated.
