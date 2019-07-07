import sys
from http import HTTPStatus
import re
import enum
from functools import wraps
import json
from wsgiref.simple_server import make_server
from urllib.parse import parse_qs


class PythonVersionError(Exception):
    pass


if sys.version_info[:2] < (3, 7):
    raise PythonVersionError("sakurapy supports python3.7+.")


def memoize(method):
    memo_name = f"_{method.__name__}"

    @wraps(method)
    def _inner(self, *args, **kwargs):
        if not hasattr(self, memo_name):
            setattr(self, memo_name, method(self, *args, **kwargs))
        return getattr(self, memo_name)

    return _inner


def http_status_code_message(status):
    formatted = " ".join((s.capitalize() for s in status.name.split("_")))
    return f"{status.value} {formatted}"


class ReservedPathError(Exception):
    pass


class InvalidHttpMethod(Exception):
    pass


class HttpMethod(enum.Enum):
    GET = "get"
    POST = "post"
    PUT = "put"
    DELETE = "delete"


class Request:
    def __init__(self, environ, method, body):
        self.environ = environ
        self.method = method
        self.body = parse_qs(body) or {}


class HttpHeader(dict):
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def as_key_value_pairs(self):
        return [
            ("-".join(s.capitalize() for s in k.split("_")), v) for k, v in self.items()
        ]


class Response:
    def __init__(self, headers, status, body):
        self.headers = headers
        self.status = status
        self.body = body


class Service:
    def __init__(self):
        self.resource_map = {}

    def service(self, resource):
        if resource.path in self.resource_map:
            raise ReservedPathError(resource.path)
        self.resource_map[resource.path] = resource
        return self


class App(Service):
    @property
    def resource_path_map(self):
        if not hasattr(self, "_resource_path_map"):

            def loop(path, value, tail, acc):
                if isinstance(value, scope):
                    [(p, v), *t] = value.resource_map.items()
                    loop(f"{path}{p}", v, t, acc)
                else:
                    acc[path] = value
                if not tail:
                    return acc
                [(p, v), *t] = tail
                return loop(p, v, t, acc)

            [(path, value), *tail] = self.resource_map.items()
            self._resource_path_map = loop(path, value, tail, {})
        return self._resource_path_map

    @property
    def resource_paths(self):
        return self.resource_path_map.keys()

    def _from_query_string(self, environ):
        return environ["QUERY_STRING"]

    def _from_stream(self, environ):
        try:
            request_body_size = int(environ.get("CONTENT_LENGTH", 0))
        except ValueError:
            request_body_size = 0
        return environ["wsgi.input"].read(request_body_size).decode()

    def _abort(self, e, *args, **kwargs):
        raise e(*args, **kwargs)

    def _build_request_body(self, method, environ):
        return {
            HttpMethod.GET: self._from_query_string,
            HttpMethod.POST: self._from_stream,
        }.get(method, lambda _: self._abort(InvalidHttpMethod, method))(environ)

    def _find_matched_path(self, path):
        filterd = [
            x for x in [(p, re.match(p, path)) for p in self.resource_paths] if x[1]
        ]
        if filterd:
            return {"name": filterd[0][0], "matched_object": filterd[0][1]}
        return

    def _build_template_response(self, status):
        body = http_status_code_message(status)
        headers = HttpHeader(content_type="text/plain", content_rength=str(len(body)))
        return Response(headers, status, body)

    @property  # type: ignore
    @memoize
    def not_found_response(self):
        return self._build_template_response(HTTPStatus.NOT_FOUND)

    @property  # type: ignore
    @memoize
    def method_not_allowed_response(self):
        return self._build_template_response(HTTPStatus.METHOD_NOT_ALLOWED)

    def _build_ok_response(self, environ, request_body, resource, path_params):
        response = resource.handler(
            Request(environ, resource.method, request_body), **path_params
        )
        content_type = "text/plain"
        if isinstance(response, dict):
            response = json.dumps(response)
            content_type = "application/json"
        return Response(
            HttpHeader(content_type=content_type, content_length=str(len(response))),
            HTTPStatus.OK,
            response,
        )

    def __call__(self, environ, start_response):
        path: str = environ["PATH_INFO"]
        method = getattr(HttpMethod, environ["REQUEST_METHOD"])
        request_body = self._build_request_body(method, environ)
        matched_path = self._find_matched_path(path)
        content_type = "text/plain"

        if not matched_path:
            response = self.not_found_response
        else:
            resource = self.resource_path_map[matched_path["name"]]
            if not resource.is_allowed_method(method):
                response = self.method_not_allowed_response
            else:
                response = self._build_ok_response(
                    environ,
                    request_body,
                    resource,
                    matched_path["matched_object"].groupdict(),
                )
        start_response(response.status, response.headers)
        return [response.body.encode()]


class Server:
    def __init__(self, app):
        self.app = app

    def bind(self, host, port):
        self.host = host
        self.port = port
        return self

    def run(self):
        with make_server(self.host, self.port, self.app) as httpd:
            print(f"Start server {self.host}:{self.port}")
            httpd.serve_forever()


class resource:
    def __new__(cls, *args, **kwargs):
        for method in list(HttpMethod):

            def _(m: HttpMethod):
                def __(self, handler):
                    return self._to(handler, m)

                return __

            setattr(cls, method.value, _(method))
        return super().__new__(cls)

    def __init__(self, path: str):
        self.path = path

    def _to(self, handler, method: HttpMethod) -> "resource":
        self.handler = handler
        self.method = method
        return self

    def is_allowed_method(self, method):
        return self.method == method


class scope(Service):
    def __init__(self, path):
        super().__init__()
        self.path = path


def method_wrapper(method: HttpMethod):
    def _inner(path: str):
        def __inner(handler):
            @wraps(handler)
            def ___inner(request: Request, *args, **kwargs):
                return handler(request, *args, **kwargs)

            return getattr(resource(path), method.value)(___inner)

        return __inner

    return _inner


get = method_wrapper(HttpMethod.GET)
post = method_wrapper(HttpMethod.POST)
put = method_wrapper(HttpMethod.PUT)
delete = method_wrapper(HttpMethod.DELETE)