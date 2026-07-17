"""Unit tests for the pure-logic helpers (no aqt)."""

import json
from pathlib import Path

import pytest

from ci_buddy import core


# --- config -------------------------------------------------------------- #


def test_default_config_matches_known_keys():
    assert core.KNOWN_CONFIG_KEYS == frozenset(core.DEFAULT_CONFIG)


def test_shipped_config_json_matches_default_config():
    # DEFAULT_CONFIG documents itself as mirroring config.json exactly; enforce
    # it so the shipped default and the in-code fallback can never drift (keys
    # AND values). config.json sits next to the package: addon/ci_buddy/.
    config_path = Path(core.__file__).with_name("config.json")
    shipped = json.loads(config_path.read_text(encoding="utf-8"))
    assert shipped == core.DEFAULT_CONFIG


def test_merge_config_fills_defaults():
    merged = core.merge_config({"lock_preferences": False})
    assert merged["lock_preferences"] is False
    # untouched keys fall back to defaults
    assert merged["lock_addons"] is True
    assert merged["ensure_latex_generation"] is True


def test_merge_config_none_is_all_defaults():
    assert core.merge_config(None) == core.DEFAULT_CONFIG
    assert core.merge_config({}) == core.DEFAULT_CONFIG


def test_unknown_keys_detected_and_warned():
    warnings: list[str] = []
    config = core.merge_config({"lock_typo": True, "another_bad": 1})
    unknown = core.warn_unknown_keys(config, printer=warnings.append)
    assert unknown == ["another_bad", "lock_typo"]
    assert len(warnings) == 2
    assert all("unknown config key" in w for w in warnings)


def test_hide_vs_disable():
    assert core.hide_actions(core.merge_config({"hide_vs_disable": "hide"})) is True
    assert core.hide_actions(core.merge_config({"hide_vs_disable": "disable"})) is False
    # anything unexpected → hide (safe default)
    assert core.hide_actions(core.merge_config({"hide_vs_disable": "??"})) is True


def test_action_lock_plan_respects_flags_and_keeps_note_types():
    plan = core.action_lock_plan(core.DEFAULT_CONFIG)
    assert plan["actionPreferences"] is True
    assert plan["actionAdd_ons"] is True
    assert plan["actionImport"] is True
    assert plan["action_open_backup"] is True
    # the v0.7.0 per-item File locks are on by default
    assert plan["actionExport"] is True
    assert plan["action_create_backup"] is True
    assert plan["actionExit"] is True
    # Check for Updates (26.05+) is locked by default too
    assert plan["action_check_for_updates"] is True
    # defaults keep these enabled
    assert plan["actionNoteTypes"] is False
    assert plan["actionFullDatabaseCheck"] is False
    # Switch Profile stays usable by default (multi-profile support, v0.7.0)
    assert plan["actionSwitchProfile"] is False


def test_new_file_locks_are_gateable():
    plan = core.action_lock_plan(
        core.merge_config(
            {"lock_export": False, "lock_create_backup": False, "lock_exit": False}
        )
    )
    assert plan["actionExport"] is False
    assert plan["action_create_backup"] is False
    assert plan["actionExit"] is False


def test_note_types_never_in_lock_map_by_accident():
    # actionNoteTypes is present but only lockable via explicit lock_note_types.
    plan = core.action_lock_plan(core.merge_config({"lock_note_types": False}))
    assert plan["actionNoteTypes"] is False


def test_check_for_updates_lock_is_gateable():
    plan = core.action_lock_plan(
        core.merge_config({"lock_check_for_updates": False})
    )
    assert plan["action_check_for_updates"] is False


def test_menu_lock_plan_keeps_file_menu_by_default():
    # menuCol is the File menu's object name in aqt forms/main.ui. Since
    # v0.7.0 (multi-profile support) the whole-menu lock is OFF by default —
    # the per-item File locks leave only Switch Profile visible.
    assert core.menu_lock_plan(core.DEFAULT_CONFIG) == {"menuCol": False}


def test_menu_lock_plan_respects_gate_on():
    plan = core.menu_lock_plan(core.merge_config({"lock_file_menu": True}))
    assert plan == {"menuCol": True}


