#!/usr/bin/env python3
"""
build_epg.py — assemble one XMLTV guide covering the verified channel pack.

Free XMLTV feeds are enormous (the US one is ~1 GB uncompressed) and each covers
a different slice of the world. This pulls several, keeps ONLY the programmes
belonging to channels we actually carry, and writes a single compact
publish/epg.xml(.gz) that both IPTV Smarters and the Tizen app can read.

Matching is the whole problem. An XMLTV <channel id="..."> must equal the M3U
tvg-id or the guide silently shows nothing. We match in three passes, most
reliable first:
  1. exact tvg-id  (after stripping the "@SD"/"@HD" aggregator suffix)
  2. case-folded tvg-id
  3. normalised display-name  (last resort; ambiguous names are skipped)

    python3 tools/build_epg.py
    python3 tools/build_epg.py --only cl,ar        # just those feeds

A COPY OF THIS FILE LIVES IN THE dadtv PUBLISH REPO. The refresh box rebuilds
the guide from a throwaway clone of that repo, so FEEDS / ALIASES / WATCHLIST
edits here only reach Dad's TV once the file is copied across and pushed:

    cp tools/build_epg.py ~/dadtv/tools/build_epg.py && cd ~/dadtv && git commit -am … && git push

Prints an honest match rate — how many of our channels actually got programmes.
A number well below 100% is normal and expected: many small regional streams
simply have no published guide data anywhere.
"""

import argparse
import copy
import gzip
import io
import json
import os
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36"

# Ordered by trust: earlier feeds win when two provide the same channel.
# i.mjh.nz is the only CORS-enabled provider, which matters for the browser
# preview; on the TV config.xml already allows any origin.
#
# 2026-08-11 — iptv-epg.org is GONE. It used to supply epg-cl / epg-ar / epg-ca
# / epg-us and was the ONLY feed publishing our exact iptv-org tvg-ids
# (ESPN.cl, TNTSportsPremium.cl, DSports.us …), so it carried nearly all of
# Dad's sport. It now 302s to a hashed path and answers 200 with a valid but
# EMPTY 20-byte gzip for every country — no error, just no data. That is why
# the guide quietly emptied out. Replaced by open-epg.com below; do not put
# iptv-epg.org back without re-checking the byte count.
FEEDS = {
    "world":   "https://i.mjh.nz/world/epg.xml.gz",
    # open-epg.com is the iptv-epg.org replacement and the backbone of Dad's own
    # tabs. Plain XML, no auth, ~2.7 MB each, refreshed daily, and it carries the
    # channels nothing else does: CDF Premium (= TNT Sports Premium Chile),
    # ESPN PREMIUM.ar, FOX SPORTS ARGENTINA, C5N, Crónica, Mega, 13C.
    # Its ids are spaced and sometimes upper-cased ("ESPN 2.cl",
    # "TNT SPORTS PREMIUM.ar"), so most of them need an ALIASES entry below.
    "open-cl": "https://www.open-epg.com/files/chile1.xml",
    "open-ar": "https://www.open-epg.com/files/argentina1.xml",
    "open-ca": "https://www.open-epg.com/files/canada1.xml",
    # DirecTV Sports' own guide — the only feed anywhere that carries DSports,
    # DSports 2 and DSports+. Four channels, so it costs nothing to pull.
    "dtv-sports": "https://epgshare01.online/epgshare01/epg_ripper_DIRECTVSPORTS1.xml.gz",
    # SV1 and UY1 are filed under El Salvador and Uruguay but are nothing of the
    # sort: both are pan-Latin-American rips that publish the ESPN *regional*
    # playouts by name — "(Sur)", "(Sudamérica)", "(Chile)". They are the only
    # source anywhere for ESPN Premium Chile and ESPN 5/6/7, which no other feed
    # carries, and they are small (1.0 MB / 2.4 MB).
    # Found 2026-08-11 by enumerating epgshare01 rather than guessing filenames.
    # Checked against the fabricated-loop heuristic before use: varied durations
    # and genuinely different titles, unlike open-epg's ESPN PREMIUM.ar.
    # UY1 BEFORE SV1, and the order is load-bearing. SV1 publishes ESPN 5 and 6
    # only as "(Centroamérica y República Dominicana)", and its display-name pass
    # cheerfully matched those to our ESPN5.ar / ESPN6.ar — the wrong playout, and
    # the kind of wrong that shows Dad a programme that is not on. UY1 carries the
    # Southern-Cone ESPN.5.HD.uy / ESPN.6.HD.uy, so letting it match first (first
    # feed wins) gets the right region. SV1 still supplies everything else,
    # including the Chile-specific ids UY1 does not have.
    "uy":      "https://epgshare01.online/epgshare01/epg_ripper_UY1.xml.gz",
    "sv":      "https://epgshare01.online/epgshare01/epg_ripper_SV1.xml.gz",
    "pluto-cl": "https://i.mjh.nz/PlutoTV/cl.xml.gz",
    "pluto-ar": "https://i.mjh.nz/PlutoTV/ar.xml.gz",
    "pluto-ca": "https://i.mjh.nz/PlutoTV/ca.xml.gz",
    "pluto-us": "https://i.mjh.nz/PlutoTV/us.xml.gz",
    "samsung-ca": "https://i.mjh.nz/SamsungTVPlus/ca.xml.gz",
    "samsung-us": "https://i.mjh.nz/SamsungTVPlus/us.xml.gz",
    "ripper-cl": "https://epgshare01.online/epgshare01/epg_ripper_CL1.xml.gz",
    "ripper-ar": "https://epgshare01.online/epgshare01/epg_ripper_AR1.xml.gz",
    "ripper-ca": "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    # USA last, and deliberately so: it is by far the largest tab in the pack,
    # but these feeds are huge (12k+ channels) and lowest-trust for matching, so
    # they only fill gaps the country-specific feeds above left behind.
    # US_SPORTS1 is what fills the generic Deportes tab (Golf Channel, CBS
    # Sports Network, Fubo, Marquee, PGA Tour).
    "us-sports": "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    # US2, not US1: epg_ripper_US1.xml.gz has 404'd since at least 2026-08-11,
    # and the fetch failure was being swallowed as a one-line FAILED in the log.
    "ripper-us": "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
}

