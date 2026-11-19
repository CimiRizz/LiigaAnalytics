"""KalPan runkosarjan otteluanalytiikka. Vaatii Python 3.10:n tai uudemman."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import webbrowser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API = "https://liiga.fi/api/v2/"
FORWARD_ROLES = {"CENTER", "LEFT_WING", "RIGHT_WING", "STRIKER", "THIRTEENTH_STRIKER",
                 "FOURTEENTH_STRIKER", "FORWARD"}
FORWARD_CODES = {"KH", "VL", "OL", "H", "13. H", "14. H"}


def helsinki(start):
    utc = datetime.fromisoformat(start.replace("Z", "+00:00"))
    try:
        return utc.astimezone(ZoneInfo("Europe/Helsinki"))
    except ZoneInfoNotFoundError:
        # Windows voi olla ilman IANA-aikavyöhykkeitä. Käytä Suomen EU-kesäaikaa.
        def last_sunday(month):
            end = datetime(utc.year, month, 31, 1, tzinfo=timezone.utc)
            return end - timedelta(days=(end.weekday() + 1) % 7)
        hours = 3 if last_sunday(3) <= utc < last_sunday(10) else 2
        return utc.astimezone(timezone(timedelta(hours=hours)))


def atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(contents)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Liiga:
    def __init__(self, cache):
        self.cache = cache
        self.last_request = 0.0

    def get(self, endpoint, filename, valid, *, fresh=False):
        path = self.cache / filename
        cached = None
        try:
            candidate = json.loads(path.read_bytes())
            if valid(candidate):
                cached = candidate
        except (OSError, ValueError):
            pass
        if cached is not None and not fresh:
            return cached

        for attempt in range(2):
            time.sleep(max(0, 1.0 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                request = Request(API + endpoint, headers={"User-Agent": "KalPa Analytics/1.0",
                                                           "Accept": "application/json"})
                with urlopen(request, timeout=12) as response:
                    raw = response.read()
                data = json.loads(raw)
                if not valid(data):
                    raise ValueError("Liiga palautti puuttuvia tai odottamattomia tietoja")
                atomic_write(path, raw)
                return data
            except (OSError, URLError, ValueError) as exc:
                if attempt == 0 and getattr(exc, "code", None) not in (401, 403, 404, 429):
                    continue
                message = f"{endpoint}: {exc}"
                if cached is not None:
                    print(f"{message}. Käytetään aiemmin tallennettuja tietoja.", file=sys.stderr)
                    return cached
                raise RuntimeError(message) from exc


def from_schedule(game):
    side = "home" if str(game.get("homeTeamId")).rsplit(":", 1)[-1] == "kalpa" else "away"
    other = "away" if side == "home" else "home"
    winner = game.get("homeTeamWinner")
    won = winner == (side == "home") if isinstance(winner, bool) else (
        game[side + "TeamGoals"] > game[other + "TeamGoals"])
    return {"id": game["id"], "season": game["season"], "date": helsinki(game["start"]).date(),
            "side": side, "opponent": game[other + "TeamName"], "won": won,
            "score": f'{game[side + "TeamGoals"]}–{game[other + "TeamGoals"]}',
            "ending": {"ENDED_DURING_EXTENDED_GAME_TIME": " JA",
                       "ENDED_DURING_WINNING_SHOT_COMPETITION": " VL"}.get(game.get("finishedType"), ""),
            "players": [], "possession": None, "error": None}


def possession(stats, side):
    periods = [p for p in (stats.get("puckStats") or []) if 1 <= p.get("periodNumber", 0) <= 4]
    keys = ("homeTeamControlDuration", "awayTeamControlDuration")
    measured = [p for p in periods if all(isinstance(p.get(k), (int, float)) for k in keys)]
    if len(measured) != len(periods) or {1, 2, 3} - {p["periodNumber"] for p in measured}:
        return None
    home, away = (sum(p[k] for p in measured) for k in keys)
    if home + away <= 0:
        return None
    return 100 * (home if side == "home" else away) / (home + away)


def match_players(lineup, stats, side):
    by_player = defaultdict(list)
    for period in stats[side + "Team"]:
        if not 1 <= period.get("period", 0) <= 4:
            continue
        for row in period.get("periodPlayerStats") or []:
            by_player[row["playerId"]].append(row)

    players = []
    for player in lineup[side + "TeamPlayers"]:
        if player.get("role") not in FORWARD_ROLES and player.get("roleCode") not in FORWARD_CODES:
            continue
        rows = by_player[player["id"]]
        ice = sum(row["period"].get("timeofice") or 0 for row in rows)
        if not ice and not player.get("line"):
            continue
        distances = [row.get("distance") for row in rows if
                     (row["period"].get("timeofice") or 0) > 0 or (row.get("distance") or 0) > 0]
        # Liigan ottelutaulukko näyttää kokonaismatkan lähimpään metriin pyöristettynä.
        distance = (math.floor(sum(distances) + 0.5)
                    if ice and distances and all(isinstance(d, (int, float)) and d >= 0
                                                 for d in distances) and sum(distances) > 0 else None)
        stats_rows = [row["period"] for row in rows]
        result = dict(id=player["id"], jersey=player.get("jersey"), name=player["lastName"],
                      ice=ice, distance=distance)
        for key in ("plusminus", "goals", "assists", "shots"):
            values = [row.get(key) for row in stats_rows]
            result[key] = sum(v or 0 for v in values) if all(v is not None for v in values) else None
        players.append(result)
    return players


def collect(client, season, refresh):
    schedule = client.get(
        f"schedule?season={season}&tournament=runkosarja", "schedule.json",
        lambda data: isinstance(data, list) and bool(data) and all(
            isinstance(row, dict) and "id" in row and "ended" in row for row in data), fresh=True)
    finished = {g["id"]: g for g in schedule if g["ended"] and
                any(str(g.get(side + "TeamId")).rsplit(":", 1)[-1] == "kalpa" for side in ("home", "away"))}
    games = []
    for row in sorted(finished.values(), key=lambda g: g["start"]):
        game = from_schedule(row)
        game_id, side = game["id"], game["side"]
        print(f'{game["date"]} {game["opponent"]}', flush=True)
        try:
            lineup = client.get(f"games/{season}/{game_id}", f"{game_id}-lineup.json",
                               lambda o: isinstance(o, dict) and
                               isinstance(o.get(side + "TeamPlayers"), list) and
                               bool(o[side + "TeamPlayers"]), fresh=refresh)
            stats = client.get(f"games/stats/{season}/{game_id}", f"{game_id}-stats.json",
                               lambda o: isinstance(o, dict) and isinstance(o.get(side + "Team"), list)
                               and bool(o[side + "Team"]), fresh=refresh)
            game["players"] = match_players(lineup, stats, side)
            game["possession"] = possession(stats, side)
            if not any(p["ice"] for p in game["players"]):
                raise ValueError("Pelaajien peliaikoja ei löytynyt")
        except (RuntimeError, KeyError, ValueError, TypeError) as exc:
            print(f"{game_id}: {exc}", file=sys.stderr)
            game["error"] = "Ottelun pelaajatilastot jäivät puutteellisiksi."
        if games:
            days = (game["date"] - games[-1]["date"]).days
            game["back_to_back"] = days == 1
        games.append(game)
    return games[::-1]


def aggregate(games):
    by_player = defaultdict(list)
    for game in reversed(games):
        for row in game["players"]:
            if row["ice"]:
                by_player[row["id"]].append(row)

    result = []
    for rows in by_player.values():
        tracked = [r for r in rows if r["distance"] is not None]
        stats = {key: (sum(r[key] for r in rows) if all(r[key] is not None for r in rows) else None)
                 for key in ("plusminus", "goals", "assists", "shots")}
        result.append(dict(id=rows[-1]["id"], jersey=rows[-1]["jersey"], name=rows[-1]["name"],
                           games=len(rows), tracked_games=len(tracked), ice=sum(r["ice"] for r in rows),
                           tracked_ice=sum(r["ice"] for r in tracked),
                           distance=sum(r["distance"] for r in tracked) if tracked else None, **stats))
    return result


def team_rate(games):
    rows = [r for g in games for r in g["players"] if r["ice"] and r["distance"] is not None]
    return rate(sum(r["distance"] for r in rows), sum(r["ice"] for r in rows))


def rate(distance, ice):
    return 60 * distance / ice if distance is not None and ice else None


def fmt(value, decimals=2):
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}".replace(",", "\u202f").replace(".", ",").replace("-", "−")


def signed(value, decimals=0):
    return ("+" if value is not None and value > 0 else "") + fmt(value, decimals)


def clock(seconds):
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}:{remainder:02d}"


def table(headers, rows):
    if not rows:
        return '<p class="note">Raportoitavia tietoja ei löytynyt.</p>'
    headings = "".join(f'<th scope="col">{escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap" tabindex="0"><table><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>'


def player_table(players, *, total=False):
    def measure(p):
        return rate(p["distance"], p.get("tracked_ice", p["ice"]))

    ranked = sorted(players, key=lambda p: (measure(p) is None, -(measure(p) or 0), p["name"]))
    rows = []
    for i, p in enumerate(ranked, 1):
        name = escape(p["name"])
        jersey = f'#{escape(str(p["jersey"]))} ' if p["jersey"] is not None else ""
        coverage = (p["games"], p["tracked_games"]) if total else ()
        counts = (str(coverage[0]), str(coverage[1])) if total else ()
        rows.append((i if measure(p) is not None else "—", jersey + name, *counts,
                     clock(p["ice"]), fmt(p["distance"], 0), f'<strong>{fmt(measure(p))}</strong>',
                     signed(p["plusminus"]), fmt(p["goals"], 0), fmt(p["assists"], 0),
                     fmt(p["shots"], 0)))
    headers = ("Sija", "Hyökkääjä", *(("Ottelut", "Matka mitattu") if total else ()),
               "Peliaika", "Matka (m)", "m/min", "+/−", "M", "S", "L")
    return table(headers, rows)


def comparison(games):
    wins = [g for g in games if g["won"]]
    losses = [g for g in games if not g["won"]]
    rows = []
    values = []
    for label, group in (("Voitot", wins), ("Tappiot", losses)):
        value = team_rate(group)
        values.append(value)
        tracked = sum(team_rate([g]) is not None for g in group)
        puck = [g["possession"] for g in group if g["possession"] is not None]
        rows.append((label, len(group), tracked, fmt(value),
                     fmt(sum(puck) / len(puck), 1) + " %" if puck else "—", len(puck)))
    box = table(("Tulos", "Ottelut", "Matka mitattu", "m/min", "Kiekonhallinta", "Hallinta mitattu"), rows)
    if all(value is not None for value in values):
        difference = values[0] - values[1]
        relative = f" ({signed(100 * difference / values[1], 1)} %)" if values[1] else ""
        box += (f'<p class="note">Ero (voitot − tappiot) '
                f'<strong>{signed(difference, 2)} m/min{relative}</strong>.</p>')
    return box


def player_comparison(games):
    groups = [{p["id"]: p for p in aggregate([g for g in games if g["won"] == won])}
              for won in (True, False)]
    names = {p["id"]: p["name"] for p in aggregate(games)}
    rows = []
    for player_id, name in sorted(names.items(), key=lambda item: item[1]):
        win, loss = (group.get(player_id) for group in groups)
        measures = [rate(p["distance"], p["tracked_ice"]) if p else None for p in (win, loss)]
        difference = measures[0] - measures[1] if None not in measures else None
        rows.append((escape(name), win["games"] if win else 0, fmt(measures[0]),
                     signed(win["plusminus"]) if win else "—",
                     loss["games"] if loss else 0, fmt(measures[1]),
                     signed(loss["plusminus"]) if loss else "—", signed(difference, 2)))
    return table(("Hyökkääjä", "Voittoja", "Voitot m/min", "Voitot +/−",
                  "Tappioita", "Tappiot m/min", "Tappiot +/−", "Ero m/min"), rows)


def label(game):
    return f'{game["date"]:%d.%m.%Y} · {game["opponent"]}'


def game_list(games):
    rows = []
    for g in games:
        puck = fmt(g["possession"], 0) + " %" if g["possession"] is not None else "—"
        rows.append((f'<a href="#ottelu-{g["id"]}">{escape(label(g))}</a>',
                     "Koti" if g["side"] == "home" else "Vieras", g["score"] + g["ending"],
                     "Voitto" if g["won"] else "Tappio", fmt(team_rate([g])), puck))
    return table(("Ottelu", "Paikka", "KalPa–vastustaja", "Tulos", "m/min", "Kiekonhallinta"), rows)


def match_report(game, expanded):
    rows = [p for p in game["players"] if p["ice"]]
    note = (f'<p class="note">{escape(game["error"])}</p>' if game["error"] else "")
    missing = sum(p["distance"] is None for p in rows)
    if missing:
        note += f'<p class="note">Luistelumatka puuttuu {missing} pelaajalta.</p>'
    won = "Voitto" if game["won"] else "Tappio"
    puck = game["possession"]
    puck_display = f'{fmt(puck, 0)} % – {fmt(100 - puck, 0)} %' if puck is not None else "—"
    streak = " · Ottelu myös edellisenä päivänä" if game.get("back_to_back") else ""
    source = f'https://liiga.fi/fi/peli/{game["season"]}/{game["id"]}'
    return (f'<details id="ottelu-{game["id"]}"{" open" if expanded else ""}>'
            f'<summary><span>{escape(label(game))}</span> <strong class="{"win" if game["won"] else "loss"}">'
            f'{game["score"]}{game["ending"]} · {won}</strong></summary>'
            f'<div class="match"><p>{("Kotiottelu" if game["side"] == "home" else "Vierasottelu")}{streak}'
            f' · <a href="{source}">Liigan ottelutilastot</a></p>'
            f'<p><strong>{fmt(team_rate([game]))} m/min</strong> · Kiekonhallinta KalPa–vastustaja '
            f'<strong>{puck_display}</strong></p>{note}{player_table(game["players"])}</div></details>')


CSS = """
:root{color-scheme:light;--ink:#1e2329;--muted:#515a64;--line:#dbe0e6;--paper:#fff;--sun:#ffd64d}
*{box-sizing:border-box}body{margin:0;background:#f3f5f7;color:var(--ink);font:16px/1.55 Inter,system-ui,sans-serif}
main{max-width:1160px;margin:auto;padding:clamp(14px,4vw,48px)}header{background:#151819;color:#fff;border-bottom:6px solid var(--sun);padding:clamp(22px,5vw,48px);border-radius:16px}
h1{margin:0;font-size:clamp(32px,5vw,62px);line-height:1.08;letter-spacing:-.04em}header p{margin:.75em 0 0;color:#e4e7ea}header a{color:var(--sun)}
nav{display:flex;gap:8px;margin:20px 0;flex-wrap:wrap}nav a{text-decoration:none;color:var(--ink);padding:9px 15px;border:1px solid var(--line);background:var(--paper);border-radius:8px;font-weight:700}nav a[aria-current=page]{background:var(--sun);border-color:#d2a917}
section{padding:clamp(14px,3vw,28px);border:1px solid var(--line);background:var(--paper);border-radius:14px;margin-bottom:16px}
h2{margin-top:0;letter-spacing:-.02em}h3{margin:1.4em 0 .45em}details{border:1px solid var(--line);border-radius:8px;margin:10px 0}
summary{padding:10px 12px;cursor:pointer}summary strong{float:right;margin-left:10px}.match{padding:0 12px 12px}.match p{margin:.65em 0}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:.92rem}
th,td{text-align:right;padding:8px 10px;white-space:nowrap}th{background:#f0f2f4;font-size:.84rem}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}tbody tr:nth-child(even){background:#f8f9fa}
td{border-top:1px solid #edf0f2}.note,footer{color:var(--muted);font-size:.9rem}a{color:#22476c}.win{color:#156049}.loss{color:#a02e32}
:focus-visible{outline:3px solid #496e94;outline-offset:2px}[hidden]{display:none!important}@media(max-width:540px){summary strong{float:none;display:block;margin-left:20px}}
"""

SCRIPT = """
const links = document.querySelectorAll('nav a');
const panels = document.querySelectorAll('main > section');
function showView() {
  const hash = location.hash.slice(1);
  const match = hash.startsWith('ottelu-') ? document.getElementById(hash) : null;
  const view = match ? 'ottelut' : [...panels].some(p => p.id === hash) ? hash : 'kausi';
  panels.forEach(p => { p.hidden = p.id !== view; });
  links.forEach(a => {
    if (a.hash === '#' + view) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  if (match) {
    match.open = true;
    requestAnimationFrame(() => match.scrollIntoView({block: 'start'}));
  }
}
window.addEventListener('hashchange', showView);
showView();
"""


def report(games, season):
    latest = games[:5]
    title = f"KalPa · {season - 1}–{season}"
    source = (f"https://liiga.fi/fi/ohjelma?kausi={season - 1}-{season}&amp;sarja=runkosarja"
              "&amp;joukkue=kalpa&amp;nakyma=ottelut&amp;rajaus=kausi")
    season_body = (comparison(games) + "<h3>Hyökkääjien kausisummat</h3>"
                   + player_table(aggregate(games), total=True)
                   + "<h3>Hyökkääjät voitoissa ja tappioissa</h3>" + player_comparison(games))
    recent_body = (game_list(latest) + "<h3>Voitot ja tappiot</h3>" + comparison(latest)
                   + "<h3>Hyökkääjät</h3>" + player_table(aggregate(latest), total=True))
    game_body = "".join(match_report(g, i == 0) for i, g in enumerate(games))
    incomplete = sum(bool(g["error"]) for g in games)
    warning = (f'<p class="note">{incomplete} ottelun pelaajatilastot ovat puutteellisia.</p>'
               if incomplete else "")
    timestamp = helsinki(datetime.now(timezone.utc).isoformat()).strftime("%d.%m.%Y %H:%M")
    return f"""<!doctype html>
<html lang="fi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | Otteluanalytiikka</title><style>{CSS}</style></head>
<body><main><header><h1>{title}</h1><p>Runkosarjan hyökkääjät · {len(games)} päättynyttä ottelua ·
<a href="{source}">Liigan otteluohjelma</a></p></header>
<nav aria-label="Raportin näkymät"><a href="#kausi">Koko kausi</a><a href="#viisi">Viisi viimeistä</a><a href="#ottelut">Otteluittain</a></nav>
<section id="kausi"><h2>Koko kausi</h2>{warning}{season_body}</section>
<section id="viisi"><h2>Viisi viimeistä</h2>{recent_body}</section>
<section id="ottelut"><h2>Otteluittain</h2>{game_body or '<p>Päättyneitä otteluita ei löytynyt.</p>'}</section>
<footer><p>m/min = kokonaismatka jaettuna saman aineiston peliaikaminuuteilla. Peliaika, ottelumäärä ja tehot
kattavat saatavilla olevat pelaajatilastot. «Matka mitattu» ilmaisee, montako ottelua on mukana m/min-luvussa.
Kiekonhallinta on osuus joukkueiden yhteenlasketusta hallinta-ajasta. Ero-sarake on voittojen m/min miinus tappioiden m/min. Ryhmävertailussa hallinta on ottelukohtaisten
prosenttien keskiarvo. M = maalit, S = syötöt, L = laukaukset.</p><p>Raportti luotu {timestamp} Suomen aikaa.</p></footer>
</main><script>{SCRIPT}</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kausi", type=int, default=2027, help="kauden päättymisvuosi, oletus 2027")
    parser.add_argument("--tuloste", type=Path, default=Path("KalPa_raportti.html"))
    parser.add_argument("--cache", type=Path, default=Path(".kalpa_cache"))
    parser.add_argument("--paivita-kaikki", action="store_true", help="hae myös aiempien otteluiden tilastot uudelleen")
    parser.add_argument("--avaa", action="store_true", help="avaa valmis raportti selaimessa")
    args = parser.parse_args()
    client = Liiga(args.cache / str(args.kausi))
    try:
        games = collect(client, args.kausi, args.paivita_kaikki)
        atomic_write(args.tuloste, report(games, args.kausi).encode("utf-8"))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Haku epäonnistui: {exc}\n")
    print(f"Valmis: {args.tuloste.resolve()}")
    if args.avaa:
        webbrowser.open(args.tuloste.resolve().as_uri())


if __name__ == "__main__":
    main()
