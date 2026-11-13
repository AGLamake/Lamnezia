"""Interface check for CI: axe-core, horizontal scroll and console errors on the main pages.

Opens every page in both themes at phone and desktop widths with the real Content Security Policy
and fails on serious or critical axe violations, on a page wider than the window and on any
console error. The server must run with DEBUG (for `/dev/ui/`) and the data of
`scripts/ui_seed.py`.

Usage: python scripts/ui_check.py --axe path/to/axe.min.js [--base http://127.0.0.1:8000]
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, Request, sync_playwright

THEMES = ("light", "dark")
WIDTHS = (360, 1280)
BLOCKING = ("serious", "critical")
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
PASSWORD = "password"  # of the demo users in scripts/ui_seed.py

# Pages by the user who opens them; None is a visitor who has not signed in.
PAGES: dict[str | None, list[str]] = {
    None: ["/accounts/login/", "/accounts/password/reset/"],
    "boss": [
        "/dev/ui/",
        "/dev/ui/?layout=portal",
        "/dashboard/",
        "/servers/",
        "/servers/1/",
        "/keys/",
        "/keys/1/",
        "/keys/drift/",
        "/users/",
        "/users/2/",
        "/operations/",
        "/operations/1/",
        "/stats/",
        "/plans/",
        "/support/inbox/",
        "/accounts/registrations/",
        "/settings/",
        "/audit/",
        "/notifications/",
        "/accounts/profile/",
    ],
    "client": [
        "/portal/",
        "/portal/usage/",
        "/portal/keys/new/",
        "/support/",
        "/guides/",
        "/accounts/profile/",
    ],
}


class UiCheck:
    """Walks the pages in one browser and prints every problem it finds."""

    def __init__(self, browser: Browser, base: str, axe_source: str) -> None:
        self.browser = browser
        self.base = base.rstrip("/")
        self.axe_source = axe_source
        self.problems = 0

    def run(self) -> None:
        for user, pages in PAGES.items():
            for theme in THEMES:
                for width in WIDTHS:
                    self.check_view(user, pages, theme, width)

    def check_view(self, user: str | None, pages: list[str], theme: str, width: int) -> None:
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        # The same switch as the theme button: theme.js reads it before the first paint.
        context.add_init_script(
            f"try {{ localStorage.setItem('theme', '{theme}') }} catch (error) {{}}"
        )
        page = context.new_page()
        errors: list[str] = []
        page.on(
            "console",
            lambda message: (
                errors.append(f"{message.text} ({message.location.get('url')}, {page.url})")
                if message.type == "error"
                else None
            ),
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        if user:
            self.sign_in(page, user)
        for url in pages:
            page.goto(self.base + url)
            page.wait_for_load_state("networkidle")
            label = f"{user or 'guest'} {theme} {width}px {url}"
            self.check_page(page, label)
            if url == "/dev/ui/":
                self.check_dialogs(page, label)
            if url == "/portal/":
                self.check_chat_widget(page, label)
        for error in errors:
            self.fail(f"{user or 'guest'} {theme} {width}px: console error: {error}")
        context.close()

    def sign_in(self, page: Page, user: str) -> None:
        page.goto(self.base + "/accounts/login/")
        page.fill("input[name=username]", user)
        page.fill("input[name=password]", PASSWORD)
        page.click("form:has(input[name=password]) button[type=submit]")
        page.wait_for_load_state("networkidle")

    def check_page(self, page: Page, label: str) -> None:
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        if overflow > 0:
            self.fail(f"{label}: horizontal scroll of {overflow}px")
        self.check_axe(page, label)

    def check_dialogs(self, page: Page, label: str) -> None:
        """The showcase dialogs in their open state."""
        page.click("form[data-confirm] button")
        page.wait_for_selector("#confirm-dialog[open]")
        self.check_axe(page, f"{label} confirm dialog")
        page.keyboard.press("Escape")
        page.click("[data-dialog-open=ui-drawer]")
        page.wait_for_selector("#ui-drawer[open]")
        self.check_axe(page, f"{label} side panel")
        page.keyboard.press("Escape")

    def check_chat_widget(self, page: Page, label: str) -> None:
        """The chat button clear of the bottom tab bar; the chat opened, closed and opened again."""
        covers = page.evaluate(
            """() => {
                const button = document.querySelector('.chat-launcher').getBoundingClientRect();
                const nav = document.querySelector('.portal-nav');
                if (!nav || !nav.offsetParent) return false;
                const bar = nav.getBoundingClientRect();
                return button.bottom > bar.top && button.top < bar.bottom;
            }"""
        )
        if covers:
            self.fail(f"{label}: the chat button covers the bottom tab bar")
        page.click(".chat-launcher")
        page.wait_for_selector("#support-chat[open] #chat-thread")
        # The live signals refetch the counters and the messages: the chat stays in its place.
        page.evaluate(
            "['unread', 'thread'].forEach(name =>"
            " document.body.dispatchEvent(new Event('support:' + name)))"
        )
        page.wait_for_load_state("networkidle")
        if not page.query_selector("#support-chat-body > #chat-thread #chat-messages"):
            self.fail(f"{label}: a live update took the chat out of the widget")
        self.check_axe(page, f"{label} chat")
        page.keyboard.press("Escape")
        page.wait_for_selector("#support-chat:not([open])", state="attached")
        # Closed, the chat sends nothing (nothing is marked seen): opened again, it shows at once.
        requests: list[Request] = []

        def listener(request: Request) -> None:
            requests.append(request)

        page.on("request", listener)
        page.evaluate("document.body.dispatchEvent(new Event('support:thread'))")
        page.wait_for_load_state("networkidle")
        if any("/support/" in request.url for request in requests):
            self.fail(f"{label}: the closed chat fetches something")
        page.click(".chat-launcher")
        if not page.query_selector("#support-chat[open] #chat-thread"):
            self.fail(f"{label}: the chat opened again is not there at once")
        page.wait_for_load_state("networkidle")
        page.remove_listener("request", listener)
        if any(request.url.endswith("/support/chat/") for request in requests):
            self.fail(f"{label}: the chat opened again loads in full")
        page.keyboard.press("Escape")

    def check_axe(self, page: Page, label: str) -> None:
        # evaluate() is not subject to the page's CSP, unlike an injected <script>.
        if not page.evaluate("typeof axe !== 'undefined'"):
            page.evaluate(self.axe_source)
        result: dict[str, Any] = page.evaluate(
            "tags => axe.run(document, {runOnly: tags})", AXE_TAGS
        )
        for violation in result["violations"]:
            if violation["impact"] in BLOCKING:
                targets = ", ".join(str(node["target"]) for node in violation["nodes"][:3])
                self.fail(f"{label}: [{violation['impact']}] {violation['id']}: {targets}")

    def fail(self, message: str) -> None:
        print(message)
        self.problems += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument("--axe", type=Path, required=True, help="axe.min.js of axe-core")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="address of the panel")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        check = UiCheck(browser, args.base, args.axe.read_text(encoding="utf-8"))
        check.run()
        browser.close()
    print(f"Problems: {check.problems}")
    return 1 if check.problems else 0


if __name__ == "__main__":
    sys.exit(main())
