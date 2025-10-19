import os
import re
import html as html_mod
import secrets
import traceback
import logging
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from PyPDF2 import PdfReader
from xhtml2pdf import pisa

# -------------------------------------------------------
# Setup
# -------------------------------------------------------
BASE = Path(__file__).parent
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

TEMPLATES = Jinja2Templates(directory=str(BASE / "templates"))

# simple env-driven config
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
APP_SAFE = os.environ.get("APP_SAFE", "0") == "1"  # when 1, skip parsing for demos

logger = logging.getLogger("station_road")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Station Road Comparator")
app.add_middleware(SessionMiddleware, secret_key="dev-secret-change-me")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"],
    allow_methods=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

# -------------------------------------------------------
# Helpers
# -------------------------------------------------------

# consistent order for display/normalisation
STAT_ORDER = [
    "already installed",
    "not applicable",
    "sap increase too small",
    "recommended",
]

def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()

def choose_status(raw: str) -> str:
    """
    Normalise any status-like text into one of our canonical labels,
    otherwise return the original (title-cased).
    """
    s = norm_text(raw)
    # canonical hits
    for token in STAT_ORDER:
        if token in s:
            return token.title()

    # common synonyms
    if any(t in s for t in ["n/a", "not app", "no ", "none ", "no flat roof", "no loft", "not relevant"]):
        return "Not Applicable"
    if any(t in s for t in ["existing", "present", "installed", "insulated", "fitted"]):
        return "Already Installed"
    if any(t in s for t in ["too small", "sap increase too small"]):
        return "Sap Increase Too Small"
    if any(t in s for t in ["uninsulated", "below", "single glazed", "poor", "recommend"]):
        return "Recommended"

    # fallback: preserve as-is (but tidy)
    return (raw or "").strip().title()

def descriptor_to_status(desc: str) -> str:
    """
    When we capture things like "Uninsulated" or "Already installed" from
    the parentheses after a measure, convert them into a recommendation status.
    """
    d = norm_text(desc)
    if not d:
        return ""
    if any(x in d for x in ["uninsulated", "below", "no insulation", "less than", "<", "not insulated"]):
        return "Recommended"
    if any(x in d for x in ["already", "insulated", "installed", "present", "existing"]):
        return "Already Installed"
    if any(x in d for x in ["sap increase too small", "too small"]):
        return "Sap Increase Too Small"
    if any(x in d for x in ["n/a", "not applicable", "no flat roof", "no cavity", "solid wall", "not relevant"]):
        return "Not Applicable"
    return choose_status(desc)

def is_authed(request: Request) -> bool:
    return request.session.get("authed") is True

def safe_error(e: Exception) -> HTMLResponse:
    tb = traceback.format_exc()
    logger.error("Comparison failed:\n%s", tb)
    # Show readable error while still logging details
    return HTMLResponse(
        f"<h2>Comparison failed</h2><p>Please check the uploaded PDFs or try again.</p>"
        f"<details><summary>Technical details</summary><pre>{html_mod.escape(tb)}</pre></details>",
        status_code=500,
    )

def extract_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n".join(pages)

def norm_lines(s: str):
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in s.splitlines()]
    return [ln for ln in lines if ln]

