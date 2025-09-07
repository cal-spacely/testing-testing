import httpx
import uuid
import csv
import os
import json
from playwright.async_api import async_playwright
import asyncio
import random
import re

target_dir = os.path.dirname(os.path.abspath(__file__))

os.chdir(target_dir)
data_dir = os.path.join(target_dir,"greystar_data")
json_path=os.path.join(data_dir, "us_states.json")

with open(json_path,"r", encoding="utf-8") as f:
    us_states = json.load(f)

async def get_fresh_token():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        page = await context.new_page()
        await page.set_viewport_size({"width":1280, "height":720})

        # Patch navigator.webdriver
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        await page.add_init_script("""
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)

        await page.wait_for_timeout(2500 + random.randint(0, 1000))

        token = None

        async def handle_request(request):
            if "productSuggest" in request.url:
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    nonlocal token
                    token = auth

        page.on("request", handle_request)
        await page.goto("https://www.greystar.com")
        await page.wait_for_timeout(3000)

        # Simulate typing to trigger productSuggest
        search_input = await page.query_selector("input[id*='search']")
        if search_input:
            await search_input.fill("South Carolina")
            await page.wait_for_timeout(3000)

        await browser.close()
        return token

def slugify(text):
    text = text.lower()
    text = re.sub(r"[^\w\s-]","", text) # remove punctuation
    text = re.sub(r"\s+", "-",text)  # replace spaces with hyphens
    return text.strip("-")

def construct_seo_url(item):
    name = slugify(item.get("ec_brand",""))

    fields = item.get("additionalFields", {})
    city = slugify(fields.get("city", "").strip())
    state = slugify(fields.get("state", "").strip())
    uri = item.get("clickUri", "")
    if uri.startswith("property://"):
        property_id = uri.split("property://")[1].split("/")[0]
    else:
        property_id = "unknown"
    return f"https://www.greystar.com/{name}-{city}-{state}/p_{property_id}"

async def get_property_address(page):
    try:
        await page.wait_for_selector("div.address-bar p.address", timeout=5000)
        address_el = await page.query_selector("div.address-bar p.address")
        address = await address_el.inner_text() if address_el else "Unknown"
        return address.strip()
    except Exception as e:
        print(f"Failed to extract address: {e}")
        return "Unknown"

def write_to_csv(results, filename="greystar_suggestions.csv"):
    with open(filename, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["Name", "URL", "City", "State"])

        for item in results:
            name = item.get("ec_brand") or "Unnamed Property"
            uri = item.get("clickUri", "")
            city = item.get("additionalFields", {}).get("city", "").strip()
            state = item.get("additionalFields", {}).get("state", "").strip()

            if uri.startswith("property://"):
                property_id = uri.split("property://")[1].split("/")[0]
                url = f"https://www.greystar.com/properties/{property_id}"
            else:
                url = uri

            writer.writerow([name, url, city, state])

def write_deduped_to_csv(results, filename="greystar_deduped_with_address.csv"):
    fieldnames = ["ec_brand", "address", "city", "state", "url"]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for item in results:
            writer.writerow({
                "ec_brand": item.get("ec_brand", ""),
                "address": item.get("address", ""),
                "city": item.get("additionalFields", {}).get("city", "").strip(),
                "state": item.get("additionalFields", {}).get("state", "").strip(),
                "url": item.get("url", "")
            })

def fetch_product_suggestions(token):
    url = "https://greystarproduction117hu38yh.org.coveo.com/rest/organizations/greystarproduction117hu38yh/commerce/v2/search/productSuggest"

    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Referer": "https://www.greystar.com",
        "Origin": "https://www.greystar.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1"
    }

    payload = {
        "trackingId": "greystar_com_tracking_id",
        "clientId": str(uuid.uuid4()),
        "query": "south carolina OR north carolina OR carolina",
        "country": "US",
        "currency": "USD",
        "language": "en",
        "context": {
            "user": {},
            "view": {"url": "https://www.greystar.com/"},
            "capture": True,
            "cart": []
        }
    }

    with httpx.Client() as client:
        response = client.post(url, headers=headers, json=payload)
        print("Raw response:")
        print(response.text[:1000])  # Preview first 1000 chars
        response.raise_for_status()
        return response.json()

async def enrich_with_address(results):
    enriched = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        for item in results:
            url = construct_seo_url(item)

            try:
                await page.goto(url)
                address = await get_property_address(page)
            except Exception as e:
                print(f"Failed to scrape address for {url}: {e}")
                address = "Unknown"
            
            item["url"] = url
            item["address"] = address.strip()
            enriched.append(item)
        await browser.close()
    return enriched

def dedupe_properties(results):
    seen = set()
    deduped = []

    for item in results:
        name = item.get("ec_brand", "").strip().lower()
        address = item.get("address", "").strip().lower()
        key = (name, address)

        if key not in seen:
            seen.add(key)
            deduped.append(item)
    
    return deduped

def export_scraper_input(deduped, data_dir):
    
    scraper_input = []
    for item in deduped:
        brand = item.get("ec_brand", "").strip()
        fields = item.get("additionalFields", {})
        city = fields.get("city", "").strip()
        state = fields.get("state", "").strip()
        uri = item.get("clickUri", "")

        if uri.startswith("property://"):
            property_id = uri.split("property://")[1].split("/")[0]
        else:
            property_id = "unknown"
        
        url = construct_seo_url(item)
        click_uri = f"property://{property_id}/"
        scraper_input.append({
            "ec_brand": brand,
            "city": city,
            "state": state,
            "property_id": property_id,
            "url": url,
            "clickUri": click_uri
        })
    
    # Sort by state before exporting to json

    scraper_input.sort(key=lambda x: (x.get("state", ""), x.get("city", ""),x.get("ec_brand", "")))
    
    json_path_1 = os.path.join(data_dir, "greystar_scraper_input.json")
    with open(json_path_1, "w", encoding="utf-8") as f:
        json.dump(scraper_input, f, indent=2, ensure_ascii=False)

    print(f"Saved {len(scraper_input)} entries to greystar_scraper_input.json")

async def main():
    all_results = []

    for state in us_states:
        print(f"Searching: {state}")

        # Refresh token for each state
        token = await get_fresh_token()
        if not token:
            print(f"Failed to get token for {state}")
            continue

        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
            "Referer": "https://www.greystar.com",
            "Origin": "https://www.greystar.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1"
        }

        payload = {
            "trackingId": "greystar_com_tracking_id",
            "clientId": str(uuid.uuid4()),
            "query": state,
            "country": "US",
            "currency": "USD",
            "language": "en",
            "context": {
                "user": {},
                "view": {"url": "https://www.greystar.com/"},
                "capture": True,
                "cart": []
            }
        }

        try:
            with httpx.Client() as client:
                response = client.post(
                    "https://greystarproduction117hu38yh.org.coveo.com/rest/organizations/greystarproduction117hu38yh/commerce/v2/search/productSuggest",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("products", [])
                print(f"{len(results)} properties found for {state}")
                all_results.extend(results)
        except Exception as e:
            print(f"Error fetching {state}: {e}")

    print(f"\nTotal properties discovered: {len(all_results)}")
    write_to_csv(all_results, filename="greystar_discovery.csv")
    print("Saved results to greystar_discovery.csv")
    print("...Deduplicating results...")
    enriched_results = await enrich_with_address(all_results)
    deduped_results = dedupe_properties(enriched_results)
    export_scraper_input(deduped_results, data_dir)
    write_deduped_to_csv(deduped_results, filename="greystar_deduped.csv")
    print("Deduped listing saved to greystar_deduped.csv")
    
if __name__ == "__main__":

    asyncio.run(main())


