from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import csv
import random
import pandas as pd
import re
import os

target_dir = r"C:\users\warre\Documents\Jordans_Crawler"
os.chdir(target_dir)

# --- Setup ---
options = webdriver.ChromeOptions()
options.add_argument("--headless=new")
options.add_argument("--disable-gpu")
options.add_argument("--enable-unsafe-swiftshader")
options.add_argument("--window-size=1920,1080")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--log-level=3")
options.add_argument("--use-gl=swiftshader")
options.add_argument("--disable-webgl")
options.add_argument("--disable-features=UseDawn")

driver = webdriver.Chrome(service=Service(), options=options)

# --- DataFrame Setup ---
columns = ['name','address']
listings_df = pd.DataFrame(columns=columns)

# --- Noise phrases to filter ---
noise_phrases = [
    "Brand New", "Virtual Visit", "New Listing",
    "Contact the Community for more", "information on price and availability",
    "Move In Specials", "Now Leasing", "Location", "Specials", "Flexible Tour Options", 
    "Active Adult", "Lux Living", "Renovated"
]

def extract_city_state(address):
    # Matches: "Street Address, City, ST ZIP" or "Street Address, City, ST ZIP+4"
    match = re.match(r'^.+?,\s*([^,]+),\s*([A-Z]{2})\s*\d{5}(?:-\d{4})?$', address)
    if match:
        city = match.group(1).strip()
        state = match.group(2)
        return city, state
    return "", ""

def clean_lines(lines):
    return [line for line in lines if all(phrase.lower() not in line.lower() for phrase in noise_phrases)]

def get_city_urls(driver):
    driver.get("https://www.greystar.com/apartments")
    WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.TAG_NAME, "a")))
    links = driver.find_elements(By.TAG_NAME, "a")
    city_urls = []

    for link in links:
        href = link.get_attribute("href")
        if href and "/s/" in href and href.startswith("https://www.greystar.com/s/"):
            city_urls.append(href)

    return list(set(city_urls))  # Remove duplicates

def scrape_city(driver, url):
    print(f"\n🌆 Scraping: {url}")
    driver.get(url)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CLASS_NAME, "property-card"))
        )
        cards = driver.find_elements(By.CLASS_NAME, "property-card")
        print(f"✅ Found {len(cards)} property cards")

        for idx, card in enumerate(cards, start=1):
            raw = card.text.strip()
            lines = clean_lines(raw.split("\n"))

            name = lines[0] if len(lines) > 0 else "N/A"
            address = lines[1] if len(lines) > 1 else "N/A"
            price_summary = next((line for line in lines if "From$" in line), "N/A")
            base_rent = next((line for line in lines if "Base rent" in line), "N/A")

            print(f"{idx:02d}. {name} | {address} | {price_summary} | {base_rent}")
            city, state = extract_city_state(address)
            csv_writer.writerow([url, idx, name, address, price_summary,base_rent, city, state, raw])
            new_listing = {
                'name': name,
                'address': address
            }
            listings_df.loc[len(listings_df)] = new_listing

    except Exception as e:
        print(f"❌ Error scraping {url}: {e}")

output_file = open("greystar_listings.csv", mode="w", newline='',encoding="utf-8")
csv_writer = csv.writer(output_file)
csv_writer.writerow(["City URL", "Card Index", "Name","Address","Price Summary","Base Rent","City","State","Raw Text"])

# --- Main Loop ---
city_urls = get_city_urls(driver)
print(f"\n🌍 Found {len(city_urls)} cities")

for city_url in city_urls:
    delay = random.uniform(1.5, 4)
    scrape_city(driver, city_url)
    time.sleep(delay)

listings_df.drop_duplicates(subset=['name', 'address'],inplace=True)
print("\n Final deduplicated listings:")
print(listings_df)
listings_df.to_csv('deduplicated_listings.csv',index=False)


# --- Cleanup ---
driver.quit()
output_file.close()
