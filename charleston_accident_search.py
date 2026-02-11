import os
import re
import sqlite3
import requests
from typing import List, Dict, Any
import sys

from newspaper import Article
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from readability import Document
from datetime import datetime, timedelta

# Extractive summarizer (TextRank)
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer


# =========================
# CONFIG
# =========================

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "36e8e572d11c710ac3d3b4b384b2861171961111647fe4eb5da22a6570bc8ef9")
DB_PATH = "accidents.db"
OLLAMA_MODEL = "llama3.2:1b"   # FAST + LIGHTWEIGHT


# =========================
# VIDEO FILTER
# =========================

def is_video_url(url: str) -> bool:
    if not url:
        return False

    url = url.lower()

    video_domains = [
        "youtube.com", "youtu.be",
        "vimeo.com",
        "facebook.com/watch",
        "tiktok.com",
        "dailymotion.com",
    ]

    video_indicators = [
        "/video/",
        "/videos/",
        "videoplayer",
        "live-video",
        "videoId=",
    ]

    if any(domain in url for domain in video_domains):
        return True

    if any(ind in url for ind in video_indicators):
        return True

    return False

# =========================
# Year Filter
# =========================

def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z",""))
    except:
        pass
    m = re.match(r"(\d+)\s+(hour|hours|day|days)\s+ago", date_str, re.I)
    if m:
        num = int(m.group(1))
        unit = m.group(2).lower()
        now = datetime.now()
        if "hour" in unit:
            return now - timedelta(hours=num)
        if "day" in unit:
            return now - timedelta(days=num)
    try:
        cleaned = date_str.split(", +")[0]
        return datetime.strptime(cleaned,"%m/%d/%Y, %I:%M %p")
    except:
        pass

    return None
    
def is_current_year(dt):
    if not dt:
        return False
    now = datetime.now()
    return dt.year == now.year

# =========================
# DB SETUP
# =========================

