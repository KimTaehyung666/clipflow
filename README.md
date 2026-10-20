# ClipRadar (clipflow)

Web app Flask buat rekomendasi momen clip dari video YouTube: transcript diambil otomatis, lalu AI (Groq) milih momen terbaik + bikin hook & hashtag per gaya konten.

## Jalanin lokal

```bash
pip install -r requirements.txt
cp .env.example .env   # lalu isi GROQ_API_KEY kamu
python app.py          # buka http://localhost:5002
```

## Deploy ke Render (gratis, tanpa kartu)

1. Di Render: **New → Blueprint**, pilih repo ini (ada `render.yaml`).
2. Isi environment variable `GROQ_API_KEY`.
3. Deploy. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`.

## Catatan

- Model default: `openai/gpt-oss-120b` (pengganti resmi `llama-3.3-70b-versatile` yang dimatikan Groq 16 Agu 2026). Bisa diganti via env `GROQ_MODEL`.
- Jangan pernah commit file `.env` — sudah masuk `.gitignore`.
