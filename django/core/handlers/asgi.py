import cgi
import codecs
import logging
import sys
import traceback
from io import BytesIO

from django import http
from django.conf import settings
from django.core import signals
from django.core.exceptions import RequestDataTooBig, RequestAborted, RequestTimeout
from django.core.handlers import base
from django.http import FileResponse, HttpResponse, HttpResponseServerError
from django.urls import set_script_prefix
from django.utils.functional import cached_property

from asgiref.sync import sync_to_async

logger = logging.getLogger("django.request")


class AsgiRequest(http.HttpRequest):
    """
    Custom request subclass that decodes from an ASGI-standard request
    dict, and wraps request body handling.
    """

    # Number of seconds until a Request gives up on trying to read a request
    # body and aborts.
    body_receive_timeout = 60

    def __init__(self, scope, body):
        self.scope = scope
        self._content_length = 0
        self._post_parse_error = False
        self._read_started = False
        self.resolver_match = None
        self.script_name = self.scope.get("root_path", "")
        if self.script_name and scope["path"].startswith(self.script_name):
            # TODO: Better is-prefix checking, slash handling?
            self.path_info = scope["path"][len(self.script_name) :]
        else:
            self.path_info = scope["path"]

        # django path is different from asgi scope path args, it should combine with script name
        if self.script_name:
            self.path = "%s/%s" % (
                self.script_name.rstrip("/"),
                self.path_info.replace("/", "", 1),
            )
        else:
            self.path = scope["path"]

        # HTTP basics
        self.method = self.scope["method"].upper()
        # Ensure query string is encoded correctly
        query_string = self.scope.get("query_string", "")
        if isinstance(query_string, bytes):
            query_string = query_string.decode("utf-8")
        self.META = {
            "REQUEST_METHOD": self.method,
            "QUERY_STRING": query_string,
            "SCRIPT_NAME": self.script_name,
            "PATH_INFO": self.path_info,
            # WSGI-epecting code will need these for a while
            "wsgi.multithread": True,
            "wsgi.multiprocess": True,
        }
        if self.scope.get("client", None):
            self.META["REMOTE_ADDR"] = self.scope["client"][0]
            self.META["REMOTE_HOST"] = self.META["REMOTE_ADDR"]
            self.META["REMOTE_PORT"] = self.scope["client"][1]
        if self.scope.get("server", None):
            self.META["SERVER_NAME"] = self.scope["server"][0]
            self.META["SERVER_PORT"] = str(self.scope["server"][1])
        else:
            self.META["SERVER_NAME"] = "unknown"
            self.META["SERVER_PORT"] = "0"
        # Handle old style-headers for a transition period
        if "headers" in self.scope and isinstance(self.scope["headers"], dict):
            self.scope["headers"] = [
                (x.encode("latin1"), y) for x, y in self.scope["headers"].items()
            ]
        # Headers go into META
        for name, value in self.scope.get("headers", []):
            name = name.decode("latin1")
            if name == "content-length":
                corrected_name = "CONTENT_LENGTH"
            elif name == "content-type":
                corrected_name = "CONTENT_TYPE"
            else:
                corrected_name = "HTTP_%s" % name.upper().replace("-", "_")
            # HTTPbis say only ASCII chars are allowed in headers, but we latin1 just in case
            value = value.decode("latin1")
            if corrected_name in self.META:
                value = self.META[corrected_name] + "," + value
            self.META[corrected_name] = value
        # Pull out request encoding if we find it
        if "CONTENT_TYPE" in self.META:
            self.content_type, self.content_params = cgi.parse_header(
                self.META["CONTENT_TYPE"]
            )
            if "charset" in self.content_params:
                try:
                    codecs.lookup(self.content_params["charset"])
                except LookupError:
                    pass
                else:
                    self.encoding = self.content_params["charset"]
        else:
            self.content_type, self.content_params = "", {}
        # Pull out content length info
        if self.META.get("CONTENT_LENGTH", None):
            try:
                self._content_length = int(self.META["CONTENT_LENGTH"])
            except (ValueError, TypeError):
                pass
        # Save body
        self._body = body
        assert isinstance(self._body, bytes), "Body is not bytes"
        # Add a stream-a-like for the body
        self._stream = BytesIO(self._body)
        # Other bits
        self.resolver_match = None

    @cached_property
    def GET(self):
        return http.QueryDict(self.scope.get("query_string", ""))

    def _get_scheme(self):
        return self.scope.get("scheme", "http")

    def _get_post(self):
        if not hasattr(self, "_post"):
            self._read_started = False
            self._load_post_and_files()
        return self._post

    def _set_post(self, post):
        self._post = post

    def _get_files(self):
        if not hasattr(self, "_files"):
            self._read_started = False
            self._load_post_and_files()
        return self._files

    POST = property(_get_post, _set_post)
    FILES = property(_get_files)

    @cached_property
    def COOKIES(self):
        return http.parse_cookie(self.META.get("HTTP_COOKIE", ""))


