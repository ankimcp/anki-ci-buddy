"""Test the entry-point double-registration guard (LOW-8)."""

import sys
import types

import ci_buddy
from ci_buddy import compat, locks, provisioners


def test_load_registers_once(monkeypatch):
    calls = {"locks": 0, "prov": 0, "compat": 0}
    monkeypatch.setattr(
        locks, "register", lambda config: calls.__setitem__("locks", calls["locks"] + 1)
    )
    monkeypatch.setattr(
        provisioners,
        "register",
        lambda config: calls.__setitem__("prov", calls["prov"] + 1),
    )
    monkeypatch.setattr(
        compat,
        "register",
        lambda config: calls.__setitem__("compat", calls["compat"] + 1),
    )

    mgr = types.SimpleNamespace(getConfig=lambda name: {})
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = types.SimpleNamespace(addonManager=mgr)
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)

    monkeypatch.setattr(ci_buddy, "_registered", False)

    ci_buddy._load()
    ci_buddy._load()  # second call must be a no-op

    assert calls == {"locks": 1, "prov": 1, "compat": 1}
