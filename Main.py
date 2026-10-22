import hashlib
import json
import math
from pathlib import Path
import socket
import struct
import threading
import time
import uuid

HOST = "0.0.0.0"
PORT = 25565 # def mc port
PROTOCOL_VERSION = 47
SERVER_VERSION = "1.8.9"
MOTD = "A Python Server"
MAX_PLAYERS = 20

FIXED_TIME = 1000 # making the time to 1000 alawys
CHUNK_RADIUS = 14

PLAYERS = {}
PLAYERS_LOCK = threading.RLock()
SEND_LOCK = threading.RLock()
WORLD_LOCK = threading.RLock()
WORLD_FILE = Path(__file__).with_name("world_data.json")
CHUNK_BLOCKS = {}

NEXT_ENTITY_ID = 1
ENTITY_ID_LOCK = threading.Lock()

def allocate_entity_id():
    global NEXT_ENTITY_ID

    with ENTITY_ID_LOCK:
        entity_id = NEXT_ENTITY_ID
        NEXT_ENTITY_ID += 1
        return entity_id

def varint(value):
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint_bytes(data, offset=0):
    result = 0
    shift = 0
    while True:
        if offset >= len(data) or shift >= 35:
            raise ValueError("Invalid VarInt")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            if result & 0x80000000:
                result -= 0x100000000
            return result, offset
        shift += 7


def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise ConnectionError("Client disconnected")
        data.extend(part)
    return bytes(data)


def recv_varint(sock):
    result = 0
    shift = 0
    while True:
        byte = recv_exact(sock, 1)[0]
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            if result & 0x80000000:
                result -= 0x100000000
            return result
        shift += 7
        if shift >= 35:
            raise ValueError("Invalid VarInt")


def recv_packet(sock):
    length = recv_varint(sock)
    if length < 0 or length > 2_000_000:
        raise ValueError("Invalid packet length")
    return recv_exact(sock, length)


def read_string(data, offset):
    length, offset = read_varint_bytes(data, offset)
    if length < 0 or offset + length > len(data):
        raise ValueError("Invalid string")
    return data[offset:offset + length].decode("utf-8"), offset + length


def packet(packet_id, payload=b""):
    body = varint(packet_id) + payload
    return varint(len(body)) + body


def send_packet(sock, packet_id, payload=b""):
    frame = packet(packet_id, payload)
    with SEND_LOCK:
        sock.sendall(frame)


def mc_string(value):
    encoded = value.encode("utf-8")
    return varint(len(encoded)) + encoded


def offline_uuid(username):
    raw = bytearray(hashlib.md5(("OfflinePlayer:" + username).encode("utf-8")).digest())
    raw[6] = (raw[6] & 0x0F) | 0x30
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def uuid_bytes(uuid_text):
    return uuid.UUID(uuid_text).bytes


def send_status(sock):
    with PLAYERS_LOCK:
        online = list(PLAYERS.values())
    response = {
        "version": {"name": SERVER_VERSION, "protocol": PROTOCOL_VERSION},
        "players": {
            "max": MAX_PLAYERS,
            "online": len(online),
            "sample": [{"name": p["name"], "id": p["uuid"]} for p in online[:12]],
        },
        "description": {"text": MOTD},
    }
    send_packet(sock, 0, mc_string(json.dumps(response, separators=(",", ":"))))
    try:
        body = recv_packet(sock)
        packet_id, offset = read_varint_bytes(body)
        if packet_id == 1 and len(body) >= offset + 8:
            send_packet(sock, 1, body[offset:offset + 8])
    except (ConnectionError, OSError, ValueError):
        pass


def default_block_state(y):
    if y == 0:
        return 7, 0       # bedrock
    if 1 <= y <= 3:
        return 3, 0       # dirt
    if y == 4:
        return 2, 0       # grass block
    return 0, 0           # air


