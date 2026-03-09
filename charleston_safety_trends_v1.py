import os
import json
import logging
import asyncio
import aiohttp
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from serpapi import GoogleSearch
import requests
from newspaper import Article
import re 
import ast

# ------------------------------------------------------------
# LOAD CREDENTIALS
# ------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(SCRIPT_DIR, "Json_Resources", "cred.json")

with open(JSON_PATH, "r") as f:
    conf = json.load(f)

# -----------------------------
# CONFIGURATION
# -----------------------------

GNEWS_API_KEY = conf["GNEWS_API_KEY"]
GEMINI_API_KEY = conf["GEMINI_API_KEY"]
EMAIL_FROM = conf["FROM_EMAIL"]
EMAIL_TO = conf["TO_EMAIL"]
EMAIL_PASSWORD = conf["APP_PASSWORD"]
SERPAPI_KEY = conf["SERPAPI_KEY"]

# Toggle search backend
USE_SERPAPI = True  # Set True when SerpAPI quota resets

# Toggle extraction backendc
USE_GEMINI = True   # True = Gemini, False = llama-server

# Safe concurrency for llama-server
MAX_CONCURRENCY = 3

# GNews max results (Option C)
GNEWS_MAX_RESULTS = conf.get("GNEWS_MAX_RESULTS", 50)

# ------------------------------------------------------------
# DOMAIN FILTERING
# ------------------------------------------------------------

ALLOWED_DOMAINS = [
    "live5news.com",
    "abcnews4.com",
    "counton2.com",
    "postandcourier.com",
    "thestate.com",
]

DOMAIN_FILTER = ",".join(ALLOWED_DOMAINS)

# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------

logging.basicConfig(
    filename="extractor.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log_response(label, data):
    logging.info(f"{label}: {json.dumps(data, indent=2)}")

# ------------------------------------------------------------
# DATE RANGE (last 30 days)
# ------------------------------------------------------------

def get_from_date():
    today_utc = datetime.now(timezone.utc)
    return (today_utc - timedelta(days=30)).strftime("%Y-%m-%d")

# ------------------------------------------------------------
# GNEWS FETCH FUNCTION (your exact version)
# ------------------------------------------------------------

def fetch_gnews_articles():
    """Fetch Charleston accident-related news using GNews.io."""
    
    query = (
        '("Charleston" OR "North Charleston" OR "Mount Pleasant" OR "Summerville" OR "Goose Creek") '
        '("crash" OR "collision" OR "wreck")'
    )

    today_utc = datetime.now(timezone.utc)
    from_date = (today_utc - timedelta(days=30)).strftime("%Y-%m-%d")

    url = "https://gnews.io/api/v4/search"

    params = {
        "q": query,
        "lang": "en",
        "country": "us",
        "from": from_date,
        "in": DOMAIN_FILTER,
        "max": 50,
        "apikey": GNEWS_API_KEY
    }

    logging.info("Running GNews.io request...")
    logging.info(f"Query: {query}")
    logging.info(f"Params: {json.dumps(params, indent=2)}")

    response = requests.get(url, params=params)
    data = response.json()

    log_response("Raw GNews.io response", data)

    raw_articles = data.get("articles", [])
    normalized = []

    for item in raw_articles:
        normalized.append({
            "title": item.get("title"),
            "description": item.get("description"),
            "url": item.get("url"),
            "content": item.get("content"),
            "published": item.get("publishedAt"),   # <-- correct field
            "source": item.get("source", {}).get("name")
        })

    return normalized

# ------------------------------------------------------------
# SERPAPI FETCH FUNCTION (Option A — same query)
# ------------------------------------------------------------

def fetch_serpapi_articles():
    """Fetch Charleston accident-related news using SerpAPI."""

    query = (
        '("Charleston" OR "North Charleston" OR "Mount Pleasant" OR "Summerville" OR "Goose Creek") '
        '("crash" OR "collision" OR "wreck")'
    )

    from_date = datetime.now(timezone.utc) - timedelta(days=30)
    year, month, day = from_date.year, from_date.month, from_date.day

    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": 10,
        "hl": "en",
        "gl": "us",
        "as_ylo": year,
        "as_mlo": month,
        "as_dlo": day
    }

    logging.info("Running SerpAPI request...")
    logging.info(f"Query: {query}")
    logging.info(f"Params: {json.dumps(params, indent=2)}")

    search = GoogleSearch(params)
    results = search.get_dict()

    log_response("Raw SerpAPI response", results)

    normalized = []

    if "organic_results" in results:
        for item in results["organic_results"]:
            if "link" in item:
                normalized.append({
                    "title": item.get("title", ""),
                    "description": item.get("snippet", ""),     # closest match
                    "url": item.get("link"),
                    "content": item.get("snippet", ""),         # SerpAPI doesn't give full text
                    "published": item.get("date"),              # relative date like "2 days ago"
                    "source": item.get("source", "Unknown")
                })

    return normalized

