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
    return "🤖 Institutional Research Terminal Bot v10.4 is running 24/7 on Render!"

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
    print("ℹ️ MONGO_URI ఇవ్వలేదు — MongoDB లేకుండా Strong RAM + Batch Deduplication modeలో నడుస్తోంది సర్!")

analysis_vault = {}
SENT_NEWS_MEMORY = []
SENT_NEWS_META = []  
RECENT_EVENT_IDS = {} 

def report_error_to_telegram(error_location, error_msg):
    if any(err in str(error_msg) for err in ["10054", "Connection aborted", "ConnectionResetError", "timed out", "Max retries exceeded"]):
        print(f"⚠️ [NETWORK NOTICE Ignored for Telegram] Location: {error_location} | Error: {error_msg}")
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
        print(f"❌ ఎర్రర్ రిపోర్ట్ టెలిగ్రామ్‌కు పంపడంలో లోపం: {telegram_err}")

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

def detect_article_type(title):
    title_lower = title.lower()
    if any(k in title_lower for k in [
        'rbi keeps', 'rbi policy', 'repo rate', 'fed cuts', 'fed hikes', 'rate cut', 'rate hike', 
        'monetary policy', 'fomc', 'ecb', 'boj', 'pboc', 'rba', 'interest rate',
        'government approves', 'cabinet clears', 'sebi bans', 'pli scheme', 'qco regime'
    ]):
        return "Policy Decision"
    elif any(k in title_lower for k in ['what is', 'what are', 'how do', 'how to', 'why is', 'why are', 'explained', 'explainer', 'guide']):
        return "Explainer"
    elif any(k in title_lower for k in ['interview', 'daily voice', 'fund manager outlook', 'in conversation with', 'speaks to']):
        return "Interview"
    elif any(k in title_lower for k in ['opinion', 'column', 'perspective', 'viewpoint', 'editorial']):
        return "Opinion"
    elif any(k in title_lower for k in ['sources say', 'exclusive', 'deep dive', 'probe', 'study', 'institutional research', 'detailed analysis']):
        return "Research"
    elif any(k in title_lower for k in ['breaking', 'live', 'imposes', 'slaps', 'acquires', 'merger', 'outbreak of war', 'airstrike']):
        return "Breaking"
    else:
        return "Update"

def classify_news(title):
    title_lower = title.lower()
    if any(k in title_lower for k in ['rbi', 'fed', 'fomc', 'ecb', 'boj', 'repo rate', 'monetary policy', 'policy', 'qco', 'pli scheme']):
        return "Policy & Central Banks"
    elif any(k in title_lower for k in ['gdp', 'inflation', 'cpi', 'ppi', 'fiscal deficit', 'jobs data', 'unemployment']):
        return "Macro Economics"
    elif any(k in title_lower for k in ['war', 'tariff', 'sanctions', 'geopolitical', 'middle east', 'trade war', 'brics']):
        return "Geopolitics & Trade"
    elif any(k in title_lower for k in ['crude', 'oil', 'gold', 'silver', 'brent', 'opec', 'gas', 'metals']):
        return "Commodities"
    elif any(k in title_lower for k in ['rupee', 'dollar', 'forex', 'currency', 'yield', 'bond', 'treasury']):
        return "Currencies & Bonds"
    elif any(k in title_lower for k in ['acquisition', 'merger', 'stake', 'buyout', 'ipo', 'earnings', 'q1', 'q2', 'q3', 'q4', 'profit', 'revenue']):
        return "Corporate & Earnings"
    elif any(k in title_lower for k in ['ai', 'tech', 'semiconductor', 'data center', 'nasdaq', 'software']):
        return "Technology & AI"
    elif any(k in title_lower for k in ['pharma', 'health', 'fda', 'drug', 'generic']):
        return "Healthcare & Pharma"
    elif any(k in title_lower for k in ['solar', 'renewable', 'ev', 'green energy', 'power', 'power grid']):
        return "Energy & Clean Tech"
    elif any(k in title_lower for k in ['auto', 'banking', 'defence', 'railway', 'realty', 'fmcg']):
        return "Sectoral Trends"
    else:
        return "General Market"

def detect_event_and_impact(title, category, article_type):
    title_lower = title.lower()
    if any(k in title_lower for k in ['rbi', 'repo rate', 'monetary policy', 'governor']):
        event_id = "RBI_MONETARY_POLICY"
    elif any(k in title_lower for k in ['fed', 'fomc', 'powell', 'us inflation', 'us cpi']):
        event_id = "US_FED_POLICY"
    elif any(k in title_lower for k in ['tariff', 'import duty', 'export duty', 'trade war']):
        event_id = "GLOBAL_TRADE_WAR"
    elif any(k in title_lower for k in ['crude', 'oil', 'brent', 'opec', 'petrol', 'diesel']):
        event_id = "GLOBAL_OIL_SHOCK"
    elif any(k in title_lower for k in ['sebi', 'penalty', 'ban', 'regulations']):
        event_id = "SEBI_REGULATORY_ACTION"
    elif any(k in title_lower for k in ['earnings', 'results', 'profit', 'net profit', 'revenue', 'q1', 'q2', 'q3', 'q4']):
        words = [w for w in title_lower.split() if w not in STORY_STOPWORDS and len(w) > 3]
        company_tag = words[0].upper() if words else "CORP"
        event_id = f"EARNINGS_{company_tag}"
    elif any(k in title_lower for k in ['war', 'conflict', 'airstrike', 'geopolitical']):
        event_id = "GEOPOLITICAL_CONFLICT"
    else:
        clean_words = [w for w in re.findall(r'[a-z0-9]+', title_lower) if w not in STORY_STOPWORDS and len(w) > 3]
        event_id = f"EVT_{'_'.join(clean_words[:2])}".upper() if clean_words else "GENERAL_MARKET_UPDATE"

    impacted_assets = set()
    impacted_sectors = set()
    impacted_stocks = set()
    
    if 'tariff' in title_lower or 'trade' in title_lower:
        impacted_assets.update(['USD/INR', 'DXY', 'Export Index'])
        impacted_sectors.update(['IT', 'Pharma', 'Textiles', 'Auto Component'])
        impacted_stocks.update(['TCS', 'Infosys', 'Sun Pharma', 'Tata Motors'])
    elif 'crude' in title_lower or 'oil' in title_lower:
        impacted_assets.update(['Brent Crude', 'USD/INR'])
        impacted_sectors.update(['Paints', 'Aviation', 'OMCs', 'Tyres'])
        impacted_stocks.update(['Reliance', 'BPCL', 'HPCL', 'Asian Paints', 'Indigo'])
    elif 'rbi' in title_lower or 'fed' in title_lower:
        impacted_assets.update(['Nifty', 'BankNifty', 'India10Y Yield'])
        impacted_sectors.update(['Banking', 'NBFCs', 'Real Estate'])
        impacted_stocks.update(['HDFC Bank', 'SBI', 'ICICI Bank', 'Bajaj Finance'])
    else:
        impacted_assets.add('Nifty 50')
        impacted_sectors.add(category)
        impacted_stocks.add('Broad Market')

    return event_id, ", ".join(list(impacted_assets)[:3]), ", ".join(list(impacted_sectors)[:3]), ", ".join(list(impacted_stocks)[:4])

