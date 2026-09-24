"""Browser check of `make demo-mock`: query -> write -> approval -> done -> DB diff.

Requires `pip install playwright` (not an app dependency) and a Chromium binary. Usage:
    make demo-mock &   # in another terminal
    python scripts/demo_ui_check.py <screenshot_dir> [chromium_executable]
"""
import sys
from playwright.sync_api import sync_playwright

out = sys.argv[1]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=sys.argv[2] if len(sys.argv) > 2 else None)
    page = b.new_page(viewport={"width": 1500, "height": 950})
    page.goto("http://127.0.0.1:8080/")
    page.wait_for_selector("#scenario option", state="attached")
    page.click("#create-session")
    page.wait_for_function("document.querySelector('#session-info').textContent.startsWith('session')")
    page.fill("#message", "Add the best wireless noise cancelling headphones under $200 to my cart")
    page.click("#send")
    page.wait_for_selector("#approval:not(.hidden)", timeout=20000)
    print("approval card:", page.inner_text("#approval-body").replace("\n", " ")[:160])
    page.screenshot(path=f"{out}/demo_1_approval.png", full_page=True)
    page.fill("#approver", "alice")
    page.click("#approve")
    page.wait_for_function("[...document.querySelectorAll('.msg.agent')].length > 0", timeout=20000)
    print("final answer:", page.inner_text(".msg.agent"))
    page.click("button[data-tab=diff]")
    page.wait_for_selector("#diff table", timeout=10000)
    print("diff:", page.inner_text("#diff").replace("\n", " | ")[:200])
    page.screenshot(path=f"{out}/demo_2_done_diff.png", full_page=True)
    print("timeline items:", page.locator("#timeline li").count())
    b.close()
