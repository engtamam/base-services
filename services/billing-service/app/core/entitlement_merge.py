"""Merging the limits of every subscription a tenant pays for into one answer.

A tenant's entitlements are the UNION of everything they bought, not the contents of one row.
Resolution used to take a single subscription — `limit=1, sort=created desc` — so a customer who
bought three things was entitled to one of them, and WHICH one depended on insertion order.

What made that expensive is the shape of the failure. The refusal is a fail-closed 402,
indistinguishable from "you have not paid for this", so the customer, the support engineer and the
operator all read it as a billing problem with the customer.

THE STRATEGY BELONGS TO THE KEY, NOT THE DEPLOYMENT.

A single "sum or max" switch breaks on the first mixed catalogue, and the counter-example is
ordinary rather than exotic:

    seats                 SUM   two packs of 10 is 20 seats
    max_retention_days    MAX   the longer plan wins; adding them is meaningless
    sso_enabled           OR    any subscription granting it grants it
    support_tier          RANK  neither sum nor max — "gold" beats "silver" by ORDER

So each limit declares its own strategy, defaulting by TYPE, and a platform overrides per key.

EVERY DEFAULT LEANS TOWARD THE ANSWER THAT IS CHEAPER TO BE WRONG ABOUT. Refusing a paying
customer is the failure that started this, so an unknown numeric key SUMS (generous) — but an
unknown STRATEGY NAME resolves conservatively and says so, because a strategy nobody recognises is
a config error and inventing a permissive reading of one is how a config error becomes free access.

PROVENANCE IS PART OF THE ANSWER, NOT A DEBUG AID. Every value carries which subscriptions
contributed and what each gave, because the question a customer actually asks is not "am I
allowed" but "I paid for 25, why do I have 20?" — and that is unanswerable from a bare number.
"""
from __future__ import annotations

from typing import Any

SUM, MAX, MIN, OR, AND, RANK, REPLACE = "sum", "max", "min", "or", "and", "rank", "replace"
STRATEGIES = (SUM, MAX, MIN, OR, AND, RANK, REPLACE)

# What a key gets when nobody declared a strategy. By TYPE, because the type is the only honest
# signal available: numbers accumulate, flags are granted.
_DEFAULT_BY_TYPE = {bool: OR, int: SUM, float: SUM, str: REPLACE}

UNLIMITED = "unlimited"


class MergeRefused(ValueError):
    """The contributions cannot be combined, and guessing would invent an entitlement."""


