import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from google import genai
from google.genai import types
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart 
import ollama
import re 
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(SCRIPT_DIR,"Json_Resources","cred.json")
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


ALLOWED_DOMAINS = [
    "live5news.com",
    "abcnews4.com",
    "counton2.com",
    "postandcourier.com",
    "thestate.com",
    "greenvilleonline.com",
    "greenvillejournal.com",
    "wyff4.com",
    "foxcarolina.com",
    "wspa.com",
    "wgog.com",
    "wsnwradio.com",
    "wlos.com",
]

# Build domain filter for GNews
DOMAIN_FILTER = ",".join(ALLOWED_DOMAINS)

# Initialize Gemini client
client = genai.Client(api_key=GEMINI_API_KEY)

# -----------------------------
# LOGGING SETUP
# -----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="\n[%(levelname)s] %(message)s\n"
)

def log_response(label, data):
    logging.info(f"{label}:\n{json.dumps(data, indent=2)}")

# ------------------------------
#  HTML SELECTORS AND PATTERNS
#  -----------------------------

ARTICLE_SELECTORS = [
    "article",
    ".article-body",
    ".article-content",
    ".story-body",
    ".post-content",
    ".entry-content",
    ".content-body",
]    

BAD_PATTERNS = [
    "forecast", "windy", "thunderstorms", "copyright", 
    "advertisement", "ad choices", "cookie", "privacy policy",
]

def filtered_pattern(p):
    text = p.lower()
    return any(bad in text for bad in BAD_PATTERNS)

def remove_byline(paragraphs):
    if not paragraphs:
        return paragraphs

    first = paragraphs[0]

    # Ensure it's a string before processing
    if not isinstance(first, str):
        return paragraphs

    first_lower = first.strip().lower()

    BYLINE_KEYWORDS = [
        "by ",
        "post and courier",
        "live5news",
        "abc news",
        "staff report",
        "updated:",
        "published:",
    ]

    if any(key in first_lower for key in BYLINE_KEYWORDS):
        return paragraphs[1:]

    return paragraphs

# -----------------------------
# BS4 HTML ARTICLE RETRIEVAL
# -----------------------------