def load_world_data():
    if not WORLD_FILE.exists():
        return
    try:
        raw = json.loads(WORLD_FILE.read_text(encoding="utf-8"))
        loaded = {}
        for chunk_key, blocks in raw.get("chunks", {}).items():
            cx_text, cz_text = chunk_key.split(",", 1)
            chunk_key_tuple = (int(cx_text), int(cz_text))
            overrides = {}
            for block_key, state in blocks.items():
                lx_text, y_text, lz_text = block_key.split(",", 2)
                lx, y, lz = int(lx_text), int(y_text), int(lz_text)
                block_id, metadata = int(state[0]), int(state[1])
                if 0 <= lx < 16 and 0 <= lz < 16 and 0 <= y < 256:
                    if 0 <= block_id <= 4095 and 0 <= metadata <= 15:
                        overrides[(lx, y, lz)] = (block_id, metadata)
            if overrides:
                loaded[chunk_key_tuple] = overrides
        with WORLD_LOCK:
            CHUNK_BLOCKS.clear()
            CHUNK_BLOCKS.update(loaded)
        count = sum(len(b) for b in loaded.values())
        print(f"[world] Loaded {count} saved block changes from {WORLD_FILE.name}")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"[world] Could not load {WORLD_FILE.name}: {exc}")


def save_world_data():
    try:
        with WORLD_LOCK:
            chunks_json = {}
            for (cx, cz), overrides in CHUNK_BLOCKS.items():
                if not overrides:
                    continue
                chunks_json[f"{cx},{cz}"] = {
                    f"{lx},{y},{lz}": [block_id, metadata]
                    for (lx, y, lz), (block_id, metadata) in overrides.items()
                }
            document = {"format": 1, "chunks": chunks_json}
            temporary = WORLD_FILE.with_suffix(WORLD_FILE.suffix + ".tmp")
            temporary.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
            temporary.replace(WORLD_FILE)
    except OSError as exc:
        print(f"[world] Could not save block changes: {exc}")

def set_block_state(x, y, z, block_id, metadata=0):
    if not (0 <= y < 256):
        return False
    if not (0 <= block_id <= 4095):
        return False
    metadata &= 0x0F
    cx = math.floor(x / 16)
    cz = math.floor(z / 16)
    lx = x - cx * 16
    lz = z - cz * 16
    key = (lx, y, lz)
    desired = (block_id, metadata)
    base_state = default_block_state(y)
    with WORLD_LOCK:
        overrides = CHUNK_BLOCKS.setdefault((cx, cz), {})
        if desired == base_state:
            overrides.pop(key, None)
        else:
            overrides[key] = desired
        if not overrides:
            CHUNK_BLOCKS.pop((cx, cz), None)
    save_world_data()
    return True


def get_block_state(x, y, z):
    if not (0 <= y < 256):
        return 0, 0
    cx = math.floor(x / 16)
    cz = math.floor(z / 16)
    lx = x - cx * 16
    lz = z - cz * 16
    with WORLD_LOCK:
        override = CHUNK_BLOCKS.get((cx, cz), {}).get((lx, y, lz))
    return override if override is not None else default_block_state(y)


def unpack_block_position(data, offset):
    if offset + 8 > len(data):
        raise ValueError("Truncated block position")
    value = struct.unpack_from(">Q", data, offset)[0]
    x = (value >> 38) & 0x3FFFFFF
    y = (value >> 26) & 0xFFF
    z = value & 0x3FFFFFF
    if x & 0x2000000:
        x -= 1 << 26
    if y & 0x800:
        y -= 1 << 12
    if z & 0x2000000:
        z -= 1 << 26
    return x, y, z, offset + 8


def make_chunk_data(chunk_x, chunk_z):
    with WORLD_LOCK:
        overrides = dict(CHUNK_BLOCKS.get((chunk_x, chunk_z), {}))

    sections = {0}
    for (_lx, y, _lz) in overrides:
        if 0 <= y < 256:
            sections.add(y >> 4)
    section_mask = sum(1 << section_y for section_y in sections)
    data = bytearray()

    for section_y in sorted(sections):
        block_states = bytearray(4096 * 2)
        for local_y in range(16):
            world_y = section_y * 16 + local_y
            for local_z in range(16):
                for local_x in range(16):
                    state_pair = overrides.get((local_x, world_y, local_z))
                    if state_pair is None:
                        block_id, metadata = default_block_state(world_y)
                    else:
                        block_id, metadata = state_pair
                    state = (block_id << 4) | (metadata & 0x0F)
                    index = (local_y << 8) | (local_z << 4) | local_x
                    block_states[index * 2] = state & 0xFF
                    block_states[index * 2 + 1] = (state >> 8) & 0xFF
        data.extend(block_states)
        data.extend(bytes(2048))
        data.extend(b"\xff" * 2048)

    data.extend(bytes([1]) * 256)
    return section_mask, bytes(data)


