#!/data/data/com.termux/files/usr/bin/bash
# ClipRadar — jalanin app di Termux.
# Pakai:  cd ~/clipflow && bash termux-run.sh
cd "$(dirname "$0")"

# Biar Android tidak mematikan proses Termux pas layar mati.
termux-wake-lock 2>/dev/null || true

echo "ClipRadar jalan di http://localhost:5002"
echo "Buka alamat itu di browser HP kamu. Berhenti: tombol CTRL lalu C, atau tutup Termux."
python app.py
