"""Headless run: python run_pipeline.py [image_or_text]"""
import asyncio, sys, time, mimetypes, pathlib
from dotenv import load_dotenv
load_dotenv("my_agent/.env")
from google.adk.runners import InMemoryRunner
from google.genai import types
from my_agent.agent import root_agent

async def main(arg):
    r = InMemoryRunner(agent=root_agent, app_name="logi")
    s = await r.session_service.create_session(app_name="logi", user_id="u")
    p = pathlib.Path(arg)
    if p.exists():
        parts = [types.Part.from_bytes(data=p.read_bytes(), mime_type=mimetypes.guess_type(p)[0]),
                 types.Part(text="Process this order.")]
    else:
        parts = [types.Part(text=arg)]
    t0 = time.time()
    async for e in r.run_async(user_id="u", session_id=s.id, new_message=types.Content(role="user", parts=parts)):
        for fc in e.get_function_calls() or []:
            print(f"{time.time()-t0:6.1f}s [{e.author}] -> {fc.name}({dict(fc.args)})")
        for fr in e.get_function_responses() or []:
            print(f"{time.time()-t0:6.1f}s [{e.author}] <- {str(fr.response)[:150]}")
        if e.is_final_response() and e.content and e.content.parts and e.author == "fulfilment":
            print("\n" + e.content.parts[0].text)
    print(f"\nTOTAL: {time.time()-t0:.1f}s")
asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "samples/order1.png"))
