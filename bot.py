import asyncio
import os


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