NOISE = re.compile(r"\b(hd|fhd|uhd|sd|4k|tv|canal|channel|en vivo|live)\b", re.I)

# Channels the app carries via BUILTINS in js/config.js rather than through the
# generated pack, so verified.json alone never asks for their guide. Keep the
# tvg-id here in step with the one in js/config.js — that is what the app
# matches on.
# Keep in step with BUILTINS in js/config.js (Ultimate CL/AR sports + older hand
# adds). Without these, verified.json never asks for their guide.
EXTRA_CHANNELS = [
    {"name": "CHV Deportes",             "tvgId": "CHVDeportes.cl",      "logo": ""},
    {"name": "TNT Sports Premium Chile", "tvgId": "TNTSportsPremium.cl", "logo": ""},
    {"name": "TNT Sports Chile",         "tvgId": "TNTSports.cl",        "logo": ""},
    {"name": "ESPN Premium Chile",       "tvgId": "ESPNPremium.cl",      "logo": ""},
    {"name": "ESPN Chile",               "tvgId": "ESPN.cl",             "logo": ""},
    {"name": "ESPN 2 Chile",             "tvgId": "ESPN2.cl",            "logo": ""},
    {"name": "ESPN 4 Chile",             "tvgId": "ESPN4.cl",            "logo": ""},
    {"name": "ESPN 5 Chile",             "tvgId": "ESPN5.cl",            "logo": ""},
    {"name": "ESPN 7 Chile",             "tvgId": "ESPN7.cl",            "logo": ""},
    {"name": "DSports",                  "tvgId": "DSports.us",          "logo": ""},
    {"name": "DirecTV Sports 2",         "tvgId": "DIRECTVSports2.cl",   "logo": ""},
    {"name": "DirecTV Sports+",          "tvgId": "DIRECTVSports+.cl",   "logo": ""},
    {"name": "ESPN Premium",             "tvgId": "ESPNPremium.ar",      "logo": ""},
    {"name": "ESPN",                     "tvgId": "ESPN.ar",             "logo": ""},
    {"name": "ESPN 2",                   "tvgId": "ESPN2.ar",            "logo": ""},
    {"name": "ESPN 3",                   "tvgId": "ESPN3.ar",            "logo": ""},
    {"name": "ESPN 4",                   "tvgId": "ESPN4.ar",            "logo": ""},
    {"name": "ESPN 5",                   "tvgId": "ESPN5.ar",            "logo": ""},
    {"name": "ESPN 6",                   "tvgId": "ESPN6.ar",            "logo": ""},
    {"name": "ESPN 7",                   "tvgId": "ESPN7.ar",            "logo": ""},
    {"name": "TNT Sports",               "tvgId": "TNTSports.ar",        "logo": ""},
    {"name": "Fox Sports 3",             "tvgId": "FoxSports3.ar",       "logo": ""},
    {"name": "TyC Sports",               "tvgId": "TyCSports.ar",        "logo": ""},
    {"name": "DSports Argentina",        "tvgId": "DSports.ar",          "logo": ""},
    # HBO family — added 2026-08-11 with the HBO tab. open-epg Chile publishes
    # the whole line-up, so these all get real listings; the spaced ids it uses
    # are mapped in ALIASES below.
    {"name": "HBO",                      "tvgId": "HBO.cl",              "logo": ""},
    {"name": "HBO 2",                    "tvgId": "HBO2.cl",             "logo": ""},
    {"name": "HBO Plus",                 "tvgId": "HBOPlus.cl",          "logo": ""},
    {"name": "HBO Family",               "tvgId": "HBOFamily.cl",        "logo": ""},
    {"name": "HBO Xtreme",               "tvgId": "HBOXtreme.cl",        "logo": ""},
    {"name": "HBO Pop",                  "tvgId": "HBOPop.cl",           "logo": ""},
    {"name": "HBO Signature",            "tvgId": "HBOSignature.cl",     "logo": ""},
    {"name": "HBO Mundi",                "tvgId": "HBOMundi.cl",         "logo": ""},
    {"name": "Cinemax",                  "tvgId": "Cinemax.cl",          "logo": ""},
]

