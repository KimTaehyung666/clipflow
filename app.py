import os, json, re
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import requests
from urllib.parse import urlparse, parse_qs

load_dotenv()

app = Flask(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Fallback transcript pihak ketiga. YouTube memblokir transcript dari IP cloud
# (Vercel/Render/dll), jadi di deploy server, transcript diambil lewat API ini.
TRANSCRIPT_API_KEY = os.getenv("TRANSCRIPT_API_KEY")
SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY")
# Model lama "llama-3.3-70b-versatile" sudah dimatikan Groq per 16 Agu 2026.
# Pengganti resmi dari Groq: openai/gpt-oss-120b. Bisa dioverride via env GROQ_MODEL.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

def call_groq(prompt, max_tokens=2500):
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY belum diset. Isi file .env (lokal) atau Environment Variable (deploy) dulu ya.")
    try:
        response = requests.post(
            GROQ_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=60
        )
    except requests.Timeout:
        raise
    except requests.RequestException as e:
        raise Exception(f"Gagal menghubungi Groq: {e}")

    try:
        result = response.json()
    except ValueError:
        raise Exception(f"Groq membalas format aneh (HTTP {response.status_code}), coba lagi.")

    if response.status_code == 401:
        raise Exception("API key Groq tidak valid / sudah mati. Cek GROQ_API_KEY kamu.")
    if response.status_code == 429:
        raise Exception("Kena rate limit Groq, tunggu sebentar lalu coba lagi.")
    if "error" in result:
        raise Exception(result["error"].get("message", "Groq error"))
    if response.status_code >= 400:
        raise Exception(f"Groq error HTTP {response.status_code}")
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise Exception("Response Groq tidak lengkap, coba lagi.")

def extract_video_id(url):
    parsed = urlparse(url)
    if parsed.hostname in ('youtu.be',):
        return parsed.path.strip('/').split('/')[0] or None
    if parsed.hostname in ('www.youtube.com', 'youtube.com', 'm.youtube.com', 'music.youtube.com'):
        if parsed.path == '/watch':
            return parse_qs(parsed.query).get('v', [None])[0]
        for prefix in ('/shorts/', '/live/', '/embed/'):
            if parsed.path.startswith(prefix):
                return parsed.path.split(prefix)[1].split('/')[0] or None
    return None

def _segment_to_dict(s):
    if isinstance(s, dict):
        return {"start": s.get('start', 0), "text": s.get('text', '')}
    return {"start": getattr(s, 'start', 0), "text": getattr(s, 'text', '')}

def fetch_transcript_with_timestamps(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    raw = None
    last_error = None

    # Cara utama (API versi >= 1.0): fetch langsung, utamakan Indonesia lalu Inggris
    try:
        raw = api.fetch(video_id, languages=['id', 'en'])
    except Exception as e:
        last_error = e

    # Fallback: ambil dari daftar transcript yang tersedia
    if raw is None:
        try:
            tlist = api.list(video_id)
            transcript = None
            for lang in (['id'], ['en']):
                try:
                    transcript = tlist.find_transcript(lang)
                    break
                except Exception:
                    continue
            if transcript is None:
                # Terakhir: terima transcript bahasa apa pun yang pertama ketemu
                for t in tlist:
                    transcript = t
                    break
            if transcript is not None:
                raw = transcript.fetch()
        except Exception as e:
            last_error = e

    if raw is None:
        raise Exception(_short_transcript_error(last_error))

    return [_segment_to_dict(s) for s in raw]

def _short_transcript_error(e):
    """Ubah exception mentah youtube-transcript-api (yang teksnya bisa satu
    halaman penuh) jadi pesan pendek yang enak dibaca user."""
    if e is None:
        return "Transcript tidak tersedia."
    name = type(e).__name__
    mapping = {
        "IpBlocked": "YouTube memblokir IP server",
        "RequestBlocked": "YouTube memblokir IP server",
        "TranscriptsDisabled": "transcript dimatikan pemilik video",
        "NoTranscriptFound": "video ini tidak punya transcript",
        "NoTranscriptAvailable": "video ini tidak punya transcript",
        "VideoUnavailable": "video tidak tersedia / private",
        "VideoUnplayable": "video tidak bisa diputar",
    }
    if name in mapping:
        return mapping[name]
    return f"{name}: {str(e)[:120]}"

def _clean_segments(raw_items, start_key="start", scale=1.0):
    """Normalisasi daftar segmen dari API pihak ketiga ke format internal."""
    segments = []
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        try:
            start = float(item.get(start_key, 0)) / scale
        except (TypeError, ValueError):
            continue
        if text:
            segments.append({"start": start, "text": text})
    segments.sort(key=lambda s: s["start"])
    return segments

def fetch_transcript_transcriptapi(video_id):
    """Fallback 1: TranscriptAPI (transcriptapi.com). Balik None kalau key belum diset."""
    if not TRANSCRIPT_API_KEY:
        return None
    last_error = "tidak ada respons"
    for lang in ("id,en", "asr-id,asr-en"):
        try:
            r = requests.get(
                "https://transcriptapi.com/api/v2/youtube/transcript",
                params={"video_url": video_id, "language": lang},
                headers={"Authorization": f"Bearer {TRANSCRIPT_API_KEY}"},
                timeout=30,
            )
            if r.status_code == 200:
                segments = _clean_segments(r.json().get("transcript"))
                if segments:
                    return segments
            last_error = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_error = str(e)[:100]
    raise Exception(f"TranscriptAPI gagal ({last_error})")

def fetch_transcript_supadata(video_id):
    """Fallback 2: Supadata (supadata.ai). offset/duration mereka milidetik.
    Balik None kalau key belum diset."""
    if not SUPADATA_API_KEY:
        return None
    last_error = "tidak ada respons"
    for lang in ("id", "en"):
        try:
            r = requests.get(
                "https://api.supadata.ai/v1/transcript",
                params={
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "mode": "native",
                    "lang": lang,
                    "text": "false",
                },
                headers={"x-api-key": SUPADATA_API_KEY},
                timeout=30,
            )
            if r.status_code == 200:
                content = r.json().get("content")
                if isinstance(content, list):
                    segments = _clean_segments(content, start_key="offset", scale=1000.0)
                    if segments:
                        return segments
            last_error = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_error = str(e)[:100]
    raise Exception(f"Supadata gagal ({last_error})")

_TS_PATTERN = r'(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)'

def _ts_to_seconds(h, m, s):
    return int(h or 0) * 3600 + int(m) * 60 + int(s)

def parse_manual_transcript(text):
    """Parse transcript yang ditempel user. Mendukung:
    - WebVTT / SRT (ada panah -->)
    - Format salinan panel transkrip YouTube: baris "0:00 teks" atau
      timestamp dan teks bergantian baris."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []

    segments = []
    if "-->" in text:
        cue_re = re.compile(_TS_PATTERN + r'[.,]\d{1,3}\s*-->')
        matches = list(cue_re.finditer(text))
        for i, mt in enumerate(matches):
            start = _ts_to_seconds(mt.group(1), mt.group(2), mt.group(3))
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[mt.end():end_pos]
            # Sisa baris pertama setelah "-->" adalah timestamp selesai; buang.
            if "\n" in block:
                block = block.split("\n", 1)[1]
            lines = []
            for ln in block.splitlines():
                ln = re.sub(r'<[^>]+>', '', ln).strip()
                if ln and not ln.isdigit():
                    lines.append(ln)
            if lines:
                segments.append({"start": start, "text": " ".join(lines)})
        segments.sort(key=lambda s: s["start"])
        return segments

    line_re = re.compile(r'^[\[]?' + _TS_PATTERN + r'[\]]?\s*(.*)$')
    pending = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = line_re.match(line)
        if m:
            start = _ts_to_seconds(m.group(1), m.group(2), m.group(3))
            rest = m.group(4).strip()
            if rest:
                segments.append({"start": start, "text": rest})
                pending = None
            else:
                pending = start
        elif pending is not None:
            segments.append({"start": pending, "text": line})
            pending = None
        elif segments:
            segments[-1]["text"] += " " + line
    segments.sort(key=lambda s: s["start"])
    return segments

def get_transcript_segments(video_id, manual_text=""):
    """Sumber transcript berurutan: tempelan manual user -> library langsung
    (gratis, jalan dari IP rumahan) -> TranscriptAPI -> Supadata.
    Balik (segments, sumber)."""
    if manual_text:
        segments = parse_manual_transcript(manual_text)
        if len(segments) >= 5:
            return segments, "manual"

    try:
        return fetch_transcript_with_timestamps(video_id), "youtube"
    except Exception as e:
        direct_error = str(e)

    def _brief(msg):
        return " ".join(str(msg).split())[:120]

    errors = [_brief(direct_error)]
    for fn in (fetch_transcript_transcriptapi, fetch_transcript_supadata):
        try:
            segments = fn(video_id)
            if segments:
                return segments, fn.__name__.replace("fetch_transcript_", "")
        except Exception as e:
            errors.append(_brief(e))

    detail = "; ".join(errors)[:300]
    raise Exception(
        "Gagal ambil transcript otomatis. YouTube memblokir IP server (Vercel), "
        "dan API fallback belum aktif/gagal. Solusi: tempel transcript manual "
        "(kolom di bawah link video), atau isi TRANSCRIPT_API_KEY / SUPADATA_API_KEY "
        f"di Environment Variables. Detail: {detail}"
    )

def format_time(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def segments_to_windows(segments, window_sec=60, step_sec=30):
    if not segments:
        return []
    max_time = segments[-1]['start'] + 10
    windows = []
    t = 0
    while t < max_time:
        texts = [s['text'] for s in segments if t <= s['start'] < t + window_sec]
        if texts:
            windows.append({"start": t, "end": min(t + window_sec, max_time), "text": ' '.join(texts)})
        t += step_sec
    return windows

def parse_clips(raw_text):
    """Parse output model jadi list clip yang bersih. Model diminta membalas
    JSON object {"clips": [...]}, tapi tetap disiapkan fallback ekstraksi array."""
    data = None
    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError):
        match = re.search(r'\[.*\]', raw_text or '', re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except ValueError:
                data = None
    if isinstance(data, dict):
        data = data.get("clips")
    if not isinstance(data, list):
        return None

    clips = []
    for c in data:
        if not isinstance(c, dict):
            continue
        start = str(c.get("start", "")).strip()
        end = str(c.get("end", "")).strip()
        hook = str(c.get("hook", "")).strip()
        if not start or not end or not hook:
            continue
        try:
            score = int(round(float(c.get("score", 5))))
        except (ValueError, TypeError):
            score = 5
        clips.append({
            "start": start,
            "end": end,
            "hook": hook,
            "hashtags": str(c.get("hashtags", "")).strip(),
            "reason": str(c.get("reason", "")).strip(),
            "score": max(1, min(10, score)),
        })
    return clips or None

HOOK_STYLE_GUIDE = {
    "VTuber / Vstreamer": """Gaya hook VTuber/streamer: casual, ekspresif, boleh pake bahasa gaul berat. Fokus ke reaksi dan ekspresi streamer.
Contoh: "dia nangis beneran pas momen ini... gue ga nyangka 😭", "chat langsung rame parah pas ini kejadian HAHAHA" """,

    "Gaming": """Gaya hook gaming: hype, dramatis, fokus ke momen gameplay atau reaksi. Boleh pake istilah gaming.
Contoh: "ini clutch paling gila yang pernah gue liat seumur hidup", "dia solo semua musuh sendirian... caranya gila banget" """,

    "IRL / Just Chatting": """Gaya hook IRL: relatable, conversational, kayak lagi cerita ke temen.
Contoh: "tolong dia ngomong apa barusan 💀", "moment paling awkward di stream dan dia malah ketawa" """,

    "Olahraga / Highlight": """Gaya hook olahraga: dramatis tapi natural, fokus ke momen krusial, bisa pake nama pemain.
Contoh: "gue rewind ini 10 kali dan masih ga percaya itu bisa masuk", "detik-detik yang bikin semua orang di stadion diem seketika" """,

    "Podcast": """Gaya hook podcast: bikin penasaran, fokus ke statement mengejutkan atau insight yang jarang dibahas.
Contoh: "dia baru aja ngungkapin sesuatu yang belum pernah diomongin ke siapapun", "pendapat dia soal ini bikin gue mikir ulang segalanya" """,

    "Edukasi": """Gaya hook edukasi: bikin penasaran, frame kayak 'fakta yang bikin kaget' atau 'hal yang kamu ga tau'.
Contoh: "ternyata yang selama ini kita percaya itu salah total", "cara kerja ini bikin gue sadar betapa bodohnya gue selama ini 😭" """,

    "Musik / Concert": """Gaya hook musik: emosional, vivid, fokus ke momen yang bikin bulu kuduk berdiri.
Contoh: "bagian ini setiap kali gue denger selalu bikin merinding", "dia improvisasi di sini dan hasilnya luar biasa banget" """,

    "Vlog": """Gaya hook vlog: personal, storytelling, bikin penonton ngerasa ikut ngalamin.
Contoh: "gue ga nyangka hari itu bakal berakhir kayak gini", "ini momen yang bikin gue sadar harus berubah" """,

    "Berita / Current Events": """Gaya hook berita: to the point tapi tetap bikin penasaran, hindari judul koran.
Contoh: "tunggu sampai lo denger apa yang dia bilang di bagian ini", "reaksi dia setelah denger kabar ini bikin semua orang kaget" """,

    "Review / Tech": """Gaya hook review/tech: fokus ke temuan mengejutkan atau perbandingan yang bikin penasaran.
Contoh: "gue ga nyangka hasilnya bakal separah ini pas ditest", "ini alasan kenapa gue ga bakal rekomendasiin produk ini ke siapapun" """,

    "Variety / Campuran": """Gaya hook variety: fleksibel, fokus ke momen paling memorable atau mengejutkan.
Contoh: "ini momen yang bikin semua orang langsung diem", "ga ada yang nyangka ini bakal kejadian hari ini" """
}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    platform = data.get("platform", "YouTube Shorts")
    content_type = data.get("contentType", "Variety / Campuran")
    try:
        max_clips = int(data.get("maxClips", 5))
    except (ValueError, TypeError):
        max_clips = 5
    max_clips = max(1, min(10, max_clips))

    if not url:
        return jsonify({"error": "URL tidak boleh kosong!"}), 400
    video_id = extract_video_id(url)
    if not video_id:
        return jsonify({"error": "URL bukan video YouTube yang valid!"}), 400

    manual_text = str(data.get("transcript", ""))[:200_000]

    try:
        segments, transcript_source = get_transcript_segments(video_id, manual_text)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if len(segments) < 5:
        return jsonify({"error": "Transcript terlalu pendek untuk dianalisis!"}), 400

    windows = segments_to_windows(segments, window_sec=60, step_sec=30)
    if len(windows) > 40:
        step = len(windows) // 40
        windows = windows[::step][:40]

    windows_text = ""
    for w in windows:
        windows_text += f"[{format_time(w['start'])} - {format_time(w['end'])}] {w['text'][:300]}\n\n"

    hook_guide = HOOK_STYLE_GUIDE.get(content_type, HOOK_STYLE_GUIDE["Variety / Campuran"])

    prompt = f"""Lo adalah clipper berpengalaman yang spesialis konten {content_type} dan udah tau persis cara bikin hook yang bikin orang stop scrolling di {platform}.

{hook_guide}

Platform target: {platform}
Jenis konten: {content_type}

Transcript video dengan timestamp:
---
{windows_text}
---

Tugas lo: pilih {max_clips} momen terbaik yang paling layak dijadiin clip.

Untuk tiap rekomendasi berikan:
- "hook": judul/caption NATURAL sesuai gaya {content_type} di atas. JANGAN kaku kayak judul berita. Panjang 60-120 karakter.
- "hashtags": 8-12 hashtag relevan untuk {platform}, mix antara hashtag besar dan spesifik. Format: "#tag1 #tag2 #tag3"
- "start": timestamp mulai (M:SS atau H:MM:SS)
- "end": timestamp selesai
- "reason": 1-2 kalimat kenapa momen ini bagus, tulis santai
- "score": potensi viral 1-10

Balas HANYA dengan JSON object persis seperti ini, tanpa teks lain:
{{"clips": [{{"start":"0:00","end":"0:45","hook":"...","hashtags":"#tag1 #tag2","reason":"...","score":8}}]}}"""

    try:
        raw = call_groq(prompt, max_tokens=2500)
        clips = parse_clips(raw)
        if not clips:
            return jsonify({"error": "Format response tidak valid, coba lagi!"}), 500
        return jsonify({"clips": clips, "video_id": video_id, "total_segments": len(segments), "transcript_source": transcript_source})
    except requests.Timeout:
        return jsonify({"error": "Timeout, coba lagi!"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)), debug=os.environ.get("DEBUG") == "1")