def get_db_connection(path: str = DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT,
            source TEXT,
            published TEXT,
            location TEXT,
            date_time TEXT,
            vehicles_involved TEXT,
            injuries TEXT,
            fatalities TEXT,
            agencies TEXT,
            cause TEXT,
            summary TEXT,
            article_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    return conn


def save_accident(conn, data: Dict[str, Any]):
    with conn:
        conn.execute("""
            INSERT OR IGNORE INTO accidents
            (url, title, source, published, location, date_time,
             vehicles_involved, injuries, fatalities, agencies,
             cause, summary, article_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("url"),
            data.get("title"),
            data.get("source"),
            data.get("published"),
            data.get("location"),
            data.get("date_time"),
            data.get("vehicles_involved"),
            data.get("injuries"),
            data.get("fatalities"),
            data.get("agencies"),
            data.get("cause"),
            data.get("summary"),
            data.get("article_text"),
        ))


# =========================
# SCORING
# =========================

CHARLESTON_LOCATIONS = [
    "charleston", "north charleston", "mt pleasant", "mount pleasant",
    "summerville", "ladson", "goose creek", "west ashley",
    "james island", "johns island", "folly beach", "isle of palms",
    "sullivan's island", "hanahan", "moncks corner", "lowcountry",
    "i-26", "i 26", "i-526", "i 526", "highway 17", "hwy 17",
    "dorchester county", "berkeley county", "charleston county"
]

ACCIDENT_KEYWORDS = [
    "crash", "accident", "collision", "wreck", "fatal", "injured",
    "injury", "hit-and-run", "rollover", "troopers", "coroner",
    "fire rescue", "ems", "highway patrol", "sheriff"
]

CHARLESTON_SOURCES = [
    "abcnews4.com", "live5news.com", "counton2.com",
    "postandcourier.com", "wspa.com", "wbtw.com", "wmbfnews.com",
    "foxcarolina.com", "wach.com", "wltx.com", "wyff4.com", "thestate.com"
]


def score_location(item):
    text = f"{item.get('title', '')} {item.get('source', {}).get('name', '')}".lower()
    return sum(1 for loc in CHARLESTON_LOCATIONS if loc in text)


def score_accident(item):
    title = item.get("title", "").lower()
    return sum(1 for kw in ACCIDENT_KEYWORDS if kw in title)


def score_source(item):
    url = item.get("link", "").lower()
    return 2 if any(domain in url for domain in CHARLESTON_SOURCES) else 0


def score_item(item):
    return (
        score_location(item) * 3 +
        score_accident(item) * 2 +
        score_source(item)
    )


# =========================
# SERPAPI SEARCH
# =========================

def serpapi_google_news_search(query: str) -> List[Dict[str, Any]]:
    print(f"\n>>> Running query: {query}")

    params = {
        "engine": "google_news",
        "q": query,
        "api_key": SERPAPI_KEY
    }

    try:
        response = requests.get("https://serpapi.com/search.json", params=params, timeout=10)
        data = response.json()
    except Exception as e:
        print(f"[SerpAPI] Error fetching results: {e}")
        return []

    raw_results = data.get("news_results", [])

    scored = []
    for item in raw_results:
        url = item.get("link", "")

        # Skip video results
        if is_video_url(url):
            continue

        s = score_item(item)
        if s > 0:
            scored.append((s, item))

    scored.sort(reverse=True, key=lambda x: x[0])
    top_items = [item for score, item in scored[:10]]

    normalized = []
    for item in top_items:
        published_raw = item.get("date") or item.get("iso_date")
        # print("RAW DATE:", published_raw)
        published_dt = parse_date(published_raw)
        if not is_current_year(published_dt):
            continue

        
        normalized.append({
            "title": item.get("title"),
            "url": item.get("link"),
            "source": item.get("source", {}).get("name"),
            "published": item.get("date") or item.get("iso_date"),
            "raw": item
        })

    return normalized


# =========================
# ARTICLE EXTRACTION
# =========================

def extract_with_playwright(url):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"]
            )

            page = browser.new_page()

            # DO NOT wait for networkidle on CountOn2
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            selectors = [
                "div.article-content.article-body.rich-text",  # CountOn2
                "article",
                "div.entry-content",
                "div.article-body",
                "div.post-content",
                "div#article-body",
                "section.article-content",
            ]

            for sel in selectors:
                try:
                    # Wait for container to appear
                    page.wait_for_selector(sel, timeout=8000)

                    # Wait for text to actually load
                    page.wait_for_function(
                        """(sel) => {
                            const el = document.querySelector(sel);
                            return el && el.innerText.trim().length > 50;
                        }""",
                        sel,
                        timeout=10000
                    )

                    content = page.locator(sel).inner_text()
                    if content and len(content.strip()) > 50:
                        browser.close()
                        return content.strip()

                except Exception:
                    pass

            browser.close()
            return ""

    except Exception as e:
        print(f"[Playwright] Error extracting JS-rendered page: {e}")
        return ""


    except Exception as e:
        print(f"[Playwright] Error extracting JS-rendered page: {e}")
        return ""
    
def fetch_article_text(url: str) -> str:
    # Newspaper3k
    try:
        article = Article(url)
        article.download()
        article.parse()
        if len(article.text.strip()) > 200:
            return article.text.strip()
    except:
        pass

    # Readability fallback
    try:
        html = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).text
        doc = Document(html)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        text = soup.get_text(separator="\n").strip()
        if len(text) > 200:
            return text
    except:
        pass
    
    # Playwright fallback
    text = extract_with_playwright(url)
    if text:
        if len(text.strip()) > 200:
            return text 
    else:
        pass

    return ""


# =========================
# LOCAL LLM (OPTIMIZED)
# =========================

def local_llm(prompt: str) -> str:
    try:
        r = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=180,
        )
        raw = r.text.strip()

        # Parse NDJSON safely
        import json
        objs = []
        for line in raw.splitlines():
            try:
                objs.append(json.loads(line))
            except:
                pass

        if not objs:
            return ""

        return objs[-1].get("response", "").strip()

    except Exception as e:
        print(f"[Error] Ollama call failed: {e}")
        return ""


# =========================
# HYBRID SUMMARIZATION
# =========================

def extractive_summary(text, sentences=4):
    parser = PlaintextParser.from_string(text, Tokenizer("english"))
    summarizer = TextRankSummarizer()
    summary = summarizer(parser.document, sentences)
    return " ".join(str(s) for s in summary)


def summarize_article(text: str) -> str:
    # Trim article to reduce load
    trimmed = text[:1500]

    # Extractive first
    base = extractive_summary(trimmed, sentences=4)

    # LLM polish
    prompt = f"""
Rewrite the following extractive summary into a clear, concise 3–5 sentence accident report:

{base}
"""
    return local_llm(prompt)


# =========================
# STRUCTURED EXTRACTION
# =========================

def extract_accident_details(text: str) -> Dict[str, Any]:
    data = {
        "location": None,
        "date_time": None,
        "vehicles_involved": None,
        "injuries": None,
        "fatalities": None,
        "agencies": None,
        "cause": None,
        "summary": None,
    }

    # Simple regex extraction
    loc = re.search(r"(Charleston|North Charleston|Summerville|Ladson|Goose Creek|Mount Pleasant|Moncks Corner)", text, re.I)
    if loc:
        data["location"] = loc.group(1)

    dt = re.search(r"(\d{1,2}:\d{2}\s?(AM|PM)|morning|afternoon|evening|night)", text, re.I)
    if dt:
        data["date_time"] = dt.group(1)

    veh = re.search(r"(\d+ vehicles?|two cars|multiple vehicles|a (car|truck|SUV|motorcycle))", text, re.I)
    if veh:
        data["vehicles_involved"] = veh.group(1)

    inj = re.search(
        r"(?:\b\d+\s+(?:people|person)\s+injured\b"
        r"|injured\b"
        r"|suffered\s+injur(?:y|ies)"
        r"|non[-\s]?life[-\s]?threatening\s+injur(?:y|ies)"
        r"|serious\s+injur(?:y|ies)"
        r"|minor\s+injur(?:y|ies)"
        r"|injur(?:y|ies)\s+were\s+reported"
        r"|taken\s+to\s+(?:the\s+)?hospital"
        r"|transported\s+to\s+(?:a\s+)?(?:medical\s+center|hospital)"
        r"|treated\s+on\s+scene)",
        text,
        re.I
    )
    if inj:
        data["injuries"] = inj.group(0)
    else:
        data["injuries"] = None

    fat = re.search(
        r"(?:\b\d+\s+(?:people|person|victim|man|men|woman|women|driver|passenger|motorcyclist)\s+killed\b"
        r"|killing\s+(?:the\s+)?(?:driver|passenger|occupant|sole\s+occupant)"
        r"|died(?:\s+at\s+the\s+scene)?"
        r"|pronounced\s+dead"
        r"|fatal\s+injur(?:y|ies))"
        r"|fatalit(?:y|ies)",
        text,
        re.I
    )
    if fat:
        data["fatalities"] = fat.group(0)
    else:
        data["fatalities"] = None

    ag = re.search(r"(Highway Patrol|SCHP|Sheriff|Fire Rescue|EMS|Coroner|Police Department)", text, re.I)
    if ag:
        data["agencies"] = ag.group(1)

    cause = re.search(r"(?:caused by|due to|after)\s([^\.]+)", text, re.I)
    if cause:
        data["cause"] = cause.group(1).strip()

    # Hybrid summary
    data["summary"] = summarize_article(text)

    return data


# =========================
# MAIN PIPELINE
# =========================

def run_pipeline():
    conn = get_db_connection()

    # Warm up LLM once
    print("Warming up LLM...")
    local_llm("Say 'ready' when loaded.")
    print("LLM ready.\n")

    queries = [
        "car crash Charleston SC",
        "auto accident Charleston County",
        "traffic accident North Charleston",
    ]

    for q in queries:
        results = serpapi_google_news_search(q)

        for r in results:
            url = r.get("url")
            if not url:
                continue
            else:
                print(f"{url}")
            # Skip duplicates
            exists = conn.execute("SELECT 1 FROM accidents WHERE url = ?", (url,)).fetchone()
            if exists:
                print(f"Skipping existing: {url}")
                continue

            print(f"\nProcessing: {r.get('title')}")

            article_text = fetch_article_text(url)
            if not article_text:
                print("No article text extracted.")
                continue

            extracted = extract_accident_details(article_text)

            record = {
                "url": url,
                "title": r.get("title"),
                "source": r.get("source"),
                "published": r.get("published"),
                "location": extracted.get("location"),
                "date_time": extracted.get("date_time"),
                "vehicles_involved": extracted.get("vehicles_involved"),
                "injuries": extracted.get("injuries"),
                "fatalities": extracted.get("fatalities"),
                "agencies": extracted.get("agencies"),
                "cause": extracted.get("cause"),
                "summary": extracted.get("summary"),
                "article_text": article_text,
            }

            save_accident(conn, record)
            print("Saved to SQLite.")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run_pipeline()
