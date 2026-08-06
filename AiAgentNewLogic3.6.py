import os
import time
import requests
import xml.etree.ElementTree as ET
import re
import threading
from datetime import datetime
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
    return "🤖 Institutional Research Terminal Bot v10.0 is running 24/7 on Render!"

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

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# =====================================================================
# 🥭 MongoDB డేటాబేస్ సెటప్
# =====================================================================
print("🥭 MongoDB కి కనెక్ట్ అవుతున్నాము సర్...")
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client["TradingBotDB"]  
    news_collection = db["processed_news"]  
    news_collection.create_index("clean_content", unique=False)
    print("✅ MongoDB తో కనెక్షన్ సక్సెస్ అయింది సర్!")
except Exception as mongo_init_err:
    print(f"❌ MongoDB స్టార్టింగ్ లోనే ఫెయిల్ అయింది: {mongo_init_err}")
    db = None
    news_collection = None

analysis_vault = {}
SENT_NEWS_MEMORY = []
RECENT_EVENT_IDS = {} 

def report_error_to_telegram(error_location, error_msg):
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
    if 'tariff' in title_lower or 'trade' in title_lower:
        event_id = "TRADE_EXPLAINER" if article_type == "Explainer" else ("TRADE_RESEARCH" if article_type == "Research" else "GLOBAL_TRADE_WAR")
    elif 'rbi' in title_lower or 'repo rate' in title_lower:
        event_id = "RBI_POLICY_EXPLAINER" if article_type == "Explainer" else "RBI_MONETARY_POLICY"
    elif 'fed' in title_lower or 'fomc' in title_lower:
        event_id = "FED_POLICY_EXPLAINER" if article_type == "Explainer" else "US_FED_POLICY"
    elif 'japan' in title_lower and 'inflation' in title_lower:
        event_id = "BOJ_JAPAN_INFLATION"
    elif any(k in title_lower for k in ['crude', 'oil', 'brent', 'opec']):
        event_id = "GLOBAL_OIL_SHOCK"
    elif any(k in title_lower for k in ['war', 'airstrike', 'middle east']):
        event_id = "GEOPOLITICAL_CONFLICT"
    elif 'pli scheme' in title_lower:
        event_id = "GOVT_PLI_SCHEME"
    elif any(k in title_lower for k in ['acquisition', 'merger', 'buyout']):
        event_id = "CORPORATE_M_AND_A"
    else:
        event_id = "GENERAL_MARKET_UPDATE"

    impacted_assets = set()
    impacted_sectors = set()
    impacted_stocks = set()
    
    if any(k in title_lower for k in ['tariff', 'trade']):
        impacted_assets.update(['USD/INR', 'DXY', 'Export Index'])
        impacted_sectors.update(['IT', 'Pharma', 'Textiles', 'Auto Component'])
        impacted_stocks.update(['TCS', 'Infosys', 'Sun Pharma', 'Tata Motors', 'Gokaldas Exports'])

    if any(k in title_lower for k in ['crude', 'oil', 'brent', 'opec']):
        impacted_assets.update(['Brent Crude', 'USD/INR', 'India10Y Yield'])
        impacted_sectors.update(['Paints', 'Aviation', 'OMCs', 'Tyres', 'Chemicals'])
        impacted_stocks.update(['Reliance', 'BPCL', 'HPCL', 'IOC', 'Asian Paints', 'Indigo', 'MRF'])

    if any(k in title_lower for k in ['rbi', 'fed', 'rate', 'repo', 'inflation']):
        if 'cut' in title_lower:
            impacted_assets.update(['Nifty', 'BankNifty (Bullish)', 'Bonds'])
        elif 'hike' in title_lower or 'high' in title_lower:
            impacted_assets.update(['India10Y Yield (Up)', 'USD/INR', 'Gold'])
        else:
            impacted_assets.update(['Nifty', 'BankNifty', 'India10Y', 'Gold'])
            
        impacted_sectors.update(['Banking', 'NBFCs', 'Real Estate', 'Auto'])
        impacted_stocks.update(['HDFC Bank', 'SBI', 'ICICI Bank', 'Bajaj Finance', 'DLF', 'Maruti'])

    if any(k in title_lower for k in ['acquisition', 'merger', 'buyout']):
        impacted_assets.update(['Target Company Share Price'])
        impacted_sectors.update(['Consolidating Sector'])
        impacted_stocks.update(['Acquiring Corp', 'Target Corp'])

    if not impacted_assets:
        impacted_assets.add('Nifty 50')
    if not impacted_sectors:
        impacted_sectors.add(category)
    if not impacted_stocks:
        impacted_stocks.add('Nifty Top 10 Broad Index')

    assets_str = ", ".join(list(impacted_assets)[:5])
    sectors_str = ", ".join(list(impacted_sectors)[:5])
    stocks_str = ", ".join(list(impacted_stocks)[:6])
    
    return event_id, assets_str, sectors_str, stocks_str

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
    if any(k in title_lower for k in research_kws):
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

