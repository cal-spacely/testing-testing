# charleston_safety_trends.py
# Automated daily auto accident fetch + weekly trend report for Charleston SC metro
# Uses Google Gemini 2.5 Flash-Lite (free tier friendly)
# Run daily via schedule; emails report

import google.genai as genai
from google.genai.types import Tool, GoogleSearchRetrieval
from google.genai.types import Tool, GoogleSearch
import json
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import schedule
import time
import pandas as pd
import os

# === CONFIG ===
SCRIPT_PATH = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(SCRIPT_PATH,"Json_Resources","cred.json")
with open(JSON_PATH, "r") as f:
    cred = json.load(f)

GEMINI_API_KEY = cred["API_KEY"]          # From https://aistudio.google.com
EMAIL_FROM = cred["FROM_EMAIL"]
EMAIL_TO = cred["TO_EMAIL"]                   # Change to your email
EMAIL_PASSWORD = cred["APP_PASSWORD"]           # Create at myaccount.google.com/apppasswords
DB_FILE = "charleston_accidents.db"

client = genai.Client(api_key=GEMINI_API_KEY)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accidents (
            url TEXT PRIMARY KEY,
            title TEXT,
            summary TEXT,
            cause TEXT,
            location TEXT,
            fetch_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def fetch_and_extract():
    """Fetch recent accidents and extract structured info (low request usage)."""
    prompt_fetch = """
        Use your web knowledge and search capabilities to find real, recent auto accident or car crashes in the Charleston SC metropolitan area (past 24-48 hours).
        Return ONLY a valid JSON array of up to 8 unique incidents. Each object MUST include:
        - "title": short title
        - "url": the ACTUAL direct URL to the original article or official source (copy the real link exactly; NEVER invent, guess, or fabricateany URL)
        - "snippet": 1-2 sentance summary of facts
        Priortize: postandcourier.com, live5news.com, wcsc.com, charlestoncounty.org, scdot.org.
        ONLY ouput the JSON array - no extra text.
    """
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents = prompt_fetch,
            config={
                "response_mime_type": "application/json"
            }
        )
        # print("Raw Gemini fetch response:", repr(resp.text))
        text = resp.text.strip()
        if text.startswith("```json"): text = text.split("```json")[1].split("```")[0].strip()
        articles = json.loads(text)
    except Exception as e:
        print(f"Fetch error: {e}")
        return []

    conn = sqlite3.connect(DB_FILE)
    new_data = []
    for art in articles:
        url = art.get("url", "")
        if not url: continue
        cursor = conn.execute("SELECT url FROM accidents WHERE url=?", (url,))
        if cursor.fetchone(): continue  # dedup

        # Extract structured fields (one extra call per new article - keep low volume)
        prompt_extract = f"""
        From this accident snippet: {art.get('snippet', '')}
        Return ONLY JSON: {{"cause": "main cause e.g. speeding/DUI/weather/unknown", "location": "specific road/area e.g. I-26 near exit 199", "summary": "concise 1-2 sentence key facts"}}
        """
        try:
            ext_resp = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt_extract,
                config={
                    "response_mime_type": "application/json"
                } 
            )
            ext_text = ext_resp.text.strip()
            if ext_text.startswith("```"): ext_text = ext_text.split("```")[1].strip()
            structured = json.loads(ext_text)
        except:
            structured = {"cause": "Unknown", "location": "Charleston Metro", "summary": art.get("snippet", "")}

        conn.execute("""
            INSERT OR IGNORE INTO accidents (url, title, summary, cause, location, fetch_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (url, art.get("title", "Untitled"), structured["summary"], structured["cause"], structured["location"], datetime.now().isoformat()))
        new_data.append({**art, **structured})
    conn.commit()
    conn.close()
    return new_data

def generate_trends_report():
    """Weekly trends from DB."""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("""
        SELECT * FROM accidents 
        WHERE fetch_date > datetime('now', '-35 days')  -- ~5 weeks for trends
    """, conn)
    conn.close()

    if df.empty:
        return "No recent data for trends."

    df['fetch_date'] = pd.to_datetime(df['fetch_date'])
    df['week'] = df['fetch_date'].dt.isocalendar().week

    total = len(df)
    top_causes = df['cause'].value_counts().head(3).to_dict()
    top_locations = df['location'].value_counts().head(3).to_dict()
    weekly_counts = df.groupby('week').size().to_dict()

    report = f"""
<h2>Traffic Safety Trends (Last 5 Weeks) - Charleston SC Metro</h2>
<p>Total incidents: {total}</p>
<p>Top causes: {', '.join([f'{k} ({v})' for k,v in top_causes.items()])}</p>
<p>Top locations: {', '.join([f'{k} ({v})' for k,v in top_locations.items()])}</p>
<p>Weekly breakdown: {weekly_counts}</p>
<p>Data from local news via Gemini AI. For official stats, check SCDOT or NHTSA.</p>
"""
    return report

def send_email(subject, body_html):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg.attach(MIMEText(body_html, 'html'))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.send_message(msg)
        print("Email sent")
    except Exception as e:
        print(f"Email error: {e}")

def daily_task():
    new_incidents = fetch_and_extract()
    if not new_incidents:
        return  # No new → skip email
    daily_html = "<h1>Daily Charleston SC Accidents Report</h1><p>New incidents today:</p>"
    for inc in new_incidents:
        daily_html += f"<h3>{inc.get('title')}</h3><p>{inc.get('summary')}</p><p>Cause: {inc.get('cause')} | Location: {inc.get('location')}</p><a href='{inc.get('url')}'>Source</a><br><br>"
    send_email("Daily Charleston Accidents", daily_html)

def weekly_task():
    trends_html = "<h1>Weekly Charleston SC Traffic Safety Trends</h1>" + generate_trends_report()
    send_email("Weekly Traffic Safety Trends - Charleston SC", trends_html)

# Initialize
init_db()

# Schedule (adjust times to your preference; EST)
# schedule.every().day.at("08:00").do(daily_task)          # Daily at 8 AM
# schedule.every().monday.at("09:00").do(weekly_task)      # Weekly trends Mondays 9 AM

print("Automation started. Press Ctrl+C to stop.")

# while True:
#     schedule.run_pending()
#     time.sleep(60)

if __name__ == "__main__":
    daily_task()