SOURCE_CONFIG = {
    "regulators": {
        "keywords": ["rbi", "fed", "federal reserve", "sebi", "sec", "imf", "world bank", "ecb", "boj", "boe"],
        "market": 10, "research": 10
    },
    "global_wires": {
        "keywords": ["reuters", "bloomberg", "wsj", "wall street journal", r"\bft\b", "financial times"],
        "market": 8, "research": 8
    },
    "national_media": {
        "keywords": ["mint", "livemint", "businessline", "economic times", r"\bet\b", "etmarkets", "cnbc", "guardian", "times of india", "ndtv"],
        "market": 5, "research": 5
    },
    "aggregators": {
        "keywords": ["moneycontrol"],
        "market": 3, "research": 3
    }
}

EVENT_WEIGHTS = {
    "#RBI_MONETARY_POLICY": {"market": 20, "research": 20},
    "#US_FED_POLICY": {"market": 20, "research": 20},
    "#GLOBAL_OIL_SHOCK": {"market": 18, "research": 15},
    "#GLOBAL_TRADE_WAR": {"market": 18, "research": 15},
    "#CYBER_ATTACK": {"market": 15, "research": 12},
    "#COMPANY_EARNINGS": {"market": 10, "research": 10}
}

INDEX_WEIGHTS = {
    "nifty_heavyweights": {
        "keywords": ["reliance", "hdfc bank", "tcs", "infosys", "icici bank", "bharti airtel", "itc", "l&t", "sbi", "axis bank", "adani ports"],
        "market": 10, "research": 6
    },
    "midcap_leaders": {
        "keywords": ["coforge", "inox wind", "suzlon", "persistent", "polycab", "swiggy", "cartrade"],
        "market": 5, "research": 4
    }
}

GEO_SCOPE_WEIGHTS = {
    "india_direct": {
        "keywords": ["rbi", "reserve bank", "indian inflation", "cpi india", "rupee", "sebi", "nifty", "sensex"],
        "market": 15, "research": 15
    },
    "global_core": {
        "keywords": ["fed", "federal reserve", "us inflation", "us cpi", "brent crude", "wti", "us-iran war", "trade war"],
        "market": 10, "research": 10
    },
    "regional_indirect": {
        "keywords": ["australia inflation", "ecb", "boj", "bank of japan", "bank of england"],
        "market": 4, "research": 8
    }
}

def compress_score(score):
    if score <= 70:
        return score
    return 70 + (score - 70) * 0.6

def calculate_dynamic_scores(title, article_type, category, source=None, event_id=None, debug=False):
    title_lower = title.lower()
    source_lower = str(source).lower()
    category_lower = str(category).lower()
      
    m_score = 0
    r_score = 0
    reasons = []

    source_matched = False
    for group, cfg in SOURCE_CONFIG.items():
        for kw in cfg["keywords"]:
            pattern = kw if kw.startswith(r"\b") else r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, source_lower) or re.search(pattern, title_lower):
                m_score += cfg["market"]
                r_score += cfg["research"]
                reasons.append(f"+{cfg['market']}M/+{cfg['research']}R Source ({group.title()})")
                source_matched = True
                break
        if source_matched:
            break

    for geo_type, cfg in GEO_SCOPE_WEIGHTS.items():
        if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in cfg["keywords"]):
            m_score += cfg["market"]
            r_score += cfg["research"]
            reasons.append(f"+{cfg['market']}M/+{cfg['research']}R Geographic Scope ({geo_type.title()})")
            break

    for cap_type, cfg in INDEX_WEIGHTS.items():
        if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in cfg["keywords"]):
            m_score += cfg["market"]
            r_score += cfg["research"]
            reasons.append(f"+{cfg['market']}M/+{cfg['research']}R Index Weighting ({cap_type.title()})")
            break

    if 'macro' in category_lower or 'policy' in category_lower:
        m_score += 12; r_score += 15
    if 'corporate' in category_lower or 'earnings' in category_lower:
        m_score += 10; r_score += 8
    if 'commodities' in category_lower or 'energy' in category_lower:
        m_score += 8; r_score += 6

    price_action_kws = [
        'jump', 'jumps', 'jumped', 'rally', 'rallies', 'rallied', 
        'surge', 'surges', 'surged', 'soars', 'soared', 'hits high',
        'record high', 'all time high', 'lifetime high', '52-week high', 'hits upper circuit',
        'plunge', 'plunges', 'plunged', 'crash', 'crashes', 'crashed',
        'tumble', 'tumbles', 'tumbled', 'slips', 'slides', 'hits low',
        'record low', '52-week low', 'hits lower circuit'
    ]
      
    if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in price_action_kws):
        m_score += 15; r_score += 8

    earnings_kws = ['q1', 'q2', 'q3', 'q4', 'fy26', 'fy27', 'earnings', 'profit', 'pat', 'net profit', 'revenue', 'ebitda', 'margin']
    if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in earnings_kws):
        is_negative = any(k in title_lower for k in ['falls', 'drops', 'slides', 'declines', 'misses', 'down', 'loss', 'slips'])
        is_positive = any(k in title_lower for k in ['surges', 'rises', 'jumps', 'soars', 'doubles', 'up', 'grows'])
          
        if is_negative:
            m_score += 22; r_score += 10
        elif is_positive:
            m_score += 22; r_score += 18
        else:
            m_score += 12; r_score += 10

    shock_kws = ['bankruptcy', 'default', 'fraud', 'investigation', 'downgrade', 'guidance cut', 'sebi notice', 'nclt', 'insolvency', 'scam', 'penalty', 'investigates']
    if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in shock_kws):
        m_score += 28; r_score += 18

    macro_kws = ['rbi', 'fed', 'federal reserve', 'ecb', 'boe', 'boj', 'monetary policy', 'fomc', 'repo rate', 'cpi', 'wpi', 'inflation', 'gdp', 'crude oil', 'oil war', 'war']
    if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in macro_kws):
        m_score += 20; r_score += 15

    sentiment_kws = ['should you buy', 'target price', 'target of', 'brokerage', 'upside', 'gmp', 'ipo gmp', 'daily voice', 'interview', 'momentum', 'technical', 'breakout', 'accumulate', 'recommend']
    if any(k in title_lower for k in sentiment_kws):
        m_score += 10; r_score += 8

    research_kws = ['goldilocks', 'outlook', 'projections', 'structural reform', 'capital allocation', 'supply chain', 'unit economics', 'portfolio simplifying', 'strategy']
    if any(re.search(r'\b' + re.escape(k) + r'\b', title_lower) for k in research_kws):
        m_score += 2; r_score += 30

    match_pct = re.search(r'(\d+(?:\.\d+)?)\s*%', title_lower)
    if match_pct:
        pct = float(match_pct.group(1))
        if pct >= 20:
            m_score += 12; r_score += 8
        elif pct >= 10:
            m_score += 8; r_score += 6
        elif pct >= 5:
            m_score += 5; r_score += 4
        else:
            m_score += 2; r_score += 2

    if re.search(r'(\d+|\d+,\d+)\s*(crore|cr|billion|bln|million|mln|mn)\b', title_lower) or re.search(r'(\$|₹|usd|rs\.?|eur)\s*\d+', title_lower):
        m_score += 10; r_score += 6

    if event_id in EVENT_WEIGHTS:
        ew = EVENT_WEIGHTS[event_id]
        m_score += ew["market"]; r_score += ew["research"]
    elif event_id and event_id != "#GENERAL_MARKET_UPDATE":
        m_score += 8; r_score += 8
    else:
        fallback_macro_regex = r'\b(rbi|fed|cpi|crude oil|war|gdp|monetary policy)\b'
        if re.search(fallback_macro_regex, title_lower):
            m_score += 10; r_score += 8

    if article_type == "Breaking":
        m_score += 10; r_score += 2
    elif article_type == "Policy Decision":
        m_score += 12; r_score += 12
    elif article_type == "Explainer":
        m_score += 0; r_score += 22

    if any(k in title_lower for k in ['denies', 'unverified', 'fake news', 'myth', 'incorrect']):
        m_score = max(5, m_score - 20)
        r_score = max(10, r_score - 10)

    if any(k in title_lower for k in ['what is', 'meaning of', 'definition', 'encyclopedia', 'britannica']):
        m_score = min(15, m_score)

    comp_market = compress_score(m_score)
    comp_research = compress_score(r_score)

    final_market_score = int(max(5, min(100, round(comp_market))))
    final_research_score = int(max(10, min(100, round(comp_research))))

    return final_market_score, final_research_score

