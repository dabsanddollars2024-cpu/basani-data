#!/usr/bin/env python3
"""
BASANI Daily Report Generator
Reads scan_output.json + history + plays_log.json
Generates a professional HTML report for the trading group.

Run: python3 daily_report.py
Output: reports/daily_report_YYYY-MM-DD.html
"""
import json, os, glob
from datetime import datetime, date, timedelta

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(BASE_DIR, "history")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
PLAYS_FILE  = os.path.join(BASE_DIR, "plays_log.json")
SCAN_FILE   = os.path.join(BASE_DIR, "scan_output.json")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ── Load data ──────────────────────────────────────────────────────────────
def load_json(path, default=None):
    try:
        with open(path) as f: return json.load(f)
    except: return default or {}

def load_latest_scan():
    return load_json(SCAN_FILE, {})

def load_history(days_back=2):
    """Load scan files from the past N days for grading."""
    cutoff = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
    files  = sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json")))
    return [load_json(f) for f in files if os.path.basename(f) >= cutoff + "_0000"]

def load_plays():
    return load_json(PLAYS_FILE, {}).get("plays", [])

# ── Grading engine ─────────────────────────────────────────────────────────
def grade_predictions(history):
    """
    Compare yesterday's scanner predictions to today's prices.
    A prediction is correct if:
      BULLISH → today's price > yesterday's price (or > yesterday's close)
      BEARISH → today's price < yesterday's price
    """
    if len(history) < 2:
        return [], {"correct": 0, "total": 0, "rate": 0}

    # Get yesterday's last scan and today's first scan
    today_scans = [h for h in history if h.get("scan_time", "")[:10] == date.today().isoformat()]
    yest_scans  = [h for h in history if h.get("scan_time", "")[:10] == (date.today() - timedelta(days=1)).isoformat()]

    if not today_scans or not yest_scans:
        return [], {"correct": 0, "total": 0, "rate": 0}

    today_prices = {t["ticker"]: t["price"] for t in today_scans[-1].get("tickers", [])}
    yesterday    = yest_scans[-1].get("tickers", [])

    grades = []
    correct = 0
    for tick in yesterday:
        sym  = tick["ticker"]
        pred = tick.get("direction", "NEUTRAL")
        if pred == "NEUTRAL" or sym not in today_prices:
            continue
        prev_price    = tick["price"]
        current_price = today_prices[sym]
        chg           = (current_price - prev_price) / prev_price * 100
        if pred == "BULLISH":
            hit = current_price > prev_price
        elif pred == "BEARISH":
            hit = current_price < prev_price
        else:
            hit = False
        correct += int(hit)
        grades.append({
            "ticker": sym, "prediction": pred, "prev_price": prev_price,
            "current_price": current_price, "chg_pct": round(chg, 2), "correct": hit
        })

    total = len(grades)
    rate  = round(correct / total * 100, 1) if total else 0
    return grades, {"correct": correct, "total": total, "rate": rate}

def grade_options_plays(plays):
    """Summarize closed options plays."""
    closed = [p for p in plays if p.get("status") == "CLOSED"]
    if not closed: return [], {}
    wins   = [p for p in closed if (p.get("pnl_pct") or 0) > 0]
    losses = [p for p in closed if (p.get("pnl_pct") or 0) <= 0]
    avg    = sum(p.get("pnl_pct") or 0 for p in closed) / len(closed)
    return closed, {
        "total": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "avg_pnl": round(avg, 1),
        "best": max(closed, key=lambda x: x.get("pnl_pct") or 0),
        "worst": min(closed, key=lambda x: x.get("pnl_pct") or 0),
    }

