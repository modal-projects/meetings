import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)
browser_args = [
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--window-size=1920,1080",
    "--window-position=0,0",
    "--hide-crash-restore-bubble",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--profile-directory=Profile 1",
]


async def start_display():
    process = await asyncio.create_subprocess_exec(
        "Xvfb", ":99", "-screen", "0", "1920x1080x24", "-ac"
    )
    async with asyncio.timeout(10):
        while not os.path.exists("/tmp/.X11-unix/X99"):
            if process.returncode is not None:
                raise RuntimeError("display failed to start")
            await asyncio.sleep(0.1)
    return process


async def read_captions(region, transcript, blocks):
    rows = await region.evaluate("""region => {
        window.meetingsCaptionIds ??= new WeakMap();
        window.meetingsCaptionId ??= 0;
        return [...region.querySelectorAll('.nMcdL')].map(block => {
            if (!window.meetingsCaptionIds.has(block))
                window.meetingsCaptionIds.set(block, ++window.meetingsCaptionId);
            return {
                id: window.meetingsCaptionIds.get(block),
                speaker: block.querySelector('.NWpY1d')?.textContent.trim(),
                text: block.querySelector('.ygicle')?.textContent.trim()
            };
        });
    }""")
    for row in rows:
        if not row["speaker"] or not row["text"]:
            continue
        previous = blocks.get(row["id"])
        if previous is not None and previous["speaker"] == row["speaker"]:
            previous["text"] = row["text"]
        else:
            entry = {"speaker": row["speaker"], "text": row["text"], "t": time.time()}
            transcript.append(entry)
            blocks[row["id"]] = entry


profile_ignore_names = (
    "Singleton*",
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "ShaderCache",
    "DawnWebGPUCache",
    "DawnGraphiteCache",
    "CacheStorage",
    "component_crx_cache",
    "extensions_crx_cache",
    "OptimizationGuidePredictionModels",
    "optimization_guide_model_store",
    "OnDeviceHeadSuggestModel",
    "WasmTtsEngine",
    "CertificateRevocation",
    "Safe Browsing",
    "ActorSafetyLists",
    "hyphen-data",
    "ZxcvbnData",
    "OptimizationHints",
    "Dictionaries",
    "PKIMetadata",
    "Subresource Filter",
    "BrowserMetrics*",
)
profile_ignore = shutil.ignore_patterns(*profile_ignore_names)


async def copy_profile(directory):
    started = time.monotonic()
    await asyncio.to_thread(
        shutil.copytree,
        "/profile/seed",
        directory,
        dirs_exist_ok=True,
        ignore=profile_ignore,
    )
    print(
        json.dumps(
            {
                "event": "profile copy",
                "seconds": round(time.monotonic() - started, 1),
            }
        ),
        flush=True,
    )


async def debug_dump(page, archive, session, stage):
    try:
        directory = Path("/archive/debug")
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        stem = f"{session}-{stage}"
        await page.screenshot(path=str(directory / f"{stem}.png"))
        parts = [await page.locator("body").aria_snapshot()]
        parts.append("frames:\n" + "\n".join(frame.url for frame in page.frames))
        for index, frame in enumerate(page.frames):
            try:
                editors = await frame.evaluate(
                    """() => [...document.querySelectorAll(
                        '[contenteditable="true"], input, textarea, [role="textbox"]'
                    )].map(el => [
                        el.tagName, el.getAttribute('role') || '',
                        el.getAttribute('aria-label') || '',
                        el.getAttribute('placeholder') || '',
                        String(el.isContentEditable),
                        String(el.className).slice(0, 60)
                    ].join('|'))"""
                )
            except Exception:
                editors = ["<eval failed>"]
            parts.append(f"frame {index} {frame.url}:\n" + "\n".join(editors))
        panel = page.get_by_role("complementary", name="Side panel", exact=True)
        if await panel.count():
            parts.append(
                "panel html:\n" + (await panel.evaluate("el => el.outerHTML"))[:8000]
            )
        await asyncio.to_thread(
            (directory / f"{stem}.txt").write_text, "\n\n".join(parts)
        )
        await archive.commit.aio()
    except Exception:
        logger.exception("debug capture failed for %s", session)


