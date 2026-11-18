"""测试通过路由匹配接口取出处理函数，以便精确控制并发顺序。"""
from starlette.routing import Match


def route_endpoint(app, path, method="POST"):
    scope = {
        "type": "http",
        "path": path,
        "root_path": "",
        "method": method,
        "headers": [],
    }
    def find(routes):
        for route in routes:
            match, child = route.matches(scope)
            if match != Match.FULL:
                continue
            if "endpoint" in child:
                return child["endpoint"]
            included = getattr(route, "original_router", None)
            if included is not None:
                endpoint = find(included.routes)
                if endpoint is not None:
                    return endpoint

    if (endpoint := find(app.routes)) is not None:
        return endpoint
    raise LookupError(f"{method} {path}")
