"""Pure-logic helpers for ci-buddy — NO ``aqt``/``anki`` imports.

Everything here is a plain function or dataclass so it can be unit-tested
directly, without a running Anki. The GUI-touching code lives in
``locks.py`` / ``provisioning.py`` and delegates the decisions to these helpers.

Kept deliberately dependency-free (stdlib only).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# Branding
# --------------------------------------------------------------------------- #

#: Human-facing label for the managed/locked appliance. Single source of truth
#: (spec: "Keep brand strings in one constant").
DEFAULT_MANAGED_BY_LABEL = "Managed environment — settings are locked"

# --------------------------------------------------------------------------- #
# Config surface (spec §4)
# --------------------------------------------------------------------------- #

#: The complete set of add-on defaults. Mirrors ``config.json`` exactly; the two
#: must stay in sync. ``config.json`` is the shipped default that Anki merges the
#: user's overrides onto; this dict is the fallback used when a key is missing.
DEFAULT_CONFIG: dict[str, Any] = {
    "managed_by_label": DEFAULT_MANAGED_BY_LABEL,
    "hide_vs_disable": "hide",
    "lock_preferences": True,
    "lock_addons": True,
    "lock_addon_config_writes": True,
    "lock_profile_switch": True,
    "lock_upgrade_downgrade": True,
    "lock_note_types": False,
    "lock_import": True,
    "lock_open_backup": True,
    "lock_database_check": False,
    "lock_debug_console": True,
    "strip_sync_link": False,
    "provisioning_enabled": False,
    "credentials_path": "/run/ankimcp/sync-credentials.json",
    "credentials_poll_seconds": 2,
    "endpoint_allowlist": ["ankiweb.net", "ankiuser.net"],
    "clear_key_on_close": True,
    "disable_native_auto_sync": False,
    "ensure_latex_generation": True,
    "hide_ankimcp_toolbar_indicator": True,
}

#: Known config keys (used to warn about typos / stale keys — never crash).
KNOWN_CONFIG_KEYS: frozenset[str] = frozenset(DEFAULT_CONFIG)

#: Maps a boolean config key to the ``mw.form`` action attribute it locks.
#: ``actionNoteTypes`` is intentionally absent: Manage Note Types must stay
#: usable (spec §A.4). The ordering matches the Seam-1 table in the spec.
LOCK_ACTION_MAP: dict[str, str] = {
    "lock_preferences": "actionPreferences",
    "lock_addons": "actionAdd_ons",
    "lock_profile_switch": "actionSwitchProfile",
    "lock_upgrade_downgrade": "action_upgrade_downgrade",
    "lock_note_types": "actionNoteTypes",
    "lock_import": "actionImport",
    "lock_open_backup": "action_open_backup",
    "lock_database_check": "actionFullDatabaseCheck",
}

#: Maps a boolean config key to the exact ``aqt.DialogManager`` registry name
#: that Seam 2 disables when that key is set. EXACT match only — an over-broad
#: filter would break AddCards/Browser/EditCurrent/etc.
DIALOG_LOCK_MAP: dict[str, str] = {
    "lock_preferences": "Preferences",
    "lock_addons": "AddonsDialog",
}


def locked_dialog_names(config: dict[str, Any]) -> set[str]:
    """Return the set of dialog registry names Seam 2 should disable, derived
    from config: ``"Preferences"`` iff ``lock_preferences`` and
    ``"AddonsDialog"`` iff ``lock_addons`` (spec §A.3 Seam 2).

    Keeps ``on_dialog_opened`` config-gated instead of hardcoded, so a deployment
    that unlocks Preferences/Add-ons doesn't get a stray programmatic-open guard.
    """
    return {
        name
        for cfg_key, name in DIALOG_LOCK_MAP.items()
        if bool(config.get(cfg_key, False))
    }


def merge_config(user: dict[str, Any] | None) -> dict[str, Any]:
    """Return DEFAULT_CONFIG overlaid with the user's values.

    Anki already merges ``config.json`` defaults with the user's ``meta.json``,
    but we defensively re-apply our in-code defaults so a partial/None config
    (e.g. a hand-mounted file that omits keys) can never raise ``KeyError``.
    """
    merged = dict(DEFAULT_CONFIG)
    if user:
        merged.update(user)
    return merged


def unknown_config_keys(config: dict[str, Any]) -> list[str]:
    """Return config keys that are not recognised (typos / stale keys)."""
    return sorted(k for k in config if k not in KNOWN_CONFIG_KEYS)


def warn_unknown_keys(
    config: dict[str, Any], printer: Callable[[str], None] = print
) -> list[str]:
    """Print a warning for every unrecognised config key. Never raises."""
    unknown = unknown_config_keys(config)
    for key in unknown:
        printer(f"[ci_buddy] warning: unknown config key {key!r} (ignored)")
    return unknown


def hide_actions(config: dict[str, Any]) -> bool:
    """True → ``setVisible(False)``; False → ``setEnabled(False)`` (grey out).

    Anything other than the exact string ``"disable"`` means hide (the safe,
    spec-default behaviour).
    """
    return config.get("hide_vs_disable", "hide") != "disable"


def action_lock_plan(config: dict[str, Any]) -> dict[str, bool]:
    """Map ``mw.form`` action attr → whether it should be locked.

    Only truthy config flags produce a locked action; keeps locks.py a dumb
    iterator over a decision made here (unit-testable).
    """
    return {
        attr: bool(config.get(cfg_key, False))
        for cfg_key, attr in LOCK_ACTION_MAP.items()
    }


# --------------------------------------------------------------------------- #
# Provisioning — credential parsing / decisions (spec Part B)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Credentials:
    """A validated sync-credentials record (see the file contract, spec §B.2)."""

    serial: int
    hkey: str
    endpoint: str | None = None
    username: str | None = None


class CredentialsError(ValueError):
    """Raised when the credentials file is malformed. Callers treat as no-op."""


def parse_credentials(raw: str) -> Credentials:
    """Parse + validate the credentials JSON. Raise ``CredentialsError`` on any
    problem (corrupt JSON, wrong types, missing required fields).

    Required: ``serial`` (int), ``hkey`` (non-empty str).
    Optional: ``endpoint`` (str), ``username`` (str). ``v`` is accepted and,
    if present, must be ``1`` — an unknown schema version is rejected rather
    than mis-applied.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CredentialsError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise CredentialsError("top-level JSON value is not an object")

    version = data.get("v", 1)
    if version != 1:
        raise CredentialsError(f"unsupported schema version {version!r}")

    serial = data.get("serial")
    # bool is a subclass of int — exclude it explicitly.
    if not isinstance(serial, int) or isinstance(serial, bool):
        raise CredentialsError("'serial' must be an integer")

    hkey = data.get("hkey")
    if not isinstance(hkey, str) or not hkey:
        raise CredentialsError("'hkey' must be a non-empty string")

    endpoint = data.get("endpoint")
    if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
        raise CredentialsError("'endpoint' must be a non-empty string if present")

    username = data.get("username")
    if username is not None and not isinstance(username, str):
        raise CredentialsError("'username' must be a string if present")

    return Credentials(
        serial=serial,
        hkey=hkey,
        endpoint=endpoint,
        username=username or None,
    )


