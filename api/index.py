import os
import re
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

app = FastAPI()

ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "x.com", "twitter.com",
    "instagram.com", "facebook.com", "fb.watch", "fb.com",
)


def check_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise HTTPException(400, "Solo se admiten enlaces de YouTube, X, Instagram y Facebook.")


def extract(url: str) -> dict:
    check_url(url)
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    cookies = os.environ.get("YT_COOKIES")  # opcional: cookies en formato Netscape
    if cookies:
        path = "/tmp/cookies.txt"
        with open(path, "w") as f:
            f.write(cookies)
        opts["cookiefile"] = path
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        raise HTTPException(422, msg[:300])


def usable(f: dict) -> bool:
    return f.get("url") and f.get("protocol") in ("https", "http")


def size_of(f: dict):
    return f.get("filesize") or f.get("filesize_approx")


@app.get("/api/info")
def info(url: str = Query(...)):
    data = extract(url)
    formats = []
    for f in data.get("formats") or []:
        if not usable(f):
            continue
        has_v = f.get("vcodec") not in (None, "none")
        has_a = f.get("acodec") not in (None, "none")
        if has_v and has_a:
            kind = "video"
        elif has_a:
            kind = "audio"
        else:
            continue
        h = f.get("height") or 0
        label = f"{h}p" if kind == "video" and h else ("Solo audio" if kind == "audio" else "Video")
        formats.append({
            "id": f["format_id"], "kind": kind, "label": label, "ext": f.get("ext"),
            "height": h, "size": size_of(f), "bitrate": f.get("tbr") or 0,
        })
    videos = sorted((f for f in formats if f["kind"] == "video"), key=lambda f: (f["height"], f["bitrate"]), reverse=True)
    audios = sorted((f for f in formats if f["kind"] == "audio"), key=lambda f: f["bitrate"], reverse=True)[:1]
    if not videos and not audios:
        raise HTTPException(422, "No se encontró ningún formato descargable para este enlace.")
    return {
        "title": data.get("title"), "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"), "uploader": data.get("uploader"),
        "formats": videos + audios,
    }


@app.get("/api/download")
def download(url: str = Query(...), format_id: str = Query(...)):
    data = extract(url)
    fmt = next((f for f in data.get("formats") or [] if f.get("format_id") == format_id and usable(f)), None)
    if not fmt:
        raise HTTPException(404, "Formato no disponible.")

    client = httpx.Client(timeout=None, follow_redirects=True)
    req = client.build_request("GET", fmt["url"], headers=fmt.get("http_headers") or {})
    upstream = client.send(req, stream=True)
    if upstream.status_code >= 400:
        upstream.close()
        client.close()
        raise HTTPException(502, f"El origen rechazó la descarga ({upstream.status_code}).")

    def body():
        try:
            yield from upstream.iter_raw(256 * 1024)
        finally:
            upstream.close()
            client.close()

    title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", data.get("title") or "video").strip()[:120] or "video"
    filename = f"{title}.{fmt.get('ext') or 'mp4'}"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    length = upstream.headers.get("content-length")
    if length and "content-encoding" not in upstream.headers:
        headers["Content-Length"] = length
    return StreamingResponse(body(), media_type=upstream.headers.get("content-type", "application/octet-stream"), headers=headers)
