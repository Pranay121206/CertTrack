"""
CertTrack Backend — Flask + Tesseract OCR  (no Celery / Redis needed)
=====================================================================
Requirements:
  pip install flask flask-cors pillow pytesseract pdf2image

System deps:
  Windows : install Tesseract (UB-Mannheim build) + Poppler, or set
            TESSERACT_CMD / POPPLER_PATH env vars
  Linux   : apt install tesseract-ocr poppler-utils

Run:
  python app.py
"""

import os, re, json, uuid, time, io, shutil, threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from flask_cors import CORS
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter

# ─── Tesseract binary auto-detect (Windows PATH is often not set) ─
_tess = os.environ.get("TESSERACT_CMD")
if not _tess and not shutil.which("tesseract"):
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe")):
        if os.path.exists(p):
            _tess = p
            break
if _tess:
    pytesseract.pytesseract.tesseract_cmd = _tess

POPPLER_PATH = os.environ.get("POPPLER_PATH") or None   # e.g. C:\poppler\Library\bin

# ─── optional PDF support ────────────────────────────────────────
try:
    from pdf2image import convert_from_bytes
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

app = Flask(__name__)
CORS(app)

OCR_TIMEOUT_SEC = 60          # hard cap per page so Tesseract can never hang forever
MAX_IMG_SIDE    = 2200        # downscale huge photos before OCR

# ─── In-process task store (replaces Celery + Redis) ─────────────
EXECUTOR = ThreadPoolExecutor(max_workers=2)
TASKS = {}                    # task_id -> {"state", "step", "result", "error", "ts"}
TASKS_LOCK = threading.Lock()


def _set_task(task_id, **kw):
    with TASKS_LOCK:
        TASKS.setdefault(task_id, {}).update(kw, ts=time.time())


