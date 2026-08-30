"""E.164 country calling codes — reference data, to tag a WhatsApp send's cost unit with its market.

Meta prices a template message by the RECIPIENT's country, and 360dialog passes that rate through.
The status webhook's pricing object carries the category but NOT the country, so we derive the
country calling code from the recipient number (the `to`/`recipient_id`, already stored on the
Delivery) and record it alongside the category. operations-service maps calling-code → Meta market
and applies the rate card — the geo/rate decision stays entirely on their side; we emit only the
observed prefix.

WHY A PLAIN LONGEST-PREFIX MATCH IS SAFE. ITU-T country calling codes are PREFIX-FREE by design (no
assigned code is a prefix of another), so trying the 3-, then 2-, then 1-digit prefix returns the one
real code or nothing. A code we omitted yields "" (unknown) — never a WRONG country — because a
shorter valid code can't be a prefix of the missing longer one. So an incomplete table degrades to
"unknown", it never misattributes. Codes are ITU-T assignments and change only rarely (years apart).
"""
from __future__ import annotations

# ITU-T assigned country/zone calling codes. Prefix-free (see module docstring), so a subset is still
# correct — it only widens the "unknown" bucket, never mislabels.
_CODES: frozenset[str] = frozenset({
    # zone 1 (NANP) and zone 7
    "1", "7",
    # 2-digit
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47",
    "48", "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65",
    "66", "81", "82", "84", "86", "90", "91", "92", "93", "94", "95", "98",
    # 3-digit
    "211", "212", "213", "216", "218", "220", "221", "222", "223", "224", "225", "226", "227", "228",
    "229", "230", "231", "232", "233", "234", "235", "236", "237", "238", "239", "240", "241", "242",
    "243", "244", "245", "246", "247", "248", "249", "250", "251", "252", "253", "254", "255", "256",
    "257", "258", "260", "261", "262", "263", "264", "265", "266", "267", "268", "269", "290", "291",
    "297", "298", "299", "350", "351", "352", "353", "354", "355", "356", "357", "358", "359", "370",
    "371", "372", "373", "374", "375", "376", "377", "378", "379", "380", "381", "382", "383", "385",
    "386", "387", "389", "420", "421", "423", "500", "501", "502", "503", "504", "505", "506", "507",
    "508", "509", "590", "591", "592", "593", "594", "595", "596", "597", "598", "599", "670", "672",
    "673", "674", "675", "676", "677", "678", "679", "680", "681", "682", "683", "685", "686", "687",
    "688", "689", "690", "691", "692", "850", "852", "853", "855", "856", "880", "886", "960", "961",
    "962", "963", "964", "965", "966", "967", "968", "970", "971", "972", "973", "974", "975", "976",
    "977", "992", "993", "994", "995", "996", "998",
})


def country_calling_code(msisdn: str) -> str:
    """The E.164 country calling code for a recipient number, or "" when it can't be determined.

    Tolerant of the shapes a `to`/`recipient_id` arrives in: a leading '+', spaces, dashes. Returns
    just the numeric calling code (e.g. "966" for a Saudi number) — never the whole number, so the
    usage row keeps no more of the recipient's MSISDN than the market it lands in."""
    digits = "".join(c for c in (msisdn or "") if c.isdigit())
    for n in (3, 2, 1):                 # longest-prefix first; prefix-freedom makes at most one hit
        if len(digits) > n and digits[:n] in _CODES:
            return digits[:n]
    return ""
