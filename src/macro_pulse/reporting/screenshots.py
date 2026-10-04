import os
import shutil
import time

from ..core.artifacts import resolve_output_path
from ..core.logging import get_logger

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:  # pragma: no cover - exercised in environments without selenium
    webdriver = None
    Options = None
    ChromeService = None
    By = None
    EC = None
    WebDriverWait = None

try:
    from webdriver_manager.chrome import ChromeDriverManager
except (
    ImportError
):  # pragma: no cover - exercised in environments without webdriver-manager
    ChromeDriverManager = None


logger = get_logger(__name__)


FINVIZ_URL = "https://finviz.com/map.ashx"
MARKETMAP_URLS = {
    "kospi": "https://markets.hankyung.com/marketmap/kospi",
    "kosdaq": "https://markets.hankyung.com/marketmap/kosdaq",
}
MARKETMAP_WRAPPER_SELECTORS = (
    "#map_area.fiq-marketmap",
    "div.fiq-marketmap",
)
MARKETMAP_SVG_SELECTOR = "svg.anychart-ui-support"


def get_chrome_driver():
    if webdriver is None or Options is None or ChromeService is None:
        logger.warning(
            "Selenium runtime is unavailable. Install selenium and webdriver-manager to enable screenshots."
        )
        return None

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1600")
    chrome_options.add_argument("--hide-scrollbars")
    chrome_options.add_argument("--force-device-scale-factor=1")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    chrome_options.set_capability("pageLoadStrategy", "eager")

    chrome_binary = _resolve_chrome_binary()
    if chrome_binary:
        chrome_options.binary_location = chrome_binary

    try:
        service = ChromeService(_resolve_chromedriver_binary())
        return webdriver.Chrome(service=service, options=chrome_options)
    except Exception as exc:
        logger.error("Failed to initialize Chrome Driver: %s", exc)
        return None


def capture_screenshots(targets):
    screenshot_paths = []
    if targets:
        logger.info("Taking screenshots for targets: %s", ", ".join(targets))

    for target in targets:
        capture = SCREENSHOT_HANDLERS.get(target)
        if capture is None:
            logger.warning("Unknown screenshot target in config: %s", target)
            continue
        screenshot_path = capture()
        if screenshot_path:
            screenshot_paths.append(screenshot_path)

    return screenshot_paths


def resize_window_for_element(driver, element, min_width=1600, padding=120):
    dimensions = driver.execute_script(
        """
        const el = arguments[0];
        el.scrollIntoView({block: 'start', inline: 'nearest'});
        const rect = el.getBoundingClientRect();
        return {
            width: Math.ceil(Math.max(rect.width, el.scrollWidth, el.clientWidth)),
            height: Math.ceil(Math.max(rect.height, el.scrollHeight, el.clientHeight)),
        };
        """,
        element,
    )

    width = max(min_width, dimensions["width"] + 40)
    height = max(1200, dimensions["height"] + padding)
    logger.info("Resizing window to %sx%s for element capture...", width, height)
    driver.set_window_size(width, height)
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'start', inline: 'nearest'});", element
    )
    time.sleep(2)


def wait_for_marketmap_svg(driver, timeout=40):
    wait = WebDriverWait(driver, timeout)
    last_error = None

    for selector in MARKETMAP_WRAPPER_SELECTORS:
        try:
            logger.info("Waiting for rendered SVG in: %s", selector)

            def svg_ready(_driver):
                wrapper = _driver.find_element(By.CSS_SELECTOR, selector)
                if not wrapper.is_displayed():
                    return False

                svg = wrapper.find_element(By.CSS_SELECTOR, MARKETMAP_SVG_SELECTOR)
                if not svg.is_displayed():
                    return False

                metrics = _driver.execute_script(
                    """
                    const svg = arguments[0];
                    const rect = svg.getBoundingClientRect();
                    return {
                        width: Math.ceil(rect.width),
                        height: Math.ceil(rect.height),
                        nodeCount: svg.querySelectorAll('*').length,
                        textLength: svg.textContent.trim().length,
                    };
                    """,
                    svg,
                )

                ready = (
                    metrics["width"] > 1000
                    and metrics["height"] > 700
                    and metrics["nodeCount"] > 25
                    and metrics["textLength"] > 20
                )
                return svg if ready else False

            return wait.until(svg_ready)
        except Exception as exc:
            last_error = exc

    if last_error:
        raise last_error

    raise RuntimeError("Failed to locate rendered market map SVG.")


def position_element_for_capture(driver, element, top_offset=160):
    driver.execute_script(
        """
        const el = arguments[0];
        const topOffset = arguments[1];
        const rect = el.getBoundingClientRect();
        window.scrollTo(0, window.scrollY + rect.top - topOffset);
        """,
        element,
        top_offset,
    )
    time.sleep(1)


