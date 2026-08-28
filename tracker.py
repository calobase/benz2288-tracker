#!/usr/bin/env python3
"""
benz2288 iPhone 17 現金價追蹤器
────────────────────────────────
用法：
  python tracker.py              正常執行（OCR + 存檔 + 發信 + 生成 index.html）
  python tracker.py --calibrate  校準模式：輸出 calibrate.jpg 確認 OCR 框位置
  python tracker.py --push       執行後自動 git push（需先建好 GitHub repo）
  python tracker.py --dry-run    只印出今日 GIF URL，不寫入任何資料
"""

import os, sys, re, io, json, smtplib, subprocess
from datetime import date, datetime

# 強制 stdout/stderr 使用 UTF-8，避免 Windows cp950 無法顯示 emoji
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from html.parser import HTMLParser

import requests
from PIL import Image, ImageEnhance, ImageDraw

try:
    import pytesseract
except ImportError:
    print("❌ 缺少套件：請先執行  pip install -r requirements.txt")
    sys.exit(1)

# ══════════════════════════════════════════════════════
#  ⚙️  設定（按需修改）
# ══════════════════════════════════════════════════════

GMAIL_USER         = "calobase@gmail.com"
NOTIFY_TO          = "calobase@gmail.com"

# 載入 .env 檔（優先用環境變數，其次讀檔）
def _load_env() -> dict:
    env_file = Path(__file__).parent / ".env"
    result = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
    return result

_ENV = _load_env()

def _load_app_password() -> str:
    return os.environ.get("GMAIL_APP_PASSWORD") or _ENV.get("GMAIL_APP_PASSWORD", "")

GMAIL_APP_PASSWORD = _load_app_password()

# Tesseract 安裝路徑（Windows 用絕對路徑，Linux 用 PATH）
import platform as _platform
TESSERACT_CMD = (r"C:\Program Files\Tesseract-OCR\tesseract.exe"
                 if _platform.system() == "Windows" else "tesseract")

BASE_DIR       = Path(__file__).parent
PRICES_JSON    = BASE_DIR / "prices.json"
DASHBOARD_HTML = BASE_DIR / "index.html"

# ══════════════════════════════════════════════════════
#  📐 機型定義：名稱 + 價格區間 + 上次成功的座標（快取）
#  座標會在辨識失敗時自動重新掃描並更新至 coords.json
# ══════════════════════════════════════════════════════

MODELS_DEF = [
    {
        "name":  "iPhone 17 256G 黑/藍/白/紫/綠",
        "range": (18000, 34000),
        "default_coords": (873, 1107, 966, 1143),
    },
    {
        "name":  "iPhone 17 Pro 256G",
        "range": (32000, 46000),
        "default_coords": (873, 1320, 966, 1356),
    },
    {
        "name":  "iPhone 17 Pro Max 256G 銀/橘",
        "range": (38000, 60000),
        "default_coords": (873, 1517, 966, 1552),
    },
]

COORDS_JSON = BASE_DIR / "coords.json"

def load_coords() -> dict:
    if COORDS_JSON.exists():
        raw = json.loads(COORDS_JSON.read_text(encoding="utf-8"))
        return {k: tuple(v) for k, v in raw.items()}
    return {m["name"]: m["default_coords"] for m in MODELS_DEF}

