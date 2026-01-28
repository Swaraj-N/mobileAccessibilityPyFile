import os
import re
import json
import base64
import tempfile
import shutil
import zipfile
from pymongo import MongoClient
from PIL import Image
import pyttsx3

# =====================================================
# CONFIG
# =====================================================
MONGO_URI = "mongodb://49.249.29.6:8081/"
DB_NAME = "fireflink_mobile_a11y"
COLLECTION = "mobile_reports"

# We'll create a temporary directory for processing
TEMP_DIR = None
OUT_DIR = "output"
ZIP_FILENAME = "accessibility_report.zip"

# =====================================================
# RULE DEFINITIONS
# =====================================================
COLOR_CONTRAST_RULE = {
    "wcag": "WCAG 2.1 – 1.4.3 Contrast (Minimum)"
}

OVERLAP_RULE = {
    "wcag": "WCAG 2.1 – 1.3.2 / 2.4.3"
}

# =====================================================
# HELPERS
# =====================================================
def parse_bounds(bounds_str):
    nums = list(map(int, re.findall(r"\d+", bounds_str or "")))
    return tuple(nums) if len(nums) == 4 else None


def decode_base64_image(data):
    if "," in data:
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def parse_screen_size(device_info):
    w, h = device_info.get("screenSize", "0x0").lower().split("x")
    return int(w), int(h)

# =====================================================
# LOAD DB
# =====================================================
def load_latest_scan():
    client = MongoClient(MONGO_URI)
    doc = client[DB_NAME][COLLECTION].find_one(
        {}, sort=[("scanInfo.timestamp", -1)]
    )
    if not doc:
        raise Exception("No scan found")
    return doc

# =====================================================
# SCREEN READING
# =====================================================
def extract_screen_reading(doc):
    timeline = []
    for e in doc.get("screenReading", {}).get("elements", []):
        spoken = (e.get("spoken") or "").strip()
        bounds = parse_bounds(e.get("bounds"))
        if spoken and bounds:
            timeline.append({
                "order": e.get("order", 0),
                "spoken": spoken,
                "bounds": bounds
            })
    return sorted(timeline, key=lambda x: x["order"])

# =====================================================
# AUDIO
# =====================================================
def generate_audio_segments(timeline, temp_dir):
    engine = pyttsx3.init()
    engine.setProperty("rate", 165)
    
    audio_files = []
    for i, step in enumerate(timeline):
        audio_filename = f"audio_{i+1:03d}.wav"
        temp_audio_path = os.path.join(temp_dir, audio_filename)
        engine.save_to_file(step["spoken"], temp_audio_path)
        audio_files.append(temp_audio_path)
        step["audio"] = audio_filename
    
    engine.runAndWait()
    return audio_files

# =====================================================
# SCREENSHOT
# =====================================================
def save_screenshot(doc, temp_dir):
    temp_screenshot_path = os.path.join(temp_dir, "screen.png")
    
    with open(temp_screenshot_path, "wb") as f:
        f.write(decode_base64_image(doc["screenshot"]["data"]))
    
    return temp_screenshot_path

