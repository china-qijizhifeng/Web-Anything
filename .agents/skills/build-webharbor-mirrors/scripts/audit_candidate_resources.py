#!/usr/bin/env python3
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from fidelity_contract import fidelity_evidence_issues
from quality_contract import quality_evidence_issues


def repository_root() -> Path:
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / "sites").is_dir() and (parent / "mirror_factory").is_dir():
            return parent
    return script_path.parents[4]


MISSING_ASSET_PATH = "/__webharbor_missing_asset"


def parse_site(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("site must use NAME=URL")
    name, url = value.split("=", 1)
    return name.strip(), url.rstrip("/") + "/"


async def inspect_page(browser, name, base_url):
    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
    page = await context.new_page()
    base_origin = urlsplit(base_url).netloc
    failed_requests = []
    http_errors = []
    external_requests = []
    missing_placeholder_requests = []
    workflow = None
    workflow_errors = []

    def record_request(request):
        request_url = request.url
        parsed = urlsplit(request_url)
        if parsed.path == MISSING_ASSET_PATH:
            missing_placeholder_requests.append(request_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc != base_origin:
            external_requests.append(request_url)

    page.on("request", record_request)
    page.on("requestfailed", lambda request: failed_requests.append(request.url))
    page.on(
        "response",
        lambda response: http_errors.append({"status": response.status, "url": response.url})
        if response.status >= 400
        else None,
    )

    image_count = 0
    broken_images = []

    async def inspect_images():
        nonlocal image_count
        await page.locator("img").evaluate_all(
            "images => images.forEach(image => { image.loading = 'eager'; })"
        )
        try:
            await page.wait_for_function(
                "() => Array.from(document.images).every(image => image.complete)",
                timeout=5_000,
            )
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(250)
        images = await page.locator("img").evaluate_all(
            """images => images.map(image => ({
                src: image.currentSrc || image.src,
                complete: image.complete,
                naturalWidth: image.naturalWidth,
                naturalHeight: image.naturalHeight
            }))"""
        )
        image_count += len(images)
        broken_images.extend(
            {"page": page.url, **image}
            for image in images
            if not image["complete"] or image["naturalWidth"] == 0 or image["naturalHeight"] == 0
        )

    response = await page.goto(base_url, wait_until="domcontentloaded", timeout=20_000)
    await page.wait_for_timeout(250)
    await inspect_images()
    root_diagnostics = await page.evaluate(
        """missingPath => {
          const links = Array.from(document.querySelectorAll('link[rel~="stylesheet"]'));
          const references = Array.from(document.querySelectorAll('[src],[href],[action]'))
            .flatMap(element => ['src', 'href', 'action'].map(attribute => ({
              tag: element.tagName.toLowerCase(),
              attribute,
              value: element.getAttribute(attribute)
            })))
            .filter(record => record.value && record.value.includes(missingPath));
          const bodyText = document.body?.innerText || '';
          const mainText = document.querySelector('main')?.innerText || '';
          const headings = document.querySelectorAll('h1,h2,h3').length;
          const contentElements = document.querySelectorAll(
            'a,button,form,h1,h2,h3,img,input,select,video'
          ).length;
          const bodyTextBytes = new TextEncoder().encode(bodyText).length;
          const mainTextBytes = new TextEncoder().encode(mainText).length;
          return {
            bodyTextBytes,
            mainTextBytes,
            headings,
            contentElements,
            hasMain: Boolean(document.querySelector('main')),
            stylesheetCount: links.length,
            missingStylesheets: links.filter(link => !link.sheet).map(link => link.href),
            missingPlaceholderReferences: references,
            meaningful: mainTextBytes >= 64 || headings > 0 ||
              contentElements >= 20 || bodyTextBytes >= 512
          };
        }""",
        MISSING_ASSET_PATH,
    )
    root_result = {
        "http_status": response.status if response else None,
        "expected_path": urlsplit(base_url).path or "/",
        "final_path": urlsplit(page.url).path or "/",
        "missing_placeholder_requests": sorted(set(missing_placeholder_requests)),
        **root_diagnostics,
    }
    root_result["failures"] = deployment_page_failures(root_result)

    cart_layout = None
    if name == "bestbuy":
        await page.goto(urljoin(base_url, "login?next=/cart"), wait_until="domcontentloaded")
        await page.locator('input[name="email"]').fill("alice.j@test.com")
        await page.locator('input[name="password"]').fill("TestPass123!")
        await page.locator('form.auth-form button[type="submit"]').click()
        await page.wait_for_load_state("domcontentloaded")
        if urlsplit(page.url).path != "/cart":
            await page.goto(urljoin(base_url, "cart"), wait_until="domcontentloaded")
        await inspect_images()
        cart_layout = await page.locator(".cart-row").evaluate_all(
            """rows => rows.map(row => {
                const product = row.querySelector('.mini-product').getBoundingClientRect();
                const actions = row.querySelector('.cart-row__actions').getBoundingClientRect();
                const bounds = row.getBoundingClientRect();
                const overlap = !(
                    product.right <= actions.left || actions.right <= product.left ||
                    product.bottom <= actions.top || actions.bottom <= product.top
                );
                return {
                    overlap,
                    overflow: Math.max(product.right, actions.right) > bounds.right + 1,
                    rowWidth: Math.round(bounds.width),
                    productWidth: Math.round(product.width),
                    actionsWidth: Math.round(actions.width)
                };
            })"""
        )

    if name == "phet_simulations":
        simulation_link = page.locator('a[href^="/simulation/"]').first
        simulation_href = await simulation_link.get_attribute("href")
        if not simulation_href:
            workflow_errors.append("homepage has no simulation link")
        else:
            await page.goto(urljoin(base_url, simulation_href), wait_until="domcontentloaded")
            await inspect_images()
            try:
                dialog_future = asyncio.get_running_loop().create_future()

                async def capture_dialog(dialog):
                    if not dialog_future.done():
                        dialog_future.set_result(dialog.message)
                    await dialog.dismiss()

                page.once("dialog", lambda dialog: asyncio.create_task(capture_dialog(dialog)))
                await page.locator(".sim-detail-play-overlay").click(timeout=3_000)
                dialog_message = await asyncio.wait_for(dialog_future, timeout=3)
                workflow = {"play_dialog": dialog_message}
            except (PlaywrightTimeoutError, TimeoutError):
                workflow_errors.append("play control did not open its benchmark-mode notice")

    if name == "discogs":
        await page.goto(urljoin(base_url, "marketplace"), wait_until="domcontentloaded")
        await inspect_images()
        action_links = await page.locator(".listing-row a.btn").evaluate_all(
            "links => links.map(link => link.getAttribute('href'))"
        )
        empty_links = [href for href in action_links if not href or href == "#"]
        workflow = {"marketplace_action_links": len(action_links), "empty_links": empty_links}
        if not action_links:
            workflow_errors.append("marketplace has no action links")
        if empty_links:
            workflow_errors.append("marketplace still contains empty action links")

        release_href = await page.locator(".listing-row a.btn").first.get_attribute("href")
        if release_href:
            await page.goto(urljoin(base_url, release_href), wait_until="domcontentloaded")
            await inspect_images()
            seller_links = await page.locator('.listing-row a.btn:has-text("View seller")').evaluate_all(
                "links => links.map(link => link.getAttribute('href'))"
            )
            workflow["release_seller_links"] = seller_links
            if not seller_links or any(not href or href == "#" for href in seller_links):
                workflow_errors.append("release page has no valid seller action links")

    result = {
        "site": name,
        "url": base_url,
        "image_count": image_count,
        "broken_images": broken_images,
        "failed_requests": sorted(set(failed_requests)),
        "http_errors": http_errors,
        "external_requests": sorted(set(external_requests)),
        "root": root_result,
        "cart_layout": cart_layout,
        "workflow": workflow,
        "workflow_errors": workflow_errors,
    }
    result["ok"] = not any(
        (
            result["broken_images"],
            result["failed_requests"],
            result["http_errors"],
            result["external_requests"],
            result["root"]["failures"],
            result["workflow_errors"],
            [row for row in cart_layout or [] if row["overlap"] or row["overflow"]],
        )
    )
    await context.close()
    return result


def deployment_page_failures(result):
    failures = []
    if result.get("http_status") != 200:
        failures.append(f"entry route returned HTTP {result.get('http_status')}")
    if result.get("final_path") != result.get("expected_path"):
        failures.append(
            f"entry route navigated to {result.get('final_path')!r} instead of "
            f"{result.get('expected_path')!r}"
        )
    if result.get("missing_placeholder_requests"):
        failures.append("entry route requested a missing-asset placeholder")
    if result.get("missingPlaceholderReferences"):
        failures.append("entry route DOM references a missing-asset placeholder")
    if result.get("missingStylesheets"):
        failures.append("entry route contains an unloaded stylesheet")
    if result.get("meaningful") is not True:
        failures.append("entry route lacks meaningful rendered content")
    return failures


def current_evidence_issues(root, site):
    evidence_root = root / "mirror_factory" / "evidence" / site
    issues = {}
    for name, validator in (
        ("fidelity", fidelity_evidence_issues),
        ("quality", quality_evidence_issues),
    ):
        path = evidence_root / f"{name}.json"
        if not path.is_file():
            issues[name] = [f"missing {name} evidence"]
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            issues[name] = [f"invalid {name} evidence: {exc}"]
            continue
        issues[name] = validator(value, root=root)
    return issues


def apply_current_evidence_gate(results, repository_root):
    for result in results:
        issues = current_evidence_issues(repository_root, result["site"])
        result["evidence_issues"] = issues
        if issues["fidelity"] or issues["quality"]:
            result["ok"] = False
    return results


async def run(args):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            results = [await inspect_page(browser, name, url) for name, url in args.site]
        finally:
            await browser.close()
    if args.require_current_quality:
        apply_current_evidence_gate(results, args.repository_root)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Audit rendered images, network resources, and selected desktop workflows."
    )
    parser.add_argument(
        "--site",
        action="append",
        type=parse_site,
        required=True,
        metavar="NAME=URL",
        help="site identifier and local base URL; repeat for multiple sites",
    )
    parser.add_argument(
        "--require-current-quality",
        action="store_true",
        help="also require current passing fidelity and three-fidelity quality evidence",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=repository_root(),
        help="repository containing mirror evidence (defaults to this skill's repository)",
    )
    args = parser.parse_args()
    args.repository_root = args.repository_root.resolve()
    results = asyncio.run(run(args))
    print(json.dumps({"ok": all(result["ok"] for result in results), "sites": results}, indent=2))
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
