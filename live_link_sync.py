

import bpy
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from mathutils import Vector

try:
    import msgpack
    MSGPACK_AVAILABLE = True
except ImportError:
    msgpack = None
    MSGPACK_AVAILABLE = False

bl_info = {
    "name": "Roblox Live Link (Blender -> Roblox EditableMesh Bridge)",
    "author": "Live Link",
    "version": (1, 4, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Live Link",
    "description": "Streams mesh objects (split normals, vertex colors, UVs, deletions, "
                    "bulk sync, optional auto-sync) to a local bridge server for live sync "
                    "into Roblox Studio",
    "category": "Object",
}

MAX_SAFE_TRIANGLES = 20000


def blender_to_roblox(co, scale):
    return [co.x * scale, co.z * scale, -co.y * scale]


def blender_normal_to_roblox(n, normal_matrix):
    world_n = normal_matrix @ n
    if world_n.length_squared > 0.0:
        world_n.normalize()
    return [world_n.x, world_n.z, -world_n.y]


def get_pivot_offset(obj, mode):
    if mode == "OBJECT_CENTER":
        return obj.matrix_world.translation.copy()
    elif mode == "CURSOR_3D":
        return bpy.context.scene.cursor.location.copy()
    return Vector((0.0, 0.0, 0.0))
def get_loop_normals(mesh):
    if bpy.app.version >= (4, 1, 0):

        return [Vector(cn.vector) for cn in mesh.corner_normals]
    else:
        mesh.calc_normals_split()
        normals = [loop.normal.copy() for loop in mesh.loops]
        mesh.free_normals_split()
        return normals
def get_object_colors(obj, mesh, loop_triangles):
    color_layer = None
    try:
        if mesh.color_attributes and mesh.color_attributes.active_color is not None:
            color_layer = mesh.color_attributes.active_color
    except AttributeError:
        color_layer = None
    if color_layer is None and getattr(mesh, "vertex_colors", None):
        if mesh.vertex_colors.active is not None:
            color_layer = mesh.vertex_colors.active
    color_lookup = {}
    colors_out = []
    def get_color_id(c):
        key = (round(c[0], 4), round(c[1], 4), round(c[2], 4), round(c[3], 4))
        idx = color_lookup.get(key)
        if idx is None:
            idx = len(colors_out)
            color_lookup[key] = idx
            colors_out.append(c)
        return idx
    if color_layer is not None:
        triangle_color_indices = []
        try:
            domain = getattr(color_layer, "domain", "CORNER")
            for tri in loop_triangles:
                corner_ids = []
                for i, loop_index in enumerate(tri.loops):
                    if domain == "POINT":
                        data_index = tri.vertices[i]
                    else:
                        data_index = loop_index
                    col = color_layer.data[data_index].color
                    corner_ids.append(get_color_id([col[0], col[1], col[2], col[3]]))
                triangle_color_indices.append(corner_ids)
            return colors_out, triangle_color_indices
        except Exception:

            pass
    if obj.material_slots and obj.material_slots[0].material:
        mat = obj.material_slots[0].material
        rgba = [0.8, 0.8, 0.8, 1.0]
        try:
            if mat.use_nodes:
                bsdf = next(
                    (n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"),
                    None,
                )
                if bsdf is not None:
                    base_color = bsdf.inputs["Base Color"].default_value
                    rgba = [base_color[0], base_color[1], base_color[2], base_color[3]]
                else:
                    rgba = list(mat.diffuse_color)
            else:
                rgba = list(mat.diffuse_color)
        except Exception:
            pass
        uniform_id = get_color_id(rgba)
        triangle_color_indices = [[uniform_id, uniform_id, uniform_id] for _ in loop_triangles]
        return colors_out, triangle_color_indices
    return None, None
def get_object_uvs(mesh, loop_triangles):
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        return None, None
    uv_lookup = {}
    uvs_out = []
    def get_uv_id(uv):
        key = (round(uv[0], 6), round(uv[1], 6))
        idx = uv_lookup.get(key)
        if idx is None:
            idx = len(uvs_out)
            uv_lookup[key] = idx
            uvs_out.append(uv)
        return idx
    triangle_uv_indices = []
    for tri in loop_triangles:
        corner_ids = []
        for loop_index in tri.loops:
            uv = uv_layer.data[loop_index].uv
            corner_ids.append(get_uv_id([uv[0], uv[1]]))
        triangle_uv_indices.append(corner_ids)
    return uvs_out, triangle_uv_indices
def build_mesh_payload(obj, action, scale, pivot_mode="WORLD_ORIGIN"):

    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    mesh = obj_eval.to_mesh()
    try:
        mesh.calc_loop_triangles()
        loop_normals = get_loop_normals(mesh)
        world_matrix = obj.matrix_world
        normal_matrix = world_matrix.to_3x3().inverted_safe().transposed()

        pivot_offset = get_pivot_offset(obj, pivot_mode)
        vertices = [
            blender_to_roblox((world_matrix @ v.co) - pivot_offset, scale)
            for v in mesh.vertices
        ]
        normal_lookup = {}
        normals_out = []
        def get_normal_id(n):
            key = (round(n[0], 6), round(n[1], 6), round(n[2], 6))
            idx = normal_lookup.get(key)
            if idx is None:
                idx = len(normals_out)
                normal_lookup[key] = idx
                normals_out.append(n)
            return idx

        triangles = []
        triangle_normals = []
        for tri in mesh.loop_triangles:
            triangles.append(list(tri.vertices))
            corner_ids = []
            for loop_index in tri.loops:
                rn = blender_normal_to_roblox(loop_normals[loop_index], normal_matrix)
                corner_ids.append(get_normal_id(rn))
            triangle_normals.append(corner_ids)
        colors_out, triangle_colors = get_object_colors(obj, mesh, mesh.loop_triangles)
        uvs_out, triangle_uvs = get_object_uvs(mesh, mesh.loop_triangles)
    finally:
        obj_eval.to_mesh_clear()

    if not vertices or not triangles:
        raise ValueError(f"'{obj.name}' has no triangulated geometry to send")

    payload = {
        "name": obj.name,
        "action": action,
        "vertices": vertices,
        "normals": normals_out,
        "triangles": triangles,
        "triangle_normals": triangle_normals,
    }

    if colors_out is not None:
        payload["colors"] = colors_out
        payload["triangle_colors"] = triangle_colors
    if uvs_out is not None:
        payload["uvs"] = uvs_out
        payload["triangle_uvs"] = triangle_uvs
    return payload

def check_poly_guard(payload, settings, operator=None):
    tri_count = len(payload["triangles"])
    if tri_count <= MAX_SAFE_TRIANGLES:
        return True, None
    msg = (
        f"'{payload['name']}' has {tri_count} triangles, over the "
        f"{MAX_SAFE_TRIANGLES}-triangle safety threshold. Sending it may "
        f"freeze Roblox Studio while it builds the MeshPart."
    )
    if operator is not None:
        operator.report({"WARNING"}, msg)
    print(f"[LiveLink] WARNING: {msg}")
    return not settings.block_oversized_meshes, msg


def _encode_body(payload, use_msgpack):
    if use_msgpack and MSGPACK_AVAILABLE:
        return msgpack.packb(payload, use_bin_type=True), "application/x-msgpack"
    return json.dumps(payload).encode("utf-8"), "application/json"
def send_to_server(url, payload, timeout=5.0, use_msgpack=False, shared_secret=""):
    try:
        body, content_type = _encode_body(payload, use_msgpack)
        headers = {"Content-Type": content_type}
        if shared_secret:
            headers["X-LiveLink-Token"] = shared_secret
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = ""
        return False, f"Server rejected request (HTTP {e.code}): {detail or e.reason}"
    except urllib.error.URLError as e:
        return False, f"Could not reach server at {url}: {e.reason}"
    except Exception as e:
        return False, f"Unexpected error: {e}"
def send_delete_to_server(url, timeout=5.0, shared_secret=""):
    try:
        headers = {"X-LiveLink-Token": shared_secret} if shared_secret else {}
        req = urllib.request.Request(url, headers=headers, method="DELETE")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True, "already removed"
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = ""
        return False, f"Server rejected delete (HTTP {e.code}): {detail or e.reason}"
    except urllib.error.URLError as e:
        return False, f"Could not reach server at {url}: {e.reason}"
    except Exception as e:
        return False, f"Unexpected error: {e}"
AUTO_SYNC_POLL_INTERVAL = 0.15
_sync_state = {
    "known_objects": set(),
    "dirty_objects": set(),
    "last_change_time": {},
    "timer_active": False,
}
def _mark_synced(name):
    _sync_state["known_objects"].add(name)
def _push_delete(name, settings):
    url = settings.server_url.rstrip("/") + "/objects/" + urllib.parse.quote(name, safe="")
    ok, message = send_delete_to_server(url, shared_secret=settings.shared_secret)
    _sync_state["known_objects"].discard(name)
    _sync_state["dirty_objects"].discard(name)
    _sync_state["last_change_time"].pop(name, None)
    return ok, message
def _sweep_deleted_objects(settings):
    removed = []
    for name in list(_sync_state["known_objects"]):
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            ok, message = _push_delete(name, settings)
            if ok:
                removed.append(name)
            else:
                settings.last_status = f"Delete error ('{name}'): {message}"
    return removed



def _auto_sync_tick():
    scene = bpy.context.scene
    if scene is None or not hasattr(scene, "livelink_settings"):
        _sync_state["timer_active"] = False
        return None

    settings = scene.livelink_settings
    if not settings.auto_sync_enabled:
        _sync_state["timer_active"] = False
        return None

    removed = _sweep_deleted_objects(settings)
    if removed:
        settings.last_status = f"Auto-sync: removed {len(removed)} object(s) - {', '.join(removed)}"
    now = time.time()
    debounce = settings.auto_sync_debounce
    ready_names = [
        name for name in list(_sync_state["dirty_objects"])
        if now - _sync_state["last_change_time"].get(name, 0.0) >= debounce
    ]

    for name in ready_names:
        _sync_state["dirty_objects"].discard(name)
        _sync_state["last_change_time"].pop(name, None)
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            continue
        action = "update" if name in _sync_state["known_objects"] else "new"
        try:
            payload = build_mesh_payload(obj, action, settings.scale_factor, settings.pivot_point_mode)
        except Exception as e:
            settings.last_status = f"Auto-sync error ('{name}'): {e}"
            continue


        check_poly_guard(payload, settings)
        url = settings.server_url.rstrip("/") + "/sync"
        ok, message = send_to_server(url, payload, use_msgpack=settings.use_msgpack, shared_secret=settings.shared_secret)
        if ok:
            _mark_synced(name)
            settings.last_status = (
                f"Auto-sync OK - '{name}' ({len(payload['vertices'])} verts, "
                f"{len(payload['triangles'])} tris, {len(payload['normals'])} normals)"
            )
        else:
            settings.last_status = f"Auto-sync error ('{name}'): {message}"
    if ready_names or removed:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    return AUTO_SYNC_POLL_INTERVAL
def _ensure_auto_sync_timer_running():
    if not _sync_state["timer_active"]:
        _sync_state["timer_active"] = True
        bpy.app.timers.register(_auto_sync_tick, first_interval=AUTO_SYNC_POLL_INTERVAL)
def _on_auto_sync_toggled(self, context):
    if self.auto_sync_enabled:
        _ensure_auto_sync_timer_running()
    else:
        pass

def _on_depsgraph_update(scene, depsgraph):
    settings = getattr(scene, "livelink_settings", None)
    if settings is None or not settings.auto_sync_enabled:
        return
    now = time.time()
    for update in depsgraph.updates:
        obj = update.id
        if isinstance(obj, bpy.types.Object) and obj.type == "MESH":
            if update.is_updated_geometry or update.is_updated_transform:
                _sync_state["dirty_objects"].add(obj.name)
                _sync_state["last_change_time"][obj.name] = now
    if _sync_state["dirty_objects"]:
        _ensure_auto_sync_timer_running()

class LiveLinkSettings(bpy.types.PropertyGroup):
    server_url: bpy.props.StringProperty(
        name="Server URL",
        description="Base URL of the local bridge server",
        default="http://localhost:5000",
    )
    shared_secret: bpy.props.StringProperty(
        name="Shared Secret",
        description="Auth token for the bridge server, if it was started with "
                    "LIVELINK_SHARED_SECRET set. Leave blank if the server "
                    "doesn't require auth",
        default="",
        subtype="PASSWORD",
    )
    scale_factor: bpy.props.FloatProperty(
        name="Scale",
        description="Multiplier applied to vertex coordinates before sending "
                    "(e.g. use ~3.57 to convert meters to studs, or 1.0 for 1:1)",
        default=1.0,
        min=0.0001,
    )
    auto_sync_enabled: bpy.props.BoolProperty(
        name="Auto-Sync",
        description="Automatically push mesh objects to Roblox shortly after "
                    "you stop editing them (and remove ones you delete), no "
                    "button click needed",
        default=False,
        update=_on_auto_sync_toggled,
    )
    auto_sync_debounce: bpy.props.FloatProperty(
        name="Debounce (s)",
        description="How long an object must be idle (no edits) before "
                    "auto-sync pushes it -- avoids flooding the server "
                    "while you're actively dragging vertices",
        default=0.4,
        min=0.05,
        max=5.0,
    )
    last_status: bpy.props.StringProperty(default="")
    pivot_point_mode: bpy.props.EnumProperty(
        name="Pivot Point Mode",
        description="Where the (0,0,0) of the sent mesh should be anchored",
        items=[
            ("WORLD_ORIGIN", "World Origin (0,0,0)", "Use Blender's world origin as-is (legacy behavior)"),
            ("OBJECT_CENTER", "Object Center", "Anchor the mesh to the object's own origin/pivot"),
            ("CURSOR_3D", "3D Cursor", "Anchor the mesh to the current 3D cursor position"),
        ],
        default="WORLD_ORIGIN",
    )
    block_oversized_meshes: bpy.props.BoolProperty(
        name="Block Oversized Meshes",
        description=f"Refuse to send (manual sync) any mesh over {MAX_SAFE_TRIANGLES} "
                    f"triangles until you confirm, to avoid freezing Roblox Studio. "
                    f"Auto-sync always warns but still sends (no dialog to block on)",
        default=True,
    )
    use_msgpack: bpy.props.BoolProperty(
        name="Use MessagePack (Binary)",
        description="Send mesh payloads as MessagePack instead of JSON to cut "
                    "transfer size by roughly 60-80%. Requires the 'msgpack' "
                    "package on both this machine and the bridge server; falls "
                    "back to JSON automatically if unavailable",
        default=MSGPACK_AVAILABLE,
    )
class LIVELINK_OT_sync_new(bpy.types.Operator):
    bl_idname = "livelink.sync_new"
    bl_label = "Sync New Object"
    bl_description = "Send the active object to Roblox as a brand-new synced object"
    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"
    def execute(self, context):
        return _run_sync(self, context, action="new")
class LIVELINK_OT_sync_update(bpy.types.Operator):
    bl_idname = "livelink.sync_update"
    bl_label = "Update Object"
    bl_description = "Push updated vertex positions/normals for the active object to Roblox"
    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"
    def execute(self, context):
        return _run_sync(self, context, action="update")
def _run_sync(operator, context, action):
    settings = context.scene.livelink_settings
    obj = context.active_object

    try:
        payload = build_mesh_payload(obj, action, settings.scale_factor, settings.pivot_point_mode)
    except Exception as e:
        settings.last_status = f"Error: {e}"
        operator.report({"ERROR"}, str(e))
        return {"CANCELLED"}
    should_send, warning = check_poly_guard(payload, settings, operator)
    if not should_send:
        settings.last_status = f"Blocked: {warning} Disable 'Block Oversized Meshes' to send anyway."
        return {"CANCELLED"}
    url = settings.server_url.rstrip("/") + "/sync"
    ok, message = send_to_server(url, payload, use_msgpack=settings.use_msgpack, shared_secret=settings.shared_secret)
    if ok:
        _mark_synced(obj.name)
        settings.last_status = (
            f"OK - '{obj.name}' sent "
            f"({len(payload['vertices'])} verts, {len(payload['triangles'])} tris, "
            f"{len(payload['normals'])} normals)"
        )
        operator.report({"INFO"}, settings.last_status)
        return {"FINISHED"}
    else:
        settings.last_status = f"Error: {message}"
        operator.report({"ERROR"}, message)
        return {"CANCELLED"}

def _run_bulk_sync(operator, context, objs):
    settings = context.scene.livelink_settings
    url = settings.server_url.rstrip("/") + "/sync"
    sent = 0
    failed = []
    blocked = []
    for obj in objs:
        action = "update" if obj.name in _sync_state["known_objects"] else "new"
        try:
            payload = build_mesh_payload(obj, action, settings.scale_factor, settings.pivot_point_mode)
        except Exception as e:
            failed.append(f"{obj.name} ({e})")
            continue
        should_send, warning = check_poly_guard(payload, settings, operator)
        if not should_send:
            blocked.append(obj.name)
            continue

        ok, message = send_to_server(url, payload, use_msgpack=settings.use_msgpack, shared_secret=settings.shared_secret)
        if ok:
            _mark_synced(obj.name)
            sent += 1
        else:
            failed.append(f"{obj.name} ({message})")
    status_parts = [f"Synced {sent}/{len(objs)}"]
    if blocked:
        status_parts.append(f"blocked (too many tris): {', '.join(blocked)}")
    if failed:
        status_parts.append(f"failed: {', '.join(failed)}")
    if failed or blocked:
        settings.last_status = " - ".join(status_parts)
        operator.report({"WARNING"}, settings.last_status)
    else:
        settings.last_status = f"OK - synced {sent} object(s)"
        operator.report({"INFO"}, settings.last_status)
    return {"FINISHED"} if sent > 0 else {"CANCELLED"}

class LIVELINK_OT_sync_all(bpy.types.Operator):
    bl_idname = "livelink.sync_all"
    bl_label = "Sync All"
    bl_description = "Send every mesh object in the current scene to Roblox"
    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.scene.objects)
    def execute(self, context):
        objs = [o for o in context.scene.objects if o.type == "MESH"]
        return _run_bulk_sync(self, context, objs)


class LIVELINK_OT_sync_selected(bpy.types.Operator):
    bl_idname = "livelink.sync_selected"
    bl_label = "Sync Selected"
    bl_description = "Send the currently selected mesh objects to Roblox"
    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)
    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type == "MESH"]
        return _run_bulk_sync(self, context, objs)
