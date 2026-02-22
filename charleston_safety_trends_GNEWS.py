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
# LLAMA EXTRACTION
# -----------------------------
def lmstudio_extract(article):
    content = article.get("content") or article.get("description") or ""

    prompt = f"""
You are an information extraction assistant. Read the ENTIRE article carefully.

Extract the following fields from the accident report:

- summary: a concise 1–3 sentence factual summary of the crash. Include:
  - what happened
  - when it happened (if the article mentions a past date)
  - the injury outcome (if mentioned)
  - any official statements or requests (police, attorneys, sheriff, etc.)
  - any context such as newly released video or ongoing investigation
  - do NOT omit important details for the sake of brevity
- location: the city/town and the most contextually relevant location description (e.g., “downtown Charleston” or “MUSC on-campus crosswalk”). Avoid overly literal street listings unless they are central to the story.
- cause: the cause of the crash if explicitly stated; otherwise "unknown".

Rules:
- Base your summary on the MOST RECENT and MOST COMPLETE information in the article.
- Do NOT assume facts or add details not present.
- Return ONLY valid JSON with keys: summary, location, cause.
- Do not include explanations, commentary, or extra text.

Article:
\"\"\"{content}\"\"\"

JSON:
"""

    response = requests.post(
        "http://localhost:8080/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "model": "Qwen2.5-14B-Instruct-GGUF",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1
        })
    )
    data = response.json() 
    # print("LM Studio raw response:", data)
    text = response.json()["choices"][0]["message"]["content"]

    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except:
        return {"summary": "unknown", "location": "unknown", "cause": "unknown"}

# -----------------------------
# OLLAMA EXTRACTION
# -----------------------------
def ollama_extract(article):
    content = article.get("content") or article.get("description") or ""

    prompt = f"""
You are an information extraction assistant. Read the ENTIRE article carefully.

Extract the following fields from the accident report:

- summary: a concise 1–3 sentence factual summary of the crash. Include:
  - what happened
  - the current road status (open or closed) based on the LATEST update in the article
  - who provided the update (police, sheriff, DOT, etc.) if mentioned
  - no assumptions or invented details
- location: the city/town and road/highway where it occurred. If unclear, return "unknown".
- cause: the cause of the crash if explicitly stated; otherwise "unknown".

Rules:
- Base your summary on the MOST RECENT information in the article, not the first paragraph.
- Do NOT assume facts or add details not present.
- Keep the summary factual, neutral, and concise.
- Return ONLY valid JSON.
- Use these exact keys: summary, location, cause.
- Do not include explanations, commentary, or extra text.
- If information is missing, return "unknown" for that field.

Article:
\"\"\"{content}\"\"\"

JSON:
"""

    response = ollama.generate(
        model="qwen2.5:7b",
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

# -----------------------------
#  WARM UP LLMS
# -----------------------------
def warm_up_ollama():
    try:
        ollama.generate(
            model="qwen2.5:7b",
            prompt="Ready.",
            options={"num_predict": 1}
        )
        print("Ollama model warmed up.")
    except Exception as e:
        print(f"Ollama warm-up faile: {e}")

def lmstudio_warmup():
    try:
        requests.post(
            "http://localhost:8080/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json={
                "model": "Qwen2.5-14B-Instruct-GGUF",
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 0
            },
            timeout=10
        )
    except Exception:
        pass

# -----------------------------
# GNEWS FETCH
# -----------------------------

def fetch_gnews_articles():
    """Fetch Charleston accident-related news using GNews.io."""
    
    query = (
        '("Charleston" OR "North Charleston" OR "Mount Pleasant" OR "Summerville" OR "Goose Creek") ("crash" OR "collision" OR "wreck")'
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

    print("Warming up Large Language Models")
    warm_up_ollama()
    lmstudio_warmup()

    articles = fetch_gnews_articles()
    articles = dedupe_articles(articles)

    logging.info(f"Found {len(articles)} raw articles")

    MAX_GEMINI = 10
    articles = articles[:MAX_GEMINI]

    incidents = []

    for art in articles:
        # extracted = gemini_extract(art)
        # extracted = ollama_extract(art)
        extracted = lmstudio_extract(art)

        if not extracted:
            continue

        incidents.append({
            "title": art.get("title", ""),
            "url": art.get("url", ""),
            "published": art.get("publishedAt", "Unknown"),
            "summary": extracted.get("summary", ""),
            "location": extracted.get("location", ""),
            "cause": extracted.get("cause", "")
        })

    print("\n=== Extracted Incidents ===")
    #for inc in incidents:
    #    print(json.dumps(inc, indent=2))
    send_incident_email(incidents)


if __name__ == "__main__":
    run_test_pipeline()