# Feeds sometimes publish a channel under an older or regional id. Map our
# tvg-id to every id a feed might use for the same channel, best first.
# TNT Sports Chile was CDF until the 2021 rebrand and feeds still carry both.
ALIASES = {
    # CDF is TNT Sports Chile's pre-2021 name. CDO is NOT — it is Canal del
    # Deporte Olimpico, a separate Chilean channel we now carry in its own
    # right, so it must keep its own schedule.
    # "CDF Premium.cl" (spaced) is open-epg's id and is the Campeonato Nacional
    # channel — the single most important guide entry in the app.
    "TNTSports.cl":        ["CDF.cl", "CDFBasico.cl", "CDF HD.cl", "CDF Básico.cl"],
    "TNTSportsPremium.cl": ["CDFPremium.cl", "CDF Premium.cl"],
    "CDOPremium.cl":       ["CDO Premium.cl"],
    "FoxSports.ar":        ["FOXSportsConoSur.ar", "FOXSports.ar",
                            "FOX SPORTS ARGENTINA.ar"],
    "FoxSports2.ar":       ["FOXSports2.ar", "FOX SPORTS 2.ar"],
    "FoxSports3.ar":       ["FOX SPORTS 3.ar", "FOXSports3.ar"],
    # TyC Sports and ESPN 7 are single Southern-Cone feeds carried in both
    # countries; only the Chilean list publishes a guide for them.
    "TyCSports.ar":        ["TyCSports.cl", "TYC SPORTS.ar", "TyC Sports.cl"],
    # --- SV1 / UY1 regional playouts. These filled seven channels that had no
    #     guide anywhere before 2026-08-11. SV1 names the region in the id, so
    #     pick "(Chile)" or "(Sudamérica)"/"(Sur)" and never "(Norte)",
    #     "(México)" or "(Centroamérica…)" — those are the wrong playout and
    #     would print the wrong programme.
    "ESPN7.ar":            ["Canal.ESPN.7.(Sudamérica).sv", "ESPN7.cl"],
    "ESPN6.ar":            ["ESPN.6.HD.uy", "ESPN6.cl"],
    "ESPNPremium.cl":      ["Canal.ESPN.Premium.(Chile).sv"],
    # --- open-epg.com spaces its numbered ESPN ids, so neither the exact nor
    # the case-folded pass reaches them. Spelled out rather than made into a
    # normalising rule: a rule that ate spaces would also fold "ESPN 2" into
    # "ESPN2" for feeds where those are genuinely different channels.
    "ESPN2.cl":            ["ESPN 2.cl"],
    "ESPN4.cl":            ["ESPN 4.cl"],
    # NOTE: one entry per key. These were briefly duplicated higher up the
    # dict and Python silently kept only the later definition, so the SV1
    # aliases were dropped and both channels stayed guide-less.
    "ESPN5.cl":            ["ESPN 5.cl", "Canal.ESPN.5.(Sudamérica).sv"],
    "ESPN7.cl":            ["ESPN 7.cl", "Canal.ESPN.7.(Chile).sv"],
    "ESPN2.ar":            ["ESPN 2.ar"],
    "ESPN3.ar":            ["ESPN 3.ar"],
    "ESPN4.ar":            ["ESPN 4.ar"],
    "ESPN5.ar":            ["ESPN 5.ar", "ESPN.5.HD.uy"],
    "ESPNExtra.ar":        ["ESPN Extra.cl", "ESPN Extra.ar"],
    # ESPN Premium is one Argentine channel (the Liga Profesional rights holder),
    # and open-epg DOES publish an "ESPN PREMIUM.ar" — but it is FABRICATED, so
    # it is deliberately NOT aliased here. Removed 2026-08-11 after the owner
    # reported the channel looking wrong.
    # What it publishes is a gapless 24-hour loop of 115-minute "Fútbol Argentino
    # Primera División" blocks with the same fixtures repeating three times a day.
    # Caught by playing the channel and reading the picture: the guide claimed
    # "Independiente vs. Platense" while the feed was showing ESPN Knock Out
    # boxing. The FEED is correct — only the listing was invented.
    # Detect this class of junk before trusting a new feed:
    #   a channel whose programmes are gapless, near-identical in duration, and
    #   repeat their titles is a placeholder, not a schedule.
    # A wrong programme on screen is worse than a blank one, so ESPN Premium now
    # carries no guide on either side of the border.
    # In Argentina the Liga Profesional co-broadcaster is branded plain "TNT
    # Sports"; open-epg lists it as "TNT SPORTS PREMIUM.ar". Same channel — not
    # to be confused with TNT Sports Premium *Chile*, which is CDF Premium.
    "TNTSports.ar":        ["TNT SPORTS PREMIUM.ar"],
    # DirecTV Sports' own feed (dtv-sports) is the only real guide for these.
    # The old DIRECTVSports*.cl aliases were iptv-epg.org ids and are dead.
    "DSports.us":          ["DSPORTS.(ARG).dtvsp", "DIRECTVSports.cl"],
    "DSports.ar":          ["DSPORTS.(ARG).dtvsp", "DIRECTVSports.cl"],
    "DIRECTVSports2.cl":   ["DSPORTS.2.HD.dtvsp"],
    "DIRECTVSports+.cl":   ["DSPORTS.+.HD.dtvsp"],
    # --- news and general, also stranded when iptv-epg.org emptied out ---
    "Mega.cl":             ["Mega.cl"],
    "ChileVision.cl":      ["Chilevisión.cl"],
    "T13.cl":              ["13C.cl", "Canal 13.cl"],
    "C5N.ar":              ["C5N.ar"],
    "CronicaTV.ar":        ["CRONICA TV.ar"],
    "A24.ar":              ["A24.ar"],
    "ElTrece.ar":          ["EL TRECE.ar"],
    "AmericaTV.ar":        ["AMERICA TV.ar"],
    "ElNueve.ar":          ["EL NUEVE.ar"],
    "NetTV.ar":            ["NET TV.ar"],
    "Canal26.ar":          ["Canal 26.ar"],
    # --- HBO family. open-epg CL spaces these ids; "HBO.cl" and "Cinemax.cl"
    # match exactly and need no alias. HBO Plus is published per-region, so both
    # the Este and Panregional feeds are offered, best first.
    "HBO2.cl":             ["HBO 2.cl", "HBO 2.ar"],
    "HBOPlus.cl":          ["HBO Plus Este.cl", "HBO Plus Panregional.cl", "HBO PLUS.ar"],
    "HBOFamily.cl":        ["HBO Family.cl", "HBO FAMILY.ar"],
    "HBOXtreme.cl":        ["HBO Xtreme.cl", "HBO Xtreme (Oeste).cl", "HBO XTREME.ar"],
    "HBOPop.cl":           ["HBO Pop.cl", "HBO POP.ar"],
    "HBOSignature.cl":     ["HBO Signature.cl", "HBO SIGNATURE.ar"],
    "HBOMundi.cl":         ["HBO Mundi.cl", "HBO MUNDI.ar"],
    # Deliberately NOT aliased: TNTSports.ar -> TNTSports.cl. They are different
    # channels — Liga Profesional in Argentina, Campeonato Nacional in Chile —
    # so borrowing the schedule would print the wrong match on screen.
    # Same reason 13 Deportes does not borrow Canal 13's guide.
    # Re-checked 2026-08-11 against open-epg CL+AR+CA, epgshare01 CL1/AR1/
    # DIRECTVSPORTS1/US_SPORTS1 and the Pluto/Samsung feeds: no feed anywhere
    # carries CHVDeportes.cl or 13Deportes.cl. Left blank rather than borrowing
    # a neighbour's schedule.
}

