"""Portal-render marker contract for phone-tier portal waits.

Pins the dual-generation detection in lib/clients/wifi.py: legacy portals
expose data-sm state attributes; the v0.6 SPA ships none and is detected
via submit copy + tab-state attributes instead.
"""
from lib.clients.wifi import (
    PORTAL_AUTHED_RE,
    PORTAL_INPUT_READY_RE,
    PORTAL_LOADED_RE,
)

LEGACY_PORTAL_XML = (
    '<node class="android.webkit.WebView" text="Tollgate Captive Portal">'
    '<node class="android.view.View" content-desc=\'data-sm="portal_ready"\'/>'
    "</node>"
)
LEGACY_AUTHED_XML = '<node content-desc=\'data-sm="authed"\'/>'
LEGACY_COUNTDOWN_XML = '<node content-desc=\'data-sm="countdown"\'/>'

V06_PORTAL_XML = (
    '<node class="android.webkit.WebView">'
    '<node class="android.view.View" content-desc=\'data-active="false" '
    "tollgate-captive-portal-tabs-tab-lightning\"/>"
    '<node class="android.view.View" content-desc=\'data-active="true" '
    "tollgate-captive-portal-tabs-tab-cashu\"/>"
    '<node class="android.widget.Button" text="Purchase Internet Access"/>'
    '<node class="android.widget.EditText"/>'
    "</node>"
)
V06_PAYLINE_XML = '<node class="android.widget.TextView" text="Pay 4 to get 150 MiB"/>'
V06_AUTHED_XML = (
    '<node class="android.webkit.WebView">'
    '<node class="android.widget.TextView" text="Data remaining: 120 MiB"/>'
    '<node class="android.widget.TextView" text="Thank you for your purchase"/>'
    "</node>"
)
V06_TAB_HIDDEN_XML = '<node content-desc=\'data-hidden="true" cashu-method\'/>'
V06_TAB_DISABLED_XML = '<node content-desc=\'data-disabled="true"\'/>'

ANDROID_SETTINGS_XML = (
    '<node class="android.widget.ListView">'
    '<node class="android.widget.TextView" text="Connected devices"/>'
    '<node class="android.widget.TextView" text="TollGate-326D, Connected"/>'
    "</node>"
)


class TestPortalLoaded:
    def test_legacy_data_sm_states_match(self):
        for state in ("portal_ready", "token_typing", "authed", "countdown"):
            assert PORTAL_LOADED_RE.search(f'content-desc=\'data-sm="{state}"\'')

    def test_legacy_title_matches(self):
        assert PORTAL_LOADED_RE.search(LEGACY_PORTAL_XML)

    def test_v06_submit_copy_matches(self):
        assert PORTAL_LOADED_RE.search(V06_PORTAL_XML)

    def test_v06_tab_state_attributes_match(self):
        for xml in (V06_TAB_HIDDEN_XML, V06_TAB_DISABLED_XML):
            assert PORTAL_LOADED_RE.search(xml)

    def test_android_settings_chrome_does_not_match(self):
        assert not PORTAL_LOADED_RE.search(ANDROID_SETTINGS_XML)

    def test_unrelated_data_attributes_do_not_match(self):
        assert not PORTAL_LOADED_RE.search('content-desc=\'data-testid="submit"\'')


class TestPortalInputReady:
    def test_legacy_portal_ready_matches(self):
        assert PORTAL_INPUT_READY_RE.search(LEGACY_PORTAL_XML)

    def test_v06_purchase_copy_matches(self):
        assert PORTAL_INPUT_READY_RE.search(V06_PORTAL_XML)

    def test_v06_payline_copy_matches(self):
        assert PORTAL_INPUT_READY_RE.search(V06_PAYLINE_XML)

    def test_authed_state_is_not_input_ready(self):
        assert not PORTAL_INPUT_READY_RE.search(LEGACY_AUTHED_XML)

    def test_v06_authed_dashboard_is_not_input_ready(self):
        assert not PORTAL_INPUT_READY_RE.search(V06_AUTHED_XML)


class TestPortalAuthed:
    def test_legacy_authed_states_match(self):
        assert PORTAL_AUTHED_RE.search(LEGACY_AUTHED_XML)
        assert PORTAL_AUTHED_RE.search(LEGACY_COUNTDOWN_XML)

    def test_v06_remaining_copy_matches(self):
        assert PORTAL_AUTHED_RE.search(V06_AUTHED_XML)

    def test_v06_thank_you_copy_matches(self):
        assert PORTAL_AUTHED_RE.search("text=\"Thank you for your purchase\"")

    def test_v06_session_active_matches(self):
        assert PORTAL_AUTHED_RE.search('text="Session active"')

    def test_preauth_portal_is_not_authed(self):
        assert not PORTAL_AUTHED_RE.search(V06_PORTAL_XML)
        assert not PORTAL_AUTHED_RE.search(V06_PAYLINE_XML)

    def test_legacy_portal_ready_is_not_authed(self):
        assert not PORTAL_AUTHED_RE.search(LEGACY_PORTAL_XML)

    def test_bare_connected_is_not_authed(self):
        assert not PORTAL_AUTHED_RE.search(ANDROID_SETTINGS_XML)

    def test_empty_dump_is_not_authed(self):
        assert not PORTAL_AUTHED_RE.search("")
