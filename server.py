import json, secrets, time
from typing import Dict, List, Optional
from collections import defaultdict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ===== Config =====
ALLOWED_ORIGINS = ["*"]  # tighten in production
MAX_MSG_BYTES = 16_384
DRAW_RATE_PER_SEC = 120
GUESS_RATE_PER_SEC = 8
ROUND_CLEAR_ON_CORRECT = True

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

async def assign_admin_if_needed(room: RoomState):
    # ensure exactly one admin: first client in the list gets admin if none
    if room.clients and not any(c.is_admin for c in room.clients):
        room.clients[0].is_admin = True
    # if somehow multiple admins existed, keep only the first as admin
    seen = False
    for c in room.clients:
        if c.is_admin and not seen:
            seen = True
        else:
            c.is_admin = False

async def rotate_and_start_round(room: RoomState):
    if len(room.clients) < 2:
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
    else:
        options = choose_options(3)
        # tell drawer to choose, and block drawing until chosen
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

                client_obj = Client(ws, name)
                room_code = candidate_room
                state.clients.append(client_obj)
                await assign_admin_if_needed(state)
                await safe_send(ws, {"type":"joined","room":room_code,"you":name})
                await send_room_settings(state, to=client_obj)
                await broadcast(state, {"type":"system","text":f"{name} joined."})

                await rotate_and_start_round(state)
                continue

            # Must join first
            if not room_code or not client_obj:
                await safe_send(ws, {"type":"error","message":"Join a room first"})
                continue

            state = rooms[room_code]
            tick_and_rate_limit(client_obj)

            # DRAW (drawer only; blocked in 'choice' until word chosen)
            if msg_type == "draw":
                if not client_obj.is_drawer:
                    continue
                if state.mode == "choice" and not state.current_word:
                    continue
                client_obj.draw_count += 1
                if client_obj.draw_count > DRAW_RATE_PER_SEC:
                    continue
                x = data.get("x"); y = data.get("y"); drag = bool(data.get("drag"))
                if not (isinstance(x,(int,float)) and isinstance(y,(int,float))):
                    continue
                await broadcast(state, {"type":"draw","x":x,"y":y,"drag":drag}, exclude=client_obj)

            # CLEAR (drawer only)
            elif msg_type == "clear":
                if client_obj.is_drawer:
                    await broadcast(state, {"type":"clear"})

            # CHAT (everyone)
            elif msg_type == "chat":
                text = str(data.get("text",""))[:200]
                if text:
                    await broadcast(state, {"type":"chat","from":client_obj.name,"text":text})

            # GUESS (everyone EXCEPT drawer)
            elif msg_type == "guess":
                if client_obj.is_drawer:
                    # drawer cannot guess
                    await safe_send(ws, {"type":"system","text":"Drawer cannot guess."})
                    continue
                client_obj.guess_count += 1
                if client_obj.guess_count > GUESS_RATE_PER_SEC:
                    continue
                guess_raw = str(data.get("text","")).strip()
                if not guess_raw:
                    continue
                await broadcast(state, {"type":"chat","from":client_obj.name,"text":f"guesses: {guess_raw}"})
                if state.current_word and guess_raw.lower() == state.current_word.lower():
                    await broadcast(state, {"type":"correct","player":client_obj.name,"word":state.current_word})
                    if len(state.clients) >= 2:
                        state.drawer_index = (state.drawer_index + 1) % len(state.clients)
                        state.current_word = ""
                        if ROUND_CLEAR_ON_CORRECT:
                            await broadcast(state, {"type":"clear"})
                        await rotate_and_start_round(state)
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

            # ADMIN COMMANDS (auto-admin = first joiner)
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
                        await rotate_and_start_round(state)
                elif action == "kick":
                    target_name = str(data.get("name","")).strip()
                    target = next((c for c in state.clients if c.name == target_name), None)
                    if target:
                        await safe_send(target.ws, {"type":"system","text":"You were kicked by admin."})
                        try:
                            await target.ws.close()
                        except Exception:
                            pass

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

                # re-affirm single admin (first in list) after a leave
                await assign_admin_if_needed(state)

                if state.clients:
                    if idx < state.drawer_index or state.drawer_index >= len(state.clients):
                        state.drawer_index = state.drawer_index % len(state.clients)
                    if was_drawer:
                        state.drawer_index = state.drawer_index % len(state.clients)
                        state.current_word = ""
                        await broadcast(state, {"type":"clear"})
                        await rotate_and_start_round(state)
                    await broadcast(state, {"type":"system","text":f"{name} left."})
                    await send_players(state)
                else:
                    rooms.pop(room_code, None)
