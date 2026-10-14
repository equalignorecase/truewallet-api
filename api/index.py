import os
import re
import sys
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from curl_cffi import requests

PROXY_URL = os.getenv("PROXY_URL") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")

app = FastAPI(
    title="TrueMoney Voucher API",
    description="API สำหรับตรวจสอบ (Verify) และกดรับ (Redeem) ซองอั่งเปา TrueMoney Wallet ด้วย TLS Impersonation หลบ Cloudflare | Powered by equalsignorecase",
    version="1.0.0",
)

# ─── CORS Middleware ────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Constants & Thai Messages ──────────────────────────────────────
TRUEMONEY_STATUS_MESSAGES = {
    "SUCCESS": "รับเงินสำเร็จ / ข้อมูลซองถูกต้อง",
    "VOUCHER_OUT_OF_STOCK": "ซองของขวัญถูกรับเงินไปหมดแล้ว",
    "VOUCHER_EXPIRED": "ซองของขวัญหมดอายุแล้ว (เกิน 72 ชั่วโมง)",
    "VOUCHER_NOT_FOUND": "ไม่พบข้อมูลซองของขวัญในระบบ หรือลิงก์ไม่ถูกต้อง",
    "CANNOT_GET_OWN_VOUCHER": "ไม่สามารถกดรับซองของตนเองได้ (เบอร์ผู้สร้างซองตรงกับเบอร์ผู้รับ)",
    "TARGET_USER_REDEEMED": "เบอร์โทรศัพท์นี้เคยรับเงินจากซองนี้ไปแล้ว",
    "TARGET_USER_NOT_FOUND": "ไม่พบบัญชี TrueMoney Wallet ของเบอร์โทรศัพท์นี้",
    "INTERNAL_ERROR": "เกิดข้อผิดพลาดจากระบบ TrueMoney กรุณาลองใหม่อีกครั้ง",
}

# ─── Exception Handlers ─────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """แจ้งเฉพาะชื่อฟิลด์ที่ขาดแบบกระชับ"""
    missing_fields = []

    for err in exc.errors():
        loc = err.get("loc", [])
        field = loc[-1] if loc else "unknown"

        # กรณีไม่ส่ง Body
        if field == "body" or loc == ["body"]:
            for f_key in ["mobile", "voucher_url"]:
                if f_key not in missing_fields:
                    missing_fields.append(f_key)
            continue

        field_name = str(field)
        if field_name not in missing_fields:
            missing_fields.append(field_name)

    if missing_fields:
        msg = f"ขาดฟิลด์ {', '.join(missing_fields)}"
    else:
        msg = "ข้อมูลไม่ถูกต้อง"

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": msg},
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """คืน detail ตามมาตรฐาน FastAPI"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

# ─── Pydantic Models ────────────────────────────────────────────────
class VoucherRequest(BaseModel):
    mobile: str = Field(..., min_length=1, description="เบอร์มือถือ TrueMoney Wallet (เช่น 0812345678)")
    voucher_url: str = Field(..., min_length=1, description="ลิงก์ซองอั่งเปา หรือ Voucher Code")

# ─── Helper Functions ───────────────────────────────────────────────
def clean_mobile(mobile: str) -> str:
    """ทำความสะอาดเบอร์มือถือ ตัดช่องว่าง/ขีด และแปลง +66 เป็น 0"""
    mobile = mobile.replace("-", "").replace(" ", "").strip()
    if mobile.startswith("+66"):
        mobile = "0" + mobile[3:]
    elif mobile.startswith("66") and len(mobile) == 11:
        mobile = "0" + mobile[2:]
    return mobile

def validate_mobile(mobile: str) -> bool:
    """ตรวจสอบความถูกต้องของเบอร์มือถือไทย 10 หลัก"""
    return bool(re.match(r"^0[0-9]{9}$", clean_mobile(mobile)))

def extract_voucher_hash(voucher: str) -> str:
    """สกัดเฉพาะ voucher hash จาก URL หรือสตริง และตรวจสอบความถูกต้องเบื้องต้น"""
    if not voucher:
        return ""
    voucher = voucher.strip()
    if "?v=" in voucher:
        voucher = voucher.split("?v=")[-1]
    elif "/v/" in voucher:
        voucher = voucher.split("/v/")[-1]

    # ตัด query params หรือ fragment อื่นๆ ที่อาจติดมา
    voucher = voucher.split("&")[0].split("#")[0].strip()

    # Hash ของ TrueMoney ต้องเป็นตัวอักษรภาษาอังกฤษหรือตัวเลข (Alphanumeric) 10-60 ตัว
    if not re.match(r"^[a-zA-Z0-9]{10,60}$", voucher):
        return ""

    return voucher

def request_truemoney(
    method: str,
    url: str,
    json_data: Optional[dict] = None,
    voucher_hash: str = "",
) -> dict:
    """ส่ง Request ไปยัง TrueMoney โดยตรง ด้วย curl_cffi จำลอง Firefox TLS Handshake"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://gift.truemoney.com",
        "Referer": f"https://gift.truemoney.com/campaign/?v={voucher_hash}" if voucher_hash else "https://gift.truemoney.com/campaign/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
        "Accept-Language": "th,en-US;q=0.7,en;q=0.3",
    }

    try:
        kwargs = {
            "headers": headers,
            "impersonate": "firefox147",
            "timeout": 15,
        }
        if PROXY_URL:
            kwargs["proxy"] = PROXY_URL

        try:
            if method.upper() == "POST":
                resp = requests.post(url, json=json_data, **kwargs)
            else:
                resp = requests.get(url, **kwargs)
        except Exception:
            # สำรอง Firefox 133 หาก runtime ไม่รองรับ 147
            kwargs["impersonate"] = "firefox133"
            if method.upper() == "POST":
                resp = requests.post(url, json=json_data, **kwargs)
            else:
                resp = requests.get(url, **kwargs)

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        # เติมคำแปลภาษาไทยสำหรับ status code ของ TrueMoney
        if isinstance(data, dict):
            status_obj = data.get("status")
            if isinstance(status_obj, dict):
                code = status_obj.get("code")
                if code and code in TRUEMONEY_STATUS_MESSAGES:
                    status_obj["message_th"] = TRUEMONEY_STATUS_MESSAGES[code]

        return {
            "proxy_error": False,
            "http_code": resp.status_code,
            "data": data,
        }

    except Exception as e:
        return {
            "proxy_error": True,
            "http_code": 502,
            "code": "REQUEST_FAILED",
            "message": str(e),
            "detail": str(e),
        }


