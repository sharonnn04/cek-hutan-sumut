"""Cek lokasi Kawasan Hutan Sumatera Utara (SK 11580/2025)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import geopandas as gpd
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from shapely.geometry import Point

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "cek_hutan.db"))
SECRET_KEY = os.getenv("SECRET_KEY", "ganti-secret-key-produksi")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com").lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ubah-password-admin")
FREE_LIMIT = 5
DAY_SECONDS = 24 * 60 * 60

# Windows can register .css as application/x-css. Chromium only applies a
# stylesheet served with the standard text/css MIME type.
mimetypes.add_type("text/css", ".css", strict=True)
app = FastAPI(title="Cek Kawasan Hutan Sumatera Utara", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database():
    connection = db()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def password_hash(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    salt_hex, _ = encoded.split("$", 1)
    return hmac.compare_digest(password_hash(password, bytes.fromhex(salt_hex)), encoded)


def token_for(user: sqlite3.Row) -> str:
    payload = {"id": user["id"], "role": user["role"], "exp": int(time.time()) + 7 * DAY_SECONDS}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(SECRET_KEY.encode(), raw, hashlib.sha256).digest()
    return (raw + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def decode_token(value: str) -> dict[str, Any]:
    try:
        payload, signature = value.encode().split(b".", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, base64.urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))):
            raise ValueError("signature")
        data = json.loads(base64.urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
        if data["exp"] < time.time():
            raise ValueError("expired")
        return data
    except (ValueError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Sesi tidak valid atau telah berakhir.")


def current_user(authorization: Optional[str] = Header(None)) -> Optional[sqlite3.Row]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    payload = decode_token(authorization[7:])
    with database() as connection:
        user = connection.execute("SELECT * FROM users WHERE id = ? AND active = 1", (payload["id"],)).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Akun tidak aktif.")
    return user


def required_admin(user: Optional[sqlite3.Row] = Depends(current_user)) -> sqlite3.Row:
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Khusus administrator.")
    return user


def setup_database() -> None:
    with database() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', active INTEGER NOT NULL DEFAULT 1,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage_events (
          id INTEGER PRIMARY KEY, identity TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS usage_identity_time ON usage_events(identity, created_at);
        CREATE TABLE IF NOT EXISTS checks (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
          input_label TEXT, result_json TEXT NOT NULL, created_at INTEGER NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """)
        exists = connection.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
        if not exists:
            connection.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                               ("Administrator", ADMIN_EMAIL, password_hash(ADMIN_PASSWORD), "admin", int(time.time())))


def read_layer(path: Path) -> Optional[gpd.GeoDataFrame]:
    try:
        frame = gpd.read_file(path)
        if frame.empty:
            return None
        if frame.crs is None:
            raise ValueError("CRS tidak tercantum")
        return frame.to_crs("EPSG:4326")
    except Exception as exc:
        print(f"Layer {path.name} tidak dimuat: {exc}")
        return None


LAYERS: dict[str, gpd.GeoDataFrame] = {}


def load_layers() -> None:
    forest = read_layer(DATA_DIR / "kawasan_hutan_sumut.geojson")
    if forest is not None:
        LAYERS["kawasan_hutan"] = forest
    # Tambahkan Shapefile/GeoJSON batas administrasi ke data/administrasi/.
    admin_dir = DATA_DIR / "administrasi"
    if admin_dir.exists():
        for file in list(admin_dir.glob("*.shp")) + list(admin_dir.glob("*.geojson")):
            frame = read_layer(file)
            if frame is not None:
                LAYERS[file.stem.lower()] = frame


def json_value(value: Any) -> Any:
    """Convert NumPy/Pandas values from a shapefile into JSON-safe values."""
    if hasattr(value, "item"):
        value = value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def props(row: Any, preferred: tuple[str, ...]) -> dict[str, Any]:
    values = {str(key).lower(): json_value(value) for key, value in row.items() if key != "geometry" and value is not None}
    for name in preferred:
        if name in values and str(values[name]).strip():
            return {"name": values[name], "attributes": values}
    return {"name": None, "attributes": values}


def feature_at(layer: gpd.GeoDataFrame, point: Point) -> Optional[Any]:
    try:
        candidates = layer.iloc[list(layer.sindex.query(point, predicate="intersects"))]
    except Exception:
        candidates = layer
    result = candidates[candidates.geometry.covers(point)]
    return None if result.empty else result.iloc[0]


def analyze_point(latitude: float, longitude: float) -> dict[str, Any]:
    point = Point(longitude, latitude)
    forest = LAYERS.get("kawasan_hutan")
    if forest is None:
        raise HTTPException(status_code=503, detail="Layer kawasan hutan belum tersedia.")
    match = feature_at(forest, point)
    response: dict[str, Any] = {
        "coordinates": {"lat": latitude, "lng": longitude},
        "reference": "SK 11580 Tahun 2025",
        "inForest": match is not None,
        "forest": None,
        "administration": {},
    }
    if match is not None:
        response["forest"] = props(match, ("namobj", "nama_kaw", "name", "desc_in", "kelas"))
    for key, layer in LAYERS.items():
        if key == "kawasan_hutan":
            continue
        row = feature_at(layer, point)
        if row is not None:
            response["administration"][key] = props(row, ("nama", "namobj", "name", "desa", "kecamatan", "kabupaten", "provinsi"))
    return response