# =====================================================
# COLOR CONTRAST
# =====================================================
def luminance(rgb):
    def c(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126*c(r) + 0.7152*c(g) + 0.0722*c(b)


def contrast_ratio(c1, c2):
    l1, l2 = luminance(c1), luminance(c2)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def detect_contrast_issues(timeline, image_path):
    img = Image.open(image_path).convert("RGB")
    issues = []

    for step in timeline:
        x1, y1, x2, y2 = step["bounds"]
        cx, cy = (x1+x2)//2, (y1+y2)//2
        try:
            fg = img.getpixel((cx, cy))
            bg = img.getpixel((max(cx-20, 0), max(cy-20, 0)))
        except:
            continue

        ratio = contrast_ratio(fg, bg)
        if ratio < 4.5:
            issues.append({
                "type": "Color Contrast",
                "severity": "FAIL" if ratio < 3 else "WARN",
                "spoken": step["spoken"],
                "bounds": step["bounds"],
                "ratio": round(ratio, 2),
                "wcag": COLOR_CONTRAST_RULE["wcag"]
            })
    
    return issues

# =====================================================
# OVERLAP
# =====================================================
def detect_overlaps(timeline):
    issues = []

    def intersect(a, b):
        return (
            min(a[2], b[2]) - max(a[0], b[0]) > 0 and
            min(a[3], b[3]) - max(a[1], b[1]) > 0
        )

    for i in range(len(timeline)):
        for j in range(i+1, len(timeline)):
            if intersect(timeline[i]["bounds"], timeline[j]["bounds"]):
                issues.append({
                    "type": "Overlapping Elements",
                    "severity": "WARN",
                    "spoken": f"{timeline[i]['spoken']} ↔ {timeline[j]['spoken']}",
                    "bounds": timeline[i]["bounds"],
                    "wcag": OVERLAP_RULE["wcag"]
                })
    
    return issues

# =====================================================
# SAVE READING ORDER
# =====================================================
def save_reading_order(timeline, temp_dir):
    temp_json_path = os.path.join(temp_dir, "reading_order.json")
    
    with open(temp_json_path, "w", encoding="utf-8") as f:
        json.dump(timeline, f, indent=2)
    
    return temp_json_path

# =====================================================
# HTML REPORT (FINAL FIXED UI)
# =====================================================
def generate_html(timeline, issues, device_info, w, h, temp_dir):

    def build_items(issue_type):
        html = ""
        for i in issues:
            if i["type"] == issue_type:
                extra = f"Contrast Ratio: <b>{i['ratio']}</b><br>" if "ratio" in i else ""
                html += f"""
                <div class="issue-item {i['severity']}">
                    <div class="issue-head">
                        <span class="badge {i['severity']}">{i['severity']}</span>
                        <span class="issue-type">{i['type']}</span>
                    </div>
                    <div class="issue-text">{i['spoken']}</div>
                    <div class="issue-meta">
                        {extra}{i['wcag']}
                    </div>
                </div>
                """
        return html or "<div class='empty'>No issues found 🎉</div>"

    html = f"""
<!DOCTYPE html>
<html>
<head>
<title>Fireflink Mobile Accessibility Report</title>

<style>
body {{
  margin:0;
  font-family:Segoe UI, Arial;
  background:#f4f6f8;
}}

header {{
  background:#111827;
  color:white;
  padding:16px 24px;
}}

.container {{
  display:grid;
  grid-template-columns:380px 1fr;
  gap:24px;
  padding:24px;
}}

.card {{
  background:white;
  border-radius:12px;
  padding:16px;
  box-shadow:0 8px 20px rgba(0,0,0,.08);
}}

button {{
  background:#2563eb;
  color:white;
  border:none;
  padding:10px 14px;
  border-radius:8px;
  cursor:pointer;
}}

.accordion {{
  margin-top:12px;
  border-radius:10px;
  overflow:hidden;
}}

.accordion-header {{
  background:#e5e7eb;
  padding:12px;
  font-weight:600;
  cursor:pointer;
  display:flex;
  justify-content:space-between;
}}

.accordion-content {{
  display:none;
  padding:12px;
  background:#fafafa;
}}

.issue-item {{
  background:#ffffff;
  border-left:5px solid;
  padding:12px;
  margin-bottom:10px;
  border-radius:8px;
}}

.issue-item.FAIL {{ border-color:#dc2626; }}
.issue-item.WARN {{ border-color:#f59e0b; }}

.issue-head {{
  display:flex;
  align-items:center;
  gap:8px;
}}

.badge {{
  padding:4px 10px;
  border-radius:999px;
  font-size:12px;
  font-weight:bold;
  color:white;
}}

.badge.FAIL {{ background:#dc2626; }}
.badge.WARN {{ background:#f59e0b; }}

.issue-type {{
  font-weight:600;
  font-size:13px;
}}

.issue-text {{
  margin-top:6px;
  font-size:14px;
}}

.issue-meta {{
  margin-top:4px;
  font-size:12px;
  opacity:.7;
}}

.empty {{
  opacity:.6;
  padding:8px;
}}

#phone {{
  width:{w}px;
  height:{h}px;
  border-radius:32px;
  border:6px solid #111;
  overflow:hidden;
  transform:scale(0.45);
  transform-origin:top center;
  position:relative;
}}

#overlay {{
  position:absolute;
  top:0;
  left:0;
}}

#spoken {{
  margin-top:12px;
  font-weight:bold;
  color:#7c2d12;
}}
</style>
</head>

<body>

<header>
<h2>Fireflink Mobile Accessibility Report</h2>
<p>{device_info.get("deviceBrand")} {device_info.get("deviceName")} • {w}×{h}</p>
</header>

<div class="container">

<div>
  <div class="card">
    <button onclick="startReplay()">▶ Play TalkBack</button>
    <a href="reading_order.json" download>
      <button style="background:#374151;margin-left:8px;">⬇ Reading Order</button>
    </a>
    <div id="spoken"></div>
  </div>

  <div class="card" style="margin-top:16px;">
    <h3>⚠ Accessibility Issues</h3>

    <div class="accordion">
      <div class="accordion-header" onclick="toggle(this)">
        🎨 Color Contrast Issues
        <span>{sum(1 for i in issues if i["type"]=="Color Contrast")}</span>
      </div>
      <div class="accordion-content">
        {build_items("Color Contrast")}
      </div>
    </div>

    <div class="accordion">
      <div class="accordion-header" onclick="toggle(this)">
        🔀 Overlapping Elements
        <span>{sum(1 for i in issues if i["type"]=="Overlapping Elements")}</span>
      </div>
      <div class="accordion-content">
        {build_items("Overlapping Elements")}
      </div>
    </div>

  </div>
</div>

<div class="card" style="display:flex;justify-content:center;">
  <div id="phone">
    <img src="screen.png" width="{w}" height="{h}">
    <canvas id="overlay" width="{w}" height="{h}"></canvas>
  </div>
</div>

</div>

<script>
const steps = {json.dumps(timeline)};
const issues = {json.dumps(issues)};
const issueMap = {{}};

// allow multiple issues per spoken text
issues.forEach(i => {{
  issueMap[i.spoken] = issueMap[i.spoken] || [];
  issueMap[i.spoken].push(i);
}});

const canvas = document.getElementById("overlay");
const ctx = canvas.getContext("2d");
const spoken = document.getElementById("spoken");
let index = 0;
let audio = new Audio();

function toggle(el) {{
  const c = el.nextElementSibling;
  c.style.display = c.style.display === "block" ? "none" : "block";
}}

function highlight(step) {{
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const [x1,y1,x2,y2] = step.bounds;

  let color = "green";
  if (issueMap[step.spoken]) {{
    color = issueMap[step.spoken][0].severity === "FAIL" ? "red" : "orange";
  }}

  ctx.strokeStyle = color;
  ctx.lineWidth = 6;
  ctx.strokeRect(x1,y1,x2-x1,y2-y1);
  spoken.innerText = step.spoken;
}}

function playNext() {{
  if (index >= steps.length) return;
  highlight(steps[index]);
  audio.src = steps[index].audio;
  audio.play();
  audio.onended = () => {{ index++; playNext(); }};
}}

function startReplay() {{
  index = 0;
  playNext();
}}
</script>

</body>
</html>
"""

    temp_html_path = os.path.join(temp_dir, "report.html")
    with open(temp_html_path, "w", encoding="utf-8") as f:
        f.write(html)
    
    return temp_html_path

# =====================================================
# CREATE ZIP ARCHIVE
# =====================================================
def create_zip_archive(temp_dir, output_dir, zip_filename):
    zip_path = os.path.join(output_dir, zip_filename)
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add all files from temp directory to zip
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                file_path = os.path.join(root, file)
                # Add file to zip with relative path
                arcname = os.path.relpath(file_path, temp_dir)
                zipf.write(file_path, arcname)
    
    return zip_path

# =====================================================
# MAIN
# =====================================================
def run():
    # Create temporary directory
    global TEMP_DIR
    TEMP_DIR = tempfile.mkdtemp(prefix="fireflink_a11y_")
    
    # Load data
    doc = load_latest_scan()
    device_info = doc.get("deviceInfo", {})
    w, h = parse_screen_size(device_info)
    
    # Process screenshot
    temp_screenshot_path = save_screenshot(doc, TEMP_DIR)
    
    # Extract timeline
    timeline = extract_screen_reading(doc)
    
    # Generate audio files
    generate_audio_segments(timeline, TEMP_DIR)
    
    # Save reading order
    temp_json_path = save_reading_order(timeline, TEMP_DIR)
    
    # Detect issues
    contrast_issues = detect_contrast_issues(timeline, temp_screenshot_path)
    overlap_issues = detect_overlaps(timeline)
    issues = contrast_issues + overlap_issues
    
    # Generate final report
    temp_html_path = generate_html(timeline, issues, device_info, w, h, TEMP_DIR)
    
    # Create output directory if it doesn't exist
    os.makedirs(OUT_DIR, exist_ok=True)
    
    # Create zip archive
    zip_path = create_zip_archive(TEMP_DIR, OUT_DIR, ZIP_FILENAME)
    
    # Print only the zip file path
    print(os.path.abspath(zip_path))
    
    # Clean up temporary directory
    shutil.rmtree(TEMP_DIR)

if __name__ == "__main__":
    run()