def get_priority(market_score, research_score, article_type):
    if article_type == "Policy Decision" or market_score >= 85:
        return "🚨 POLICY / HIGH IMPACT"
    elif article_type == "Explainer":
        return "📚 EXPLAINER"
    elif market_score >= 65 or research_score >= 70:
        return "⚡ HIGH"
    else:
        return "ℹ️ NORMAL"

STORY_STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'to', 'of', 'in', 'on', 'for', 'with',
    'from', 'by', 'as', 'at', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'says', 'said', 'say', 'amid', 'after', 'before', 'over', 'under', 'into',
    'may', 'might', 'could', 'would', 'will', 'new', 'latest', 'update', 'news',
    'report', 'reports', 'according', 'source', 'sources', 'today', 'now', 'live',
    'india', 'indian', 'market', 'markets', 'shares', 'stock', 'stocks', 'price',
    'prices', 'rises', 'rise', 'falls', 'fall', 'up', 'down', 'higher', 'lower',
    'likely', 'expects', 'expect', 'outlook', 'impact', 'amidst', 'here', 'how',
    'what', 'why', 'who', 'when', 'where', 'their', 'its', 'this', 'that'
}

FINANCE_CONTEXT_KWS = {
    'market', 'markets', 'stock', 'stocks', 'share', 'shares', 'equity', 'equities',
    'investor', 'investors', 'investment', 'fund', 'funds', 'mutual', 'portfolio',
    'trading', 'trade', 'brokerage', 'earnings', 'profit', 'revenue', 'ebitda',
    'company', 'companies', 'corporate', 'bank', 'banks', 'credit', 'loan',
    'rbi', 'fed', 'fomc', 'inflation', 'cpi', 'gdp', 'bond', 'yield', 'rupee',
    'dollar', 'forex', 'currency', 'tariff', 'opec', 'crude', 'oil', 'brent',
    'wti', 'gold', 'silver', 'copper', 'commodity', 'commodities', 'energy',
    'cyber', 'cybersecurity', 'data breach', 'hack', 'ransomware', 'nclt', 'sebi'
}

NOISE_KWS = {
    'cricket', 'ipl', 'football', 'soccer', 'tennis', 'movie', 'movies', 'film',
    'bollywood', 'celebrity', 'actor', 'actress', 'entertainment', 'tv show',
    'web series', 'recipe', 'travel', 'tourism', 'fashion', 'horoscope',
    'lottery', 'game', 'gaming', 'sports', 'match', 'player', 'concert'
}

COMMODITY_SOURCE_NAMES = {'global_commodities_energy'}
CYBER_SOURCE_NAMES = {'india_banking_arc_cyber'}

