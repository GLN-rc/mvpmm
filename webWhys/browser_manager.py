"""
Playwright Browser Manager - Singleton Instance & Context Pooling
Manages a shared browser instance with lazy initialization, context pooling,
and graceful error handling for JavaScript-heavy site rendering.
"""

import asyncio
import logging
from typing import Optional, Tuple
from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)

class PlaywrightBrowserManager:
    """Singleton pattern for shared Playwright browser instance with context pooling."""

    _browser: Optional[Browser] = None
    _playwright = None
    _lock = asyncio.Lock()
    _context_pool: list[BrowserContext] = []
    _max_pool_size = 5
    _browser_retry_count = 0
    _browser_max_retries = 2

    @staticmethod
    async def get_browser() -> Browser:
        """
        Get or initialize the shared browser instance (lazy initialization).
        Returns the singleton browser instance.
        """
        if PlaywrightBrowserManager._browser is None:
            async with PlaywrightBrowserManager._lock:
                # Double-check pattern to avoid race conditions
                if PlaywrightBrowserManager._browser is None:
                    try:
                        PlaywrightBrowserManager._playwright = await async_playwright().start()
                        PlaywrightBrowserManager._browser = await PlaywrightBrowserManager._playwright.chromium.launch(
                            args=["--disable-blink-features=AutomationControlled"],  # Stealth mode
                            headless=True
                        )
                        logger.info("Playwright browser initialized")
                    except Exception as e:
                        logger.error(f"Failed to initialize Playwright browser: {str(e)}")
                        raise

        return PlaywrightBrowserManager._browser

    @staticmethod
    async def _get_or_create_context() -> BrowserContext:
        """
        Get an existing context from the pool or create a new one.
        Each context is isolated (no shared cookies/state).
        """
        # Reuse existing context if available
        if PlaywrightBrowserManager._context_pool:
            context = PlaywrightBrowserManager._context_pool.pop()
            return context

        # Create new context
        browser = await PlaywrightBrowserManager.get_browser()
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York",
            geolocation={"latitude": 40.7128, "longitude": -74.0060},  # NYC as default
            permissions=[]  # No geolocation permission
        )
        return context

    @staticmethod
    async def _return_context_to_pool(context: BrowserContext):
        """Return a context to the pool for reuse."""
        if len(PlaywrightBrowserManager._context_pool) < PlaywrightBrowserManager._max_pool_size:
            try:
                # Clear cookies and storage for clean reuse
                await context.clear_cookies()
                await context.add_init_script("window.localStorage.clear(); window.sessionStorage.clear();")
                PlaywrightBrowserManager._context_pool.append(context)
            except Exception as e:
                # If cleanup fails, just close the context
                try:
                    await context.close()
                except:
                    pass
        else:
            # Pool is full, close the context
            try:
                await context.close()
            except:
                pass

    @staticmethod
    async def render_page(url: str, timeout: int = 10) -> Tuple[str, dict]:
        """
        Render a JavaScript-heavy page and return the rendered HTML + metadata.
        Uses smart fallback: tries "networkidle" first, falls back to "load" on timeout.

        Args:
            url: URL to render
            timeout: Timeout in seconds for initial page load (default: 10s)

        Returns:
            Tuple of (html_string, metadata_dict)
            Returns ("", {}) on timeout or error (graceful fallback)
        """
        context = None
        try:
            # Get or create a browser context
            context = await PlaywrightBrowserManager._get_or_create_context()
            page = await context.new_page()

            render_mode = None
            try:
                # First attempt: thorough render with "networkidle"
                await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                render_mode = "networkidle"

            except asyncio.TimeoutError:
                # Fallback: fast render with "load" on timeout
                try:
                    logger.debug(f"Networkidle timeout for {url}, retrying with 'load'")
                    await page.goto(url, wait_until="load", timeout=5000)
                    render_mode = "load"
                except Exception as e:
                    await page.close()
                    logger.warning(f"Failed to render {url}: {str(e)}")
                    return "", {"error": f"render_failed: {str(e)}", "url": url}

            # Get rendered HTML
            html = await page.content()

            # Collect metadata
            metadata = {
                "rendered_at": True,
                "render_mode": render_mode,
                "url": url,
                "title": await page.title(),
            }

            await page.close()
            return html, metadata

        except Exception as e:
            logger.error(f"Critical error rendering {url}: {str(e)}")
            # Browser might have crashed - reset it for retry
            await PlaywrightBrowserManager._reset_browser()
            return "", {"error": f"critical: {str(e)}", "url": url}

        finally:
            # Return context to pool for reuse
            if context:
                await PlaywrightBrowserManager._return_context_to_pool(context)

    @staticmethod
    async def _reset_browser():
        """Reset the browser instance (on crash or critical error)."""
        PlaywrightBrowserManager._browser_retry_count += 1

        if PlaywrightBrowserManager._browser_retry_count > PlaywrightBrowserManager._browser_max_retries:
            logger.error("Browser max retries exceeded, not restarting")
            return

        logger.warning(f"Resetting browser (attempt {PlaywrightBrowserManager._browser_retry_count})")

        async with PlaywrightBrowserManager._lock:
            # Close all contexts
            for ctx in PlaywrightBrowserManager._context_pool:
                try:
                    await ctx.close()
                except:
                    pass
            PlaywrightBrowserManager._context_pool.clear()

            # Close browser
            if PlaywrightBrowserManager._browser:
                try:
                    await PlaywrightBrowserManager._browser.close()
                except:
                    pass

            # Close playwright
            if PlaywrightBrowserManager._playwright:
                try:
                    await PlaywrightBrowserManager._playwright.stop()
                except:
                    pass

            # Reset for next initialization
            PlaywrightBrowserManager._browser = None
            PlaywrightBrowserManager._playwright = None

    @staticmethod
    async def render_page_with_consent_bypass(url: str, timeout: int = 15) -> Tuple[str, dict]:
        """
        Render a page and attempt to dismiss cookie consent popups before extracting content.
        Tries common consent button selectors and text patterns (OneTrust, Cookiebot, custom).
        Returns (html, metadata) — same signature as render_page().
        """
        context = None
        try:
            context = await PlaywrightBrowserManager._get_or_create_context()
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except asyncio.TimeoutError:
                await page.close()
                return "", {"error": "timeout", "url": url}

            # Common cookie consent selectors (ID/class/attribute based)
            consent_selectors = [
                "#onetrust-accept-btn-handler",       # OneTrust (very common)
                "#accept-cookies",
                "#cookieAccept",
                "#cookie-accept",
                "#cookie_action_close_header",
                ".cookie-accept",
                ".accept-cookies",
                ".cc-accept",
                "#cc-accept",
                ".gdpr-accept",
                "[data-cookiebanner='accept']",
                "[data-testid='cookie-accept']",
                "[aria-label*='Accept all']",
                "[aria-label*='accept all']",
            ]

            # Text-based button patterns (fallback)
            consent_texts = [
                "Accept all", "Accept All", "Accept All Cookies",
                "Accept Cookies", "Accept", "I Accept", "I Agree",
                "Agree", "Allow All", "Allow all cookies",
                "Got it", "OK", "Okay",
            ]

            clicked = False

            # Try selector-based first (faster)
            for selector in consent_selectors:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=400):
                        await btn.click(timeout=2000)
                        clicked = True
                        break
                except Exception:
                    continue

            # Try text-based if no selector matched
            if not clicked:
                for text in consent_texts:
                    try:
                        btn = page.get_by_role("button", name=text, exact=True).first
                        if await btn.is_visible(timeout=400):
                            await btn.click(timeout=2000)
                            clicked = True
                            break
                    except Exception:
                        continue

            if clicked:
                # Wait for consent overlay to disappear and page to settle
                await page.wait_for_timeout(1500)
                logger.debug(f"Cookie consent dismissed for {url}")
            else:
                logger.debug(f"No consent button found for {url}")

            html = await page.content()
            metadata = {
                "rendered_at": True,
                "consent_bypassed": clicked,
                "url": url,
                "title": await page.title(),
            }

            await page.close()
            return html, metadata

        except Exception as e:
            logger.error(f"Consent bypass error for {url}: {str(e)}")
            return "", {"error": str(e), "url": url}

        finally:
            if context:
                await PlaywrightBrowserManager._return_context_to_pool(context)

    @staticmethod
    async def shutdown():
        """
        Graceful shutdown - close all contexts and browser instance.
        Call this on application shutdown.
        """
        logger.info("Shutting down Playwright browser")

        async with PlaywrightBrowserManager._lock:
            # Close all contexts in pool
            for context in PlaywrightBrowserManager._context_pool:
                try:
                    await context.close()
                    logger.debug("Closed Playwright context")
                except Exception as e:
                    logger.warning(f"Error closing context: {str(e)}")

            PlaywrightBrowserManager._context_pool.clear()

            # Close browser
            if PlaywrightBrowserManager._browser:
                try:
                    await PlaywrightBrowserManager._browser.close()
                    logger.debug("Closed Playwright browser")
                except Exception as e:
                    logger.warning(f"Error closing browser: {str(e)}")

            # Stop playwright
            if PlaywrightBrowserManager._playwright:
                try:
                    await PlaywrightBrowserManager._playwright.stop()
                    logger.debug("Stopped Playwright")
                except Exception as e:
                    logger.warning(f"Error stopping Playwright: {str(e)}")

            # Reset
            PlaywrightBrowserManager._browser = None
            PlaywrightBrowserManager._playwright = None
            PlaywrightBrowserManager._browser_retry_count = 0

        logger.info("Playwright shutdown complete")
