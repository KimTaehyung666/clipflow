# ClipRadar v2 (clipflow)

Tools clipper AI lengkap: tempel link YouTube → AI (Groq) nyari momen terbaik + bikin hook & hashtag → **potong clip-nya langsung jadi MP4** (bisa vertikal 9:16 + caption nempel), ada mode batch, riwayat, pencarian momen, dan export.

## Fitur

- 🎯 Analisis AI dengan target durasi clip (30/45/60/90 detik) + gaya hook per jenis konten
- ✂️ Potong video jadi MP4 (yt-dlp + ffmpeg), opsi **vertikal 9:16** (background blur) dan **auto-caption** dari transcript — butuh app jalan lokal/Termux, tidak bisa di serverless
- 📥 Batch: banyak link sekaligus, diproses antri
- 🕘 Riwayat analisis tersimpan (SQLite lokal) + export hasil ke .txt
- 🔍 Cari momen: ketik kata kunci, lompat ke timestamp di transcript
- 📄 Fallback transcript berlapis: tempelan manual → library → TranscriptAPI → Supadata

## Jalanin di HP sendiri (Termux) — transcript full otomatis

YouTube memblokir transcript dari IP cloud, tapi tidak dari IP HP/rumahan. Kalau app ini dijalankan di HP Android pakai Termux, analisisnya full otomatis lagi tanpa API transcript berbayar. Panduan lengkap: [PANDUAN-TERMUX.md](PANDUAN-TERMUX.md).

## Jalanin lokal (PC/laptop)

```bash
pip install -r requirements.txt
# fitur potong video butuh ffmpeg di PATH (Linux: apt install ffmpeg)
cp .env.example .env   # lalu isi GROQ_API_KEY kamu
python app.py          # buka http://localhost:5002
```

## Deploy ke Render (gratis, tanpa kartu)

1. Di Render: **New → Blueprint**, pilih repo ini (ada `render.yaml`).
2. Isi environment variable `GROQ_API_KEY`.
3. Deploy. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`.

Catatan: di deploy cloud (Vercel/Render) fitur potong video mati (serverless/tidak ada ffmpeg persisten) dan transcript otomatis bisa keblokir IP — lihat bagian transcript di bawah.

## Catatan

- Model default: `openai/gpt-oss-120b` (pengganti resmi `llama-3.3-70b-versatile` yang dimatikan Groq 16 Agu 2026). Bisa diganti via env `GROQ_MODEL`.
- Jangan pernah commit file `.env` — sudah masuk `.gitignore`. Folder `data/` (riwayat, cache, clip) juga lokal saja.

## Transcript & blokir IP YouTube

YouTube memblokir pengambilan transcript dari IP cloud (Vercel/Render/dsb), jadi urutan sumber transcript di app ini:

1. **Tempelan manual user** — kolom "Tempel transcript manual" di web (salin dari panel transkrip YouTube). Selalu jalan, tanpa key.
2. **Library langsung** — gratis tanpa kuota, jalan dari IP rumahan (lokal/Termux).
3. **TranscriptAPI** (`TRANSCRIPT_API_KEY`, daftar gratis di transcriptapi.com, 100 kredit awal tanpa kartu).
4. **Supadata** (`SUPADATA_API_KEY`, daftar gratis di supadata.ai, 100 kredit/bulan tanpa kartu).

Buat deploy di Vercel: isi `GROQ_API_KEY` + salah satu key transcript di atas di **Project → Settings → Environment Variables**, lalu redeploy.
