from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select, text
import requests
from bs4 import BeautifulSoup
import re
import smtplib
from email.message import EmailMessage
from app.db import engine
from app.config import SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM
from app.models import Base, Business

app = FastAPI(title="Client Hunting Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://client-hunting-platform.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class BusinessCreate(BaseModel):
    name: str
    website: str | None = None
    phone: str | None = None

@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)

@app.get("/")
def root():
    return {
        "name": "Client Hunting Platform",
        "status": "running"
    }

@app.get("/health")
def health():
    return {
        "status": "ok"
    }

@app.get("/db-test")
def db_test():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        return {
            "database": "connected",
            "result": result.scalar()
        }

@app.post("/businesses")
def create_business(business: BusinessCreate):
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "INSERT INTO businesses (name, website, phone) VALUES (:name, :website, :phone) RETURNING id, name, website, phone"
            ),
            {
                "name": business.name,
                "website": business.website,
                "phone": business.phone,
            },
        )
        row = result.mappings().first()
        conn.commit()
        return {
            "id": row["id"],
            "name": row["name"],
            "website": row["website"],
            "phone": row["phone"],
        }

@app.get("/businesses")
def list_businesses():
    with engine.connect() as conn:
        result = conn.execute(
            select(
                Business.id,
                Business.name,
                Business.website,
                Business.phone,
                Business.latitude,
                Business.longitude,
            )
        )
        businesses = [dict(row) for row in result.mappings().all()]
        return businesses

@app.get("/businesses/{business_id}")
def get_business(business_id: int):
    with engine.connect() as conn:
        result = conn.execute(
            select(
                Business.id,
                Business.name,
                Business.website,
                Business.phone,
                Business.latitude,
                Business.longitude,
                Business.email,
                Business.facebook,
                Business.instagram,
                Business.linkedin,
            ).where(Business.id == business_id)
        )
        business = result.mappings().first()
        if business is None:
            return {"error": "Business not found"}
        return dict(business)

