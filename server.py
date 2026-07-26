

import hmac
import os
import threading
import time
import uuid
from collections import defaultdict, deque

from flask import Flask, request, jsonify, Response

try:
    import msgpack
    MSGPACK_AVAILABLE = True
except ImportError:
    msgpack = None
    MSGPACK_AVAILABLE = False

app = Flask(__name__)

SHARED_SECRET = os.environ.get("LIVELINK_SHARED_SECRET", "").strip()
AUTH_HEADER = "X-LiveLink-Token"

app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("LIVELINK_MAX_CONTENT_LENGTH", 50 * 1024 * 1024))

RATE_LIMIT_MAX = int(os.environ.get("LIVELINK_RATE_LIMIT_MAX", 120))
RATE_LIMIT_WINDOW = float(os.environ.get("LIVELINK_RATE_LIMIT_WINDOW", 60))
_rate_lock = threading.Lock()
_rate_hits = defaultdict(deque)

_lock = threading.Lock()
_objects = {}
_tombstones = {}
_revision = 0
_session_id = uuid.uuid4().hex


def _check_rate_limit(client_ip):
    now = time.time()
    with _rate_lock:
        hits = _rate_hits[client_ip]
        while hits and now - hits[0] > RATE_LIMIT_WINDOW:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_MAX:
            return False
        hits.append(now)
        return True
@app.before_request
def _security_gate():
    client_ip = request.remote_addr or "unknown"
    if SHARED_SECRET and request.path not in ("/", "/health"):
        provided = request.headers.get(AUTH_HEADER, "")
        if not hmac.compare_digest(provided, SHARED_SECRET):
            print(f"[security] rejected request from {client_ip} to {request.path}: bad/missing {AUTH_HEADER}")
            return _make_response({"status": "error", "message": "unauthorized"}, 401)
    if request.method in ("POST", "DELETE"):
        if not _check_rate_limit(client_ip):
            print(f"[security] rate limit exceeded for {client_ip} on {request.method} {request.path}")
            return _make_response(
                {"status": "error", "message": "rate limit exceeded, slow down"}, 429
            )
    return None

@app.errorhandler(413)
def _handle_payload_too_large(_e):
    print(f"[security] rejected oversized request from {request.remote_addr or 'unknown'} to {request.path}")
    return _make_response(
        {"status": "error", "message": "payload too large (see LIVELINK_MAX_CONTENT_LENGTH)"}, 413
    )



def _parse_request_body():
    content_type = (request.content_type or "").lower()
    if "msgpack" in content_type:
        if not MSGPACK_AVAILABLE:
            return None, "server does not have the 'msgpack' package installed"
        try:
            data = msgpack.unpackb(request.get_data(), raw=False)
        except Exception as e:
            return None, f"invalid msgpack payload: {e}"
        return data, None
    data = request.get_json(silent=True, force=True)
    return data, None
def _wants_msgpack():
    accept = (request.headers.get("Accept") or "").lower()
    return MSGPACK_AVAILABLE and "msgpack" in accept
def _make_response(payload_dict, status=200):
    if _wants_msgpack():
        body = msgpack.packb(payload_dict, use_bin_type=True)
        return Response(body, status=status, mimetype="application/x-msgpack")
    resp = jsonify(payload_dict)
    resp.status_code = status
    return resp


