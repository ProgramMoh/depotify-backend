from flask import Flask, request, jsonify
import subprocess
import json

app = Flask(__name__)

@app.route('/stream', methods=['GET'])
def get_stream():
    query = request.args.get('q')
    quality = request.args.get('quality', 'high') # default to high
    
    if not query:
        return jsonify({"error": "Missing query parameter 'q'"}), 400

    print(f"Searching YouTube for: {query} (Quality: {quality})")
    
    # Determine format based on quality
    if quality == 'low':
        # target the lowest bitrate audio to save data (e.g. ~48kbps or 64kbps opus/m4a)
        format_str = "m4a[abr<=64]/worstaudio/worst"
    else:
        # target best audio available, preferring m4a which is natively supported by iOS AVPlayer
        format_str = "m4a/bestaudio/best"
        
    command = [
        "python3", "-m", "yt_dlp",
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

if __name__ == '__main__':
    # Run on all interfaces so your iPhone can connect if on the same Wi-Fi
    # Port 5001 is used to avoid conflict with macOS AirPlay receiver on port 5000
    app.run(host='0.0.0.0', port=5001)