def fetch_article_text(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        # Try to locate the main article container
        container = None
        for selector in ARTICLE_SELECTORS:
            container = soup.select_one(selector)
            if container:
                break

        # Extract paragraphs from the container if found
        if container:
            paragraphs = [
                p.get_text(strip=True)
                for p in container.find_all("p")
                if p.get_text(strip=True)
            ]

            # Fallback: container exists but has no <p> tags
            if not paragraphs:
                paragraphs = [
                    p.get_text(strip=True)
                    for p in soup.find_all("p")
                    if p.get_text(strip=True)
                ]

        else:
            # No container found → fallback to all <p> tags
            paragraphs = [
                p.get_text(strip=True)
                for p in soup.find_all("p")
                if p.get_text(strip=True)
            ]

        # Remove byline if present
        paragraphs = remove_byline(paragraphs)

        return "\n".join(paragraphs).strip()

    except Exception as e:
        print(f"Error fetching article: {e}")
        return None
       
# -----------------------------
# PLAYWRIGHT ARTICLE RETRIEVAL
# -----------------------------

def fetch_article_text_playwright(url):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(30000)

            page.goto(url, timeout=45000)
            page.wait_for_selector("p", state="attached")

            # Try article containers first
            container = None
            for selector in ARTICLE_SELECTORS:
                container = page.query_selector(selector)
                if container:
                    break

            if container:
                paragraphs = [
                    p.inner_text().strip()
                    for p in container.query_selector_all("p")
                    if p.inner_text().strip()
                ]
            else:
                paragraphs = [
                    p.inner_text().strip()
                    for p in page.query_selector_all("p")
                    if p.inner_text().strip()
                ]

            # 🔥 Remove byline here
            paragraphs = remove_byline(paragraphs)

            return "\n".join(paragraphs).strip()

    except Exception as e:
        print(f"Playwright error: {e}")
        return None

    finally:
        try:
            browser.close()
        except:
            pass

# -----------------------------
# GEMINI EXTRACTION
# -----------------------------

def gemini_extract(article):
    """Extract cause/location/summary using Gemini with safe fallbacks."""
    title = article.get("title", "")
    desc = article.get("description", "")
    url = article.get("url", "")

    prompt = f"""
Analyze this Charleston-area accident article:

Title: {title}
Description: {desc}
URL: {url}

Return ONLY JSON:
{{
    "cause": "main cause (speeding/DUI/weather/unknown)",
    "location": "specific road/area",
    "summary": "concise 1-3 sentence key facts"
}}

Do NOT fabricate information.
Do NOT modify URLs.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        return None

# -----------------------------
# OLLAMA EXTRACTION
# -----------------------------
def ollama_extract(article):
    # Pull the article text safely
    content = article.get("content") or article.get("description") or ""
    prompt = f"""
You are an information extraction assistant.

Extract the following fields from the accident report:

- summary: a 1–3 sentence summary of the crash
- location: the city, road, or area where it happened
- cause: the cause of the crash if known, otherwise "unknown"

Return ONLY valid JSON with these exact keys:
summary, location, cause

Article:
\"\"\"{content}\"\"\"

JSON:
"""

    response = ollama.generate(
        model="qwen2:0.5b",
        prompt=prompt,
        options={
            "temperature": 0.1,
            "num_predict": 300
        }
    )
    text = response["response"].strip()

    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        json_str = text[start:end]
        return json.loads(json_str)
    except Exception:
        return {
            "summary": "unknown",
            "location": "unknown",
            "cause": "unknown"
        }

# ========================================
# ===== QWEN BLOG SUMMARY EXTRACTION =====
# ========================================

def qwen_blog_summary(article):
    """
    Generate a 3–5 paragraph narrative blog-style summary using Qwen 0.5B.
    """
    url = article.get("url", "")

    # content = article.get("content") or article.get("description") or ""
    full_text = (fetch_article_text_playwright(article["url"]) or 
        fetch_article_text(article["url"]) or 
        article.get("content") or 
        article.get("description") or 
        ""
    )
    full_text = re.sub(r"\[\+?\d+\schars\]","",full_text).strip()

    prompt = f"""
You are a writing assistant that produces narrative-style blog summaries based strictly on the provided article.

Write a 3–5 paragraph blog-style summary of the accident described in the article.

Tone:
- Narrative and readable, like a news blog.
- Clear, factual, and grounded in the article.
- Smooth transitions between paragraphs.
- No dramatic embellishment or invented details.

Rules:
- Use ONLY information explicitly stated in the article.
- Do NOT invent causes, numbers, injuries, or details.
- Do NOT create names, ages, timelines, or events that are not explicitly stated.
- Every sentence must be directly supported by the article text.
- If the article does not describe how the event unfolded, explicitly state that the article does not provide those details.
- If the article does not specify a detail, write around it without inventing anything.
- If the article is unclear or incomplete, summarize only what is known.
- Do NOT add fictional narrative elements or emotional history.
- If the article does not mention something, do not include it.
- Write ONLY in English.
- Do not include commentary about the writing process.
- Do not quote the article verbatim; paraphrase naturally.

Structure:
- Paragraph 1: Introduce the incident and key facts.
- Paragraph 2: Describe how the event unfolded.
- Paragraph 3: Include details from authorities, witnesses, or official statements.
- Paragraph 4–5 (optional): Add context, aftermath, or community response if mentioned.

Article:
\"\"\"{full_text}\"\"\"

Write the full blog summary now.
"""

    try:
        response = requests.post(
            "http://localhost:8081/v1/chat/completions",
            headers={"content-type": "application/json"},
            json={
                "model": "qwen2.5-0.5b-instruct-q2_k.gguf",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You write clear, factual, narrative-style blog summaries. "
                            "You always follow the user's instructions exactly and write only in English."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.4
            }
        )

        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
        return text

    except Exception as e:
        logging.error(f"Blog summary error: {e}")
        return "Unable to generate blog summary."

#------------------------------
# LLAMA-SERVER EXTRACTION
#------------------------------

def llama_server_extract(article):
    """
    Extract summary/location/cause using Qwen 0.5B running on llama-server (port 8081)
    """

    content = article.get("content") or article.get("description") or ""

    prompt = f"""
You are an information extraction assistant.

Extract the following fields from the accident report:

- summary: a 1–3 sentence factual summary of the crash
- location: the city, road, or area where it happened
- cause: the cause of the crash if explicitly stated, otherwise "unknown"

Rules:
- Use ONLY information explicitly stated in the article.
- If a detail is missing, write "unknown".
- NEVER guess, speculate, or invent numbers or causes.
- Output ONLY valid JSON. No commentary, no markdown, no extra text.
- You must respond ONLY in English. Never use any other language.

Return JSON in this exact structure:
{{
  "summary": "",
  "location": "",
  "cause": ""
}}

Article:
\"\"\"{content}\"\"\"
"""

    try:
        response = requests.post(
            "http://localhost:8081/v1/chat/completions",
            headers={"content-type": "application/json"},
            json={
                "model": "qwen2.5-0.5b-instruct-q2_k.gguf",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a strict information extraction assistant. "
                            "You output ONLY valid JSON. No explanations, no markdown, no commentary."
                        )
                    },
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1
            }
        )

        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()

        json_match = re.search(r"\{[\s\S]*\}", text)
        if not json_match:
            raise ValueError("No JSON Found")

        json_str = json_match.group(0)
        return json.loads(json_str)

    except Exception as e:
        logging.error(f"llama-server extraction error: {e}")
        return {
            "summary": "unknown",
            "location": "unknown",
            "cause": "unknown"
        }
    
# -----------------------------
#  WARM UP OLLAMA LLM
# -----------------------------
def warm_up_ollama():
    try:
        ollama.generate(
            model="qwen2:0.5b",
            prompt="Ready.",
            options={"num_predict": 1}
        )
        print("Ollama model warmed up.")
    except Exception as e:
        print(f"Ollama warm-up faile: {e}")
   
# -----------------------------
# GNEWS FETCH
# -----------------------------

def fetch_gnews_articles():
    """Fetch accident-related news using GNews.io."""
    
    query = (
        '("Charleston SC" OR "Charleston South Carolina" OR "North Charleston" OR "Mount Pleasant SC" OR "Mount Pleasnt South Carolina" OR "Summerville" OR "Goose Creek") ("crash" OR "collision" OR "wreck")'
        #'("Spartanburg" OR "Greenville" OR "Anderson" OR "Duncan" OR "Lyman" OR "Boiling Springs") ("crash" OR "collision" OR "wreck")'
    )

    today_utc = datetime.now(timezone.utc)
    from_date = (today_utc - timedelta(days=30)).strftime("%Y-%m-%d")

    url = "https://gnews.io/api/v4/search"

    params = {
        "q": query,
        "lang": "en",
        "country": "us",
        "from": from_date, # last 30 days
        "in": DOMAIN_FILTER,   # domain filter
        "max": 50,
        "apikey": GNEWS_API_KEY
    }

    logging.info("Running GNews.io request...")
    logging.info(f"Query: {query}")
    logging.info(f"Params: {json.dumps(params, indent=2)}")

    response = requests.get(url, params=params)
    data = response.json()

    log_response("Raw GNews.io response", data)

    return data.get("articles", [])
# -----------------------------
# BUILD THE EMAIL BODY
# -----------------------------
def build_email_body(incidents):
    separator = "\n" + ("-" * 70) + "\n"
    blocks = []
    for inc in incidents:
        block = f"""Title: {inc.get('title', 'N/A')}
Published: {inc.get('published', 'N/A')}
Location: {inc.get('location', 'N/A')}
Cause: {inc.get('cause', 'N/A')}
URL: {inc.get('url', 'N/A')}
Summary:
{inc.get('summary', 'N/A')}
"""
        blocks.append(block.strip())
    return separator.join(blocks)

# -----------------------------
# EMAIL BLOCK
# -----------------------------
def send_incident_email(incidents):
    body = build_email_body(incidents)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = "Accident report"
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg.attach(MIMEText(body, 'plain'))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.send_message(msg)
        print("Email sent")
    except Exception as e:
        print("Email error: {e}")
# -----------------------------
# DEDUPLICATION BASED ON URL
#------------------------------
def dedupe_articles(articles):
    seen = set()
    unique = []
    for art in articles:
        url = art.get("url")
        if not url:
            continue
        if url not in seen:
            seen.add(url)
            unique.append(art)
    return unique

# -----------------------------
# MAIN PIPELINE
# -----------------------------

def run_test_pipeline():

    # print("Warming up Ollama LLM")
    # warm_up_ollama()
    
    articles = fetch_gnews_articles()
    articles = dedupe_articles(articles)

    logging.info(f"Found {len(articles)} raw articles")

    MAX_GEMINI = 10
    articles = articles[:MAX_GEMINI]

    incidents = []

    for art in articles:
        # extracted = gemini_extract(art)
        # extracted = ollama_extract(art)
        extracted = llama_server_extract(art)
        if not extracted:
            continue
        blog = qwen_blog_summary(art)

        if not extracted:
            continue

        incidents.append({
            "title": art.get("title", ""),
            "url": art.get("url", ""),
            "published": art.get("publishedAt", "Unknown"),
            "summary": blog,
            "location": extracted.get("location", ""),
            "cause": extracted.get("cause", "")
        })

    print(f"\n=== Extracted {len(incidents)} Incidents ===")

    send_incident_email(incidents)


if __name__ == "__main__":
    run_test_pipeline()
