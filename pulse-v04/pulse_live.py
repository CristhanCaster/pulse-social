"""Small-room WebRTC signaling for Pulse Live."""
import base64
import hashlib
import hmac
import os
import secrets
import time
from threading import Lock

rooms = {}
lock = Lock()
lease = 25
viewer_limit = 4


def ice_servers(uid):
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    urls = os.getenv("PULSE_TURN_URLS", "").split()
    secret = os.getenv("PULSE_TURN_SECRET", "")
    if urls and secret:
        username = f"{int(time.time()) + 3600}:{uid}"
        credential = base64.b64encode(
            hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
        ).decode()
        servers.append({"urls": urls, "username": username, "credential": credential})
    return servers


def visible(c, room, uid):
    host = room["host"]
    if host == uid:
        return True
    return bool(c.execute(
        """SELECT 1 FROM users WHERE id=%s AND banned=0
        AND (%s OR EXISTS(SELECT 1 FROM follows WHERE follower=%s AND followed=%s))""",
        (host, room["visibility"] == "public", uid, host),
    ).fetchone())


def summary(room):
    return {key: room[key] for key in ("id", "host", "username", "title", "visibility")}


def handle(app, c, me, action, data=None):
    if me["banned"]:
        return app.fail("Account suspended", 403)
    uid = me["id"]
    if data is not None and app.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
        return app.fail("Invalid request origin", 403)
    with lock:
        status, response = dispatch(c, me, uid, action, data or {})
    return app.send(status, response)


def error(message, status=400):
    return status, {"error": message}


def dispatch(c, me, uid, action, data):
    now = time.monotonic()
    for room_id, room in list(rooms.items()):
        if room["updated"] < now - lease:
            del rooms[room_id]
            continue
        for viewer, peer in list(room["peers"].items()):
            if peer["updated"] < now - lease:
                del room["peers"][viewer]

    if action == "list":
        return 200, {"rooms": [summary(room) for room in reversed(rooms.values())
                               if visible(c, room, uid)]}
    if action == "start":
        title = data.get("title")
        visibility = data.get("visibility")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            return error("Enter a title up to 100 characters")
        if visibility not in ("public", "followers"):
            return error("Choose a stream audience")
        if any(room["host"] == uid for room in rooms.values()):
            return error("You already have an active broadcast", 409)
        if len(rooms) >= 50:
            return error("Pulse Live is at capacity", 503)
        room_id = secrets.token_hex(12)
        rooms[room_id] = dict(id=room_id, host=uid, username=me["username"],
                              title=title.strip(), visibility=visibility, updated=now,
                              peers={}, chat=[], next_message=1, muted=set(), last_chat={})
        return 200, {"room": summary(rooms[room_id]), "ice": ice_servers(uid)}

    room_id = data.get("room")
    room = rooms.get(room_id) if isinstance(room_id, str) else None
    if not room or not visible(c, room, uid):
        return error("This stream ended or is unavailable to your account", 404)
    host = room["host"] == uid
    peers = room["peers"]
    if action == "join" and not host:
        if uid not in peers and len(peers) >= viewer_limit:
            return error("This broadcast is full", 409)
        peers[uid] = {"id": secrets.token_hex(12), "updated": now, "offer": None, "answer": None}
        return 200, {"room": summary(room), "peer": peers[uid]["id"], "ice": ice_servers(uid)}
    if action == "end" and host:
        del rooms[room_id]
        return 200, {}
    peer = peers.get(uid)
    if not host and (not peer or data.get("peer") != peer["id"]):
        return error("Your viewing session ended. Rejoin the stream.", 409)
    if action == "leave" and not host:
        del peers[uid]
        return 200, {}
    if action == "poll":
        if host:
            room["updated"] = now
            eligible = c.execute(
                """SELECT id FROM users WHERE id=ANY(%s) AND banned=0 AND
                (%s OR EXISTS(SELECT 1 FROM follows WHERE follower=users.id AND followed=%s))""",
                (list(peers), room["visibility"] == "public", uid),
            ).fetchall() if peers else []
            allowed = {row["id"] for row in eligible}
            for viewer in peers.keys() - allowed:
                del peers[viewer]
            signals = [{"id": p["id"], "answer": p["answer"]} for p in peers.values()]
        else:
            peer["updated"] = now
            signals = peer["offer"]
        return 200, {"signals": signals, "chat": list(room["chat"]), "muted": uid in room["muted"]}
    if action in ("offer", "answer"):
        sdp = data.get("sdp")
        if not isinstance(sdp, str) or not 1 <= len(sdp) <= 24000 or not sdp.startswith("v=0"):
            return error("Invalid WebRTC description")
        if action == "offer" and host:
            peer = next((p for p in peers.values() if p["id"] == data.get("peer")), None)
        elif action != "answer" or host:
            return error("Invalid signal sender", 403)
        if not peer:
            return error("Viewer disconnected", 409)
        peer[action] = sdp
        return 200, {}
    if action == "chat":
        body = data.get("body")
        if uid in room["muted"]:
            return error("You are muted in this chat", 403)
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 280:
            return error("Enter a message up to 280 characters")
        room["last_chat"] = {user: sent for user, sent in room["last_chat"].items() if sent > now - 2}
        if uid in room["last_chat"]:
            return error("Wait two seconds between messages", 429)
        room["last_chat"][uid] = now
        room["chat"] = room["chat"][-99:] + [dict(id=room["next_message"], user=uid,
                                                username=me["username"], body=body.strip())]
        room["next_message"] += 1
        return 200, {}
    if host and action in ("delete", "mute"):
        message = next((m for m in room["chat"] if m["id"] == data.get("message")), None)
        if not message:
            return error("Message expired", 404)
        if action == "mute" and message["user"] != uid:
            room["muted"].add(message["user"])
        if action == "delete":
            room["chat"].remove(message)
        return 200, {}
    return error("Unknown live action", 404)
