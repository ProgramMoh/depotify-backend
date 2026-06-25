from flask import Flask, request, jsonify, send_file
import subprocess
import json
import os
import urllib.request
import tarfile
import sqlite3
import uuid
import hashlib
from pathlib import Path

try:
    import librosa
    import soundfile as sf
    import mutagen
    import numpy as np
except ImportError:
    pass

# --- BOOTSTRAP NODE.JS FOR YT-DLP ---
# yt-dlp requires a JavaScript runtime (like Node.js) to decrypt YouTube's bot-protection signatures.
# Since Render's Python environment doesn't have Node, we download a portable Linux binary on startup!
NODE_DIR = os.path.join(os.getcwd(), "node_bin")
NODE_EXEC = os.path.join(NODE_DIR, "bin", "node")

if not os.path.exists(NODE_EXEC):
    print("Downloading portable Node.js for yt-dlp...")
    node_url = "https://nodejs.org/dist/v20.11.1/node-v20.11.1-linux-x64.tar.xz"
    tar_path = "node.tar.xz"
    
    urllib.request.urlretrieve(node_url, tar_path)
    os.system(f'mkdir -p "{NODE_DIR}"')
    os.system(f'tar -xf "{tar_path}" -C "{NODE_DIR}" --strip-components=1')
    os.remove(tar_path)
    print("Node.js installed successfully.")

# Add node to PATH so yt-dlp can find it automatically
os.environ["PATH"] = os.path.join(NODE_DIR, "bin") + os.pathsep + os.environ.get("PATH", "")

app = Flask(__name__)

