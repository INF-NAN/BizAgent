"""Browser check of `make demo-mock`: query -> write -> preview + approval -> done -> DB diff.

Besides the full-page screenshots in <screenshot_dir>, it saves the three README screenshots
in docs/assets/ (unless --assets says otherwise): the approval card with the preview of the
rows the call will change (demo-approval.png), the DB diff after approval (demo-diff.png) and
the whole page at the approval step (demo-overview.png: conversation, timeline and approval
card together). They show the scripted mock LLM, not a model.

Requires playwright (not an app dependency) and a Chromium binary. Usage:
    make demo-mock &   # in another terminal
    uv run --with playwright python scripts/demo_ui_check.py <screenshot_dir> [chromium_executable]
"""

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
REQUEST = "Add the best wireless noise cancelling headphones under $200 to my cart"
# Wide enough for the 3-column layout (it stacks below 1100px). The middle column is then about as
# wide as the right one, so the two README screenshots sit side by side at their natural size.
VIEWPORT = {"width": 1180, "height": 900}


def diff_clip(page: Page) -> dict[str, float]:
    """The right panel, from its tab bar down to the end of the diff table."""
    panel = page.locator("section.panel", has=page.locator("#tab-diff")).bounding_box()
    table = page.locator("#diff").bounding_box()
    assert panel and table
    height = table["y"] + table["height"] + 12 - panel["y"]
    return {"x": panel["x"], "y": panel["y"], "width": panel["width"], "height": height}


def main() -> None:
    ap = argparse.ArgumentParser(description="Browser check of `make demo-mock`.")
    ap.add_argument("shots", type=Path, help="directory for the full-page screenshots")
    ap.add_argument("chromium", nargs="?", help="Chromium executable (default: Playwright's own)")
    ap.add_argument("--assets", type=Path, default=ROOT / "docs" / "assets", help="README screenshots")
    args = ap.parse_args()
    args.assets.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=args.chromium)
        page = b.new_page(viewport=VIEWPORT, device_scale_factor=2, color_scheme="light")
        page.goto("http://127.0.0.1:8080/")
        page.wait_for_selector("#scenario option", state="attached")
        page.click("#create-session")
        page.wait_for_function("document.querySelector('#session-info').textContent.startsWith('session')")
        page.fill("#message", REQUEST)
        page.click("#send")
        # the preview runs a shadow environment first (a few seconds, ADR-029)
        page.wait_for_selector("#approval:not(.hidden)", timeout=60000)
        page.wait_for_selector("#approval-body .preview-box", timeout=5000)
        print("approval card:", page.inner_text("#approval-body").replace("\n", " ")[:300])
        page.screenshot(path=args.shots / "demo_1_approval.png", full_page=True)
        page.locator("#approval").screenshot(path=args.assets / "demo-approval.png")
        page.screenshot(path=args.assets / "demo-overview.png", full_page=True, scale="css")
        page.fill("#approver", "alice")
        page.click("#approve")
        page.wait_for_function("[...document.querySelectorAll('.msg.agent')].length > 0", timeout=20000)
        print("final answer:", page.inner_text(".msg.agent"))
        page.click("button[data-tab=diff]")
        page.wait_for_selector("#diff table", timeout=10000)
        print("diff:", page.inner_text("#diff").replace("\n", " | ")[:200])
        page.screenshot(path=args.shots / "demo_2_done_diff.png", full_page=True)
        page.screenshot(path=args.assets / "demo-diff.png", clip=diff_clip(page))
        print("timeline items:", page.locator("#timeline li").count())
        checks = page.locator("#timeline li.tool_call .badge.match").all()
        print("preview checks:", [c.inner_text() for c in checks])
        saved = ("demo-approval.png", "demo-diff.png", "demo-overview.png")
        print("saved:", *(args.assets / name for name in saved))
        b.close()


if __name__ == "__main__":
    main()
