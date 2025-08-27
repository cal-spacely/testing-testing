import asyncio
import json
import re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import os
from collections import defaultdict
from datetime import datetime

target_dir = r"C:\users\warre\Documents\Jordans_Crawler"
os.chdir(target_dir)

json_filename = "icon_map.json"
json_path = os.path.join(target_dir, json_filename)

with open(json_path, "r") as f:
    icon_map = json.load(f)

def identify_icon(path_d):
    for key, label in icon_map.items():
        if key in path_d:
            return label
    return None

def group_units_by_floorplan(units):
    grouped = defaultdict(list)

    for unit in units:
        label = unit.get("floorPlanLabel") or unit.get("floorplan", {}).get("label")
        grouped[label].append({
            "unit_number": unit["unitNumber"],
            "building": unit["buildingLabel"],
            "floor": unit["floorLabel"],
            "area": unit["area"],
            "bedrooms": unit["floorplan"]["bedroomCount"],
            "bathrooms": unit["floorplan"]["bathroomCount"],
            "available_on": unit["availableOn"],
            "base_price": unit["minBasePrice"],
            "price_with_fees": unit["minPriceMinFee"],
            "image": unit["floorplan"]["imageUrl"]
        })

    return grouped

# Stealth setup
async def apply_stealth(page):
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined })")
    # await page.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36")
    # await page.emulate_timezone("America/New_York")
    await page.set_extra_http_headers({ "Accept-Language": "en-US,en;q=0.9" })

PRICING_KEYS = {"rent","price","monthlyRate","startingFrom","floorPlans","units","availability","bedrooms"}
BLOCKED_DOMAINS =[
    "cdn.cookielaw.org",
    "static.matterport.com",
    "my.matterport.com",
    "api-v3.peek.us"
]

