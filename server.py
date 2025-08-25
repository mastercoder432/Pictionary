import json, secrets, time, re
from typing import Dict, List, Optional
from collections import defaultdict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ===== Config =====
ALLOWED_ORIGINS = ["*"]  # tighten in production (e.g. your domain)
MAX_MSG_BYTES = 16_384
DRAW_RATE_PER_SEC = 160
GUESS_RATE_PER_SEC = 8
ROUND_CLEAR_ON_CORRECT = True

# Basic, editable profanity list. Add more entries as needed.
BAD_WORDS = {
    "badword", "dummy", "stupid"  # <-- extend this set
}
BAD_WORDS_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in BAD_WORDS) + r")\b", re.IGNORECASE) if BAD_WORDS else None

def censor(text: str) -> str:
    if not text or not BAD_WORDS_RE:
        return text
    return BAD_WORDS_RE.sub("***", text)

WORDS = [
    "apple","cat","house","tree","car","dog","star","flower",
    "plane","bottle","phone","chair","sun","moon","pizza","fish",
    "elephant","guitar","book","rocket","computer","ball","mountain",
    "beach","umbrella","camera","butterfly","football"
]

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health():
    return {"ok": True}

# ===== State =====
class Client:
    def __init__(self, ws: WebSocket, name: str):
        self.ws = ws
        self.name = name
        self.is_drawer = False
        self.is_admin = False
        self.last_tick = int(time.time())
        self.draw_count = 0
        self.guess_count = 0

class RoomState:
    def __init__(self):
        self.clients: List[Client] = []
        self.drawer_index: int = 0
        self.current_word: str = ""
        self.mode: str = "random"  # "random" or "choice"
        self.locked: bool = False  # prevent new joins when True

rooms: Dict[str, RoomState] = defaultdict(RoomState)

# ===== Helpers =====
async def safe_send(ws: WebSocket, payload: dict):
    await ws.send_text(json.dumps(payload))

async def broadcast(room: RoomState, payload: dict, exclude: Optional[Client] = None):
    data = json.dumps(payload)
    dead = []
    for c in list(room.clients):
        if exclude is not None and c is exclude:
            continue
        try:
            await c.ws.send_text(data)
        except Exception:
            dead.append(c)
    for d in dead:
        if d in room.clients:
            room.clients.remove(d)

def choose_word() -> str:
    import random
    return random.choice(WORDS)

def choose_options(n=3) -> List[str]:
    import random
    return random.sample(WORDS, k=n)

def tick_and_rate_limit(client: Client):
    now = int(time.time())
    if now != client.last_tick:
        client.last_tick = now
        client.draw_count = 0
        client.guess_count = 0

async def send_players(room: RoomState):
    players = [{"name": c.name, "drawer": c.is_drawer, "admin": c.is_admin} for c in room.clients]
    await broadcast(room, {"type":"players","players":players})

async def send_room_settings(room: RoomState, to: Optional[Client]=None):
    payload = {"type":"room_settings","mode":room.mode,"locked":room.locked}
    if to:
        await safe_send(to.ws, payload)
    else:
        await broadcast(room, payload)

async def ensure_at_least_one_admin(room: RoomState):
    if room.clients and not any(c.is_admin for c in room.clients):
        room.clients[0].is_admin = True

def unique_name(room: RoomState, desired: str) -> str:
    if not any(c.name == desired for c in room.clients):
        return desired
    base = desired
    n = 2
    while any(c.name == f"{base} ({n})" for c in room.clients):
        n += 1
    return f"{base} ({n})"

async def start_round(room: RoomState):
    if len(room.clients) < 2:
        await send_players(room)
        for c in room.clients:
            await safe_send(c.ws, {"type":"waiting"})
        return

    room.drawer_index %= len(room.clients)
    for i, c in enumerate(room.clients):
        c.is_drawer = (i == room.drawer_index)

    drawer = room.clients[room.drawer_index]
    for c in room.clients:
        await safe_send(c.ws, {
            "type":"role",
            "role":"drawer" if c.is_drawer else "guesser",
            "drawerName": drawer.name
        })

    room.current_word = ""
    if room.mode == "random":
        room.current_word = choose_word()
        await safe_send(drawer.ws, {"type":"secret_word","word":room.current_word})
    else:  # choice
        options = choose_options(3)
        await safe_send(drawer.ws, {"type":"word_options","options":options})
        await safe_send(drawer.ws, {"type":"system","text":"Choose a word from the options above before drawing."})

    await send_players(room)
    await send_room_settings(room)

def sanitize_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name: name = "Player"
    return name[:24]