# ─── Endpoints ──────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "message": "TrueMoney Voucher API (Python / FastAPI)",
        "powered_by": "equalsignorecase",
        "docs": "/docs",
        "endpoints": {
            "health": "GET /health",
            "verify": "POST /verify or GET /verify/{voucher_hash}/{mobile}",
            "redeem": "POST /redeem or GET /truemoney/{voucher_hash}/{mobile}",
        }
    }

@app.get("/health")
def health_check():
    """ตรวจสอบสถานะของ API และ Engine ที่ใช้งาน"""
    return {
        "status": "ok",
        "powered_by": "equalsignorecase",
        "engine": "curl_cffi",
        "impersonate": "firefox147",
        "python_version": sys.version,
    }

@app.api_route("/verify", methods=["GET", "POST"])
def verify_voucher(
    payload: Optional[VoucherRequest] = None,
    mobile: Optional[str] = None,
    voucher_url: Optional[str] = None,
    v: Optional[str] = None,
):
    """ตรวจสอบสถานะซองและยอดเงิน โดยยังไม่กดรับเงิน (รองรับทั้ง JSON Body, Query Params, GET และ POST)"""
    m = (payload.mobile if payload else None) or mobile
    u = (payload.voucher_url if payload else None) or voucher_url or v

    if not m or not u:
        missing = []
        if not m:
            missing.append("mobile")
        if not u:
            missing.append("voucher_url")
        return JSONResponse(status_code=400, content={"detail": f"ขาดฟิลด์ {', '.join(missing)}"})

    clean_m = clean_mobile(m)
    if not validate_mobile(clean_m):
        raise HTTPException(status_code=400, detail="เบอร์มือถือไม่ถูกต้อง (ต้องเป็น 10 หลักขึ้นต้นด้วย 0)")

    voucher_hash = extract_voucher_hash(u)
    if not voucher_hash:
        raise HTTPException(status_code=400, detail="รูปแบบลิงก์ซองหรือรหัส Voucher ไม่ถูกต้อง")

    url = f"https://gift.truemoney.com/campaign/vouchers/{voucher_hash}/verify?mobile={clean_m}"
    result = request_truemoney("GET", url, voucher_hash=voucher_hash)

    http_status = result.get("http_code", 200) if not result.get("proxy_error") else 502
    return JSONResponse(status_code=http_status, content=result)

