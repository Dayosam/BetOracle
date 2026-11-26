"""
BetOracle deterministic prediction engine.

Prediction requests are handled here instead of Groq.

Uses:
    sportapi.fixtures()
    sportapi.stats()
    sportybet.get_markets()

Features:
    - Reads nested SportAPI statistics correctly.
    - Checks recent form, home/away form, league table and H2H.
    - Retrieves SportyBet markets.
    - Filters invalid markets and odds.
    - Removes duplicate selections.
    - Ranks qualifying selections using evidence coverage.
    - Provides diagnostic information when no picks qualify.
    - Never creates booking codes automatically.
"""

import re
import math
import time
from datetime import date, timedelta


# ============================================================
# GENERAL HELPERS
# ============================================================

def _get(data, *keys, default=None):
    if not isinstance(data, dict):
        return default

    for key in keys:
        if data.get(key) is not None:
            return data[key]

    return default


def _normalise(value):
    return re.sub(
        r"[^a-z0-9]",
        "",
        str(value or "").lower()
    )


def _team_name(fixture, side):
    value = _get(
        fixture,
        side,
        f"{side}_team",
        f"{side}Team",
        f"{side}_name",
        f"{side}TeamName",
        default=""
    )

    if isinstance(value, dict):
        value = _get(
            value,
            "name",
            "team_name",
            "teamName",
            default=""
        )

    return str(value or "").strip()


def _team_id(fixture, side):
    value = _get(
        fixture,
        f"{side}_id",
        f"{side}Id",
        f"{side}_team_id",
        f"{side}TeamId"
    )

    if value is None:
        team = _get(
            fixture,
            side,
            f"{side}_team",
            f"{side}Team"
        )

        if isinstance(team, dict):
            value = _get(
                team,
                "id",
                "team_id",
                "teamId"
            )

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _league_id(fixture):
    value = _get(
        fixture,
        "league_id",
        "leagueId",
        "competition_id",
        "competitionId"
    )

    if value is None:
        league = _get(
            fixture,
            "league",
            "competition"
        )

        if isinstance(league, dict):
            value = _get(
                league,
                "id",
                "league_id",
                "leagueId"
            )

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _league_name(fixture):
    value = _get(
        fixture,
        "league",
        "league_name",
        "leagueName",
        "competition",
        "competition_name",
        default=""
    )

    if isinstance(value, dict):
        value = _get(
            value,
            "name",
            "league_name",
            default=""
        )

    return str(value or "")


def _kickoff(fixture):
    return str(_get(
        fixture,
        "kickoff",
        "date",
        "start",
        "start_time",
        "startTime",
        "fixture_date",
        "fixtureDate",
        default=""
    ))


def _as_list(value):
    if isinstance(value, list):
        return value

    if isinstance(value, dict):
        for key in (
            "response",
            "data",
            "fixtures",
            "results"
        ):
            if isinstance(value.get(key), list):
                return value[key]

    return []


# ============================================================
# STATISTICS EVIDENCE DETECTION
# ============================================================

def _stats_quality(stats):
    if not isinstance(stats, dict):
        return 0, []

    evidence = []

    home = stats.get("home", {})
    away = stats.get("away", {})
    h2h = stats.get("h2h", [])

    if not isinstance(home, dict):
        home = {}

    if not isinstance(away, dict):
        away = {}

    home_form = home.get("last5_overall", [])
    away_form = away.get("last5_overall", [])

    if home_form or away_form:
        evidence.append("recent form")

    if home.get("last5_at_home"):
        evidence.append("home form")

    if away.get("last5_away"):
        evidence.append("away form")

    if (
        home.get("position") is not None
        and away.get("position") is not None
    ):
        evidence.append("league table")

    if isinstance(h2h, list) and h2h:
        evidence.append("head-to-head")

    return len(evidence), evidence


# ============================================================
# STATISTICS SUMMARY
# ============================================================