def should_inject(new_serial: int, last_applied_serial: int | None) -> bool:
    """Inject only when the serial strictly increases (spec §B.2/B.3).

    ``last_applied_serial is None`` means "nothing applied yet" → always inject.
    A same-or-lower serial (including rollback) is a no-op.
    """
    if last_applied_serial is None:
        return True
    return new_serial > last_applied_serial


def endpoint_allowed(endpoint: str, allowlist: Iterable[str]) -> bool:
    """True if ``endpoint`` has an http(s) scheme and its host is in the
    allowlist (exact host or a subdomain of an allowlisted host).

    Anything that fails to parse, uses a non-http scheme, or points at a host
    outside the allowlist is rejected → caller keeps the default endpoint
    (spec §B.4, exfil hardening).
    """
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False

    if parts.scheme not in ("http", "https"):
        return False

    host = (parts.hostname or "").lower()
    if not host:
        return False

    for allowed in allowlist:
        allowed = str(allowed).lower().strip()
        if not allowed:
            continue
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def hkey_fingerprint(hkey: str) -> str:
    """Non-reversible short fingerprint (first 8 hex of sha256) for logging.

    The hkey itself must NEVER be logged (spec §B.8); log this instead.
    """
    return hashlib.sha256(hkey.encode("utf-8")).hexdigest()[:8]


def is_sync_toolbar_link(link_html: str) -> bool:
    """True if the toolbar link HTML is the built-in *sync* link.

    Anki builds it with ``id="sync"`` (aqt/toolbar.py ``_create_sync_link``);
    match that exact attribute. The nested spinner uses ``id=sync-spinner``
    (no quotes), so this does not false-positive on it.
    """
    return 'id="sync"' in link_html