def test_profile_manager_lock_plan_locks_dangerous_buttons_by_default():
    # Button object names verified against aqt forms/profiles.ui. Open / Add /
    # Rename / Delete (login/add/rename/delete_2) are deliberately NOT in the
    # map — users manage their own profiles.
    assert core.profile_manager_lock_plan(core.DEFAULT_CONFIG) == {
        "openBackup": True,
        "quit": True,
        "downgrade_button": True,
    }


def test_profile_manager_lock_plan_gates_are_individually_toggleable():
    plan = core.profile_manager_lock_plan(
        core.merge_config({"lock_profile_manager_quit": False})
    )
    assert plan == {
        "openBackup": True,
        "quit": False,
        "downgrade_button": True,
    }


def test_profile_manager_lock_map_never_touches_profile_management_buttons():
    # The kept buttons must never creep into the lock map by accident.
    for kept in ("login", "add", "rename", "delete_2"):
        assert kept not in core.PROFILE_MANAGER_LOCK_MAP.values()


def test_update_check_setters_pin_the_pm_contract():
    # The exact mw.pm setter names Seam 7 calls (aqt/profiles.py); a rename in
    # aqt must be caught here, not by a runtime warning alone.
    assert core.UPDATE_CHECK_SETTERS == (
        "set_update_check",
        "set_check_for_addon_updates",
    )


# --- Seam 2 config-gated dialog names (MEDIUM-2) ------------------------- #


@pytest.mark.parametrize(
    "lock_prefs,lock_addons,expected",
    [
        (True, True, {"Preferences", "AddonsDialog"}),
        (True, False, {"Preferences"}),
        (False, True, {"AddonsDialog"}),
        (False, False, set()),
    ],
)
def test_locked_dialog_names_all_combos(lock_prefs, lock_addons, expected):
    config = core.merge_config(
        {"lock_preferences": lock_prefs, "lock_addons": lock_addons}
    )
    assert core.locked_dialog_names(config) == expected


# --- toolbar sync link --------------------------------------------------- #


def test_sync_link_detection():
    sync_link = '<a class=hitem id="sync" href=#>Sync<img id=sync-spinner></a>'
    other_link = '<a class=hitem id="decks" href=#>Decks</a>'
    assert core.is_sync_toolbar_link(sync_link) is True
    assert core.is_sync_toolbar_link(other_link) is False
    # the nested spinner id must not trigger a false positive on its own
    spinner_only = "<img id=sync-spinner>"
    assert core.is_sync_toolbar_link(spinner_only) is False


# --- hide AnkiMCP UI (plan) ---------------------------------------------- #

# Config with BOTH ci-buddy hide gates enabled (the shipped default).
_BOTH_GATES = {
    "hide_ankimcp_toolbar_indicator": True,
    "hide_ankimcp_settings_menu_item": True,
}


def test_ankimcp_keys_to_force_maps_gates_to_keys():
    assert core.ankimcp_keys_to_force(core.merge_config(_BOTH_GATES)) == [
        "show_toolbar_indicator",
        "show_settings_menu_item",
    ]


def test_ankimcp_keys_to_force_drops_disabled_gate():
    cfg = core.merge_config(
        {
            "hide_ankimcp_toolbar_indicator": True,
            "hide_ankimcp_settings_menu_item": False,
        }
    )
    assert core.ankimcp_keys_to_force(cfg) == ["show_toolbar_indicator"]


def test_ankimcp_keys_to_force_empty_when_all_gates_off():
    cfg = core.merge_config(
        {
            "hide_ankimcp_toolbar_indicator": False,
            "hide_ankimcp_settings_menu_item": False,
        }
    )
    assert core.ankimcp_keys_to_force(cfg) == []


def test_plan_hide_ankimcp_forces_both_keys():
    current = {
        "show_toolbar_indicator": True,
        "show_settings_menu_item": True,
        "port": 8765,
    }
    plan = core.plan_hide_ankimcp_ui(core.merge_config(_BOTH_GATES), current)
    assert plan is not None
    assert plan["show_toolbar_indicator"] is False
    assert plan["show_settings_menu_item"] is False
    # other keys preserved, and the input dict is not mutated
    assert plan["port"] == 8765
    assert current["show_toolbar_indicator"] is True
    assert current["show_settings_menu_item"] is True


