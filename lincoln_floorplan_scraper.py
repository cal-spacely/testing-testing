import os
import json
import asyncio
from playwright.async_api import async_playwright
import random
import csv
from urllib.parse import urlparse 
from bs4 import BeautifulSoup
import re
import logging

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:119.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; Samsung Galaxy S22) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},  # Desktop Full HD
    {"width": 1366, "height": 768},   # Common laptop
    {"width": 1440, "height": 900},   # Macbook Pro
    {"width": 2560, "height": 1440},  # Large desktop
    {"width": 390, "height": 844},    # iPhone 13 Pro
    {"width": 412, "height": 915},    # Pixel 6 Pro
    {"width": 820, "height": 1180},   # iPad Air
]

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "playwright_pfofile")

# ---------- POPUP HANDLER ----------
async def dismiss_popups(page):
    """Dismiss common banners and modals."""
    selectors = [
        ("button#onetrust-accept-btn-handler", "Cookie consent"),
        ("button#specialsClose", "Leasing special"),
        ("button#getFlexClose", "GetFlex modal"),
        ("button[data-selenium-id='customNudgeClose']", "Nudge popup"),
    ]
    for sel, label in selectors:
        try:
            await page.click(sel, timeout=4000)
            print(f"✅ {label} dismissed.")
        except:
            pass