def send_chunk(sock, chunk_x, chunk_z):
    section_mask, chunk_data = make_chunk_data(chunk_x, chunk_z)
    payload = (
        struct.pack(">iiBH", chunk_x, chunk_z, 1, section_mask)
        + varint(len(chunk_data))
        + chunk_data
    )
    send_packet(sock, 0x21, payload)


def send_block_change_to_all(x, y, z):
    block_id, metadata = get_block_state(x, y, z)
    state = (block_id << 4) | (metadata & 0x0F)
    payload = packed_block_position(x, y, z) + varint(state)
    with PLAYERS_LOCK:
        players = list(PLAYERS.values())
    for player in players:
        try:
            send_packet(player["conn"], 0x23, payload)
        except OSError:
            pass

load_world_data()

def packed_block_position(x, y, z):
    value = ((x & 0x3FFFFFF) << 38) | ((y & 0xFFF) << 26) | (z & 0x3FFFFFF)
    return struct.pack(">Q", value)


def send_time_frozen(sock):
    send_packet(sock, 0x03, struct.pack(">qq", 0, -FIXED_TIME))

def send_message(player, message, position=1):
    component = json.dumps(
        {"text": str(message)},
        ensure_ascii=False,
        separators=(",", ":")
    )

    payload = mc_string(component) + bytes([position])
    send_packet(player["conn"], 0x02, payload)

def send_world(sock, player):
    join_payload = (
        struct.pack(">iBbBB", player["entity_id"], 1, 0, 2, 255)
        + mc_string("flat")
        + b"\x00"
    )
    send_packet(sock, 0x01, join_payload)
    send_packet(sock, 0x3F, mc_string("MC|Brand") + mc_string("Python"))
    send_packet(sock, 0x05, packed_block_position(0, 5, 0))
    send_time_frozen(sock)
    
    send_packet(sock, 0x08, struct.pack(">dddffB", 0.5, 5.0, 0.5, 0.0, 0.0, 0))
    send_message(player,"Loading SomeThing")
    stream_chunks(sock, player, 0.5, 0.5)
    send_packet(sock, 0x3F, mc_string("MC|Brand") + mc_string("Python"))
    send_message(player,"Fully Loaded")

def add_player_info_payload(player):
    return (
        varint(0)
        + varint(1)
        + uuid_bytes(player["uuid"])
        + mc_string(player["name"])
        + varint(0)
        + varint(1)
        + varint(0)
        + b"\x00"
    )

def remove_player_info_payload(player_uuid):
    return varint(4) + varint(1) + uuid_bytes(player_uuid)

def angle_byte(degrees):
    return int(degrees * 256 / 360) & 0xFF

def held_item_id(player):
    inventory = player.get("inventory", {})
    slot = 36 + player.get("selected_slot", 0)
    item = inventory.get(slot)

    if item and item[1] > 0:
        return max(0, min(int(item[0]), 32767))

    return 0

def make_spawn_player_packet(player):
    entity_id = player["entity_id"]

    x = int(player["x"] * 32)
    y = int(player["y"] * 32)
    z = int(player["z"] * 32)

    yaw = angle_byte(player.get("yaw", 0))
    pitch = angle_byte(player.get("pitch", 0))

    return (
        varint(entity_id)
        + uuid_bytes(player["uuid"])
        + struct.pack(">iii", x, y, z)
        + bytes((yaw, pitch))
        + struct.pack(">h", held_item_id(player))
        + b"\x0A\x7F\x7F"
    )