def _gc_tasks(max_age=3600):
    cutoff = time.time() - max_age
    with TASKS_LOCK:
        for k in [k for k, v in TASKS.items() if v.get("ts", 0) < cutoff]:
            TASKS.pop(k, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NAME MATCHING LOGIC
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def match_names(ocr_name: str, registered_name: str) -> dict:
    """
    Compare OCR-extracted name vs registered student name.
    
    Returns:
        {
          "match": True | False | "partial",
          "ocr_name": str,
          "registered_name": str,
          "confidence": float (0.0 – 1.0),
          "method": str
        }
    """
    if not ocr_name or ocr_name.strip() in ('', '—', '-'):
        return {
            "match": None,
            "ocr_name": ocr_name or "",
            "registered_name": registered_name,
            "confidence": 0.0,
            "method": "no_ocr_name"
        }

    # Normalise both strings
    def _normalise(s):
        s = s.lower().strip()
        # Remove titles like Mr, Ms, Dr, Prof
        s = re.sub(r'\b(mr|mrs|ms|dr|prof|sr|jr)\.?\s*', '', s)
        # Remove punctuation except spaces
        s = re.sub(r'[^\w\s]', '', s)
        # Collapse whitespace
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    ocr_n   = _normalise(ocr_name)
    reg_n   = _normalise(registered_name)

    # Exact match
    if ocr_n == reg_n:
        return {"match": True,  "ocr_name": ocr_name, "registered_name": registered_name,
                "confidence": 1.0, "method": "exact"}

    # Full containment (one inside the other — covers middle-name omissions)
    if ocr_n in reg_n or reg_n in ocr_n:
        return {"match": True,  "ocr_name": ocr_name, "registered_name": registered_name,
                "confidence": 0.92, "method": "containment"}

    # Token-based overlap
    ocr_tokens = [t for t in ocr_n.split() if len(t) > 1]
    reg_tokens = [t for t in reg_n.split() if len(t) > 1]

    if not ocr_tokens or not reg_tokens:
        return {"match": False, "ocr_name": ocr_name, "registered_name": registered_name,
                "confidence": 0.0, "method": "empty_tokens"}

    matched_tokens = [t for t in ocr_tokens if t in reg_tokens]
    overlap_ratio  = len(matched_tokens) / max(len(ocr_tokens), len(reg_tokens))

    # Check if first AND last name both present
    first_last_match = (
        len(reg_tokens) >= 2 and len(ocr_tokens) >= 2 and
        reg_tokens[0] in ocr_tokens and reg_tokens[-1] in ocr_tokens
    )

    if first_last_match or overlap_ratio >= 0.75:
        return {"match": True,    "ocr_name": ocr_name, "registered_name": registered_name,
                "confidence": round(overlap_ratio, 2), "method": "token_overlap"}

    if overlap_ratio >= 0.4:
        return {"match": "partial", "ocr_name": ocr_name, "registered_name": registered_name,
                "confidence": round(overlap_ratio, 2), "method": "partial_token_overlap"}

    return {"match": False, "ocr_name": ocr_name, "registered_name": registered_name,
            "confidence": round(overlap_ratio, 2), "method": "insufficient_overlap"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CELERY TASK — OCR + Name Matching + Rule-based verification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def verify_certificate(task_id: str, file_bytes: bytes, filename: str,
                       platform: str, course_claim: str,
                       file_type: str, student_name: str = ""):
    """
    OCR the certificate, extract student name, compare vs registered name,
    then run rule-based platform/course verification.
    """
    _set_task(task_id, state="PROGRESS", step="extracting_text")

    # ── Step 1: extract raw text ─────────────────────────────────
    text      = ""
    pages_used = 0

    try:
        if file_type == "pdf":
            if PDF_SUPPORT:
                images = convert_from_bytes(file_bytes, first_page=1, last_page=3, dpi=200,
                                           poppler_path=POPPLER_PATH, timeout=90)
                for img in images:
                    text      += pytesseract.image_to_string(img, lang="eng", timeout=OCR_TIMEOUT_SEC) + "\n"
                    pages_used += 1
            else:
                return _error_result("pdf2image not installed — cannot parse PDFs")
        else:
            img = Image.open(io.BytesIO(file_bytes))
            img.load()
            if max(img.size) > MAX_IMG_SIDE:
                img.thumbnail((MAX_IMG_SIDE, MAX_IMG_SIDE))
            img = img.convert("L")
            img = ImageEnhance.Contrast(img).enhance(2.0)
            img = img.filter(ImageFilter.SHARPEN)
            text = pytesseract.image_to_string(img, lang="eng",
                                               config="--oem 3 --psm 6",
                                               timeout=OCR_TIMEOUT_SEC)
            pages_used = 1
    except Exception as e:
        return _error_result(f"OCR failed: {str(e)[:120]}")

    if not text.strip():
        return {
            "valid": False, "pending": True, "confidence": "low",
            "title": "Text Extraction Failed",
            "detail": "Could not extract readable text. The image may be too small, blurry, or scanned at low DPI.",
            "extracted": {}, "flags": ["No text extracted by Tesseract"],
            "nameMatch": None, "nameMatchDetail": {}
        }

    _set_task(task_id, state="PROGRESS", step="matching_name")

    # ── Step 2: field extraction ──────────────────────────────────
    extracted = _extract_fields(text)

    # ── Step 3: name matching ─────────────────────────────────────
    name_result = {"match": None, "ocr_name": "", "registered_name": student_name,
                   "confidence": 0.0, "method": "not_attempted"}
    flags = []

    if student_name:
        ocr_name = extracted.get("name", "")
        name_result = match_names(ocr_name, student_name)

        if name_result["match"] is False:
            flags.append(
                f'Name on certificate "{ocr_name or "not detected"}" '
                f'does not match registered name "{student_name}"'
            )
        elif name_result["match"] == "partial":
            flags.append(
                f'Partial name match: OCR found "{ocr_name}" vs registered "{student_name}" '
                f'(confidence {name_result["confidence"]:.0%}) — faculty review recommended'
            )
    else:
        flags.append("No registered student name provided — name verification skipped")

    _set_task(task_id, state="PROGRESS", step="verifying_platform")

    # ── Step 4: platform / course verification ────────────────────
    platform_signals = {
        "NPTEL":    ["nptel", "iit", "iise", "swayam", "national programme"],
        "Coursera": ["coursera", "coursera.org"],
        "edX":      ["edx", "edx.org", "verified certificate"],
        "Udemy":    ["udemy", "udemy.com"],
        "LinkedIn": ["linkedin", "linkedin learning"],
        "Google":   ["google", "google certificate", "grow with google"],
        "AWS":      ["amazon web services", "aws", "amazon"],
        "Microsoft":["microsoft", "microsoft learn", "azure"],
        "Cisco":    ["cisco", "cisco networking"],
        "Oracle":   ["oracle", "oracle university"],
    }

    text_lower = text.lower()
    platform_verified = any(s in text_lower for s in platform_signals.get(platform, [platform.lower()]))

    course_match = True
    if course_claim:
        words = [w for w in course_claim.lower().split() if len(w) > 3]
        course_match = sum(1 for w in words if w in text_lower) >= max(1, len(words) // 2)

    cert_keywords = ["certificate", "certify", "completion", "successfully completed",
                     "awarded", "has completed", "this is to certify"]
    has_cert_keyword = any(k in text_lower for k in cert_keywords)

    if not platform_verified:
        flags.append(f"Platform '{platform}' branding not clearly detected")
    if not has_cert_keyword:
        flags.append("No standard certificate language found")
    if course_claim and not course_match:
        flags.append(f"Course '{course_claim}' not clearly matched in document")

    # ── Step 5: determine validity ────────────────────────────────
    # Name mismatch is an automatic fail (potential fraud)
    name_failed = name_result["match"] is False
    platform_score = sum([platform_verified, course_match, has_cert_keyword])
    confidence_str = "high" if platform_score == 3 else "medium" if platform_score == 2 else "low"

    if name_failed:
        valid = False
        confidence_str = "low"
        title = "❌ Name Mismatch — Submission Flagged"
        detail = (
            f"The name on the certificate does not match your registered student name. "
            f"OCR detected: \"{name_result['ocr_name'] or 'could not read'}\". "
            f"Expected: \"{student_name}\". This submission has been flagged for faculty review."
        )
    elif name_result["match"] == "partial":
        # Partial match + platform OK → verified with warning
        valid = platform_score >= 2
        title = "⚠️ Verified with Name Warning" if valid else "⚠️ Partial Match — Review Needed"
        detail = (
            f"Certificate content looks valid, but name match is only partial "
            f"({name_result['confidence']:.0%} overlap). Faculty review may be required."
        )
    else:
        valid = platform_score >= 2
        title = "✅ Certificate Verified" if valid else (
            "⚠️ Certificate Flagged — Manual Review" if platform_score == 1 else "❌ Verification Failed"
        )
        parts = []
        if platform_verified:  parts.append(f"{platform} branding confirmed.")
        if has_cert_keyword:   parts.append("Certificate language detected.")
        if course_claim and course_match: parts.append(f"Course matched.")
        if name_result["match"] is True:  parts.append("Name verified ✓")
        if flags:              parts.append("Some fields could not be confirmed — see flags.")
        detail = " ".join(parts) or "Verification complete."

    return {
        "valid":    valid,
        "pending":  False,
        "confidence": confidence_str,
        "title":    title,
        "detail":   detail,
        "extracted": {
            "Student Name": extracted.get("name", "—"),
            "Course":       extracted.get("course") or course_claim or "—",
            "Issuer":       extracted.get("issuer") or platform,
            "Date":         extracted.get("date", "—"),
            "Score":        extracted.get("score", "—"),
            "Cert ID":      extracted.get("cert_id", "—"),
        },
        "flags":    flags,
        # Name match result for the frontend
        "nameMatch":       name_result["match"],    # True | False | "partial" | None
        "nameMatchDetail": {
            "ocr_name":        name_result["ocr_name"],
            "registered_name": name_result["registered_name"],
            "confidence":      name_result["confidence"],
            "method":          name_result["method"],
        },
        "raw_chars":  len(text),
        "pages_ocr":  pages_used,
    }


# ─── Field extraction helpers ────────────────────────────────────
def _extract_fields(text: str) -> dict:
    result = {}

    # Score / percentage
    score_m = re.search(r'(\d{1,3})\s*%|\bscore[:\s]+(\d+)', text, re.I)
    if score_m:
        result["score"] = (score_m.group(1) or score_m.group(2)) + "%"

    # Certificate ID
    id_m = re.search(r'(?:cert(?:ificate)?[- ]?(?:no|id|number)[:\s]+)([A-Z0-9\-]+)', text, re.I)
    if id_m:
        result["cert_id"] = id_m.group(1)

    # Date patterns
    date_m = re.search(
        r'\b(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4}|\w+ \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2})\b', text)
    if date_m:
        result["date"] = date_m.group(1)

    # Name — try multiple patterns in priority order
    name = None

    # Pattern 1: "This is to certify that <Name>"
    m = re.search(r'certif(?:y|ied|icate)?[^.]{0,30}that\s+([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})', text)
    if m: name = m.group(1)

    # Pattern 2: "Name: <Name>" or "Student: <Name>"
    if not name:
        m = re.search(r'(?:name|student)[:\s]+([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})', text)
        if m: name = m.group(1)

    # Pattern 3: "awarded to <Name>"
    if not name:
        m = re.search(r'(?:awarded|presented|given)\s+to\s+([A-Z][a-z]+(?: [A-Z][a-z]+){1,3})', text)
        if m: name = m.group(1)

    # Pattern 4: bold/large standalone name — line of 2-4 capitalized words at top of text
    if not name:
        for line in text.splitlines()[:15]:
            line = line.strip()
            if re.match(r'^[A-Z][a-z]+( [A-Z][a-z]+){1,3}$', line):
                name = line
                break

    if name:
        result["name"] = name.strip()

    # Issuer
    iss_m = re.search(r'(?:offered by|presented by|by|from)\s+([A-Z][A-Za-z ]{3,40})', text)
    if iss_m:
        result["issuer"] = iss_m.group(1).strip()

    # Course
    course_m = re.search(r'(?:course|in|for)\s*:\s*(.+)', text, re.I)
    if course_m:
        result["course"] = course_m.group(1).strip()[:80]

    return result


def _error_result(msg: str) -> dict:
    return {
        "valid": False, "pending": True, "confidence": "low",
        "title": "Processing Error",
        "detail": msg,
        "extracted": {}, "flags": [msg],
        "nameMatch": None, "nameMatchDetail": {}
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REST ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/api/verify", methods=["POST"])
def submit_verification():
    """
    Accepts multipart/form-data:
      file         : certificate image or PDF
      platform     : e.g. NPTEL
      course       : course name claimed by student
      student_name : registered student name for name matching  ← NEW
    Returns: { task_id }
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f            = request.files["file"]
    platform     = request.form.get("platform", "Unknown")
    course       = request.form.get("course", "")
    student_name = request.form.get("student_name", "")   # ← NEW

    file_bytes = f.read()
    filename   = f.filename or "upload"
    mime       = f.content_type or ""
    file_type  = "pdf" if "pdf" in mime or filename.lower().endswith(".pdf") else "image"

    _gc_tasks()
    task_id = uuid.uuid4().hex
    _set_task(task_id, state="PENDING", step="queued")

    def _job():
        try:
            _set_task(task_id, state="PROGRESS", step="extracting_text")
            result = verify_certificate(task_id, file_bytes, filename, platform,
                                        course, file_type, student_name)
            _set_task(task_id, state="SUCCESS", result=result)
        except Exception as e:
            _set_task(task_id, state="FAILURE", error=f"{type(e).__name__}: {e}"[:300])

    EXECUTOR.submit(_job)
    return jsonify({"task_id": task_id}), 202


@app.route("/api/verify/<task_id>", methods=["GET"])
def poll_verification(task_id):
    """Poll for task result."""
    with TASKS_LOCK:
        t = dict(TASKS.get(task_id) or {})
    if not t:
        return jsonify({"status": "error", "error": "Unknown or expired task"})
    st = t.get("state")
    if st == "PENDING":
        return jsonify({"status": "pending"})
    if st == "PROGRESS":
        return jsonify({"status": "processing", "step": t.get("step", "")})
    if st == "SUCCESS":
        return jsonify({"status": "done", "result": t["result"]})
    return jsonify({"status": "error", "error": t.get("error", "Unknown error")})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status":      "ok",
        "tesseract":   _check_tesseract(),
        "pdf_support": PDF_SUPPORT,
        "name_match":  True,   # flag that this backend supports name matching
        "time":        datetime.utcnow().isoformat()
    })


def _check_tesseract():
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception as e:
        return f"error: {e}"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)