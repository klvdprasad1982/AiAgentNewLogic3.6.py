import os
import time
import requests
import xml.etree.ElementTree as ET
import re
import threading
import html
from datetime import datetime, timedelta
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton  
from google import genai  
from flask import Flask
from pymongo import MongoClient 
from deep_translator import GoogleTranslator

# --- 🌐 Render Web Service కోసం ఫ్లాస్క్ సెటప్ ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Institutional Research Terminal Bot v10.5 (Indian Equities Edition) is running 24/7 on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)  

# =====================================================================
# 🌟 Render Environment Variables
# =====================================================================
MY_GEMINI_API_KEY_1 = os.environ.get("MY_GEMINI_API_KEY_1", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
YOUR_TELEGRAM_CHAT_ID = os.environ.get("YOUR_TELEGRAM_CHAT_ID", "")
MONGO_URI = os.environ.get("MONGO_URI", "")

GEMINI_API_KEY = MY_GEMINI_API_KEY_1
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

if not TELEGRAM_BOT_TOKEN:
    print("⚠️ TELEGRAM_BOT_TOKEN environment variable is empty.")
if not GEMINI_API_KEY:
    print("⚠️ GEMINI_API_KEY environment variable is empty.")
if not YOUR_TELEGRAM_CHAT_ID:
    print("⚠️ TELEGRAM_CHAT_ID environment variable is empty.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

# =====================================================================
# 🥭 MongoDB డేటాబేస్ సెటప్
# =====================================================================
db = None
news_collection = None
if MONGO_URI.strip():
    print("🥭 MongoDB కి కనెక్ట్ అవుతున్నాము సర్...")
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        db = mongo_client["TradingBotDB"]
        news_collection = db["processed_news"]
        news_collection.create_index("clean_content", unique=False)
        news_collection.create_index("story_fingerprint", unique=False)
        news_collection.create_index("event_id", unique=False)
        news_collection.create_index("timestamp", unique=False)
        news_collection.create_index("dedupe_key", unique=True, sparse=True)
        news_collection.create_index([("event_id", 1), ("timestamp", -1)])
        print("✅ MongoDB తో కనెక్షన్ సక్సెస్ అయింది సర్!")
    except Exception as mongo_init_err:
        print(f"⚠️ MongoDB అందుబాటులో లేదు — RAM deduplicationతో కొనసాగుతున్నాము: {mongo_init_err}")
        db = None
        news_collection = None
else:
    print("ℹ️ MONGO_URI ఇవ్వలేదు — Strong RAM + Batch Deduplication mode లో నడుస్తోంది సర్!")

analysis_vault = {}
SENT_NEWS_MEMORY = []
SENT_NEWS_META = []  
RECENT_EVENT_IDS = {} 

def report_error_to_telegram(error_location, error_msg):
    if any(err in str(error_msg) for err in ["10054", "Connection aborted", "ConnectionResetError", "timed out", "Max retries exceeded"]):
        print(f"⚠️ [NETWORK NOTICE Ignored] Location: {error_location} | Error: {error_msg}")
        return

    if bot is None or not YOUR_TELEGRAM_CHAT_ID:
        print(f"⚠️ [{error_location}] {error_msg}")
        return
    try:
        err_text = f"⚠️ <b>[BOT ERROR ALERT]</b>\n\n" \
                   f"📍 <b>Location:</b> {error_location}\n" \
                   f"❌ <b>Error:</b> <code>{error_msg}</code>\n" \
                   f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        bot.send_message(YOUR_TELEGRAM_CHAT_ID, err_text, parse_mode="HTML")
    except Exception as telegram_err:
        print(f"❌ ఎర్రర్ రిపోర్ట్ పంపడంలో లోపం: {telegram_err}")

def clean_main_content(text):
    text = text.lower()
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'[^a-zA-Z0-9\s]', '', text)
    return " ".join(text.split())

def clean_for_html(text):
    if not text:
        return ""
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'`(.*?)`', r'<code>\1</code>', text)
    return text

# =====================================================================
# 🛡️ GLOBAL STOCKS ఫిల్టర్ & కేవలం INDIAN EQUITIES మాత్రమే అనుమతించే ఫంక్షన్
# =====================================================================
GLOBAL_COMPANIES_AND_INDICES = {
    'tesla', 'apple', 'nvidia', 'microsoft', 'alphabet', 'google', 'amazon',
    'meta', 'netflix', 'amd', 'intel', 'qualcomm', 'tsmc', 'super micro',
    'nasdaq', 'dow jones', 's&p 500', 's&p500', 'wall street', 'boeing',
    'ford', 'general motors', 'toyota', 'volkswagen', 'exxon', 'chevron',
    'pfizer', 'moderna', 'arm holdings', 'asml', 'broadcom'
}

