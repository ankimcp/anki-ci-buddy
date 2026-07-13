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
    assert merged["credentials_path"] == "/run/ankimcp/sync-credentials.json"


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
    # defaults keep these enabled
    assert plan["actionNoteTypes"] is False
    assert plan["actionFullDatabaseCheck"] is False


def test_note_types_never_in_lock_map_by_accident():
    # actionNoteTypes is present but only lockable via explicit lock_note_types.
    plan = core.action_lock_plan(core.merge_config({"lock_note_types": False}))
    assert plan["actionNoteTypes"] is False


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


# --- credentials parsing ------------------------------------------------- #


def test_parse_valid_full_record():
    creds = core.parse_credentials(
        '{"v":1,"serial":7,"hkey":"abc","endpoint":"https://sync.ankiweb.net/",'
        '"username":"u@example.com"}'
    )
    assert creds.serial == 7
    assert creds.hkey == "abc"
    assert creds.endpoint == "https://sync.ankiweb.net/"
    assert creds.username == "u@example.com"


def test_parse_minimal_record():
    creds = core.parse_credentials('{"serial":1,"hkey":"k"}')
    assert creds.endpoint is None
    assert creds.username is None


@pytest.mark.parametrize(
    "raw",
    [
        "",  # empty
        "not json",  # garbage
        "{",  # partial
        "[]",  # not an object
        '{"serial":1}',  # missing hkey
        '{"hkey":"k"}',  # missing serial
        '{"serial":"1","hkey":"k"}',  # serial not int
        '{"serial":true,"hkey":"k"}',  # bool serial rejected
        '{"serial":1,"hkey":""}',  # empty hkey
        '{"serial":1,"hkey":123}',  # hkey not str
        '{"v":2,"serial":1,"hkey":"k"}',  # unknown version
        '{"serial":1,"hkey":"k","endpoint":123}',  # endpoint wrong type
    ],
)
def test_parse_rejects_bad_records(raw):
    with pytest.raises(core.CredentialsError):
        core.parse_credentials(raw)


# --- serial comparison --------------------------------------------------- #


def test_should_inject_first_time():
    assert core.should_inject(1, None) is True


def test_should_inject_only_on_increase():
    assert core.should_inject(8, 7) is True
    assert core.should_inject(7, 7) is False  # same
    assert core.should_inject(6, 7) is False  # rollback


# --- endpoint allowlist -------------------------------------------------- #


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://ankiweb.net/", True),
        ("https://sync.ankiweb.net/", True),  # subdomain
        ("https://sync.ankiuser.net/", True),
        ("http://ankiweb.net/", True),  # http scheme allowed
        ("https://evil.com/", False),
        ("https://notankiweb.net/", False),  # not a real subdomain
        ("https://ankiweb.net.evil.com/", False),  # suffix trick
        ("ftp://ankiweb.net/", False),  # bad scheme
        ("://broken", False),
        ("", False),
        ("https:///nopath", False),  # no host
    ],
)
def test_endpoint_allowlist(endpoint, expected):
    allowlist = ["ankiweb.net", "ankiuser.net"]
    assert core.endpoint_allowed(endpoint, allowlist) is expected


def test_endpoint_empty_allowlist_rejects_all():
    assert core.endpoint_allowed("https://ankiweb.net/", []) is False


# --- fingerprint --------------------------------------------------------- #


def test_fingerprint_is_short_and_stable_and_hides_hkey():
    hkey = "super-secret-hkey-value"
    fp = core.hkey_fingerprint(hkey)
    assert len(fp) == 8
    assert fp == core.hkey_fingerprint(hkey)  # stable
    assert hkey not in fp  # the secret is not in the fingerprint


# --- toolbar sync link --------------------------------------------------- #


def test_sync_link_detection():
    sync_link = '<a class=hitem id="sync" href=#>Sync<img id=sync-spinner></a>'
    other_link = '<a class=hitem id="decks" href=#>Decks</a>'
    assert core.is_sync_toolbar_link(sync_link) is True
    assert core.is_sync_toolbar_link(other_link) is False
    # the nested spinner id must not trigger a false positive on its own
    spinner_only = "<img id=sync-spinner>"
    assert core.is_sync_toolbar_link(spinner_only) is False