# After matching: copy an already-matched channel's programmes onto a twin that
# has no feed of its own. Needed when both ids are real EXTRA_CHANNELS (alias
# registration cannot steal an id another channel owns — see wanted_index).
GUIDE_SHARE = {
    # ESPN 5 and 7 AR now match directly off SV1/UY1, so these two are only a
    # fallback for the day those feeds go the way of iptv-epg.org.
    "ESPN7.ar":  "ESPN7.cl",     # Southern-Cone ESPN 7
    "DSports.ar": "DSports.us",  # same three panel mirrors / DirecTV Sports brand
    "ESPN5.ar":  "ESPN5.cl",     # Southern-Cone ESPN 5; ESPN5.cl is a channel we
                                 # carry, so an alias cannot claim its id
    # ESPN Premium: the sharing is back, but the direction is reversed and the
    # source is different. open-epg's "ESPN PREMIUM.ar" was fabricated and is
    # still not used; SV1 publishes a REAL "Canal.ESPN.Premium.(Chile).sv",
    # which ESPNPremium.cl takes as an alias above. The Argentine row borrows it
    # from there — legitimate, because the two are the same feed: frame-checked
    # 2026-08-11, both showed byte-identical ESPN Knock Out coverage.
    "ESPNPremium.ar": "ESPNPremium.cl",
    # Southern-Cone ESPN 4, same logic as ESPN 5 and 7 above — but sharing the
    # other way round now: open-epg publishes "ESPN 4.ar" and no Chilean twin,
    # where iptv-epg.org used to publish both.
    "ESPN4.cl": "ESPN4.ar",
}