def scan_json_for_pricing(json_data, url):
    
    matches = []

    def recursive_search(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                key_path = f"{path}.{k}" if path else k
                if any(pk in k.lower() for pk in PRICING_KEYS):
                    matches.append((key_path, v))
                recursive_search(v, key_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                recursive_search(item, f"{path}[{i}]")

    recursive_search(json_data)

    if matches:
        print(f"\n🔍 Pricing matches found in: {url}")
        #for path, value in matches:
        #   print(f"  {path}: {value}")

        # Optional: write to file
        try:
            with open("pricing_matches.txt", "w", encoding="utf-8") as f:
                print("Writing pricing_matches.txt...")
                f.write(f"\nURL: {url}\n")
                for path, value in matches:
                    f.write(f"{path}: {value}\n")
        except Exception as e:
            print("X File write error:",e)


# Try extracting from __NEXT_DATA__
async def extract_next_data(page):
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if script_tag:
        try:
            return json.loads(script_tag.string)
        except Exception as e:
            print(f"❌ Failed to parse _NEXT_DATA_: {e}")
    return None

# Sniff for API responses
async def sniff_api(page):

    api_data = {}


    async def handle_response(response):
        try:
            url = response.url
            if any(domain in url for domain in BLOCKED_DOMAINS):
                return 
            
            if "application/json" in response.headers.get("content-type", ""):
                url = response.url
                json_data = await response.json()
                api_data[url] = json_data 
                scan_json_for_pricing(json_data, url) 
        except:
            pass

    page.on("response", handle_response)
    await page.wait_for_timeout(3000)
    return api_data

#   Generate floorplan details
async def extract_floorplan_details(plan):
    try:
        name = await plan.locator(".floorplan--heading-container h3").text_content()
        range_ = await plan.locator(".floorplan--tmlp").text_content()
        base = await plan.locator(".floorplan--price").text_content()

        # Square Footage
        sqft = await plan.locator(".floorplan--area").text_content()

        # Bedrooms

        info_blocks = await plan.locator(".icon-text.icon-text--left.icon-text--text-secondary.icon-text--icon-primary").all()

        bedroom_count = None

        for block in info_blocks:
            path = await block.locator("path").get_attribute("d")
            label = identify_icon(path) if path else None
            if label == "bedrooms":
                bedroom_count = await block.locator("span.icon-text--text").text_content()
                break  # Stop once we find the bedroom count

        return {
            "name": name.strip(),
            "sqft": sqft.strip(),
            "bedrooms": bedroom_count.strip() if bedroom_count else None
        }

    except Exception as e:
        print(f"Failed to extract floorplan details: {e}")
        return {}
# Fallback: DOM parsing
async def parse_dom(page):
    try:
        # Address
        address = await page.locator(".address-bar .address").text_content()
        print("Address:", address.strip())

        # Floor Plan Details
        floorplans = await page.locator(".floorplan").all()
        pricing_info = []

        for plan in floorplans:
            name = await plan.locator(".floorplan--heading-container h3").text_content()
            tmlp = await plan.locator(".floorplan--tmlp").text_content()
            base = await plan.locator(".floorplan--price").text_content()

            details = await extract_floorplan_details(plan)

            pricing_info.append({
                "name": name.strip(),
                "range": tmlp.strip(),
                "base": base.strip(),
                "sqft": details.get("sqft"),
                "bedrooms": details.get("bedrooms")
            })

        print("Floor Plans:")
        for fp in pricing_info:
            print(f"  {fp['name']}: {fp['range']} | {fp['base']} | {fp['sqft']} | {fp['bedrooms']}")

        # Optional: write to file
        with open("dom_pricing.txt", "w", encoding="utf-8") as f:
            f.write(f"Address: {address.strip()}\n\n")
            f.write("Floor Plans:\n")
            for fp in pricing_info:
                f.write(f"{fp['name']}: {fp['range']} | {fp['base']} | {fp['sqft']} | {fp['bedrooms']}\n")

        return {
            "address": address.strip(),
            "floor_plans": pricing_info
        }

    except Exception as e:
        print(f"DOM parsing failed: {e}")
        return {}


#  Optional: dump HTML for offline inspection
async def dump_html(page, filename="debug.html"):
    html = await page.content()
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)

#  Unified fallback wrapper
# async def extract_property_data(page):
#    data = {}
#
#    # Extract from __NEXT_DATA__
#    next_data = await extract_next_data(page)
#    if next_data:
#        print("✅ Extracted from _NEXT_DATA_")
#        data["next_data"] = next_data
#        scan_json_for_pricing(next_data, "NEXT_DATA")
#
#        try:
#            property_data = next_data["props"]["pageProps"]["layoutData"]["sitecore"]["context"]["property"]
#            property_name = property_data.get("name", "Unnamed Property")
#            units = property_data.get("availableUnits", [])
#
#            grouped_units = group_units_by_floorplan(units)
#            output = {property_name: grouped_units}
#
#            with open("grouped_units.json", "w", encoding="utf-8") as f:
#                json.dump(output, f, indent=2)
#
#        except Exception as e:
#           print(f"❌ Failed to group units: {e}")
async def extract_property_data(page):
    try:
        next_data = await extract_next_data(page)
        scan_json_for_pricing(next_data, "NEXT_DATA")

        prop = next_data["props"]["pageProps"]["layoutData"]["sitecore"]["context"]["property"]
        name = prop.get("name", "Unnamed Property")
        units = prop.get("availableUnits", [])

        grouped = group_units_by_floorplan(units)
        return name, grouped

    except Exception as e:
        print(f"❌ Failed to extract property data: {e}")
        return None, None
    
    # Sniff for API responses
    api_data = await sniff_api(page)
    if api_data:
        print("✅ Extracted from API")
        data["api_data"] = api_data

    # Fallback: DOM parsing
    dom_data = await parse_dom(page)
    if dom_data:
        print("✅ Extracted from DOM")
        data["dom_data"] = dom_data

    if not data:
        print("❌ No data found")

    return data

async def get_property_links_from_search(page, search_url):
    await page.goto(search_url, timeout=60000)
    await page.wait_for_timeout(2000)

    next_data = await extract_next_data(page)
    page_props = next_data.get("props", {}).get("pageProps", {})

    print("🔍 NEXT_DATA keys under pageProps:")
    for key in page_props:
        print(f" - {key}")

    # Define nested path to property listings
    path = ["layoutData", "sitecore", "context", "properties"]
    listings = page_props
    try:
        for key in path:
            listings = listings.get(key, {})
        if not isinstance(listings, list):
            raise ValueError("Expected a list of properties, got something else")

        links = [
            f"https://www.greystar.com{prop['url']}"
            for prop in listings
            if isinstance(prop, dict) and "url" in prop
        ]

        print(f"✅ Extracted {len(links)} property links")
        return links

    except Exception as e:
        print(f"❌ Failed to extract property links: {e}")
        return []

# Crawl all Charleston properties from search results
async def crawl_charleston_properties():
    grouped_by_property = {}

    search_url = "https://www.greystar.com/s/us?bb=eyJzdyI6eyJsYXQiOjMyLjY5NTYwNzcxODYzNDE4LCJsbmciOi04MC4xMTUxNTY1MTQ1NTAzOX0sIm5lIjp7ImxhdCI6MzIuOTQ3NDgxNTQyMTY4OTUsImxuZyI6LTc5Ljg0NDk2MTUwNzIyNjE3fSwiY2VudGVyTGF0IjozMi44MjE2MzM5MDA1ODE5NiwiY2VudGVyTG5nIjotNzkuOTgwMDU5MDEwODg4Mjh9&resetPagination=false"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # ✅ Now page is defined, so this works
        property_links = await get_property_links_from_search(page, search_url)

        for i, url in enumerate(property_links):
            print(f"\n[{i+1}/{len(property_links)}] Visiting: {url}")
            try:
                await page.goto(url, timeout=60000)
                await page.wait_for_timeout(2000)

                name, grouped = await extract_property_data(page)
                if name and grouped:
                    grouped_by_property[name] = grouped

            except Exception as e:
                print(f"❌ Error at {url}: {e}")

        await browser.close()

    # ✅ Write final JSON
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"grouped_units_{timestamp}.json"

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(grouped_by_property, f, indent=2)

    print(f"\n📦 Saved grouped units to {filename}")
    print(f"🗂️ Total properties processed: {len(grouped_by_property)}")
        

#  Main crawl logic
async def crawl_property(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="America/New_York"
        )
        page = await context.new_page()
        await apply_stealth(page)  # Now only sets headers

        print(f"🔗 Navigating to: {url}")
        await page.goto(url, timeout=60000)
        await page.wait_for_timeout(2000)

        data = await extract_property_data(page)
        # print(f"📦 Extracted data: {data}")

        await browser.close()

#  Entry point
if __name__ == "__main__":
    mode = "charleston"  # Set to "single" or "charleston"

    if mode == "single":
        test_url = "https://www.greystar.com/1000-west-apartments-charleston-sc/p_11000"
        asyncio.run(crawl_property(test_url))
    elif mode == "charleston":
        asyncio.run(crawl_charleston_properties())