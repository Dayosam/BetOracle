"""
BetOracle deterministic prediction engine.

Prediction requests are handled here instead of Groq.
Groq remains available for normal chat and explanations.

Uses:
    sportapi.fixtures()
    sportapi.stats()
    sportybet.get_markets()

The engine never creates booking codes automatically.
"""

import re
import math
from datetime import date, timedelta
from difflib import SequenceMatcher


def _get(data, *keys, default=None):
    if not isinstance(data, dict):
        return default

    for key in keys:
        if data.get(key) is not None:
            return data[key]

    return default


def _normalise(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


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
        value = _get(value, "name", "team_name", "teamName", default="")

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
        team = _get(fixture, side, f"{side}_team", f"{side}Team")

        if isinstance(team, dict):
            value = _get(team, "id", "team_id", "teamId")

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
        league = _get(fixture, "league", "competition")

        if isinstance(league, dict):
            value = _get(league, "id", "league_id", "leagueId")

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
        value = _get(value, "name", "league_name", default="")

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
        for key in ("response", "data", "fixtures", "results"):
            if isinstance(value.get(key), list):
                return value[key]

    return []


def _stats_quality(stats):
    """
    Check whether the statistics response contains usable evidence.

    This does not manufacture probabilities or confidence percentages.
    """

    if not isinstance(stats, dict):
        return 0, []

    keys = " ".join(str(key) for key in stats.keys()).lower()

    evidence = []

    checks = [
        ("league table", ("standing", "table")),
        ("head-to-head", ("h2h", "head")),
        ("recent form", ("form", "results", "last")),
        ("home form", ("home",)),
        ("away form", ("away",))
    ]

    for label, patterns in checks:
        if any(pattern in keys for pattern in patterns):
            evidence.append(label)

    return len(evidence), evidence


def _stats_summary(stats):
    if not isinstance(stats, dict):
        return "No usable statistics were returned."

    parts = []

    for key, value in stats.items():
        name = str(key).lower()

        if any(word in name for word in (
            "standing",
            "table",
            "h2h",
            "form",
            "home",
            "away"
        )):
            text = str(value)

            if len(text) > 450:
                text = text[:450] + "..."

            parts.append(f"{key}: {text}")

    if not parts:
        return "No recognised form, table or H2H information was returned."

    return " | ".join(parts)[:1300]


def _market_rows(raw):
    if isinstance(raw, dict):
        for key in ("markets", "odds", "data", "results"):
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
    """
    Only accept recognised goal markets with a valid price and reference.
    """

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


def predict(
    sportapi_module,
    sportybet_module,
    count=2,
    date_from=None,
    date_to=None,
    league=None,
    min_odds=1.15,
    max_candidates=12
):
    """
    Generate evidence-screened football predictions.

    This function does not call Groq.
    This function does not create booking codes.
    """

    count = max(1, min(int(count), 20))

    date_from = date_from or date.today().isoformat()

    date_to = date_to or (
        date.today() + timedelta(days=1)
    ).isoformat()

    fixtures_raw = sportapi_module.fixtures(
        "football",
        date_from,
        date_to,
        league
    )

    fixtures = _as_list(fixtures_raw)

    candidates = []

    for fixture in fixtures:

        if not isinstance(fixture, dict):
            continue

        home = _team_name(fixture, "home")
        away = _team_name(fixture, "away")

        home_id = _team_id(fixture, "home")
        away_id = _team_id(fixture, "away")
        league_id = _league_id(fixture)

        if not home or not away:
            continue

        if home_id is None or away_id is None or league_id is None:
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

    ranked = []
    errors = []

    for (
        fixture,
        home,
        away,
        home_id,
        away_id,
        league_id
    ) in candidates:

        try:

            # Get team and match statistics.
            stats = sportapi_module.stats(
                "football",
                home_id,
                away_id,
                league_id
            )

            evidence_score, evidence_labels = _stats_quality(stats)

            # Reject matches with insufficient evidence.
            if evidence_score < 2:
                continue

            if not any("form" in item for item in evidence_labels):
                continue

            # Retrieve the available SportyBet markets.
            markets_raw = sportybet_module.get_markets(
                "football",
                home,
                away
            )

            markets = _market_candidates(
                _market_rows(markets_raw)
            )

            if not markets:
                continue

            evidence = _stats_summary(stats)

            for market in markets:

                odds = _market_odds(market)

                if odds is None or odds < min_odds:
                    continue

                text = _market_text(market)

                # Ranking is based on evidence coverage.
                # Odds are only a market availability filter.
                specificity = (
                    1
                    if "over 1.5" in text or "under 3.5" in text
                    else 0
                )

                score = evidence_score * 10 + specificity

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
                    "reason": _reason(market, evidence),
                    "_score": score
                })

        except Exception as exc:

            errors.append(
                f"{home} v {away}: {type(exc).__name__}: {exc}"
            )

    # Remove duplicate selections.
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

    if not selected:

        reply = (
            f"I couldn't find qualifying picks between "
            f"{date_from} and {date_to}. "
            "I won't force selections when the available evidence "
            "is insufficient."
        )

    else:

        lines = [
            f"Found {len(selected)} evidence-screened pick(s):"
        ]

        for index, pick in enumerate(selected, 1):

            lines.append(
                f"{index}. {pick['home']} v {pick['away']}\n"
                f"Market: {pick['market']} — {pick['pick']}\n"
                f"Odds: {pick['odds']:.2f}\n"
                f"Reason: {pick['reason']}"
            )

        if len(selected) < count:

            lines.append(
                f"Only {len(selected)} picks passed the checks. "
                f"I did not fill the remaining {count - len(selected)} places."
            )

        reply = "\n\n".join(lines)

    return {
        "reply": reply,
        "ticket": selected,
        "candidates_checked": len(candidates),
        "candidate_errors": errors[:5],
        "booking_created": False
    }
