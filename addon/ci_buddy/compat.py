"""Seam 10 — unsupported-Anki tripwire (a WARNING, never a gate).

ci-buddy ships baked into the managed image next to a pinned ``aqt``, and every
lock seam is deliberately fail-open — so an unverified Anki degrades silently.
This module compares ``anki.buildinfo.version`` against the hardcoded
``core.SUPPORTED_ANKI_VERSIONS`` matrix at add-on load time and, on a mismatch:

- prints a loud, greppable multi-line warning to stderr (pod logs), marker
  ``CI_BUDDY_UNSUPPORTED_ANKI``;
- prepends a big red bold banner to the deck browser (the landing screen of
  the operator's VNC sanity check) via ``deck_browser_will_render_content``;
- appends a persistent red bold badge to the top toolbar via
  ``top_toolbar_did_init_links``.

Anki still starts and every seam still applies normally. When the version IS
supported (or the ``warn_unsupported_anki`` gate is off): zero output, zero
hooks registered.

MUST NOT import ``locks`` or ``provisioners`` — the top-level modules stay
decoupled. ``aqt``/``anki`` are imported lazily so ``import ci_buddy.compat``
works without a running Anki and everything here is unit-testable directly.
Every piece — the buildinfo import, the manifest read, hook registration, the
HTML injection — is individually guarded: a future Anki that reshapes any of
them degrades to "no banner" with a logged skip, never a crash.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from . import core


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a hook handler so any exception is logged, never propagated."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — fail open by design
            print(f"[ci_buddy] compat handler {fn.__name__} failed: {exc!r}")
        return None

    return wrapper


def read_anki_version() -> str | None:
    """Return ``anki.buildinfo.version`` — the runtime source of truth Anki
    itself uses (``anki.utils.version_with_build``) — or ``None`` when it is
    missing/unreadable/not a string. ``None`` is treated as unsupported by
    ``core.anki_version_supported`` (check failure trips the warning, it never
    crashes)."""
    try:
        from anki.buildinfo import version  # lazy — no anki at module load
    except Exception:  # noqa: BLE001 — any import failure ⇒ version unknown
        return None
    return version if isinstance(version, str) else None


def read_ci_buddy_version() -> str:
    """Return ci-buddy's own ``human_version`` from the ``manifest.json`` next
    to this package (single source of truth — no hardcoded copy to drift), or
    ``"unknown"`` on any failure."""
    try:
        manifest_path = os.path.join(os.path.dirname(__file__), "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        human_version = manifest.get("human_version")
        if isinstance(human_version, str) and human_version:
            return human_version
    except (OSError, ValueError, AttributeError):
        pass
    return "unknown"


def emit_unsupported_warning(
    found: str | None,
    ci_buddy_version: str,
    printer: Callable[[str], None] | None = None,
) -> None:
    """Print the multi-line ``CI_BUDDY_UNSUPPORTED_ANKI`` warning to stderr
    (pod logs). ``printer`` is injectable for tests; the default writes each
    line to ``sys.stderr``."""
    if printer is None:

        def printer(line: str) -> None:
            print(line, file=sys.stderr)

    for line in core.unsupported_anki_warning_lines(found, ci_buddy_version):
        printer(line)


def on_deck_browser_will_render_content(
    _deck_browser: Any, content: Any, banner_html: str
) -> None:
    """Prepend the red warning banner to the deck-browser content.

    ``content`` is ``aqt.deckbrowser.DeckBrowserContent`` with two str fields:
    ``tree`` (the deck table rows, rendered at the TOP of the body inside
    ``<table>…</table>``) and ``stats`` (rendered BELOW the tree) — verified
    against qt/aqt/deckbrowser.py (``DeckBrowserContent`` + ``_body``). We
    prepend to ``tree``: per the HTML5 tree-construction spec a non-table
    element opened "in table" mode is foster-parented BEFORE the ``<table>``,
    so the ``<div>`` banner renders above the deck list — the top of the
    landing screen. Prepending to ``stats`` would put it at the bottom.

    Fail-open: a future ``DeckBrowserContent`` without a str ``tree`` is a
    logged skip — never an AttributeError in Anki's render path."""
    tree = getattr(content, "tree", None)
    if not isinstance(tree, str):
        print(
            "[ci_buddy] warning: deck-browser content has no str 'tree' field "
            "on this Anki version (unsupported-Anki banner skipped)"
        )
        return
    content.tree = banner_html + "\n" + tree


def on_top_toolbar_did_init_links(
    links: list[str], _toolbar: Any, badge_html: str
) -> None:
    """Append the red warning badge to the top-toolbar links so it persists on
    every screen. ``links`` is a plain list of HTML strings joined with
    newlines (aqt/toolbar.py ``_centerLinks``); a non-link ``<span>`` is a
    valid entry. Mutates ``links`` in place, as the hook contract requires."""
    links.append(badge_html)


def register(config: dict[str, Any]) -> None:
    """Run the compatibility check and wire the warning surfaces (Seam 10).

    Gated as a whole by ``warn_unsupported_anki``. On a supported version this
    returns without any output and without registering any hook (no
    do-nothing handlers). On an unsupported/unknown version it emits the
    stderr warning first (so the log line lands even if the GUI wiring fails),
    then registers both GUI surfaces — each hook individually fail-open.
    """
    if not config.get("warn_unsupported_anki", True):
        return

    found = read_anki_version()
    if core.anki_version_supported(found):
        return  # verified version — zero output, zero hooks

    ci_buddy_version = read_ci_buddy_version()
    _safe(emit_unsupported_warning)(found, ci_buddy_version)

    from aqt import gui_hooks  # lazy — keeps this module import-safe for tests

    banner_html = core.unsupported_anki_deck_banner_html(found, ci_buddy_version)
    badge_html = core.unsupported_anki_toolbar_html(found, ci_buddy_version)
    surfaces: tuple[tuple[str, Callable[..., Any]], ...] = (
        (
            "deck_browser_will_render_content",
            lambda deck_browser, content: on_deck_browser_will_render_content(
                deck_browser, content, banner_html
            ),
        ),
        (
            "top_toolbar_did_init_links",
            lambda links, toolbar: on_top_toolbar_did_init_links(
                links, toolbar, badge_html
            ),
        ),
    )
    for hook_name, handler in surfaces:
        hook = getattr(gui_hooks, hook_name, None)
        append = getattr(hook, "append", None)
        if not callable(append):
            print(
                f"[ci_buddy] warning: gui_hooks.{hook_name} not present on "
                "this Anki version (unsupported-Anki warning surface skipped)"
            )
            continue
        append(_safe(handler))