def get_base_url(url: str) ->str:
    """Strip query params, fragemsnts and only keep scheme + netloc."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"

async def new_stealth_context(browser):
    """Randomize user-agent + viewport for stealth."""
    user_agent = random.choice(USER_AGENTS)
    viewport = random.choice(VIEWPORTS)
    return await browser.new_context(
        user_agent=user_agent,
        viewport=viewport,
        device_scale_factor=1.0,
        is_mobile=viewport["width"] < 500,
        has_touch=viewport["width"] < 500,
    )

#-----------Extract card-style floorplans--------------
async def extract_card_style_floorplans(page):
    """Extract floorplans from card-style layouts (e.g. Chase Landing)."""
    floorplans, seen = [], set()

    # Each floorplan card
    cards = await page.query_selector_all("div.fp-container")

    for card in cards:
        # Floorplan name
        name_el = await card.query_selector(".card-title")
        name = (await name_el.inner_text()).strip() if name_el else None

        # Beds (look for text containing "Bed")
        beds = None
        beds_el = await card.query_selector(".nu-bed, .nu.nu-bed, .nu-bedrooms")
        if beds_el:
            beds = (await beds_el.evaluate("el => el.parentElement.textContent")).strip()

        # Baths (look for text containing "Bath")
        baths = None
        baths_el = await card.query_selector(".nu-bathroom, .nu-bath, .nu-baths")
        if baths_el:
            baths = (await baths_el.evaluate("el => el.parentElement.textContent")).strip()

        # Sqft (look for text containing "Sq. Ft.")
        sqft = None
        sqft_el = await card.query_selector(".nu-area, .nu-squarefeet, .nu-sqft")
        if sqft_el:
            sqft = (await sqft_el.evaluate("el => el.parentElement.textContent")).strip()

        # Price (look for "Starting at")
        price = None
        price_el = await card.query_selector(".font-weight-bold")
        if price_el:
            price = (await price_el.inner_text()).strip()

        # Deduplicate by name
        if name and name not in seen:
            seen.add(name)
            floorplans.append({
                "fp_name": name,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "price": price
            })

    return floorplans

#---------RealPage floorplan scraper--------------
async def extract_realpage_style_floorplans(page):
    """Extract floorplans from RealPage-style layout (e.g. Paseo Eilan)."""
    floorplans, seen = [], set()
    cards = await page.query_selector_all("div.plan__cell")
    for card in cards:
        name = await card.eval_on_selector("h2.realpageTitle", "el => el.innerText.trim()") if await card.query_selector("h2.realpageTitle") else None
        info = await card.eval_on_selector("h3.listing-unit-info", "el => el.innerText.trim()") if await card.query_selector("h3.listing-unit-info") else None

        # crude parsing of info like "Studio | 1 Bath 514 SQFT Starting At $1,205"
        beds = baths = sqft = price = None
        if info:
            # Split on <br> artifacts and pipes
            parts = [p.strip() for p in info.replace("\n", " ").split("|")]
            if len(parts) >= 1:
                beds = parts[0]
            if len(parts) >= 2:
                baths = parts[1]
            # look for SQFT
            if "SQFT" in info:
                sqft = info.split("SQFT")[0].split()[-1] + " SQFT"
            # look for price
            if "Starting At" in info:
                price = info.split("Starting At")[-1].strip()

        if name and name not in seen:
            seen.add(name)
            floorplans.append({
                "fp_name": name,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "price": price
            })
    return floorplans

#-----------Original RentCafe floorplan extractor ------------
async def extract_rentcafe_floorplans_old(page):
    floorplans, seen = [], set()
    cards = await page.query_selector_all("div#floorplans-container div.fp-container")
    for card in cards:
        name = await card.locator(".fp-title").inner_text() if await card.locator(".fp-title").count() else None
        beds = await card.locator(".fp-beds").inner_text() if await card.locator(".fp-beds").count() else None
        baths = await card.locator(".fp-baths").inner_text() if await card.locator(".fp-baths").count() else None
        price = await card.locator(".fp-rent").inner_text() if await card.locator(".fp-rent").count() else None
        sqft  = await card.locator(".fp-sqft").inner_text() if await card.locator(".fp-sqft").count() else None

        if name and name not in seen:
            seen.add(name)
            floorplans.append({
                "fp_name": name,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "price": price
            })
    return floorplans

#----------Revised RentCafe floorplan extractor ----------
async def extract_rentcafe_floorplans(page):
    floorplans = []
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")

    # ---- Primary: parse modal-content ----
    modals = soup.select("div.modal-content[id^='modal-content-']")
    for modal in modals:
        try:
            # Name
            name_el = modal.select_one("h2[id^='fp-modalLabel']")
            name = name_el.get_text(strip=True) if name_el else None

            # Beds, Baths, SqFt
            details = modal.select_one("ul.list-unstyled")
            beds = baths = sqft = None
            if details:
                items = [li.get_text(strip=True) for li in details.select("li")]
                for item in items:
                    if "Bed" in item:
                        beds = re.sub(r"\s*Beds?", "", item).strip()
                    elif "Bath" in item:
                        baths = re.sub(r"\s*Baths?", "", item).strip()
                    elif "Sq" in item:
                        sqft = re.sub(r"[^\d\-]", "", item)

            # Price
            price_el = modal.select_one("span.text-dark.font-weight-medium")
            price = price_el.get_text(" ", strip=True) if price_el else None

            if name:
                floorplans.append({
                    "name": name,
                    "beds": beds,
                    "baths": baths,
                    "sqft": sqft,
                    "price": price
                })
        except Exception as e:
            print(f"⚠️ Error parsing modal: {e}")

    # ---- Fallback: parse JSON-LD if no floorplans found ----
    if not floorplans:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and "accommodationFloorPlan" in data:
                    for fp in data["accommodationFloorPlan"]:
                        floorplans.append({
                            "name": fp.get("name"),
                            "beds": fp.get("numberOfBedrooms"),
                            "baths": fp.get("numberOfFullBathrooms"),
                            "sqft": fp.get("floorSize", {}).get("maxValue"),
                            "price": None  # no price info in JSON-LD
                        })
            except Exception:
                continue

    return floorplans

# ---------- FLOORPLAN SCRAPER ----------
async def extract_floorplans_from_page(page):
    """Helper to extract floorplans given a page with WillowBridge selectors."""
    floorplans, seen = [], set()
    try:
        slides = await page.query_selector_all("div.floorplan-slide .floorplan")
    except:
        slides = []
    for slide in slides:
        name = await slide.eval_on_selector("h2.title", "el => el.innerText.trim()") if await slide.query_selector("h2.title") else None
        beds = await slide.eval_on_selector("span.plan-beds", "el => el.innerText.trim()") if await slide.query_selector("span.plan-beds") else None
        baths = await slide.eval_on_selector("span.plan-bath", "el => el.innerText.trim()") if await slide.query_selector("span.plan-bath") else None
        sqft = await slide.eval_on_selector("span.plan-sqft", "el => el.innerText.trim()") if await slide.query_selector("span.plan-sqft") else None
        price = await slide.eval_on_selector("div.plan-price", "el => el.innerText.trim()") if await slide.query_selector("div.plan-price") else None

        if name and name not in seen:
            seen.add(name)
            floorplans.append({
                "fp_name": name, "beds": beds, "baths": baths, "sqft": sqft, "price": price
            })
    return floorplans


async def launch_persistent():
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.70 Safari/537.36"
    ]
    ua = random.choice(user_agents)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=False,  # debug mode; switch to True when stable
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--window-size=1280,800",
        ],
        viewport={"width": 1280, "height": 800},
        user_agent=ua,
        locale="en-US",
    )

    page = await context.new_page()

    # Patch navigator props
    await page.add_init_script(
        """() => {
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        }"""
    )

    return pw, context, page

# ---------- MAIN SCRAPER ----------
async def scrape_floorplans(url: str):
    floorplans = []
    pw, context, page = await launch_persistent()
    try:
        await page.goto(url, timeout=60000)
        await dismiss_popups(page)

        # ---- CASE 1: Native Willow Bridge ----
        try:
            await page.wait_for_selector("div.floorplan-slide .floorplan", timeout=8000)
            print("✅ Found Willow Bridge floorplans")
            floorplans = await extract_floorplans_from_page(page)
        except:
               floorplans = []

        # ---- CASE 2: External "Visit Website" ----
        if not floorplans:
            visit_btn = await page.query_selector("a.community__visit-website-btn, a.cta[title='Visit Website']")
            if visit_btn:
                external_url = await visit_btn.get_attribute("href")
                if external_url:
                    if external_url.startswith("/"):
                        external_url = f"https://www.willowbridgepc.com{external_url}"
                    print(f"↪ Following external site: {external_url}")
                    await page.goto(external_url, timeout=60000)
                    await dismiss_popups(page)

                    # Sub-case A: Click nav link "Floor Plans"
                    nav_link = None
                    try:
                        nav_link = await page.query_selector("a:has-text('Floor Plans'), a:has-text('View all Floor Plans)")
                    except:
                        pass
                    if nav_link:
                        print("↪ Clicking 'Floor Plans' nav link")
                        await nav_link.click()
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        await dismiss_popups(page)
                        floorplans = await extract_floorplans_from_page(page)

                    # Sub-case B: Direct /floorplans/ URL
                    if not floorplans and "/floorplans" not in page.url:
                        base_url = get_base_url(external_url)
                        test_url = base_url.rstrip("/") + "/floorplans/"
                        print(f"↪ Testing {test_url}")
                        try:
                            await page.goto(test_url, timeout=15000)
                            await dismiss_popups(page)
                            floorplans = await extract_floorplans_from_page(page)
                        except:
                            print(f"⚠️ Could not load {test_url}")

                    # Sub-case C: Nested "Floor Plan" → "Floorplans"
                    if not floorplans:
                        try:
                            # Click the anchor with href="#floorplan"
                            floorplan_anchor = await page.query_selector("a[href='#floorplan']")
                            if floorplan_anchor:
                                print("↪ Clicking '#floorplan' link")
                                await floorplan_anchor.click()
                                await page.wait_for_timeout(2000)
                                # Then try to follow the real floorplans link
                                deeper = await page.query_selector("a[href*='/floor-plan']")
                                if deeper:
                                    target = await deeper.get_attribute("href")
                                    if target:
                                        if target.startswith("/"):
                                            target = external_url.rstrip("/") + target
                                        print(f"↪ Navigating to {target}")
                                        await page.goto(target, timeout=15000)
                                        await dismiss_popups(page)
                                        floorplans = await extract_floorplans_from_page(page)
                        except:
                            pass
                        # Sub-case D: RealPage-style floorplans
                        if not floorplans:
                            try:
                                await page.wait_for_selector("div.plan__cell", timeout=6000)
                                print("✅ Found RealPage-style floorplans")
                                floorplans = await extract_realpage_style_floorplans(page)
                            except:
                                pass
                        # Sub-case E: RentCafe / Card-style grid
                        if not floorplans:
                            try:
                                await dismiss_popups(page)
                                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                            except:
                                print("Network never went idle, continuing anyway")
                            for attempt in range(3):
                                try:
                                    # Look for either card containers or modal triggers
                                    await page.wait_for_selector(".fp-container, div.modal-content", timeout=15000)
                                    # If modal-content exists, parse with new function
                                    html = await page.content()
                                    if "modal-content" in html:
                                        print("✅ Found RentCafe modal-style floorplans")
                                        floorplans = await extract_rentcafe_floorplans(page)
                                        if floorplans:
                                            break
                                    else:
                                        # Fallback to older card-style detection if needed
                                        cards = await page.query_selector_all(".fp-container")
                                        if cards:
                                            print("✅ Found RentCafe card-style floorplans")
                                            floorplans = await extract_card_style_floorplans(page)
                                            if floorplans:
                                                break
                                except:
                                    print(f"⚠️ No RentCafe floorplans detected (attempt {attempt+1}/3)")
                                    await page.wait_for_timeout(2000)
                    else:
                        print(f"ℹ️ No external site link found at {url}")

    except Exception as e:
        print(f"❌ Error scraping {url}: {e}")

    finally:
        await context.close()
        await pw.stop()
            # await browser.close()

    return floorplans

def flatten_properties(enriched, csv_file):
    """
    Flatten enriched properties into CSV format.
    One row per floorplan. Communities with no floorplans still get a row.
    """
    with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        # header row
        writer.writerow([
            "community_name",
            "address",
            "city",
            "state",
            "url",
            "bedrooms",
            "base_price",
            "phone",
            "fp_name",
            "fp_beds",
            "fp_baths",
            "sqft",
            "fp_price"
        ])

        for comm in enriched:
            name = comm.get("name")
            address = comm.get("address")
            city = comm.get("city")
            state = comm.get("state")
            url = comm.get("url")
            bedrooms = comm.get("bedrooms")
            base_price = comm.get("base_price")
            phone = comm.get("phone")
            floorplans = comm.get("floorplans", [])

            if floorplans:
                for fp in floorplans:
                    writer.writerow([
                        name,
                        address,
                        city,
                        state,
                        url,
                        bedrooms,
                        base_price,
                        phone,
                        fp.get("fp_name"),
                        fp.get("beds"),
                        fp.get("baths"),
                        fp.get("sqft"),
                        fp.get("price"),
                    ])
            else:
                # blank row to preserve community
                writer.writerow([name, address, city, state, url, bedrooms, base_price, phone, None, None, None, None, None])

# ---------- DRIVER ----------
async def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(script_dir, "lincoln_data")
    os.makedirs(data_dir, exist_ok=True)

    input_file = os.path.join(data_dir, "deduped_communities.json")
    output_file = os.path.join(data_dir, "communities_with_floorplans.json")
    csv_file = os.path.join(data_dir, "lincoln_properties.csv")

    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    communities = data.get("communities", data)
    EXCLUDED_COMMUNITIES = {
            "Chase Landing","Paddock Estates","Tempo Cambridge","Trilogy Chapel Hill","Brookside 17",
            "Ritterhouse Row","Meeting Street Lofts","Overlook Point","Simmons Park",
            "Summer Wind","1200 Broadway","Chapel Hill","Viero Twelve Oaks"
        }
    INCLUDED_COMMUNITIES = {
        "Legacy at Manchester Village","Reserve at Park West"
    }
    communities = [
        comm for comm in communities
        if comm.get("name") in INCLUDED_COMMUNITIES
    ]

    n = len(communities)

    i =1
    
    enriched = []
    for comm in communities:
        url = comm.get("url")
        if not url:
            continue
        print(f"\n🏢 [{i}/{n}] Scraping floorplans for {comm['name']} ({url}) ...")
        floorplans = await scrape_floorplans(url)
        comm["floorplans"] = floorplans
        enriched.append(comm)
        i += 1
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(enriched, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Done. Saved {output_file}")

    flatten_properties(enriched, csv_file)

    print(f"\n CSV saved to {csv_file}")

if __name__ == "__main__":
    asyncio.run(main())