def generate_options_plays(tickers):
    """Generate today's intraday scalp + swing option suggestions from scan data."""
    scalps  = []
    swings  = []
    from datetime import date, timedelta

    def next_friday(weeks=0):
        today = date.today()
        days_ahead = (4 - today.weekday()) % 7
        if days_ahead == 0: days_ahead = 7
        return (today + timedelta(days=days_ahead + weeks * 7)).strftime("%b %d")

    top = [t for t in tickers if t["score"] >= 75 and t["direction"] == "BULLISH" and t.get("rsi") and t["rsi"] < 75]
    bearish = [t for t in tickers if t["score"] <= 30 or (t.get("rsi") and t["rsi"] < 40)]

    for t in top[:5]:
        price = t["price"]
        r     = t.get("rsi", 60)
        # Scalp: ATM, this Friday
        scalp_strike = round(price * 1.005 / 0.5) * 0.5  # round to nearest $0.50
        scalps.append({
            "ticker": t["ticker"], "type": "CALL", "strike": scalp_strike,
            "expiry": next_friday(0), "score": t["score"], "rsi": r,
            "price": price, "thesis": f"Score {t['score']}, RSI {r}, {', '.join(t['signals'][:2])}",
            "style": "SCALP", "dte": "2-5 days"
        })
        # Swing: 1-2% OTM, 2 weeks out
        swing_strike = round(price * 1.02 / 1.0) * 1.0
        swings.append({
            "ticker": t["ticker"], "type": "CALL", "strike": swing_strike,
            "expiry": next_friday(2), "score": t["score"], "rsi": r,
            "price": price, "thesis": f"Score {t['score']}, RSI {r}, {', '.join(t['signals'][:3])}",
            "style": "SWING", "dte": "14-16 days"
        })

    for t in bearish[:2]:
        price = t["price"]
        r     = t.get("rsi", 40)
        put_strike = round(price * 0.98 / 1.0) * 1.0
        swings.append({
            "ticker": t["ticker"], "type": "PUT", "strike": put_strike,
            "expiry": next_friday(1), "score": t["score"], "rsi": r,
            "price": price, "thesis": f"Score {t['score']}, RSI {r} — bearish setup, {', '.join(t['signals'][:2])}",
            "style": "SWING", "dte": "7-10 days"
        })

    return scalps, swings

# ── HTML builder ───────────────────────────────────────────────────────────
def score_color(score):
    if score >= 80: return "#00d084"
    if score >= 60: return "#f5a623"
    if score >= 40: return "#e8e8e8"
    return "#ff4d4d"

def chg_color(chg):
    return "#00d084" if chg >= 0 else "#ff4d4d"

def grade_badge(grade):
    colors = {"A+":"#00d084","A":"#00d084","B+":"#7ed321","B":"#7ed321",
              "C":"#f5a623","D":"#ff6b35","F":"#ff4d4d"}
    color = colors.get(grade, "#aaa")
    return f'<span style="background:{color};color:#000;padding:2px 8px;border-radius:4px;font-weight:bold;font-size:12px">{grade}</span>'

