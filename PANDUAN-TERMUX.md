# Jalanin ClipRadar di HP sendiri (Termux)

Kenapa cara ini bisa, padahal di Vercel gagal terus? Karena yang diblokir YouTube itu **IP server cloud**. Kalau app-nya jalan di HP kamu, transcript diambil pakai **IP HP kamu sendiri (IP rumahan)** — dan itu tidak diblokir. Hasilnya: analisis full otomatis lagi, tanpa API transcript berbayar, tanpa tempel manual.

Kekurangannya: app cuma bisa dibuka dari HP ini selama Termux nyala. Buat dipakai sendiri, cukup banget.

## 1. Install Termux

Install **Termux dari F-Droid**: https://f-droid.org/packages/com.termux/
Jangan dari Play Store — versi di sana sudah kuno dan reponya mati.

## 2. Install ClipRadar (sekali jalan)

Buka Termux, tempel perintah ini persis, lalu Enter:

```bash
pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/KimTaehyung666/clipflow/main/termux-install.sh | bash
```

- Download total ±300MB (Python + compiler, karena sebagian paket dicompile langsung di HP — di Android 32-bit memang begitu). **Pakai WiFi.**
- Prosesnya bisa 5–15 menit tergantung HP & koneksi. Biarkan sampai muncul "=== SELESAI ===".
- Di akhir kamu diminta menempel **GROQ_API_KEY** (ambil di console.groq.com → API Keys — key yang sama yang selama ini kamu pakai). Key tersimpan di file `.env` di HP kamu saja.

## 3. Jalanin app-nya

Setiap mau pakai, buka Termux lalu:

```bash
cd ~/clipflow && bash termux-run.sh
```

Lalu buka **http://localhost:5002** di browser HP. Tempel link YouTube → Analisis → clip keluar, full otomatis.

Berhenti: di Termux tekan tombol **CTRL** (di baris tombol tambahan Termux) lalu **C**, atau tutup Termux sekalian.

## Tips

- Jangan bersihkan Termux dari recent apps selama app dipakai, biar tidak dimatikan Android.
- Kalau analisis pertama agak lambat, itu normal (Groq + ambil transcript butuh beberapa detik).
- Update app ke versi terbaru: `cd ~/clipflow && git pull`, lalu jalanin lagi.
- Kalau ada error merah pas install, screenshot aja errornya — biasanya urusan paket yang gagal compile, ada jalan keluarnya.