def _stats_summary(stats):
    if not isinstance(stats, dict):
        return "No usable statistics were returned."

    parts = []

    home = stats.get("home", {})
    away = stats.get("away", {})
    h2h = stats.get("h2h", [])

    if not isinstance(home, dict):
        home = {}

    if not isinstance(away, dict):
        away = {}

    def add_section(title, data):
        if not data:
            return

        text = str(data)

        if len(text) > 450:
            text = text[:450] + "..."

        parts.append(f"{title}: {text}")

    home_table = {
        "team": home.get("team"),
        "position": home.get("position"),
        "played": home.get("played"),
        "won": home.get("won"),
        "draw": home.get("draw"),
        "lost": home.get("lost"),
        "goals_for": home.get("goals_for"),
        "goals_against": home.get("goals_against")
    }

    home_table = {
        key: value
        for key, value in home_table.items()
        if value is not None
    }

    add_section("Home team statistics", home_table)

    add_section(
        "Home recent form",
        home.get("last5_overall")
    )

    add_section(
        "Home form at home",
        home.get("last5_at_home")
    )

    away_table = {
        "team": away.get("team"),
        "position": away.get("position"),
        "played": away.get("played"),
        "won": away.get("won"),
        "draw": away.get("draw"),
        "lost": away.get("lost"),
        "goals_for": away.get("goals_for"),
        "goals_against": away.get("goals_against")
    }

    away_table = {
        key: value
        for key, value in away_table.items()
        if value is not None
    }

    add_section("Away team statistics", away_table)

    add_section(
        "Away recent form",
        away.get("last5_overall")
    )

    add_section(
        "Away form away from home",
        away.get("last5_away")
    )

    add_section("Head-to-head", h2h)

    if not parts:
        return (
            "No recognised form, table or H2H "
            "information was returned."
        )

    return " | ".join(parts)[:1300]


# ============================================================
# SPORTYBET MARKET HELPERS
# ============================================================

def _market_rows(raw):
    if isinstance(raw, dict):
        for key in (
            "markets",
            "odds",
            "data",
            "results"
        ):
            if isinstance(raw.get(key), list):
                return raw[key]

    return raw if isinstance(raw, list) else []


def _market_text(market):
    fields = (
        "market",
        "market_name",
        "marketName",
        "name",
        "pick",
        "selection",
        "outcome",
        "label"
    )

    return " ".join(
        str(market.get(field, ""))
        for field in fields
    ).lower()


def _market_odds(market):
    try:
        odds = float(_get(
            market,
            "odds",
            "odd",
            "price",
            default=0
        ))

        if math.isfinite(odds) and odds > 1:
            return odds

    except (TypeError, ValueError):
        pass

    return None


def _market_reference(market):
    return _get(
        market,
        "ref",
        "selection_ref",
        "selectionRef",
        "id"
    )


def _market_candidates(rows):
    accepted = []

    for market in rows:

        if not isinstance(market, dict):
            continue

        odds = _market_odds(market)
        reference = _market_reference(market)

        if odds is None or not reference:
            continue

        text = _market_text(market)

        recognised = (
            "over 1.5" in text
            or "over 2.5" in text
            or "under 3.5" in text
            or "both teams to score" in text
            or re.search(r"\bbtts\b", text)
        )

        if recognised:
            accepted.append(market)

    return accepted


# ============================================================
# PICK REASON
# ============================================================

def _reason(market, evidence):
    market_name = _get(
        market,
        "market",
        "market_name",
        "marketName",
        "name",
        default="Selected market"
    )

    selection = _get(
        market,
        "pick",
        "selection",
        "outcome",
        "label",
        default=""
    )

    return (
        f"{market_name} — {selection}. "
        f"Supporting SportAPI information: {evidence}. "
        "This is an assessment, not a guarantee."
    )


# ============================================================
# MAIN PREDICTION ENGINE
# ============================================================

