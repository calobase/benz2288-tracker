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
# Gmail App Password 請用環境變數設定，不要寫在程式裡：
#   Windows PowerShell: $env:GMAIL_APP_PASSWORD = "xxxx xxxx xxxx xxxx"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# Tesseract 安裝路徑（預設 Windows 安裝位置）
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

BASE_DIR       = Path(__file__).parent
PRICES_JSON    = BASE_DIR / "prices.json"
DASHBOARD_HTML = BASE_DIR / "index.html"

# ══════════════════════════════════════════════════════
#  📐 OCR 座標（3302×4066 原圖中「現金價」欄位的像素範圍）
#  格式：(x1, y1, x2, y2)
#  若辨識結果不對，請執行 --calibrate 模式後對照 calibrate.jpg 調整
# ══════════════════════════════════════════════════════

MODELS = {
    "iPhone 17 256G 綠/紫/白":      (873, 1192, 966, 1228),
    "iPhone 17 256G 黑/藍":         (873, 1219, 966, 1257),
    "iPhone 17 Pro 256G 銀/藍/橘":  (873, 1465, 966, 1503),
    "iPhone 17 Pro Max 256G 銀":    (873, 1686, 966, 1724),
    "iPhone 17 Pro Max 256G 橘/藍": (873, 1719, 966, 1757),
}

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
    從首頁 HTML 解析報價 GIF 連結。
    店家的 GIF 檔名不一定每天更換，但內容會覆蓋更新，
    日期顯示在圖片右上角。資料庫日期使用系統今日日期。
    回傳 (absolute_url, date.today())
    """
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
        print(f"⚠️  首頁解析失敗：{e}")

    return None, None


def download_image(url: str) -> Image.Image:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")

# ══════════════════════════════════════════════════════
#  🔍 OCR
# ══════════════════════════════════════════════════════

def ocr_price(img: Image.Image, box: tuple, scale: int = 4) -> int | None:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    cropped = img.crop(box)
    w, h = cropped.size
    cropped = cropped.resize((w * scale, h * scale), Image.LANCZOS)
    cropped = cropped.convert("L")
    cropped = ImageEnhance.Contrast(cropped).enhance(2.5)
    text = pytesseract.image_to_string(
        cropped,
        config="--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789$,"
    ).strip()
    m = re.search(r"(\d[\d,]{3,})", text)
    return int(m.group(1).replace(",", "")) if m else None


def calibrate(img: Image.Image):
    """在原圖上標示所有 OCR 框，儲存為 calibrate.jpg 供人工比對"""
    debug = img.copy()
    draw = ImageDraw.Draw(debug)
    for name, box in MODELS.items():
        draw.rectangle(box, outline="red", width=4)
        price = ocr_price(img, box)
        label = f"{name[:20]} → {'$'+f'{price:,}' if price else 'FAIL'}"
        draw.text((box[0], max(0, box[1] - 20)), label, fill=(255, 255, 0))
    out = BASE_DIR / "calibrate.jpg"
    debug.save(out)
    print(f"📸 已儲存 {out}")
    print("   請用圖片檢視器開啟，確認紅框是否落在正確的價格儲存格上")
    print("   若有偏差，調整 tracker.py 頂部 MODELS 字典內的座標後重試")

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

# ══════════════════════════════════════════════════════
#  📊 公開 Dashboard（index.html）
#  靜態 HTML，可直接部署到 Cloudflare Pages / GitHub Pages
# ══════════════════════════════════════════════════════

def generate_dashboard(all_data: dict):
    dates = sorted(all_data.keys())
    model_names = list(MODELS.keys())

    datasets = []
    for i, model in enumerate(model_names):
        color = CHART_COLORS[i % len(CHART_COLORS)]
        datasets.append({
            "label": model,
            "data": [{"x": d, "y": all_data[d].get(model)} for d in dates],
            "borderColor": color,
            "backgroundColor": color + "22",
            "tension": 0.35,
            "fill": False,
            "pointRadius": 5,
            "pointHoverRadius": 8,
        })

    chart_json = json.dumps({"datasets": datasets}, ensure_ascii=False)

    latest_date = dates[-1] if dates else "N/A"
    latest = all_data.get(latest_date, {})

    today_rows = "".join(
        f"""<tr>
          <td class="model-name">{model}</td>
          <td class="model-price">{"$" + f"{latest[model]:,}" if latest.get(model) else "—"}</td>
        </tr>"""
        for model in model_names
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>benz2288 iPhone 17 價格追蹤</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
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

    .chart-wrap {{ position: relative; height: 380px }}

    table {{ width: 100%; border-collapse: collapse; font-size: 14px }}
    th, td {{ padding: 11px 14px; border-bottom: 1px solid #21262d }}
    th {{ font-size: 11px; font-weight: 600; color: #6e7681;
          text-transform: uppercase; letter-spacing: .06em }}
    tr:last-child td {{ border-bottom: none }}
    tr:hover td {{ background: #1c2128 }}
    td.model-name  {{ color: #c9d1d9 }}
    td.model-price {{ text-align: right; font-weight: 700;
                      color: #f87171; font-size: 16px; font-variant-numeric: tabular-nums }}

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
      <tr><th>機型</th><th style="text-align:right">現金價</th></tr>
      {today_rows}
    </table>
  </div>

  <div class="card">
    <div class="card-label">價格趨勢</div>
    <div class="chart-wrap">
      <canvas id="priceChart"></canvas>
    </div>
  </div>

  <footer>
    最後更新：{datetime.now():%Y-%m-%d %H:%M:%S} ·
    資料以實際報價為準，本頁僅供參考
  </footer>
</div>

<script>
const cfg = {chart_json};
new Chart(document.getElementById("priceChart"), {{
  type: "line",
  data: cfg,
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    parsing: false,
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
      x: {{
        type: "time",
        time: {{ unit: "day", tooltipFormat: "yyyy-MM-dd", displayFormats: {{ day: "MM/dd" }} }},
        grid: {{ color: "#21262d" }},
        ticks: {{ color: "#6e7681" }}
      }},
      y: {{
        grid: {{ color: "#21262d" }},
        ticks: {{ color: "#6e7681", callback: v => "$" + v.toLocaleString() }}
      }}
    }}
  }}
}});
</script>
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
        print("❌ 找不到可用的 GIF（店家可能尚未更新今日報價）")
        sys.exit(1)

    if dry_run:
        print("ℹ️  Dry-run 模式，不執行 OCR 及寫入")
        return

    print("⬇️  下載圖片...")
    img = download_image(url)
    print(f"   尺寸：{img.size[0]}×{img.size[1]} px")

    if is_calibrate:
        calibrate(img)
        return

    # OCR
    print("\n💰 OCR 辨識現金價：")
    today_prices = {}
    all_ok = True
    for model, box in MODELS.items():
        price = ocr_price(img, box)
        today_prices[model] = price
        status = f"${price:,}" if price else "❌ 辨識失敗（請執行 --calibrate）"
        if not price:
            all_ok = False
        print(f"   {model}: {status}")

    if not all_ok:
        print("\n⚠️  部分機型辨識失敗，建議執行 python tracker.py --calibrate 校準座標")

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

    # 選擇性 git push
    if auto_push:
        print("\n📤 Git push 至 GitHub...")
        subprocess.run(["git", "add", "prices.json", "index.html"], cwd=BASE_DIR, check=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"chore: price update {date_str}"],
            cwd=BASE_DIR, capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout + result.stderr:
            print("ℹ️  無新變動，跳過 commit")
        else:
            subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
            print("✅ 已推送至 GitHub")

    print(f"\n🎉 完成！用瀏覽器開啟 index.html 查看 Dashboard")


if __name__ == "__main__":
    main()
