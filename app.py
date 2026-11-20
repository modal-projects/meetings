import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode

import httpx
import modal

root = Path(__file__).resolve().parent
meeting_s = 4 * 3600
app = modal.App("meetings")
profile = modal.Volume.from_name(
    "meetings-chrome-profile", create_if_missing=True, environment_name="demos"
)
archive = modal.Volume.from_name(
    "meetings-archive", create_if_missing=True, environment_name="demos"
)
bot_state = modal.Dict.from_name(
    "meetings-bot-state", create_if_missing=True, environment_name="demos"
)

meet_link = re.compile(r"https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}")
stale_s = 15 * 60
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("curl", "fonts-dejavu-core", "xvfb", "x11vnc", "novnc", "websockify")
    .uv_sync(uv_project_dir=str(root), extra_options="--no-dev")
    .run_commands(
        "curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /chrome.deb && apt-get update && apt-get install -y /chrome.deb && rm /chrome.deb",
        "useradd --create-home chrome",
    )
    .add_local_dir(root / "web", "/meetings/web", copy=True)
    .add_local_python_source("app", "bot")
)


@app.server(
    image=image,
    port=8000,
    min_containers=1,
    max_concurrency=4,
    cpu=8,
    memory=8192,
    volumes={"/profile": profile, "/archive": archive},
    secrets=[modal.Secret.from_name("meetings-endpoints")],
    compute_region="us-east",
)
@modal.sessioned()
class Bot:
    @modal.enter()
    async def enter(self):
        import uvicorn

        from bot import Meetings, start_display

        self.display = await start_display()
        self.meetings = Meetings(
            Bot.sessions.terminate.aio,
            archive,
            (await Bot.get_url.aio()).rstrip("/"),
            bot_state,
        )
        self.http_server = uvicorn.Server(
            uvicorn.Config(
                self.meetings.api, host="0.0.0.0", port=8000, access_log=False
            )
        )
        self.server_task = asyncio.create_task(self.http_server.serve())

    @modal.exit()
    async def exit(self):
        await self.meetings.close()
        self.http_server.should_exit = True
        await self.server_task
        with suppress(ProcessLookupError):
            self.display.terminate()
        await self.display.wait()


viewer_cookie = "meetings_viewer"
login_html = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>meeting</title>
    <style>
      @font-face {{
        font-family: "Inter Variable";
        src: url("https://modal-cdn.com/fonts/inter-variable.woff2") format("woff2");
        font-weight: 100 900;
      }}
      @font-face {{
        font-family: "Goga";
        src: url("https://modal-cdn.com/fonts/Goga-VariableVF.woff2") format("woff2");
        font-weight: 100 900;
      }}
      :root {{
        color-scheme: dark;
        --ground: #181818;
        --elevated: #232323;
        --divider: #272727;
        --heading: #ffffff;
        --primary: #d1d1d1;
        --tertiary: #747474;
        --accent: #7fee64;
        --accent-bg: #1d231c;
        --sans: "Inter Variable", ui-sans-serif, system-ui, sans-serif;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: var(--ground);
        color: var(--primary);
        font-family: var(--sans);
        font-size: 14px;
      }}
      form {{
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
        width: 18rem;
        background: var(--elevated);
        border: 1px solid var(--divider);
        border-radius: 0.5rem;
        padding: 1.25rem;
      }}
      input {{
        font: inherit;
        color: var(--primary);
        background: var(--ground);
        border: 1px solid var(--divider);
        border-radius: 0.375rem;
        padding: 0.5rem 0.75rem;
      }}
      input:focus {{ outline: none; border-color: var(--accent); }}
      button {{
        font: inherit;
        font-weight: 500;
        color: var(--accent);
        background: var(--accent-bg);
        border: 1px solid var(--accent-bg);
        border-radius: 0.375rem;
        padding: 0.5rem 0.75rem;
        cursor: pointer;
      }}
      button:hover {{ border-color: var(--accent); }}
    </style>
  </head>
  <body>
    <form method="post" action="/login">
      <input type="password" name="password" placeholder="password" autofocus />
      <input type="hidden" name="next" value="{next}" />
      <button type="submit">view</button>
    </form>
  </body>