def predict(
    sportapi_module,
    sportybet_module,
    count=2,
    date_from=None,
    date_to=None,
    league=None,
    min_odds=1.15,
    max_candidates=20  # FIX: Reduced from 80 to 20 to prevent timeouts
):
    """
    Generate evidence-screened football predictions.
    """

    count = max(1, min(int(count), 20))

    date_from = date_from or date.today().isoformat()

    date_to = date_to or (
        date.today() + timedelta(days=1)
    ).isoformat()

    # --------------------------------------------------------
    # 0. START TIMER (FIX: Hard stop at 45 seconds)
    # --------------------------------------------------------
    start_time = time.time()
    TIME_LIMIT = 45.0

    # --------------------------------------------------------
    # 1. FETCH FIXTURES
    # --------------------------------------------------------

    fixtures_raw = sportapi_module.fixtures(
        "football",
        date_from,
        date_to,
        league
    )

    fixtures = _as_list(fixtures_raw)

    candidates = []
    rejected_fixtures = 0

    for fixture in fixtures:

        if not isinstance(fixture, dict):
            rejected_fixtures += 1
            continue

        home = _team_name(fixture, "home")
        away = _team_name(fixture, "away")

        home_id = _team_id(fixture, "home")
        away_id = _team_id(fixture, "away")
        league_id = _league_id(fixture)

        if not home or not away:
            rejected_fixtures += 1
            continue

        if (
            home_id is None
            or away_id is None
            or league_id is None
        ):
            rejected_fixtures += 1
            continue

        candidates.append((
            fixture,
            home,
            away,
            home_id,
            away_id,
            league_id
        ))

    candidates = candidates[:max_candidates]

    # --------------------------------------------------------
    # 2. INITIALISE DIAGNOSTICS
    # --------------------------------------------------------

    ranked = []
    errors = []

    stats_checked = 0
    stats_rejected = 0
    markets_checked = 0
    markets_rejected = 0

    # --------------------------------------------------------
    # 3. ANALYSE EACH FIXTURE
    # --------------------------------------------------------

    for (
        fixture,
        home,
        away,
        home_id,
        away_id,
        league_id
    ) in candidates:

        # FIX: Check the clock. If we are out of time, stop looping.
        if time.time() - start_time > TIME_LIMIT:
            errors.append("Time limit reached. Returning partial results.")
            break

        try:

            stats = sportapi_module.stats(
                "football",
                home_id,
                away_id,
                league_id
            )

            stats_checked += 1

            evidence_score, evidence_labels = (
                _stats_quality(stats)
            )

            if evidence_score < 2:
                stats_rejected += 1
                continue

            if not any(
                "form" in item
                for item in evidence_labels
            ):
                stats_rejected += 1
                continue

            evidence = _stats_summary(stats)

            markets_raw = sportybet_module.get_markets(
                "football",
                home,
                away
            )

            markets_checked += 1

            markets = _market_candidates(
                _market_rows(markets_raw)
            )

            if not markets:
                markets_rejected += 1
                continue

            for market in markets:

                odds = _market_odds(market)

                if odds is None or odds < min_odds:
                    continue

                text = _market_text(market)

                specificity = (
                    1
                    if (
                        "over 1.5" in text
                        or "under 3.5" in text
                    )
                    else 0
                )

                score = (
                    evidence_score * 10
                    + specificity
                )

                ranked.append({
                    "home": home,
                    "away": away,
                    "sport": "football",
                    "kickoff": _kickoff(fixture),
                    "league": _league_name(fixture),

                    "market": str(_get(
                        market,
                        "market",
                        "market_name",
                        "marketName",
                        "name",
                        default=""
                    )),

                    "pick": str(_get(
                        market,
                        "pick",
                        "selection",
                        "outcome",
                        "label",
                        default=""
                    )),

                    "odds": odds,

                    "ref": _market_reference(market),

                    "reason": _reason(
                        market,
                        evidence
                    ),

                    "_score": score
                })

        except Exception as exc:

            errors.append(
                f"{home} v {away}: "
                f"{type(exc).__name__}: {exc}"
            )

    # --------------------------------------------------------
    # 4. REMOVE DUPLICATE SELECTIONS
    # --------------------------------------------------------

    unique = {}

    for pick in ranked:

        key = (
            _normalise(pick["home"]),
            _normalise(pick["away"]),
            pick["market"],
            pick["pick"]
        )

        if (
            key not in unique
            or pick["_score"] > unique[key]["_score"]
        ):
            unique[key] = pick

    ranked = list(unique.values())

    # --------------------------------------------------------
    # 5. RANK QUALIFYING SELECTIONS
    # --------------------------------------------------------

    ranked.sort(
        key=lambda item: (
            item["_score"],
            -item["odds"]
        ),
        reverse=True
    )

    selected = ranked[:count]

    for pick in selected:
        pick.pop("_score", None)

    # --------------------------------------------------------
    # 6. GENERATE RESPONSE
    # --------------------------------------------------------

    if not selected:

        reply = (
            f"I couldn't find qualifying picks between "
            f"{date_from} and {date_to}. "
            "I won't force selections when the available "
            "evidence is insufficient."
        )

    else:

        lines = [
            f"Found {len(selected)} evidence-screened pick(s):"
        ]

        for index, pick in enumerate(selected, 1):

            lines.append(
                f"{index}. {pick['home']} v {pick['away']}\n"
                f"League: {pick['league']}\n"
                f"Market: {pick['market']} — {pick['pick']}\n"
                f"Odds: {pick['odds']:.2f}\n"
                f"Reason: {pick['reason']}"
            )

        if len(selected) < count:

            lines.append(
                f"Only {len(selected)} picks passed "
                f"the checks. I did not fill the remaining "
                f"{count - len(selected)} places."
            )

        reply = "\n\n".join(lines)

    # --------------------------------------------------------
    # 7. RETURN RESULTS AND DIAGNOSTICS
    # --------------------------------------------------------

    return {
        "reply": reply,
        "ticket": selected,

        "fixtures_received": len(fixtures),

        "candidates_checked": len(candidates),

        "fixtures_rejected": rejected_fixtures,

        "statistics_checked": stats_checked,

        "statistics_rejected": stats_rejected,

        "markets_checked": markets_checked,

        "markets_rejected": markets_rejected,

        "qualifying_selections": len(selected),

        "candidate_errors": errors[:10],

        "booking_created": False
    }
