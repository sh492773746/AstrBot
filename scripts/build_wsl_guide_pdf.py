"""Render the generated local beginner guide to a searchable Chinese PDF."""

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    """Print the local HTML with embedded fonts and no remote requests."""
    root = Path(__file__).resolve().parents[1] / "dashboard/public/local-docs"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("WSL_GUIDE_CHROMIUM"),
            args=["--no-sandbox"],
        )
        page = await browser.new_page()
        await page.goto((root / "wangshangliao-start.html").as_uri())
        await page.evaluate("document.fonts.ready")
        await page.pdf(
            path=str(root / "wangshangliao-start.pdf"),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=True,
            header_template="<span></span>",
            footer_template='<div style="width:100%;text-align:center;font-size:9px;color:#777"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
        )
        await browser.close()
    print("PDF generated:", root / "wangshangliao-start.pdf")


if __name__ == "__main__":
    asyncio.run(main())
