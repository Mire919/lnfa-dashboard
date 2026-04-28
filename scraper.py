import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup


LEAGUE_URL = "https://fsr.sportlomo.com/league/208470/"
OUTPUT_FILE = Path("data.json")
DEBUG_LINES_FILE = Path("sportlomo_debug_lines.txt")

TEAM_ALIASES = {
    "Rugby Club Luzern Dangels": "Luzern Dangels",
    "Luzern Dangels": "Luzern Dangels",
    "Grasshopper Club Zurich Rugby Section Valkyries": "GC Zurich Valkyries",
    "GC Zurich Valkyries": "GC Zurich Valkyries",
    "Rugby Football Club Basel W": "Basel RFC W",
    "Basel RFC W": "Basel RFC W",
    "Rugby Club CERN Meyrin St. Genis Wildcats": "CERN MSG Wildcats",
    "CERN MSG Wildcats": "CERN MSG Wildcats",
    "Switzers/Palezieux": "Switzers/Palezieux",
    "Albaladejo Rugby Club W": "Albaladejo W",
    "Albaladejo W": "Albaladejo W",
    "Mermigans": "Mermigans",
    "Entente Red Wolves": "Entente Red Wolves",
}

KNOWN_TEAMS = sorted(TEAM_ALIASES.keys(), key=len, reverse=True)