def extract_keywords(text):
    text = re.sub(r'(bbc|ndtv|the hindu|reuters|cnbc|economic times|financial times|ettech|moneycontrol|sarkaritel|investment guru).*', '', text.lower())
    text = re.sub(r'[^\w\s]', '', text)
    words = text.split()
    stopwords = {'a', 'an', 'the', 'is', 'on', 'in', 'to', 'for', 'of', 'and', 'with', 'by', 'at', 'as', 'new', 'says', 'amid', 'over', 'via', 'how', 'what', 'why', 'are', 'they'}
    return set(w for w in words if w not in stopwords and len(w) > 2)

def is_same_event(new_title, event_id, threshold=0.35):
    global SENT_NEWS_MEMORY, RECENT_EVENT_IDS
    current_time = time.time()
    
    if event_id != "GENERAL_MARKET_UPDATE" and event_id in RECENT_EVENT_IDS:
        last_seen = RECENT_EVENT_IDS[event_id]
        if current_time - last_seen < 2700: 
            print(f"🚫 [EVENT ID TIMEOUT] #{event_id} వార్త 45 నిమిషాల వ్యవధిలో పంపబడింది. స్కిప్ చేస్తున్నాం.")
            return True

    new_keywords = extract_keywords(new_title)
    if not new_keywords:
        return False

    for sent_title in SENT_NEWS_MEMORY:
        sent_keywords = extract_keywords(sent_title)
        if not sent_keywords:
            continue
        common = new_keywords.intersection(sent_keywords)
        similarity = len(common) / min(len(new_keywords), len(sent_keywords))
        if similarity >= threshold:
            return True
    return False

def load_past_news_from_db():
    global SENT_NEWS_MEMORY
    if news_collection is not None:
        try:
            past_docs = list(news_collection.find({}, {"title": 1}).sort("timestamp", -1).limit(200))
            for doc in past_docs:
                if "title" in doc and doc["title"]:
                    SENT_NEWS_MEMORY.append(doc["title"])
            print(f"✅ MongoDB నుండి {len(SENT_NEWS_MEMORY)} పాత వార్తలను RAM లోకి లోడ్ చేసాము సర్!")
        except Exception as e:
            print(f"⚠️ పాత వార్తలను లోడ్ చేయడంలో లోపం: {e}")

