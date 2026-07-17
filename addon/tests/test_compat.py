"""Unit tests for Seam 10 — the unsupported-Anki tripwire (warning, not gate).

``ci_buddy.compat`` imports ``anki``/``aqt`` lazily, so the module imports
cleanly here; the version source and the gui_hooks surface are faked via
sys.modules, mirroring test_locks.py.
"""

import sys
import types

from ci_buddy import compat, core


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeHook:
    def __init__(self):
        self.handlers = []

    def append(self, fn):
        self.handlers.append(fn)


def install_fake_buildinfo(monkeypatch, version):
    """Install a fake ``anki.buildinfo`` so the lazy ``from anki.buildinfo
    import version`` inside ``read_anki_version`` resolves without Anki."""
    anki_mod = types.ModuleType("anki")
    buildinfo_mod = types.ModuleType("anki.buildinfo")
    buildinfo_mod.version = version
    anki_mod.buildinfo = buildinfo_mod
    monkeypatch.setitem(sys.modules, "anki", anki_mod)
    monkeypatch.setitem(sys.modules, "anki.buildinfo", buildinfo_mod)


def install_missing_buildinfo(monkeypatch):
    """Make ``import anki.buildinfo`` fail (no Anki at all)."""
    # A None entry in sys.modules makes the import machinery raise ImportError.
    monkeypatch.setitem(sys.modules, "anki", None)
    monkeypatch.setitem(sys.modules, "anki.buildinfo", None)


def install_fake_gui_hooks(monkeypatch, *drop_hooks):
    """Install a fake ``aqt`` exposing the Seam 10 gui_hooks (minus any named
    in ``drop_hooks``, to model an Anki that removed one)."""
    hooks = types.SimpleNamespace(
        deck_browser_will_render_content=FakeHook(),
        top_toolbar_did_init_links=FakeHook(),
    )
    for name in drop_hooks:
        delattr(hooks, name)
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.gui_hooks = hooks
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    return hooks


SUPPORTED = core.SUPPORTED_ANKI_VERSIONS[0]  # "26.05" — the shipped pin


# --------------------------------------------------------------------------- #
# version sources
# --------------------------------------------------------------------------- #


def test_read_anki_version_returns_buildinfo_version(monkeypatch):
    install_fake_buildinfo(monkeypatch, "26.05")
    assert compat.read_anki_version() == "26.05"


def test_read_anki_version_none_when_anki_missing(monkeypatch):
    install_missing_buildinfo(monkeypatch)
    assert compat.read_anki_version() is None  # check failure ⇒ unsupported


def test_read_anki_version_none_when_not_a_string(monkeypatch):
    # a reshaped buildinfo (version no longer a str) is treated as unknown
    install_fake_buildinfo(monkeypatch, 2605)
    assert compat.read_anki_version() is None


def test_read_ci_buddy_version_reads_shipped_manifest():
    # single source of truth: the banner version comes from manifest.json —
    # this also pins the v0.8.0 release bump.
    assert compat.read_ci_buddy_version() == "0.8.0"


# --------------------------------------------------------------------------- #
# stderr warning
# --------------------------------------------------------------------------- #


def test_emit_unsupported_warning_writes_marker_lines_to_stderr(capsys):
    compat.emit_unsupported_warning("27.01", "0.8.0")

    captured = capsys.readouterr()
    assert captured.out == ""  # stderr surface, not stdout
    err_lines = [l for l in captured.err.splitlines() if l]
    assert len(err_lines) > 1  # loud: multi-line
    assert all(core.UNSUPPORTED_ANKI_MARKER in l for l in err_lines)
    assert "27.01" in captured.err


# --------------------------------------------------------------------------- #
# GUI surface handlers
# --------------------------------------------------------------------------- #


def test_deck_browser_handler_prepends_banner_to_tree():
    content = types.SimpleNamespace(tree="<tr>deck row</tr>", stats="<div>s</div>")

    compat.on_deck_browser_will_render_content(object(), content, "<div>WARN</div>")

    assert content.tree.startswith("<div>WARN</div>\n")  # prepended, at top
    assert content.tree.endswith("<tr>deck row</tr>")  # original preserved
    assert content.stats == "<div>s</div>"  # stats untouched


def test_deck_browser_handler_missing_tree_fails_open(capsys):
    # a future DeckBrowserContent without a str tree: logged skip, no crash
    content = types.SimpleNamespace(stats="<div>s</div>")

    compat.on_deck_browser_will_render_content(object(), content, "<div>WARN</div>")

    out = capsys.readouterr().out
    assert "banner skipped" in out
    assert content.stats == "<div>s</div>"


def test_deck_browser_handler_reshaped_tree_fails_open(capsys):
    content = types.SimpleNamespace(tree=42, stats="<div>s</div>")

    compat.on_deck_browser_will_render_content(object(), content, "<div>WARN</div>")

    assert "banner skipped" in capsys.readouterr().out
    assert content.tree == 42  # left untouched


