
from trapdoor import handlers

HANDLERS = [
    (r"/", handlers.Index),
    (r"/resolve/?", handlers.Resolve),
    (r"/resolve_all/?", handlers.ResolveAll),

    # API
    (r"/api/varbinds/(?P<notification_id>\d+)", handlers.ApiVarBinds),

    # Default
    (r"/.*", handlers.NotFound),
]
