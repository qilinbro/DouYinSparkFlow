"""Exercise real input events on a synthetic local page, without cookies or network."""
import os
import unittest
from types import SimpleNamespace
from urllib.parse import quote

from core.douyin_im import DouyinIM, JS_LIST_READY


@unittest.skipUnless(os.getenv("RUN_BROWSER_INPUT_TESTS") == "1" or os.getenv("TEST_BROWSER_CHANNEL"),
                     "Browser integration is enabled explicitly by Actions")
class BrowserInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv("TEST_BROWSER_CHANNEL"):
            from playwright.sync_api import sync_playwright
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(channel=os.environ["TEST_BROWSER_CHANNEL"], headless=True)
        else:
            from core.browser import get_browser
            cls.playwright = None
            cls.browser = get_browser()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        if cls.playwright:
            cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        html = '''<meta charset="utf-8"><title>Input regression</title>
          <div data-e2e="msg-input" class="DraftEditor-root">
          <div class="public-DraftEditor-content" contenteditable="true"></div></div>
          <button class="messageMsgInputpublishBtn" onclick="window.sendClicks=(window.sendClicks||0)+1">Send</button>
          <script>document.querySelector('[contenteditable]').addEventListener('input',()=>{
            document.querySelector('button').classList.toggle('messageMsgInputpublishRedBtn',
              !!document.querySelector('[contenteditable]').textContent.trim());});</script>'''
        self.page.goto("data:text/html;charset=utf-8," + quote(html),
                       wait_until="domcontentloaded", timeout=15000)
        self.im = object.__new__(DouyinIM)
        self.im.page = self.page
        self.im.mon = SimpleNamespace(sends=[])
        self.hit = {"is_group": False, "conv_id": "0:1:100:200", "display": "Synthetic peer"}

    def tearDown(self):
        self.context.close()

    def test_real_mouse_movement_selects_real_input(self):
        self.assertEqual(self.im._input_mode(), "real")

    def test_optional_avatar_and_blank_placeholder_do_not_block_ready_list(self):
        self.page.evaluate('''() => {
          const box = document.createElement('div');
          box.className = 'conversationConversationListwrapper'; box.style.height = '100px';
          box.innerHTML = '<div data-e2e="conversation-item"><img><span class="conversationConversationItemtitle">Synthetic peer</span></div><div data-e2e="conversation-item"></div>';
          document.body.append(box);
          Object.defineProperty(box.querySelector('img'), 'complete', {get: () => false});
        }''')
        state = self.page.evaluate(JS_LIST_READY)
        self.assertTrue(state["ready"])
        self.assertEqual(state["usable"], 1)

    def test_complete_multiline_input_and_one_send_click_without_fake_success(self):
        result = self.im.type_and_send(self.hit, "第一行\nSecond line", timeout=0.3)
        self.assertEqual(self.page.evaluate("window.sendClicks"), 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "receipt-unconfirmed")

    def test_disabled_send_button_is_never_clicked(self):
        self.page.evaluate("document.querySelector('button').disabled=true")
        result = self.im.type_and_send(self.hit, "test", timeout=0.3)
        self.assertEqual(self.page.evaluate("window.sendClicks || 0"), 0)
        self.assertEqual(result["reason"], "send-not-triggered")


if __name__ == "__main__":
    unittest.main()