def add_player_to_tab_lists(player):
    with PLAYERS_LOCK:
        current_players = list(PLAYERS.values())

    for other in current_players:
        send_packet(
            player["conn"],
            0x38,
            add_player_info_payload(other)
        )

    for other in current_players:
        if other["uuid"] == player["uuid"]:
            continue

        try:
            send_packet(
                other["conn"],
                0x38,
                add_player_info_payload(player)
            )

            send_packet(
                other["conn"],
                0x0C,
                make_spawn_player_packet(player)
            )

            send_packet(
                player["conn"],
                0x0C,
                make_spawn_player_packet(other)
            )

        except OSError:
            pass

def remove_player_from_tabs(player_uuid, entity_id=None):
    payload = remove_player_info_payload(player_uuid)

    with PLAYERS_LOCK:
        remaining = list(PLAYERS.values())

    for other in remaining:
        try:
            send_packet(other["conn"], 0x38, payload)

            if entity_id is not None:
                destroy_payload = varint(1) + varint(entity_id)
                send_packet(other["conn"], 0x13, destroy_payload)

        except OSError:
            pass

def stream_chunks(sock, player, x, z):
    center_x = math.floor(x / 16.0)
    center_z = math.floor(z / 16.0)
    target = {
        (cx, cz)
        for cx in range(center_x - CHUNK_RADIUS, center_x + CHUNK_RADIUS + 1)
        for cz in range(center_z - CHUNK_RADIUS, center_z + CHUNK_RADIUS + 1)
    }
    missing = target.difference(player["sent_chunks"])
    for chunk_x, chunk_z in sorted(
        missing,
        key=lambda pair: (pair[0] - center_x) ** 2 + (pair[1] - center_z) ** 2,
    ):
        player["sent_chunks"].add((chunk_x, chunk_z))
        send_chunk(sock, chunk_x, chunk_z)
    player["last_chunk_center"] = (center_x, center_z)

def handle_creative_inventory_action(player, conn, body, offset):
    if offset + 4 > len(body):
        return

    slot = struct.unpack_from(">h", body, offset)[0]
    item_offset = offset + 2

    if item_offset + 2 > len(body):
        return

    item_id = struct.unpack_from(">h", body, item_offset)[0]
    count = 0
    damage = 0

    if item_id >= 0:
        if item_offset + 5 > len(body):
            return

        count = body[item_offset + 2]
        damage = struct.unpack_from(">h", body, item_offset + 3)[0]

    if 0 <= slot <= 44:
        if item_id >= 0 and count > 0:
            player["inventory"][slot] = (
                item_id, count, damage
            )
        else:
            player["inventory"].pop(slot, None)

        payload = (
            b"\x00"
            + struct.pack(">h", slot)
            + body[item_offset:]
        )
        send_packet(conn, 0x2F, payload)

def handle_player_digging(body, offset):
    status, offset = read_varint_bytes(body, offset)
    x, y, z, offset = unpack_block_position(body, offset)
    if offset >= len(body):
        return
    face = body[offset]

    if status not in (0, 2) or not (0 <= y < 256):
        return

    old_id, old_meta = get_block_state(x, y, z)
    if old_id == 0:
        return

    if set_block_state(x, y, z, 0, 0):
        send_block_change_to_all(x, y, z)

def block_intersects_player(bx, by, bz):
    with PLAYERS_LOCK:
        players = list(PLAYERS.values())

    for p in players:
        px = p["x"]
        py = p["y"]
        pz = p["z"]
        intersects_x = bx < px + 0.3 and bx + 1 > px - 0.3
        intersects_y = by < py + 1.8 and by + 1 > py
        intersects_z = bz < pz + 0.3 and bz + 1 > pz

        if intersects_x and intersects_y and intersects_z:
            return True

    return False

