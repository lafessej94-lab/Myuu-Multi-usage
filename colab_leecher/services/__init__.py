"""
colab_leecher/services/__init__.py

Callback-data dispatcher for every inline-button flow that used to live in
__main__.py's single giant `callbacks()` if/elif chain (everything except
the video-tools menu and the link section-menu navigation, which stay in
colab_leecher/video_menu.py and colab_leecher/link_menu.py respectively —
see the note in each of those files).

This is intentionally NOT a second Pyrogram `@on_callback_query()` handler
per module (that would reopen the group-ordering bug already fixed once for
video_menu.py, where an unfiltered handler earlier in the chain can eat
callback_data meant for a later, filtered handler). Instead, __main__.py
keeps its single `@colab_bot.on_callback_query()` entry point and calls
`services.dispatch(client, cq)` from it — routing happens here, in plain
Python, with no Pyrogram propagation semantics involved.

Each services/*_flow.py / *_menu.py module registers its handlers at import
time with the decorators below:

    from colab_leecher.services import on_exact, on_prefix

    @on_exact("cb_help", "cb_settings")
    async def handle(client, cq, data): ...

    @on_prefix("status_kill|")
    async def handle_kill(client, cq, data): ...

`on_exact` is for plain string callback_data ("close", "cancel", "video"...).
`on_prefix` is for the namespaced ones that carry extra data after a
delimiter ("status_kill|1234", "cc_res|720p"...). A callback_data is looked
up as an exact match first, then against every registered prefix in
registration order — none of the current prefixes overlap (see the
extraction manifest in each module's docstring), but if a new one ever
does, put the more specific module's import first in the list at the
bottom of this file.
"""
from __future__ import annotations

from typing import Awaitable, Callable

Handler = Callable[..., Awaitable[None]]

_EXACT: dict[str, Handler] = {}
_PREFIXES: list[tuple[str, Handler]] = []


def on_exact(*keys: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        for key in keys:
            if key in _EXACT:
                raise RuntimeError(f"callback_data {key!r} already registered (duplicate on_exact)")
            _EXACT[key] = fn
        return fn
    return deco


def on_prefix(*prefixes: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        for prefix in prefixes:
            _PREFIXES.append((prefix, fn))
        return fn
    return deco


async def dispatch(client, cq) -> bool:
    """Try every registered handler for cq.data. Returns True if one ran."""
    data = cq.data or ""
    fn = _EXACT.get(data)
    if fn is None:
        for prefix, handler in _PREFIXES:
            if data.startswith(prefix):
                fn = handler
                break
    if fn is None:
        return False
    await fn(client, cq, data)
    return True


# NOTE: submodules are intentionally NOT auto-imported here.
#
# colab_leecher/claude_agent.py does `from colab_leecher.services.subtitle_probe
# import ...`, and importing ANY submodule of a package first fully executes
# that package's __init__.py. If this file eagerly imported claude_menu (which
# needs `from colab_leecher.claude_agent import run_manual_hardsub, ...`),
# that would run while claude_agent.py is still mid-import — a circular
# import (claude_agent -> services.__init__ -> claude_menu -> claude_agent).
#
# Instead, colab_leecher/__main__.py explicitly imports every *_menu.py /
# *_flow.py submodule (for its registration side effects) AFTER
# colab_leecher.claude_agent has already finished loading. Each submodule
# still only needs `on_exact`/`on_prefix` from this file, which are defined
# above and available as soon as this module object exists — so the
# submodules themselves have no trouble importing from here.