def _kind(value: Any) -> str:
    """The broad category a value belongs to, for detecting subscriptions that disagree.

    int and float are ONE kind: a plan limit of 5 and another of 5.5 are both numbers and summing
    them is meaningful. bool is its own kind despite being an int in Python, because OR-ing a flag
    and summing a count are different operations on different things.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "other"


def default_strategy(value: Any) -> str:
    """The strategy for a key nobody declared one for.

    TWO defences against the same trap, and only one of them is load-bearing — said plainly
    because a mutation test proved it. `_DEFAULT_BY_TYPE` is keyed on the EXACT type and has its
    own `bool` entry, so the lookup alone is already correct. The isinstance check in front of it
    is belt-and-braces for the day someone "simplifies" that dict to an isinstance chain, where
    `isinstance(True, int)` being true would silently give every boolean SUM and turn two granted
    flags into `2`.
    """
    if isinstance(value, bool):
        return OR
    return _DEFAULT_BY_TYPE.get(type(value), REPLACE)


def merge(key: str, contributions: list[dict[str, Any]], strategy: str | None = None,
          rank_order: tuple[str, ...] = ()) -> dict[str, Any]:
    """Combine one key across subscriptions. Returns the value AND how it was reached.

    `contributions` is `[{"subscription": id, "value": v}, …]` in a stable order — the caller sorts,
    so a merge is a pure function of its inputs and two identical inputs cannot produce two answers.
    """
    live = [c for c in contributions if c.get("value") is not None]
    if not live:
        return {"value": None, "strategy": strategy or "none", "from": []}

    values = [c["value"] for c in live]
    # UNLIMITED is a SENTINEL, not a type. It is deliberately spelled as a string and mixes with
    # numbers on purpose ("10 seats here, no cap there"), so it must not read as subscriptions
    # disagreeing about what the key is.
    kinds = {_kind(v) for v in values if v != UNLIMITED}
    if strategy is None and len(kinds) > 1:
        # MIXED TYPES WITH NO DECLARED STRATEGY. Inferring from values[0] would pick REPLACE for
        # a string and then cheerfully return the trailing number — so "twenty" and 5 resolves to
        # 5, silently, and a limit somebody typed wrongly becomes a limit that looks deliberate.
        # A key whose subscriptions disagree about what KIND of thing it is cannot be merged by
        # any rule we could guess.
        raise MergeRefused(
            f"{key}: subscriptions disagree on type ({values!r}); declare a strategy or fix the "
            f"plan limits")
    # Infer from a REAL value, never from the sentinel — default_strategy("unlimited") would
    # give REPLACE and quietly discard the numeric contributions it was meant to beat.
    concrete = next((v for v in values if v != UNLIMITED), values[0])
    chosen = strategy or default_strategy(concrete)
    if chosen not in STRATEGIES:
        # A strategy nobody recognises is a CONFIG ERROR. Refusing is the only safe reading:
        # picking a permissive default here is how a typo becomes free access to a paid feature.
        #
        # `_apply` refuses an unrecognised strategy too, so removing this check changes no
        # behaviour — verified by mutation. It stays because it fails EARLY, with the full list of
        # valid names, rather than at the bottom of a dispatch chain.
        raise MergeRefused(
            f"{key}: unknown merge strategy {chosen!r} — one of {', '.join(STRATEGIES)}")

    # UNLIMITED beats every arithmetic. Summing it, or comparing it as a string, would produce a
    # number smaller than unlimited and quietly CAP a customer who bought no cap at all.
    if any(v == UNLIMITED for v in values):
        if chosen in (SUM, MAX):
            return _answer(UNLIMITED, chosen, live)
        if chosen is MIN:
            values = [v for v in values if v != UNLIMITED] or [UNLIMITED]

    try:
        value = _apply(chosen, values, rank_order, key)
    except MergeRefused:
        raise
    except (TypeError, ValueError) as exc:
        # Mixed types across subscriptions — a number and a string for the same key. Refuse rather
        # than coerce: coercion is how "20" and 5 becomes 205.
        raise MergeRefused(f"{key}: cannot merge {values!r} with {chosen!r} ({exc})") from exc
    return _answer(value, chosen, live)


def _apply(strategy: str, values: list[Any], rank_order: tuple[str, ...], key: str) -> Any:
    if strategy == SUM:
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            raise MergeRefused(f"{key}: SUM needs numbers, got {values!r}")
        return sum(values)
    if strategy == MAX:
        return max(values)
    if strategy == MIN:
        return min(values)
    if strategy == OR:
        return any(bool(v) for v in values)
    if strategy == AND:
        return all(bool(v) for v in values)
    if strategy == REPLACE:
        # LAST wins, and the caller's ordering is what makes that meaningful — newest last.
        return values[-1]
    if strategy == RANK:
        if not rank_order:
            # No vocabulary means no order, and an invented order silently promotes or demotes a
            # customer's support tier. Refuse and say what is missing.
            raise MergeRefused(
                f"{key}: RANK needs a declared order (e.g. bronze,silver,gold); none configured")
        unknown = [v for v in values if v not in rank_order]
        if unknown:
            raise MergeRefused(f"{key}: {unknown!r} not in the declared rank order {rank_order!r}")
        return max(values, key=rank_order.index)
    raise MergeRefused(f"{key}: unreachable strategy {strategy!r}")


def _answer(value: Any, strategy: str, live: list[dict]) -> dict[str, Any]:
    return {
        "value": value,
        "strategy": strategy,
        # Which subscription gave what. The answer to "I paid for 25, why do I have 20?".
        "from": [{"subscription": c.get("subscription", ""), "contributed": c["value"]}
                 for c in live],
    }
