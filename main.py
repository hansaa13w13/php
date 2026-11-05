import os
import re
import json
import uuid
import asyncio
from datetime import datetime, timezone
from dateutil import parser
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
import websockets

app = FastAPI()

# --- SABİTLER ---
ONEFILE_DEFAULT_WS_URL = os.getenv('MERITBET_WS_URL', 'wss://eu-swarm-newm.meritbet884.com/')
ONEFILE_DEFAULT_ORIGIN = os.getenv('MERITBET_ORIGIN', 'https://www.meritbet884.com')
ONEFILE_REQUIRED_HANDICAP_LINE = 1.5
ONEFILE_MIN_HANDICAP_ODDS = 1.70

# --- YARDIMCI FONKSİYONLAR ---
def onefile_text(value) -> str:
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join([onefile_text(v) for v in value])
    return ""

def onefile_is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    text = onefile_text(value).strip().lower()
    return text in ['1', 'true', 'yes', 'live']

def onefile_is_placeholder_team(team: str) -> bool:
    return bool(re.match(r'^(?:\d+\.\s*Takım|Takım\s*\d+)(?:\s*\([^)]*\))?$', team.strip(), re.IGNORECASE))

def onefile_is_virtual_match(game: dict, home: str, away: str) -> bool:
    labels = [
        home, away,
        game.get('competition_name'),
        game.get('tournament_name'),
        game.get('league_name')
    ]
    for label in labels:
        text = onefile_text(label).strip()
        if not text:
            continue
        if re.search(r'(?:^|[^a-z0-9])(?:virtual|e[\s-]?football|e[\s-]?soccer|cyber[\s-]+(?:football|soccer))(?=$|[^a-z0-9])', text, re.IGNORECASE):
            return True
    return False

def onefile_numbers(value) -> list:
    if isinstance(value, (int, float)):
        return [float(value)]
    text = onefile_text(value).strip()
    if not text:
        return []
    try:
        return [float(text)]
    except ValueError:
        pass
    
    matches = re.findall(r'(?<![\w\d.])([+-])?\s*(\d+(?:[.,]\d+)?)(?![\d.,])', text)
    numbers = []
    for sign, num_str in matches:
        num = float(num_str.replace(',', '.'))
        numbers.append(-num if sign == '-' else num)
    return numbers

def onefile_find_handicap_value(candidates: list) -> float:
    for candidate in candidates:
        for number in onefile_numbers(candidate):
            if abs(number) > 0.000001:
                return number
    return None

