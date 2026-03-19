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
from collections import defaultdict 


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
    ".article-body-content",
    ".story-content",
    ".c-article__body",
    ".article__content",
    ".article-text",
    ".article__body",
    ".article__body-content",
    "div[itemprop='articleBody']",
]    

BAD_PATTERNS = [
    "forecast", "windy", "thunderstorms", "copyright", 
    "advertisement", "ad choices", "cookie", "privacy policy",
]

def filtered_pattern(p):
    text = p.lower()
    return any(bad in text for bad in BAD_PATTERNS)

def remove_author_bio(text):
    lines = text.splitlines()
    cleaned = []
    skip = True

    for line in lines:
        stripped = line.strip()

        # Detect start of real article body
        if (
            stripped.startswith(("CHARLESTON", "NORTH CHARLESTON", "COLUMBIA", "GREENVILLE", "SPARTANBURG"))
            or stripped.startswith("—")  # em dash lead
            or stripped[:1].isupper() and " — " in stripped  # e.g., "CHARLESTON —"
            or stripped.startswith("It was")  # many P&C articles start this way
            or stripped.startswith("On ")  # date-led ledes
        ):
            skip = False

        if not skip:
            cleaned.append(line)

    return "\n".join(cleaned).strip()

def extract_author_name(soup):
    """
    Attempts to extract an author/reporter name from a wide range of news sites.
    Covers Live5News, ABC4, CountOn2, Post & Courier, The State, Greenville Online,
    Greenville Journal, WYFF4, FOX Carolina, WSPA, WGOG, WSNW, WLOS, etc.
    """

    # 1. Known site-specific selectors
    selectors = [
        ".author", ".author-name", ".byline", ".byline-name", ".article-author",
        ".story-author", ".post-author", ".c-article__author", ".article__byline",
        ".meta-author", ".article-meta-author", ".author-block span",
        ".article-byline", ".story-byline", ".article-header__author"
    ]

    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            name = el.get_text(strip=True)
            if name and 2 <= len(name.split()) <= 4:
                return name

    # 2. Look for "By John Doe" patterns anywhere in the HTML
    possible = soup.find_all(string=True)
    for text in possible:
        t = text.strip()
        if t.lower().startswith("by "):
            name = t[3:].strip()
            if 2 <= len(name.split()) <= 4:
                return name

    # 3. Look for short capitalized names near the top of the article
    #    (common on Live5News, ABC4, WYFF4, etc.)
    top_candidates = soup.find_all(["p", "span", "div"], limit=10)
    for el in top_candidates:
        t = el.get_text(strip=True)
        if not t:
            continue

        # Skip long text blocks
        if len(t) > 60:
            continue

        # Look for capitalized name-like patterns
        words = t.split()
        if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w.isalpha()):
            # Avoid grabbing dates or locations
            if any(x.lower() in t.lower() for x in ["updated", "published", "charleston", "south carolina"]):
                continue
            return t

    return None

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
# REGEX FACT EXTRACTOR
# -----------------------------