# ------------------------------------------------------------
# NEWSPAPER 3K ARTICLE EXTRACTION
# ------------------------------------------------------------

async def fetch_article_text(url):
    """Download and parse article text using Newspaper3k in a thread."""
    loop = asyncio.get_event_loop()

    def _download():
        try:
            article = Article(url)
            article.download()
            article.parse()
            return article.text
        except Exception as e:
            logging.error(f"Newspaper3k failed for {url}: {e}")
            return ""

    return await loop.run_in_executor(None, _download)


# ------------------------------------------------------------
# GEMINI SANITIZER
# ------------------------------------------------------------

def sanitize_json(raw):
    """Extract and convert Gemini-style Python-dict output into valid JSON."""
    if not raw or not isinstance(raw, str):
        return raw

    # Remove backticks or markdown fences
    raw = raw.strip().strip("`")

    # Extract the first {...} block
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None  # No JSON-like structure found

    block = match.group(0)

    try:
        # Try interpreting it as a Python literal (Gemini often outputs Python dicts)
        data = ast.literal_eval(block)
    except Exception:
        # Fallback: attempt naive JSON cleanup
        block = block.replace("'", '"')
        block = re.sub(r",\s*}", "}", block)
        block = re.sub(r",\s*]", "]", block)
        try:
            data = json.loads(block)
        except Exception:
            return None  # Still invalid

    # Convert to strict JSON
    return json.dumps(data, ensure_ascii=False)

def extract_gemini_text(data):
    try:
        candidates = data.get("candidates", [])
        if not candidates:
            return ""

        parts = candidates[0].get("content", {}).get("parts", [])
        texts = [p.get("text", "") for p in parts if "text" in p]

        # Join all text parts (Gemini sometimes splits JSON across parts)
        return "\n".join(texts).strip()

    except Exception:
        return ""

# ------------------------------------------------------------
# GEMINI EXTRACTOR (async)
# ------------------------------------------------------------

async def gemini_extract(session, article):
    """Extract cause/location/summary using Gemini with safe fallbacks."""

    full_text = await fetch_article_text(article["url"])

    title = article.get("title", "")
    desc = article.get("description", "")
    url = article.get("url", "")

    prompt = f"""
Extract accident information from the article below.

Return ONLY valid JSON with this exact structure:
{{
  "cause": "...",
  "location": "...",
  "summary": "..."
}}

Rules:
- Use ONLY double quotes.
- No markdown.
- No backticks.
- No explanations.
- If cause is not explicitly stated, use "unknown".
- If location is not explicitly stated, use "unknown".

Article title: {title}
Description: {desc}
URL: {url}

Full article text:
{full_text}
"""

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash-lite:generateContent"
    )

    params = {"key": GEMINI_API_KEY}

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }

    try:
        async with session.post(api_url, params=params, json=payload) as resp:
            data = await resp.json()
            logging.error(f"GEMINI RAW OUTPUT for {url}: {data}")

        # Extract raw text
        raw = extract_gemini_text(data)
        # Try strict JSON first
        try:
            return json.loads(raw)
        except:
            pass

        # Try sanitized JSON
        try:
            fixed = sanitize_json(raw)
            return json.loads(fixed)
        except Exception as inner:
            logging.error(f"Gemini JSON parse error for {url}: {inner}")
            return {
                "cause": "unknown",
                "location": "unknown",
                "summary": raw
            }

    except Exception as e:
        logging.error(f"Gemini API error for {url}: {e}")
        return {
            "cause": "unknown",
            "location": "unknown",
            "summary": ""
        }

# ------------------------------------------------------------
# LLAMA EXTRACTOR (async)
# ------------------------------------------------------------

LLAMA_URL = "http://localhost:8080/v1/chat/completions"