class Meetings:
    def status(self, state, status):
        state["status"] = status
        print(json.dumps({"session": state["session"], "status": status}), flush=True)

    def __init__(self, terminate, archive, base_url):
        self.terminate = terminate
        self.archive = archive
        self.base_url = base_url
        self.states = {}
        self.tokens = {}
        self.tasks = {}
        self.api = FastAPI()

        @self.api.get("/")
        async def meeting(request: Request, meet: str, token: str, link: str = ""):
            session = request.headers.get("x-modal-server-session-id")
            if session is None:
                raise HTTPException(status_code=404, detail="meeting not found")
            if session not in self.states:
                self.tokens[session] = token
                self.states[session] = {
                    "session": session,
                    "meet": meet,
                    "url": link
                    or f"{self.base_url}/?{urlencode({'modal_session_token': token, 'token': token, 'meet': meet})}",
                    "started": time.time(),
                    "transcript": [],
                    "chat": [],
                }
                self.status(self.states[session], "joining")
                self.tasks[session] = asyncio.create_task(self.run(session))
            return FileResponse("/meetings/web/meeting.html")

        @self.api.get("/events")
        async def events(request: Request):
            session = request.headers.get("x-modal-server-session-id")
            if session is None or session not in self.states:
                raise HTTPException(status_code=404, detail="meeting not started")

            async def stream():
                previous = ""
                while not await request.is_disconnected():
                    state = self.states[session]
                    payload = json.dumps(state)
                    if payload != previous:
                        previous = payload
                        yield f"data: {payload}\n\n"
                    if state["status"] == "left":
                        break
                    await asyncio.sleep(1)

            return StreamingResponse(stream(), media_type="text/event-stream")

    async def run(self, session):
        state = self.states[session]
        try:
            with tempfile.TemporaryDirectory() as directory:
                await copy_profile(directory)
                async with async_playwright() as playwright:
                    await self.join(playwright, directory, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: meeting failed"
            logger.exception("session %s failed", session)
        finally:
            self.status(state, "left")
            await self.terminate(self.tokens.pop(session))

    async def join(self, playwright, directory, state):
        context = None
        page = None
        stage = "launch"
        try:
            context = await playwright.chromium.launch_persistent_context(
                directory,
                channel="chrome",
                ignore_default_args=["--enable-automation"],
                headless=False,
                no_viewport=True,
                args=browser_args,
                env={**os.environ, "DISPLAY": ":99"},
                permissions=["camera", "microphone"],
            )
            page = await context.new_page()
            warning = page.get_by_role(
                "dialog", name="Camera might be blocked", exact=True
            )
            await page.add_locator_handler(
                warning,
                lambda: warning.get_by_role("button", name="Close", exact=True).click(),
            )
            gemini = page.get_by_role("dialog", name="Gemini is available", exact=True)
            await page.add_locator_handler(
                gemini,
                lambda: gemini.get_by_role(
                    "button", name="Don't show again", exact=True
                ).click(),
            )
            video = page.get_by_role(
                "dialog", name="Others may see your video differently", exact=True
            )
            await page.add_locator_handler(
                video,
                lambda: video.get_by_role("button", name="Got it", exact=True).click(),
            )
            notes_offer = page.get_by_role(
                "dialog", name="More information about Smart Notes.", exact=True
            )
            await page.add_locator_handler(
                notes_offer, lambda: page.keyboard.press("Escape"), no_wait_after=True
            )
            missing = page.get_by_role("dialog", name=re.compile(r"not found", re.I))
            await page.add_locator_handler(
                missing,
                lambda: missing.get_by_role("button", name="Close", exact=True).click(),
            )
            stage = "prejoin"
            await page.goto(state["meet"], wait_until="domcontentloaded")
            join_now = page.get_by_role("button", name=re.compile(r"^Join now\b"))
            ask = page.get_by_role("button", name="Ask to join", exact=True)
            switch = page.get_by_role(
                "button", name=re.compile(r"^Switch here|^Rejoin")
            )
            enter = join_now.or_(ask).or_(switch).first
            for attempt in range(2):
                try:
                    await enter.wait_for(timeout=90000)
                    break
                except PlaywrightTimeoutError:
                    if attempt:
                        raise
                    await page.reload(wait_until="domcontentloaded")
            mic = page.get_by_role("button", name=re.compile(r"^Turn off microphone"))
            muted = page.get_by_role("button", name=re.compile(r"^Turn on microphone"))
            try:
                await mic.wait_for(timeout=15000)
                await mic.click()
                await muted.wait_for(timeout=15000)
            except PlaywrightTimeoutError:
                pass
            if await join_now.is_visible():
                await join_now.click()
            elif await ask.is_visible():
                await ask.click()
                self.status(state, "waiting")
            else:
                await enter.click()
            stage = "admission"
            leave = page.get_by_role("button", name=re.compile(r"^Leave call\b"))
            denied = page.get_by_role(
                "heading", name=re.compile(r"You can't join|denied|not allowed", re.I)
            )
            joined = page.get_by_text(re.compile(r"You have joined the call\."))
            admission = leave.or_(joined).or_(denied).first
            await admission.wait_for(timeout=600000)
            if not await leave.count() and not await joined.count():
                raise RuntimeError("meeting admission denied")
            stage = "microphone"
            try:
                await mic.or_(muted).first.wait_for(timeout=15000)
                if await mic.count():
                    await mic.click()
                    await muted.wait_for(timeout=15000)
            except PlaywrightTimeoutError:
                pass
            self.status(state, "in")
            entered = time.monotonic()
            stage = "captions"
            captions = page.get_by_role("region", name="Captions", exact=True)
            for attempt in range(3):
                try:
                    if not await captions.count():
                        await page.get_by_role(
                            "button", name="Captions and translation", exact=True
                        ).click(timeout=5000)
                        await (
                            page.get_by_role(
                                "dialog", name="Captions and translation", exact=True
                            )
                            .get_by_role("button", name="Turn on captions", exact=True)
                            .click(timeout=5000)
                        )
                    await captions.wait_for(state="attached", timeout=5000)
                    break
                except PlaywrightTimeoutError:
                    if attempt == 2:
                        raise
            stage = "participants"
            blocks = {}
            seen_others = False
            alone_since = None
            flushed = screened = time.monotonic()
            while True:
                try:
                    await read_captions(captions, state["transcript"], blocks)
                except Exception:
                    logger.exception("session %s: poll failed", state["session"])
                try:
                    texts = await page.get_by_role(
                        "button", name=re.compile(r"^People\b")
                    ).all_inner_texts()
                except Exception:
                    texts = []
                counts = [
                    int(match[1])
                    for text in texts
                    if (match := re.search(r"(\d+)\s*$", text))
                ]
                if counts:
                    seen_others = seen_others or any(count > 1 for count in counts)
                    alone = 1 in counts and (
                        seen_others or time.monotonic() - entered >= 900
                    )
                else:
                    alone = not await page.get_by_role(
                        "button", name=re.compile(r"^Leave call\b")
                    ).count()
                if not alone:
                    alone_since = None
                elif alone_since is None:
                    alone_since = time.monotonic()
                elif time.monotonic() - alone_since >= 60:
                    try:
                        async with kimi.client() as client:
                            await kimi.recap(client)
                    except Exception:
                        logger.exception("session %s: recap failed", state["session"])
                    break
                await asyncio.sleep(0.5)
        except Exception:
            logger.exception("session %s failed during %s", state["session"], stage)
            if page is not None:
                await debug_dump(page, self.archive, state["session"], stage)
            raise
        finally:
            if context is not None:
                await context.close()

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