def onefile_find_kickoff(game: dict) -> str:
    candidates = [
        game.get('start_ts'), game.get('start_time'), game.get('start_date'),
        game.get('date_start'), game.get('kickoff_time'), game.get('game_date')
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        val = str(candidate).strip()
        if not val or re.match(r'^\d{1,2}:\d{2}(?::\d{2})?$', val):
            continue
        try:
            if val.replace('.', '', 1).isdigit():
                ts = float(val)
                if len(str(int(ts))) == 13:
                    ts /= 1000
                elif len(str(int(ts))) == 8:
                    dt = datetime.strptime(str(int(ts)), "%Y%m%d").replace(tzinfo=timezone.utc)
                    return dt.isoformat()
                if 1000000000 <= ts <= 9999999999:
                    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            return parser.parse(val).replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            continue
    return None

def onefile_format_handicap(line: float) -> str:
    formatted = f"{abs(line):.2f}".rstrip('0').rstrip('.')
    return f"+{formatted}"

def onefile_is_binary_half_line(line: float) -> bool:
    fraction = abs(line) - int(abs(line))
    return abs(fraction - 0.5) < 0.000001

def onefile_normalize_result_side(value) -> str:
    side = onefile_text(value).strip().upper()
    if side in ['W1', 'HOME', '1']: return 'W1'
    if side in ['W2', 'AWAY', '2']: return 'W2'
    if side in ['X', 'DRAW']: return 'X'
    return side

def onefile_no_vig_probability(selected_odds, opposite_odds):
    try:
        s_odds, o_odds = float(selected_odds), float(opposite_odds)
        if s_odds <= 1.0 or o_odds <= 1.0:
            return None
        s_impl = 1.0 / s_odds
        o_impl = 1.0 / o_odds
        total = s_impl + o_impl
        return (s_impl / total) if total > 0 else None
    except (TypeError, ValueError):
        return None

def onefile_find_handicap_market_prices(game: dict, positive_line: float) -> dict:
    for market in game.get('market', []):
        if not isinstance(market, dict) or onefile_text(market.get('type')).strip().lower() != 'asianhandicap':
            continue
        pos_odds, neg_odds = None, None
        for event in market.get('event', []):
            if not isinstance(event, dict): continue
            line = onefile_find_handicap_value([event.get('base'), event.get('home_value'), event.get('away_value')])
            if line is None or abs(abs(line) - positive_line) >= 0.000001:
                continue
            if line > 0.0:
                pos_odds = event.get('price')
            elif line < 0.0:
                neg_odds = event.get('price')
        
        if pos_odds is not None and neg_odds is not None:
            return {'positiveOdds': float(pos_odds), 'negativeOdds': float(neg_odds)}
    return {}

def onefile_find_qualifying_upcoming_handicap(game: dict) -> dict:
    best = None
    for market in game.get('market', []):
        if not isinstance(market, dict) or onefile_text(market.get('type')).strip().lower() != 'asianhandicap':
            continue
        for event in market.get('event', []):
            if not isinstance(event, dict) or not event.get('price'): continue
            try:
                price = float(event['price'])
            except ValueError:
                continue
            
            if price < ONEFILE_MIN_HANDICAP_ODDS: continue
            
            line = onefile_find_handicap_value([event.get('base'), event.get('home_value'), event.get('away_value')])
            if line is None or line + 0.000001 < ONEFILE_REQUIRED_HANDICAP_LINE:
                continue
            
            side = onefile_text(event.get('type_1')).strip()
            team = onefile_text(event.get('name')).strip()
            if not team:
                norm_side = onefile_normalize_result_side(side)
                if norm_side == 'W1': team = onefile_text(game.get('team1_name')).strip()
                elif norm_side == 'W2': team = onefile_text(game.get('team2_name')).strip()
                else: team = side
            
            if not team: continue
            
            market_name = onefile_text(market.get('name', market.get('name_template', 'Asya handikapı'))).strip() or 'Asya handikapı'
            candidate = {
                'team': team, 'side': side, 'line': line, 'odds': price, 'market': market_name
            }
            if best is None or candidate['odds'] > best['odds']:
                best = candidate
    return best

def onefile_filter_games(games: dict) -> list:
    matches = []
    now = datetime.now(timezone.utc).timestamp()

    for game_id, game in games.items():
        if not isinstance(game, dict): continue
        if onefile_is_truthy(game.get('is_live')) or onefile_is_truthy(game.get('is_started')) or onefile_is_truthy(game.get('is_blocked')):
            continue

        kickoff_at = onefile_find_kickoff(game)
        if not kickoff_at: continue
        try:
            if datetime.fromisoformat(kickoff_at).timestamp() <= now:
                continue
        except Exception:
            continue

        home = onefile_text(game.get('team1_name')).strip()
        away = onefile_text(game.get('team2_name')).strip()
        if not home or not away or onefile_is_placeholder_team(home) or onefile_is_placeholder_team(away) or onefile_is_virtual_match(game, home, away):
            continue

        handicap = onefile_find_qualifying_upcoming_handicap(game)
        if not handicap: continue

        handicap_market = {
            'market': handicap['market'],
            'handicapPlus': onefile_format_handicap(handicap['line']),
            'selections': [{
                'selection': f"{handicap['team']} {onefile_format_handicap(handicap['line'])}",
                'side': handicap['side'],
                'odds': handicap['odds']
            }]
        }
        
        prices = onefile_find_handicap_market_prices(game, handicap['line'])
        handicap_market['oppositeOdds'] = prices.get('negativeOdds')
        
        if onefile_is_binary_half_line(handicap['line']) and prices:
            handicap_market['handicapProbability'] = onefile_no_vig_probability(handicap['odds'], prices.get('negativeOdds'))
        else:
            handicap_market['handicapProbability'] = None

        match_name = f"{home} - {away}".strip(' -')
        matches.append({
            'gameId': game.get('id', game_id),
            'match': match_name if match_name else str(game_id),
            'homeTeam': home,
            'awayTeam': away,
            'competition': onefile_text(game.get('competition_name', game.get('tournament_name', game.get('league_name')))).strip() or None,
            'isLive': False,
            'kickoffAt': kickoff_at,
            'markets': [handicap_market]
        })

    matches.sort(key=lambda x: (x['kickoffAt'], x['match']))
    return matches

# --- WEBSOCKET İSTEMCİSİ (API) ---
async def fetch_swarm_data():
    headers = {
        "Origin": ONEFILE_DEFAULT_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        async with websockets.connect(ONEFILE_DEFAULT_WS_URL, extra_headers=headers) as ws:
            # 1. Oturum İsteği
            session_rid = uuid.uuid4().hex[:16]
            session_req = {
                "command": "request_session",
                "rid": session_rid,
                "params": {
                    "site_id": int(os.getenv('MERITBET_SITE_ID', 1013)),
                    "language": os.getenv('MERITBET_LANGUAGE', 'tur'),
                    "source": 42,
                    "release_date": os.getenv('MERITBET_RELEASE_DATE', '09/23/2026-16:33')
                }
            }
            await ws.send(json.dumps(session_req))
            
            # Oturum cevabını bekle
            while True:
                resp = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(resp)
                if str(data.get("rid")) == session_rid:
                    break

            # 2. AsianHandicap Market İsteği
            data_rid = uuid.uuid4().hex[:16]
            data_req = {
                "command": "get",
                "rid": data_rid,
                "params": {
                    "source": "betting",
                    "subscribe": True,
                    "what": {
                        "game": ["id", "is_live", "is_blocked", "team1_name", "team2_name", "start_ts", "start_time", "start_date", "competition_name", "tournament_name", "league_name", "is_started", "text_info", "info"],
                        "market": ["id", "type", "name", "name_template", "base", "point_sequence", "market_type", "extra_info"],
                        "event": ["id", "type_1", "price", "name", "base", "home_value", "away_value", "type_id"]
                    },
                    "where": {
                        "sport": {"alias": "Soccer"},
                        "game": {"is_live": 0, "is_blocked": 0},
                        "market": {"type": "AsianHandicap"}
                    }
                }
            }
            await ws.send(json.dumps(data_req))
            
            # Veri cevabını bekle
            raw_games = {}
            while True:
                resp = await asyncio.wait_for(ws.recv(), timeout=15)
                data = json.loads(resp)
                if str(data.get("rid")) == data_rid:
                    # Unsubscribe
                    subid = data.get("data", {}).get("subid")
                    if subid:
                        await ws.send(json.dumps({"command": "unsubscribe", "params": {"subid": str(subid)}, "rid": uuid.uuid4().hex[:16]}))
                    
                    games_dict = data.get("data", {}).get("data", {}).get("game", {})
                    if isinstance(games_dict, dict):
                        raw_games = games_dict
                    break
                    
            matches = onefile_filter_games(raw_games)
            return {
                'connected': True,
                'fetchedAt': datetime.now(timezone.utc).isoformat(),
                'count': len(matches),
                'matches': matches,
                'error': None
            }
    except Exception as e:
        print(f"WS Error: {e}")
        return {
            'connected': False,
            'fetchedAt': datetime.now(timezone.utc).isoformat(),
            'count': 0,
            'matches': [],
            'error': 'Maç sağlayıcısına şu anda ulaşılamıyor. Bağlantıyı yenileyip tekrar deneyin.'
        }

# --- HTML TEMPLATE ---
HTML_CONTENT = """
<!doctype html>
<html lang="tr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#214c3d">
    <title>Futbol Maç Analiz Paneli</title>
    <style>
        :root {
            color-scheme: light;
            --ink: #20372f;
            --muted: #77817a;
            --green: #214c3d;
            --green-soft: #e7efe9;
            --paper: #f7f5ee;
            --line: #e4e0d4;
            --accent: #ae6651;
            --white: #fffefa;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            background: var(--paper);
            color: var(--ink);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        header {
            min-height: 68px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 12px max(24px, calc((100% - 1240px) / 2));
            background: var(--green);
            color: #fffefa;
        }
        .brand { display: flex; align-items: center; gap: 12px; font-weight: 750; }
        .brand-mark {
            display: grid;
            width: 40px;
            height: 40px;
            place-items: center;
            border: 1px solid #ffffff7a;
            border-radius: 50%;
            font-size: 12px;
        }
        .brand small { display: block; margin-top: 3px; color: #c7d5cc; font-size: 9px; letter-spacing: .18em; }
        .header-note { color: #d4ddd7; font-size: 12px; }
        main { max-width: 1240px; margin: 0 auto; padding: 42px 24px 60px; }
        .intro { display: flex; align-items: end; justify-content: space-between; gap: 28px; margin-bottom: 28px; }
        .eyebrow { margin: 0 0 12px; color: var(--accent); font-size: 11px; font-weight: 750; letter-spacing: .2em; text-transform: uppercase; }
        h1 { max-width: 540px; margin: 0; font-size: clamp(34px, 5vw, 52px); line-height: 1.03; letter-spacing: -.055em; }
        .description { max-width: 600px; margin: 14px 0 0; color: var(--muted); font-size: 14px; line-height: 1.65; }
        .actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: end; gap: 10px; }
        .status {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 10px 13px;
            border: 1px solid var(--line);
            border-radius: 999px;
            background: #fffefa8c;
            color: var(--muted);
            font-size: 12px;
            white-space: nowrap;
        }
        .dot { width: 8px; height: 8px; border-radius: 50%; background: #b0aaa0; }
        .status[data-connected="true"] .dot { background: #4d9a70; box-shadow: 0 0 0 3px #4d9a7021; }
        .status[data-connected="false"] .dot { background: #ba5b4b; }
        button, .download {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 42px;
            padding: 0 16px;
            border: 1px solid var(--green);
            border-radius: 9px;
            background: var(--green);
            color: #fffefa;
            font: inherit;
            font-size: 12px;
            font-weight: 700;
            text-decoration: none;
            cursor: pointer;
        }
        button:disabled { cursor: wait; opacity: .65; }
        .download { border-color: var(--line); background: var(--white); color: var(--green); }
        .summary {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            margin-bottom: 20px;
            border: 1px solid var(--line);
            border-radius: 12px;
            background: #fffefa72;
            overflow: hidden;
        }
        .summary-item { min-width: 0; min-height: 76px; padding: 16px 20px; }
        .summary-item + .summary-item { border-left: 1px solid var(--line); }
        .summary-label { color: var(--muted); font-size: 9px; font-weight: 750; letter-spacing: .16em; text-transform: uppercase; }
        .summary-value { display: block; margin-top: 7px; font-size: 17px; font-weight: 750; }
        .odds-product-table { width: 100%; margin-top: 5px; border-collapse: collapse; table-layout: fixed; font-size: 9px; line-height: 1.45; }
        .odds-product-table th { width: 48px; padding: 3px 5px 0 0; color: var(--muted); text-align: left; vertical-align: top; font-weight: 600; }
        .odds-product-table td { padding-top: 3px; overflow-wrap: anywhere; vertical-align: top; font-variant-numeric: tabular-nums; }
        .product-total { color: var(--green); font-size: 13px; font-weight: 800; }
        .summary-detail { display: block; margin-top: 5px; color: var(--muted); font-size: 10px; line-height: 1.5; }
        .board { border: 1px solid var(--line); border-radius: 12px; background: var(--white); overflow: hidden; }
        .board-title {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            min-height: 58px;
            padding: 14px 20px;
            border-bottom: 1px solid var(--line);
            font-size: 14px;
            font-weight: 750;
        }
        .board-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: end; gap: 8px; }
        .filter-control { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 10px; font-weight: 600; }
        .column-head, .match-row {
            display: grid;
            grid-template-columns: minmax(220px, 1.2fr) minmax(150px, .7fr) minmax(260px, 1.4fr) minmax(90px, .45fr);
            align-items: center;
            gap: 16px;
        }
        .column-head { min-height: 38px; padding: 0 20px; background: #f8f6f0; color: #8d938b; font-size: 9px; font-weight: 700; letter-spacing: .11em; text-transform: uppercase; }
        .match-row { min-height: 92px; padding: 14px 20px; border-top: 1px solid #efede5; }
        .select-control { display: inline-flex; align-items: center; gap: 7px; margin-bottom: 7px; color: var(--muted); font-size: 9px; cursor: pointer; }
        .select-control input { width: 14px; height: 14px; accent-color: var(--green); }
        .team-pair { font-size: 12px; font-weight: 730; line-height: 1.7; }
        .competition { margin-top: 3px; color: var(--muted); font-size: 10px; }
        .kickoff { color: var(--ink); font-size: 11px; font-weight: 700; }
        .kickoff small { display: block; margin-top: 4px; color: var(--muted); font-size: 10px; font-weight: 450; }
        .market-pill { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 9px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted); font-size: 10px; }
        .market-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 7px; }
        .best-option { border-color: #c9dfd0; background: #f4faf5; color: var(--green); }
        .best-option strong { color: var(--green); font-size: 12px; }
        .handicap { color: var(--green); font-size: 12px; font-weight: 800; }
        .side { padding-left: 8px; border-left: 1px solid var(--line); }
        .odds { color: var(--accent); font-size: 12px; font-weight: 800; }
        .game-id { color: #999d96; text-align: right; font-size: 10px; }
        .empty, .error { padding: 42px 20px; color: var(--muted); text-align: center; font-size: 13px; }
        .error { display: none; margin-bottom: 16px; border: 1px solid #e6c6bd; border-radius: 10px; background: #fff6f2; color: #9c4b3b; text-align: left; }
        footer { margin-top: 16px; color: var(--muted); text-align: right; font-size: 10px; }
        @media (max-width: 760px) {
            header { padding: 12px 18px; }
            .header-note { display: none; }
            main { padding: 30px 16px 44px; }
            .intro { align-items: start; flex-direction: column; }
            .actions { justify-content: start; }
            .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .summary-item { padding: 14px 12px; }
            .summary-item:nth-child(3) { border-top: 1px solid var(--line); border-left: 0; }
            .column-head { display: none; }
            .match-row { grid-template-columns: minmax(0, 1fr) auto; gap: 10px; padding: 16px; }
            .team-cell { grid-column: 1; }
            .kickoff { grid-column: 2; grid-row: 1; text-align: right; }
            .market-cell { grid-column: 1 / -1; }
            .game-id { display: none; }
            .board-title { align-items: flex-start; flex-direction: column; }
        }
    </style>
</head>
<body>
    <header>
        <div class="brand">
            <span class="brand-mark">PA</span>
            <span>Futbol Maç Analizi<small>MAÇ ÖNCESİ ANALİZ</small></span>
        </div>
        <div class="header-note">Yalnızca başlamamış karşılaşmalar</div>
    </header>

    <main>
        <section class="intro">
            <div>
                <p class="eyebrow">Maç öncesi pazar karşılaştırması</p>
                <h1>Yaklaşan futbol karşılaşmaları</h1>
                <p class="description">Başlama zamanı gelmemiş, +1.5 ve üzeri Asya handikapı oranı en az 1,70 olan futbol maçlarını inceleyin.</p>
            </div>
            <div class="actions">
                <span class="status" id="feed-status" data-connected="idle"><span class="dot"></span><span id="feed-status-text">Veri için Yenile'ye basın</span></span>
                <button id="refresh-button" type="button">Yenile</button>
                <a class="download" href="/?download=1" download="main.py">main.py indir</a>
            </div>
        </section>

        <section class="summary" aria-label="Akış durumu">
            <div class="summary-item"><span class="summary-label">Uygun karşılaşma</span><strong class="summary-value" id="match-count">— maç</strong></div>
            <div class="summary-item">
                <span class="summary-label">Elle seçilen kupon</span>
                <strong class="summary-value" id="selected-count">0 seçim</strong>
                <table class="odds-product-table" aria-label="Seçilen maçların yaklaşık oran hesabı">
                    <tbody>
                        <tr><th>Oran</th><td id="coupon-odds">—</td></tr>
                        <tr><th>Gösterge</th><td class="product-total" id="coupon-probability">—</td></tr>
                    </tbody>
                </table>
                <small class="summary-detail" id="coupon-warning">Kupon için maçların yanındaki kutuları elle seçin.</small>
            </div>
            <div class="summary-item">
                <span class="summary-label">Veri akışı</span>
                <strong class="summary-value" id="connection-value">Yenile bekleniyor</strong>
                <small class="summary-detail">Son güncelleme: <span id="updated-at">—</span></small>
            </div>
        </section>

        <div class="error" id="feed-error" role="status"></div>

        <section class="board" aria-label="Futbol pazar analizi">
            <div class="board-title">
                <span>Yaklaşan karşılaşmalar <span id="visible-match-count" style="color:#9a9b92;font-size:10px;font-weight:450">· yenile bekleniyor</span></span>
                <div class="board-actions"><span class="filter-control">+1,5 ve üzeri Asya handikapı · oran ≥ 1,70</span></div>
            </div>
            <div class="column-head"><span>Karşılaşma / kupon seçimi</span><span>Başlama zamanı</span><span>Asya handikapı</span><span style="text-align:right">Karşılaşma no</span></div>
            <div id="match-list"><div class="empty">Maç verisini almak için Yenile düğmesine basın.</div></div>
        </section>
        <footer>Veriler yalnızca Yenile düğmesine basıldığında alınır. Bu ekran bahis yapmaz; hesaplar geçmiş sonuçlarla doğrulanmış başarı tahmini değildir.</footer>
    </main>

    <script>
        const matchList = document.getElementById('match-list');
        const countValue = document.getElementById('match-count');
        const selectedCountValue = document.getElementById('selected-count');
        const couponOdds = document.getElementById('coupon-odds');
        const couponProbability = document.getElementById('coupon-probability');
        const couponWarning = document.getElementById('coupon-warning');
        const connectionValue = document.getElementById('connection-value');
        const updatedAt = document.getElementById('updated-at');
        const status = document.getElementById('feed-status');
        const statusText = document.getElementById('feed-status-text');
        const errorBox = document.getElementById('feed-error');
        const refreshButton = document.getElementById('refresh-button');
        const visibleMatchCount = document.getElementById('visible-match-count');
        let requestInProgress = false;
        let dataRequestPending = false;
        let currentMatches = [];
        const selectedGameIds = new Set();

        function makeNode(tag, className, text) {
            const node = document.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined && text !== null && text !== '') node.textContent = String(text);
            return node;
        }

        function formatKickoff(value) {
            if (!value) return ['Saat bilgisi yok', ''];
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return ['Saat bilgisi yok', ''];
            return [
                new Intl.DateTimeFormat('tr-TR', { day: '2-digit', month: 'short', weekday: 'short' }).format(date),
                new Intl.DateTimeFormat('tr-TR', { hour: '2-digit', minute: '2-digit' }).format(date) + ' yerel saat'
            ];
        }

        function formatPercent(value) {
            return value !== null && value !== undefined && value !== ''
                && Number.isFinite(Number(value))
                ? (Number(value) * 100).toLocaleString('tr-TR', { maximumFractionDigits: 1 }) + '%'
                : '—';
        }

        function formatOdds(value) {
            return Number.isFinite(Number(value))
                ? Number(value).toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                : '—';
        }

        function getCouponOption(match) {
            const market = match.markets?.[0];
            const selection = market?.selections?.[0];
            if (!selection || !Number.isFinite(Number(selection.odds)) || Number(selection.odds) <= 1) {
                return null;
            }
            return {
                selection: selection.selection,
                odds: Number(selection.odds),
                probability: market.handicapProbability
            };
        }

        function renderMatches(matches, emptyMessage) {
            matchList.replaceChildren();
            if (!matches.length) {
                matchList.append(makeNode('div', 'empty', emptyMessage || 'Uygun yaklaşan maç bulunamadı. +1.5 ve üzeri Asya handikapı oranı 1,70 veya üzeri olan maçlar burada görünür.'));
                return;
            }

            for (const match of matches) {
                const row = makeNode('article', 'match-row');
                const teamCell = makeNode('div', 'team-cell');
                const selectLabel = makeNode('label', 'select-control');
                const checkbox = document.createElement('input');
                const gameId = String(match.gameId);
                const couponOption = getCouponOption(match);
                checkbox.type = 'checkbox';
                checkbox.disabled = !couponOption;
                checkbox.checked = selectedGameIds.has(gameId);
                if (!couponOption) selectedGameIds.delete(gameId);
                checkbox.addEventListener('change', () => {
                    if (checkbox.checked) selectedGameIds.add(gameId);
                    else selectedGameIds.delete(gameId);
                    updateCouponAnalysis(currentMatches);
                });
                selectLabel.append(checkbox, makeNode('span', '', couponOption ? 'Kupona ekle' : 'Oran yok'));
                teamCell.append(selectLabel);
                const teamPair = makeNode('div', 'team-pair');
                teamPair.append(makeNode('div', '', match.homeTeam));
                teamPair.append(makeNode('div', '', match.awayTeam));
                teamCell.append(teamPair);
                if (match.competition) teamCell.append(makeNode('div', 'competition', match.competition));

                const [dateText, timeText] = formatKickoff(match.kickoffAt);
                const kickoff = makeNode('div', 'kickoff');
                kickoff.append(makeNode('strong', '', 'Başlama zamanı'));
                kickoff.append(makeNode('small', '', dateText && timeText ? dateText + ' · ' + timeText : 'Başlangıç saati yok'));

                const marketCell = makeNode('div', 'market-cell');
                const handicapMarket = match.markets?.[0];
                const handicapSelection = handicapMarket?.selections?.[0];
                if (handicapSelection) {
                    const handicapPill = makeNode('div', 'market-pill best-option');
                    handicapPill.append(
                        makeNode('span', '', 'Asya handikapı'),
                        makeNode('strong', 'handicap', handicapSelection.selection || '+1.5'),
                        makeNode('span', 'side', 'Oran'),
                        makeNode('strong', 'odds', formatOdds(handicapSelection.odds))
                    );
                    marketCell.append(handicapPill);
                }
                row.append(teamCell, kickoff, marketCell, makeNode('div', 'game-id', match.gameId));
                matchList.append(row);
            }
        }

        function updateCouponAnalysis(matches) {
            const selected = matches
                .filter((match) => selectedGameIds.has(String(match.gameId)))
                .map((match) => ({ match, option: getCouponOption(match) }))
                .filter((item) => item.option);
            selectedCountValue.textContent = selected.length + ' seçim';
            if (!selected.length) {
                couponOdds.textContent = '—';
                couponProbability.textContent = '—';
                couponWarning.textContent = selectedGameIds.size && matches.length
                    ? 'Seçilen maçlarda oran bulunamadı.'
                    : 'Kupon için maçların yanındaki kutuları elle seçin.';
                return;
            }

            const odds = selected.map(({ option }) => Number(option.odds));
            const validOdds = odds.every((value) => Number.isFinite(value) && value > 1);
            const product = validOdds ? odds.reduce((total, value) => total * value, 1) : null;
            couponOdds.textContent = product === null
                ? 'Oran bilgisi eksik'
                : product.toLocaleString('tr-TR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

            const probabilities = selected.map(({ option }) => option.probability);
            const validProbabilities = probabilities.every((value) =>
                Number.isFinite(Number(value)) && Number(value) > 0 && Number(value) < 1
            );
            if (validProbabilities) {
                const combined = probabilities.reduce((total, value) => total * Number(value), 1);
                couponProbability.textContent = formatPercent(combined);
            } else {
                couponProbability.textContent = 'Hesaplanmıyor';
            }

            if (selected.length >= 2) {
                couponWarning.textContent = 'Birleşik gösterge, maç olasılıklarının bağımsız olduğu varsayımına dayanır; garanti değildir.';
            } else if (!validProbabilities) {
                couponWarning.textContent = 'Karşı oran eksik olduğundan gösterge hesaplanamıyor.';
            } else {
                couponWarning.textContent = 'Oran ve piyasa göstergesi tahminidir; garanti değildir.';
            }
        }

        function renderMatchList() {
            const sorted = [...currentMatches].sort((left, right) => {
                const leftKickoff = Date.parse(left.kickoffAt || '');
                const rightKickoff = Date.parse(right.kickoffAt || '');
                const leftHasKickoff = Number.isFinite(leftKickoff);
                const rightHasKickoff = Number.isFinite(rightKickoff);
                if (leftHasKickoff !== rightHasKickoff) return leftHasKickoff ? -1 : 1;
                if (leftHasKickoff && leftKickoff !== rightKickoff) {
                    return leftKickoff - rightKickoff;
                }
                return String(left.match || '').localeCompare(String(right.match || ''));
            });
            visibleMatchCount.textContent = '· ' + sorted.length + ' maç';
            renderMatches(sorted, dataRequestPending ? 'Maç verisi yenileniyor…' : 'Bu koşullarda yaklaşan maç bulunamadı.');
        }

        async function loadMatches() {
            if (requestInProgress) return;
            requestInProgress = true;
            dataRequestPending = true;
            refreshButton.disabled = true;
            errorBox.style.display = 'none';
            status.dataset.connected = 'pending';
            statusText.textContent = 'Veri yenileniyor';
            currentMatches = [];
            countValue.textContent = '0 maç';
            updatedAt.textContent = '—';
            connectionValue.textContent = 'Kontrol ediliyor';
            updateCouponAnalysis(currentMatches);
            renderMatchList();

            try {
                const response = await fetch('/?data=1&_=' + Date.now(), { cache: 'no-store' });
                const snapshot = await response.json();
                dataRequestPending = false;
                currentMatches = snapshot.connected && Array.isArray(snapshot.matches) ? snapshot.matches : [];
                const activeIds = new Set(currentMatches.map((match) => String(match.gameId)));
                for (const selectedId of selectedGameIds) {
                    if (!activeIds.has(selectedId)) selectedGameIds.delete(selectedId);
                }
                countValue.textContent = snapshot.count + ' maç';
                updatedAt.textContent = new Intl.DateTimeFormat('tr-TR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(snapshot.fetchedAt));
                connectionValue.textContent = snapshot.connected ? 'Bağlı' : 'Bağlantı yok';
                status.dataset.connected = snapshot.connected ? 'true' : 'false';
                statusText.textContent = snapshot.connected ? 'Veri akışı bağlı' : 'Veri akışı bağlantısı yok';
                if (snapshot.error) {
                    errorBox.textContent = snapshot.error;
                    errorBox.style.display = 'block';
                } else if (!snapshot.connected) {
                    errorBox.textContent = 'Maç sağlayıcısına bağlanılamadı; güncel maç verisi gösterilmiyor.';
                    errorBox.style.display = 'block';
                }
                updateCouponAnalysis(currentMatches);
                renderMatchList();
            } catch (error) {
                dataRequestPending = false;
                status.dataset.connected = 'false';
                statusText.textContent = 'Veri akışı bağlantısı yok';
                connectionValue.textContent = 'Bağlantı yok';
                errorBox.textContent = 'Uygulama sunucusuna ulaşılamadı. Sayfayı yenileyin veya biraz sonra tekrar deneyin.';
                errorBox.style.display = 'block';
                currentMatches = [];
                countValue.textContent = '0 maç';
                updateCouponAnalysis(currentMatches);
                renderMatchList();
            } finally {
                dataRequestPending = false;
                requestInProgress = false;
                refreshButton.disabled = false;
            }
        }

        refreshButton.addEventListener('click', loadMatches);
    </script>
</body>
</html>
"""

# --- ROUTER ---
@app.get("/")
async def root(request: Request):
    query_params = request.query_params
    if query_params.get("download"):
        return FileResponse(__file__, media_type='application/octet-stream', filename="main.py")
    
    if query_params.get("data"):
        snapshot = await fetch_swarm_data()
        return JSONResponse(content=snapshot, status_code=200 if snapshot['connected'] else 503)

    return HTMLResponse(content=HTML_CONTENT)