def fetch_cafes_from_overpass():
    """Fetch cafes from Overpass API"""
    overpass_url = "https://overpass-api.de/api/interpreter"
    query = """
    [out:json];
    (
      node["amenity"="cafe"](around:5000,51.5074,-0.1278);
      way["amenity"="cafe"](around:5000,51.5074,-0.1278);
    );
    out center body;
    """
    
    headers = {
        "User-Agent": "ClientHuntingPlatform/1.0",
        "Accept": "application/json"
    }
    
    try:
        response = requests.post(overpass_url, data=query, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        cafes = []
        for element in data.get("elements", [])[:5]:
            cafe_info = {}
            
            # Extract name
            if "tags" in element and "name" in element["tags"]:
                cafe_info["name"] = element["tags"]["name"]
            else:
                cafe_info["name"] = "Unknown"
            
            # Extract website from tags
            website = None
            if "tags" in element:
                tags = element["tags"]
                # Try website first, then contact:website
                website = tags.get("website") or tags.get("contact:website")
            cafe_info["website"] = website
            
            # Extract coordinates
            if "center" in element:
                cafe_info["lat"] = element["center"]["lat"]
                cafe_info["lon"] = element["center"]["lon"]
            elif "lat" in element and "lon" in element:
                cafe_info["lat"] = element["lat"]
                cafe_info["lon"] = element["lon"]
            else:
                continue
            
            cafes.append(cafe_info)
        
        return cafes[:5]
    except Exception as e:
        raise e

@app.get("/collect-test")
def collect_test():
    try:
        return fetch_cafes_from_overpass()
    except Exception as e:
        return {
            "error": str(e),
            "type": type(e).__name__
        }

@app.post("/collect-save")
def collect_save():
    try:
        cafes = fetch_cafes_from_overpass()
        
        with engine.begin() as conn:
            # Ensure tables exist with new columns
            Base.metadata.create_all(bind=engine)
            
            saved_count = 0
            for cafe in cafes:
                conn.execute(
                    text(
                        "INSERT INTO businesses (name, website, phone, latitude, longitude) VALUES (:name, :website, :phone, :latitude, :longitude)"
                    ),
                    {
                        "name": cafe.get("name", "Unknown"),
                        "website": cafe.get("website"),
                        "phone": None,
                        "latitude": cafe.get("lat"),
                        "longitude": cafe.get("lon"),
                    },
                )
                saved_count += 1
        
        return {"saved": saved_count}
    except Exception as e:
        return {
            "error": str(e),
            "type": type(e).__name__
        }

def extract_email(html):
    """Extract email address from HTML"""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    matches = re.findall(email_pattern, html)
    return matches[0] if matches else None

def extract_facebook(html):
    """Extract Facebook link from HTML"""
    soup = BeautifulSoup(html, 'html.parser')
    for link in soup.find_all('a', href=True):
        href = link['href']
        if 'facebook.com' in href:
            return href
    return None

def extract_instagram(html):
    """Extract Instagram link from HTML"""
    soup = BeautifulSoup(html, 'html.parser')
    for link in soup.find_all('a', href=True):
        href = link['href']
        if 'instagram.com' in href:
            return href
    return None

def extract_linkedin(html):
    """Extract LinkedIn link from HTML"""
    soup = BeautifulSoup(html, 'html.parser')
    for link in soup.find_all('a', href=True):
        href = link['href']
        if 'linkedin.com' in href:
            return href
    return None

def extract_phone(html):
    """Extract phone number from HTML with validation"""
    
    def is_valid_phone(phone_str):
        """Validate phone number format and content"""
        if not phone_str:
            return False
        
        # Normalize: keep only digits and +
        digits_only = re.sub(r'[^\d+]', '', phone_str)
        
        # Ignore max int32
        if digits_only == '2147483647':
            return False
        
        # Count actual digits
        digit_count = len(re.findall(r'\d', digits_only))
        
        # Must have between 7 and 15 digits
        if digit_count < 7 or digit_count > 15:
            return False
        
        # If no separators and all digits (no +), likely an ID
        has_separators = bool(re.search(r'[-\s().]', phone_str))
        has_plus = '+' in phone_str
        if not has_separators and not has_plus:
            return False
        
        return True
    
    def normalize_phone(phone_str):
        """Normalize phone format"""
        normalized = re.sub(r'\s{2,}', ' ', phone_str)  # Remove extra spaces
        return normalized.strip()
    
    # First try to find tel: links (highest priority)
    tel_pattern = r'tel:([+\d\-\s()]+)'
    matches = re.findall(tel_pattern, html)
    for match in matches:
        if is_valid_phone(match):
            return normalize_phone(match.strip())
    
    # Try international phone formats (e.g., +1-234-567-8900, +44 20 1234 5678)
    international_pattern = r'\+[1-9]\d{1,14}(?:[-.\s]?\d{1,4})*'
    matches = re.findall(international_pattern, html)
    for match in matches:
        if is_valid_phone(match):
            return normalize_phone(match.strip())
    
    # Try UK formats (e.g., 020 1234 5678, (020) 1234 5678)
    uk_pattern = r'(?:\(0\d{3,4}\)|0\d{3,4})[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}'
    matches = re.findall(uk_pattern, html)
    for match in matches:
        if is_valid_phone(match):
            return normalize_phone(match.strip())
    
    # Try US formats (e.g., (123) 456-7890, 123-456-7890)
    us_pattern = r'(?:\(\d{3}\)[\s\-.]?\d{3}[\s\-.]?\d{4}|\d{3}[\s\-.]?\d{3}[\s\-.]?\d{4})'
    matches = re.findall(us_pattern, html)
    for match in matches:
        if is_valid_phone(match):
            return normalize_phone(match.strip())
    
    return None

def compute_seo_audit(html):
    soup = BeautifulSoup(html, 'html.parser')
    title_tag = soup.find('title')
    has_title = title_tag is not None
    title = title_tag.get_text().strip() if has_title else None

    meta_description_tag = soup.find('meta', attrs={'name': 'description'})
    has_meta_description = meta_description_tag is not None
    meta_description = meta_description_tag.get('content', '').strip() if has_meta_description else None

    h1_tag = soup.find('h1')
    has_h1 = h1_tag is not None
    h1 = h1_tag.get_text().strip() if has_h1 else None

    seo_score = 0
    if has_title:
        seo_score += 30
    if has_meta_description:
        seo_score += 30
    if has_h1:
        seo_score += 40

    return {
        "has_title": has_title,
        "title": title,
        "has_meta_description": has_meta_description,
        "meta_description": meta_description,
        "has_h1": has_h1,
        "h1": h1,
        "seo_score": seo_score,
    }

@app.post("/enrich/{business_id}")
def enrich_business(business_id: int):
    try:
        # Load business from database
        with engine.connect() as conn:
            result = conn.execute(
                select(Business.id, Business.name, Business.website).where(Business.id == business_id)
            )
            row = result.mappings().first()
            
            if row is None:
                return {"error": "Business not found"}
            
            business = dict(row)
            
            # Check if website exists
            if not business.get("website"):
                return {"status": "no_website"}
            
            # Fetch homepage HTML
            try:
                response = requests.get(business["website"], timeout=10)
                response.raise_for_status()
                html = response.text
            except Exception as e:
                return {"error": f"Failed to fetch website: {str(e)}"}
            
            # Extract data
            email = extract_email(html)
            phone = extract_phone(html)
            facebook = extract_facebook(html)
            instagram = extract_instagram(html)
            linkedin = extract_linkedin(html)
            
            # Save to database
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE businesses SET email = :email, phone = :phone, facebook = :facebook, instagram = :instagram, linkedin = :linkedin WHERE id = :id"
                    ),
                    {
                        "id": business_id,
                        "email": email,
                        "phone": phone,
                        "facebook": facebook,
                        "instagram": instagram,
                        "linkedin": linkedin,
                    },
                )
            
            return {
                "email": email,
                "phone": phone,
                "facebook": facebook,
                "instagram": instagram,
                "linkedin": linkedin,
            }
    except Exception as e:
        return {
            "error": str(e),
            "type": type(e).__name__
        }