async def llama_extract(session, article):
    # Fetch full article text using Newspaper3k
    full_text = await fetch_article_text(article["url"])

    payload = {
        "model": "Qwen2.5-14B-Instruct-GGUF",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You extract structured information from traffic‑related news articles. "
                    "Return ONLY valid JSON with the following fields:\n"
                    "  - cause: the primary cause of the crash. Allowed values:\n"
                    "           speeding, DUI, weather, mechanical failure, deer collision, medical event, unknown\n"
                    "  - location: the specific road, intersection, or area where the crash occurred\n"
                    "  - summary: a concise 1–2 sentence summary of the key facts\n\n"
                    "Rules:\n"
                    "- Use ONLY information explicitly stated in the article text.\n"
                    "- If cause is not explicitly stated, return \"unknown\".\n"
                    "- If location is not explicitly stated, return \"unknown\".\n"
                    "- Do NOT invent details.\n"
                    "- Do NOT use placeholders like [date] or [location].\n"
                    "- Do NOT include personal details unless they appear in the article.\n"
                    "- Keep the summary factual and avoid speculation.\n"
                )
            },
            {
                "role": "user",
                "content": (
                    f"Title: {article['title']}\n"
                    f"URL: {article['url']}\n\n"
                    f"Full article text:\n{full_text}"
                )
            }
        ]
    }

    async with session.post(LLAMA_URL, json=payload) as resp:
        data = await resp.json()

    return data["choices"][0]["message"]["content"]


# ------------------------------------------------------------
# ASYNC EXTRACTION WRAPPER
# ------------------------------------------------------------

async def extract_article(session, article, semaphore):
    async with semaphore:
        logging.info(f"Extracting: {article['title']} | {article['url']}")

        try:
            # Call the appropriate extractor
            if USE_GEMINI:
                raw = await gemini_extract(session, article)
            else:
                raw = await llama_extract(session, article)

            # Parse JSON if possible
            try:
                summary = json.loads(raw)
            except Exception:
                summary = {
                    "cause": "unknown",
                    "location": "unknown",
                    "summary": raw
                }

            return {
                "article": article,
                "summary": summary
            }

        except Exception as e:
            logging.error(f"Extraction failed for {article['url']}: {e}")
            return None


# ------------------------------------------------------------
# ASYNC RUNNER
# ------------------------------------------------------------

async def process_articles(articles):
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        tasks = [
            extract_article(session, article, semaphore)
            for article in articles
        ]
        return await asyncio.gather(*tasks)

# ------------------------------------------------------------
# EMAIL SENDER
# ------------------------------------------------------------

def send_email(subject, body):
    msg = MIMEText(body, "html")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)

def build_email_body(results):
    blocks = []

    for result in results:
        if result is None:
            continue

        article = result["article"]
        summary = result["summary"]

        # Gemini returns JSON; llama returns plain text
        if isinstance(summary, dict):
            cause = summary.get("cause", "N/A")
            location = summary.get("location", "N/A")
            summary_text = summary.get("summary", "N/A")
        else:
            cause = "N/A"
            location = "N/A"
            summary_text = summary

        block = f"""
        <div style="margin-bottom: 25px; padding-bottom: 15px; border-bottom: 1px solid #ccc;">
            <p><strong>Title:</strong> {article.get('title', 'N/A')}</p>
            <p><strong>Published:</strong> {article.get('published', 'N/A')}</p>
            <p><strong>Location:</strong> {location}</p>
            <p><strong>Cause:</strong> {cause}</p>
            <p><strong>URL:</strong> <a href="{article.get('url', '#')}">{article.get('url', 'N/A')}</a></p>
            <p><strong>Summary:</strong><br>{summary_text}</p>
        </div>
        """

        blocks.append(block)

    # Wrap everything in a simple HTML container
    html_email = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.5; font-size: 14px;">
        {''.join(blocks)}
    </body>
    </html>
    """

    return html_email

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():
    if USE_SERPAPI:
        articles = fetch_serpapi_articles()
    else:
        articles = fetch_gnews_articles()

    if not articles:
        logging.info("No articles found. Exiting.")
        return

    logging.info(f"Processing {len(articles)} articles asynchronously")

    results = asyncio.run(process_articles(articles))
    
    email_body = build_email_body(results)

    send_email("Charleston Safety Trends Summary", email_body)
    logging.info("Email sent.")
    print("Email sent")

# ------------------------------------------------------------
# ENTRY POINT
# ------------------------------------------------------------

if __name__ == "__main__":
    main()