# ===== WebSocket =====
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    room_code = None
    client_obj: Optional[Client] = None
    try:
        while True:
            raw = await ws.receive_text()
            if len(raw.encode("utf-8")) > MAX_MSG_BYTES:
                await safe_send(ws, {"type":"error","message":"Message too large"})
                continue
            try:
                data = json.loads(raw)
            except Exception:
                await safe_send(ws, {"type":"error","message":"Invalid JSON"})
                continue

            msg_type = data.get("type")

            # JOIN
            if msg_type == "join":
                candidate_room = str(data.get("room","")).upper().strip() or secrets.token_hex(2).upper()
                name = sanitize_name(data.get("name"))
                state = rooms[candidate_room]

                if state.locked:
                    await safe_send(ws, {"type":"error","message":"Room is locked by admin"})
                    continue

                name = unique_name(state, name)  # ensure no dup names in room
                client_obj = Client(ws, name)
                room_code = candidate_room
                state.clients.append(client_obj)
                await ensure_at_least_one_admin(state)

                await safe_send(ws, {"type":"joined","room":room_code,"you":name})
                await send_room_settings(state, to=client_obj)
                await send_players(state)
                await broadcast(state, {"type":"system","text":f"{name} joined."})
                await start_round(state)
                continue

            # Must join first
            if not room_code or not client_obj:
                await safe_send(ws, {"type":"error","message":"Join a room first"})
                continue

            state = rooms[room_code]
            tick_and_rate_limit(client_obj)

            # DRAW (drawer only; supports color/size/eraser; blocked in 'choice' until word chosen)
            if msg_type == "draw":
                if not client_obj.is_drawer:
                    continue
                if state.mode == "choice" and not state.current_word:
                    continue
                client_obj.draw_count += 1
                if client_obj.draw_count > DRAW_RATE_PER_SEC:
                    continue
                x = data.get("x"); y = data.get("y"); drag = bool(data.get("drag"))
                color = data.get("color", "#111")
                size = data.get("size", 4)
                erase = bool(data.get("erase", False))
                if not (isinstance(x,(int,float)) and isinstance(y,(int,float))):
                    continue
                # clamp brush size
                try:
                    size = int(size)
                except Exception:
                    size = 4
                size = max(1, min(size, 40))
                await broadcast(state, {"type":"draw","x":x,"y":y,"drag":drag,"color":color,"size":size,"erase":erase}, exclude=client_obj)

            # CLEAR (drawer only)
            elif msg_type == "clear":
                if client_obj.is_drawer:
                    await broadcast(state, {"type":"clear"})

            # CHAT (only guessers can chat, drawer blocked)
            elif msg_type == "chat":
                if client_obj.is_drawer:
                    await safe_send(ws, {"type":"system","text":"Drawer cannot chat during their turn."})
                    continue
                text = str(data.get("text",""))[:200]
                if text:
                    text = censor(text)
                    await broadcast(state, {"type":"chat","from":client_obj.name,"text":text})

            # GUESS (only guessers can guess; hide 'guesses:' label)
            elif msg_type == "guess":
                if client_obj.is_drawer:
                    await safe_send(ws, {"type":"system","text":"Drawer cannot guess."})
                    continue
                client_obj.guess_count += 1
                if client_obj.guess_count > GUESS_RATE_PER_SEC:
                    continue
                guess_raw = str(data.get("text","")).strip()
                if not guess_raw:
                    continue
                guess_sanitized = censor(guess_raw)
                # Show guess text in chat without "guesses:"
                await broadcast(state, {"type":"chat","from":client_obj.name,"text":guess_sanitized})
                # Check correctness on original (un-censored) guess
                if state.current_word and guess_raw.lower() == state.current_word.lower():
                    await broadcast(state, {"type":"correct","player":client_obj.name,"word":state.current_word})
                    if len(state.clients) >= 2:
                        state.drawer_index = (state.drawer_index + 1) % len(state.clients)
                        state.current_word = ""
                        if ROUND_CLEAR_ON_CORRECT:
                            await broadcast(state, {"type":"clear"})
                        await start_round(state)
                else:
                    await safe_send(ws, {"type":"feedback","result":"incorrect"})

            # PICK WORD (choice mode; drawer only)
            elif msg_type == "pick_word":
                if not client_obj.is_drawer:
                    continue
                word = str(data.get("word","")).strip().lower()
                if word and word in WORDS:
                    state.current_word = word
                    await safe_send(client_obj.ws, {"type":"secret_word","word":state.current_word})

            # ADMIN COMMANDS (first joiner is admin; can add/remove admins)
            elif msg_type == "admin":
                if not client_obj.is_admin:
                    await safe_send(ws, {"type":"error","message":"Admin only"})
                    continue
                action = data.get("action")
                if action == "lock":
                    state.locked = True
                    await send_room_settings(state)
                elif action == "unlock":
                    state.locked = False
                    await send_room_settings(state)
                elif action == "mode":
                    new_mode = str(data.get("mode","")).lower()
                    if new_mode in ("random","choice"):
                        state.mode = new_mode
                        await send_room_settings(state)
                        state.current_word = ""
                        await broadcast(state, {"type":"clear"})
                        await start_round(state)
                elif action == "kick":
                    target_name = str(data.get("name","")).strip()
                    target = next((c for c in state.clients if c.name == target_name), None)
                    if target:
                        await safe_send(target.ws, {"type":"system","text":"You were kicked by admin."})
                        try: await target.ws.close()
                        except Exception: pass
                elif action == "make_admin":
                    target_name = str(data.get("name","")).strip()
                    target = next((c for c in state.clients if c.name == target_name), None)
                    if target:
                        target.is_admin = True
                        await send_players(state)
                elif action == "revoke_admin":
                    target_name = str(data.get("name","")).strip()
                    target = next((c for c in state.clients if c.name == target_name), None)
                    if target:
                        target.is_admin = False
                        await ensure_at_least_one_admin(state)  # keep at least one admin
                        await send_players(state)

    except WebSocketDisconnect:
        pass
    finally:
        if room_code and client_obj:
            state = rooms.get(room_code)
            if state and client_obj in state.clients:
                name = client_obj.name
                idx = state.clients.index(client_obj)
                was_drawer = client_obj.is_drawer
                state.clients.remove(client_obj)

                await ensure_at_least_one_admin(state)

                if state.clients:
                    if idx < state.drawer_index or state.drawer_index >= len(state.clients):
                        state.drawer_index = state.drawer_index % len(state.clients)
                    if was_drawer:
                        state.drawer_index = state.drawer_index % len(state.clients)
                        state.current_word = ""
                        await broadcast(state, {"type":"clear"})
                        await start_round(state)
                    await broadcast(state, {"type":"system","text":f"{name} left."})
                    await send_players(state)
                else:
                    rooms.pop(room_code, None)
