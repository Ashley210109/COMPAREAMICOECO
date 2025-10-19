import os, re, html as html_mod, secrets, traceback, logging
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from fastapi.middleware.cors import CORSMiddleware

from PyPDF2 import PdfReader
from xhtml2pdf import pisa

BASE = Path(__file__).parent
TEMPLATES_DIR = BASE / "templates"
STATIC_DIR = BASE / "static"
UPLOADS_DIR = BASE / "uploads"

TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))

APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
APP_SAFE = os.environ.get("APP_SAFE", "0") == "1"

logger = logging.getLogger("station_road")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Amico Eco • EPC Comparator")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET", "dev-secret-change-me"))
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_headers=["*"], allow_methods=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

STAT_ORDER = ["already installed","not applicable","sap increase too small","recommended"]

def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def is_authed(request: Request) -> bool:
    return request.session.get("authed") is True

def safe_error(e: Exception) -> HTMLResponse:
    tb = traceback.format_exc()
    logger.error("Error:\n%s", tb)
    return HTMLResponse(f"<h2>Internal error</h2><pre>{html_mod.escape(tb)}</pre>", status_code=500)

def extract_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    lines = [norm_ws(ln) for ln in "\n".join(pages).splitlines()]
    return "\n".join([ln for ln in lines if ln])

def pick_status(pre_text: str, post_text: str):
    def choose(raw: str) -> str:
        txt = (raw or "").lower()
        for token in STAT_ORDER:
            if token in txt:
                return token.title()
        return raw.strip() if raw else ""
    return choose(pre_text), choose(post_text)

def _search(pat: str, text: str, cast=str, default=None):
    m = re.search(pat, text, re.IGNORECASE)
    if not m: return default
    val = m.group(1).strip()
    try: return cast(val)
    except Exception: return val

def _search_float(pat: str, text: str):
    m = re.search(pat, text, re.IGNORECASE)
    if not m: return None
    v = m.group(1).replace(",", "")
    try: return float(v)
    except Exception: return None