def test_toolbar_handler_appends_badge_and_keeps_links():
    links = ['<a id="decks">Decks</a>', '<a id="sync">Sync</a>']

    compat.on_top_toolbar_did_init_links(links, object(), "<span>BADGE</span>")

    # mutated in place, badge appended last, existing links intact
    assert links[:2] == ['<a id="decks">Decks</a>', '<a id="sync">Sync</a>']
    assert links[-1] == "<span>BADGE</span>"


# --------------------------------------------------------------------------- #
# register — gating, zero-cost supported path, fail-open wiring
# --------------------------------------------------------------------------- #


def test_register_unsupported_emits_stderr_and_wires_both_hooks(
    monkeypatch, capsys
):
    install_fake_buildinfo(monkeypatch, "27.01")
    hooks = install_fake_gui_hooks(monkeypatch)

    compat.register(core.merge_config({}))

    captured = capsys.readouterr()
    assert core.UNSUPPORTED_ANKI_MARKER in captured.err
    assert "27.01" in captured.err
    assert len(hooks.deck_browser_will_render_content.handlers) == 1
    assert len(hooks.top_toolbar_did_init_links.handlers) == 1

    # the registered handlers carry the version-specific warning HTML
    content = types.SimpleNamespace(tree="<tr>x</tr>", stats="")
    hooks.deck_browser_will_render_content.handlers[0](object(), content)
    assert "color:red" in content.tree
    assert "font-weight:bold" in content.tree
    assert "27.01" in content.tree
    assert content.tree.endswith("<tr>x</tr>")

    links = []
    hooks.top_toolbar_did_init_links.handlers[0](links, object())
    assert len(links) == 1
    assert "color:red" in links[0]
    assert "27.01" in links[0]


def test_register_supported_version_zero_output_zero_hooks(monkeypatch, capsys):
    install_fake_buildinfo(monkeypatch, SUPPORTED)
    hooks = install_fake_gui_hooks(monkeypatch)

    compat.register(core.merge_config({}))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    # no do-nothing hooks on the verified version
    assert hooks.deck_browser_will_render_content.handlers == []
    assert hooks.top_toolbar_did_init_links.handlers == []


def test_register_gate_off_is_silent_even_when_unsupported(monkeypatch, capsys):
    install_fake_buildinfo(monkeypatch, "27.01")
    hooks = install_fake_gui_hooks(monkeypatch)

    compat.register(core.merge_config({"warn_unsupported_anki": False}))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert hooks.deck_browser_will_render_content.handlers == []
    assert hooks.top_toolbar_did_init_links.handlers == []


def test_register_missing_anki_treated_as_unsupported(monkeypatch, capsys):
    # buildinfo unreadable → version unknown → the warning must trip
    install_missing_buildinfo(monkeypatch)
    hooks = install_fake_gui_hooks(monkeypatch)

    compat.register(core.merge_config({}))

    captured = capsys.readouterr()
    assert core.UNSUPPORTED_ANKI_MARKER in captured.err
    assert "Anki unknown" in captured.err
    assert len(hooks.deck_browser_will_render_content.handlers) == 1


def test_register_missing_hook_skips_that_surface_only(monkeypatch, capsys):
    # a future aqt without the deck-browser hook: logged skip, the toolbar
    # surface still wired — never a crash
    install_fake_buildinfo(monkeypatch, "27.01")
    hooks = install_fake_gui_hooks(
        monkeypatch, "deck_browser_will_render_content"
    )

    compat.register(core.merge_config({}))

    out = capsys.readouterr().out
    assert "deck_browser_will_render_content" in out
    assert "warning surface skipped" in out
    assert len(hooks.top_toolbar_did_init_links.handlers) == 1


def test_register_reshaped_hook_skips_that_surface_only(monkeypatch, capsys):
    # a hook attr that exists but is no longer appendable is a logged skip too
    install_fake_buildinfo(monkeypatch, "27.01")
    hooks = install_fake_gui_hooks(monkeypatch)
    hooks.top_toolbar_did_init_links = object()

    compat.register(core.merge_config({}))

    out = capsys.readouterr().out
    assert "top_toolbar_did_init_links" in out
    assert "warning surface skipped" in out
    assert len(hooks.deck_browser_will_render_content.handlers) == 1


def test_registered_handlers_are_safe_wrapped(monkeypatch, capsys):
    # an exception inside a registered handler must be logged, never raised
    # into Anki's render path
    install_fake_buildinfo(monkeypatch, "27.01")
    hooks = install_fake_gui_hooks(monkeypatch)
    compat.register(core.merge_config({}))
    capsys.readouterr()  # drop the registration-time stderr warning

    class ExplodingContent:
        @property
        def tree(self):
            raise RuntimeError("boom")

    hooks.deck_browser_will_render_content.handlers[0](
        object(), ExplodingContent()
    )  # must not raise
    assert "failed" in capsys.readouterr().out
