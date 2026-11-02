"""Static wiring checks for the panel front end; no browser is used.

These tests only read ``app.HTML`` and ``panel.js`` as text.  They guard the
front-end wiring of the login gate and the one-click confirmation entry: the
gate must exist and must never capture school credentials, the one-click
button must exist, its enable rule must keep the manual per-person
acknowledgement as the human gate, and the handler must chain the same
supervised APIs as the step-by-step flow without ever ticking an
acknowledgement checkbox on the user's behalf.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]

SPEC = importlib.util.spec_from_file_location("app_panel_wiring", ROOT / "app.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)

PANEL = (ROOT / "panel.js").read_text(encoding="utf-8")


def handler_source():
    """Return the one-click handler block, up to the next wired handler."""
    start = PANEL.index("$('oneClickSubmit').onclick")
    end = PANEL.index("$('refresh').onclick", start)
    return PANEL[start:end]


def gate_source():
    """Return the login-gate block, from status mapping to initialize()."""
    start = PANEL.index("const gateWaiting")
    end = PANEL.index("initialize();", start)
    return PANEL[start:end]


class PanelWiringTests(unittest.TestCase):
    def test_html_exposes_one_click_entry_with_warning(self):
        self.assertIn('id="oneClickSubmit"', app.HTML)
        self.assertIn("一键代确认并立即提交", app.HTML)
        self.assertIn("点击后将立即提交，无二次确认", app.HTML)
        self.assertIn("每个日期最多尝试一次", app.HTML)

    def test_enable_rule_requires_manual_acknowledgement(self):
        rule = "$('oneClickSubmit').disabled = busy || previewPreparing || !schoolReady || !schoolRecords.length || !$('schoolAck').checked;"
        self.assertIn(rule, PANEL)

    def test_handler_chains_supervised_apis_without_auto_ticking(self):
        source = handler_source()
        for path in ("/api/verify-school", "/api/prepare-batch", "/api/batch-submit"):
            self.assertIn(path, source)
        self.assertIn("supervised_approved: true", source)
        self.assertIn("$('batchMethod').value", source)
        self.assertIn("$('batchNote').value.trim()", source)
        # The human gate is only read, never set by the tool.
        self.assertNotIn("schoolAck').checked = true", PANEL)
        self.assertNotIn("batchAck').checked = true", source)

    def test_handler_keeps_failure_state_for_manual_follow_up(self):
        source = handler_source()
        catch_block = source[source.index("} catch"):]
        self.assertIn("一键代确认已中止", catch_block)
        # A failed chain must not wipe the acknowledgement, the per-row drafts,
        # or any in-flight preview the user may still finish by hand.
        self.assertNotIn("clearBatch", catch_block)
        self.assertNotIn("clearTrial", catch_block)
        self.assertNotIn("schoolAck').checked = false", catch_block)

    def test_login_gate_exists_and_promises_no_credential_access(self):
        for marker in ('id="loginGate"', 'id="gateLogin"', 'id="gateStatus"', 'id="gateHint"', 'id="gateSkip"'):
            self.assertIn(marker, app.HTML)
        self.assertIn("不读取、不保存你的账号、密码、验证码或 Cookie", app.HTML)

    def test_login_automatically_reads_today_roster(self):
        enter = PANEL[PANEL.index("function enterTool"):PANEL.index("$('gateLogin').onclick")]
        self.assertIn("autoOpenRoster(page)", enter)
        helper = PANEL[PANEL.index("async function autoOpenRoster"):PANEL.index("function enterTool")]
        self.assertIn("/api/open-roster", helper)
        # Already-loaded rosters are not re-read, and a running batch is never
        # disturbed by the automatic read.
        self.assertIn("page.roster_ready", helper)
        self.assertIn("submitting()", helper)

    def test_all_roster_view_wiring(self):
        for marker in ('id="readAllRoster"', 'id="allRosterTable"', 'id="allRosterRows"',
                       'id="allRosterResult"', 'id="allRosterSummary"'):
            self.assertIn(marker, app.HTML)
        self.assertIn("/api/all-roster", PANEL)
        self.assertIn("function readAllRoster", PANEL)
        self.assertIn("名单实时核验", PANEL)

    def test_login_gate_never_captures_credentials(self):
        # The school login always happens on the official page inside Edge;
        # the gate itself must not grow a credential form.
        self.assertNotIn("password", app.HTML)
        self.assertNotIn("password", PANEL)
        self.assertNotIn("cookie", PANEL.lower())

    def test_gate_enters_tool_only_on_connected_status(self):
        source = gate_source()
        check = source[source.index("async function gateCheck"):source.index("function enterTool")]
        self.assertIn("/api/school-page", check)
        self.assertIn("page.status === 'connected'", check)
        self.assertIn("enterTool(page)", check)
        # A batch already running must let a reloaded page through to progress.
        self.assertIn("serverRunning", check)
        opener = source[source.index("$('gateLogin').onclick"):source.index("$('gateSkip').onclick")]
        self.assertIn("/api/open-school", opener)


if __name__ == "__main__":
    unittest.main()