def normalize_story_text(text):
    text = str(text or '').lower()
    text = re.sub(r'https?://\S+|www\.\S+', ' ', text)
    text = re.sub(r'[^a-z0-9%₹$€£.\s-]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def extract_numbers(text):
    text = normalize_story_text(text)
    patterns = [
        r'\b\d+(?:\.\d+)?%\b',
        r'\b\d+(?:,\d{3})*(?:\.\d+)?\s*(?:crore|cr|billion|bln|million|mln|mn)\b',
        r'(?:₹|\$|€|£)\s*\d+(?:,\d{3})*(?:\.\d+)?',
        r'\bq[1-4]\b',
        r'\bfy\d{2}\b',
    ]
    found = set()
    for pattern in patterns:
        found.update(re.findall(pattern, text))
    return {re.sub(r'\s+', '', x) for x in found}

def extract_keywords(text):
    text = normalize_story_text(text)
    words = text.split()
    return {
        w for w in words
        if w not in STORY_STOPWORDS and len(w) > 2 and not w.replace('.', '').isdigit()
    }

def extract_anchor_tokens(text):
    kws = extract_keywords(text)
    anchor_words = {
        'rbi', 'fed', 'fomc', 'ecb', 'boj', 'pboc', 'opec', 'sebi', 'nclt',
        'tariff', 'sanctions', 'war', 'airstrike', 'acquisition', 'merger',
        'buyout', 'ipo', 'earnings', 'results', 'profit', 'revenue', 'ebitda',
        'dividend', 'guidance', 'downgrade', 'upgrade', 'default', 'fraud',
        'investigation', 'bankruptcy', 'insolvency', 'cyber', 'ransomware',
        'breach', 'inflation', 'cpi', 'gdp', 'crude', 'brent', 'wti', 'gold',
        'silver', 'copper', 'natural', 'gas', 'bond', 'yield', 'rupee', 'dollar'
    }
    return (kws & anchor_words) | {w for w in kws if len(w) >= 6}

def story_similarity(title_a, title_b):
    a = extract_keywords(title_a)
    b = extract_keywords(title_b)
    if not a or not b:
        return 0.0, set(), set()
    common = a & b
    containment = len(common) / max(1, min(len(a), len(b)))
    jaccard = len(common) / max(1, len(a | b))
    score = 0.65 * containment + 0.35 * jaccard
    return score, common, a | b

def detect_allowed_publisher(title, link="", description=""):
    blob = f"{title} {link} {description}".lower()
    if "investing.com" in blob or re.search(r"\binvesting\s*\.?\s*com\b", blob):
        return "Investing.com"
    if "moneycontrol.com" in blob or "moneycontrol" in blob:
        return "Moneycontrol.com"
    return ""

def is_relevant_news(source_name, title, description=''):
    text = normalize_story_text(f"{title} {description}")
    source_key = normalize_story_text(source_name).replace(' ', '_')
    noise_hits = [k for k in NOISE_KWS if k in text]
    finance_hits = [k for k in FINANCE_CONTEXT_KWS if k in text]

    if source_key in COMMODITY_SOURCE_NAMES:
        commodity_hits = [k for k in ('crude', 'oil', 'brent', 'wti', 'opec', 'gold', 'silver', 'copper', 'natural gas', 'lng', 'iron ore', 'coal') if k in text]
        if not commodity_hits or (noise_hits and not finance_hits):
            return False

    if source_key in CYBER_SOURCE_NAMES:
        cyber_hits = [k for k in ('arc', 'asset reconstruction', 'jc flowers', 'acre', 'data breach', 'cyber', 'ransomware', 'credit growth', 'credit card', 'bank credit', 'liquidity', 'nri deposit') if k in text]
        if not cyber_hits:
            return False

    if noise_hits and not finance_hits:
        return False
    return True

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

def _print_duplicate_rejection(reason, new_title, old_title="", event_id="", score=None,
                              new_source="", old_source=""):
    print("\n" + "=" * 78)
    print("🚫 [DUPLICATE REJECTED]")
    print(f"    Reason    : {reason}")
    print(f"    Event ID  : NOT GENERATED (duplicate checked first)")
    if score is not None:
        print(f"    Similarity: {score:.3f}")
    if new_source:
        print(f"    New source: {new_source}")
    if old_source:
        print(f"    Old source: {old_source}")
    print(f"    NEW       : {new_title}")
    if old_title:
        print(f"    EXISTING  : {old_title}")
    print("=" * 78 + "\n")

def _number_conflict(title_a, title_b):
    a = extract_numbers(title_a)
    b = extract_numbers(title_b)
    return bool(a and b and not (a & b))

def _same_story_score(title_a, title_b):
    score, common, union = story_similarity(title_a, title_b)
    return score, common, union

STORY_TOPIC_GROUPS = {
    "GDP": {"gdp", "gross domestic product", "economy expands", "economy growth"},
    "INFLATION": {"inflation", "cpi", "pce", "consumer prices", "price pressures"},
    "RATES_FED": {"fed", "fomc", "federal reserve", "interest rate", "rate cut", "rate hike", "policy rate"},
    "RATES_RBI": {"rbi", "repo rate", "monetary policy", "policy rate"},
    "GOLD": {"gold", "bullion"},
    "SILVER": {"silver"},
    "OIL": {"crude", "oil", "brent", "wti", "opec"},
    "MUTUAL_FUNDS": {"mutual fund", "mutual funds", "sip", "nfo", "fund scheme", "funds"},
    "EARNINGS": {"earnings", "results", "profit", "revenue", "ebitda", "net profit"},
    "TARIFF_TRADE": {"tariff", "trade war", "import duty", "export duty", "sanctions"},
    "SEBI": {"sebi", "f&o", "fno", "derivatives", "brokerage app"},
    "IPO": {"ipo", "initial public offering", "listing"},
    "MERGER": {"merger", "acquisition", "buyout", "stake"},
}

def detect_story_topics(text):
    t = normalize_story_text(text)
    topics = set()
    for topic, terms in STORY_TOPIC_GROUPS.items():
        if any(term in t for term in terms):
            topics.add(topic)
    return topics

def important_story_tokens(text):
    words = extract_keywords(text)
    generic = {
        "remains", "remain", "elevated", "push", "pushes", "pushing", "brightens",
        "brighten", "growth", "grows", "growing", "beats", "beat", "beating",
        "forecast", "expected", "expects", "outlook", "signals", "signal", "await",
        "awaits", "awaiting", "today", "daily", "voice", "current", "recent", "developments",
        "financial", "economic", "economics", "data", "index", "report", "reports",
        "says", "said", "according", "reveals", "revealed", "explains", "explains", "time",
        "buy", "sell", "dip", "rise", "rises", "fall", "falls", "higher", "lower", "amid",
        "energy", "costs", "prices", "price", "market", "markets", "traders", "trading"
    }
    return {w for w in words if w not in generic and len(w) >= 4}

def _extract_title_dates(title):
    s = title or ""
    patterns = [
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b",
        r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
        r"\b\d{4}-\d{1,2}-\d{1,2}\b",
    ]
    found = set()
    for p in patterns:
        found.update(x.lower() for x in re.findall(p, s, flags=re.I))
    return found

def _is_generic_title(title):
    toks = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    generic = {
        "stock", "stocks", "market", "markets", "today", "live", "news",
        "update", "updates", "latest", "moneycontrol", "investing", "india",
        "indian", "shares", "share", "business", "finance", "financial"
    }
    meaningful = toks - generic
    return len(meaningful) <= 3

def find_duplicate_reason(new_title, event_id=None, article_link="", source="",
                         candidates=None):
    new_title = (new_title or "").strip()
    new_fp = canonical_story_fingerprint(new_title)
    new_link = (article_link or "").strip().lower()
    new_dates = _extract_title_dates(new_title)
    new_numbers = extract_numbers(new_title)
    new_topics = detect_story_topics(new_title)
    new_important = important_story_tokens(new_title)
    new_anchors = extract_anchor_tokens(new_title)
    new_generic = _is_generic_title(new_title)

    action_terms = {
        "launches", "launched", "launch", "acquires", "acquired", "acquisition",
        "merges", "merged", "merger", "buys", "bought", "sells", "sold",
        "stake", "dividend", "downgrade", "upgrade", "cuts", "cut", "hikes",
        "hike", "holds", "held", "unchanged", "maintains", "maintained",
        "approves", "approved", "rejects", "rejected", "sanctions", "sanctioned",
        "penalty", "warns", "warning", "inflation", "gdp", "cpi", "pce",
        "repo", "tariff", "airstrike", "profit", "revenue", "earnings",
        "guidance", "default", "bankruptcy", "growth", "expands", "expanded",
        "rises", "rose", "falls", "fell", "surges", "surged", "tumbles", "tumbled"
    }
    new_actions = new_important & action_terms

    for old in candidates or []:
        old_title = str(old.get("title") or "").strip()
        if not old_title:
            continue

        old_link = str(old.get("link") or "").strip().lower()
        old_fp = old.get("story_fingerprint") or canonical_story_fingerprint(old_title)
        old_dates = _extract_title_dates(old_title)
        old_numbers = extract_numbers(old_title)
        old_topics = detect_story_topics(old_title)
        old_important = important_story_tokens(old_title)
        old_anchors = extract_anchor_tokens(old_title)
        old_actions = old_important & action_terms
        old_generic = _is_generic_title(old_title)

        if new_link and old_link and new_link == old_link:
            return True, "SAME_ARTICLE_URL", 1.0, old_title, old.get("source", "")

        if new_fp and old_fp and new_fp == old_fp:
            return True, "EXACT_CANONICAL_STORY", 1.0, old_title, old.get("source", "")

        score, _, _ = _same_story_score(new_title, old_title)

        if new_dates and old_dates and new_dates != old_dates:
            continue

        number_overlap = bool(new_numbers and old_numbers and (new_numbers & old_numbers))
        number_conflict = bool(new_numbers and old_numbers and not (new_numbers & old_numbers))
        topic_overlap = new_topics & old_topics
        important_overlap = len(new_important & old_important)
        anchor_overlap = len(new_anchors & old_anchors)
        action_overlap = len(new_actions & old_actions)

        if number_conflict:
            continue

        if new_generic or old_generic:
            if not (number_overlap and anchor_overlap >= 2 and score >= 0.55):
                continue

        if topic_overlap and number_overlap:
            if important_overlap >= 2 or anchor_overlap >= 2:
                return True, "SAME_TOPIC_SAME_KEY_FACT", score, old_title, old.get("source", "")

        if topic_overlap and action_overlap >= 1 and score >= 0.50:
            if important_overlap >= 3 or anchor_overlap >= 2:
                return True, "SAME_TOPIC_SAME_EVENT_ACTION", score, old_title, old.get("source", "")

        if score >= 0.78 and (important_overlap >= 3 or anchor_overlap >= 3):
            return True, "VERY_HIGH_STORY_SIMILARITY", score, old_title, old.get("source", "")

        if anchor_overlap >= 3 and number_overlap and score >= 0.30:
            return True, "SAME_ANCHORS_AND_FACTS", score, old_title, old.get("source", "")

    return False, "", 0.0, "", ""

def _get_recent_db_candidates(hours=48, limit=5000):
    if news_collection is None:
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    try:
        docs = list(
            news_collection.find(
                {"timestamp": {"$gte": cutoff}},
                {
                    "title": 1,
                    "event_id": 1,
                    "link": 1,
                    "source": 1,
                    "story_fingerprint": 1,
                    "timestamp": 1,
                }
            ).sort("timestamp", -1).limit(limit)
        )
        return docs
    except Exception as e:
        print(f"⚠️ MongoDB duplicate candidates read failed: {e}")
        return []

def is_same_event(new_title, event_id, article_link="", source="",
                  extra_candidates=None):
    candidates = []
    candidates.extend(_get_recent_db_candidates(hours=48, limit=5000))

    for meta in SENT_NEWS_META:
        candidates.append({
            "title": meta.get("title", ""),
            "event_id": meta.get("event_id", "GENERAL_MARKET_UPDATE"),
            "link": meta.get("link", ""),
            "source": meta.get("source", ""),
            "story_fingerprint": meta.get("story_fingerprint", ""),
        })

    if extra_candidates:
        candidates.extend(extra_candidates)

    seen = set()
    unique_candidates = []
    for item in candidates:
        key = (
            str(item.get("title", "")).strip().lower(),
            str(item.get("event_id", "")),
        )
        if key in seen or not key[0]:
            continue
        seen.add(key)
        unique_candidates.append(item)

    duplicate, reason, score, old_title, old_source = find_duplicate_reason(
        new_title=new_title,
        event_id=event_id,
        article_link=article_link,
        source=source,
        candidates=unique_candidates,
    )

    if duplicate:
        _print_duplicate_rejection(
            reason=reason,
            new_title=new_title,
            old_title=old_title,
            event_id=event_id,
            score=score,
            new_source=source,
            old_source=old_source,
        )
        return True

    print(f"✅ [UNIQUE STORY — DUPLICATE CHECK PASSED] | {source} | {new_title}")
    return False

def claim_exact_news_in_db(title, article_link, event_id, source):
    if news_collection is None:
        return True
    dedupe_key = make_dedupe_key(title, article_link)
    if not dedupe_key:
        return True
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
        if "E11000" in str(e) or "duplicate key" in str(e).lower():
            _print_duplicate_rejection(
                reason="MONGODB_CANONICAL_KEY_ALREADY_EXISTS",
                new_title=title,
                event_id=event_id,
                new_source=source,
            )
            return False
        return True

def load_past_news_from_db():
    global SENT_NEWS_MEMORY, SENT_NEWS_META
    if news_collection is not None:
        try:
            past_docs = list(
                news_collection.find(
                    {},
                    {
                        "title": 1,
                        "event_id": 1,
                        "timestamp": 1,
                        "story_fingerprint": 1,
                        "link": 1,
                        "source": 1,
                    }
                ).sort("timestamp", -1).limit(5000)
            )
            SENT_NEWS_MEMORY.clear()
            SENT_NEWS_META.clear()

            for doc in past_docs:
                title = doc.get("title", "")
                if title:
                    SENT_NEWS_MEMORY.append(title)
                    SENT_NEWS_META.append({
                        "title": title,
                        "event_id": doc.get("event_id", "GENERAL_MARKET_UPDATE"),
                        "timestamp": doc.get("timestamp", datetime.now()),
                        "story_fingerprint": doc.get(
                            "story_fingerprint",
                            canonical_story_fingerprint(title)
                        ),
                        "link": doc.get("link", ""),
                        "source": doc.get("source", ""),
                    })
            print(f"✅ MongoDB నుండి {len(SENT_NEWS_MEMORY)} పాత వార్తలను RAM లోకి లోడ్ చేసాము సర్!")
        except Exception as e:
            print(f"⚠️ పాత వార్తలను లోడ్ చేయడంలో లోపం: {e}")

# =====================================================================
# 📰 Continuous Macro & Sector Surveillance Team
# =====================================================================
def live_research_surveillance_worker():
    global analysis_vault, SENT_NEWS_MEMORY, RECENT_EVENT_IDS
    print("\n🕵️‍♂️ [START] Live Research Team నిరంతర నిఘా లూప్ ప్రారంభమైంది సర్...")
    
    print("🎯 SOURCE WHITELIST: Moneycontrol.com + Investing.com మాత్రమే")
    macro_feeds = [
        (
            "Moneycontrol_Insights_Via_Google",
            "https://news.google.com/rss/search?q=site:moneycontrol.com+intitle:(%22mutual+funds%22+OR+%22Monetary+Policy+Committee%22+OR+%22Credit+Growth%22+OR+%22Credit+Card%22+OR+%22Reserve+Bank+of+India%22+OR+%22Gold%22+OR+%22Silver%22+OR+%22market+outlook%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ainvesting.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        (
            "Global_Central_Banks_Macro",
            "https://news.google.com/rss/search?q=(%22Federal+Reserve%22+OR+%22FOMC%22+OR+%22European+Central+Bank%22+OR+%22ECB%22+OR+%22Bank+of+England%22+OR+%22Bank+of+Japan%22+OR+%22BOJ%22+OR+%22Peoples+Bank+of+China%22+OR+%22PBOC%22+OR+%22US+Inflation%22+OR+%22Personal+Consumption+Expenditures%22+OR+%22PCE%22+OR+%22Nonfarm+Payrolls%22+OR+%22Unemployment%22+OR+%22Treasury+Yield%22+OR+%22Dollar+Index%22+OR+%22DXY%22+OR+%22US+GDP%22+OR+%22China+Economy%22+OR+%22Trade+War%22+OR+%22Tariffs%22+OR+%22Recession%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ainvesting.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        (
            "Expert_Articles_Brokerage_Reports",
            "https://news.google.com/rss/search?q=(%22Daily+Voice%22+OR+%22Expert+View%22+OR+%22Market+Guru%22+OR+%22Brokerage+Report%22+OR+%22Stock+Strategy%22+OR+%22Investing+Strategy%22)+AND+(%22Moneycontrol%22+OR+%22Economic+Times%22+OR+%22Livemint%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ainvesting.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        (
            "Economy_Policy_Budget",
            "https://news.google.com/rss/search?q=(%22RBI%22+OR+%22Repo+Rate%22+OR+%22Inflation%22+OR+%22GDP%22+OR+%22Union+Budget%22+OR+%22Tax%22+OR+%22Fiscal+Deficit%22+OR+%22Monetary+Policy%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ainvesting.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        (
            "Brokerage_Recommendations",
            "https://news.google.com/rss/search?q=(%22Brokerage%22+OR+%22ICICI+Direct%22+OR+%22Motilal+Oswal%22+OR+%22Jefferies%22+OR+%22Target+Price%22+OR+%22Buy+Call%22+OR+%22Sell+Call%22+OR+%22Stock+Recommendation%22)+AND+(site%3Amoneycontrol.com+OR+site%3Ainvesting.com)&hl=en-IN&gl=IN&ceid=IN:en"
        ),
    ]
    
    while True:
        try:
            collected_news = []
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Connection": "keep-alive"
            }
            
            print(f"\n🔄 [{datetime.now().strftime('%H:%M:%S')}] Google News RSS → Moneycontrol + Investing.com వార్తలను సేకరిస్తున్నాను...")
            for source_name, url in macro_feeds:
                for attempt in range(1, 4):
                    try:
                        response = requests.get(url, headers=headers, timeout=25)
                        if response.status_code == 200:
                            root = ET.fromstring(response.content)
                            items = root.findall('.//item')
                            for item in items[:8]:
                                title = item.find('title').text or ""
                                desc = item.find('description').text or ""
                                link = item.find('link').text or ""
                                clean_desc = re.sub('<[^<]+?>', '', desc)
                                full_text = f"{title} {clean_desc}".strip()
                                publisher = detect_allowed_publisher(title, link, clean_desc)

                                if not publisher:
                                    continue

                                if full_text and is_relevant_news(source_name, title, clean_desc):
                                    collected_news.append((publisher, title, full_text, link))
                            break 
                    except Exception as feed_err:
                        print(f"⚠️ RSS Fetch warning ({source_name}): {feed_err}")
                        time.sleep(2)

            if not collected_news:
                time.sleep(300)
                continue

            filtered_batch = []
            batch_seen_records = []

            for source, raw_title, news_text, article_link in collected_news:
                if is_same_event(
                    raw_title,
                    event_id=None,
                    article_link=article_link,
                    source=source,
                    extra_candidates=batch_seen_records,
                ):
                    continue

                article_type = detect_article_type(raw_title)
                news_category = classify_news(raw_title)
                event_id, impacted_assets, impacted_sectors, impacted_stocks = detect_event_and_impact(
                    raw_title, news_category, article_type
                )

                record = {
                    "title": raw_title,
                    "event_id": event_id,
                    "link": article_link,
                    "source": source,
                    "story_fingerprint": canonical_story_fingerprint(raw_title),
                }

                batch_seen_records.append(record)
                filtered_batch.append((
                    source, raw_title, news_text, article_link, article_type,
                    news_category, event_id, impacted_assets, impacted_sectors, impacted_stocks
                ))

            print(
                f"📊 RSS Batch: {len(collected_news)} collected → "
                f"{len(filtered_batch)} unique → "
                f"{len(collected_news) - len(filtered_batch)} duplicates rejected"
            )

            for source, raw_title, news_text, article_link, article_type, news_category, event_id, impacted_assets, impacted_sectors, impacted_stocks in filtered_batch:
                if not claim_exact_news_in_db(raw_title, article_link, event_id, source):
                    continue

                SENT_NEWS_MEMORY.append(raw_title)
                SENT_NEWS_META.append({
                    "title": raw_title,
                    "event_id": event_id,
                    "timestamp": datetime.now(),
                    "story_fingerprint": canonical_story_fingerprint(raw_title),
                    "link": article_link,
                    "source": source,
                })

                if len(SENT_NEWS_MEMORY) > 5000:
                    SENT_NEWS_MEMORY.pop(0)
                if len(SENT_NEWS_META) > 5000:
                    SENT_NEWS_META.pop(0)

                safe_title = clean_for_html(raw_title)
                safe_source = clean_for_html(source)
                  
                market_score, research_score = calculate_dynamic_scores(
                    raw_title, article_type, news_category, source, event_id, debug=True
                )
                priority = get_priority(market_score, research_score, article_type)

                try:
                    telugu_title = GoogleTranslator(source='auto', target='te').translate(raw_title)
                    safe_telugu_title = clean_for_html(telugu_title)
                except Exception:
                    safe_telugu_title = "తెలుగు అనువాదం లభించలేదు"

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
                                   f"📝 <b>Article Type:</b> {article_type}\n" \
                                   f"🌍 <b>Category:</b> {news_category}\n" \
                                   f"🎯 <b>Impacted Assets:</b> {impacted_assets}\n" \
                                   f"🏭 <b>Key Sectors:</b> {impacted_sectors}\n" \
                                   f"📈 <b>Affected Stocks:</b> {impacted_stocks}\n" \
                                   f"📊 <b>Market Impact:</b> {market_score}/100 | 📚 <b>Research Value:</b> {research_score}/100\n\n" \
                                   f"🗞️ <b>శీర్షిక:</b> {safe_title}\n" \
                                   f"🔄 <b>తెలుగు:</b> {safe_telugu_title}\n\n" \
                                   f"🌐 <b>మూలం:</b> {safe_source}\n" \
                                   f"🔗 <b>మూల వార్త:</b> <a href='{article_link}'>ఇక్కడ క్లిక్ చేసి చదవండి</a>"

                markup = InlineKeyboardMarkup()
                analyze_btn = InlineKeyboardButton(text="🧠 Analyze with AI", callback_data=analyze_btn_id)
                link_btn = InlineKeyboardButton(text="🌐 Read Full Article", url=article_link)
                markup.add(analyze_btn)
                markup.add(link_btn)
                  
                try:
                    bot.send_message(YOUR_TELEGRAM_CHAT_ID, short_telegram_msg, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
                except Exception as send_err:
                    print(f"⚠️ టెలిగ్రామ్ మెసేజ్ పంపడంలో చిన్న వార్నింగ్: {send_err}")
                
                time.sleep(2)
              
        except Exception as e:
            print(f"⚠️ [LOOP NETWORK EXCEPTION] కనెక్షన్ తాత్కాలికంగా కట్ అయింది: {e}")
            time.sleep(10)
            continue
              
        time.sleep(300)

# =====================================================================
# 🧠 జెమిని AI విశ్లేషణ
# =====================================================================
def run_gemini_analysis(news_item):
    if not client:
        return None

    news_text = news_item.get('full_text', '')
    event_id = news_item.get('event_id', 'GENERAL')
    article_type = news_item.get('article_type', 'Update')
    category = news_item.get('category', 'General')
    assets = news_item.get('impacted_assets', 'N/A')
    sectors = news_item.get('impacted_sectors', 'N/A')
    stocks = news_item.get('impacted_stocks', 'N/A')
    m_score = news_item.get('market_score', 50)
    r_score = news_item.get('research_score', 40)

    prompt = f"""ముందుగా ఈ వార్త ప్రాథమిక వివరాలు:
- Event ID: #{event_id}
- Article Type: {article_type}
- Category: {category}
- Impacted Assets: {assets}
- Impacted Sectors: {sectors}
- Affected Stocks: {stocks}
- Market Impact Score: {m_score}/100
- Research Value Score: {r_score}/100

meru oka Senior Global Research Analyst Indian Stock Market mariyu Global Markets lo 50+ samvatsarala atyantha anubavam unna oka Market Legend meru. meru vandala kotla (Multi-Crore) institutional funds ni manage chese highly successful Professional Value Investor mariyu Macro Strategist . market cycles, sector rotations, mariyu smart money (FIIs/DIIs) yokka prathiey okka kadalikanu meru mundugaane anchana veyagalaru.

ee parinaamanni oka Top-Level Fund Manager mind-set tho chala pragalbhamga vishlesinchandi: {news_text}

CRITICAL RULES FOR GOOGLE SEARCH & ACCURACY:
1. USE LIVE SEARCH: ఈ వార్త నిజంగానే సెక్టార్ లేదా మార్కెట్ చేంజ్ తెచ్చేదైతే... వెంటనే గూగుల్ సెర్చ్ ఉపయోగించి ఆ సెక్టార్/మార్కెట్ యొక్క గత 2-3 సంవత్సరాల హిస్టరీ, ప్రస్తుత పరిస్థితి మరియు లేటెస్ట్ మేనేజ్‌మెంట్/గవర్నమెంట్ కామెంటరీ డేటాను సేకరించు.
2. FUTURE MARKET OUTLOOK: ఈ పరిణామం వల్ల రాబోయే రోజుల్లో FIIs, DIIs మరియు బిగ్ ప్లేయర్స్ సెంటిమెంట్ ఎలా ఉండబోతోంది, ఎలాంటి ట్రెండ్ రాబోతోంది అనేది స్పష్టంగా వివరించు.
3. NO HALLUCINATION: నీ సొంత ఊహలతో నంబర్లను లేదా పర్సంటేజీలను సృష్టించవద్దు. గూగుల్ సెర్చ్ లో దొరికిన కచ్చితమైన ఫ్యాక్ట్స్ ఆధారంగానే విశ్లేషణ రాయి.

OUTPUT FORMAT:
ముందుగా [ONE_LINE] అనే టాగ్ పెట్టి కేవలం ఒకే ఒక్క లైన్ లో క్విక్ విశ్లేషణ రాయి.
ఆ తర్వాత [DEEP_ANALYSIS] అనే టాగ్ పెట్టి, ఒక బిగ్ ఇన్వెస్టర్ మైండ్‌సెట్ తో దీని వెనుక ఉన్న అసలు కారణం ఏంటి, మార్కెట్/సెక్టార్/స్టాక్స్ పై దీని దీర్ఘకాలిక ప్రభావం ఎలా ఉంటుంది అనేది చాలా అద్భుతమైన, సుదీర్ఘమైన పూర్తి తెలుగు విశ్లేషణను కింద వివరించు సర్."""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=prompt,
            config={"tools": [{"google_search": {}}]}
        )
        return response.text.strip()
    except Exception as err:
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

    start_msg = "🤖 <b>Institutional Research Terminal Bot v10.4 సిద్ధంగా ఉంది సర్!</b>\n\n" \
                "🏆 <b>Smart 5-Step Deduplication Engine:</b> Active & Running!"
      
    try: bot.send_message(YOUR_TELEGRAM_CHAT_ID, start_msg, parse_mode="HTML")
    except: pass
      
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=live_research_surveillance_worker, daemon=True).start()

    @bot.message_handler(commands=['help', 'start'])
    def send_command_list(message):
        try:
            help_text = "🤖 <b>AI రీసెర్చ్ నిఘా బాట్ సర్:</b>\n\n" \
                        "ఈ బాట్ మార్కెట్ వార్తలను లైవ్‌లో పంపుతుంది. మీకు కావలసిన వార్త కింద ఉన్న 'Analyze with AI' బటన్ నొక్కితే AI విశ్లేషణ పొందుతారు."
            bot.send_message(message.chat.id, help_text, parse_mode="HTML")
        except Exception as e:
            report_error_to_telegram("Command Handler (Start/Help)", str(e))

    @bot.callback_query_handler(func=lambda call: True)
    def callback_listener(call):
        global analysis_vault
        msg_key = call.data
          
        if msg_key.startswith("ai_analyze_"):
            if msg_key in analysis_vault:
                vault_item = analysis_vault[msg_key]
                bot.answer_callback_query(call.id, text="⏳ AI విశ్లేషిస్తోంది... దయచేసి కొన్ని సెకన్లు వేచి ఉండండి...", show_alert=False)
                  
                bot.edit_message_text(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    text=f"{vault_item.get('priority', '📢')}\n\n"
                         f"🔑 <b>Event ID:</b> #{vault_item.get('event_id', 'GENERAL')}\n"
                         f"📝 <b>Article Type:</b> {vault_item.get('article_type', 'Update')}\n"
                         f"🌍 <b>Category:</b> {vault_item.get('category', 'General')}\n"
                         f"🎯 <b>Assets:</b> {vault_item.get('impacted_assets', 'N/A')}\n"
                         f"🏭 <b>Sectors:</b> {vault_item.get('impacted_sectors', 'N/A')}\n"
                         f"📈 <b>Affected Stocks:</b> {vault_item.get('impacted_stocks', 'N/A')}\n"
                         f"📊 <b>Market Impact:</b> {vault_item.get('market_score', 50)}/100\n\n"
                         f"🗞️ <b>శీర్షిక:</b> {vault_item['title']}\n"
                         f"🌐 <b>మూలం:</b> {vault_item['source']}\n"
                         f"🔗 <b>మూల వార్త:</b> <a href='{vault_item.get('link', '#')}'>ఇక్కడ క్లిక్ చేసి చదవండి</a>\n\n"
                         f"⏳ <i>జెమిని AI ప్రత్యక్ష మార్కెట్ డేటాతో విశ్లేషణ చేస్తోంది...</i>",
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                  
                agent_output = run_gemini_analysis(vault_item)
                  
                if agent_output and "[DEEP_ANALYSIS]" in agent_output:
                    parts = agent_output.split("[DEEP_ANALYSIS]")
                    one_line_part = parts[0].replace("[ONE_LINE]", "").replace("HIGH_IMPACT", "").strip()
                    deep_analysis_part = parts[1].strip()
                      
                    deep_analysis_part = re.sub(r'(\n\s*\d+[\.\)]\s*)', r'\n\n\1', deep_analysis_part)
                    deep_analysis_part = re.sub(r'(\n\s*\*+\s*)', r'\n\n\1', deep_analysis_part)
                    deep_analysis_part = re.sub(r'\n{3,}', '\n\n', deep_analysis_part)
                      
                    safe_one_line = clean_for_html(one_line_part)
                      
                    def split_analysis(text, size=3500):
                        return [text[i:i+size] for i in range(0, len(text), size)]
                      
                    report_parts = split_analysis(deep_analysis_part.strip())
                    unique_id = vault_item['unique_id']
                      
                    view_id = f"view_{unique_id}"
                    back_id = f"back_{unique_id}"
                      
                    short_telegram_msg = f"📢 <b>రీసెర్చ్ టీమ్ లైవ్ అలర్ట్</b>\n\n" \
                                           f"🔑 <b>Event ID:</b> #{vault_item.get('event_id', 'GENERAL')}\n" \
                                           f"🗞️ <b>వార్త శీర్షిక:</b> {vault_item['title']}\n" \
                                           f"🌐 <b>మూలం:</b> {vault_item['source']}\n" \
                                           f"🔗 <b>మూల వార్త:</b> <a href='{vault_item.get('link', '#')}'>ఇక్కడ క్లిక్ చేసి చదవండి</a>\n" \
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
                    view_btn = InlineKeyboardButton(text="🔎 పూర్తి విశ్లేషణ చదవండి (Read Full View)", callback_data=f"page_{unique_id}_0")
                    link_btn = InlineKeyboardButton(text="🌐 Read Full Article", url=vault_item.get('link', '#'))
                    markup.add(view_btn)
                    markup.add(link_btn)
                      
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
                    link_btn = InlineKeyboardButton(text="🌐 Read Full Article", url=vault_item.get('link', '#'))
                    markup.add(link_btn)
                      
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text=f"📢 <b>మార్కెట్ లైవ్ వార్త</b>\n\n"
                             f"🗞️ <b>శీర్షిక:</b> {vault_item['title']}\n"
                             f"🌐 <b>మూలం:</b> {vault_item['source']}\n"
                             f"🔗 <b>మూల వార్త:</b> <a href='{vault_item.get('link', '#')}'>ఇక్కడ క్లిక్ చేసి చదవండి</a>\n\n"
                             f"❌ <i>క్షమించండి, ప్రస్తుతం AI విశ్లేషణ అందుబాటులో లేదు లేదా లిమిట్ దాటింది.</i>",
                        reply_markup=markup,
                        parse_mode="HTML",
                        disable_web_page_preview=True
                    )
            else:
                try: bot.answer_callback_query(call.id, text="❌ ఈ వార్త సమాచారం మెమొరీలో లేదు.", show_alert=True)
                except: pass

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
                              f"🔗 <b>మూల వార్త:</b> <a href='{vault_data.get('link', '#')}'>ఇక్కడ క్లిక్ చేసి చదవండి</a>\n" \
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