def _validate_payload(data):
    if not isinstance(data, dict):
        return "payload must be a JSON object"
    name = data.get("name")
    vertices = data.get("vertices")
    triangles = data.get("triangles")

    if not isinstance(name, str) or not name.strip():
        return "'name' must be a non-empty string"
    if not isinstance(vertices, list) or len(vertices) == 0:
        return "'vertices' must be a non-empty list"
    for v in vertices:
        if not (isinstance(v, list) and len(v) == 3 and all(isinstance(n, (int, float)) for n in v)):
            return "each entry in 'vertices' must be a [x, y, z] list of numbers"
    if not isinstance(triangles, list) or len(triangles) == 0:
        return "'triangles' must be a non-empty list"
    vertex_count = len(vertices)
    for t in triangles:
        if not (isinstance(t, list) and len(t) == 3 and all(isinstance(i, int) for i in t)):
            return "each entry in 'triangles' must be a [i, j, k] list of integers"
        if any(i < 0 or i >= vertex_count for i in t):
            return "triangle references a vertex index that is out of range"

    normals = data.get("normals")
    triangle_normals = data.get("triangle_normals")

    if normals is not None:
        if not isinstance(normals, list) or len(normals) == 0:
            return "'normals', if present, must be a non-empty list"
        for n in normals:
            if not (isinstance(n, list) and len(n) == 3 and all(isinstance(x, (int, float)) for x in n)):
                return "each entry in 'normals' must be a [x, y, z] list of numbers"
    if triangle_normals is not None:
        if not isinstance(triangle_normals, list) or len(triangle_normals) != len(triangles):
            return "'triangle_normals', if present, must have one entry per triangle"
        normal_count = len(normals) if isinstance(normals, list) else 0
        for tn in triangle_normals:
            if not (isinstance(tn, list) and len(tn) == 3 and all(isinstance(i, int) for i in tn)):
                return "each entry in 'triangle_normals' must be a [i, j, k] list of normal indices"
            if any(i < 0 or i >= normal_count for i in tn):
                return "triangle_normals references a normal index that is out of range"

    colors = data.get("colors")
    if colors is not None:
        if not isinstance(colors, list) or len(colors) == 0:
            return "'colors', if present, must be a non-empty list"
        for c in colors:
            if not (isinstance(c, list) and len(c) == 4 and all(isinstance(x, (int, float)) for x in c)):
                return "each entry in 'colors' must be a [r, g, b, a] list of numbers"
        if len(colors) not in (1, vertex_count):
            return "'colors' must contain exactly 1 entry (uniform) or one entry per vertex"

    uvs = data.get("uvs")
    triangle_uvs = data.get("triangle_uvs")
    if uvs is not None:
        if not isinstance(uvs, list) or len(uvs) == 0:
            return "'uvs', if present, must be a non-empty list"
        for uv in uvs:
            if not (isinstance(uv, list) and len(uv) == 2 and all(isinstance(x, (int, float)) for x in uv)):
                return "each entry in 'uvs' must be a [u, v] list of numbers"
    if triangle_uvs is not None:
        if not isinstance(triangle_uvs, list) or len(triangle_uvs) != len(triangles):
            return "'triangle_uvs', if present, must have one entry per triangle"
        uv_count = len(uvs) if isinstance(uvs, list) else 0
        for tuv in triangle_uvs:
            if not (isinstance(tuv, list) and len(tuv) == 3 and all(isinstance(i, int) for i in tuv)):
                return "each entry in 'triangle_uvs' must be a [i, j, k] list of uv indices"
            if any(i < 0 or i >= uv_count for i in tuv):
                return "triangle_uvs references a uv index that is out of range"
    return None

@app.route("/sync", methods=["POST"])
def sync():

    global _revision
    data, parse_error = _parse_request_body()
    if parse_error:
        return _make_response({"status": "error", "message": parse_error}, 400)
    error = _validate_payload(data)
    if error:
        return _make_response({"status": "error", "message": error}, 400)
    with _lock:
        _revision += 1
        entry = {
            "name": data["name"],
            "action": data.get("action", "update"),
            "vertices": data["vertices"],
            "triangles": data["triangles"],
            "normals": data.get("normals"),
            "triangle_normals": data.get("triangle_normals"),
            "colors": data.get("colors"),
            "uvs": data.get("uvs"),
            "triangle_uvs": data.get("triangle_uvs"),
            "revision": _revision,
            "updated_at": time.time(),
        }
        _objects[entry["name"]] = entry

        _tombstones.pop(entry["name"], None)
        current_revision = _revision
    normal_count = len(entry["normals"]) if entry["normals"] else 0
    uv_count = len(entry["uvs"]) if entry["uvs"] else 0
    color_count = len(entry["colors"]) if entry["colors"] else 0
    print(
        f"[sync] '{entry['name']}' ({entry['action']}) - "
        f"{len(entry['vertices'])} verts, {len(entry['triangles'])} tris, "
        f"{normal_count} normals, {uv_count} uvs, {color_count} colors [rev {current_revision}]"
    )
    return _make_response({"status": "ok", "name": entry["name"], "revision": current_revision})
