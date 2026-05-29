"""
PROFESSIONAL DOCUMENT TOOLKIT API
=================================
Real features customers pay for.
Completely local - no external APIs needed.
"""

from fastapi import FastAPI, HTTPException, Header, Depends, File, UploadFile, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, EmailStr
import sqlite3
import uuid
import hashlib
import re
import io
import os
import qrcode
import tempfile
from datetime import datetime
from typing import Optional, List
from collections import Counter

# ─── APP ─────────────────────────────────────
app = FastAPI(
    title="Document Toolkit API",
    description="Professional tools: invoices, text analysis, QR codes, file processing",
    version="2.0"
)

# ─── DATABASE ────────────────────────────────
def get_db():
    conn = sqlite3.connect("api_keys.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            name TEXT,
            key_hash TEXT UNIQUE,
            created_at TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    # Usage tracking - who used what and when
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id TEXT,
            endpoint TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

def hash_key(key): return hashlib.sha256(key.encode()).hexdigest()
def gen_key(): return f"ak_{uuid.uuid4().hex}"

init_db()

# ─── DATA MODELS ─────────────────────────────
class KeyRequest(BaseModel):
    name: str

class TextInput(BaseModel):
    text: str

class InvoiceData(BaseModel):
    company_name: str
    customer_name: str
    items: List[dict]  # [{"description": "...", "quantity": 1, "price": 50.00}]
    notes: Optional[str] = ""
    invoice_number: Optional[str] = None

class UrlInput(BaseModel):
    url: str

# ─── AUTH ────────────────────────────────────
def verify_api_key(x_api_key: str = Header(...)):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash = ? AND active = 1",
        (hash_key(x_api_key),)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return dict(row)

def log_usage(key_id: str, endpoint: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO usage_log (key_id, endpoint, timestamp) VALUES (?, ?, ?)",
        (key_id, endpoint, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════
#  KEY MANAGEMENT
# ═══════════════════════════════════════════════

@app.post("/admin/generate-key")
def create_key(request: KeyRequest):
    raw = gen_key()
    conn = get_db()
    conn.execute(
        "INSERT INTO api_keys (id, name, key_hash, created_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), request.name, hash_key(raw), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return {"api_key": raw, "name": request.name, "warning": "SAVE NOW - never shown again"}

@app.get("/admin/keys")
def list_keys():
    conn = get_db()
    keys = conn.execute("SELECT id, name, created_at, active FROM api_keys").fetchall()
    conn.close()
    return [dict(k) for k in keys]

@app.delete("/admin/keys/{key_id}")
def revoke_key(key_id: str):
    conn = get_db()
    conn.execute("UPDATE api_keys SET active = 0 WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return {"status": "revoked"}

@app.get("/admin/usage")
def view_usage():
    """See which customers are using what"""
    conn = get_db()
    rows = conn.execute("""
        SELECT k.name, u.endpoint, u.timestamp 
        FROM usage_log u JOIN api_keys k ON u.key_id = k.id 
        ORDER BY u.timestamp DESC LIMIT 100
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════
#  TOOL 1: TEXT SUMMARIZER
#  Extractive - picks best sentences
# ═══════════════════════════════════════════════

@app.post("/api/summarize")
def summarize_text(request: TextInput, key: dict = Depends(verify_api_key), 
                   sentences: int = Query(3, description="Number of summary sentences")):
    """Summarize text by extracting most important sentences"""
    log_usage(key["id"], "/api/summarize")
    
    text = request.text
    if len(text) < 100:
        return {"summary": text, "note": "Text too short to summarize"}
    
    # Split into sentences
    raw_sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(raw_sentences) <= sentences:
        return {"summary": text, "original_sentences": len(raw_sentences)}
    
    # Score sentences by word frequency
    words = re.findall(r'\b\w+\b', text.lower())
    word_freq = Counter(words)
    max_freq = max(word_freq.values()) if word_freq else 1
    
    # Normalize scores
    for w in word_freq:
        word_freq[w] = word_freq[w] / max_freq
    
    # Score each sentence
    scored = []
    for s in raw_sentences:
        s_words = re.findall(r'\b\w+\b', s.lower())
        if not s_words: continue
        score = sum(word_freq.get(w, 0) for w in s_words) / len(s_words) + len(s_words) * 0.001
        scored.append((s.strip(), score))
    
    # Pick top sentences and sort by original position
    top = sorted(scored, key=lambda x: x[1], reverse=True)[:sentences]
    top_in_order = sorted(top, key=lambda x: raw_sentences.index(next(
        s for s in raw_sentences if s.strip() == x[0]
    )))
    
    return {
        "summary": " ".join(s[0] for s in top_in_order),
        "original_length": len(text),
        "summary_length": sum(len(s[0]) for s in top),
        "compression_ratio": f"{sum(len(s[0]) for s in top) / len(text) * 100:.1f}%",
        "used_by": key["name"]
    }


# ═══════════════════════════════════════════════
#  TOOL 2: KEYWORD EXTRACTOR
#  Pulls top keywords from any text
# ═══════════════════════════════════════════════

@app.post("/api/keywords")
def extract_keywords(request: TextInput, key: dict = Depends(verify_api_key),
                     top_n: int = Query(10, description="Number of keywords")):
    """Extract most important keywords from text"""
    log_usage(key["id"], "/api/keywords")
    
    text = request.text.lower()
    
    # Common stop words to filter out
    stop_words = {'the','a','an','is','are','was','were','be','been','being',
                  'have','has','had','do','does','did','will','would','could',
                  'should','may','might','can','shall','to','of','in','for',
                  'on','with','at','by','from','as','into','through','during',
                  'before','after','above','below','between','and','but','or',
                  'nor','not','so','yet','both','either','neither','each','every',
                  'all','any','few','more','most','other','some','such','no',
                  'only','own','same','than','too','very','just','about','up',
                  'out','if','then','that','this','these','those','it','its',
                  'he','she','they','them','we','us','my','your','his','her','our'}
    
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text)
    filtered = [w for w in words if w not in stop_words]
    
    freq = Counter(filtered).most_common(top_n)
    
    return {
        "keywords": [{"word": w, "count": c} for w, c in freq],
        "total_words_analyzed": len(filtered),
        "used_by": key["name"]
    }


# ═══════════════════════════════════════════════
#  TOOL 3: QR CODE GENERATOR
#  URL → QR code image
# ═══════════════════════════════════════════════

@app.post("/api/qrcode")
def generate_qr(request: UrlInput, key: dict = Depends(verify_api_key)):
    """Generate a QR code from any URL or text"""
    log_usage(key["id"], "/api/qrcode")
    
    img = qrcode.make(request.url)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(tmp.name)
    
    return FileResponse(tmp.name, media_type="image/png", 
                       filename=f"qr_{uuid.uuid4().hex[:8]}.png")


# ═══════════════════════════════════════════════
#  TOOL 4: PROFESSIONAL INVOICE GENERATOR
#  Creates a clean text invoice
# ═══════════════════════════════════════════════

@app.post("/api/invoice")
def create_invoice(data: InvoiceData, key: dict = Depends(verify_api_key)):
    """Generate a professional invoice"""
    log_usage(key["id"], "/api/invoice")
    
    inv_num = data.invoice_number or f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    
    total = 0
    items_text = ""
    for i, item in enumerate(data.items, 1):
        qty = item.get("quantity", 1)
        price = item.get("price", 0)
        line_total = qty * price
        total += line_total
        items_text += f"  {i}. {item['description']:<40} x{qty} @ ${price:.2f} = ${line_total:.2f}\n"
    
    invoice = f"""
╔══════════════════════════════════════════════════════════╗
║                    PROFESSIONAL INVOICE                   ║
╠══════════════════════════════════════════════════════════╣
║                                                          ║
║  INVOICE #: {inv_num:<44}║
║  DATE:      {datetime.now().strftime('%B %d, %Y'):<44}║
║                                                          ║
║  FROM: {data.company_name:<52}║
║                                                          ║
║  TO:   {data.customer_name:<52}║
║                                                          ║
╠══════════════════════════════════════════════════════════╣
║  ITEMS:                                                  ║
║                                                          ║
{items_text}
╠══════════════════════════════════════════════════════════╣
║                                                    ║
║  TOTAL DUE:   ${total:,.2f}                                  ║
║                                                          ║
╠══════════════════════════════════════════════════════════╣
║  NOTES: {data.notes or 'Thank you for your business!':<52}║
╚══════════════════════════════════════════════════════════╝
"""
    
    return {
        "invoice_number": inv_num,
        "total": round(total, 2),
        "items_count": len(data.items),
        "invoice_text": invoice,
        "used_by": key["name"]
    }


# ═══════════════════════════════════════════════
#  TOOL 5: SENTIMENT ANALYZER
#  Positive / Negative / Neutral
# ═══════════════════════════════════════════════

@app.post("/api/sentiment")
def analyze_sentiment(request: TextInput, key: dict = Depends(verify_api_key)):
    """Analyze text sentiment - positive, negative, or neutral"""
    log_usage(key["id"], "/api/sentiment")
    
    text = request.text.lower()
    
    positive_words = {
        'good','great','excellent','amazing','wonderful','fantastic','awesome',
        'love','happy','beautiful','best','perfect','outstanding','superb',
        'pleased','glad','delighted','thrilled','joy','win','success','profit',
        'growth','improved','better','positive','recommend','impressive'
    }
    
    negative_words = {
        'bad','terrible','awful','horrible','worst','hate','ugly','poor',
        'disappointed','sad','angry','frustrated','fail','loss','problem',
        'issue','broken','wrong','waste','useless','terrible','negative',
        'never','refund','complaint','slow','expensive','overpriced'
    }
    
    words = re.findall(r'\b\w+\b', text.lower())
    pos_count = sum(1 for w in words if w in positive_words)
    neg_count = sum(1 for w in words if w in negative_words)
    total = len(words)
    
    if pos_count > neg_count:
        sentiment, score = "POSITIVE", round(pos_count / max(total, 1) * 100, 1)
    elif neg_count > pos_count:
        sentiment, score = "NEGATIVE", round(neg_count / max(total, 1) * 100, 1)
    else:
        sentiment, score = "NEUTRAL", 0
    
    return {
        "sentiment": sentiment,
        "confidence": f"{score}%",
        "positive_words_found": pos_count,
        "negative_words_found": neg_count,
        "total_words": total,
        "used_by": key["name"]
    }


# ═══════════════════════════════════════════════
#  PRICING PAGE
# ═══════════════════════════════════════════════

@app.get("/")
def home():
    return {
        "api": "Professional Document Toolkit v2.0",
        "pricing": {
            "starter": "$9/month - 1000 requests",
            "professional": "$49/month - 10000 requests",
            "enterprise": "$199/month - unlimited"
        },
        "endpoints": {
            "text_tools": [
                "POST /api/summarize - Text summarization",
                "POST /api/keywords - Keyword extraction",
                "POST /api/sentiment - Sentiment analysis"
            ],
            "generation": [
                "POST /api/qrcode - QR code generator",
                "POST /api/invoice - Professional invoice creator"
            ],
            "admin": [
                "POST /admin/generate-key",
                "GET /admin/keys",
                "GET /admin/usage",
                "DELETE /admin/keys/{id}"
            ]
        }
    }