# --- DATABASE SETUP ---
DB_PATH = "depotify.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS songs (
            id TEXT PRIMARY KEY,
            title TEXT,
            artist TEXT,
            album TEXT,
            source TEXT,
            spotify_id TEXT,
            itunes_id TEXT,
            file_path TEXT,
            artwork_hash TEXT,
            play_count INTEGER DEFAULT 0,
            last_played_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS song_analysis (
            song_id TEXT PRIMARY KEY,
            bpm REAL,
            key TEXT,
            energy REAL,
            beat_timestamps TEXT,
            outro_start REAL,
            camelot_key TEXT,
            FOREIGN KEY(song_id) REFERENCES songs(id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_camelot_key(chroma):
    chroma_vals = chroma.mean(axis=1)
    major_profile = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor_profile = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    
    best_corr = -1
    best_key = "8B"
    key_name_out = "C Major"
    key_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    camelot_major = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
    camelot_minor = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]
    
    for i in range(12):
        maj_rot = np.roll(major_profile, i)
        min_rot = np.roll(minor_profile, i)
        corr_maj = np.corrcoef(chroma_vals, maj_rot)[0, 1]
        corr_min = np.corrcoef(chroma_vals, min_rot)[0, 1]
        
        if corr_maj > best_corr:
            best_corr = corr_maj
            best_key = camelot_major[i]
            key_name_out = f"{key_names[i]} Major"
        if corr_min > best_corr:
            best_corr = corr_min
            best_key = camelot_minor[i]
            key_name_out = f"{key_names[i]} Minor"
            
    return key_name_out, best_key

def detect_outro(y, sr, rms):
    peak = float(rms.max())
    threshold = peak * 0.1
    start_idx = int(len(rms[0]) * 0.80)
    consecutive = 0
    required_consecutive = int(2.0 * sr / 512)
    for i in range(start_idx, len(rms[0])):
        if rms[0][i] < threshold:
            consecutive += 1
            if consecutive >= required_consecutive:
                return float(librosa.frames_to_time([i - required_consecutive], sr=sr)[0])
        else:
            consecutive = 0
    return float(librosa.get_duration(y=y, sr=sr))

# --- ENDPOINTS ---

@app.route('/stream', methods=['GET'])
def get_stream():
    query = request.args.get('q')
    quality = request.args.get('quality', 'high') # default to high
    
    if not query:
        return jsonify({"error": "Missing query parameter 'q'"}), 400

    print(f"Searching YouTube for: {query} (Quality: {quality})")
    
    if quality == 'low':
        format_str = "m4a[abr<=64]/worstaudio/worst"
    else:
        format_str = "m4a/bestaudio/best"
        
    command = [
        "python3", "-m", "yt_dlp",
        "--extractor-args", "youtube:player_client=android",
        f"ytsearch1:{query}",
        "-f", format_str,
        "--get-url"
    ]
    
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        stream_url = result.stdout.strip()
        
        if stream_url:
            print(f"Found stream URL.")
            return jsonify({"stream_url": stream_url})
        else:
            return jsonify({"error": "No stream found"}), 404
            
    except subprocess.CalledProcessError as e:
        print(f"yt-dlp error: {e.stderr}")
        return jsonify({"error": "Failed to extract stream"}), 500

@app.route('/songs/import', methods=['POST'])
def import_song():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
        
    file = request.files['file']
    song_id = str(uuid.uuid4())
    
    media_dir = Path("media/owned")
    audio_dir = media_dir / "audio"
    artwork_dir = media_dir / "artwork"
    audio_dir.mkdir(parents=True, exist_ok=True)
    artwork_dir.mkdir(parents=True, exist_ok=True)
    
    audio_path = audio_dir / f"{song_id}.flac"
    file.save(audio_path)
    
    try:
        tags = mutagen.File(audio_path, easy=True)
        title = tags.get("title", ["Unknown"])[0] if tags else "Unknown"
        artist = tags.get("artist", ["Unknown"])[0] if tags else "Unknown"
        album = tags.get("album", [None])[0] if tags else None
        
        artwork_hash = None
        if tags and hasattr(tags, 'pictures') and tags.pictures:
            pic_bytes = tags.pictures[0].data
            artwork_hash = hashlib.sha256(pic_bytes).hexdigest()
            art_path = artwork_dir / f"{artwork_hash}.jpg"
            if not art_path.exists():
                art_path.write_bytes(pic_bytes)
                
        y, sr = librosa.load(audio_path)
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        tempo_val = float(tempo[0]) if isinstance(tempo, (list, tuple, np.ndarray)) else float(tempo)
        beat_times = librosa.frames_to_time(beats, sr=sr).tolist()
        
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        key_name, camelot = get_camelot_key(chroma)
        
        rms = librosa.feature.rms(y=y)
        energy = float(rms.mean())
        
        outro_start = detect_outro(y, sr, rms)
        
        with get_db() as db:
            db.execute("""INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?)""",
                (song_id, title, artist, album, "owned", None, None, str(audio_path), artwork_hash))
            db.execute("""INSERT INTO song_analysis VALUES (?,?,?,?,?,?,?)""",
                (song_id, tempo_val, key_name, energy, json.dumps(beat_times), outro_start, camelot))
            db.commit()
                
        return jsonify({"id": song_id, "title": title, "artist": artist, "status": "imported"})
    except Exception as e:
        print(f"Import error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/songs/<song_id>/analysis', methods=['GET'])
def get_analysis(song_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM song_analysis WHERE song_id = ?", (song_id,)).fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({"error": "Not found"}), 404

@app.route('/songs/<song_id>', methods=['DELETE'])
def delete_song(song_id):
    with get_db() as db:
        row = db.execute("SELECT file_path, artwork_hash FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row:
            file_path = Path(row['file_path'])
            if file_path.exists():
                file_path.unlink()
            
            db.execute("DELETE FROM song_analysis WHERE song_id = ?", (song_id,))
            db.execute("DELETE FROM songs WHERE id = ?", (song_id,))
            db.commit()
            return jsonify({"status": "deleted"})
        return jsonify({"error": "Not found"}), 404

@app.route('/artwork/<hash_val>.jpg', methods=['GET'])
def get_artwork(hash_val):
    artwork_path = Path("media/owned/artwork") / f"{hash_val}.jpg"
    if artwork_path.exists():
        return send_file(artwork_path, mimetype='image/jpeg')
    return jsonify({"error": "Not found"}), 404

@app.route('/songs/<song_id>/play', methods=['POST'])
def increment_play(song_id):
    with get_db() as db:
        try:
            db.execute("UPDATE songs SET play_count = play_count + 1, last_played_at = CURRENT_TIMESTAMP WHERE id = ?", (song_id,))
            db.commit()
        except sqlite3.OperationalError:
            # Handle if the column didn't exist from earlier run
            db.execute("ALTER TABLE songs ADD COLUMN play_count INTEGER DEFAULT 0")
            db.execute("ALTER TABLE songs ADD COLUMN last_played_at TEXT")
            db.execute("UPDATE songs SET play_count = 1, last_played_at = CURRENT_TIMESTAMP WHERE id = ?", (song_id,))
            db.commit()
    return jsonify({"status": "updated"})

@app.route('/songs', methods=['GET'])
def get_songs():
    source_filter = request.args.get('source')
    with get_db() as db:
        if source_filter:
            cur = db.execute("SELECT * FROM songs WHERE source = ?", (source_filter,))
        else:
            cur = db.execute("SELECT * FROM songs")
        rows = cur.fetchall()
        
        songs = []
        for r in rows:
            songs.append({
                "id": r["id"],
                "title": r["title"],
                "artist": r["artist"],
                "album": r["album"],
                "source": r["source"],
                "spotify_id": r["spotify_id"],
                "itunes_id": r["itunes_id"]
            })
    return jsonify({"songs": songs})

if __name__ == '__main__':
    # Run on all interfaces so your iPhone can connect if on the same Wi-Fi
    # Port 5001 is used to avoid conflict with macOS AirPlay receiver on port 5000
    app.run(host='0.0.0.0', port=5001)