@app.post("/seo-audit/{business_id}")
def seo_audit(business_id: int):
    try:
        # Load business from database
        with engine.connect() as conn:
            result = conn.execute(
                select(Business.id, Business.name, Business.website).where(Business.id == business_id)
            )
            row = result.mappings().first()
            
            if row is None:
                return {"error": "Business not found"}
            
            business = dict(row)
            
            # Check if website exists
            if not business.get("website"):
                return {"status": "no_website"}
            
            # Fetch homepage HTML
            try:
                response = requests.get(business["website"], timeout=10)
                response.raise_for_status()
                html = response.text
            except Exception as e:
                return {"error": f"Failed to fetch website: {str(e)}"}
            
            seo_results = compute_seo_audit(html)
            return seo_results
    except Exception as e:
        return {
            "error": str(e),
            "type": type(e).__name__
        }

def compute_lead_profile(business):
    seo_score = 0
    if business.get("website"):
        try:
            response = requests.get(business["website"], timeout=10)
            response.raise_for_status()
            seo_score = compute_seo_audit(response.text)["seo_score"]
        except Exception:
            seo_score = 0

    lead_score = seo_score
    if business.get("email"):
        lead_score += 20
    if business.get("website"):
        lead_score += 10
    if business.get("linkedin"):
        lead_score += 10

    if lead_score > 100:
        lead_score = 100

    return seo_score, lead_score

def generate_outreach_content(business, seo_score, lead_score):
    name = business.get("name") or "there"
    website = business.get("website") or "your website"

    subject = f"Quick idea for improving {name}'s online visibility"
    email = (
        f"Hi {name},\n\n"
        f"I reviewed your online presence at {website} and noticed your SEO score is {seo_score} and your lead score is {lead_score}.\n\n"
        "A few simple updates to your website can help more customers find you online.\n\n"
        "Best,\n"
        "Client Hunting Platform"
    )

    return subject, email

@app.post("/lead-score/{business_id}")
def lead_score(business_id: int):
    try:
        with engine.connect() as conn:
            result = conn.execute(
                select(Business.id, Business.website, Business.email, Business.linkedin).where(Business.id == business_id)
            )
            row = result.mappings().first()
            if row is None:
                return {"error": "Business not found"}

            business = dict(row)
            seo_score, lead_score_value = compute_lead_profile(business)

            return {
                "business_id": business_id,
                "seo_score": seo_score,
                "lead_score": lead_score_value,
            }
    except Exception as e:
        return {
            "error": str(e),
            "type": type(e).__name__
        }

class EmailRequest(BaseModel):
    to: str

@app.post("/outreach/{business_id}")
def outreach(business_id: int):
    try:
        with engine.connect() as conn:
            result = conn.execute(
                select(Business.id, Business.name, Business.website, Business.email, Business.linkedin).where(Business.id == business_id)
            )
            row = result.mappings().first()
            if row is None:
                return {"error": "Business not found"}

            business = dict(row)
            if not business.get("website"):
                return {"error": "Business has no website"}

            seo_score, lead_score_value = compute_lead_profile(business)

            subject = f"Quick idea for improving {business.get('name', 'your business')}'s online visibility"
            email = (
                f"Hi {business.get('name', 'there')},\n\n"
                f"I reviewed your online presence at {business['website']} and noticed your SEO score is {seo_score} and your lead score is {lead_score_value}.\n\n"
                "A few simple updates to your website can help more customers find you online.\n\n"
                "Best,\n"
                "Client Hunting Platform"
            )

            return {
                "subject": subject,
                "email": email,
            }
    except Exception as e:
        return {
            "error": str(e),
            "type": type(e).__name__
        }

@app.post("/send-email/{business_id}")
def send_email(business_id: int, request: EmailRequest):
    try:
        with engine.connect() as conn:
            result = conn.execute(
                select(Business.id, Business.name, Business.website, Business.email, Business.linkedin).where(Business.id == business_id)
            )
            row = result.mappings().first()
            if row is None:
                return {"error": "Business not found"}

            business = dict(row)
            if not business.get("website"):
                return {"error": "Business has no website"}

            seo_score, lead_score_value = compute_lead_profile(business)
            subject, email = generate_outreach_content(business, seo_score, lead_score_value)

            if not (SMTP_HOST and SMTP_PORT and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM):
                return {"status": "smtp_not_configured"}

            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = SMTP_FROM
            message["To"] = request.to
            message.set_content(email)

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
                smtp.starttls()
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
                smtp.send_message(message)

            return {
                "status": "sent",
                "to": request.to,
                "subject": subject,
            }
    except Exception as e:
        return {
            "error": str(e),
            "type": type(e).__name__
        }

@app.get("/businesses/count")
def businesses_count():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM businesses"))
        return {
            "count": result.scalar()
        }