INDIAN_EQUITY_MARKERS = {
    'nifty', 'sensex', 'bse', 'nse', 'dalal street', 'india', 'indian',
    'rupee', 'rs.', 'inr', 'crore', 'cr', 'target price', 'target of',
    'share price', 'stock price', 'q1', 'q2', 'q3', 'q4', 'fy25', 'fy26', 'fy27'
}

def is_strictly_indian_equity(title, description=""):
    blob = f"{title} {description}".lower()
    
    # 1. ఒకవేళ విదేశీ పాపులర్ కంపెనీ పేరు మాత్రమే ఉండి భారత మార్కెట్ ప్రస్తావన లేకపోతే తిరస్కరించాలి
    for foreign_entity in GLOBAL_COMPANIES_AND_INDICES:
        # Check boundary match
        if re.search(r'\b' + re.escape(foreign_entity) + r'\b', blob):
            # భారతీయ మార్కెట్ ప్రస్తావన కూడా లేకపోతే వెంటనే డ్రాప్ చేయండి
            if not any(k in blob for k in ['nifty', 'sensex', 'dalal street', 'bse', 'nse', 'indian market', 'rupee', 'crore']):
                return False
    
    # 2. మనీకంట్రోల్ లేదా ఇన్వెస్టింగ్ డాట్ కామ్ ఇండియా వెర్షన్ ఆర్టికల్స్ సాధారణంగా ఇండియన్ స్టాక్ మార్కెట్ విశ్లేషణలై ఉంటాయి
    return True

def detect_article_type(title):
    title_lower = title.lower()
    if any(k in title_lower for k in [
        'rbi keeps', 'rbi policy', 'repo rate', 'fed cuts', 'fed hikes', 'rate cut', 'rate hike', 
        'monetary policy', 'fomc', 'government approves', 'cabinet clears', 'sebi bans', 'pli scheme'
    ]):
        return "Policy Decision"
    elif any(k in title_lower for k in ['target price', 'target of', 'buy call', 'sell call', 'initiates coverage', 'overweight', 'outperform', 'downgrades', 'upgrades', 'brokerage report', 'reiterates']):
        return "Brokerage Action"
    elif any(k in title_lower for k in ['interview', 'daily voice', 'fund manager outlook', 'in conversation with', 'speaks to']):
        return "Interview"
    elif any(k in title_lower for k in ['breaking', 'live', 'imposes', 'slaps', 'acquires', 'merger']):
        return "Breaking"
    elif any(k in title_lower for k in ['earnings', 'q1 results', 'q2 results', 'q3 results', 'q4 results', 'net profit', 'ebitda', 'revenue']):
        return "Earnings Research"
    else:
        return "Research Note"

def classify_news(title):
    title_lower = title.lower()
    if any(k in title_lower for k in ['target price', 'brokerage', 'buy', 'sell', 'accumulate', 'overweight', 'neutral', 'jefferies', 'morgan stanley', 'goldman sachs', 'motilal oswal', 'icici direct', 'kotak', 'clsa', 'nomura', 'ubs', 'nuvama']):
        return "Brokerage & Research"
    elif any(k in title_lower for k in ['rbi', 'fed', 'repo rate', 'monetary policy', 'cpi', 'inflation', 'gdp']):
        return "Macro & Policy"
    elif any(k in title_lower for k in ['acquisition', 'merger', 'stake', 'ipo', 'earnings', 'q1', 'q2', 'q3', 'q4', 'profit']):
        return "Corporate & Earnings"
    elif any(k in title_lower for k in ['crude', 'oil', 'gold', 'silver', 'metals']):
        return "Commodities"
    elif any(k in title_lower for k in ['auto', 'banking', 'defence', 'it', 'pharma', 'fmcg', 'realty', 'energy']):
        return "Sectoral Action"
    else:
        return "General Market"