def test_plan_hide_ankimcp_forces_only_the_still_needed_key():
    # one gated key already false, the other true → write only flips the true one
    current = {"show_toolbar_indicator": False, "show_settings_menu_item": True}
    plan = core.plan_hide_ankimcp_ui(core.merge_config(_BOTH_GATES), current)
    assert plan == {
        "show_toolbar_indicator": False,
        "show_settings_menu_item": False,
    }


def test_plan_hide_ankimcp_idempotent_when_both_already_false():
    # both gated keys already hidden → no-op, so we never re-dirty meta.json
    current = {"show_toolbar_indicator": False, "show_settings_menu_item": False}
    assert core.plan_hide_ankimcp_ui(core.merge_config(_BOTH_GATES), current) is None


def test_plan_hide_ankimcp_only_touches_enabled_gate():
    # only the toolbar gate enabled → the menu-item key is left untouched even
    # though it is true (it isn't a surface ci-buddy is configured to hide here)
    cfg = core.merge_config(
        {
            "hide_ankimcp_toolbar_indicator": True,
            "hide_ankimcp_settings_menu_item": False,
        }
    )
    current = {"show_toolbar_indicator": True, "show_settings_menu_item": True}
    plan = core.plan_hide_ankimcp_ui(cfg, current)
    assert plan is not None
    assert plan["show_toolbar_indicator"] is False
    assert plan["show_settings_menu_item"] is True  # untouched


def test_plan_hide_ankimcp_noop_when_no_gate_enabled():
    cfg = core.merge_config(
        {
            "hide_ankimcp_toolbar_indicator": False,
            "hide_ankimcp_settings_menu_item": False,
        }
    )
    current = {"show_toolbar_indicator": True, "show_settings_menu_item": True}
    assert core.plan_hide_ankimcp_ui(cfg, current) is None


@pytest.mark.parametrize("current", [None, {}])
def test_plan_hide_ankimcp_noop_when_absent(current):
    # AnkiMCP not installed / no config → safe no-op
    assert core.plan_hide_ankimcp_ui(core.merge_config(_BOTH_GATES), current) is None


def test_plan_hide_ankimcp_writes_when_keys_missing():
    # keys absent (e.g. an old AnkiMCP build) → force them false
    plan = core.plan_hide_ankimcp_ui(core.merge_config(_BOTH_GATES), {"port": 8765})
    assert plan is not None
    assert plan["show_toolbar_indicator"] is False
    assert plan["show_settings_menu_item"] is False


# --- resolve add-on directory by manifest package ------------------------ #


def test_resolve_dir_matches_when_dir_differs_from_package():
    # THE BUG: hosted image dir 'ankimcp' but manifest package 'anki_mcp_server'.
    installed = [
        ("other", "com.example.other"),
        ("ankimcp", "anki_mcp_server"),
    ]
    logs: list[str] = []
    assert (
        core.resolve_addon_dir_by_package(
            installed, "anki_mcp_server", logs.append
        )
        == "ankimcp"
    )
    assert logs == []  # clean match → no warning


def test_resolve_dir_exact_dir_equals_package():
    # dev/source install: directory name == manifest package.
    installed = [("anki_mcp_server", "anki_mcp_server")]
    assert (
        core.resolve_addon_dir_by_package(installed, "anki_mcp_server")
        == "anki_mcp_server"
    )


def test_resolve_dir_no_match_returns_none_and_warns():
    installed = [("foo", "foo"), ("bar", None)]  # None never matches
    logs: list[str] = []
    assert (
        core.resolve_addon_dir_by_package(
            installed, "anki_mcp_server", logs.append
        )
        is None
    )
    assert len(logs) == 1
    assert "CI_BUDDY_ADDON_PACKAGE_NOT_FOUND" in logs[0]
    assert "anki_mcp_server" in logs[0]  # names the package it looked for


def test_resolve_dir_empty_input_warns():
    logs: list[str] = []
    assert (
        core.resolve_addon_dir_by_package([], "anki_mcp_server", logs.append)
        is None
    )
    assert any("CI_BUDDY_ADDON_PACKAGE_NOT_FOUND" in l for l in logs)