def dismiss_finviz_overlays(driver):
    """Remove Finviz promotional dialogs and tutorial callouts before capture."""
    removed = driver.execute_script(
        """
        const map = document.getElementById('canvas-wrapper');
        const promoPhrases = [
            'Upgrade your Finviz Experience',
            'Learn more about Finviz Elite',
            'Finviz Elite'
        ];
        const tutorialPhrases = [
            'An interactive directory of the market',
            'Pick a sector, drop into an industry',
            'New: Why Is It Moving',
            'Hover any tile for an AI summary of why it moved',
            'Show Industry and Colorblind Mode live here too'
        ];
        const targetPhrases = [...promoPhrases, ...tutorialPhrases];
        let removedCount = 0;

        function removeOverlayAncestor(node) {
            let current = node;
            for (let depth = 0; depth < 12 && current && current !== document.body; depth++) {
                const style = window.getComputedStyle(current);
                const rect = current.getBoundingClientRect();
                const positioned =
                    style.position === 'fixed' ||
                    style.position === 'absolute' ||
                    style.position === 'sticky';
                const plausibleOverlay =
                    positioned &&
                    rect.width >= 180 &&
                    rect.width <= window.innerWidth * 0.95 &&
                    rect.height >= 60 &&
                    rect.height <= window.innerHeight * 0.8;

                if (plausibleOverlay) {
                    current.remove();
                    removedCount += 1;
                    return true;
                }
                current = current.parentElement;
            }
            return false;
        }

        // Remove known Finviz promotional and onboarding/tutorial boxes by text.
        for (const node of Array.from(document.querySelectorAll('body *'))) {
            if (!node.isConnected) {
                continue;
            }
            const text = (node.innerText || '').trim();
            if (!text || !targetPhrases.some((phrase) => text.includes(phrase))) {
                continue;
            }
            removeOverlayAncestor(node);
        }

        // Some onboarding cards use small X buttons. If one remains inside an
        // overlay containing known tutorial wording, remove the whole card.
        for (const node of Array.from(document.querySelectorAll('body *'))) {
            if (!node.isConnected) {
                continue;
            }
            const text = (node.innerText || '').trim();
            if (!text || !tutorialPhrases.some((phrase) => text.includes(phrase))) {
                continue;
            }
            removeOverlayAncestor(node);
        }

        // Remove any full-screen fixed backdrop still sitting above the map.
        for (const node of Array.from(document.querySelectorAll('body *'))) {
            if (!node.isConnected || node === map || (map && node.contains(map))) {
                continue;
            }
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
            const coversViewport =
                rect.width >= window.innerWidth * 0.7 &&
                rect.height >= window.innerHeight * 0.7;
            if (style.position === 'fixed' && coversViewport && zIndex >= 10) {
                node.remove();
                removedCount += 1;
            }
        }

        document.documentElement.style.overflow = 'auto';
        document.body.style.overflow = 'auto';
        return removedCount;
        """
    )
    if removed:
        logger.info("Removed %s Finviz overlay element(s) before capture", removed)
        time.sleep(1)


def take_finviz_screenshot(output_path=None):
    driver = get_chrome_driver()
    if not driver:
        return None

    try:
        output_path = resolve_output_path(output_path, "finviz_map")
        logger.info("Navigating to %s...", FINVIZ_URL)
        driver.get(FINVIZ_URL)

        logger.info("Waiting for map element...")
        element = WebDriverWait(driver, 20).until(
            EC.visibility_of_element_located((By.ID, "canvas-wrapper"))
        )

        logger.info("Waiting for canvas to render...")
        time.sleep(5)
        dismiss_finviz_overlays(driver)

        # Finviz can inject onboarding tips a moment after the map first renders.
        time.sleep(1)
        dismiss_finviz_overlays(driver)

        # Re-fetch the element after DOM cleanup in case Finviz re-rendered the map.
        element = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.ID, "canvas-wrapper"))
        )
        element.screenshot(output_path)
        logger.info("Screenshot saved to %s", output_path)
        return output_path
    except Exception as exc:
        logger.exception("Failed to take screenshot: %s", exc)
        return None
    finally:
        driver.quit()


def take_kospi_screenshot(output_path=None):
    return _take_hankyung_marketmap_screenshot("kospi", output_path)


def take_kosdaq_screenshot(output_path=None):
    return _take_hankyung_marketmap_screenshot("kosdaq", output_path)


def _take_hankyung_marketmap_screenshot(market, output_path):
    driver = get_chrome_driver()
    if not driver:
        return None

    try:
        output_path = resolve_output_path(output_path, f"{market}_map")
        url = MARKETMAP_URLS[market]

        for attempt in range(2):
            logger.info("Navigating to %s... (attempt %s)", url, attempt + 1)
            driver.get(url)
            WebDriverWait(driver, 30).until(
                lambda current_driver: (
                    current_driver.execute_script("return document.readyState")
                    in ("interactive", "complete")
                )
            )

            try:
                logger.info("Waiting for chart SVG to render...")
                svg = wait_for_marketmap_svg(driver, timeout=40)
                resize_window_for_element(driver, svg, min_width=1800, padding=240)
                svg = wait_for_marketmap_svg(driver, timeout=20)
                position_element_for_capture(driver, svg, top_offset=180)
                time.sleep(3)
                svg.screenshot(output_path)
                logger.info("Screenshot saved to %s", output_path)
                return output_path
            except Exception as exc:
                logger.warning("Capture attempt %s failed: %s", attempt + 1, exc)
                if attempt == 1:
                    raise
    except Exception as exc:
        logger.exception("Failed to take %s screenshot: %s", market.upper(), exc)
        return None
    finally:
        driver.quit()


def _resolve_chrome_binary():
    return (
        os.environ.get("CHROME_BIN")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )


def _resolve_chromedriver_binary():
    if os.environ.get("CHROMEDRIVER_BIN"):
        return os.environ["CHROMEDRIVER_BIN"]

    local_binary = shutil.which("chromedriver")
    if local_binary:
        return local_binary

    if ChromeDriverManager is None:
        raise RuntimeError(
            "No chromedriver binary found and webdriver-manager is not installed."
        )

    return ChromeDriverManager().install()


SCREENSHOT_HANDLERS = {
    "finviz": take_finviz_screenshot,
    "kospi": take_kospi_screenshot,
    "kosdaq": take_kosdaq_screenshot,
}