# =====================================================================
# 📰 Continuous Macro & Sector Surveillance Team
# =====================================================================
def live_research_surveillance_worker():
    global analysis_vault, SENT_NEWS_MEMORY, RECENT_EVENT_IDS
    print("\n🕵️‍♂️ [START] Live Research Team నిరంతర నిఘా లూప్ ప్రారంభమైంది సర్...")
    
    macro_feeds = [
    # 1. ET Markets Direct Feed
    ("ET_Markets_Global", "https://economictimes.indiatimes.com/markets/rssfeeds/2146842.cms"),

    # 2. Moneycontrol Financial Articles Master Feed
    ("Moneycontrol_Insights_Via_Google", 
     "https://news.google.com/rss/search?q=site:moneycontrol.com+intitle:(%22mutual+funds%22+OR+%22investment%22+OR+%22strategy%22+OR+%22market%22+OR+%22stocks%22+OR+%22finance%22+OR+%22outlook%22)&hl=en-IN&gl=IN&ceid=IN:en"),

    # 3. Indian Banking, ARC, Cyber & Credit Feed
    ("India_Banking_ARC_Cyber",
     "https://news.google.com/rss/search?q=(%22ARC%22+OR+%22Asset+Reconstruction%22+OR+%22JC+Flowers%22+OR+%22ACRE%22+OR+%22Data+Breach%22+OR+%22Cyber%22+OR+%22NRI+Deposit%22+OR+%22Credit+Growth%22+OR+%22Credit+Card%22+OR+%22Bank+Credit%22+OR+%22Liquidity%22)&hl=en-IN&gl=IN&ceid=IN:en"),

    # 4. Indian Macro, RBI Policies & Economic Outlook Feed
    ("India_Macro_RBI_Govt",
     "https://news.google.com/rss/search?q=(%22RBI%22+OR+%22Reserve+Bank+of+India%22+OR+%22Monetary+Policy+Committee%22+OR+%22Repo+Rate%22+OR+%22Reverse+Repo%22+OR+%22Cash+Reserve+Ratio%22+OR+%22Statutory+Liquidity+Ratio%22+OR+%22Inflation%22+OR+%22Consumer+Price+Index%22+OR+%22Core+CPI%22+OR+%22Wholesale+Price+Index%22+OR+%22Gross+Domestic+Product%22+OR+%22Fiscal+Deficit%22+OR+%22Current+Account+Deficit%22+OR+%22Index+of+Industrial+Production%22+OR+%22Purchasing+Managers+Index%22+OR+%22Industrial+Production%22+OR+%22Retail+Inflation%22+OR+%22Bond+Yield%22+OR+%22Government+Securities%22+OR+%22G-Sec%22+OR+%22Rupee%22+OR+%22Forex+Reserves%22+OR+%22Foreign+Exchange+Reserves%22+OR+%22Cabinet%22+OR+%22Goods+and+Services+Tax%22+OR+%22GST%22+OR+%22Budget%22+OR+%22Union+Budget%22+OR+%22Production+Linked+Incentive%22+OR+%22PLI%22+OR+%22Fund+Manager%22+OR+%22Weekly+Outlook%22+OR+%22Daily+Voice%22)&hl=en-IN&gl=IN&ceid=IN:en"),

    # 5. Global Central Banks & Foreign Macro Economy Feed
    ("Global_Central_Banks_Macro",
     "https://news.google.com/rss/search?q=(%22Federal+Reserve%22+OR+%22FOMC%22+OR+%22European+Central+Bank%22+OR+%22ECB%22+OR+%22Bank+of+England%22+OR+%22Bank+of+Japan%22+OR+%22BOJ%22+OR+%22Peoples+Bank+of+China%22+OR+%22PBOC%22+OR+%22US+Inflation%22+OR+%22Personal+Consumption+Expenditures%22+OR+%22PCE%22+OR+%22Nonfarm+Payrolls%22+OR+%22Unemployment%22+OR+%22Treasury+Yield%22+OR+%22Dollar+Index%22+OR+%22DXY%22+OR+%22US+GDP%22+OR+%22China+Economy%22+OR+%22Trade+War%22+OR+%22Tariffs%22+OR+%22Recession%22)&hl=en-IN&gl=IN&ceid=IN:en"),

    # 6. Global Commodities & Energy Markets Feed
    ("Global_Commodities_Energy",
     "https://news.google.com/rss/search?q=(%22Crude+Oil%22+OR+%22Brent+Crude%22+OR+%22WTI+Crude%22+OR+%22OPEC%22+OR+%22Natural+Gas%22+OR+%22Gold%22+OR+%22Silver%22+OR+%22Copper%22+OR+%22Iron+Ore%22+OR+%22Coal%22+OR+%22Liquefied+Natural+Gas%22+OR+%22LNG%22)&hl=en-IN&gl=IN&ceid=IN:en")
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
            
            print(f"\n🔄 [{datetime.now().strftime('%H:%M:%S')}] RSS ఫీడ్స్ నుండి వార్తలను సేకరిస్తున్నాను...")
            for source_name, url in macro_feeds:
                success = False
                for attempt in range(1, 4):
                    try:
                        response = requests.get(url, headers=headers, timeout=25)
                        if response.status_code == 200:
                            root = ET.fromstring(response.content)
                            items = root.findall('.//item')
                            feed_count = 0
                            for item in items[:8]:
                                title = item.find('title').text or ""
                                desc = item.find('description').text or ""
                                # 🔗 వార్త లింక్ ని లాగుతున్నాం సర్
                                link = item.find('link').text or ""
                                clean_desc = re.sub('<[^<]+?>', '', desc)
                                full_text = f"{title} {clean_desc}".strip()
                                if full_text: 
                                    collected_news.append((source_name, title, full_text, link))
                                    feed_count += 1
                            print(f"📥 {source_name} నుండి {feed_count} వార్తలు సేకరించాను.")
                            success = True
                            break 
                    except Exception as feed_err:
                        print(f"⚠️ {source_name} ప్రయత్నం {attempt}/3 ఫెయిల్ అయింది: {feed_err}")
                        if attempt < 3:
                            time.sleep(2) 
                        else:
                            report_error_to_telegram(f"RSS Fetch ({source_name})", str(feed_err))
                    continue

            if not collected_news:
                print("💤 వార్తలు ఏవీ దొరకలేదు సర్. 5 నిమిషాల గ్యాప్ తీసుకుంటున్నాను...")
                time.sleep(300)
                continue

            filtered_batch = []
            batch_seen_titles = []

            for source, raw_title, news_text, article_link in collected_news:
                article_type = detect_article_type(raw_title)
                news_category = classify_news(raw_title)
                event_id, impacted_assets, impacted_sectors, impacted_stocks = detect_event_and_impact(raw_title, news_category, article_type)

                if is_same_event(raw_title, event_id):
                    print(f"🚫 [SKIP DUPLICATE EVENT] {raw_title}")
                    continue

                is_batch_duplicate = False
                new_keywords = extract_keywords(raw_title)
                
                for seen_title in batch_seen_titles:
                    seen_keywords = extract_keywords(seen_title)
                    if not new_keywords or not seen_keywords:
                        continue
                    common = new_keywords.intersection(seen_keywords)
                    similarity = len(common) / min(len(new_keywords), len(seen_keywords))
                    if similarity >= 0.30: 
                        is_batch_duplicate = True
                        break

                if is_batch_duplicate:
                    print(f"🚫 [SKIP BATCH DUPLICATE] {raw_title}")
                    continue

                batch_seen_titles.append(raw_title)
                filtered_batch.append((source, raw_title, news_text, article_link, article_type, news_category, event_id, impacted_assets, impacted_sectors, impacted_stocks))

            for source, raw_title, news_text, article_link, article_type, news_category, event_id, impacted_assets, impacted_sectors, impacted_stocks in filtered_batch:
                current_clean_content = clean_main_content(news_text)

                SENT_NEWS_MEMORY.append(raw_title)
                if len(SENT_NEWS_MEMORY) > 150:
                    SENT_NEWS_MEMORY.pop(0)

                if event_id != "GENERAL_MARKET_UPDATE":
                    RECENT_EVENT_IDS[event_id] = time.time()

                if news_collection is not None:
                    try:
                        news_collection.insert_one({
                            "clean_content": current_clean_content,
                            "title": raw_title.strip(),
                            "event_id": event_id,
                            "link": article_link,
                            "timestamp": datetime.now()
                        })
                    except Exception:
                        pass

                safe_title = clean_for_html(raw_title)
                safe_source = clean_for_html(source)
                
                market_score, research_score = calculate_dynamic_scores(
                    raw_title, 
                    article_type, 
                    news_category, 
                    source, 
                    event_id, 
                    debug=True
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
                    "link": article_link, # 🔗 ఒరిజినల్ ఆర్టికల్ లింక్
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

                # 📩 లింక్ ఉన్న మెసేజ్ ఫార్మాట్
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
                # 🌐 ఒరిజినల్ ఆర్టికల్ ని రీడైరెక్ట్ చేసే బటన్
                link_btn = InlineKeyboardButton(text="🌐 Read Full Article", url=article_link)
                markup.add(analyze_btn)
                markup.add(link_btn)
                
                bot.send_message(YOUR_TELEGRAM_CHAT_ID, short_telegram_msg, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
                print(f"📤 [TELEGRAM SENT] [{event_id}] {safe_title}")
                time.sleep(2)
                
            print(f"\n📡📡 నిరంతర నిఘా లూప్ ముగిసింది. మళ్లీ 5 నిమిషాల తర్వాత చెక్ చేస్తుంది సర్...")
            
        except Exception as e:
            print(f"⚠️ [LOOP EXCEPTION] Live Research లూప్‌లో అంతరాయం: {e}")
            report_error_to_telegram("Live Research Main Loop", str(e))
            
        time.sleep(300)

# =====================================================================
# 🧠 జెమిని AI విశ్లేషణ
# =====================================================================
def run_gemini_analysis(news_item):
    if not client:
        print("❌ జెమిని API కీ సెట్ చేయలేదు సర్.")
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
        print(f"❌ జెమిని API ఎర్రర్ ఇచ్చింది: {err}")
        return None

# =====================================================================
# 🚀 మాస్టర్ కమాండ్ సెంటర్ & బాట్ ఈవెంట్స్
# =====================================================================
if __name__ == "__main__":
    print("🧹 Render లో Token Conflict రాకుండా పాత కనెక్షన్‌లను క్లియర్ చేస్తున్నాను సర్...")
    try:
        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(2) 
        print("✅ పాత కనెక్షన్‌లు క్లియర్ అయ్యాయి సర్!")
    except Exception as token_err:
        pass

    load_past_news_from_db()

    start_msg = "🤖 <b>Institutional Research Terminal Bot v10.0 సిద్ధంగా ఉంది సర్!</b>\n\n" \
                "🏆 <b>10/10 Complete Feature Upgrade:</b> Direct Article Links Layer యాడ్ చేయబడింది!"
    
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