def parse_summary(text: str):
    def search_pat(pat, cast=str, default=None):
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            return default
        val = m.group(1).strip()
        try:
            return cast(val)
        except Exception:
            return val

    data = {}
    data["survey_reference"] = search_pat(r"Survey Reference:\s*([A-Za-z0-9\-\/ ]+?)\s", str)
    data["reference_number"] = search_pat(r"Reference Number:\s*([A-Za-z0-9\-\/]+)", str)
    data["process_date"] = search_pat(r"Process date:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", str)

    sap_block = re.search(
        r"Current SAP rating:\s*([A-G])\s*(\d+)\s*Potential SAP rating:\s*([A-G])\s*(\d+)",
        text, re.IGNORECASE)
    if sap_block:
        data["sap_current_band"] = sap_block.group(1)
        data["sap_current"] = int(sap_block.group(2))
        data["sap_potential_band"] = sap_block.group(3)
        data["sap_potential"] = int(sap_block.group(4))

    ei_block = re.search(
        r"Current EI rating:\s*([A-G])\s*(\d+)\s*Potential EI rating:\s*([A-G])\s*(\d+)",
        text, re.IGNORECASE)
    if ei_block:
        data["ei_current_band"] = ei_block.group(1)
        data["ei_current"] = int(ei_block.group(2))
        data["ei_potential_band"] = ei_block.group(3)
        data["ei_potential"] = int(ei_block.group(4))

    fuel = re.search(r"Fuel Bill:\s*£?\s*([\d,]+(?:\.\d+)?)", text, re.IGNORECASE)
    data["fuel_bill"] = float(fuel.group(1).replace(",", "")) if fuel else None

    data["uprn"] = search_pat(r"UPRN:\s*([A-Za-z0-9]+)", str)
    data["postcode"] = search_pat(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b", str)
    return data

def parse_numeric_measures(text: str):
    """
    Extract common “Key Areas (m²)” values from a single PDF.
    Works with ‘Label: 12.34’ OR ‘Label .... 12.34’ style text.
    """
    labels = [
        "Room(s) in Roof",
        "1st Floor",
        "Ground Floor",
        "2nd Floor",
        "Total Floor Area",
    ]
    result = {}
    for label in labels:
        # tolerate any non-digits between label and number
        pat = rf"{re.escape(label)}\D*([0-9]+(?:\.[0-9]+)?)"
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                result[label] = float(m.group(1))
            except Exception:
                pass
    return result

def parse_recommendations(text: str):
    """
    Pull out the recommendation status *per measure*.
    Strategy 1: look for 'Measure (Descriptor)' and map descriptor -> status.
    Strategy 2: fallback to any nearby explicit status words.
    """
    names = [
        "Flat roof insulation",
        "Room-in-roof insulation",
        "Floor insulation (solid floor)",
        "Heating controls for wet central heating system",
        "Loft insulation",
        "Cavity wall insulation",
        "Draught proofing",
        "Low energy lighting",
    ]
    recs = {}

    for name in names:
        # (1) Descriptor within parentheses after the measure
        m = re.search(rf"{re.escape(name)}\s*\(([^)]+)\)", text, re.IGNORECASE)
        if m:
            recs[name] = descriptor_to_status(m.group(1))
            continue

        # (2) Explicit status words within ~60 chars after the measure name
        m2 = re.search(
            rf"{re.escape(name)}\D{{0,60}}(recommended|already installed|not applicable|sap increase too small)",
            text, re.IGNORECASE
        )
        if m2:
            recs[name] = choose_status(m2.group(1))
            continue

        # (3) Leave blank if not found; we won’t invent a status
        recs[name] = ""

    return recs

def gather(path: Path):
    raw = extract_text(path)
    text = "\n".join(norm_lines(raw))
    return {
        "summary": parse_summary(text),
        "measures": parse_numeric_measures(text),
        "recs": parse_recommendations(text),
    }

# -------------------------------------------------------
# Routes
# -------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not is_authed(request):
        return TEMPLATES.TemplateResponse("login.html", {"request": request, "error": None})
    return TEMPLATES.TemplateResponse("index.html", {"request": request})

@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form(...)):
    if password == APP_PASSWORD:
        request.session["authed"] = True
        return RedirectResponse(url="/", status_code=302)
    return TEMPLATES.TemplateResponse("login.html", {"request": request, "error": "Wrong password"})

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)