class LIVELINK_OT_remove_active(bpy.types.Operator):
    bl_idname = "livelink.remove_active"
    bl_label = "Remove From Roblox"
    bl_description = "Delete the active object from the Roblox side only, without deleting it in Blender"
    @classmethod
    def poll(cls, context):
        return context.active_object is not None
    def execute(self, context):
        settings = context.scene.livelink_settings
        name = context.active_object.name
        ok, message = _push_delete(name, settings)
        if ok:
            settings.last_status = f"OK - removed '{name}' from Roblox"
            self.report({"INFO"}, settings.last_status)
            return {"FINISHED"}
        else:
            settings.last_status = f"Error: {message}"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
class LIVELINK_OT_sync_deletions(bpy.types.Operator):
    bl_idname = "livelink.sync_deletions"
    bl_label = "Sync Deletions Now"
    bl_description = "Remove any previously-synced object from Roblox that no longer exists in this file"

    def execute(self, context):
        settings = context.scene.livelink_settings
        removed = _sweep_deleted_objects(settings)
        if removed:
            settings.last_status = f"Removed {len(removed)} object(s) from Roblox: {', '.join(removed)}"
            self.report({"INFO"}, settings.last_status)
        else:
            settings.last_status = "Nothing to remove - Roblox is already up to date"
            self.report({"INFO"}, settings.last_status)
        return {"FINISHED"}