def detect_event_and_impact(title, category, article_type):
    title_lower = title.lower()
    
    # ప్రముఖ గ్లోబల్ & డొమెస్టిక్ బ్రోకర్ల గుర్తింపు
    broker_found = None
    all_known_brokers = [
        ('JEFFERIES', 'jefferies'),
        ('MORGAN_STANLEY', 'morgan stanley'),
        ('GOLDMAN_SACHS', 'goldman sachs'),
        ('JPMORGAN', 'jpmorgan|jp morgan'),
        ('CLSA', 'clsa'),
        ('NOMURA', 'nomura'),
        ('UBS', 'ubs'),
        ('MACQUARIE', 'macquarie'),
        ('BERNSTEIN', 'bernstein'),
        ('HSBC', 'hsbc'),
        ('CITI', 'citi|citigroup'),
        ('BOFA', 'bofa|bank of america'),
        ('MOTILAL_OSWAL', 'motilal oswal|mosl'),
        ('ICICI_DIRECT', 'icici direct'),
        ('KOTAK', 'kotak institutional|kotak sec'),
        ('HDFC_SEC', 'hdfc sec'),
        ('AXIS_CAPITAL', 'axis capital'),
        ('NUVAMA', 'nuvama'),
        ('EMKAY', 'emkay'),
        ('ANTIQUE', 'antique stock'),
        ('PRABHUDAS', 'prabhudas lilladher|pl india')
    ]
    
    for tag, pattern in all_known_brokers:
        if re.search(r'\b(' + pattern + r')\b', title_lower):
            broker_found = tag
            break
            
    if broker_found:
        event_id = f"RESEARCH_{broker_found}"
    elif 'rbi' in title_lower:
        event_id = "RBI_MONETARY_POLICY"
    elif 'fed' in title_lower:
        event_id = "US_FED_POLICY"
    elif any(k in title_lower for k in ['earnings', 'results', 'profit', 'q1', 'q2', 'q3', 'q4']):
        event_id = "EARNINGS_REPORT"
    else:
        clean_words = [w for w in re.findall(r'[a-z0-9]+', title_lower) if w not in STORY_STOPWORDS and len(w) > 3]
        event_id = f"EVT_{'_'.join(clean_words[:2])}".upper() if clean_words else "INDIAN_EQUITY_UPDATE"

    impacted_assets = set()
    impacted_sectors = set()
    impacted_stocks = set()
    
    if any(k in title_lower for k in ['bank', 'nbfc', 'hdfc', 'sbi', 'icici', 'axis']):
        impacted_sectors.add('Banking & Financials')
        impacted_assets.add('BankNifty')
    if any(k in title_lower for k in ['tcs', 'infosys', 'wipro', 'hcl', 'tech m', 'it sector']):
        impacted_sectors.add('IT & Tech')
        impacted_assets.add('Nifty IT')
    if any(k in title_lower for k in ['auto', 'tata motors', 'maruti', 'm&m', 'bajaj auto']):
        impacted_sectors.add('Automobile')
    if any(k in title_lower for k in ['pharma', 'sun pharma', 'dr reddy', 'cipla']):
        impacted_sectors.add('Pharma & Healthcare')

    impacted_assets.add('Nifty 50')
    if not impacted_sectors:
        impacted_sectors.add(category)
        
    # ఆర్టికల్ లో ఉన్న ప్రముఖ ఇండియన్ స్టాక్స్ పేర్లను వెతకడం
    indian_stock_names = ['Reliance', 'HDFC Bank', 'ICICI Bank', 'TCS', 'Infosys', 'Tata Motors', 'SBI', 'L&T', 'Bharti Airtel', 'ITC', 'Adani Enterprises', 'Sun Pharma', 'Tata Steel', 'Maruti']
    for stk in indian_stock_names:
        if stk.lower() in title_lower:
            impacted_stocks.add(stk)
            
    if not impacted_stocks:
        impacted_stocks.add('Indian Equities')

    return event_id, ", ".join(list(impacted_assets)[:3]), ", ".join(list(impacted_sectors)[:3]), ", ".join(list(impacted_stocks)[:4])

SOURCE_CONFIG = {
    "regulators": {"keywords": ["rbi", "fed", "sebi"], "market": 10, "research": 10},
    "global_banks": {
        "keywords": ["jefferies", "morgan stanley", "goldman sachs", "jp morgan", "jpmorgan", "clsa", "nomura", "ubs", "macquarie", "hsbc", "citi", "bernstein", "bofa"],
        "market": 15, "research": 25
    },
    "domestic_brokers": {
        "keywords": ["motilal oswal", "icici direct", "kotak", "hdfc securities", "axis capital", "nuvama", "emkay", "antique", "prabhudas lilladher", "anand rathi", "sharekhan"],
        "market": 12, "research": 20
    },
    "aggregators": {"keywords": ["moneycontrol", "investing.com"], "market": 5, "research": 5}
}

def compress_score(score):
    if score <= 70: return score
    return 70 + (score - 70) * 0.6

def calculate_dynamic_scores(title, article_type, category, source=None, event_id=None, debug=False):
    title_lower = title.lower()
    m_score = 40
    r_score = 45

    for group, cfg in SOURCE_CONFIG.items():
        for kw in cfg["keywords"]:
            if re.search(r'\b' + re.escape(kw) + r'\b', title_lower):
                m_score += cfg["market"]
                r_score += cfg["research"]
                break

    if any(k in title_lower for k in ['target price', 'target of', 'buy call', 'upgrades', 'initiates coverage']):
        m_score += 15
        r_score += 20

    if article_type == "Brokerage Action":
        r_score += 20
    elif article_type == "Policy Decision":
        m_score += 25; r_score += 15

    final_market_score = int(max(10, min(100, round(compress_score(m_score)))))
    final_research_score = int(max(15, min(100, round(compress_score(r_score)))))
    return final_market_score, final_research_score