def extract_structured_facts(text):
    facts = {
        "people": [],
        "driver": None,
        "passengers": [],
        "victims": [],
        "family_members": [],
        "officials": [],
        "dates": [],
        "court": {},
        "crash_details": [],
        "quotes": [],
        "suspect": None
    }

    lower_text = text.lower()

    # --- HELPERS ---

    def normalize_name(name):
        return " ".join(part.strip() for part in name.split())

    def unique_append(lst, item):
        if item not in lst:
            lst.append(item)

    # --- QUOTES ---
    quote_pattern = r"“([^”]+)”"
    facts["quotes"] = re.findall(quote_pattern, text)

    # --- DATES & TIMES ---
    date_pattern_full = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4}"
    date_pattern_md = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}"
    time_pattern = r"\b\d{1,2}:\d{2}\s*(?:a\.m\.|p\.m\.)"

    dates = set(re.findall(date_pattern_full, text))
    dates.update(re.findall(date_pattern_md, text))
    dates.update(re.findall(time_pattern, text))
    facts["dates"] = list(dates)

    # --- CRASH DETAILS ---
    crash_keywords = [
        "collision", "crash", "accident", "impact", "ejected",
        "train", "railroad", "crossing", "intersection", "wreckage",
        "rollover", "entrapment", "hit-and-run", "multi-vehicle"
    ]
    for kw in crash_keywords:
        if kw in lower_text:
            unique_append(facts["crash_details"], kw)

    # speed
    speed_pattern = r"(\d+)\s*mph"
    for m in re.findall(speed_pattern, lower_text):
        unique_append(facts["crash_details"], f"{m} mph")

    # --- NAMES (broad) ---
    # 2–4 capitalized tokens
    name_pattern = r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})\b"
    raw_names = set(re.findall(name_pattern, text))

    # Filter out obvious non-persons
    blacklist_first = {
        "CHARLESTON", "NORTH", "SOUTH", "Monday", "Tuesday", "Wednesday",
        "Thursday", "Friday", "Saturday", "Sunday", "Copyright"
    }
    names = set()
    for n in raw_names:
        first = n.split()[0]
        if first.upper() in blacklist_first:
            continue
        names.add(normalize_name(n))

    # --- AGES ---
    ages_simple_pattern = r"\b(\d{1,2})\s*(?:years?\s*old|year-old)"
    ages_now_pattern = r"now\s+(\d{1,2})\b"
    ages_trailing_pattern = r"\b(\d{1,2})\s*[,;]"

    ages = set(re.findall(ages_simple_pattern, text))
    ages.update(re.findall(ages_now_pattern, text))
    ages.update(re.findall(ages_trailing_pattern, text))

    # --- NAME + AGE PAIRS ---
    # e.g. "Tiasia Monique Newton, 22;" or "Roger Anibal Cardona Lopez, 26,"
    name_age_pattern = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3}),?\s*(?:now\s*)?(\d{1,2})\b"
    name_age_pairs = []
    for name, age in re.findall(name_age_pattern, text):
        name_norm = normalize_name(name)
        name_age_pairs.append((name_norm, int(age)))

    # Add to people
    seen_people = set()
    for name, age in name_age_pairs:
        key = (name, age)
        if key not in seen_people:
            facts["people"].append({"name": name, "age": age})
            seen_people.add(key)

    # --- OFFICIALS ---
    official_pattern = r"\b(Solicitor|Assistant Solicitor|Judge|Sheriff|Officer|Detective|Cpl\.|Coroner)\s+([A-Z][a-z]+(?: [A-Z][a-z]+){0,2})"
    for title, name in re.findall(official_pattern, text):
        name_norm = normalize_name(name)
        facts["officials"].append({"title": title.replace("Cpl.", "Cpl"), "name": name_norm})

    # --- FAMILY MEMBERS ---
    family_pattern = r"\b(mother|father|sister|brother|son|daughter|cousin|aunt|uncle|grandmother|grandfather)\s+of\s+([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})"
    for relation, person in re.findall(family_pattern, text, re.IGNORECASE):
        facts["family_members"].append({
            "relation": relation.lower(),
            "to": normalize_name(person)
        })

    # --- VICTIM LISTS ---
    # e.g. "Tiasia Monique Newton, 22; Danielle Shon Branton, 29; and Reshana Simone Lambright, 32;"
    victim_list_pattern = r"((?:[A-Z][a-z]+(?: [A-Z][a-z]+){1,3},\s*\d{1,2}\s*;?\s*(?:and\s*)?)+)"
    for block in re.findall(victim_list_pattern, text):
        pair_pattern = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3}),\s*(\d{1,2})"
        for name, age in re.findall(pair_pattern, block):
            name_norm = normalize_name(name)
            victim_entry = {"name": name_norm, "age": int(age)}
            if victim_entry not in facts["victims"]:
                facts["victims"].append(victim_entry)

    # Also catch single victims with "lost their lives", "were killed", etc.
    victim_single_pattern = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3}),\s*(\d{1,2})\s*;?.{0,80}?(?:died|killed|lost their lives)"
    for name, age in re.findall(victim_single_pattern, text, re.IGNORECASE | re.DOTALL):
        name_norm = normalize_name(name)
        victim_entry = {"name": name_norm, "age": int(age)}
        if victim_entry not in facts["victims"]:
            facts["victims"].append(victim_entry)

    # --- SUSPECT DETECTION ---
    # Pattern: "NAME, 26, is charged with ..."
    suspect_pattern = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3}),\s*(\d{1,2}),\s+is charged with\s+([^\.]+)\."
    suspect_match = re.search(suspect_pattern, text)
    suspect = None
    if suspect_match:
        s_name = normalize_name(suspect_match.group(1))
        s_age = int(suspect_match.group(2))
        charges_text = suspect_match.group(3)
        # split charges by "and" and commas
        parts = re.split(r",\s*| and ", charges_text)
        charges = [p.strip() for p in parts if p.strip()]
        suspect = {
            "name": s_name,
            "age": s_age,
            "charges": charges,
            "arrested": "arrested" in lower_text or "detention center" in lower_text
        }

    # Fallback suspect: "NAME ... was arrested" or "police arrested NAME"
    if not suspect:
        arrested_pattern = r"(?:arrested\s+([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})|([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})\s+was arrested)"
        m = re.search(arrested_pattern, text)
        if m:
            name_candidate = m.group(1) or m.group(2)
            s_name = normalize_name(name_candidate)
            s_age = None
            for n, a in name_age_pairs:
                if n == s_name:
                    s_age = a
                    break
            suspect = {
                "name": s_name,
                "age": s_age,
                "charges": [],
                "arrested": True
            }

    facts["suspect"] = suspect

    # --- DRIVER DETECTION ---
    # Heuristics: name near "driver", "driver's seat", "was driving", "behind the wheel", "SUV", "vehicle"
    driver = None

    # explicit "NAME, 24, was driving" or "NAME was driving"
    driver_pattern1 = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3}).{0,40}?\bwas driving\b"
    m = re.search(driver_pattern1, text)
    if m:
        driver = normalize_name(m.group(1))

    # "ejected from driver’s seat" near a name
    if not driver:
        ejected_pattern = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3}).{0,80}?ejected from driver"
        m = re.search(ejected_pattern, text, re.IGNORECASE)
        if m:
            driver = normalize_name(m.group(1))

    # "NAME's SUV" or "NAME’s SUV"
    if not driver:
        suv_pattern = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})[’']s SUV"
        m = re.search(suv_pattern, text)
        if m:
            driver = normalize_name(m.group(1))

    # If still no driver but there is a suspect and crash context, suspect may be driver
    if not driver and suspect and ("crash" in lower_text or "collision" in lower_text):
        driver = suspect["name"]

    facts["driver"] = driver

    # --- COURT DETAILS ---
    # Plea
    if "pleaded guilty" in lower_text or "guilty plea" in lower_text:
        facts["court"]["plea"] = "guilty"

    # Sentence: prefer explicit "sentenced NAME to 10 years"
    sentence_pattern_strict = r"sentenced[^\.]*?\bto\s+(\d+)\s+(?:years|year)"
    m_strict = re.search(sentence_pattern_strict, text, re.IGNORECASE)
    sentence_years = None
    if m_strict:
        sentence_years = int(m_strict.group(1))
    else:
        # fallback: any "sentenced ... X years" but avoid "could have been sentenced"
        sentence_pattern_loose = r"(?<!could have been )sentenced[^\.]*?(\d+)\s+(?:years|year)"
        m_loose = re.search(sentence_pattern_loose, text, re.IGNORECASE)
        if m_loose:
            sentence_years = int(m_loose.group(1))

    if sentence_years is not None:
        facts["court"]["sentence"] = f"{sentence_years} years"

    # --- PASSENGERS (simple heuristic) ---
    # If text mentions "passenger" with a name
    passenger_pattern = r"([A-Z][a-z]+(?: [A-Z][a-z]+){1,3}).{0,40}?\bpassenger\b"
    for name in re.findall(passenger_pattern, text, re.IGNORECASE):
        name_norm = normalize_name(name)
        if name_norm not in facts["passengers"]:
            facts["passengers"].append(name_norm)

    return facts

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
        author = extract_author_name(soup)
        if author:
            paragraphs = [
                p for p in paragraphs
                if author.lower() not in p.lower()
            ]
        
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
    #print("\n==== FULL TEXT (before) ====")
    #print(full_text)
    #print("=========================")
    full_text = re.sub(r"\[\+?\d+\schars\]","",full_text).strip()
    #print("\n==== FULL TEXT (after) =======")
    #print(full_text)
    #print("=========================")
    clean_text = remove_author_bio(full_text)

    facts = extract_structured_facts(clean_text)
    facts_json = json.dumps(facts, indent=2)
    
    #print("\n===== CLEAN TEXT ======")
    #print(clean_text)
    #print("=========================")
    print("\n=== EXTRACTED FACTS ===")
    print(facts_json)
    print("=========================")

    prompt = f"""
You are a writing assistant that produces narrative-style blog summaries based strictly on the provided article.

You are also given STRUCTURED FACTS extracted from the article.  
These facts are the authoritative source for:
- who the driver was
- who the passengers were
- who died or survived
- ages
- dates
- timeline order
- courtroom actions
- quotes
- any other factual details

You MUST NOT contradict or alter any structured fact.  
If the article text appears ambiguous, follow the structured facts.

Write a 3–5 paragraph blog-style summary of the accident described in the article.

Tone:
- Narrative and readable, like a news blog.
- Clear, factual, and grounded in the article.
- Smooth transitions between paragraphs.
- No dramatic embellishment or invented details.

Rules:
- Use ONLY information explicitly stated in the article or the structured facts.
- Do NOT infer or assume details not present in either source.
- Do NOT merge or collapse events from different years.
- Do NOT merge ages from different years or different people.
- Do NOT use outside knowledge.
- If the article does not describe how the event unfolded, state that the article does not provide those details.
- Do NOT add fictional narrative elements or invented context.
- Write ONLY in English.
- Do not quote the article verbatim; paraphrase naturally.

Identity Rules:
- Use the STRUCTURED FACTS as the authoritative source for all roles (driver, passenger, victim, family member, official).
- Do NOT invent or assume roles that are not present in the structured facts.
- If the structured facts do not identify a driver, do NOT guess or assign one.
- If the structured facts do not identify victims, do NOT claim anyone died.
- If the structured facts do not identify survivors, do NOT claim anyone survived.
- If the structured facts do not identify courtroom actions, do NOT invent any.

Structure:
- Paragraph 1: Introduce the incident using structured facts.
- Paragraph 2: Describe the crash as presented in the article.
- Paragraph 3: Include details from authorities, witnesses, or official statements.
- Paragraph 4–5: Add courtroom developments or aftermath.

STRUCTURED FACTS:
{facts_json}

ARTICLE TEXT:
\"\"\"{clean_text}\"\"\"

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
# DEDUPLICATION BASED ON TITLE
# -----------------------------
def dedupe_article_title(articles):
    seen_titles = set()
    unique = []
    for art in articles:
        title = art.get("title")
        normalized_title = title.strip().lower()
        if normalized_title in seen_titles:
            continue
        if normalized_title not in seen_titles:
            seen_titles.add(normalized_title)
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
    articles = dedupe_article_title(articles)

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