# --------------------------------------------------------------------------- #
# Sibling add-on coercion — hide the AnkiMCP top-toolbar indicator
# --------------------------------------------------------------------------- #

#: Attribute under which locks Seam 4's write-dropping ``writeConfig`` shim
#: stashes the genuine original ``writeConfig``. Single source of truth for the
#: producer (``locks.install_write_config_shim``) and the consumer
#: (``provisioning._real_write_config``) — both modules already import ``core``,
#: so the contract has one name and can't silently drift between them.
ORIGINAL_WRITE_CONFIG_ATTR = "_ci_buddy_original"

#: The AnkiMCP server add-on's manifest ``package`` field — the STABLE identity
#: ci-buddy searches for among installed add-ons. This is NOT necessarily the
#: on-disk directory name: on the hosted image AnkiMCP is installed under the
#: directory ``ankimcp`` while its ``manifest.json`` still declares
#: ``"package": "anki_mcp_server"``. Anki keys ``addonManager.getConfig`` /
#: ``writeConfig`` by the *directory* name, so ci-buddy resolves the directory
#: from this package (see ``resolve_addon_dir_by_package``) instead of assuming
#: the two are equal — they only coincide on a source/dev install.
ANKIMCP_PACKAGE = "anki_mcp_server"

#: The AnkiMCP config key that renders the persistent "[• AnkiMCP]" button in
#: Anki's top toolbar. ci-buddy forces this false in the managed environment.
ANKIMCP_TOOLBAR_INDICATOR_KEY = "show_toolbar_indicator"


def resolve_addon_dir_by_package(
    installed: Iterable[tuple[str, str | None]],
    package: str,
    printer: Callable[[str], None] = print,
) -> str | None:
    """Return the *directory* name of the installed add-on whose manifest
    ``package`` equals ``package``, or ``None`` if none match.

    ``installed`` is an iterable of ``(dir_name, manifest_package)`` pairs — one
    per installed add-on. ``manifest_package`` is ``None`` for an add-on with no
    or unreadable manifest, which therefore never matches.

    Anki keys ``addonManager.getConfig``/``writeConfig`` by the on-disk directory
    name, but that name is not stable across installs: on the hosted image
    AnkiMCP lives in directory ``ankimcp`` while its manifest still declares
    ``package = "anki_mcp_server"``. Matching on the manifest package and
    returning the *directory* is what lets ci-buddy address the sibling add-on
    regardless of how it was installed (the historical bug assumed the directory
    name equalled the package and silently no-op'd on the hosted image).

    First match wins (deterministic, in iteration order). On a multi-match a
    distinctive line is logged and the first is used; on no match a distinctive,
    greppable warning is logged (fail-open — the caller then no-ops). ``printer``
    is injected so this stays pure and unit-testable.
    """
    matches = [dir_name for dir_name, pkg in installed if pkg == package]
    if not matches:
        printer(
            f"[ci_buddy] CI_BUDDY_ADDON_PACKAGE_NOT_FOUND — no installed add-on "
            f"declares manifest package {package!r}; cannot resolve its "
            "directory (dependent feature no-op)"
        )
        return None
    if len(matches) > 1:
        printer(
            f"[ci_buddy] multiple add-ons declare manifest package {package!r} "
            f"({matches!r}); using the first ({matches[0]!r})"
        )
    return matches[0]


def plan_hide_ankimcp_indicator(
    ankimcp_config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Decide what (if anything) to write back into AnkiMCP's config to hide its
    top-toolbar indicator.

    Returns a NEW config dict (a shallow copy with ``show_toolbar_indicator``
    forced to ``False``) when a durable write is needed, or ``None`` for a safe
    no-op:

    - ``ankimcp_config`` is ``None``/empty → AnkiMCP is not installed or exposes
      no config → nothing to do.
    - the key is already exactly ``False`` → idempotent no-op (same write-only-
      when-needed spirit as ``CollectionConfigProvisioner.ensure_render_latex``),
      so ci-buddy never re-writes ``meta.json`` on a subsequent load.

    Pure/decision-only: the caller performs the actual write.
    """
    if not ankimcp_config:
        return None
    if ankimcp_config.get(ANKIMCP_TOOLBAR_INDICATOR_KEY) is False:
        return None
    new_config = dict(ankimcp_config)
    new_config[ANKIMCP_TOOLBAR_INDICATOR_KEY] = False
    return new_config
