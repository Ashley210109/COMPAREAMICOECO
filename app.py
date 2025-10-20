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
# ====== Site-notes parsing & QA checks ======

YES_TOKENS = {"y", "yes", "true", "present", "installed", "fitted", "exists", "smart", "on"}
NO_TOKENS  = {"n", "no", "false", "absent", "none", "not present", "off"}

def to_bool(raw) -> bool | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    # accept bare 'smart' or similar as True
    if s in YES_TOKENS:
        return True
    if s in NO_TOKENS:
        return False
    # If the text contains explicit yes/no words
    if any(tok in s for tok in YES_TOKENS):
        return True
    if any(tok in s for tok in NO_TOKENS):
        return False
    return None

def to_float(raw) -> float | None:
    if raw is None:
        return None
    try:
        s = str(raw).replace(",", "").strip()
        return float(s)
    except:
        return None

def to_int(raw) -> int | None:
    v = to_float(raw)
    return int(v) if v is not None else None

def parse_site_notes(text: str) -> dict:
    """
    Heuristic parser for common items that appear in EPC/EPR 'site notes'.
    Adjust patterns as your PDFs evolve.
    """
    out = {}

    # Meters / utilities
    m = re.search(r"smart\s+gas\s+meter(?:\s*[:\-]?\s*(yes|no|present|absent|true|false|on|off))?", text, re.I)
    out["smart_gas_meter"] = to_bool(m.group(1) if m and m.lastindex else (m.group(0) if m else None))

    m = re.search(r"smart\s+electric(\w*)\s+meter(?:\s*[:\-]?\s*(yes|no|present|absent|true|false|on|off))?", text, re.I)
    out["smart_elec_meter"] = to_bool(m.group(1) if m and m.lastindex else (m.group(0) if m else None))

    # Insulation
    m = re.search(r"loft\s+insulation(?:\s*[:\-]?\s*(\d+)\s*mm)?", text, re.I)
    out["loft_insulation_mm"] = to_int(m.group(1)) if m and m.lastindex else None
    out["loft_insulated"] = (out["loft_insulation_mm"] is not None and out["loft_insulation_mm"] > 0)

    m = re.search(r"(?:cavity|cav)\s+wall\s+insulation(?:\s*[:\-]?\s*(yes|no|present|absent|true|false))?", text, re.I)
    out["cavity_wall_insulation"] = to_bool(m.group(1) if m and m.lastindex else (m.group(0) if m else None))

    m = re.search(r"(internal|solid)\s+wall\s+insulation(?:\s*[:\-]?\s*(\d+)\s*mm)?", text, re.I)
    out["internal_wall_insulation_mm"] = to_int(m.group(2)) if m and m.lastindex and m.group(2) else None

    m = re.search(r"flat\s+roof\s+insulation(?:\s*[:\-]?\s*(yes|no|present|absent|true|false))?", text, re.I)
    out["flat_roof_insulated"] = to_bool(m.group(1) if m and m.lastindex else (m.group(0) if m else None))

    # Ventilation / airtightness
    m = re.search(r"\bMEV\b|\bdecentralised\s+extract\b|\bMVHR\b|\bmechanical\s+ventilation\b", text, re.I)
    out["mechanical_ventilation"] = bool(m)

    m = re.search(r"(?:air\s*pressure|AP4)[^0-9]*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    out["air_permeability_ap4"] = to_float(m.group(1)) if m else None

    # Openings / glazing / lighting
    m = re.search(r"double\s+glaz(ed|ing)(?:\s*[:\-]?\s*(yes|no))?", text, re.I)
    out["double_glazed"] = to_bool(m.group(2)) if m and m.lastindex and m.group(2) else bool(m)

    m = re.search(r"doors?\s*[:\-]?\s*(\d+)\s*\(uninsulated\)", text, re.I)
    out["doors_uninsulated"] = to_int(m.group(1)) if m else None

    m = re.search(r"(\d+)\s+low[- ]energy\s+of\s+(\d+)", text, re.I)
    if m:
        out["low_energy_lights"] = to_int(m.group(1))
        out["lights_total"] = to_int(m.group(2))
    else:
        out["low_energy_lights"] = None
        out["lights_total"] = None

    # Heating & hot water
    m = re.search(r"main\s+heating\s+system.*?(\d{2,3}\.\d)%", text, re.I)
    out["main_heat_eff_pct"] = to_float(m.group(1)) if m else None

    m = re.search(r"heating\s+controls?.*?(smart|zoned|trv|programm(er|able)|room\s*thermostat)", text, re.I)
    out["heating_controls_smart"] = bool(m and re.search(r"smart|zoned|trv", m.group(0), re.I))

    m = re.search(r"water\s+heating.*?(cylinder|combi|no\s+cylinder)", text, re.I)
    out["hot_water_type"] = m.group(1).lower() if m else None

    # Renewables
    m = re.search(r"\bsolar\s+pv\b|\bphotovoltaic\b", text, re.I)
    out["pv_present"] = bool(m)

    return out

def compare_site_notes(pre: dict, post: dict) -> list[dict]:
    """
    Compare normalised site notes between PRE and POST and emit QA flags.
    Each flag: {level: 'error'|'warning'|'info', field: '...', message: '...'}
    """
    issues = []

    def add(level, field, msg):
        issues.append({"level": level, "field": field, "message": msg})

    # 1) simple boolean consistency checks
    for field, label in [
        ("smart_gas_meter", "Smart gas meter"),
        ("smart_elec_meter", "Smart electric meter"),
        ("mechanical_ventilation", "Mechanical ventilation"),
        ("double_glazed", "Double glazing"),
        ("pv_present", "Solar PV"),
        ("flat_roof_insulated", "Flat roof insulation"),
    ]:
        pre_v, post_v = pre.get(field), post.get(field)
        if pre_v is True and post_v is False:
            add("error", label, f"{label} ticked PRE but not POST — likely missed on POST.")
        elif pre_v is False and post_v is True:
            add("info", label, f"{label} added on POST — verify this was actually installed.")

    # 2) numeric deltas with sanity checks
    def delta(a, b):
        if a is None or b is None: return None
        return b - a

    ap4_d = delta(pre.get("air_permeability_ap4"), post.get("air_permeability_ap4"))
    if ap4_d is not None:
        if ap4_d > 0.5:
            add("warning", "Air permeability (AP4)",
                f"AP4 got worse by +{ap4_d:.2f}. Re-check air test entry.")
        elif ap4_d < -2.0:
            add("info", "Air permeability (AP4)",
                f"AP4 improved by {abs(ap4_d):.2f}. Ensure test evidence attached.")

    # lighting consistency
    pre_le, pre_total = pre.get("low_energy_lights"), pre.get("lights_total")
    post_le, post_total = post.get("low_energy_lights"), post.get("lights_total")
    if pre_total and post_total and pre_total != post_total:
        add("warning", "Lighting totals", f"Total points changed {pre_total} → {post_total}. Confirm count method.")
    if pre_le and post_le and post_le < pre_le:
        add("warning", "Low-energy lighting", f"Low-energy fittings dropped {pre_le} → {post_le}. Check data.")

    # doors sanity
    pre_doors, post_doors = pre.get("doors_uninsulated"), post.get("doors_uninsulated")
    if pre_doors is not None and post_doors is not None:
        if post_doors < pre_doors - 2:
            add("info", "Doors", f"Uninsulated doors reduced {pre_doors} → {post_doors}. Were doors replaced/insulated?")

    # insulation coherence
    pre_loft_mm, post_loft_mm = pre.get("loft_insulation_mm"), post.get("loft_insulation_mm")
    if pre_loft_mm is not None and post_loft_mm is not None and post_loft_mm < pre_loft_mm:
        add("warning", "Loft insulation", f"Thickness decreased {pre_loft_mm}mm → {post_loft_mm}mm. Check entry.")

    # controls coherence
    if post.get("heating_controls_smart") and not post.get("main_heat_eff_pct"):
        add("warning", "Heating controls", "Smart controls marked but main system details missing. Add boiler/system data.")

    # PV coherence
    if post.get("pv_present") and post.get("low_energy_lights") is None:
        add("info", "Solar PV", "PV present but lighting counts missing. Consider completing lighting data for SAP.")
    if pre.get("pv_present") and not post.get("pv_present"):
        add("error", "Solar PV", "PV ticked PRE but not POST — confirm which is correct.")

    return issues

    for name in REC_NAMES:
        pat = rf"{re.escape(name)}\s*\(([^)]+)\)"
        m = re.search(pat, text, re.IGNORECASE)
        if m: recs[name] = m.group(1).strip().title()
    return recs

def gather(path: Path):def gather(path: Path):
    raw = extract_text(path)
    text = "\n".join(norm_lines(raw))
    return {
        "summary": parse_summary(text),
        "measures": parse_numeric_measures(text),
        "recs": parse_recommendations(text),
        "notes": parse_site_notes(text),   # <— add this line
        "raw_text": text,                  # optional, handy for debugging
    }

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