def save_coords(coords: dict):
    COORDS_JSON.write_text(
        json.dumps({k: list(v) for k, v in coords.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

SCAN_RADIUS = 150  # 只掃描已知座標 ±150px，避免抓到遠處的干擾行

def auto_scan_coords(img: Image.Image, current_coords: dict | None = None) -> dict:
    """針對每個機型，只在已知 y ±150px 範圍內掃描，自動修正小幅漂移。
    回傳 {model_name: (coords_tuple, price)} — price 直接來自掃描，避免重複 OCR 的不一致。
    """
    print("🔍 自動掃描座標中...")
    results = {}  # name → (coords, price)

    for m in MODELS_DEF:
        lo, hi = m["range"]
        name = m["name"]

        # 決定掃描範圍
        if current_coords and name in current_coords:
            known_y = (current_coords[name][1] + current_coords[name][3]) // 2
        else:
            known_y = (m["default_coords"][1] + m["default_coords"][3]) // 2
        y_start = max(500, known_y - SCAN_RADIUS)
        y_end   = min(img.size[1] - 36, known_y + SCAN_RADIUS)

        # 掃描
        hits = []
        for y1 in range(y_start, y_end, 4):
            price = ocr_price(img, (873, y1, 966, y1 + 36))
            if price and lo <= price <= hi:
                hits.append((y1 + 18, price))

        if not hits:
            print(f"   ❌ {name} → ±{SCAN_RADIUS}px 範圍內未找到符合的行")
            continue

        # 合併鄰近命中
        clusters = []
        for y, price in hits:
            if clusters and y - clusters[-1][0] < 25:
                clusters[-1] = ((y + clusters[-1][0]) // 2, price)
            else:
                clusters.append((y, price))

        # 選最靠近已知位置的 cluster，直接保留掃描到的價格
        best_y, best_p = min(clusters, key=lambda c: abs(c[0] - known_y))
        coord = (873, best_y - 18, 966, best_y + 18)
        results[name] = (coord, best_p)
        print(f"   ✅ {name} → y={best_y-18}~{best_y+18}  ${best_p:,}")

    return results

CHART_COLORS = ["#f87171", "#60a5fa", "#fbbf24", "#34d399", "#a78bfa"]

# ══════════════════════════════════════════════════════
#  🌐 GIF 下載（從網站首頁解析真實連結）
# ══════════════════════════════════════════════════════

SITE_ROOT = "https://www.benz2288.com.tw"
HEADERS   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


class _GifFinder(HTMLParser):
    """從 HTML 中找 href/src 含 .gif 的連結"""
    def __init__(self):
        super().__init__()
        self.gif_urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ("href", "src"):
            val = attrs.get(key, "")
            if val and val.lower().endswith(".gif"):
                self.gif_urls.append(val)


def find_latest_gif():
    """
    從首頁 HTML 解析報價 GIF 連結，失敗時自動重試 3 次（間隔 30 秒）。
    喚醒後網路尚未就緒時仍能成功。
    """
    import time
    last_err = None
    for attempt in range(1, 6):
        try:
            r = requests.get(SITE_ROOT, headers=HEADERS, timeout=15)
            r.raise_for_status()
            parser = _GifFinder()
            parser.feed(r.text)
            if parser.gif_urls:
                href = parser.gif_urls[0]
                url  = href if href.startswith("http") else SITE_ROOT + "/" + href.lstrip("/")
                print(f"✅ 從首頁找到報價表：{url}")
                return url, date.today()
        except Exception as e:
            last_err = e
            print(f"⚠️  首頁解析失敗（第 {attempt} 次）：{e}")
            if attempt < 5:
                print(f"   60 秒後重試...")
                time.sleep(60)

    return None, None


def download_image(url: str) -> Image.Image:
    import time
    last_err = None
    for attempt in range(1, 6):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            last_err = e
            print(f"⚠️  GIF 下載失敗（第 {attempt} 次）：{e}")
            if attempt < 5:
                print("   60 秒後重試...")
                time.sleep(60)
    raise RuntimeError(f"GIF 下載重試 5 次仍失敗：{last_err}")

# ══════════════════════════════════════════════════════
#  🔍 OCR
# ══════════════════════════════════════════════════════

def ocr_price(img: Image.Image, box: tuple, scale: int = 4) -> int | None:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    try:
        cropped = img.crop(box)
        w, h = cropped.size
        if w < 3 or h < 3:
            return None
        cropped = cropped.resize((w * scale, h * scale), Image.LANCZOS)
        cropped = cropped.convert("L")
        cropped = ImageEnhance.Contrast(cropped).enhance(2.5)
        text = pytesseract.image_to_string(
            cropped,
            config="--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789,"
        ).strip()
        m = re.search(r"(\d[\d,]{3,})", text)
        return int(m.group(1).replace(",", "")) if m else None
    except Exception:
        return None


def calibrate(img: Image.Image, coords: dict):
    """在原圖上標示所有 OCR 框，儲存為 calibrate.jpg 供人工比對"""
    debug = img.copy()
    draw = ImageDraw.Draw(debug)
    for name, box in coords.items():
        draw.rectangle(box, outline="red", width=4)
        price = ocr_price(img, box)
        label = f"{name[:20]} → {'$'+f'{price:,}' if price else 'FAIL'}"
        draw.text((box[0], max(0, box[1] - 20)), label, fill=(255, 255, 0))
    out = BASE_DIR / "calibrate.jpg"
    debug.save(out)
    print(f"📸 已儲存 {out}")

# ══════════════════════════════════════════════════════
#  💾 資料儲存（prices.json）
#  結構：{ "2026-04-28": { "機型名": 25800, ... }, ... }
# ══════════════════════════════════════════════════════

def load_prices() -> dict:
    if PRICES_JSON.exists():
        return json.loads(PRICES_JSON.read_text(encoding="utf-8"))
    return {}


def save_prices(data: dict):
    PRICES_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

# ══════════════════════════════════════════════════════
#  📧 Gmail 通知
# ══════════════════════════════════════════════════════

def send_email(changes: dict, date_str: str):
    if not GMAIL_APP_PASSWORD:
        print("⚠️  GMAIL_APP_PASSWORD 未設定，略過發信")
        print("   請在 PowerShell 執行：$env:GMAIL_APP_PASSWORD = '你的 App Password'")
        return

    rows = ""
    for model, (old, new) in changes.items():
        diff = new - old
        color = "#16a34a" if diff < 0 else "#dc2626"
        arrow = "🔻 降價" if diff < 0 else "🔺 漲價"
        rows += f"""
        <tr>
          <td style="padding:10px;border:1px solid #e5e7eb">{model}</td>
          <td style="padding:10px;border:1px solid #e5e7eb;text-align:right">${old:,}</td>
          <td style="padding:10px;border:1px solid #e5e7eb;text-align:right;
              color:{color};font-weight:700">${new:,}</td>
          <td style="padding:10px;border:1px solid #e5e7eb;text-align:right;
              color:{color}">{arrow} {abs(diff):,}</td>
        </tr>"""

    body = f"""
    <html><body style="font-family:-apple-system,sans-serif;max-width:600px;margin:auto;padding:20px">
      <h2 style="color:#111827">📱 benz2288 iPhone 17 價格異動（{date_str}）</h2>
      <p style="color:#6b7280;font-size:14px">以下機型<strong>現金價</strong>出現變動：</p>
      <table style="border-collapse:collapse;width:100%;font-size:14px;margin-top:12px">
        <thead><tr style="background:#f9fafb">
          <th style="padding:10px;border:1px solid #e5e7eb;text-align:left">機型</th>
          <th style="padding:10px;border:1px solid #e5e7eb;text-align:right">舊價</th>
          <th style="padding:10px;border:1px solid #e5e7eb;text-align:right">新價</th>
          <th style="padding:10px;border:1px solid #e5e7eb;text-align:right">變動</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#9ca3af;font-size:12px;margin-top:20px">
        來源：benz2288.com.tw · 抓取時間：{datetime.now():%Y-%m-%d %H:%M}
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ iPhone 17 價格異動 - benz2288 ({date_str})"
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_TO
    msg.attach(MIMEText(body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)
    print(f"📧 通知信已寄出 → {NOTIFY_TO}")

def send_error_email(subject: str, detail: str):
    if not GMAIL_APP_PASSWORD:
        return
    body = f"""
    <html><body style="font-family:-apple-system,sans-serif;max-width:600px;margin:auto;padding:20px">
      <h2 style="color:#dc2626">❌ benz2288 價格追蹤器執行失敗</h2>
      <p style="color:#374151"><strong>{subject}</strong></p>
      <pre style="background:#f3f4f6;padding:16px;border-radius:8px;font-size:13px;
                  white-space:pre-wrap;word-break:break-all">{detail}</pre>
      <p style="color:#9ca3af;font-size:12px;margin-top:20px">
        發生時間：{datetime.now():%Y-%m-%d %H:%M} · 請手動執行 python tracker.py --push 補跑
      </p>
    </body></html>"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"❌ iPhone 17 追蹤器失敗 - {datetime.now():%m/%d %H:%M}"
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFY_TO
        msg.attach(MIMEText(body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print(f"📧 錯誤通知已寄出 → {NOTIFY_TO}")
    except Exception as e:
        print(f"⚠️  錯誤通知寄送失敗：{e}")

# ══════════════════════════════════════════════════════
#  📊 公開 Dashboard（index.html）
#  靜態 HTML，可直接部署到 Cloudflare Pages / GitHub Pages
# ══════════════════════════════════════════════════════

CHART_GROUPS = [
    ("iPhone 17", ["iPhone 17 256G 黑/藍/白/紫/綠"]),
    ("iPhone 17 Pro", ["iPhone 17 Pro 256G"]),
    ("iPhone 17 Pro Max", ["iPhone 17 Pro Max 256G 銀/橘"]),
]

def _y_range(all_data: dict, models: list) -> tuple:
    vals = [all_data[d][m] for d in all_data for m in models
            if m in all_data[d] and all_data[d][m] is not None]
    if not vals:
        return 20000, 50000
    lo, hi = min(vals), max(vals)
    margin = max((hi - lo) * 1.5, 500)
    return int(lo - margin), int(hi + margin)

def _chart_json(all_data: dict, dates: list, models: list, color_offset: int) -> str:
    labels = [d[5:] for d in dates]
    datasets = []
    for i, model in enumerate(models):
        color = CHART_COLORS[(color_offset + i) % len(CHART_COLORS)]
        datasets.append({
            "label": model,
            "data": [all_data[d].get(model) for d in dates],
            "borderColor": color,
            "backgroundColor": color + "22",
            "tension": 0.3,
            "fill": False,
            "pointRadius": 5,
            "pointHoverRadius": 8,
            "spanGaps": True,
        })
    return json.dumps({"labels": labels, "datasets": datasets}, ensure_ascii=False)

def generate_dashboard(all_data: dict):
    dates = sorted(all_data.keys())
    model_names = [m["name"] for m in MODELS_DEF]

    latest_date = dates[-1] if dates else "N/A"
    prev_date   = dates[-2] if len(dates) >= 2 else None
    latest = all_data.get(latest_date, {})
    prev   = all_data.get(prev_date, {}) if prev_date else {}

    def diff_cell(model):
        cur = latest.get(model)
        pre = prev.get(model)
        if cur is None or pre is None:
            return '<td class="diff-none">—</td>'
        d = cur - pre
        if d > 0:
            return f'<td class="diff-up">+{d:,}</td>'
        if d < 0:
            return f'<td class="diff-down">{d:,}</td>'
        return '<td class="diff-none">—</td>'

    today_rows = "".join(
        f"""<tr>
          <td class="model-name">{model}</td>
          <td class="model-price">{"$" + f"{latest[model]:,}" if latest.get(model) else "—"}</td>
          {diff_cell(model)}
        </tr>"""
        for model in model_names
    )

    # 為每個圖表群組產生 JSON 與 y 軸範圍
    color_offset = 0
    chart_blocks = []
    for group_name, group_models in CHART_GROUPS:
        cj = _chart_json(all_data, dates, group_models, color_offset)
        y_min, y_max = _y_range(all_data, group_models)
        chart_blocks.append((group_name, cj, y_min, y_max))
        color_offset += len(group_models)

    def chart_script(idx, cj, y_min, y_max):
        return f"""
<script>
new Chart(document.getElementById("chart{idx}"), {{
  type: "line",
  data: {cj},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{
        position: "bottom",
        labels: {{ color: "#8b949e", font: {{ size: 12 }}, padding: 18, boxWidth: 14 }}
      }},
      tooltip: {{
        callbacks: {{
          label: c =>
            ` ${{c.dataset.label}}: ${{c.parsed.y != null ? "$" + c.parsed.y.toLocaleString() : "N/A"}}`
        }}
      }}
    }},
    scales: {{
      x: {{ grid: {{ color: "#21262d" }}, ticks: {{ color: "#6e7681" }} }},
      y: {{
        min: {y_min},
        max: {y_max},
        grid: {{ color: "#21262d" }},
        ticks: {{ color: "#6e7681", callback: v => "$" + v.toLocaleString() }}
      }}
    }}
  }}
}});
</script>"""

    chart_cards = "".join(
        f"""  <div class="card">
    <div class="card-label">價格趨勢 · {name}</div>
    <div class="chart-wrap"><canvas id="chart{i}"></canvas></div>
  </div>
""" + chart_script(i, cj, y_min, y_max)
        for i, (name, cj, y_min, y_max) in enumerate(chart_blocks)
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>benz2288 iPhone 17 價格追蹤</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0 }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
      background: #0d1117; color: #c9d1d9; min-height: 100vh; padding: 28px 16px;
    }}
    .wrap {{ max-width: 900px; margin: 0 auto }}

    header {{ text-align: center; margin-bottom: 36px }}
    header h1 {{ font-size: 22px; font-weight: 700; color: #f0f6fc; letter-spacing: -.3px }}
    header p  {{ color: #6e7681; font-size: 13px; margin-top: 6px }}

    .card {{
      background: #161b22; border: 1px solid #30363d;
      border-radius: 12px; padding: 22px; margin-bottom: 18px;
    }}
    .card-label {{
      font-size: 11px; font-weight: 600; color: #6e7681;
      text-transform: uppercase; letter-spacing: .08em; margin-bottom: 14px;
    }}
    .badge {{
      display: inline-block; font-size: 11px; padding: 1px 9px;
      border-radius: 20px; background: #1f3a5f; color: #58a6ff;
      border: 1px solid #1f6feb40; margin-left: 8px; vertical-align: middle;
      font-weight: 400; text-transform: none; letter-spacing: 0;
    }}

    .chart-wrap {{ position: relative; height: 260px }}

    table {{ width: 100%; border-collapse: collapse; font-size: 14px }}
    th, td {{ padding: 11px 14px; border-bottom: 1px solid #21262d }}
    th {{ font-size: 11px; font-weight: 600; color: #6e7681;
          text-transform: uppercase; letter-spacing: .06em }}
    tr:last-child td {{ border-bottom: none }}
    tr:hover td {{ background: #1c2128 }}
    td.model-name  {{ color: #c9d1d9 }}
    td.model-price {{ text-align: right; font-weight: 700;
                      color: #f87171; font-size: 16px; font-variant-numeric: tabular-nums }}
    td.diff-up   {{ text-align: right; font-weight: 600; color: #f87171; font-variant-numeric: tabular-nums }}
    td.diff-down {{ text-align: right; font-weight: 600; color: #3fb950; font-variant-numeric: tabular-nums }}
    td.diff-none {{ text-align: right; color: #484f58 }}

    footer {{ text-align: right; font-size: 11px; color: #484f58; margin-top: 12px }}
    a {{ color: #58a6ff; text-decoration: none }}
    a:hover {{ text-decoration: underline }}
  </style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📱 benz2288 iPhone 17 現金價追蹤</h1>
    <p>每日自動抓取 · 資料來源 <a href="https://www.benz2288.com.tw" target="_blank">benz2288.com.tw</a></p>
  </header>

  <div class="card">
    <div class="card-label">最新報價 <span class="badge">{latest_date}</span></div>
    <table>
      <tr><th>機型</th><th style="text-align:right">現金價</th><th style="text-align:right">與前日</th></tr>
      {today_rows}
    </table>
  </div>

{chart_cards}

  <footer>
    最後更新：{datetime.now():%Y-%m-%d %H:%M:%S} ·
    資料以實際報價為準，本頁僅供參考
  </footer>
</div>
</body>
</html>"""

    DASHBOARD_HTML.write_text(html, encoding="utf-8")
    print(f"📊 Dashboard 已生成：{DASHBOARD_HTML}")

# ══════════════════════════════════════════════════════
#  🚀 主程式
# ══════════════════════════════════════════════════════

def main():
    is_calibrate = "--calibrate" in sys.argv
    auto_push    = "--push"      in sys.argv
    dry_run      = "--dry-run"   in sys.argv

    print("=" * 54)
    print(f"  benz2288 iPhone 17 價格追蹤器")
    print(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 54)

    url, gif_date = find_latest_gif()
    if not url:
        raise RuntimeError("重試 3 次仍無法取得 GIF，網路可能異常或店家暫時下架報價表")

    if dry_run:
        print("ℹ️  Dry-run 模式，不執行 OCR 及寫入")
        return

    print("⬇️  下載圖片...")
    img = download_image(url)
    print(f"   尺寸：{img.size[0]}×{img.size[1]} px")

    coords = load_coords()

    if is_calibrate:
        calibrate(img, coords)
        return

    # 每次都先跑 auto_scan（傳入快取座標讓它優先選最近的 row）
    # scan 直接回傳 (coords, price)，避免重複 OCR 的不一致
    # 掃不到的機型 fallback 用快取座標重新 OCR
    model_range = {m["name"]: m["range"] for m in MODELS_DEF}
    scan_results = auto_scan_coords(img, current_coords=coords)  # {name: (coord, price)}

    final_coords = {}
    print("\n💰 OCR 辨識現金價：")
    today_prices = {}
    for model in coords:
        lo, hi = model_range.get(model, (0, 999999))
        if model in scan_results:
            coord, price = scan_results[model]
            final_coords[model] = coord
            today_prices[model] = price
            print(f"   {model}: ${price:,}")
        else:
            # fallback：用快取座標重新 OCR
            final_coords[model] = coords[model]
            print(f"   ⚠️  {model} 掃描未找到，使用快取座標")
            price = ocr_price(img, coords[model])
            if price and lo <= price <= hi:
                today_prices[model] = price
                print(f"   {model}: ${price:,}")
            else:
                today_prices[model] = None
                print(f"   {model}: ⚠️  辨識失敗")

    save_coords(final_coords)

    still_failed = [m for m, p in today_prices.items() if not p]
    if still_failed:
        send_error_email(
            "OCR 辨識失敗，需要人工介入",
            "失敗機型：\n" + "\n".join(f"  - {m}" for m in still_failed) +
            f"\n\nGIF 尺寸：{img.size[0]}×{img.size[1]} px"
        )

    # 儲存
    date_str = gif_date.isoformat()
    all_data = load_prices()
    prev_date = max((d for d in all_data if d < date_str), default=None)
    prev = all_data.get(prev_date, {}) if prev_date else {}

    all_data[date_str] = today_prices
    save_prices(all_data)
    print(f"\n💾 已寫入 prices.json（{date_str}）")

    # 比對變動
    changes = {
        m: (prev[m], p)
        for m, p in today_prices.items()
        if p and prev.get(m) and prev[m] != p
    }
    if changes:
        print(f"\n🚨 {len(changes)} 個機型價格異動，發送 Gmail 通知...")
        send_email(changes, date_str)
    elif prev:
        print("\n✅ 價格無變動")
    else:
        print("\nℹ️  首次執行，基準資料已建立")

    # 生成 Dashboard
    generate_dashboard(all_data)

    # 選擇性部署到 Cloudflare Pages
    if auto_push:
        print("\n📤 部署至 Cloudflare Pages...")
        deploy_env = os.environ.copy()
        cf_token = os.environ.get("CLOUDFLARE_API_TOKEN") or _ENV.get("CLOUDFLARE_API_TOKEN", "")
        if cf_token:
            deploy_env["CLOUDFLARE_API_TOKEN"] = cf_token
        result = subprocess.run(
            "npx wrangler pages deploy . --project-name benz2288-tracker --branch main --commit-dirty=true",
            cwd=BASE_DIR, capture_output=True, text=True, shell=True,
            encoding="utf-8", errors="replace", env=deploy_env
        )
        if result.returncode == 0:
            # 從輸出中擷取部署 URL
            for line in result.stdout.splitlines():
                if "pages.dev" in line:
                    print(f"✅ {line.strip()}")
                    break
            else:
                print("✅ 部署完成")
        else:
            print(f"⚠️  部署失敗：{result.stderr[-300:]}")

    print(f"\n🎉 完成！用瀏覽器開啟 index.html 查看 Dashboard")


if __name__ == "__main__":
    try:
        main()
    except (Exception, SystemExit) as e:
        import traceback
        tb = traceback.format_exc()
        print(f"\n❌ 執行失敗：{e}\n{tb}")
        if not isinstance(e, SystemExit) or e.code != 0:
            send_error_email(str(e), tb)
        sys.exit(getattr(e, "code", 1) or 1)