def handle_block_placement(player, body, offset):
    try:
        x, y, z, offset = unpack_block_position(body, offset)

        if offset >= len(body):
            return

        face = body[offset]
        item_offset = offset + 1

        if item_offset + 2 > len(body):
            return

        item_id = struct.unpack_from(">h", body, item_offset)[0]
        count = 0
        damage = 0
        source = "packet"

        if item_id >= 0:
            if item_offset + 5 > len(body):
                return

            count = body[item_offset + 2]
            damage = struct.unpack_from(
                ">h", body, item_offset + 3
            )[0]

        if not (1 <= item_id <= 255) or count <= 0:
            selected_slot = player["selected_slot"]
            held = player["inventory"].get(36 + selected_slot)

            if held is None:
                return

            item_id, count, damage = held
            source = "inventory"

        if face == 255:
            return

        if face not in range(6):
            return

        if not (1 <= item_id <= 255):
            return

        if face == 0:
            y -= 1
        elif face == 1:
            y += 1
        elif face == 2:
            z -= 1
        elif face == 3:
            z += 1
        elif face == 4:
            x -= 1
        elif face == 5:
            x += 1

        if not (0 <= y < 256):
            return
        
        if block_intersects_player(x, y, z):
            return

        if set_block_state(x, y, z, item_id, damage & 0x0F):
            send_block_change_to_all(x, y, z)

    except (ValueError, struct.error, IndexError) as exc:
        print(f"[place] Invalid packet: {exc}")

def broadcast_player_teleport(player, include_head=False):
    payload = (
        varint(player["entity_id"])
        + struct.pack(
            ">iii",
            int(player["x"] * 32),
            int(player["y"] * 32),
            int(player["z"] * 32)
        )
        + bytes((
            angle_byte(player.get("yaw", 0)),
            angle_byte(player.get("pitch", 0))
        ))
        + (b"\x01" if player.get("on_ground") else b"\x00")
    )

    head_payload = (
        varint(player["entity_id"])
        + bytes((angle_byte(player.get("yaw", 0)),))
    )

    with PLAYERS_LOCK:
        recipients = [
            other for other in PLAYERS.values()
            if other["uuid"] != player["uuid"]
        ]

    for other in recipients:
        try:
            send_packet(other["conn"], 0x18, payload)

            if include_head:
                send_packet(other["conn"], 0x19, head_payload)

        except OSError:
            pass