@app.api_route("/redeem", methods=["GET", "POST"])
def redeem_voucher(
    payload: Optional[VoucherRequest] = None,
    mobile: Optional[str] = None,
    voucher_url: Optional[str] = None,
    v: Optional[str] = None,
):
    """กดรับเงินจากซองอั่งเปาเข้ากระเป๋า TrueMoney Wallet จริง (รองรับทั้ง JSON Body, Query Params, GET และ POST)"""
    m = (payload.mobile if payload else None) or mobile
    u = (payload.voucher_url if payload else None) or voucher_url or v

    if not m or not u:
        missing = []
        if not m:
            missing.append("mobile")
        if not u:
            missing.append("voucher_url")
        return JSONResponse(status_code=400, content={"detail": f"ขาดฟิลด์ {', '.join(missing)}"})

    clean_m = clean_mobile(m)
    if not validate_mobile(clean_m):
        raise HTTPException(status_code=400, detail="เบอร์มือถือไม่ถูกต้อง (ต้องเป็น 10 หลักขึ้นต้นด้วย 0)")

    voucher_hash = extract_voucher_hash(u)
    if not voucher_hash:
        raise HTTPException(status_code=400, detail="รูปแบบลิงก์ซองหรือรหัส Voucher ไม่ถูกต้อง")

    url = f"https://gift.truemoney.com/campaign/vouchers/{voucher_hash}/redeem"
    body = {
        "mobile": clean_m,
        "voucher_hash": voucher_hash,
    }
    result = request_truemoney("POST", url, json_data=body, voucher_hash=voucher_hash)

    http_status = result.get("http_code", 200) if not result.get("proxy_error") else 502
    return JSONResponse(status_code=http_status, content=result)

def resolve_hash_and_mobile(param1: str, param2: str) -> tuple[str, str]:
    """สลับตำแหน่งให้อัตโนมัติ หากส่งเบอร์โทรมาก่อนรหัสซอง หรือรหัสซองมาก่อนเบอร์โทร"""
    if validate_mobile(param1) and not validate_mobile(param2):
        return param2, param1
    return param1, param2

# ─── Missing Parameter Fallback Routes ──────────────────────────────
@app.api_route("/truemoney", methods=["GET", "POST"])
def missing_truemoney_all():
    return JSONResponse(status_code=400, content={"detail": "ขาดฟิลด์ voucher_hash, mobile"})

@app.api_route("/truemoney/{voucher_hash}", methods=["GET", "POST"])
def missing_truemoney_mobile(voucher_hash: str):
    return JSONResponse(status_code=400, content={"detail": "ขาดฟิลด์ mobile"})

@app.api_route("/verify/{voucher_hash}", methods=["GET", "POST"])
def missing_verify_mobile(voucher_hash: str):
    return JSONResponse(status_code=400, content={"detail": "ขาดฟิลด์ mobile"})

# ─── Quick Endpoints (รองรับทั้ง GET และ POST, สลับลำดับเบอร์/hash อัตโนมัติ) ──
@app.api_route("/truemoney/{param1}/{param2}", methods=["GET", "POST"])
def quick_redeem(param1: str, param2: str):
    """Endpoint ลัดสำหรับกดรับซอง รองรับทั้ง GET และ POST"""
    voucher_hash, mobile = resolve_hash_and_mobile(param1, param2)
    return redeem_voucher(VoucherRequest(mobile=mobile, voucher_url=voucher_hash))

@app.api_route("/verify/{param1}/{param2}", methods=["GET", "POST"])
def quick_verify(param1: str, param2: str):
    """Endpoint ลัดสำหรับตรวจสอบซอง รองรับทั้ง GET และ POST"""
    voucher_hash, mobile = resolve_hash_and_mobile(param1, param2)
    return verify_voucher(VoucherRequest(mobile=mobile, voucher_url=voucher_hash))

