# Cek Kawasan Hutan Sumatera Utara

Aplikasi FastAPI + GeoPandas untuk mengecek titik terhadap GeoJSON kawasan hutan SK 11580 Tahun 2025.

## Jalankan lokal

```powershell
.\venv\Scripts\python -m pip install -r requirements.txt
.\venv\Scripts\uvicorn main:app --reload
```

Buka `http://127.0.0.1:8000`. Sebelum produksi, tetapkan `SECRET_KEY`, `ADMIN_EMAIL`, dan `ADMIN_PASSWORD` sebagai environment variable. Database SQLite dibuat otomatis di `cek_hutan.db`.

## Layer administrasi

Tambahkan Shapefile lengkap (semua berkas `.shp`, `.shx`, `.dbf`, `.prj`) atau GeoJSON batas desa, kecamatan, kabupaten, dan provinsi ke `data/administrasi/`. Nama file akan tampil sebagai kategori administrasi dalam hasil pemeriksaan.

## Publikasi di Render

Push proyek ini ke GitHub, lalu buat Web Service baru dari repositori tersebut atau gunakan `render.yaml`. Tetapkan `SECRET_KEY` yang panjang dan acak, serta `ADMIN_EMAIL` dan `ADMIN_PASSWORD`. Untuk riwayat pengguna yang tahan restart/deploy, gunakan penyimpanan persisten dan arahkan `DATABASE_PATH` ke sana, atau migrasikan database ke PostgreSQL. Jangan gunakan kredensial admin bawaan pada produksi.
