#!/usr/bin/env python3
"""
refresh_youtube_channels.py — keep YouTube-only live channels playable in M3U.

C5N (and similar AR news nets) no longer publish a durable free HLS URL; the
official live feed is YouTube. googlevideo HLS tokens expire in a few hours, so
this script re-extracts them with yt-dlp and rewrites playlist.m3u (+ optional
verified.json) in place. Intended to run on a schedule (GitHub Actions / cron).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

# Stable YouTube live pages → channel name / tvg-id used in our playlist.
# Only channels that reliably run a full-time /live broadcast.
YOUTUBE_LIVES = [
    {
        "name": "C5N",
        "tvg_id": "C5N.ar",
        "page": "https://www.youtube.com/c/c5n/live",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ad/C5N_2017_Alternate.svg/960px-C5N_2017_Alternate.svg.png",
    },
    {
        "name": "TN",
        "tvg_id": "TN.ar",
        "page": "https://www.youtube.com/c/todonoticias/live",
        "logo": "https://i.imgur.com/vFwPhPS.png",
    },
    {
        "name": "A24",
        "tvg_id": "A24.ar",
        "page": "https://www.youtube.com/@A24com/live",
        "logo": "https://i.imgur.com/E3pamA5.png",
    },
    {
        "name": "Cronica TV",
        "tvg_id": "CronicaTV.ar",
        "page": "https://www.youtube.com/@cronicatv/live",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Cr%C3%B3nica_TV_logo.svg/960px-Cr%C3%B3nica_TV_logo.svg.png",
    },
    {
        "name": "Telefe Noticias",
        "tvg_id": "TelefeNoticias.ar",
        "page": "https://www.youtube.com/@TelefeNoticias/live",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/cc/Telefe_%28nuevo_logo%29.png/960px-Telefe_%28nuevo_logo%29.png",
    },
    {
        "name": "La Nacion+",
        "tvg_id": "LaNacionMas.ar",
        "page": "https://www.youtube.com/@lanacion/live",
        "logo": "",
    },
    {
        "name": "24 Horas",
        "tvg_id": "24Horas.cl",
        "page": "https://www.youtube.com/user/24horas/live",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/6/6b/Logotipo_del_Canal_24_Horas.png",
    },
]


def yt_hls(page: str) -> str:
    # -4 is load-bearing, not a preference. googlevideo HLS URLs embed the
    # requesting client's address in an /ip/<addr>/ path segment and only serve
    # that address. Over IPv6 that is this Mac's own globally-unique address, so
    # the URL plays here and 403s on the TV — which is exactly what happened:
    # the refresher "succeeded" for weeks while those channels stayed dark.
    # Forcing IPv4 binds the URL to the household's shared NAT address instead,
    # which the TV egresses through too.
    out = subprocess.check_output(
        [
            "yt-dlp",
            "-4",
            "-g",
            "-f",
            "best[height<=1080]/best",
            "--no-warnings",
            "--no-playlist",
            page,
        ],
        timeout=60,
        stderr=subprocess.PIPE,   # keep the real error; DEVNULL hid it before
    ).decode().strip().splitlines()
    if not out:
        raise RuntimeError(f"yt-dlp returned no URL for {page}")
    return out[0].strip()


def patch_playlist(path: str, name: str, tvg_id: str, logo: str, url: str) -> bool:
    text = open(path, encoding="utf-8", errors="replace").read()
    # Match an existing EXTINF for this channel (by tvg-id or exact name).
    pat = re.compile(
        rf'(#EXTINF:-1[^\n]*?(?:tvg-id="{re.escape(tvg_id)}"[^\n]*|,{re.escape(name)})\n)'
        rf'https?://[^\n]+',
        re.I,
    )
    repl = rf"\g<1>{url}"
    new, n = pat.subn(repl, text, count=0)
    if n:
        open(path, "w", encoding="utf-8").write(new)
        return True

    # Not present — append under Argentina Deportes / Argentina.
    block = (
        f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{name}" tvg-logo="{logo}" '
        f'group-title="Argentina",{name}\n{url}\n'
    )
    if not text.endswith("\n"):
        text += "\n"
    open(path, "w", encoding="utf-8").write(text + block)
    return True


def patch_verified(path: str, name: str, tvg_id: str, logo: str, url: str) -> bool:
    if not os.path.isfile(path):
        return False
    chans = json.load(open(path, encoding="utf-8"))
    key = name.strip().lower()
    hit = None
    for c in chans:
        if (c.get("tvgId") or "").lower() == tvg_id.lower() or (c.get("name") or "").strip().lower() == key:
            hit = c
            break
    if hit:
        urls = [url] + [u for u in (hit.get("urls") or []) if u != url]
        hit["urls"] = urls[:5]
        hit["name"] = name
        hit["tvgId"] = tvg_id
        if logo:
            hit["logo"] = logo
        cats = hit.setdefault("cats", [])
        for cat in ("argentina", "news"):
            if cat not in cats:
                cats.insert(0, cat)
        hit["geo"] = "global"
        hit.setdefault("srcs", [])
        if "youtube-refresh" not in hit["srcs"]:
            hit["srcs"].append("youtube-refresh")
    else:
        chans.append({
            "name": name,
            "cats": ["argentina", "news"],
            "country": "ar",
            "group": "Argentina",
            "logo": logo,
            "tvgId": tvg_id,
            "urls": [url],
            "w": 1920,
            "h": 1080,
            "vcodec": "h264",
            "acodec": "aac",
            "nstreams": 1,
            "geo": "global",
            "cdn": "youtube",
            "srcs": ["youtube-refresh"],
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(chans, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--playlist", required=True, help="Path to playlist.m3u")
    ap.add_argument("--verified", default="", help="Optional verified.json to update")
    args = ap.parse_args()

    changed = False
    for ch in YOUTUBE_LIVES:
        try:
            url = yt_hls(ch["page"])
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or b"").decode(errors="replace").strip().splitlines()
            print(f"FAIL {ch['name']}: {detail[-1] if detail else e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"FAIL {ch['name']}: {e}", file=sys.stderr)
            continue
        print(f"OK {ch['name']}: {url[:96]}…")
        if patch_playlist(args.playlist, ch["name"], ch["tvg_id"], ch["logo"], url):
            changed = True
        if args.verified and patch_verified(args.verified, ch["name"], ch["tvg_id"], ch["logo"], url):
            changed = True

    if not changed:
        print("nothing updated", file=sys.stderr)
        sys.exit(1)
    print("updated")


if __name__ == "__main__":
    main()