# Channels the owner actually cares about. Their guide status is reported
# explicitly at the end of every build so a silent regression is visible —
# a 1% overall match rate is normal and hides exactly this.
WATCHLIST = [
    "ChileVision.cl", "Mega.cl", "TVN.cl", "T13.cl", "CNNChile.cl",
    "24Horas.cl", "Telefe.ar", "ElTrece.ar", "AmericaTV.ar", "ElNueve.ar",
    "NetTV.ar", "TN.ar", "C5N.ar", "A24.ar", "CronicaTV.ar", "Canal26.ar",
    "HBO.cl", "HBO2.cl", "HBOPlus.cl", "HBOFamily.cl", "HBOXtreme.cl", "HBOPop.cl",
    "HBOSignature.cl", "HBOMundi.cl", "Cinemax.cl",
    "CHVDeportes.cl", "TNTSports.cl", "TNTSportsPremium.cl",
    "13Deportes.cl", "DIRECTVSports2.cl", "DIRECTVSports+.cl",
    "DSports.us", "ESPN.cl", "ESPN2.cl", "ESPN4.cl",
    "ESPN5.cl", "ESPN7.cl", "ESPN5.ar", "ESPN6.ar", "ESPNPremium.cl",
    "ESPNPremium.ar", "ESPN.ar", "ESPN2.ar", "ESPN3.ar", "ESPN4.ar",
    "ESPN7.ar", "ESPNExtra.ar", "TyCSports.ar", "TNTSports.ar",
    "DSports.ar", "FoxSports.ar", "FoxSports2.ar"
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def bare_id(s):
    return re.sub(r"@[a-z0-9]+$", "", (s or "").strip(), flags=re.I)


def fetch(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw


def _assert_no_duplicate_alias_keys():
    """A repeated key in the ALIASES literal is invisible: Python keeps only the
    last definition and the earlier aliases vanish. That happened on 2026-08-11
    — ESPN 5 and ESPN 7 Chile were given SV1 aliases, a second ESPN5.cl/ESPN7.cl
    pair further down the dict silently won, and both channels stayed guide-less
    through a full build that reported no error at all. Fail loudly instead."""
    import re, collections
    src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    block = src[src.index("ALIASES = {"):src.index("# After matching:")]
    keys = re.findall(r'^\s{4}"([^"]+)":\s*\[', block, re.M)
    dupes = sorted(k for k, n in collections.Counter(keys).items() if n > 1)
    if dupes:
        sys.exit("ALIASES has duplicate keys, so some aliases are being "
                 "silently discarded: %s" % ", ".join(dupes))


def wanted_index(chans):
    """Build the lookup structures the three matching passes need."""
    by_id, by_lower, by_name = {}, {}, {}
    ambiguous = set()
    for c in chans:
        key = c["name"]
        tid = bare_id(c.get("tvgId"))
        if tid:
            by_id.setdefault(tid, key)
            by_lower.setdefault(tid.lower(), key)
        n = norm_name(c["name"])
        if n:
            if n in by_name and by_name[n] != key:
                ambiguous.add(n)          # two channels share a name: unsafe
            else:
                by_name.setdefault(n, key)
    for n in ambiguous:
        by_name.pop(n, None)

    # Register the alternative ids feeds use for the same channel. setdefault so
    # an alias can never steal an id a real channel already owns. is_alias marks
    # them so a channel's own id always beats an alias when both are present in
    # the same feed — otherwise both get merged and you get the same match listed
    # twice at two different times.
    is_alias = set()
    for our_id, alts in ALIASES.items():
        key = by_id.get(our_id)
        if not key:
            continue
        for alt in alts:
            if alt not in by_id:
                by_id[alt] = key
                is_alias.add(alt)
            by_lower.setdefault(alt.lower(), key)

    return by_id, by_lower, by_name, ambiguous, is_alias


def parse_feed(name, raw, by_id, by_lower, by_name, is_alias=frozenset()):
    """Return {our_channel_key: [programme elements]} plus per-pass counts."""
    hits = defaultdict(list)
    stats = {"exact": 0, "lower": 0, "name": 0}

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"    {name}: unparseable XML ({e})")
        return hits, stats

    # Collect every candidate with a quality rank, then keep exactly ONE feed
    # channel per channel of ours. A feed that carries both TNTSports.cl and its
    # legacy CDF.cl would otherwise contribute both, and their programmes
    # interleave into one unreadable overlapping schedule.
    RANK = {"exact": 0, "alias": 1, "lower": 2, "name": 3}
    best = {}                       # our key -> (rank, cid)
    for ch in root.findall("channel"):
        cid = (ch.get("id") or "").strip()
        if not cid:
            continue
        b = bare_id(cid)
        ours = kind = None
        if b in by_id:
            ours, kind = by_id[b], ("alias" if b in is_alias else "exact")
        elif b.lower() in by_lower:
            ours, kind = by_lower[b.lower()], "lower"
        else:
            for dn in ch.findall("display-name"):
                n = norm_name(dn.text or "")
                if n and n in by_name:
                    ours, kind = by_name[n], "name"
                    break
        if not ours:
            continue
        rank = RANK[kind]
        if ours not in best or rank < best[ours][0]:
            best[ours] = (rank, cid, kind)

    xmltv_to_ours = {}
    for ours, (_, cid, kind) in best.items():
        xmltv_to_ours[cid] = ours
        stats[kind if kind in stats else "exact"] += 1

    if not xmltv_to_ours:
        return hits, stats

    for pr in root.findall("programme"):
        ours = xmltv_to_ours.get((pr.get("channel") or "").strip())
        if ours:
            hits[ours].append(pr)
    return hits, stats


def main():
    ap = argparse.ArgumentParser()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--pack", default=os.path.join(root, "data", "verified.json"))
    ap.add_argument("--out", default=os.path.join(root, "publish", "epg.xml"))
    ap.add_argument("--only", default="", help="comma-separated feed keys")
    ap.add_argument("--gzip", action="store_true", help="also write epg.xml.gz")
    args = ap.parse_args()

    _assert_no_duplicate_alias_keys()

    chans = json.load(open(args.pack))

    # Fold in the channels the app ships via BUILTINS, skipping any the pack
    # already carries so we never index the same channel twice.
    have = {bare_id(c.get("tvgId")) for c in chans if c.get("tvgId")}
    for extra in EXTRA_CHANNELS:
        if bare_id(extra["tvgId"]) not in have:
            chans.append(dict(extra))
            log(f"  + builtin: {extra['name']} ({extra['tvgId']})")

    by_id, by_lower, by_name, ambig, is_alias = wanted_index(chans)
    log(f"pack: {len(chans)} channels  ({len(by_id)} with a tvg-id, "
        f"{len(by_name)} name-matchable, {len(ambig)} names too ambiguous to use)\n")

    feeds = FEEDS
    if args.only:
        keys = {k.strip() for k in args.only.split(",")}
        feeds = {k: v for k, v in FEEDS.items() if k in keys}

    def pull(item):
        key, url = item
        try:
            raw = fetch(url)
            log(f"  {key:12s} {len(raw)/1048576:6.1f} MB")
            return key, raw
        except Exception as e:
            log(f"  {key:12s} FAILED  {type(e).__name__}")
            return key, None

    log("fetching feeds")
    with ThreadPoolExecutor(max_workers=6) as ex:
        raws = list(ex.map(pull, feeds.items()))

    log("\nmatching")
    merged, seen_feed = {}, {}
    for key, raw in raws:
        if not raw:
            continue
        hits, st = parse_feed(key, raw, by_id, by_lower, by_name, is_alias)
        new = 0
        for ours, progs in hits.items():
            if ours in merged:          # first (most-trusted) feed wins
                continue
            merged[ours] = progs
            seen_feed[ours] = key
            new += 1
        log(f"  {key:12s} matched {len(hits):5d} channels "
            f"(exact {st['exact']}, lower {st['lower']}, name {st['name']}) "
            f"-> {new} new")

    id_to_key = {bare_id(c.get("tvgId")): c["name"] for c in chans if c.get("tvgId")}
    for dest_id, src_id in GUIDE_SHARE.items():
        dest_key, src_key = id_to_key.get(dest_id), id_to_key.get(src_id)
        if not dest_key or not src_key or dest_key in merged or src_key not in merged:
            continue
        # Deep-copy: the write loop mutates programme channel= attributes.
        merged[dest_key] = [copy.deepcopy(pr) for pr in merged[src_key]]
        seen_feed[dest_key] = seen_feed.get(src_key, "?") + "+share"
        log(f"  share       {dest_id} <- {src_id} ({len(merged[dest_key])} progs)")

    # ---- write one merged XMLTV ----
    out_root = ET.Element("tv", {
        "generator-info-name": "dadlivetv/build_epg.py",
        "source-info-name": "merged free XMLTV feeds",
    })
    by_key = {c["name"]: c for c in chans}
    n_prog = 0
    for ours, progs in merged.items():
        c = by_key.get(ours)
        if not c:
            continue
        cid = bare_id(c.get("tvgId")) or ours
        ch_el = ET.SubElement(out_root, "channel", {"id": cid})
        ET.SubElement(ch_el, "display-name").text = c["name"]
        if c.get("logo"):
            ET.SubElement(ch_el, "icon", {"src": c["logo"]})
        for pr in progs:
            pr.set("channel", cid)
            out_root.append(pr)
            n_prog += 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    ET.ElementTree(out_root).write(args.out, encoding="utf-8", xml_declaration=True)
    size = os.path.getsize(args.out)
    if args.gzip:
        with open(args.out, "rb") as fi, gzip.open(args.out + ".gz", "wb") as fo:
            fo.writelines(fi)

    # ---- watchlist report ----
    # The headline match rate is dominated by thousands of tiny regional streams
    # that have no guide anywhere, so it stays ~1% no matter what. These are the
    # channels whose guide actually matters; call them out by name.
    got, missing = [], []
    for wid in WATCHLIST:
        key = id_to_key.get(wid)
        if key and key in merged:
            got.append(f"{wid} [{seen_feed.get(key)}]")
        else:
            missing.append(wid)
    log(f"\nwatchlist: {len(got)}/{len(WATCHLIST)} of the channels that matter have a guide")
    for g in got:
        log(f"    ok      {g}")
    for m in missing:
        log(f"    MISSING {m}")

    pct = 100 * len(merged) // max(len(chans), 1)
    log(f"\n{len(merged)}/{len(chans)} channels have a guide ({pct}%)")
    log(f"{n_prog} programmes, {size/1048576:.1f} MB -> {args.out}")
    if args.gzip:
        log(f"{os.path.getsize(args.out + '.gz')/1048576:.1f} MB -> {args.out}.gz")


if __name__ == "__main__":
    main()
