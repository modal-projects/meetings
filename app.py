import asyncio
import hmac
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlencode

import httpx
import modal

root = Path(__file__).resolve().parent
meeting_s = 4 * 3600
app = modal.App("meetings")
profile = modal.Volume.from_name("meetings-chrome-profile", create_if_missing=True)
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


@app.function(
    image=image,
    min_containers=1,
    secrets=[modal.Secret.from_name("meetings-proxy")],
)
@modal.asgi_app()
def launcher():
    from fastapi import FastAPI, Form, HTTPException, Request
    from fastapi.responses import RedirectResponse

    api = FastAPI()

    def authed(request: Request) -> bool:
        return hmac.compare_digest(
            request.headers.get("x-meetings-key", ""), os.environ["MODAL_PROXY_KEY"]
        ) and hmac.compare_digest(
            request.headers.get("x-meetings-secret", ""),
            os.environ["MODAL_PROXY_SECRET"],
        )

    @api.post("/join")
    async def join(request: Request, meet: str = Form()):
        if not authed(request):
            raise HTTPException(status_code=401, detail="proxy auth required")
        meet = meet.strip()
        if not meet_link.fullmatch(meet):
            raise HTTPException(status_code=400, detail="not a google meet url")
        try:
            session = await asyncio.wait_for(
                Bot.sessions.start.aio(idle_timeout=meeting_s), timeout=170
            )
        except TimeoutError:
            raise HTTPException(
                status_code=503, detail="session start timed out"
            ) from None
        url = (await Bot.get_url.aio()).rstrip("/") + "/"
        location = f"{url}?{urlencode({'modal_session_token': session.token, 'token': session.token, 'meet': meet})}"
        return RedirectResponse(location, status_code=303)

    return api


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
            if match := meet_link.search(body):
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
        if url and bot_state.get(f"notified:{url}") == ts:
            bot_state["cursor"] = ts
            continue
        if url:
            try:
                invite(url)
                bot_state[f"notified:{url}"] = ts
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