def test_resolve_dir_multiple_matches_takes_first_and_logs():
    installed = [
        ("ankimcp", "anki_mcp_server"),
        ("anki_mcp_server", "anki_mcp_server"),
    ]
    logs: list[str] = []
    assert (
        core.resolve_addon_dir_by_package(
            installed, "anki_mcp_server", logs.append
        )
        == "ankimcp"  # first in iteration order
    )
    assert len(logs) == 1
    assert "multiple add-ons" in logs[0]
    assert "ankimcp" in logs[0]


# --- Anki compatibility matrix (Seam 10) ---------------------------------- #


def test_supported_versions_matrix_nonempty_and_pins_shipped_string():
    # The matrix must never ship empty (that would flag EVERY Anki, including
    # the pinned one) and must contain the exact buildinfo string of the
    # shipped wheel: PyPI anki==26.5 declares version = '26.05' (zero-padded
    # month) in anki/buildinfo.py — verified empirically from the wheel.
    assert core.SUPPORTED_ANKI_VERSIONS  # non-empty guard
    assert all(isinstance(v, str) and v for v in core.SUPPORTED_ANKI_VERSIONS)
    assert "26.05" in core.SUPPORTED_ANKI_VERSIONS


def test_anki_version_supported_exact_match_only():
    assert core.anki_version_supported("26.05") is True
    # no normalisation/prefix magic: the PyPI pin spelling and point releases
    # do NOT match — every Anki bump is a deliberate matrix edit
    assert core.anki_version_supported("26.5") is False
    assert core.anki_version_supported("26.05.1") is False
    assert core.anki_version_supported("25.09.2") is False


def test_anki_version_supported_unknown_is_unsupported():
    assert core.anki_version_supported(None) is False
    assert core.anki_version_supported("") is False


def test_unsupported_warning_lines_are_greppable_and_complete():
    lines = core.unsupported_anki_warning_lines("27.01", "0.8.0")
    assert len(lines) > 1  # loud: multi-line
    # every line carries the stable marker, so any grep hit shows context
    assert all(core.UNSUPPORTED_ANKI_MARKER in line for line in lines)
    text = "\n".join(lines)
    assert "27.01" in text  # found version
    assert "0.8.0" in text  # ci-buddy version
    for supported in core.SUPPORTED_ANKI_VERSIONS:
        assert supported in text  # the supported list
    # the operator instruction
    assert "SUPPORTED_ANKI_VERSIONS" in text
    assert "core.py" in text
    # explicitly a warning, not a gate
    assert "not a gate" in text


def test_unsupported_warning_lines_none_version_reads_unknown():
    text = "\n".join(core.unsupported_anki_warning_lines(None, "0.8.0"))
    assert "Anki unknown" in text


def test_deck_banner_html_is_big_red_bold_and_names_versions():
    banner = core.unsupported_anki_deck_banner_html("27.01", "0.8.0")
    assert "color:red" in banner
    assert "font-weight:bold" in banner
    assert "font-size" in banner  # the deck banner is the larger surface
    assert "padding" in banner
    assert "27.01" in banner
    assert "0.8.0" in banner
    assert "NOT verified" in banner


def test_toolbar_html_is_red_bold_non_link():
    badge = core.unsupported_anki_toolbar_html("27.01", "0.8.0")
    assert "color:red" in badge
    assert "font-weight:bold" in badge
    # a plain element, not a toolbar link: no anchor, no pycmd bridge call
    assert "<a " not in badge
    assert "pycmd" not in badge
    # the full sentence rides in the tooltip
    assert 'title="' in badge
    assert "27.01" in badge


def test_banner_html_escapes_version_strings():
    # a hostile/odd buildinfo string must not inject markup into the banner
    banner = core.unsupported_anki_deck_banner_html('27<script>"x', "0.8.0")
    assert "<script>" not in banner
    assert "&lt;script&gt;" in banner
    badge = core.unsupported_anki_toolbar_html('27<script>"x', "0.8.0")
    assert "<script>" not in badge
