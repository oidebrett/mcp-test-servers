import contextlib
import logging
from collections.abc import AsyncIterator
from http import HTTPStatus
from uuid import uuid4

import anyio
import click
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from .event_store import InMemoryEventStore

# Configure logging
logger = logging.getLogger(__name__)

# Store for request headers - use a simple current request approach
current_request_headers = {}

# Global session manager that will be initialized in the lifespan
session_manager = None


class HeaderCaptureMiddleware(BaseHTTPMiddleware):
    """Middleware to capture request headers."""
    
    async def dispatch(self, request, call_next):
        # Store headers from the request with detailed logging
        headers = dict(request.headers)
        
        # Store in both the global current headers and the request state
        global current_request_headers
        current_request_headers = headers
        request.state.headers = headers
        
        # Log all headers in detail
        logger.info(f"MIDDLEWARE: Captured headers: {headers}")
        for key, value in headers.items():
            logger.info(f"MIDDLEWARE: Header - {key}: {value}")
        
        # Process the request
        response = await call_next(request)
        
        return response


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    """Context manager for session manager."""
    global session_manager
    
    # Create the MCP server
    mcp_app = Server("identity-streamablehttp-demo")
    
    @mcp_app.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        ctx = mcp_app.request_context
        
        # Debug logging for request context
        logger.info(f"TOOL: Request context: {ctx}")
        logger.info(f"TOOL: MCP Request ID from context: {ctx.request_id}")
        
        # Get headers from the global current request headers
        # This works because each request is processed synchronously
        headers = current_request_headers.copy()
        logger.info(f"TOOL: Current request headers: {headers}")
        
        if name == "get_logged_in_user":
            # Headers are case-insensitive
            header_name = "X-Forwarded-User".lower()
            logger.info(f"TOOL: Looking for header: {header_name}")
            for key, value in headers.items():
                logger.info(f"TOOL: Checking header {key.lower()} against {header_name}")
                if key.lower() == header_name:
                    logger.info(f"TOOL: Found header {key} with value {value}")
                    return [types.TextContent(type="text", text=value)]
            logger.info("TOOL: X-Forwarded-User header not found, returning 'Not logged in'")
            return [types.TextContent(type="text", text="Not logged in")]
        
        elif name == "get_request_headers":
            # Return all headers for this request
            headers_text = "\n".join([f"{k}: {v}" for k, v in headers.items()])
            if not headers_text:
                headers_text = "No headers found for request"
                logger.info(f"TOOL: {headers_text}")
            else:
                logger.info(f"TOOL: Found headers:\n{headers_text}")
            return [types.TextContent(type="text", text=headers_text)]
        
        elif name == "get_header":
            header_name = arguments.get("header_name", "")
            if not header_name:
                return [types.TextContent(type="text", text="Missing header_name argument")]
            
            # Headers are case-insensitive
            header_name_lower = header_name.lower()
            for key, value in headers.items():
                if key.lower() == header_name_lower:
                    logger.info(f"TOOL: Found header {key} with value {value}")
                    return [types.TextContent(type="text", text=value)]
            logger.info(f"TOOL: Header {header_name} not found")
            return [types.TextContent(type="text", text="Not found")]
        
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

    @mcp_app.list_tools()
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

    # Create the session manager with stateless mode
    session_manager = StreamableHTTPSessionManager(
        app=mcp_app,
        event_store=InMemoryEventStore(),  # Keep event store for resumability
        json_response=app.state.json_response,
        stateless=True,  # Enable stateless mode
    )
    
    # Start the session manager
    async with session_manager.run():
        logger.info("Application started with stateless StreamableHTTP session manager!")
        try:
            yield
        finally:
            logger.info("Application shutting down...")


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

    async def handle_streamable_http(
        scope: Scope, receive: Receive, send: Send
    ) -> None:
        # Get the request
        request = Request(scope, receive)
        
        logger.info(f"HANDLER: Processing request")
        
        try:
            # Handle the request using the global session manager
            if session_manager:
                logger.info(f"HANDLER: Passing request to session manager")
                await session_manager.handle_request(scope, receive, send)
                logger.info(f"HANDLER: Request completed successfully")
            else:
                logger.error("HANDLER: Session manager not initialized")
                await send({
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"Internal server error: Session manager not initialized",
                })
        except Exception as e:
            logger.error(f"HANDLER: Error handling request: {str(e)}")
            import traceback
            logger.error(f"HANDLER: Traceback: {traceback.format_exc()}")
            
            # Try to send an error response
            try:
                await send({
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({
                    "type": "http.response.body",
                    "body": f"Internal server error: {str(e)}".encode("utf-8"),
                })
            except Exception as send_error:
                logger.error(f"HANDLER: Failed to send error response: {str(send_error)}")

    # Create an ASGI application using the transport
    starlette_app = Starlette(
        debug=True,
        routes=[
            Mount("/mcp", app=handle_streamable_http),
        ],
        middleware=[
            Middleware(HeaderCaptureMiddleware),
        ],
        lifespan=lifespan,
    )
    
    # Store json_response in app state for access in lifespan
    starlette_app.state.json_response = json_response

    import uvicorn

    uvicorn.run(starlette_app, host="0.0.0.0", port=port)

    return 0