@app.post("/compare", response_class=HTMLResponse)
async def compare(
    request: Request,
    pre: UploadFile,
    post: UploadFile,
    address: str = Form(""),
    uprn: str = Form(""),
    pre_date: str = Form(""),
    post_date: str = Form(""),
    notes: str = Form(""),
):
    try:
        if not is_authed(request):
            return RedirectResponse("/", status_code=302)

        if not pre.filename or not post.filename:
            raise ValueError("Please attach both PRE and POST PDFs before submitting.")

        # Save uploads
        pre_path = UPLOADS / f"pre_{pre.filename}"
        post_path = UPLOADS / f"post_{post.filename}"
        pre_path.write_bytes(await pre.read())
        post_path.write_bytes(await post.read())

        # Parse (or skip in SAFE mode)
        if APP_SAFE:
            pre_data = {"summary": {}, "measures": {}, "recs": {}}
            post_data = {"summary": {}, "measures": {}, "recs": {}}
        else:
            pre_data = gather(pre_path)
            post_data = gather(post_path)

        # Override dates if provided via form
        if pre_date:
            pre_data.setdefault("summary", {})["process_date"] = pre_date
        if post_date:
            post_data.setdefault("summary", {})["process_date"] = post_date

        # Normalise statuses & build diff
        names = sorted(set(pre_data["recs"].keys()) | set(post_data["recs"].keys()))
        rec_diff = {}
        for n in names:
            pre_status = choose_status(pre_data["recs"].get(n, ""))
            post_status = choose_status(post_data["recs"].get(n, ""))
            rec_diff[n] = {"pre": pre_status, "post": post_status}

        def delta(a, b):
            if a is None or b is None:
                return None
            return b - a

        diff = {
            "sap_change": delta(pre_data["summary"].get("sap_current"), post_data["summary"].get("sap_current")),
            "ei_change": delta(pre_data["summary"].get("ei_current"), post_data["summary"].get("ei_current")),
            "fuel_bill_change": delta(pre_data["summary"].get("fuel_bill"), post_data["summary"].get("fuel_bill")),
            "recs": rec_diff,
        }

        header = {
            "address": address,
            "uprn": uprn,
            "pre_date": pre_data["summary"].get("process_date", pre_date),
            "post_date": post_data["summary"].get("process_date", post_date),
            "notes": notes,
            "theme": "modern",  # keep the coloured look in report.html
        }

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        html_str = TEMPLATES.get_template("report.html").render(
            request=request, now=now, pre=pre_data, post=post_data, diff=diff, header=header
        )

        # Save the report html for PDF conversion
        token = secrets.token_hex(8)
        html_file = UPLOADS / f"report_{token}.html"
        html_file.write_text(html_str, encoding="utf-8")
        request.session["last_token"] = token

        return HTMLResponse(content=html_str)

    except Exception as e:
        return safe_error(e)

@app.post("/pdf")
async def pdf(request: Request):
    if not is_authed(request):
        return RedirectResponse("/", status_code=302)
    try:
        token = request.session.get("last_token")
        html_file = (UPLOADS / f"report_{token}.html") if token else None
        html_str = html_file.read_text(encoding="utf-8") if (html_file and html_file.exists()) else None
        if not html_str:
            return HTMLResponse("<h3>No report available yet. Please generate a comparison first.</h3>", status_code=400)

        pdf_bytes = bytearray()
        result = pisa.CreatePDF(src=html_str, dest=pdf_bytes)
        if result.err:
            return HTMLResponse("<h3>PDF generation failed. Please use the Print button instead.</h3>", status_code=500)

        return Response(bytes(pdf_bytes), media_type="application/pdf", headers={
            "Content-Disposition": "attachment; filename=station_road_comparison.pdf"
        })
    except Exception as e:
        return safe_error(e)

@app.exception_handler(Exception)
async def all_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    logger.error("Unhandled exception on %s: %s\n%s", str(request.url), repr(exc), tb)
    return HTMLResponse(
        f"<h2>Unhandled error</h2><pre>{html_mod.escape(tb)}</pre>",
        status_code=500
    )

@app.get("/compare")
async def compare_get():
    return RedirectResponse("/", status_code=302)

@app.get("/__tail__", response_class=PlainTextResponse)
async def tail_log(lines: int = 200):
    log_path = BASE / "app.log"
    if not log_path.exists():
        return "No app.log yet."
    data = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(data[-max(1, min(lines, 2000)):])