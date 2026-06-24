import urllib.request
import re
import json

url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
try:
    html = urllib.request.urlopen(req).read().decode('utf-8')
    # Spotify puts state in a script tag
    # <script id="initial-state" type="text/plain">base64_encoded_json</script>
    match = re.search(r'<script id="initial-state" type="text/plain">([^<]+)</script>', html)
    if match:
        import base64
        state_json = base64.b64decode(match.group(1)).decode('utf-8')
        state = json.loads(state_json)
        print("Found state! Keys:", state.keys())
    else:
        print("No initial-state script found.")
except Exception as e:
    print("Error:", e)