def take_quota(identity: str) -> int:
    now = int(time.time())
    with database() as connection:
        connection.execute("DELETE FROM usage_events WHERE created_at < ?", (now - DAY_SECONDS,))
        used = connection.execute("SELECT COUNT(*) FROM usage_events WHERE identity = ? AND created_at >= ?",
                                  (identity, now - DAY_SECONDS)).fetchone()[0]
        if used >= FREE_LIMIT:
            raise HTTPException(status_code=429, detail="Batas 5 titik dalam 24 jam telah tercapai.")
        connection.execute("INSERT INTO usage_events(identity,created_at) VALUES(?,?)", (identity, now))
    return FREE_LIMIT - used - 1


class CoordinateRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: Optional[str] = Field(default=None, max_length=200)


class Credentials(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    email: str
    password: str = Field(min_length=8, max_length=128)


@app.on_event("startup")
def startup() -> None:
    setup_database()
    load_layers()


@app.get("/")
def homepage() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/analyze")
def analyze(payload: CoordinateRequest, request: Request, user: Optional[sqlite3.Row] = Depends(current_user)) -> dict[str, Any]:
    visitor = request.headers.get("X-Visitor-ID") or request.client.host or "anonymous"
    identity = f"user:{user['id']}" if user else f"guest:{hashlib.sha256(visitor.encode()).hexdigest()}"
    remaining = take_quota(identity)
    result = analyze_point(payload.lat, payload.lng)
    result["quotaRemaining"] = remaining
    if user:
        with database() as connection:
            connection.execute("INSERT INTO checks(user_id,latitude,longitude,input_label,result_json,created_at) VALUES(?,?,?,?,?,?)",
                (user["id"], payload.lat, payload.lng, payload.label, json.dumps(result), int(time.time())))
    return result


@app.get("/api/geocode")
def geocode(q: str = "") -> list[dict[str, Any]]:
    if len(q.strip()) < 3:
        raise HTTPException(status_code=422, detail="Masukkan alamat minimal 3 karakter.")
    query = urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 5, "countrycodes": "id"})
    try:
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{query}", headers={"User-Agent": "cek-hutan-sumut/1.0"})
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read())
        return [{"label": item["display_name"], "lat": float(item["lat"]), "lng": float(item["lon"])} for item in data]
    except Exception:
        raise HTTPException(status_code=503, detail="Layanan pencarian alamat sedang tidak tersedia.")


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: Credentials) -> dict[str, Any]:
    if not payload.name:
        raise HTTPException(status_code=422, detail="Nama wajib diisi.")
    with database() as connection:
        try:
            cursor = connection.execute("INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)",
                (payload.name, payload.email.lower().strip(), password_hash(payload.password), int(time.time())))
            user = connection.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Email sudah terdaftar.")
    return {"token": token_for(user), "user": {"name": user["name"], "email": user["email"], "role": user["role"]}}


@app.post("/api/auth/login")
def login(payload: Credentials) -> dict[str, Any]:
    with database() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (payload.email.lower().strip(),)).fetchone()
    if not user or not user["active"] or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email atau kata sandi salah.")
    return {"token": token_for(user), "user": {"name": user["name"], "email": user["email"], "role": user["role"]}}


@app.get("/api/history")
def history(user: Optional[sqlite3.Row] = Depends(current_user)) -> list[dict[str, Any]]:
    if not user:
        raise HTTPException(status_code=401, detail="Masuk untuk melihat riwayat.")
    with database() as connection:
        rows = connection.execute("SELECT * FROM checks WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()
    return [{"id": row["id"], "lat": row["latitude"], "lng": row["longitude"], "label": row["input_label"], "result": json.loads(row["result_json"]), "createdAt": row["created_at"]} for row in rows]


@app.get("/api/admin/users")
def users(_: sqlite3.Row = Depends(required_admin)) -> list[dict[str, Any]]:
    with database() as connection:
        rows = connection.execute("SELECT id,name,email,role,active,created_at FROM users ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/admin/users/{user_id}")
def change_user(user_id: int, active: Optional[bool] = None, role: Optional[str] = None, _: sqlite3.Row = Depends(required_admin)) -> dict[str, bool]:
    if role and role not in {"user", "admin"}:
        raise HTTPException(status_code=422, detail="Peran tidak valid.")
    if active is None and role is None:
        raise HTTPException(status_code=422, detail="Tidak ada perubahan.")
    fields, params = [], []
    if active is not None: fields.append("active=?"); params.append(int(active))
    if role: fields.append("role=?"); params.append(role)
    params.append(user_id)
    with database() as connection:
        if connection.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?", params).rowcount == 0:
            raise HTTPException(status_code=404, detail="Pengguna tidak ditemukan.")
    return {"ok": True}