</html>"""


@app.function(
    image=image,
    min_containers=1,
    volumes={"/archive": archive},
    secrets=[
        modal.Secret.from_name("meetings-proxy"),
        modal.Secret.from_name("meetings-viewer"),
    ],
)
@modal.asgi_app(custom_domains=["meet.modal.dev"])
def launcher():
    from fastapi import FastAPI, Form, HTTPException, Request
    from fastapi.responses import (
        HTMLResponse,
        RedirectResponse,
        StreamingResponse,
    )

    api = FastAPI()

    def authed(request: Request) -> bool:
        return hmac.compare_digest(
            request.headers.get("x-meetings-key", ""), os.environ["MODAL_PROXY_KEY"]
        ) and hmac.compare_digest(
            request.headers.get("x-meetings-secret", ""),
            os.environ["MODAL_PROXY_SECRET"],
        )

    def proxy_headers() -> dict[str, str]:
        return {
            "Modal-Key": os.environ["MODAL_PROXY_KEY"],
            "Modal-Secret": os.environ["MODAL_PROXY_SECRET"],
        }

    def viewer_token() -> str:
        expiry = str(int(time.time()) + 7 * 86400)
        sig = hmac.new(
            os.environ.get("VIEWER_PASSWORD", "").encode(),
            f"meetings-viewer:{expiry}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"{expiry}:{sig}"

    def viewer_ok(request: Request) -> bool:
        expiry, _, sig = request.cookies.get(viewer_cookie, "").partition(":")
        return (
            os.environ.get("VIEWER_PASSWORD", "") != ""
            and expiry.isdigit()
            and time.time() < int(expiry)
            and hmac.compare_digest(
                sig,
                hmac.new(
                    os.environ["VIEWER_PASSWORD"].encode(),
                    f"meetings-viewer:{expiry}".encode(),
                    hashlib.sha256,
                ).hexdigest(),
            )
        )

    @api.post("/join")
    async def join(request: Request, meet: str = Form()):
        if not authed(request):
            raise HTTPException(status_code=401, detail="proxy auth required")
        meet = meet.strip()
        if not meet_link.fullmatch(meet):
            raise HTTPException(status_code=400, detail="not a google meet url")
        previous = await bot_state.get.aio(f"active:{meet}")
        if previous:
            url, token, location, *rest = previous
            link_id = rest[0] if rest else None
            try:
                async with httpx.AsyncClient(
                    timeout=10, headers=proxy_headers()
                ) as client:
                    response = await client.get(
                        f"{url}?modal_session_token={token}&token={token}&meet={meet}",
                        follow_redirects=True,
                    )
            except Exception:
                response = None
            if response is not None and response.status_code == 200:
                return RedirectResponse(location, status_code=303)
            await bot_state.pop.aio(f"active:{meet}")
            if link_id:
                await bot_state.pop.aio(f"link:{link_id}")
        try:
            session = await asyncio.wait_for(
                Bot.sessions.start.aio(idle_timeout=meeting_s), timeout=170
            )
        except TimeoutError:
            raise HTTPException(
                status_code=503, detail="session start timed out"
            ) from None
        url = (await Bot.get_url.aio()).rstrip("/") + "/"
        link_id = await bot_state.get.aio(f"meet_link:{meet}") or secrets.token_urlsafe(4)
        link = f"https://meet.modal.dev/m/{link_id}"
        location = f"{url}?{urlencode({'modal_session_token': session.token, 'token': session.token, 'meet': meet, 'link': link})}"
        await bot_state.put.aio(f"link:{link_id}", location)
        await bot_state.put.aio(f"meet_link:{meet}", link_id)
        await bot_state.put.aio(
            f"active:{meet}", (url, session.token, location, link_id)
        )
        return RedirectResponse(location, status_code=303)

    def checked_next(next: str) -> str:
        if not next.startswith("/") or next.startswith("//"):
            return "/"
        return next

    @api.get("/login")
    async def login_page(next: str = "/"):
        return HTMLResponse(
            login_html.format(next=html.escape(checked_next(next), quote=True))
        )

    @api.post("/login")
    async def login_submit(password: str = Form(), next: str = Form("/")):
        next = checked_next(next)
        if not os.environ.get("VIEWER_PASSWORD") or not hmac.compare_digest(
            password, os.environ["VIEWER_PASSWORD"]
        ):
            await asyncio.sleep(1)
            return HTMLResponse(
                login_html.format(next=html.escape(next, quote=True)),
                status_code=401,
            )
        response = RedirectResponse(next, status_code=303)
        response.set_cookie(
            viewer_cookie,
            viewer_token(),
            max_age=7 * 86400,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return response

    @api.get("/m/{link_id}")
    async def m(request: Request, link_id: str):
        if not viewer_ok(request):
            target = f"/m/{link_id}"
            return RedirectResponse(
                f"/login?{urlencode({'next': target})}", status_code=303
            )
        location = await bot_state.get.aio(f"link:{link_id}")
        if not location:
            raise HTTPException(status_code=404, detail="link not found")
        if location.startswith("archive:"):
            await archive.reload.aio()
            name = await bot_state.get.aio(f"archive:{location[len('archive:') :]}")
            path = Path("/archive/meetings") / (name or "") / "state.json"
            if not name or not path.exists():
                raise HTTPException(status_code=404, detail="archive not found")
            return HTMLResponse(
                (Path("/meetings/web") / "meeting.html")
                .read_text()
                .replace('EventSource("events")', f'EventSource("/m/{link_id}/events")')
            )
        try:
            async with httpx.AsyncClient(timeout=30, headers=proxy_headers()) as client:
                response = await client.get(location, follow_redirects=True)
                response.raise_for_status()
        except Exception:
            raise HTTPException(status_code=502, detail="meeting unavailable") from None
        return HTMLResponse(
            response.text.replace(
                'EventSource("events")', f'EventSource("/m/{link_id}/events")'
            )
        )

    @api.get("/m/{link_id}/events")
    async def m_events(request: Request, link_id: str):
        if not viewer_ok(request):
            raise HTTPException(status_code=401, detail="login required")
        location = await bot_state.get.aio(f"link:{link_id}")
        if not location:
            raise HTTPException(status_code=404, detail="link not found")
        if location.startswith("archive:"):
            await archive.reload.aio()
            name = await bot_state.get.aio(f"archive:{location[len('archive:') :]}")
            path = Path("/archive/meetings") / (name or "") / "state.json"

            async def archived():
                if name and path.exists():
                    yield f"data: {path.read_text()}\n\n"

            return StreamingResponse(archived(), media_type="text/event-stream")
        base, _, query = location.partition("?")

        async def stream():
            async with httpx.AsyncClient(
                timeout=None, headers=proxy_headers(), follow_redirects=True
            ) as client:
                async with client.stream("GET", f"{base}events?{query}") as upstream:
                    async for chunk in upstream.aiter_raw():
                        yield chunk

        return StreamingResponse(stream(), media_type="text/event-stream")

    @api.get("/m/{link_id}/screens/{file}")
    async def m_screen(request: Request, link_id: str, file: str):
        if not viewer_ok(request):
            raise HTTPException(status_code=401, detail="login required")
        location = await bot_state.get.aio(f"link:{link_id}")
        if not location:
            raise HTTPException(status_code=404, detail="link not found")
        if location.startswith("archive:"):
            meet = location[len("archive:") :]
        else:
            meet = dict(
                pair.split("=", 1)
                for pair in location.partition("?")[2].split("&")
                if "=" in pair
            ).get("meet", "")
            meet = unquote(meet)
        await archive.reload.aio()
        name = await bot_state.get.aio(f"archive:{meet}")
        path = Path("/archive/meetings") / (name or "") / "screens" / Path(file).name
        if not name or not path.exists():
            raise HTTPException(status_code=404, detail="screenshot not found")
        return FileResponse(path)

    return api


organizer = re.compile(r"(?m)^Organizer\n[^\n]*\n\S+@modal\.com\s*$")


def reminder_meet_url(message: dict) -> str | None:
    for file in message.get("files", []):
        if (
            file.get("filetype") == "email"
            and any(
                sender.get("address") == "calendar-notification@google.com"
                for sender in file.get("from", [])
            )
            and file.get("subject", "").startswith("Notification:")
        ):
            body = file.get("plain_text") or file.get("preview", "")
            if (match := meet_link.search(body)) and organizer.search(body):
                return match.group(0)
    return None


def invite(url: str) -> None:
    launcher_url = launcher.get_web_url()
    httpx.post(
        f"{launcher_url.rstrip('/')}/join",
        data={"meet": url},
        headers={
            "X-Meetings-Key": os.environ["MODAL_PROXY_KEY"],
            "X-Meetings-Secret": os.environ["MODAL_PROXY_SECRET"],
        },
        follow_redirects=True,
        timeout=180,
    ).raise_for_status()


@app.function(
    image=modal.Image.debian_slim(python_version="3.13").pip_install("httpx"),
    schedule=modal.Cron("* * * * *"),
    secrets=[
        modal.Secret.from_name("meetings-slack"),
        modal.Secret.from_name("meetings-proxy"),
    ],
    timeout=300,
)
def poll() -> None:
    cursor = bot_state.get("cursor") or str(time.time() - stale_s)
    response = httpx.get(
        "https://slack.com/api/conversations.history",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        params={
            "channel": os.environ["SLACK_CHANNEL"],
            "oldest": cursor,
            "limit": 200,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"conversations.history failed: {payload.get('error')}")
    for message in reversed(payload.get("messages") or []):
        ts = message["ts"]
        if float(ts) < time.time() - stale_s:
            continue
        url = reminder_meet_url(message)
        if url:
            try:
                invite(url)
                print(json.dumps({"event": "joined", "url": url, "ts": ts}))
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "invite_failed",
                            "url": url,
                            "ts": ts,
                            "error": str(exc),
                        }
                    )
                )
                break
        bot_state["cursor"] = ts


@app.function(
    image=image, volumes={"/profile": profile}, timeout=meeting_s, region="us-east"
)
async def login_browser(password: str, events: modal.Queue, stop: modal.Queue):
    import os
    import pwd
    import queue
    import tempfile

    from bot import start_display

    display = await start_display()
    processes = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            auth = str(Path(directory) / "passwd")
            store = await asyncio.create_subprocess_exec(
                "x11vnc",
                "-storepasswd",
                password,
                auth,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await store.wait():
                raise RuntimeError("could not configure vnc password")
            for command in (
                [
                    "x11vnc",
                    "-display",
                    ":99",
                    "-rfbauth",
                    auth,
                    "-localhost",
                    "-forever",
                    "-shared",
                    "-rfbport",
                    "5900",
                    "-quiet",
                ],
                ["websockify", "--web", "/usr/share/novnc", "8080", "localhost:5900"],
            ):
                processes.append(
                    await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                )
            seed = Path("/profile/seed")
            seed.mkdir(parents=True, exist_ok=True)
            user = pwd.getpwnam("chrome")
            for path in [seed, *seed.rglob("*")]:
                os.chown(path, user.pw_uid, user.pw_gid, follow_symlinks=False)
            browser = await asyncio.create_subprocess_exec(
                "runuser",
                "-u",
                "chrome",
                "--",
                "google-chrome-stable",
                "--user-data-dir=/profile/seed",
                "--profile-directory=Profile 1",
                "--no-first-run",
                "https://accounts.google.com",
                env={**os.environ, "DISPLAY": ":99"},
                stdout=asyncio.subprocess.DEVNULL,
            )
            processes.append(browser)
            try:
                await asyncio.wait_for(browser.wait(), timeout=2)
            except TimeoutError:
                pass
            if browser.returncode is not None:
                raise RuntimeError("chrome failed to start")
            async with modal.forward(8080) as tunnel:
                await events.put.aio(
                    f"{tunnel.url}/vnc.html?autoconnect=1&resize=scale"
                )
                while browser.returncode is None:
                    try:
                        await stop.get.aio(timeout=1)
                        break
                    except queue.Empty:
                        pass
            if browser.returncode is None:
                browser.terminate()
            await browser.wait()
            if browser.returncode not in (0, -15):
                raise RuntimeError("chrome failed to start or exited unexpectedly")
            await profile.commit.aio()
    finally:
        for process in processes + [display]:
            if process.returncode is None:
                process.terminate()
            await process.wait()


@app.local_entrypoint()
def login():
    import secrets

    password = secrets.token_urlsafe(6)
    with modal.Queue.ephemeral() as events, modal.Queue.ephemeral() as stop:
        call = login_browser.spawn(password, events, stop)
        print(f"novnc: {events.get(timeout=180)}", flush=True)
        print(f"password: {password}", flush=True)
        try:
            input("press enter after signing in to save the profile: ")
        finally:
            stop.put(True)
            call.get()
            print("profile saved", flush=True)
