"""
omnor.ru — отдача лендинга и приём заявок с формы.

Заявка уходит письмом на почту. Перед отправкой она всегда пишется в stdout:
логи приложения видно в панели Timeweb, и если почта отвалится, контакт
всё равно можно будет достать оттуда.
"""
import os
import re
import json
import time
import smtplib
import logging
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).parent
INDEX = ROOT / "index.html"

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.timeweb.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_LOGIN = os.getenv("SMTP_LOGIN", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_TO = os.getenv("MAIL_TO", "") or SMTP_LOGIN

# не больше пяти заявок с одного адреса в час — от случайного спама хватит
RATE_LIMIT = 5
RATE_WINDOW = 3600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("omnor")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_hits: dict[str, list[float]] = {}


def _too_often(ip: str) -> bool:
    now = time.time()
    seen = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
    _hits[ip] = seen + [now]
    return len(seen) >= RATE_LIMIT


def _client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")


def _send(subject: str, body: str) -> None:
    if not (SMTP_LOGIN and SMTP_PASSWORD and MAIL_TO):
        raise RuntimeError("почта не настроена: нет SMTP_LOGIN / SMTP_PASSWORD / MAIL_TO")
    msg = EmailMessage()
    msg["From"] = SMTP_LOGIN
    msg["To"] = MAIL_TO
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.login(SMTP_LOGIN, SMTP_PASSWORD)
        s.send_message(msg)


@app.post("/api/zayavka")
async def zayavka(req: Request):
    ip = _client_ip(req)
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False}, status_code=400)

    # ловушка: поле скрыто от людей, заполняют только боты.
    # отвечаем как при успехе, чтобы бот не подбирал обход
    if str(data.get("website", "")).strip():
        log.info("ловушка сработала, ip=%s", ip)
        return {"ok": True}

    name = str(data.get("name", "")).strip()[:120]
    phone = str(data.get("phone", "")).strip()[:60]
    company = str(data.get("company", "")).strip()[:200]
    page = str(data.get("page", "")).strip()[:300]

    if len(name) < 2 or len(re.findall(r"\d", phone)) < 10:
        return JSONResponse({"ok": False, "error": "bad_fields"}, status_code=422)

    if _too_often(ip):
        log.warning("слишком часто, ip=%s", ip)
        return JSONResponse({"ok": False, "error": "rate"}, status_code=429)

    # пишем до отправки: если почта не дойдёт, контакт останется в логах
    log.info("ЗАЯВКА %s", json.dumps(
        {"name": name, "phone": phone, "company": company, "page": page, "ip": ip},
        ensure_ascii=False))

    body = "\n".join([
        f"Имя:      {name}",
        f"Телефон:  {phone}",
        f"Компания: {company or '—'}",
        "",
        f"Страница: {page or '—'}",
        f"IP:       {ip}",
    ])
    try:
        _send(f"Заявка с сайта — {name}", body)
    except Exception as e:
        log.error("почта не ушла: %s", e)
        return JSONResponse({"ok": False, "error": "mail"}, status_code=502)

    return {"ok": True}


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "index": INDEX.exists(),
        "mail_configured": bool(SMTP_LOGIN and SMTP_PASSWORD and MAIL_TO),
    }


@app.get("/{path:path}")
async def page(path: str):
    """Лендинг — одна страница, любой адрес отдаёт её же."""
    return FileResponse(INDEX, media_type="text/html; charset=utf-8")