def get_priority(market_score, research_score, article_type):
    if article_type == "Brokerage Action" or research_score >= 80:
        return "🎯 BROKERAGE / INSTITUTIONAL REPORT"
    elif article_type == "Policy Decision" or market_score >= 85:
        return "🚨 POLICY / HIGH IMPACT"
    elif market_score >= 65 or research_score >= 70:
        return "⚡ HIGH"
    else:
        return "ℹ️ NORMAL"

STORY_STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'to', 'of', 'in', 'on', 'for', 'with',
    'from', 'by', 'as', 'at', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'says', 'said', 'say', 'amid', 'after', 'before', 'over', 'under', 'into',
    'today', 'now', 'live', 'shares', 'stock', 'stocks', 'price', 'prices',
    'here', 'how', 'what', 'why', 'who', 'when', 'where', 'their', 'its'
}

def normalize_story_text(text):
    text = str(text or '').lower()
    text = re.sub(r'https?://\S+|www\.\S+', ' ', text)
    text = re.sub(r'[^a-z0-9%₹$€£.\s-]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def extract_keywords(text):
    text = normalize_story_text(text)
    return {w for w in text.split() if w not in STORY_STOPWORDS and len(w) > 2}

def clean_news_title_core(title):
    if not title: return ""
    title = html.unescape(title)
    sources_pattern = r"(Investing\.com|Moneycontrol\.com|Moneycontrol|CNBC-TV18|CNBC|Economic Times|ETMarkets\.com|Business Standard|Mint|Livemint|Reuters|NDTV Profit|ET NOW)"
    title = re.sub(rf"\s*[-–|]?\s*{sources_pattern}.*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r'https?://\S+|www\.\S+', '', title)
    title = re.sub(r'[^a-zA-Z0-9\s]', '', title)
    return " ".join(title.lower().split())

def canonical_story_fingerprint(text):
    clean_t = clean_news_title_core(text)
    words = [w for w in clean_t.split() if w not in STORY_STOPWORDS and len(w) > 2]
    return " ".join(words)

def make_dedupe_key(title, article_link=""):
    return canonical_story_fingerprint(title)

def detect_allowed_publisher(title, link="", description=""):
    blob = f"{title} {link} {description}".lower()
    if "in.investing.com" in blob or "investing.com" in blob:
        return "Investing.com India"
    if "moneycontrol.com" in blob or "moneycontrol" in blob:
        return "Moneycontrol.com"
    return ""

def is_duplicate_story(title, candidates):
    new_fp = canonical_story_fingerprint(title)
    new_kws = extract_keywords(title)
    
    for old in candidates:
        old_title = old.get("title", "")
        old_fp = old.get("story_fingerprint", canonical_story_fingerprint(old_title))
        if new_fp and old_fp and new_fp == old_fp:
            return True
        old_kws = extract_keywords(old_title)
        if new_kws and old_kws:
            overlap = len(new_kws & old_kws) / max(1, min(len(new_kws), len(old_kws)))
            if overlap >= 0.80:
                return True
    return False

def _get_recent_db_candidates(hours=48, limit=3000):
    if news_collection is None: return []
    cutoff = datetime.now() - timedelta(hours=hours)
    try:
        return list(news_collection.find({"timestamp": {"$gte": cutoff}}, {"title": 1, "story_fingerprint": 1}).sort("timestamp", -1).limit(limit))
    except Exception:
        return []

def claim_exact_news_in_db(title, article_link, event_id, source):
    if news_collection is None: return True
    dedupe_key = make_dedupe_key(title, article_link)
    if not dedupe_key: return True
    try:
        news_collection.insert_one({
            "dedupe_key": dedupe_key,
            "clean_content": clean_main_content(title),
            "title": title.strip(),
            "story_fingerprint": canonical_story_fingerprint(title),
            "event_id": event_id,
            "link": article_link,
            "source": source,
            "timestamp": datetime.now()
        })
        return True
    except Exception as e:
        if "duplicate key" in str(e).lower() or "E11000" in str(e):
            return False
        return True

def load_past_news_from_db():
    global SENT_NEWS_MEMORY, SENT_NEWS_META
    if news_collection is not None:
        try:
            past_docs = list(news_collection.find({}, {"title": 1, "event_id": 1, "story_fingerprint": 1}).sort("timestamp", -1).limit(3000))
            SENT_NEWS_MEMORY.clear()
            SENT_NEWS_META.clear()
            for doc in past_docs:
                title = doc.get("title", "")
                if title:
                    SENT_NEWS_MEMORY.append(title)
                    SENT_NEWS_META.append({
                        "title": title,
                        "event_id": doc.get("event_id", "GENERAL"),
                        "story_fingerprint": doc.get("story_fingerprint", canonical_story_fingerprint(title))
                    })
            print(f"✅ MongoDB నుండి {len(SENT_NEWS_MEMORY)} పాత వార్తలను RAM లోకి లోడ్ చేసాము సర్!")
        except Exception as e:
            print(f"⚠️ పాత వార్తలను లోడ్ చేయడంలో లోపం: {e}")

# =====================================================================
# 📰 Continuous Surveillance Worker (Global & Indian Brokerage Focus)
# =====================================================================
def live_research_surveillance_worker():
    global analysis_vault, SENT_NEWS_MEMORY
    print("\n🕵️‍♂️ [START] Institutional Research Surveillance v10.5 ప్రారంభమైంది...")
    print("🎯 కేవలం భారతీయ కంపెనీలపై విడుదలయ్యే అన్ని గ్లోబల్ & డొమెస్టిక్ బ్రోకరేజ్ రిపోర్ట్స్ నిఘాలో ఉన్నాయి!")

    # బ్రోకర్లను గూగుల్ న్యూస్ RSS క్వెరీల ద్వారా సమగ్రంగా సేకరించే సెటప్:
    # 1. in.investing.com (ఇండియన్ ఎడిషన్) మరియు moneycontrol.com లకు ప్రాధాన్యం
    # 2. భారత మార్కెట్ కీవర్డ్స్ (Nifty, Sensex, Target, Share, Buy, Sell) తప్పనిసరి
    macro_feeds = [
        (
            "Global_Investment_Banks_India_Reports",
            "https://news.google.com/rss/search?q=(%22Morgan+Stanley%22+OR+%22Goldman+Sachs%22+OR+%22Jefferies%22+OR+%22CLSA%22+OR+%22Nomura%22+OR+%22UBS%22+OR+%22Macquarie%22+OR+%22JPMorgan%22+OR+%22Bernstein%22+OR+%22HSBC%22+OR+%22Citi%22)+AND+(%22Target+Price%22+OR+%22Target%22+OR+%22initiates%22+OR+%22upgrades%22+OR+%22downgrades%22+OR+%22Buy%22+OR+%22shares%22)+AND+(%22Nifty%22+OR+%22Sensex%22+OR+%22India%22+OR+%22Dalal+Street%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ain.investing.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        (
            "Domestic_Top_Brokers_India",
            "https://news.google.com/rss/search?q=(%22Motilal+Oswal%22+OR+%22ICICI+Direct%22+OR+%22Kotak+Securities%22+OR+%22Kotak+Institutional%22+OR+%22HDFC+Securities%22+OR+%22Nuvama%22+OR+%22Axis+Capital%22+OR+%22Emkay%22+OR+%22Antique%22+OR+%22Prabhudas+Lilladher%22)+AND+(%22Target+Price%22+OR+%22Buy+Call%22+OR+%22Accumulate%22+OR+%22Stock+Recommendation%22+OR+%22Quarterly+Preview%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ain.investing.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        (
            "Moneycontrol_Brokerage_Radar",
            "https://news.google.com/rss/search?q=(%22Brokerage+Radar%22+OR+%22Brokerages+on%22+OR+%22Target+Price%22+OR+%22Buy+Call%22+OR+%22Stock+Radar%22)+AND+(site%3Amoneycontrol.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        (
            "Macro_RBI_Inflation_India",
            "https://news.google.com/rss/search?q=(%22RBI%22+OR+%22Repo+Rate%22+OR+%22Monetary+Policy%22+OR+%22India+GDP%22+OR+%22FII+Inflows%22+OR+%22DII%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ain.investing.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
    ]

    while True:
        try:
            collected_news = []
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }

            print(f"\n🔄 [{datetime.now().strftime('%H:%M:%S')}] బ్రోకరేజ్ & రీసెర్చ్ నివేదికలను సేకరిస్తున్నాము...")
            for source_name, url in macro_feeds:
                for attempt in range(1, 4):
                    try:
                        response = requests.get(url, headers=headers, timeout=25)
                        if response.status_code == 200:
                            root = ET.fromstring(response.content)
                            items = root.findall('.//item')
                            # మునుపటి 8 కి బదులు 25 ఐటమ్స్ చదివి అన్ని బ్రోకర్లను కవర్ చేస్తాము
                            for item in items[:25]:
                                title = item.find('title').text or ""
                                desc = item.find('description').text or ""
                                link = item.find('link').text or ""
                                clean_desc = re.sub('<[^<]+?>', '', desc)
                                full_text = f"{title} {clean_desc}".strip()
                                publisher = detect_allowed_publisher(title, link, clean_desc)

                                if not publisher:
                                    continue

                                # 🛡️ కేవలం భారతీయ కంపెనీలు/మార్కెట్ కి సంబంధించినదా కాదా అని ఫిల్టర్ చేయడం
                                if not is_strictly_indian_equity(title, clean_desc):
                                    continue

                                collected_news.append((publisher, title, full_text, link))
                            break
                    except Exception as feed_err:
                        print(f"⚠️ RSS Fetch warning ({source_name}): {feed_err}")
                        time.sleep(2)

            if not collected_news:
                time.sleep(180)
                continue

            # డూప్లికేషన్ చెక్
            db_candidates = _get_recent_db_candidates(hours=48)
            all_candidates = db_candidates + SENT_NEWS_META

            for source, raw_title, news_text, article_link in collected_news:
                if is_duplicate_story(raw_title, all_candidates):
                    continue

                article_type = detect_article_type(raw_title)
                news_category = classify_news(raw_title)
                event_id, impacted_assets, impacted_sectors, impacted_stocks = detect_event_and_impact(
                    raw_title, news_category, article_type
                )

                if not claim_exact_news_in_db(raw_title, article_link, event_id, source):
                    continue

                record = {
                    "title": raw_title,
                    "event_id": event_id,
                    "story_fingerprint": canonical_story_fingerprint(raw_title)
                }
                SENT_NEWS_META.append(record)
                SENT_NEWS_MEMORY.append(raw_title)
                all_candidates.append(record)

                if len(SENT_NEWS_MEMORY) > 3000: SENT_NEWS_MEMORY.pop(0)
                if len(SENT_NEWS_META) > 3000: SENT_NEWS_META.pop(0)

                safe_title = clean_for_html(raw_title)
                safe_source = clean_for_html(source)
                market_score, research_score = calculate_dynamic_scores(raw_title, article_type, news_category, source, event_id)
                priority = get_priority(market_score, research_score, article_type)

                try:
                    telugu_title = GoogleTranslator(source='auto', target='te').translate(raw_title)
                    safe_telugu_title = clean_for_html(telugu_title)
                except Exception:
                    safe_telugu_title = "తెలుగు శీర్షిక అందుబాటులో లేదు"

                unique_id = int(time.time() * 1000)
                analyze_btn_id = f"ai_analyze_{unique_id}"

                analysis_vault[analyze_btn_id] = {
                    "title": safe_title,
                    "telugu_title": safe_telugu_title,
                    "source": safe_source,
                    "full_text": news_text,
                    "link": article_link,
                    "unique_id": unique_id,
                    "event_id": event_id,
                    "article_type": article_type,
                    "category": news_category,
                    "impacted_assets": impacted_assets,
                    "impacted_sectors": impacted_sectors,
                    "impacted_stocks": impacted_stocks,
                    "market_score": market_score,
                    "research_score": research_score,
                    "priority": priority
                }

                short_telegram_msg = f"{priority}\n\n" \
                                   f"🔑 <b>Event ID:</b> #{event_id}\n" \
                                   f"📝 <b>రిపోర్ట్ రకం:</b> {article_type}\n" \
                                   f"🌍 <b>విభాగం:</b> {news_category}\n" \
                                   f"🎯 <b>ప్రభావిత అసెట్స్:</b> {impacted_assets}\n" \
                                   f"🏭 <b>కీలక రంగాలు:</b> {impacted_sectors}\n" \
                                   f"📈 <b>ఫోకస్ స్టాక్స్:</b> {impacted_stocks}\n" \
                                   f"📊 <b>Market Impact:</b> {market_score}/100 | 📚 <b>Research Value:</b> {research_score}/100\n\n" \
                                   f"🗞️ <b>శీర్షిక:</b> {safe_title}\n" \
                                   f"🔄 <b>తెలుగు:</b> {safe_telugu_title}\n\n" \
                                   f"🌐 <b>మూలం:</b> {safe_source}\n" \
                                   f"🔗 <b>పూర్తి ఆర్టికల్:</b> <a href='{article_link}'>ఇక్కడ క్లిక్ చేసి చదవండి</a>"

                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton(text="🧠 Analyze with AI", callback_data=analyze_btn_id))
                markup.add(InlineKeyboardButton(text="🌐 Read Full Article", url=article_link))

                try:
                    bot.send_message(YOUR_TELEGRAM_CHAT_ID, short_telegram_msg, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
                except Exception as send_err:
                    print(f"⚠️ టెలిగ్రామ్ మెసేజ్ పంపడంలో లోపం: {send_err}")

                time.sleep(2)

        except Exception as e:
            print(f"⚠️ నిఘా లూప్‌లో తాత్కాలిక ఎర్రర్: {e}")
            time.sleep(10)

        time.sleep(180)

# =====================================================================
# 🧠 జెమిని AI విశ్లేషణ (Indian Stock Market Focused)
# =====================================================================
def run_gemini_analysis(news_item):
    if not client: return None

    news_text = news_item.get('full_text', '')
    event_id = news_item.get('event_id', 'GENERAL')
    article_type = news_item.get('article_type', 'Update')
    category = news_item.get('category', 'General')
    assets = news_item.get('impacted_assets', 'N/A')
    sectors = news_item.get('impacted_sectors', 'N/A')
    stocks = news_item.get('impacted_stocks', 'N/A')
    m_score = news_item.get('market_score', 50)
    r_score = news_item.get('research_score', 40)

    prompt = f"""వార్త ప్రాథమిక వివరాలు:
- Event ID: #{event_id}
- Report Type: {article_type}
- Category: {category}
- Impacted Assets: {assets}
- Impacted Sectors: {sectors}
- Focus Stocks: {stocks}
- Market Impact: {m_score}/100
- Research Score: {r_score}/100

మీరు భారతీయ స్టాక్ మార్కెట్లో 50+ సంవత్సరాల అనుభవం ఉన్న ఒక లెజెండరీ చీఫ్ ఇన్వెస్ట్‌మెంట్ ఆఫీసర్ (CIO) మరియు గ్లోబల్ మ్యాక్రో స్ట్రాటజిస్ట్.
భారతీయ కంపెనీలపై విడుదలైన ఈ బ్రోకరేజ్ / రీసెర్చ్ నివేదికను విశ్లేషించండి:
{news_text}

నియమాలు:
1. కేవలం భారతీయ స్టాక్ మార్కెట్ (NSE/BSE/Nifty) దృష్టికోణంలోనే ఆలోచించండి. బ్రోకరేజ్ సంస్థ ఇచ్చిన టార్గెట్ ప్రైస్, రేటింగ్ (Buy/Sell/Hold/Overweight), రెవెన్యూ అంచనాలు, వ్యాల్యుయేషన్లు స్పష్టంగా విశ్లేషించండి.
2. రాబోయే రోజుల్లో DIIs, FIIs ఇన్వెస్ట్‌మెంట్ సెంటిమెంట్ మరియు రిటైల్ ఇన్వెస్టర్లు తీసుకోవలసిన జాగ్రత్తలను వివరించండి.
3. కల్పిత నంబర్లు సృష్టించవద్దు.

OUTPUT FORMAT:
[ONE_LINE]
(కేవలం ఒకే ఒక్క లైన్‌లో సంక్షిప్త సారాంశం రాయండి)

[DEEP_ANALYSIS]
(పూర్తి సుదీర్ఘమైన స్పష్టమైన తెలుగు విశ్లేషణను ఒక సీనియర్ ఫండ్ మేనేజర్ దృక్కోణంలో ఇక్కడ సమగ్రంగా రాయండి సర్)"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config={"tools": [{"google_search": {}}]}
        )
        return response.text.strip()
    except Exception as err:
        print(f"⚠️ Gemini API ఎర్రర్: {err}")
        return None

# =====================================================================
# 🚀 మాస్టర్ కమాండ్ సెంటర్ & బాట్ ఈవెంట్స్
# =====================================================================
if __name__ == "__main__":
    try:
        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(2)
    except Exception:
        pass

    load_past_news_from_db()

    start_msg = "🤖 <b>Institutional Research Terminal Bot v10.5 సిద్ధంగా ఉంది సర్!</b>\n\n" \
                "🇮🇳 <b>Focus:</b> కేవలం భారతీయ కంపెనీలపై విడుదలయ్యే అన్ని గ్లోబల్ & డొమెస్టిక్ బ్రోకర్ల రీసెర్చ్ రిపోర్ట్స్ మాత్రమే లైవ్ ట్రాక్ అవుతాయి!"
    
    try: bot.send_message(YOUR_TELEGRAM_CHAT_ID, start_msg, parse_mode="HTML")
    except: pass

    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=live_research_surveillance_worker, daemon=True).start()

    @bot.message_handler(commands=['help', 'start'])
    def send_command_list(message):
        try:
            help_text = "🤖 <b>ఇండియన్ ఈక్విటీస్ బ్రోకరేజ్ రీసెర్చ్ బాట్:</b>\n\n" \
                        "ఈ బాట్ భారతీయ కంపెనీలపై విడుదలయ్యే Morgan Stanley, Goldman Sachs, Jefferies, CLSA, Motilal Oswal, ICICI Direct, Kotak మొదలైన అన్ని ప్రముఖ సంస్థల టార్గెట్ ప్రైస్‌లు మరియు రీసెర్చ్ రిపోర్ట్స్‌ను లైవ్ లో పంపుతుంది."
            bot.send_message(message.chat.id, help_text, parse_mode="HTML")
        except Exception as e:
            report_error_to_telegram("Command Handler", str(e))

    @bot.callback_query_handler(func=lambda call: True)
    def callback_listener(call):
        global analysis_vault
        msg_key = call.data
        
        if msg_key.startswith("ai_analyze_"):
            if msg_key in analysis_vault:
                vault_item = analysis_vault[msg_key]
                bot.answer_callback_query(call.id, text="⏳ AI విశ్లేషిస్తోంది... దయచేసి వేచి ఉండండి...", show_alert=False)

                agent_output = run_gemini_analysis(vault_item)
                
                if agent_output and "[DEEP_ANALYSIS]" in agent_output:
                    parts = agent_output.split("[DEEP_ANALYSIS]")
                    one_line_part = parts[0].replace("[ONE_LINE]", "").strip()
                    deep_analysis_part = parts[1].strip()
                    safe_one_line = clean_for_html(one_line_part)

                    def split_analysis(text, size=3500):
                        return [text[i:i+size] for i in range(0, len(text), size)]

                    report_parts = split_analysis(deep_analysis_part)
                    unique_id = vault_item['unique_id']
                    view_id = f"view_{unique_id}"
                    back_id = f"back_{unique_id}"

                    short_telegram_msg = f"📢 <b>రీసెర్చ్ టీమ్ లైవ్ అలర్ట్</b>\n\n" \
                                         f"🔑 <b>Event ID:</b> #{vault_item.get('event_id', 'GENERAL')}\n" \
                                         f"🗞️ <b>వార్త:</b> {vault_item['title']}\n" \
                                         f"🌐 <b>మూలం:</b> {vault_item['source']}\n" \
                                         f"🔗 <b>మూల వార్త:</b> <a href='{vault_item.get('link', '#')}'>ఇక్కడ క్లిక్ చేయండి</a>\n\n" \
                                         f"💡 <b>క్విక్ వ్యూ:</b> {safe_one_line}"

                    analysis_vault[view_id] = {
                        "title": vault_item['title'],
                        "source": vault_item['source'],
                        "link": vault_item.get('link', '#'),
                        "parts": report_parts,
                        "original_text": short_telegram_msg,
                        "back_key": back_id
                    }
                    analysis_vault[back_id] = view_id

                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton(text="🔎 పూర్తి విశ్లేషణ చదవండి (Read Full View)", callback_data=f"page_{unique_id}_0"))
                    markup.add(InlineKeyboardButton(text="🌐 Read Full Article", url=vault_item.get('link', '#')))

                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=short_telegram_msg,
                        reply_markup=markup,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
                else:
                    markup = InlineKeyboardMarkup()
                    markup.add(InlineKeyboardButton(text="🌐 Read Full Article", url=vault_item.get('link', '#')))
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=f"📢 <b>మార్కెట్ లైవ్ వార్త</b>\n\n🗞️ {vault_item['title']}\n\n❌ <i>AI విశ్లేషణ ప్రస్తుతం అందుబాటులో లేదు.</i>",
                        reply_markup=markup,
                        parse_mode="HTML"
                    )

        elif msg_key.startswith("page_"):
            parts_key = msg_key.split("_")
            unique_id = parts_key[1]
            current_page = int(parts_key[2])
            vault_id = f"view_{unique_id}"

            if vault_id in analysis_vault:
                vault_data = analysis_vault[vault_id]
                report_parts = vault_data["parts"]
                total_pages = len(report_parts)
                page_content = report_parts[current_page]

                full_report = f"📊 <b>పూర్తి రీసెర్చ్ నివేదిక (Page {current_page + 1}/{total_pages})</b>\n\n" \
                              f"🗞 <b>వార్త:</b> {vault_data['title']}\n" \
                              f"🌐 <b>మూలం:</b> {vault_data['source']}\n" \
                              f"🔗 <b>మూల వార్త:</b> <a href='{vault_data.get('link', '#')}'>ఇక్కడ క్లిక్ చేయండి</a>\n" \
                              f"--------------------------------------------------\n\n" \
                              f"{page_content.strip()}"

                markup = InlineKeyboardMarkup()
                row_btns = []
                if current_page > 0:
                    row_btns.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"page_{unique_id}_{current_page - 1}"))
                if current_page < total_pages - 1:
                    row_btns.append(InlineKeyboardButton(text="➡️ Next", callback_data=f"page_{unique_id}_{current_page + 1}"))
                if row_btns:
                    markup.row(*row_btns)

                markup.add(InlineKeyboardButton(text="🏠 Back to Alert", callback_data=vault_data['back_key']))
                markup.add(InlineKeyboardButton(text="🌐 Read Full Article", url=vault_data.get('link', '#')))

                bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=full_report, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

        elif msg_key.startswith("back_"):
            if msg_key in analysis_vault:
                view_key = analysis_vault[msg_key]
                if view_key in analysis_vault:
                    vault_data = analysis_vault[view_key]
                    parts_key = view_key.split("_")[1]
                    original_markup = InlineKeyboardMarkup()
                    original_markup.add(InlineKeyboardButton(text="🔎 పూర్తి విశ్లేషణ చదవండి (Read Full View)", callback_data=f"page_{parts_key}_0"))
                    original_markup.add(InlineKeyboardButton(text="🌐 Read Full Article", url=vault_data.get('link', '#')))
                    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=vault_data['original_text'], reply_markup=original_markup, parse_mode="HTML", disable_web_page_preview=True)

    bot.infinity_polling()