@app.route("/objects", methods=["GET"])
def list_objects():
    since_raw = request.args.get("since", "0")
    try:
        since = int(since_raw)
    except ValueError:
        return _make_response({"status": "error", "message": "'since' must be an integer"}, 400)
    with _lock:
        current_revision = _revision
        changed = [o for o in _objects.values() if o["revision"] > since]
        deleted = [t["name"] for t in _tombstones.values() if t["revision"] > since]
    return _make_response({
        "revision": current_revision,
        "session_id": _session_id,
        "objects": changed,
        "deleted": deleted,
    })
@app.route("/objects/<name>", methods=["GET"])
def get_object(name):
    with _lock:
        entry = _objects.get(name)
    if entry is None:
        return _make_response({"status": "error", "message": "object not found"}, 404)
    return _make_response(entry)
@app.route("/objects/<name>", methods=["DELETE"])
def delete_object(name):
    global _revision
    with _lock:
        if name not in _objects:
            return _make_response({"status": "error", "message": "object not found"}, 404)
        del _objects[name]
        _revision += 1
        current_revision = _revision
        _tombstones[name] = {
            "name": name,
            "revision": current_revision,
            "deleted_at": time.time(),
        }
    print(f"[sync] '{name}' deleted [rev {current_revision}]")
    return _make_response({"status": "ok", "revision": current_revision})

@app.route("/health", methods=["GET"])
def health():
    with _lock:
        return _make_response({
            "status": "running",
            "object_count": len(_objects),
            "revision": _revision,
            "session_id": _session_id,
            "msgpack_available": MSGPACK_AVAILABLE,
        })
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Blender <-> Roblox Live Link Bridge",
        "endpoints": {
            "POST /sync": "Receive mesh data from Blender (JSON or application/x-msgpack)",
            "GET /objects?since=<rev>": "Poll for objects changed/deleted since a revision (used by Roblox)",
            "GET /objects/<name>": "Fetch a single object's current data",
            "DELETE /objects/<name>": "Remove an object from the store",
            "GET /health": "Server status (also reports msgpack_available)",
        },
        "msgpack_available": MSGPACK_AVAILABLE,
        "auth_required": bool(SHARED_SECRET),
        "note": "Send 'Accept: application/x-msgpack' to receive msgpack-encoded responses; "
                "send 'Content-Type: application/x-msgpack' with a msgpack body to POST /sync in binary. "
                "If auth_required is true, every endpoint except this one and /health needs an "
                f"'{AUTH_HEADER}' header matching the server's LIVELINK_SHARED_SECRET.",
    })


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

if __name__ == "__main__":
    if not MSGPACK_AVAILABLE:
        print("[server] NOTE: 'msgpack' package not installed - binary transport disabled, "
              "falling back to JSON only. Run: pip install msgpack")
    if not SHARED_SECRET:
        print("[server] NOTE: LIVELINK_SHARED_SECRET is not set - running with NO AUTH. "
              "Anyone who can reach this port can read/modify/delete your synced meshes. "
              "See README.md if you plan to run this on a shared network.")
    else:
        print("[server] Auth enabled - clients must send a matching "
              f"'{AUTH_HEADER}' header.")
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