def handle_client(conn, address):
    print(f"[+] Connection from {address[0]}:{address[1]}")
    conn.settimeout(30)
    player = None
    registered = False
    try:
        handshake = recv_packet(conn)
        packet_id, offset = read_varint_bytes(handshake)
        if packet_id != 0:
            return
        protocol, offset = read_varint_bytes(handshake, offset)
        _server_address, offset = read_string(handshake, offset)
        if offset + 3 > len(handshake):
            return
        _port = struct.unpack_from(">H", handshake, offset)[0]
        next_state = handshake[offset + 2]

        if next_state == 1:
            body = recv_packet(conn)
            request_id, _ = read_varint_bytes(body)
            if request_id == 0:
                send_status(conn)
            return

        if next_state != 2:
            return
        if protocol != PROTOCOL_VERSION:
            print(f"[-] Client protocol {protocol}; expected {PROTOCOL_VERSION}")
            return

        login_start = recv_packet(conn)
        login_id, login_offset = read_varint_bytes(login_start)
        if login_id != 0:
            return
        username, _ = read_string(login_start, login_offset)
        if not username or len(username) > 16:
            return

        player_uuid = offline_uuid(username)
        send_packet(conn, 0x02, mc_string(player_uuid) + mc_string(username))
        player = {
            "uuid": player_uuid,
            "name": username,
            "conn": conn,
            "sent_chunks": set(),
            "last_chunk_center": None,
            "x": 0.5,
            "y": 5.0,
            "z": 0.5,
            "yaw": 0,
            "pitch": 0,
            "entity_id": allocate_entity_id(),
            "inventory": {},
            "selected_slot": 0,
        }
        with PLAYERS_LOCK:
            PLAYERS[player_uuid] = player
        registered = True
        print(f"[+] {username} logged in (offline mode)")

        send_world(conn, player)
        add_player_to_tab_lists(player)

        conn.settimeout(1.0)
        last_keepalive = time.monotonic()
        keepalive_id = 1

        while True:
            now = time.monotonic()
            if now - last_keepalive >= 10:
                send_packet(conn, 0x00, varint(keepalive_id))
                keepalive_id = (keepalive_id + 1) & 0x7FFFFFFF
                last_keepalive = now

            try:
                body = recv_packet(conn)
            except socket.timeout:
                continue
            if not body:
                continue
            incoming_id, incoming_offset = read_varint_bytes(body)

            if incoming_id == 0x00:
                reply_id, _ = read_varint_bytes(body, incoming_offset)
                send_packet(conn, 0x00, varint(reply_id))
            elif incoming_id == 0x07:
                handle_player_digging(body, incoming_offset)
            elif incoming_id == 0x08:
                handle_block_placement(
                    player, body, incoming_offset
                )
            elif incoming_id == 0x04:
                if len(body) >= incoming_offset + 25:
                    x, y, z = struct.unpack_from(
                        ">ddd", body, incoming_offset
                    )

                    player["x"] = x
                    player["y"] = y
                    player["z"] = z
                    player["on_ground"] = (
                        body[incoming_offset + 24] != 0
                    )

                    broadcast_player_teleport(player)

                    new_center = (
                        math.floor(x / 16.0),
                        math.floor(z / 16.0)
                    )

                    if new_center != player["last_chunk_center"]:
                        stream_chunks(conn, player, x, z)

            elif incoming_id == 0x05:
                if len(body) >= incoming_offset + 9:
                    yaw, pitch = struct.unpack_from(
                        ">ff", body, incoming_offset
                    )

                    player["yaw"] = yaw
                    player["pitch"] = pitch
                    player["on_ground"] = (
                        body[incoming_offset + 8] != 0
                    )

                    broadcast_player_teleport(player, include_head=True)

            elif incoming_id == 0x06:
                if len(body) >= incoming_offset + 33:
                    x, y, z, yaw, pitch = struct.unpack_from(
                        ">dddff", body, incoming_offset
                    )

                    player["x"] = x
                    player["y"] = y
                    player["z"] = z
                    player["yaw"] = yaw
                    player["pitch"] = pitch
                    player["on_ground"] = (
                        body[incoming_offset + 32] != 0
                    )

                    broadcast_player_teleport(player, include_head=True)

                    new_center = (
                        math.floor(x / 16.0),
                        math.floor(z / 16.0)
                    )

                    if new_center != player["last_chunk_center"]:
                        stream_chunks(conn, player, x, z)
            elif incoming_id == 0x10:
                handle_creative_inventory_action(
                    player, conn, body, incoming_offset
                )
            elif incoming_id == 0x09:
                if incoming_offset + 2 <= len(body):
                    slot = struct.unpack_from(
                        ">h", body, incoming_offset
                    )[0]

                    if 0 <= slot <= 8:
                        player["selected_slot"] = slot
            elif incoming_id == 0x0A:
                payload = varint(player["entity_id"]) + b"\x00"

                with PLAYERS_LOCK:
                    recipients = [
                        other for other in PLAYERS.values()
                        if other["uuid"] != player["uuid"]
                    ]

                for other in recipients:
                    try:
                        send_packet(other["conn"], 0x0B, payload)
                    except OSError:
                        pass

    except (ConnectionError, OSError, ValueError, struct.error) as exc:
        print(f"[-] {address[0]} disconnected: {exc}")
    finally:
        if registered and player is not None:
            with PLAYERS_LOCK:
                PLAYERS.pop(player["uuid"], None)
            remove_player_from_tabs(player["uuid"],player["entity_id"])
        try:
            conn.close()
        except OSError:
            pass
        print(f"[-] Connection closed: {address[0]}:{address[1]}")

def main():
    print(f"Minecraft Java {SERVER_VERSION} / protocol {PROTOCOL_VERSION}")
    print(f"Listening on {HOST}:{PORT} (offline mode)")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen()
    try:
        while True:
            conn, address = server.accept()
            threading.Thread(target=handle_client, args=(conn, address), daemon=True).start()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.close()

if __name__ == "__main__":
    main()
