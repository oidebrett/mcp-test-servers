import contextlib
import logging
from http import HTTPStatus
from uuid import uuid4

import anyio
import click
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http import (
    MCP_SESSION_ID_HEADER,
    StreamableHTTPServerTransport,
)
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount

from .event_store import InMemoryEventStore

# Configure logging
logger = logging.getLogger(__name__)

# Global task group that will be initialized in the lifespan
task_group = None

# Store for request headers
request_headers = {}

# Global session ID - we'll use a single session for all requests
GLOBAL_SESSION_ID = uuid4().hex

# Event store for resumability
event_store = InMemoryEventStore()


@contextlib.asynccontextmanager
async def lifespan(app):
    """Application lifespan context manager for managing task group."""
    global task_group

    async with anyio.create_task_group() as tg:
        task_group = tg
        logger.info("Application started, task group initialized!")
        try:
            yield
        finally:
            logger.info("Application shutting down, cleaning up resources...")
            if task_group:
                tg.cancel_scope.cancel()
                task_group = None
            logger.info("Resources cleaned up successfully.")


@click.command()
@click.option("--port", default=3000, help="Port to listen on for HTTP")
@click.option(
    "--log-level",
    default="INFO",
    help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
)
@click.option(
    "--json-response",
    is_flag=True,
    default=False,
    help="Enable JSON responses instead of SSE streams",
)
def main(
    port: int,
    log_level: str,
    json_response: bool,
) -> int:
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    app = Server("identity-streamablehttp-demo")

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        ctx = app.request_context
        
        # Use the global session ID
        session_id = GLOBAL_SESSION_ID
        
        if name == "get_logged_in_user":
            # Get headers for this session
            if session_id in request_headers:
                headers = request_headers[session_id]
                # Headers are case-insensitive
                header_name = "X-Forwarded-User".lower()
                for key, value in headers.items():
                    if key.lower() == header_name:
                        return [types.TextContent(type="text", text=value)]
                return [types.TextContent(type="text", text="Not logged in")]
            else:
                return [types.TextContent(type="text", text="No headers found for session")]
        
        elif name == "get_request_headers":
            # Return all headers for this session
            if session_id in request_headers:
                headers_text = "\n".join([f"{k}: {v}" for k, v in request_headers[session_id].items()])
                return [types.TextContent(type="text", text=headers_text)]
            else:
                return [types.TextContent(type="text", text="No headers found for session")]
        
        elif name == "get_header":
            header_name = arguments.get("header_name", "")
            if not header_name:
                return [types.TextContent(type="text", text="Missing header_name argument")]
            
            if session_id in request_headers:
                headers = request_headers[session_id]
                # Headers are case-insensitive
                header_name = header_name.lower()
                for key, value in headers.items():
                    if key.lower() == header_name:
                        return [types.TextContent(type="text", text=value)]
                return [types.TextContent(type="text", text="Not found")]
            else:
                return [types.TextContent(type="text", text="No headers found for session")]
        
        elif name == "start-notification-stream":
            interval = arguments.get("interval", 1.0)
            count = arguments.get("count", 5)
            caller = arguments.get("caller", "unknown")

            # Send the specified number of notifications with the given interval
            for i in range(count):
                notification_msg = (
                    f"[{i+1}/{count}] Event from '{caller}' - "
                    f"Use Last-Event-ID to resume if disconnected"
                )
                await ctx.session.send_log_message(
                    level="info",
                    data=notification_msg,
                    logger="notification_stream",
                    related_request_id=ctx.request_id,
                )
                logger.debug(f"Sent notification {i+1}/{count} for caller: {caller}")
                if i < count - 1:  # Don't wait after the last notification
                    await anyio.sleep(interval)

            await ctx.session.send_resource_updated(uri=AnyUrl("http:///test_resource"))
            return [
                types.TextContent(
                    type="text",
                    text=(
                        f"Sent {count} notifications with {interval}s interval"
                        f" for caller: {caller}"
                    ),
                )
            ]
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="get_logged_in_user",
                description="Returns the logged in user from X-Forwarded-User header",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.Tool(
                name="get_request_headers",
                description="Returns all HTTP headers from the client request",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.Tool(
                name="get_header",
                description="Returns a specific HTTP header value",
                inputSchema={
                    "type": "object",
                    "required": ["header_name"],
                    "properties": {
                        "header_name": {
                            "type": "string",
                            "description": "The name of the header to retrieve (case-insensitive)",
                        },
                    },
                },
            ),
            types.Tool(
                name="start-notification-stream",
                description=(
                    "Sends a stream of notifications with configurable count"
                    " and interval"
                ),
                inputSchema={
                    "type": "object",
                    "required": ["interval", "count", "caller"],
                    "properties": {
                        "interval": {
                            "type": "number",
                            "description": "Interval between notifications in seconds",
                        },
                        "count": {
                            "type": "number",
                            "description": "Number of notifications to send",
                        },
                        "caller": {
                            "type": "string",
                            "description": (
                                "Identifier of the caller to include in notifications"
                            ),
                        },
                    },
                },
            )
        ]

    # We need to store the server instances between requests
    server_instances = {}
    # Lock to prevent race conditions when creating new sessions
    session_creation_lock = anyio.Lock()

    # ASGI handler for streamable HTTP connections
    async def handle_streamable_http(scope, receive, send):
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)
        
        # Always use our global session ID
        session_id = GLOBAL_SESSION_ID
        
        # Store headers for this request
        request_headers[session_id] = dict(request.headers)
        logger.debug(f"Updated headers for global session {session_id}")
        
        if session_id in server_instances:
            transport = server_instances[session_id]
            logger.debug("Session already exists, handling request directly")
            await transport.handle_request(scope, receive, send)
        else:
            # Create a new transport with our global session ID
            logger.debug("Creating new transport with global session ID")
            http_transport = StreamableHTTPServerTransport(
                mcp_session_id=session_id,
                is_json_response_enabled=json_response,
                event_store=event_store,  # Enable resumability
            )
            server_instances[session_id] = http_transport
            logger.info(f"Created new transport with global session ID: {session_id}")

            async def run_server(task_status=None):
                async with http_transport.connect() as streams:
                    read_stream, write_stream = streams
                    if task_status:
                        task_status.started()
                    await app.run(
                        read_stream,
                        write_stream,
                        app.create_initialization_options(),
                    )

            if not task_group:
                raise RuntimeError("Task group is not initialized")

            await task_group.start(run_server)

            # Handle the HTTP request and return the response
            await http_transport.handle_request(scope, receive, send)

    # Create an ASGI application using the transport
    starlette_app = Starlette(
        debug=True,
        routes=[
            Mount("/mcp", app=handle_streamable_http),
        ],
        lifespan=lifespan,
    )

    import uvicorn

    uvicorn.run(starlette_app, host="0.0.0.0", port=port)

    return 0