def parse_summary(text: str):
    d = {}
    d["survey_reference"] = _search(r"Survey Reference:\s*([A-Za-z0-9\-/ ]+)", text)
    d["reference_number"] = _search(r"Reference Number:\s*([A-Za-z0-9\-/]+)", text)
    d["process_date"]     = _search(r"Process date:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})", text)
    sap = re.search(r"Current SAP rating:\s*([A-G])\s*(\d+)\s*Potential SAP rating:\s*([A-G])\s*(\d+)", text, re.IGNORECASE)
    if sap:
        d["sap_current_band"], d["sap_current"] = sap.group(1), int(sap.group(2))
        d["sap_potential_band"], d["sap_potential"] = sap.group(3), int(sap.group(4))
    ei = re.search(r"Current EI rating:\s*([A-G])\s*(\d+)\s*Potential EI rating:\s*([A-G])\s*(\d+)", text, re.IGNORECASE)
    if ei:
        d["ei_current_band"], d["ei_current"] = ei.group(1), int(ei.group(2))
        d["ei_potential_band"], d["ei_potential"] = ei.group(3), int(ei.group(4))
    d["fuel_bill"] = _search_float(r"(?:Fuel Bill|Estimated Fuel Costs?)\s*:\s*£?\s*([\d,]+(?:\.\d+)?)", text)
    d["uprn"] = _search(r"UPRN:\s*([A-Za-z0-9\-]+)", text)
    d["postcode"] = _search(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b", text)
    d["address"] = _search(r"Address\s*:\s*(.+?)\s*(?:UPRN|Postcode|$)", text)
    return d

AREA_LABELS = ["Room(s) in Roof","Rooms in Roof","Room in Roof","1st Floor","First Floor","Ground Floor","2nd Floor","Second Floor","Total Floor Area"]

def parse_numeric_measures(text: str):
    m = {}
    for label in AREA_LABELS:
        pat = rf"{re.escape(label)}\s*:\s*([0-9]+(?:\.[0-9]+)?)"
        mm = re.search(pat, text, re.IGNORECASE)
        if mm:
            try: m[label] = float(mm.group(1))
            except: pass
    return m

REC_NAMES = [
    "Flat roof insulation","Room-in-roof insulation","Floor insulation (solid floor)",
    "Heating controls for wet central heating system","Loft insulation",
    "Cavity wall insulation","Draught proofing","Low energy lighting",
]

def parse_recommendations(text: str):
    recs = {}
    for name in REC_NAMES:
        pat = rf"{re.escape(name)}\s*\(([^)]+)\)"
        m = re.search(pat, text, re.IGNORECASE)
        if m: recs[name] = m.group(1).strip().title()
    return recs

def gather(path: Path):
    raw = extract_text(path)
    text = "\n".join([norm_ws(x) for x in raw.splitlines()])
    return {"summary": parse_summary(text), "measures": parse_numeric_measures(text), "recs": parse_recommendations(text)}

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
            return HTMLResponse("<h3>Please attach both PRE and POST PDFs.</h3>", status_code=400)

        pre_path = UPLOADS_DIR / f"pre_{pre.filename}"
        post_path = UPLOADS_DIR / f"post_{post.filename}"
        pre_path.write_bytes(await pre.read())
        post_path.write_bytes(await post.read())

        pre_data = {"summary": {}, "measures": {}, "recs": {}}
        post_data = {"summary": {}, "measures": {}, "recs": {}}
        if not APP_SAFE:
            pre_data = gather(pre_path)
            post_data = gather(post_path)

        if pre_date:  pre_data["summary"]["process_date"]  = pre_date
        if post_date: post_data["summary"]["process_date"] = post_date

        header = {
            "address": address or pre_data["summary"].get("address") or post_data["summary"].get("address") or "",
            "uprn": uprn or pre_data["summary"].get("uprn") or post_data["summary"].get("uprn") or "",
            "pre_date": pre_data["summary"].get("process_date", pre_date),
            "post_date": post_data["summary"].get("process_date", post_date),
            "notes": notes,
        }

        def delta(a, b): return None if (a is None or b is None) else (b - a)
        diff = {
            "sap_change": delta(pre_data["summary"].get("sap_current"), post_data["summary"].get("sap_current")),
            "ei_change": delta(pre_data["summary"].get("ei_current"), post_data["summary"].get("ei_current")),
            "fuel_bill_change": delta(pre_data["summary"].get("fuel_bill"), post_data["summary"].get("fuel_bill")),
            "recs": {}, "areas": {},
        }
        for n in sorted(set(list(pre_data["recs"].keys()) + list(post_data["recs"].keys()))):
            p, q = pick_status(pre_data["recs"].get(n, ""), post_data["recs"].get(n, ""))
            diff["recs"][n] = {"pre": p, "post": q}

        for label in sorted(set(list(pre_data["measures"].keys()) + list(post_data["measures"].keys()))):
            a = pre_data["measures"].get(label); b = post_data["measures"].get(label)
            diff["areas"][label] = {"pre": a, "post": b, "delta": delta(a, b)}

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        html_str = TEMPLATES.get_template("report.html").render(
            request=request, now=now, pre=pre_data, post=post_data, diff=diff, header=header
        )

        token = secrets.token_hex(8)
        html_file = UPLOADS_DIR / f"report_{token}.html"
        html_file.write_text(html_str, encoding="utf-8")
        request.session["last_token"] = token

        return HTMLResponse(content=html_str)

    except Exception as e:
        return safe_error(e)

@app.post("/pdf")
async def pdf(request: Request):
    if not is_authed(request): return RedirectResponse("/", status_code=302)
    try:
        token = request.session.get("last_token")
        html_file = (UPLOADS_DIR / f"report_{token}.html") if token else None
        html_str = html_file.read_text(encoding="utf-8") if (html_file and html_file.exists()) else None
        if not html_str:
            return HTMLResponse("<h3>No report available. Please generate a comparison first.</h3>", status_code=400)

        pdf_bytes = bytearray()
        result = pisa.CreatePDF(src=html_str, dest=pdf_bytes)
        if result.err:
            return HTMLResponse("<h3>PDF generation failed. Try Print → Save as PDF.</h3>", status_code=500)

        return Response(bytes(pdf_bytes), media_type="application/pdf", headers={
            "Content-Disposition": "attachment; filename=station_road_comparison.pdf"
        })
    except Exception as e:
        return safe_error(e)

@app.get("/health")
async def health(): return {"ok": True}

@app.get("/__tail__", response_class=PlainTextResponse)
async def tail_log(lines: int = 200):
    log_path = BASE / "app.log"
    if not log_path.exists(): return "No app.log yet."
    data = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(data[-max(1, min(lines, 2000)):])
