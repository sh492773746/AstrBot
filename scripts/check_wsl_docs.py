"""Check generated guides on desktop/mobile without contacting live bot APIs."""

import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1] / "dashboard/public/local-docs"


async def check(browser_path, screenshots):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=browser_path, args=["--no-sandbox"]
        )
        try:
            pages = sorted(ROOT.glob("wangshangliao*.html"))
            if not pages:
                raise RuntimeError("Generate the local guides first")
            for width, height in ((390, 844), (1440, 900)):
                page = await browser.new_page(
                    viewport={"width": width, "height": height}
                )
                failures = []
                page.on("pageerror", lambda error: failures.append(str(error)))
                for file in pages:
                    await page.goto(file.as_uri())
                    await page.evaluate("document.fonts.ready")
                    metrics = await page.evaluate(
                        """() => ({
                          overflow: document.documentElement.scrollWidth > innerWidth + 1,
                          missingImages: [...document.images].filter(img => !img.complete || !img.naturalWidth).map(img => img.src),
                          title: document.querySelector('h1')?.textContent
                        })"""
                    )
                    if (
                        metrics["overflow"]
                        or metrics["missingImages"]
                        or not metrics["title"]
                    ):
                        raise RuntimeError(f"{file.name} at {width}px: {metrics}")
                    if failures:
                        raise RuntimeError(f"{file.name}: {failures}")
                    if screenshots and file.name in (
                        "wangshangliao-index.html",
                        "wangshangliao-start.html",
                        "wangshangliao.html",
                    ):
                        await page.screenshot(
                            path=str(screenshots / f"{file.stem}-{width}.png"),
                            full_page=True,
                        )
                await page.close()
            print(
                f"Checked {len(pages)} guides at 390px and 1440px; no page errors, missing images or overflow."
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=os.environ.get("WSL_GUIDE_CHROMIUM"))
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    asyncio.run(check(args.browser, args.screenshots))