class AsgiHandler(base.BaseHandler):
    """
    Handler for ASGI requests.
    """

    request_class = AsgiRequest

    # Size to chunk response bodies into for multiple response messages
    chunk_size = 512 * 1024

    def __init__(self):
        super(AsgiHandler, self).__init__()
        self.load_middleware()

    async def __call__(self, scope, receive, send):
        """
        Async entrypoint - parses the request and hands off to get_response.
        """
        # Only serve HTTP connections (we should allow some way to override this)
        if scope["type"] != "http":
            raise ValueError(
                "Django can only handle ASGI/HTTP connections, not %s"
                % scope["type"]
            )
        # Receive the HTTP request body
        body = self.read_body()
        # If we've got here, then the request is complete and we can serve it.
        set_script_prefix(self.get_script_prefix(scope))
        # TODO: Is signal sending async-safe?
        signals.request_started.send(sender=self.__class__, environ=environ)
        # Get the request and check for basic issues
        request, error_response = self.create_request(scope, body)
        if request is None:
            await self.send_response(error_response, send)
            return
        # Get the response (async or sync dispatch is done inside get_response,
        # but we detect here in case someone subclassed us)
        if asyncio.iscoroutinefunction(self.get_response):
            response = await self.get_response(request)
        else:
            # If get_response is synchronous, run it non-blocking
            response = await sync_to_async(self.get_response(request))
        response._handler_class = self.__class__
        # Increase chunk size on file responses (ASGI servers will handle low-level chunking)
        if isinstance(response, FileResponse):
            response.block_size = self.chunk_size
        # Send the response
        await self.send_response(response, send)

    async def read_body(self, read):
        """
        Reads a HTTP body from an ASGI connection.
        """
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # Bye bye.
                raise RequestAborted()
            else:
                # See if the message has body, and if it's the end, launch into
                # handling (and a synchronous subthread)
                if "body" in message:
                    body += message["body"]
                if not message.get("more_body", False):
                    break
        # Limit the maximum request data size that will be handled in-memory.
        # TODO: Stream the body to temp disk instead?
        # (we can't provide a file-like with direct reading as we would not be async)
        if (
            settings.DATA_UPLOAD_MAX_MEMORY_SIZE is not None
            and self._content_length > settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        ):
            raise RequestDataTooBig(
                "Request body exceeded settings.DATA_UPLOAD_MAX_MEMORY_SIZE."
            )
        return body

    def create_request(self, scope, body):
        """
        Creates the Request object. Returns either (request, None) or
        (None, response) if there is an error response.
        """
        try:
            return self.request_class(scope, body), None
        except UnicodeDecodeError:
            logger.warning(
                "Bad Request (UnicodeDecodeError)",
                exc_info=sys.exc_info(),
                extra={"status_code": 400},
            )
            return None, http.HttpResponseBadRequest()
        except RequestTimeout:
            # Parsing the rquest failed, so the response is a Request Timeout error
            return None, HttpResponse("408 Request Timeout (upload too slow)", status=408)
        except RequestDataTooBig:
            return None, HttpResponse("413 Payload too large", status=413)

    def handle_uncaught_exception(self, request, resolver, exc_info):
        """
        Last-chance handler for exceptions.
        """
        # There's no WSGI server to catch the exception further up if this fails,
        # so translate it into a plain text response.
        try:
            return super(AsgiHandler, self).handle_uncaught_exception(
                request, resolver, exc_info
            )
        except Exception:
            return HttpResponseServerError(
                traceback.format_exc() if settings.DEBUG else "Internal Server Error",
                content_type="text/plain",
            )

    async def send_response(self, response, send):
        """
        Encodes and sends a response out over ASGI
        """
        # Collect cookies into headers.
        # Note that we have to preserve header case as there are some non-RFC
        # compliant clients that want things like Content-Type correct. Ugh.
        response_headers = []
        for header, value in response.items():
            if isinstance(header, str):
                header = header.encode("ascii")
            if isinstance(value, str):
                value = value.encode("latin1")
            response_headers.append((bytes(header), bytes(value)))
        for c in response.cookies.values():
            response_headers.append(
                (b"Set-Cookie", c.output(header="").encode("ascii").strip())
            )
        # Make initial response message
        await send({
            "type": "http.response.start",
            "status": response.status_code,
            "headers": response_headers,
        })
        # Streaming responses need to be pinned to their iterator
        if response.streaming:
            # Access `__iter__` and not `streaming_content` directly in case
            # it has been overridden in a subclass.
            for part in response:
                for chunk, _ in cls.chunk_bytes(part):
                    await send({
                        "type": "http.response.body",
                        "body": chunk,
                        # We ignore "more" as there may be more parts; instead,
                        # we use an empty final closing message with False.
                        "more_body": True,
                    })
            # Final closing message
            await send({"type": "http.response.body"})
        # Other responses just need chunking
        else:
            # Yield chunks of response
            for chunk, last in cls.chunk_bytes(response.content):
                await send({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": not last,
                })
        response.close()

    @classmethod
    def chunk_bytes(cls, data):
        """
        Chunks some data up so it can be sent in reasonable size messages.
        Yields (chunk, last_chunk) tuples.
        """
        position = 0
        if not data:
            yield data, True
            return
        while position < len(data):
            yield (
                data[position : position + cls.chunk_size],
                (position + cls.chunk_size) >= len(data),
            )
            position += cls.chunk_size

    def get_script_prefix(self, scope):
        """
        Returns the script prefix to use from either the scope or a setting.
        """
        if settings.FORCE_SCRIPT_NAME:
            return settings.FORCE_SCRIPT_NAME
        return scope.get("root_path", "") or ""