def build_html(scan, grades, grade_stats, plays, play_stats, scalps, swings):
    today_str  = datetime.now().strftime("%A, %B %d, %Y")
    scan_time  = scan.get("scan_time", "N/A")
    tickers    = scan.get("tickers", [])
    spy        = next((t for t in tickers if t["ticker"] == "SPY"), {})
    qqq        = next((t for t in tickers if t["ticker"] == "QQQ"), {})
    top_picks  = [t for t in tickers if t["score"] >= 75 and t["direction"] == "BULLISH"][:6]
    bearish    = [t for t in tickers if t["score"] <= 30][:3]

    spy_chg  = spy.get("chg_pct", 0)
    qqq_chg  = qqq.get("chg_pct", 0)
    market_sentiment = "🟢 RISK ON" if spy_chg > 0.3 else ("🔴 RISK OFF" if spy_chg < -0.3 else "🟡 MIXED")

    def row(t):
        sc = t['score']; ch = t['chg_pct']
        r  = t.get('rsi','—')
        return f"""
        <tr>
          <td><strong>{t['ticker']}</strong></td>
          <td>${t['price']:.2f}</td>
          <td style="color:{chg_color(ch)};font-weight:bold">{ch:+.2f}%</td>
          <td><span style="color:{score_color(sc)};font-weight:bold">{sc}</span></td>
          <td>{r}</td>
          <td>${t.get('sma20') or '—'}</td>
          <td>${t.get('sma50') or '—'}</td>
          <td style="color:{score_color(sc)}">{t['direction']}</td>
          <td style="font-size:11px;color:#aaa">{' · '.join(t['signals'][:3])}</td>
        </tr>"""

    def opt_row(o):
        badge_color = "#00d084" if o["type"] == "CALL" else "#ff4d4d"
        style_badge = "#f5a623" if o["style"] == "SCALP" else "#7b68ee"
        return f"""
        <tr>
          <td><strong>{o['ticker']}</strong></td>
          <td style="color:{badge_color};font-weight:bold">{o['type']}</td>
          <td>${o['strike']:.2f}</td>
          <td>{o['expiry']}</td>
          <td>{o['dte']}</td>
          <td><span style="background:{style_badge};color:#fff;padding:1px 7px;border-radius:3px;font-size:11px">{o['style']}</span></td>
          <td style="font-size:11px;color:#bbb">{o['thesis']}</td>
        </tr>"""

    def grade_row(g):
        color = "#00d084" if g["correct"] else "#ff4d4d"
        icon  = "✅" if g["correct"] else "❌"
        return f"""
        <tr>
          <td><strong>{g['ticker']}</strong></td>
          <td style="color:{color}">{g['prediction']}</td>
          <td>${g['prev_price']:.2f}</td>
          <td>${g['current_price']:.2f}</td>
          <td style="color:{chg_color(g['chg_pct'])};font-weight:bold">{g['chg_pct']:+.2f}%</td>
          <td>{icon}</td>
        </tr>"""

    def play_row(p):
        pnl   = p.get("pnl_pct")
        gr    = p.get("grade") or "—"
        color = "#00d084" if (pnl or 0) > 0 else "#ff4d4d"
        status = p.get("status", "OPEN")
        if status == "OPEN":
            return f"""
        <tr>
          <td><strong>{p['id']}</strong></td>
          <td>{p['ticker']}</td>
          <td>{p['type']}</td>
          <td>${p['strike']}</td>
          <td>{p.get('expiry','—')}</td>
          <td style="font-family:monospace">${p.get('est_option_mid') or '—'}</td>
          <td>{grade_badge(gr)}</td>
          <td style="font-size:11px;color:#aaa">{p.get('thesis','')[:90]}</td>
        </tr>"""
        else:
            return f"""
        <tr>
          <td><strong>{p['id']}</strong></td>
          <td>{p['ticker']}</td>
          <td>{p['type']}</td>
          <td>${p['strike']}</td>
          <td>{p.get('exit_date','—')}</td>
          <td style="color:{color};font-weight:bold">{f'{pnl:+.1f}%' if pnl is not None else '—'}</td>
          <td>{grade_badge(gr)}</td>
          <td style="font-size:11px;color:#aaa">{p.get('notes','')[:80]}</td>
        </tr>"""

    ticker_rows  = "".join(row(t) for t in tickers)
    scalp_rows   = "".join(opt_row(o) for o in scalps)
    swing_rows   = "".join(opt_row(o) for o in swings)
    grade_rows   = "".join(grade_row(g) for g in grades)
    open_play_rows   = "".join(play_row(p) for p in plays if p.get("status") == "OPEN")
    closed_play_rows = "".join(play_row(p) for p in plays if p.get("status") == "CLOSED")

    pred_rate   = grade_stats.get("rate", 0)
    pred_color  = "#00d084" if pred_rate >= 60 else ("#f5a623" if pred_rate >= 45 else "#ff4d4d")
    opt_winrate = play_stats.get("win_rate", 0)
    opt_color   = "#00d084" if opt_winrate >= 55 else ("#f5a623" if opt_winrate >= 40 else "#ff4d4d")

    open_plays = [p for p in plays if p.get("status") == "OPEN"]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BASANI Market Report — {today_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600;700&family=Playfair+Display+SC:wght@400;700&family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=EB+Garamond:ital,wght@0,300;0,400;0,500;1,300&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --black:#000000; --white:#ffffff; --off-white:#f0ece4;
    --gold:#c9a84c; --dim:#555; --dimmer:#2a2a2a;
    --green:#4caf7d; --red:#c0392b; --amber:#c9a84c;
    --surface:#0a0a0a; --card:#0d0d0d;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ background:var(--black); color:var(--off-white); font-family:'EB Garamond',Georgia,serif; font-size:15px; line-height:1.7; -webkit-font-smoothing:antialiased }}
  .container {{ max-width:980px; margin:0 auto; padding:40px 28px }}

  /* HEADER */
  .header {{ text-align:center; padding:48px 40px 36px; border-bottom:1px solid var(--dimmer); margin-bottom:40px }}
  .logo-wrap {{ display:flex; flex-direction:column; align-items:center; gap:18px; margin-bottom:24px }}
  .brand-name {{ font-family:'Playfair Display SC',serif; font-size:40px; font-weight:400; letter-spacing:0.3em; color:var(--white) }}
  .brand-sub {{ font-family:'EB Garamond',serif; font-size:13px; letter-spacing:0.28em; color:var(--dim); font-style:italic; margin-top:4px }}
  .header-meta {{ display:flex; justify-content:center; align-items:center; gap:28px; flex-wrap:wrap; margin-top:20px }}
  .meta-item {{ text-align:center }}
  .meta-label {{ font-family:'Cinzel',serif; font-size:9px; letter-spacing:0.2em; color:var(--dim); text-transform:uppercase }}
  .meta-value {{ font-family:'Playfair Display',serif; font-size:17px; color:var(--off-white); margin-top:2px }}
  .meta-divider {{ width:1px; height:36px; background:var(--dimmer) }}

  /* SECTIONS */
  .section {{ margin-bottom:40px }}
  .section-title {{ font-family:'Cinzel',serif; font-size:10px; font-weight:600; letter-spacing:0.3em; color:var(--dim); text-transform:uppercase; padding-bottom:10px; border-bottom:1px solid var(--dimmer); margin-bottom:20px }}

  /* STATS */
  .stats-row {{ display:flex; gap:1px; background:var(--dimmer); border:1px solid var(--dimmer); margin-bottom:24px }}
  .stat-box {{ background:var(--black); padding:18px 22px; flex:1 }}
  .s-label {{ font-family:'Cinzel',serif; font-size:9px; letter-spacing:0.2em; color:var(--dim); text-transform:uppercase }}
  .s-val {{ font-family:'Space Mono',monospace; font-size:22px; font-weight:700; margin-top:6px }}
  .market-bar {{ display:flex; gap:1px; background:var(--dimmer); border:1px solid var(--dimmer); margin-top:20px; flex-wrap:wrap }}
  .market-pill {{ background:var(--black); padding:14px 20px; flex:1; min-width:110px }}
  .m-label {{ font-family:'Cinzel',serif; font-size:9px; letter-spacing:0.15em; color:var(--dim); text-transform:uppercase }}
  .m-val {{ font-family:'Space Mono',monospace; font-size:17px; font-weight:700; margin-top:4px }}

  /* TABLES */
  table {{ width:100%; border-collapse:collapse }}
  th {{ font-family:'Cinzel',serif; font-size:9px; letter-spacing:0.2em; color:var(--dim); padding:10px 12px; text-align:left; font-weight:400; border-bottom:1px solid var(--dimmer) }}
  td {{ padding:11px 12px; border-bottom:1px solid #111; font-size:14px; color:#bbb }}
  tr:last-child td {{ border-bottom:none }}
  tr:hover td {{ background:var(--surface) }}
  .t-ticker {{ font-family:'Cinzel',serif; font-weight:600; font-size:14px; color:var(--white); letter-spacing:0.08em }}
  .t-mono {{ font-family:'Space Mono',monospace; font-size:12px }}
  .badge {{ display:inline-block; padding:1px 8px; font-family:'Cinzel',serif; font-size:8px; letter-spacing:0.1em; border:1px solid }}
  .bull {{ color:var(--green); border-color:rgba(76,175,125,0.3) }}
  .bear {{ color:var(--red); border-color:rgba(192,57,43,0.3) }}
  .neut {{ color:var(--dim); border-color:var(--dimmer) }}
  .footer {{ text-align:center; color:#333; font-size:11px; padding:30px 0 10px; border-top:1px solid var(--dimmer); margin-top:40px; letter-spacing:0.08em }}
  .footer-logo {{ font-family:'Playfair Display SC',serif; font-size:13px; letter-spacing:0.35em; color:var(--dimmer); margin-bottom:6px }}
  .open-pill {{ display:inline-block; background:#0d1f15; border:1px solid rgba(76,175,125,0.3); color:var(--green); padding:3px 12px; margin:3px; font-family:'Cinzel',serif; font-size:9px; letter-spacing:0.1em }}
</style>
</head>
<body>
<div class="container">

  <!-- HEADER -->
  <div class="header">
    <div class="logo-wrap">
      <svg viewBox="0 0 160 200" fill="none" xmlns="http://www.w3.org/2000/svg" style="width:64px;height:80px;opacity:0.95">
        <rect x="65" y="177" width="30" height="4" rx="0.5" fill="none" stroke="white" stroke-width="1.3" opacity="0.9"/>
        <rect x="63" y="169" width="34" height="4" rx="0.5" fill="none" stroke="white" stroke-width="1.3" opacity="0.9"/>
        <rect x="62" y="161" width="36" height="4" rx="0.5" fill="none" stroke="white" stroke-width="1.3" opacity="0.9"/>
        <path d="M74 156 C72 158 68 160 65 162" stroke="white" stroke-width="1.1" fill="none" opacity="0.7"/>
        <path d="M86 156 C86 158 86 159 86 162" stroke="white" stroke-width="1.1" fill="none" opacity="0.7"/>
        <path d="M106 156 C108 158 112 160 115 162" stroke="white" stroke-width="1.1" fill="none" opacity="0.7"/>
        <path d="M58 145 C58 150 62 157 65 162" stroke="white" stroke-width="1.2" fill="none" opacity="0.75"/>
        <path d="M102 145 C102 150 118 157 115 162" stroke="white" stroke-width="1.2" fill="none" opacity="0.75"/>
        <path d="M58 115 C52 122 50 132 52 142 C54 150 58 155 58 155 L102 155 C102 155 106 150 108 142 C110 132 108 122 102 115" stroke="white" stroke-width="1.3" fill="none" opacity="0.9"/>
        <line x1="80" y1="44" x2="80" y2="152" stroke="white" stroke-width="0.9" stroke-dasharray="2.5,2.5" opacity="0.45"/>
        <path d="M80 44 C74 40 64 40 56 46 C48 52 44 62 44 72 C44 84 48 94 54 102 C58 108 58 115 58 115" stroke="white" stroke-width="1.5" fill="none" opacity="0.95"/>
        <path d="M66 46 C64 50 60 52 58 56 C57 60 60 64 64 63 C68 62 70 58 68 54" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M56 58 C52 62 50 68 52 74 C54 80 60 80 62 76 C64 72 60 68 62 64" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M48 78 C46 84 48 92 52 96 C56 100 60 98 60 94 C60 90 56 88 56 84 C56 80 58 76 56 74" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M72 46 C70 52 66 56 68 62 C70 68 76 68 78 64 C80 60 78 54 80 50" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M62 84 C66 80 72 80 74 84 C76 88 72 92 68 92 C64 92 62 96 66 100 C70 104 76 102 78 98" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M66 106 C64 110 64 116 66 120 C68 124 72 124 74 120 C76 116 74 112 76 108" stroke="white" stroke-width="1.05" fill="none" opacity="0.75"/>
        <path d="M80 44 C86 40 96 40 104 46 C112 52 116 62 116 72 C116 84 112 94 106 102 C102 108 102 115 102 115" stroke="white" stroke-width="1.5" fill="none" opacity="0.95"/>
        <path d="M94 46 C96 50 100 52 102 56 C103 60 100 64 96 63 C92 62 90 58 92 54" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M104 58 C108 62 110 68 108 74 C106 80 100 80 98 76 C96 72 100 68 98 64" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M112 78 C114 84 112 92 108 96 C104 100 100 98 100 94 C100 90 104 88 104 84 C104 80 102 76 104 74" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M88 46 C90 52 94 56 92 62 C90 68 84 68 82 64 C80 60 82 54 80 50" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M98 84 C94 80 88 80 86 84 C84 88 88 92 92 92 C96 92 98 96 94 100 C90 104 84 102 82 98" stroke="white" stroke-width="1.1" fill="none" opacity="0.8"/>
        <path d="M94 106 C96 110 96 116 94 120 C92 124 88 124 86 120 C84 116 86 112 84 108" stroke="white" stroke-width="1.05" fill="none" opacity="0.75"/>
      </svg>
      <div class="brand-name">BASANI</div>
      <div class="brand-sub">Market Intelligence &nbsp;·&nbsp; Daily Briefing</div>
    </div>
    <div class="header-meta">
      <div class="meta-item"><div class="meta-label">Date</div><div class="meta-value">{today_str}</div></div>
      <div class="meta-divider"></div>
      <div class="meta-item"><div class="meta-label">Last Scan</div><div class="meta-value">{scan_time}</div></div>
      <div class="meta-divider"></div>
      <div class="meta-item"><div class="meta-label">Regime</div><div class="meta-value" style="color:var(--green)">{market_sentiment}</div></div>
    </div>
    <div class="market-bar" style="margin-top:28px">
      <div class="market-pill">
        <div class="m-label">SPY</div>
        <div class="m-val" style="color:{chg_color(spy_chg)}">${spy.get('price',0):.2f} <span style="font-size:13px">{spy_chg:+.2f}%</span></div>
      </div>
      <div class="market-pill">
        <div class="m-label">QQQ</div>
        <div class="m-val" style="color:{chg_color(qqq_chg)}">${qqq.get('price',0):.2f} <span style="font-size:13px">{qqq_chg:+.2f}%</span></div>
      </div>
      <div class="market-pill">
        <div class="m-label">Prediction Rate</div>
        <div class="m-val" style="color:{pred_color}">{pred_rate}%</div>
      </div>
      <div class="market-pill">
        <div class="m-label">Options Win Rate</div>
        <div class="m-val" style="color:{opt_color}">{opt_winrate}%</div>
      </div>
    </div>
  </div>

  <!-- PERFORMANCE STATS -->
  <div class="section">
    <div class="section-title">Scanner Performance</div>
    <div class="stats-row">
      <div class="stat-box">
        <div class="s-label">Move Prediction</div>
        <div class="s-val" style="color:{pred_color}">{pred_rate}%</div>
        <div style="font-size:11px;color:#666;margin-top:4px">{grade_stats.get('correct',0)}/{grade_stats.get('total',0)} correct today</div>
      </div>
      <div class="stat-box">
        <div class="s-label">Options Win Rate</div>
        <div class="s-val" style="color:{opt_color}">{opt_winrate}%</div>
        <div style="font-size:11px;color:#666;margin-top:4px">{play_stats.get('wins',0)}W / {play_stats.get('losses',0)}L</div>
      </div>
      <div class="stat-box">
        <div class="s-label">Avg Options P&L</div>
        <div class="s-val" style="color:{'#00d084' if play_stats.get('avg_pnl',0)>0 else '#ff4d4d'}">{play_stats.get('avg_pnl',0):+.1f}%</div>
      </div>
      <div class="stat-box">
        <div class="s-label">Open Positions</div>
        <div class="s-val" style="color:#f5a623">{len(open_plays)}</div>
      </div>
      {'<div class="stat-box"><div class="s-label">Best Play</div><div class="s-val" style="color:#00d084;font-size:14px">'+play_stats["best"]["ticker"]+" "+f'{play_stats["best"].get("pnl_pct",0):+.0f}%'+'</div></div>' if play_stats.get("best") else ""}
    </div>
    {"<div style='margin-top:8px'><strong style='color:#888;font-size:12px'>OPEN POSITIONS: </strong>" + "".join(f'<span class="open-pill">{p["id"]} {p["ticker"]} {p["type"]} ${p["strike"]}</span>' for p in open_plays) + "</div>" if open_plays else ""}
  </div>

  <!-- TODAY'S SCANNER -->
  <div class="section">
    <div class="section-title">Today's Scanner — All Tickers</div>
    <table>
      <tr><th>Ticker</th><th>Price</th><th>Chg%</th><th>Score</th><th>RSI</th><th>SMA20</th><th>SMA50</th><th>Direction</th><th>Signals</th></tr>
      {ticker_rows}
    </table>
  </div>

  <!-- INTRADAY SCALP OPTIONS -->
  <div class="section">
    <div class="section-title">Intraday Scalp Options (2–5 DTE)</div>
    <p style="color:#888;font-size:12px;margin-bottom:12px">Short-duration plays for quick moves. Enter on confirmation, exit same day or next morning.</p>
    <table>
      <tr><th>Ticker</th><th>Type</th><th>Strike</th><th>Expiry</th><th>DTE</th><th>Style</th><th>Thesis</th></tr>
      {scalp_rows if scalps else '<tr><td colspan="7" style="color:#555;text-align:center;padding:20px">No high-conviction scalp setups today</td></tr>'}
    </table>
  </div>

  <!-- SWING OPTIONS -->
  <div class="section">
    <div class="section-title">Swing Options (7–16 DTE)</div>
    <p style="color:#888;font-size:12px;margin-bottom:12px">1–2 week plays. Size smaller, give room to work. Stop at -50% on premium.</p>
    <table>
      <tr><th>Ticker</th><th>Type</th><th>Strike</th><th>Expiry</th><th>DTE</th><th>Style</th><th>Thesis</th></tr>
      {swing_rows if swings else '<tr><td colspan="7" style="color:#555;text-align:center;padding:20px">No swing setups today</td></tr>'}
    </table>
  </div>

  <!-- PREDICTION GRADING -->
  <div class="section">
    <div class="section-title">Yesterday's Predictions vs Actual</div>
    {f'<div style="background:#1a2a1a;border:1px solid #00d084;border-radius:6px;padding:12px;margin-bottom:14px;color:#00d084"><strong>{pred_rate}% accuracy</strong> — {grade_stats.get("correct",0)} correct out of {grade_stats.get("total",0)} predictions</div>' if grades else '<p style="color:#555">Not enough history yet — accuracy tracking builds over time.</p>'}
    {'<table><tr><th>Ticker</th><th>Prediction</th><th>Prev Price</th><th>Today</th><th>Chg%</th><th>Result</th></tr>' + grade_rows + '</table>' if grades else ''}
  </div>

  <!-- OPTIONS SCORECARD -->
  <div class="section">
    <div class="section-title">Options Play Log — Open Positions ({len(open_plays)})</div>
    {('<table><tr><th>ID</th><th>Ticker</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Entry Mid</th><th>Grade</th><th>Thesis</th></tr>' + open_play_rows + '</table>') if open_plays else '<p style="color:#555">No open positions.</p>'}
  </div>

  <div class="section">
    <div class="section-title">Options Play Log — Closed Plays</div>
    {('<table><tr><th>ID</th><th>Ticker</th><th>Type</th><th>Strike</th><th>Exit Date</th><th>P&L</th><th>Grade</th><th>Notes</th></tr>' + closed_play_rows + '</table>') if [p for p in plays if p.get("status")=="CLOSED"] else '<p style="color:#555">No closed plays yet.</p>'}
  </div>

  <div class="footer">
    <div class="footer-logo">BASANI</div>
    <div>Market Intelligence &nbsp;·&nbsp; Generated {datetime.now().strftime("%Y-%m-%d %H:%M ET")} &nbsp;·&nbsp; Powered by Alpaca Data</div>

    <div style="margin-top:28px;border:1px solid #1a1a1a;padding:22px 28px;text-align:left;max-width:820px;margin-left:auto;margin-right:auto">
      <div style="font-family:'Cinzel',serif;font-size:9px;letter-spacing:0.25em;color:#444;text-transform:uppercase;margin-bottom:12px">Legal Disclaimer</div>
      <p style="font-family:'EB Garamond',serif;font-size:12.5px;color:#3a3a3a;line-height:1.9">
        The information, analysis, and trade ideas presented in this report are provided solely for <strong style="color:#444">educational and informational purposes only</strong> and do not constitute financial advice, investment advice, trading advice, or any other type of advice. Nothing contained herein should be construed as a recommendation to buy, sell, or hold any security, options contract, or other financial instrument. BASANI Market Intelligence is not a registered investment adviser, broker-dealer, or financial planning firm, and no information in this report should be interpreted as such.
      </p>
      <p style="font-family:'EB Garamond',serif;font-size:12.5px;color:#3a3a3a;line-height:1.9;margin-top:10px">
        <strong style="color:#444">Trading involves substantial risk of loss.</strong> Options trading, in particular, carries a high degree of risk and is not suitable for all investors. You may lose the entire amount invested in any options position, and losses may exceed your initial investment in certain strategies. Past performance of any scan signal, score, or trade idea is not indicative of future results. All trade ideas, price targets, stop levels, and options plays presented herein are hypothetical and speculative in nature.
      </p>
      <p style="font-family:'EB Garamond',serif;font-size:12.5px;color:#3a3a3a;line-height:1.9;margin-top:10px">
        <strong style="color:#444">You are solely responsible for your own trading decisions and any financial outcomes that result.</strong> All capital at risk in any trade suggested or referenced in this report is entirely your own. You should conduct your own due diligence, consult a qualified financial professional, and carefully consider your investment objectives, risk tolerance, and financial situation before placing any trade. By using this report, you acknowledge and accept that all trading decisions and their consequences are yours alone.
      </p>
      <p style="font-family:'EB Garamond',serif;font-size:11px;color:#2a2a2a;line-height:1.7;margin-top:14px;letter-spacing:0.02em">
        Options premium estimates are approximations only and may differ materially from actual market prices. All data sourced from Alpaca Markets IEX feed. BASANI Market Intelligence makes no representations as to the accuracy, completeness, or timeliness of any information herein. &copy; BASANI Market Intelligence {datetime.now().year}. All rights reserved.
      </p>
    </div>
  </div>
</div>
</body>
</html>"""
    return html

# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Generating BASANI Daily Report...")

    scan         = load_latest_scan()
    history      = load_history(days_back=3)
    plays        = load_plays()
    tickers      = scan.get("tickers", [])

    grades, grade_stats    = grade_predictions(history)
    closed_plays, play_stats = grade_options_plays(plays)
    scalps, swings         = generate_options_plays(tickers)

    html = build_html(scan, grades, grade_stats, plays, play_stats, scalps, swings)

    today    = date.today().isoformat()
    out_file = os.path.join(REPORTS_DIR, f"daily_report_{today}.html")
    with open(out_file, "w") as f:
        f.write(html)

    # Also overwrite a fixed "latest" file for easy sharing
    latest_file = os.path.join(REPORTS_DIR, "daily_report_latest.html")
    with open(latest_file, "w") as f:
        f.write(html)

    print(f"✅ Report saved: reports/daily_report_{today}.html")
    print(f"✅ Latest link:  reports/daily_report_latest.html")

    # Auto-publish to GitHub Pages
    try:
        import publish_to_github
        publish_to_github.publish()
    except Exception as e:
        print(f"⚠  GitHub publish skipped: {e}")
