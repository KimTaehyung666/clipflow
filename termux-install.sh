#!/data/data/com.termux/files/usr/bin/bash
# ClipRadar — installer sekali jalan buat Termux (Android).
# Jalanin:  pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/KimTaehyung666/clipflow/main/termux-install.sh | bash
set -e

echo "=== ClipRadar: install di Termux ==="
echo "Download total bisa ±300MB (Python + compiler). Pake WiFi ya."
echo ""

# clang & make disiapkan sekalian: sebagian paket Python (mis. MarkupSafe)
# perlu dicompile di HP, dan di Android 32-bit wheel siap pakai sering tidak ada.
pkg update -y
pkg install -y python git clang make pkg-config ffmpeg

if [ -d "$HOME/clipflow" ]; then
  echo ""
  echo "Folder clipflow sudah ada, update aja..."
  cd "$HOME/clipflow"
  git pull
else
  git clone https://github.com/KimTaehyung666/clipflow.git "$HOME/clipflow"
  cd "$HOME/clipflow"
fi

python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo ""
  echo "Terakhir: app butuh GROQ_API_KEY (ambil di console.groq.com -> API Keys)."
  printf "Tempel GROQ_API_KEY kamu di sini, lalu Enter: "
  read -r GROQ_KEY
  printf "GROQ_API_KEY=%s\n" "$GROQ_KEY" > .env
  echo "Key tersimpan di file .env (cuma di HP ini, tidak dikirim ke mana-mana)."
fi

echo ""
echo "=== SELESAI ==="
echo "Jalanin app-nya:"
echo "  cd ~/clipflow && bash termux-run.sh"
echo "Lalu buka http://localhost:5002 di browser HP kamu."
