import os
import math
import asyncio
import sqlite3
import logging
import json
from datetime import date
from contextlib import asynccontextmanager
import secrets
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from fastapi.responses import Response
from aiohttp import ClientSession
from myskoda import MySkoda

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_FILE", "data/skoda.db")
SKODA_USER = os.getenv("SKODA_USER")
SKODA_PASS = os.getenv("SKODA_PASS")
VIN = os.getenv("VIN")
TOTAL_LEASING_KM = int(os.getenv("TOTAL_LEASING_KM", "40000"))
LEASING_START_DATE = date.fromisoformat(os.getenv("LEASING_START_DATE", "2024-01-01"))
LEASING_END_DATE = date.fromisoformat(os.getenv("LEASING_END_DATE", "2027-01-01"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_HOURS", "24")) * 3600

APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "secret")

security = HTTPBasic()

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = APP_USERNAME.encode("utf8")
    is_correct_username = secrets.compare_digest(current_username_bytes, correct_username_bytes)

    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = APP_PASSWORD.encode("utf8")
    is_correct_password = secrets.compare_digest(current_password_bytes, correct_password_bytes)

    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS mileage_logs (date TEXT PRIMARY KEY, mileage INTEGER)")

def save_mileage(mileage: int):
    today = date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT OR REPLACE INTO mileage_logs (date, mileage) VALUES (?, ?)", (today, mileage))

def get_latest_mileage() -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT mileage FROM mileage_logs ORDER BY date DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else 0

def get_mileage_history() -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT date, mileage FROM mileage_logs ORDER BY date ASC")
        rows = cur.fetchall()
        return {
            "dates": [row[0] for row in rows],
            "mileages": [row[1] for row in rows]
        }

async def fetch_skoda_data():
    while True:
        try:
            async with ClientSession() as session:
                myskoda = MySkoda(session, mqtt_enabled=False)
                await myskoda.connect(SKODA_USER, SKODA_PASS)

                health = await myskoda.get_health(VIN)
                mileage = health.mileage_in_km or 0

                if mileage == 0:
                    maintenance = await myskoda.get_maintenance(VIN)
                    if maintenance.maintenance_report:
                        mileage = maintenance.maintenance_report.mileage_in_km or 0

                if mileage > 0:
                    logger.info("Successfully fetched mileage: %s KM", mileage)
                    save_mileage(mileage)
                else:
                    logger.warning("Could not find mileage in vehicle data.")

                await myskoda.disconnect()
        except Exception as e:
            logger.error("Failed to fetch Skoda data: %s", e)

        await asyncio.sleep(POLL_INTERVAL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    skoda_task = asyncio.create_task(fetch_skoda_data())
    yield
    skoda_task.cancel()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@app.get("/")
async def read_root(request: Request, username: str = Depends(verify_credentials)):
    current_mileage = get_latest_mileage()
    history = get_mileage_history()

    total_days = (LEASING_END_DATE - LEASING_START_DATE).days
    days_passed = (date.today() - LEASING_START_DATE).days
    days_remaining = total_days - days_passed

    if days_passed < 0: days_passed = 0
    if days_remaining < 0: days_remaining = 0
    if current_mileage == 0: current_mileage = 0

    remaining_km = TOTAL_LEASING_KM - current_mileage
    months_remaining = days_remaining / 30.44
    km_per_month = remaining_km / months_remaining if months_remaining > 0 else 0

    daily_allowance = TOTAL_LEASING_KM / total_days if total_days > 0 else 0
    actual_daily_burn = current_mileage / days_passed if days_passed > 0 else 0
    projected_total = actual_daily_burn * total_days

    expected_mileage_so_far = daily_allowance * days_passed
    health_diff = current_mileage - expected_mileage_so_far
    health_status = "OVER BUDGET" if health_diff > 0 else "UNDER BUDGET"

    home_office_days = math.ceil(health_diff / daily_allowance) if health_diff > 0 and daily_allowance > 0 else 0

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "current_mileage": current_mileage,
            "remaining_km": remaining_km,
            "months_remaining": round(months_remaining, 1),
            "km_per_month": round(km_per_month),
            "daily_allowance": round(daily_allowance, 1),
            "actual_daily_burn": round(actual_daily_burn, 1),
            "projected_total": round(projected_total),
            "health_status": health_status,
            "health_diff": abs(int(health_diff)),
            "home_office_days": home_office_days,
            "total_km": TOTAL_LEASING_KM,
            "progress_percent": round((current_mileage / TOTAL_LEASING_KM) * 100, 1) if TOTAL_LEASING_KM > 0 else 0,
            "time_percent": round((days_passed / total_days) * 100, 1) if total_days > 0 else 0,
            "history_dates_json": json.dumps(history["dates"]),
            "history_mileages_json": json.dumps(history["mileages"])
        }
    )
