"""Läuft in GitHub Actions (Repo hub-autopost): postet fällige Reels aus queue/*.json.

Jeder Eintrag in queue/ ist eine Datei (keine Merge-Konflikte zwischen Cloud und PC):
  {"account": "GTA", "asset_id": 123, "caption": "...", "publish_at": "2026-10-02T00:00:00+02:00", "status": "pending"}
Das Video liegt als Release-Asset im Release "media" und wird nach dem Posten gelöscht.

Umgebung: GITHUB_TOKEN, GITHUB_REPOSITORY (von Actions), INSTAGRAM_TOKEN_<ACCOUNT> (Secrets),
AUTOPOST_PAT (Secret, nur für die Token-Verlängerung per `gh secret set`).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://graph.instagram.com/v23.0"
GH = "https://api.github.com"
REPO = os.environ["GITHUB_REPOSITORY"]
GH_HEAD = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "X-GitHub-Api-Version": "2022-11-28"}
QUEUE = Path("queue")
STATE = Path("state")
MAX_PER_RUN = 2
MAX_ATTEMPTS = 3
REFRESH_EVERY_DAYS = 7


def ig(method, path, token, **params):
    params["access_token"] = token
    r = requests.request(method, f"{API}/{path}", params=params, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"{method} {path}: {data.get('error', data)}")
    return data


def token_for(acc):
    token = os.environ.get(f"INSTAGRAM_TOKEN_{acc}", "").strip()
    if not token:
        raise RuntimeError(f"Secret INSTAGRAM_TOKEN_{acc} fehlt")
    state_file = STATE / f"token_{acc}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    if time.time() - state.get("refreshed_at", 0) > REFRESH_EVERY_DAYS * 86400 and os.environ.get("AUTOPOST_PAT"):
        data = requests.get("https://graph.instagram.com/refresh_access_token",
                            params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=30).json()
        if "access_token" in data:
            token = data["access_token"]
            subprocess.run(["gh", "secret", "set", f"INSTAGRAM_TOKEN_{acc}", "--repo", REPO, "--body", token],
                           check=True, env={**os.environ, "GH_TOKEN": os.environ["AUTOPOST_PAT"]})
            STATE.mkdir(exist_ok=True)
            state_file.write_text(json.dumps({"refreshed_at": time.time(),
                                              "expires_in_days": round(data.get("expires_in", 0) / 86400)}))
            print(f"Token {acc} verlängert")
        else:
            print(f"Token {acc} nicht verlängert: {data.get('error', data)}")
    return token


def download_asset(asset_id, dest):
    r = requests.get(f"{GH}/repos/{REPO}/releases/assets/{asset_id}", stream=True, timeout=300,
                     headers={**GH_HEAD, "Accept": "application/octet-stream"})
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)


def post(item, token):
    uid = ig("GET", "me", token, fields="user_id")["user_id"]
    video = Path("/tmp/reel.mp4")
    download_asset(item["asset_id"], video)
    size = video.stat().st_size
    c = ig("POST", f"{uid}/media", token, media_type="REELS", upload_type="resumable",
           caption=item["caption"], share_to_feed="true")
    with open(video, "rb") as f:
        r = requests.post(c["uri"], data=f, timeout=900,
                          headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
    if r.status_code >= 400:
        raise RuntimeError(f"Upload: {r.status_code} {r.text[:300]}")
    for _ in range(90):
        st = ig("GET", c["id"], token, fields="status_code,status")
        if st["status_code"] == "FINISHED":
            break
        if st["status_code"] in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Verarbeitung: {st}")
        time.sleep(10)
    else:
        raise RuntimeError("Timeout bei der Verarbeitung")
    media_id = ig("POST", f"{uid}/media_publish", token, creation_id=c["id"])["id"]
    link = ig("GET", media_id, token, fields="permalink").get("permalink")
    requests.delete(f"{GH}/repos/{REPO}/releases/assets/{item['asset_id']}", headers=GH_HEAD, timeout=60)
    return media_id, link


def main():
    now = datetime.now(timezone.utc)
    items = []
    for p in sorted(QUEUE.glob("*.json")):
        it = json.loads(p.read_text(encoding="utf-8"))
        if it["status"] == "pending" and datetime.fromisoformat(it["publish_at"]) <= now:
            items.append((p, it))
    items.sort(key=lambda x: x[1]["publish_at"])
    print(f"{len(items)} fällig")
    tokens, failed = {}, False
    for p, it in items[:MAX_PER_RUN]:
        acc = it["account"].upper()
        try:
            if acc not in tokens:
                tokens[acc] = token_for(acc)
            media_id, link = post(it, tokens[acc])
            it.update(status="posted", media_id=media_id, permalink=link,
                      posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            it.pop("error", None)
            print(f"OK {p.name} -> {link}")
        except Exception as e:
            it["attempts"] = it.get("attempts", 0) + 1
            it["error"] = str(e)[:300]
            if it["attempts"] >= MAX_ATTEMPTS:
                it["status"] = "failed"
            failed = True
            print(f"FEHLER {p.name}: {e}")
        p.write_text(json.dumps(it, indent=2, ensure_ascii=False), encoding="utf-8")
    sys.exit(1 if failed else 0)  # roter Lauf in GitHub = Benachrichtigung per Mail


if __name__ == "__main__":
    main()
