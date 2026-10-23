"""Render the bilingual local guides to searchable offline PDFs."""

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
        for name in (
            "wangshangliao-start",
            "wangshangliao-start-en",
            "wangshangliao-rules-ai",
            "wangshangliao-rules-ai-en",
            "wangshangliao-component-runtime",
            "wangshangliao-component-runtime-en",
        ):
            await page.goto((root / (name + ".html")).as_uri())
            await page.evaluate("document.fonts.ready")
            await page.pdf(
                path=str(root / (name + ".pdf")),
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template='<div style="width:100%;text-align:center;font-size:9px;color:#777"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
            )
            print("PDF generated:", root / (name + ".pdf"))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
