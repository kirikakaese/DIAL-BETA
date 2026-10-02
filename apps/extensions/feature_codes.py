"""Phone-side call forwarding via feature codes (``feature-code`` hook handler).

The number plan defines four codes (defaults in brackets):

- ``forward_set_code`` [``*21``]      ``*21<number>`` → forward *always* to ``<number>``
- ``forward_busy_code`` [``*22``]     ``*22<number>`` → forward when busy
- ``forward_noanswer_code`` [``*23``] ``*23<number>`` → forward on no answer
- ``forward_clear_code`` [``*20``]    ``*20``          → all forwarding off

The caller is resolved to their active extension (``CALLERID(num)``), the target must be a live extension of
the same event and pass the same loop/self checks as the web UI (``validate_forward_target``). Changes go
through :func:`apps.extensions.services.set_forwarding` so they are audited, emit ``extension.updated`` and
re-provision the extension exactly like the portal does.
"""
from __future__ import annotations

import logging

from apps.callback.services import active_extension
from apps.extensions.models import Extension
from apps.extensions.services import (
    ExtensionError,
    get_plan,
    resolve_forward_target,
    set_forwarding,
    validate_forward_target,
)

log = logging.getLogger("dial.extensions.feature_codes")

FM = Extension.ForwardMode


def code_modes(plan) -> dict[str, str]:
    """Configured feature code → forwarding mode (empty codes are skipped)."""
    pairs = ((plan.forward_set_code, FM.ALWAYS), (plan.forward_busy_code, FM.BUSY),
             (plan.forward_noanswer_code, FM.NOANSWER), (plan.forward_clear_code, FM.OFF))
    return {code.strip(): mode for code, mode in pairs if (code or "").strip()}


def handle_feature_code(event, caller: str, code: str, target: str) -> bool:
    """``feature-code`` hook. Returns ``True`` when ``code`` is a forwarding code and was applied."""
    code = (code or "").strip()
    target = (target or "").strip()
    if not code:
        return False
    mode = code_modes(get_plan(event)).get(code)
    if mode is None:
        return False
    ext = active_extension(event, caller)
    if ext is None:
        log.info("forward code %s from unknown caller %r in %s", code, caller, event.slug)
        return False
    try:
        if mode == FM.OFF:
            set_forwarding(ext, ext.owner, mode=FM.OFF, target=None)
            return True
        if not target:
            return False
        dest = resolve_forward_target(event, target)
        if dest is None:
            log.info("forward code %s from %s: target %r is not a live extension", code, ext.number, target)
            return False
        validate_forward_target(ext, dest)
        set_forwarding(ext, ext.owner, mode=mode, target=dest)
        return True
    except ExtensionError as exc:
        log.info("forward code %s from %s rejected: %s", code, ext.number, exc)
        return False