def clean(text: str) -> str:
    text = str(text)
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("\xa0", " ")
        .replace("\u202f", " ")
        .replace("\u2007", " ")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )
    text = re.sub(r"^[*•]\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def short_team(name: str) -> str:
    name = clean(name)
    return TEAM_ALIASES.get(name, name)


def is_int(s: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", clean(s)))


def is_date(s: str) -> bool:
    return bool(re.fullmatch(r"\d{2}/\d{2}/\d{4}", clean(s)))


def is_short_date(s: str) -> bool:
    return bool(re.fullmatch(r"\d{2} [A-Za-z]{3}", clean(s)))


def is_time(s: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", clean(s)))


def is_score_text(s: str) -> bool:
    return bool(re.fullmatch(r"\d+\s*-\s*\d+", clean(s)))


def is_score_piece_sequence(lines, i):
    # score is split: 29, (5), V, 33, (5), 29 - 33
    return (
        i + 5 < len(lines)
        and is_int(lines[i])
        and re.fullmatch(r"\(\d+\)", lines[i + 1] or "")
        and clean(lines[i + 2]).upper() == "V"
        and is_int(lines[i + 3])
        and re.fullmatch(r"\(\d+\)", lines[i + 4] or "")
        and is_score_text(lines[i + 5])
    )


def fetch_page() -> str:
    headers = {"User-Agent": "Mozilla/5.0 LNFA Dashboard Scraper"}
    response = requests.get(LEAGUE_URL, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def get_lines(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    DEBUG_LINES_FILE.write_text("\n".join(f"{i}: {line}" for i, line in enumerate(lines)), encoding="utf-8")
    return lines


def parse_table(lines):
    """
    Sportlomo liefert die Tabelle aktuell nicht zeilenweise, sondern feldweise:
    0, Teamname, P, W, L, D, PF, PA, Diff, TF, TA, TD, Pts
    """
    teams = []
    try:
        start = lines.index("Pts") + 1
    except ValueError:
        return teams

    try:
        end = lines.index("Date", start)
    except ValueError:
        end = len(lines)

    i = start
    while i < end:
        if lines[i] != "0":
            i += 1
            continue

        if i + 12 >= end:
            break

        team = lines[i + 1]
        nums = lines[i + 2:i + 13]

        if len(nums) == 11 and all(is_int(x) for x in nums):
            p, w, l, d, pf, pa, diff, tf, ta, td, pts = map(int, nums)
            teams.append({
                "team": clean(team),
                "short": short_team(team),
                "played": p,
                "wins": w,
                "losses": l,
                "draws": d,
                "points_for": pf,
                "points_against": pa,
                "difference": diff,
                "tries_for": tf,
                "tries_against": ta,
                "tries_difference": td,
                "table_points": pts,
                "win_rate": round((w / p * 100) if p else 0, 1),
                "points_for_per_game": round((pf / p) if p else 0, 2),
                "points_against_per_game": round((pa / p) if p else 0, 2),
                "net_rating": round((diff / p) if p else 0, 2),
                "try_difference_per_game": round((td / p) if p else 0, 2),
                "home_wins": 0,
                "home_losses": 0,
                "away_wins": 0,
                "away_losses": 0,
                "form": "",
            })
            i += 13
        else:
            i += 1

    teams.sort(key=lambda x: x["table_points"], reverse=True)
    for idx, team in enumerate(teams, start=1):
        team["position"] = idx

    return teams


def match_known_team(lines, start):
    joined = ""
    best = None
    best_end = start

    for end in range(start + 1, min(start + 8, len(lines)) + 1):
        joined = clean(" ".join(lines[start:end]))
        if joined in TEAM_ALIASES:
            best = joined
            best_end = end

    if best:
        return best, best_end

    # Fallback: single line
    return clean(lines[start]), start + 1


def parse_fixtures(lines):
    fixtures = []
    try:
        start = lines.index("Date")
        end = lines.index("Comments:")
    except ValueError:
        return fixtures

    i = start
    current_date = None

    while i < end:
        if is_date(lines[i]):
            current_date = lines[i]
            i += 1
            continue

        if current_date and is_short_date(lines[i]):
            # Layout: shortdate, shortdate, venue, home parts..., time, time, away parts..., venue, Referee...
            j = i + 1
            if j < end and is_short_date(lines[j]):
                j += 1

            if j >= end:
                break

            venue = lines[j]
            j += 1

            home_start = j
            while j < end and not is_time(lines[j]):
                j += 1
            home = clean(" ".join(lines[home_start:j]))

            if j >= end:
                i += 1
                continue

            time = lines[j]
            j += 1
            if j < end and is_time(lines[j]):
                j += 1

            away_start = j
            while j < end and lines[j] not in [venue, "Referee", "Evaluator"] and not is_short_date(lines[j]) and not is_date(lines[j]):
                j += 1
            away = clean(" ".join(lines[away_start:j]))

            if home and away:
                fixtures.append({
                    "date": current_date,
                    "time": time,
                    "home": short_team(home),
                    "away": short_team(away),
                    "home_full": home,
                    "away_full": away,
                    "venue": venue,
                })

            i = max(j, i + 1)
            continue

        i += 1

    return dedupe(fixtures, ("date", "time", "home", "away", "venue"))


def parse_results(lines):
    results = []
    try:
        start = lines.index("Comments:")
    except ValueError:
        return results

    i = start
    current_date = None

    while i < len(lines):
        if is_date(lines[i]):
            current_date = lines[i]
            i += 1
            continue

        if current_date and is_short_date(lines[i]):
            # Layout: shortdate, time, venue, home parts..., score pieces, away parts..., venue, Team sheet...
            j = i + 1
            if j >= len(lines) or not is_time(lines[j]):
                i += 1
                continue

            time = lines[j]
            j += 1

            if j >= len(lines):
                break
            venue = lines[j]
            j += 1

            home_start = j
            score_start = None
            while j < len(lines):
                if is_score_piece_sequence(lines, j):
                    score_start = j
                    break
                # stop safety
                if is_date(lines[j]) or lines[j] == "Comments:":
                    break
                j += 1

            if score_start is None:
                i += 1
                continue

            home = clean(" ".join(lines[home_start:score_start]))
            hs = int(lines[score_start])
            ht = int(lines[score_start + 1].strip("()"))
            aas = int(lines[score_start + 3])
            at = int(lines[score_start + 4].strip("()"))
            score = lines[score_start + 5]

            away_start = score_start + 6
            k = away_start
            while k < len(lines) and lines[k] not in [venue, "Team sheet", "Referee", "Evaluator"] and not is_date(lines[k]) and not is_short_date(lines[k]):
                k += 1
            away = clean(" ".join(lines[away_start:k]))

            if home and away:
                results.append({
                    "date": current_date,
                    "time": time,
                    "home": short_team(home),
                    "away": short_team(away),
                    "home_full": home,
                    "away_full": away,
                    "home_score": hs,
                    "away_score": aas,
                    "home_tries": ht,
                    "away_tries": at,
                    "score": score,
                    "venue": venue,
                })

            i = max(k, i + 1)
            continue

        i += 1

    return dedupe(results, ("date", "time", "home", "away", "score"))


def dedupe(items, fields):
    unique = []
    seen = set()
    for item in items:
        key = tuple(item.get(f) for f in fields)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def enrich_team_home_away_and_form(teams, results):
    by_team = {team["short"]: team for team in teams}

    for team in teams:
        team["home_wins"] = 0
        team["home_losses"] = 0
        team["away_wins"] = 0
        team["away_losses"] = 0
        team["_form_list"] = []

    def date_key(result):
        return datetime.strptime(result["date"], "%d/%m/%Y")

    for r in sorted(results, key=date_key):
        home = by_team.get(r["home"])
        away = by_team.get(r["away"])

        if not home or not away:
            continue

        hs = r["home_score"]
        aas = r["away_score"]

        if hs > aas:
            home["home_wins"] += 1
            away["away_losses"] += 1
            home["_form_list"].append("W")
            away["_form_list"].append("L")
        elif hs < aas:
            home["home_losses"] += 1
            away["away_wins"] += 1
            home["_form_list"].append("L")
            away["_form_list"].append("W")
        else:
            home["_form_list"].append("D")
            away["_form_list"].append("D")

    for team in teams:
        team["form"] = "".join(team["_form_list"][-5:])
        team.pop("_form_list", None)


def main():
    html = fetch_page()
    lines = get_lines(html)

    teams = parse_table(lines)
    results = parse_results(lines)
    fixtures = parse_fixtures(lines)

    enrich_team_home_away_and_form(teams, results)

    data = {
        "source": LEAGUE_URL,
        "league": "LNFA",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "teams": teams,
        "results": results,
        "fixtures": fixtures,
        "notes": [
            "Spielerinnen/Team-Sheets werden nur übernommen, wenn Sportlomo sie öffentlich und strukturiert im HTML ausgibt.",
            "Falls Sportlomo die Seitenstruktur ändert, muss scraper.py eventuell angepasst werden."
        ],
    }

    OUTPUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK: {OUTPUT_FILE} erstellt/aktualisiert")
    print(f"Teams: {len(teams)} | Resultate: {len(results)} | kommende Spiele: {len(fixtures)}")
    print(f"Debug-Datei erstellt: {DEBUG_LINES_FILE}")

    if len(teams) != 8:
        print("HINWEIS: Erwartet wären aktuell 8 Teams.")
    if len(results) == 0:
        print("HINWEIS: Keine Resultate erkannt.")


if __name__ == "__main__":
    main()
