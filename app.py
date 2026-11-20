import asyncio
from pathlib import Path

import modal

root = Path(__file__).resolve().parent
meeting_s = 4 * 3600
app = modal.App("meetings")
profile = modal.Volume.from_name(
    "meetings-chrome-profile", create_if_missing=True, environment_name="demos"
)
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("curl", "fonts-dejavu-core", "xvfb", "x11vnc", "novnc", "websockify")
    .uv_sync(uv_project_dir=str(root), extra_options="--no-dev")
    .run_commands(
        "curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /chrome.deb && apt-get update && apt-get install -y /chrome.deb && rm /chrome.deb",
        "useradd --create-home chrome",
    )
    .add_local_python_source("app", "bot")
)


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