class LIVELINK_PT_panel(bpy.types.Panel):
    bl_label = "Roblox Live Link"
    bl_idname = "LIVELINK_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Live Link"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.livelink_settings
        obj = context.active_object

        col = layout.column(align=True)
        col.prop(settings, "server_url")
        col.prop(settings, "shared_secret")
        col.prop(settings, "scale_factor")

        col.prop(settings, "pivot_point_mode")

        row_mp = layout.row()
        row_mp.enabled = MSGPACK_AVAILABLE
        row_mp.prop(settings, "use_msgpack")
        if not MSGPACK_AVAILABLE:
            layout.label(text="Install 'msgpack' to enable binary transport", icon="ERROR")
        layout.prop(settings, "block_oversized_meshes")
        layout.separator()
        col2 = layout.column(align=True)
        col2.prop(settings, "auto_sync_enabled", icon="TIME")
        if settings.auto_sync_enabled:
            col2.prop(settings, "auto_sync_debounce")
            watching = len(_sync_state["dirty_objects"])
            if watching:
                layout.label(text=f"Waiting to sync {watching} object(s)...", icon="SORTTIME")
        layout.separator()
        if obj and obj.type == "MESH":
            layout.label(text=f"Active object: {obj.name}", icon="MESH_DATA")
        else:
            layout.label(text="Select a mesh object", icon="ERROR")
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator("livelink.sync_new", icon="ADD")
        row.operator("livelink.sync_update", icon="FILE_REFRESH")
        row_bulk = layout.row(align=True)
        row_bulk.operator("livelink.sync_all", icon="SCENE_DATA")
        row_bulk.operator("livelink.sync_selected", icon="RESTRICT_SELECT_OFF")
        layout.separator()
        row_del = layout.row(align=True)
        row_del.operator("livelink.remove_active", icon="TRASH")
        row_del.operator("livelink.sync_deletions", icon="FILE_REFRESH")
        tracked = len(_sync_state["known_objects"])
        layout.label(text=f"Tracking {tracked} synced object(s)", icon="INFO")
        if settings.last_status:
            layout.separator()
            box = layout.box()
            box.label(text=settings.last_status)

classes = (
    LiveLinkSettings,
    LIVELINK_OT_sync_new,
    LIVELINK_OT_sync_update,
    LIVELINK_OT_sync_all,
    LIVELINK_OT_sync_selected,
    LIVELINK_OT_remove_active,
    LIVELINK_OT_sync_deletions,
    LIVELINK_PT_panel,
)
def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.livelink_settings = bpy.props.PointerProperty(type=LiveLinkSettings)
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
def unregister():
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

    _sync_state["known_objects"].clear()
    _sync_state["dirty_objects"].clear()
    _sync_state["last_change_time"].clear()
    _sync_state["timer_active"] = False
    if bpy.app.timers.is_registered(_auto_sync_tick):
        bpy.app.timers.unregister(_auto_sync_tick)
    del bpy.types.Scene.livelink_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
if __name__ == "__main__":
    register()
