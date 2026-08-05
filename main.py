from fastapi import FastAPI
from pydantic import BaseModel
import geopandas as gpd
from shapely.geometry import Point
import os

app = FastAPI()

# Load data GeoJSON saat server pertama kali menyala
file_path = os.path.join("data", "kawasan_hutan_sumut.geojson")
print(f"Loading data dari {file_path}...")

try:
    gdf_hutan = gpd.read_file(file_path)
    print("Data kawasan hutan berhasil dimuat!")
except Exception as e:
    print(f"Gagal memuat data: {e}")
    gdf_hutan = None

# Format data yang diterima dari frontend
class RequestKoord(BaseModel):
    lat: float
    lng: float

@app.post("/analyze")
def analyze_data(req: RequestKoord):
    if gdf_hutan is None:
        return {"error": "Data spasial belum dimuat di server"}
        
    # Buat titik dari koordinat (Shapely pakai format X,Y yaitu Lng,Lat)
    titik = Point(req.lng, req.lat)
    
    # Cek apakah titik masuk ke poligon kawasan hutan
    cek_hutan = gdf_hutan[gdf_hutan.contains(titik)]
    in_forest = not cek_hutan.empty
    
    # Ambil nama kawasan jika ada
    if in_forest:
        # Coba ambil kolom nama (sesuaikan jika namanya beda, misal 'NAMA', 'Name', dll)
        forest_name = cek_hutan.iloc[0].get('NAMA_KAW', cek_hutan.iloc[0].get('Name', 'Kawasan Hutan'))
    else:
        forest_name = "Tidak berada di kawasan hutan"
    
    return {
        "inForest": in_forest,
        "forestName": forest_name,
        "coordinates": {"lat": req.lat, "lng": req.lng}
    }