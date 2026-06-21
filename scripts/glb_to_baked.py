"""
glb_to_baked.py — generic: import a GLB (or SKP), keep the real structure, drop
Google-Earth backdrop objects, scale to a real-world height, recenter footprint to
local origin (X=Y=0, base z=0), orient so the long horizontal axis is +Y, then
Smart-UV-unwrap and BAKE all materials (textures + flat colours) into ONE 2048 atlas.
Exports a triangulated, UV'd OBJ + the baked PNG.

This is the canonical, repo-tracked copy of the per-object Blender converter that
``scripts/batch_migrate.py`` drives in parallel. It is byte-for-byte the same
converter first proven in ``.sandbox/landmarks/_work/glb_to_baked.py`` (kept there
unchanged); the batch tool copies THIS file into each object's private work dir at
run time so its ``__file__``-relative outputs (``<base>.obj`` / ``<base>_bake.png``)
are isolated and safe to produce concurrently.

Usage:
  blender -b --python glb_to_baked.py -- <in.glb|in.skp> <out_basename> <target> [orient]
  target: a plain number (height in m), 'len:214' (scale so long XY axis = 214 m),
          or 'native' (keep the model's own real-world metres).
  orient: 'longY' (default, rotate so longest XY extent -> +Y) or 'none'

Outputs (next to this script):  <out_basename>.obj  and  <out_basename>_bake.png
Prints a BAKE| line with final verts/tris/size and a JSON-ish summary.
"""
import bpy, addon_utils, os, sys, math, mathutils, json

argv = sys.argv[sys.argv.index("--")+1:]
SRC      = argv[0]
OUTBASE  = argv[1]
# TARGET can be a plain number (height in m) or 'len:214' to scale by long-axis length
TARGET_RAW = argv[2]
ORIENT   = argv[3] if len(argv) > 3 else "longY"
if TARGET_RAW.startswith("len:"):
    SCALE_MODE = "len"; TARGET_V = float(TARGET_RAW[4:])
elif TARGET_RAW == "native":
    SCALE_MODE = "native"; TARGET_V = 0.0   # keep the model's own real-world metres
else:
    SCALE_MODE = "height"; TARGET_V = float(TARGET_RAW)
WORK = os.path.dirname(os.path.abspath(__file__))
OUT_OBJ = os.path.join(WORK, OUTBASE + ".obj")
OUT_TEX = os.path.join(WORK, OUTBASE + "_bake.png")

def log(*a): print("BAKE|", *a)

bpy.ops.wm.read_factory_settings(use_empty=True)

# import
ext = os.path.splitext(SRC)[1].lower()
if ext == ".skp":
    _AD = r"C:/Users/ilija/AppData/Roaming/Blender Foundation/Blender/5.1/scripts/addons/sketchup_importer"
    os.add_dll_directory(_AD)
    addon_utils.enable("sketchup_importer", default_set=True, persistent=True)
    bpy.ops.import_scene.skp(filepath=SRC)
else:
    bpy.ops.import_scene.gltf(filepath=SRC)

# Drop Google-Earth backdrop objects (photo quad + GE terrain blob). These are
# named 'Google Earth ...' or are big flat (z~0) snapshot planes with a GE material.
def is_ge(o):
    n = o.name.lower()
    if "google earth" in n or "snapshot" in n: return True
    if o.type == 'MESH' and o.data.materials:
        names = [ (m.name.lower() if m else "") for m in o.data.materials ]
        # GE / location snapshot backdrop planes
        if names and all(("google earth" in s) or ("snapshot" in s) or
                         ("location snapshot" in s) for s in names):
            return True
        # scale-reference human figure some modellers embed ('Chris_Skin' etc.)
        if names and all(s.startswith("chris_") or s.startswith("man_") or
                         "_skin" in s or "_hair" in s for s in names):
            return True
    return False

removed = []
for o in list(bpy.data.objects):
    if o.type == 'MESH' and is_ge(o):
        removed.append(o.name); bpy.data.objects.remove(o, do_unlink=True)
log("dropped GE backdrop objs:", len(removed))

mesh_objs = [o for o in bpy.data.objects if o.type == 'MESH']
assert mesh_objs, "no mesh objects after GE removal"

# Bake each mesh's FULL world matrix into its vertex data. The glTF hierarchy parents
# meshes under empties with scale 0.025 and the mesh data is in cm-ish raw units, so
# matrix_world (which composes the whole chain) is the single source of truth. We bake
# it, then fully detach (identity basis, no parent) so nothing re-applies a transform.
import mathutils as _mu
for o in mesh_objs:
    mw = o.matrix_world.copy()
    o.data.transform(mw)
    o.parent = None
    o.matrix_basis = _mu.Matrix.Identity(4)
# drop every non-mesh helper (Root Node empties, cameras, lights)
for o in list(bpy.data.objects):
    if o.type != 'MESH':
        bpy.data.objects.remove(o, do_unlink=True)

mesh_objs = [o for o in bpy.data.objects if o.type == 'MESH']
bpy.ops.object.select_all(action='DESELECT')
for o in mesh_objs:
    o.select_set(True)
bpy.context.view_layer.objects.active = mesh_objs[0]
if len(mesh_objs) > 1:
    bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
obj.name = OUTBASE
bpy.ops.object.select_all(action='DESELECT')
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
assert obj.type == 'MESH', f"active is {obj.type}, expected MESH"

def bbox(o):
    mn = mathutils.Vector(( 1e18,)*3); mx = mathutils.Vector((-1e18,)*3)
    for v in o.data.vertices:
        w = o.matrix_world @ v.co
        for i in range(3): mn[i]=min(mn[i],w[i]); mx[i]=max(mx[i],w[i])
    return mn, mx

mn, mx = bbox(obj)
log(f"raw bbox X[{mn[0]:.2f},{mx[0]:.2f}] Y[{mn[1]:.2f},{mx[1]:.2f}] Z[{mn[2]:.2f},{mx[2]:.2f}]")

# orient: rotate about Z so the longest horizontal extent aligns to +Y
if ORIENT == "longY":
    dx = mx[0]-mn[0]; dy = mx[1]-mn[1]
    if dx > dy:   # longest axis is X -> rotate 90deg so it becomes Y
        for v in obj.data.vertices:
            x, y = v.co.x, v.co.y
            v.co.x = -y; v.co.y = x
        obj.data.update()
        mn, mx = bbox(obj)
        log("rotated 90deg (long axis -> +Y)")

# scale uniformly: native (no scale), by height, or by long-axis (+Y) length
if SCALE_MODE == "native":
    s = 1.0
    log(f"native scale kept (model real-world metres)")
elif SCALE_MODE == "len":
    cur = max(mx[1]-mn[1], mx[0]-mn[0])
    s = TARGET_V / cur if cur > 1e-6 else 1.0
    log(f"scale by length: current {cur:.2f} -> {TARGET_V}")
else:
    cur_h = mx[2]-mn[2]
    s = TARGET_V / cur_h if cur_h > 1e-6 else 1.0
    log(f"scale by height: current {cur_h:.2f} -> {TARGET_V}")
obj.scale = (s, s, s)
bpy.ops.object.transform_apply(scale=True)
mn, mx = bbox(obj)

# recenter footprint to X=Y=0, base z=0
cx=(mn[0]+mx[0])/2; cy=(mn[1]+mx[1])/2; cz=mn[2]
for v in obj.data.vertices:
    v.co.x -= cx; v.co.y -= cy; v.co.z -= cz
obj.data.update()
mn, mx = bbox(obj)
log(f"final bbox X[{mn[0]:.2f},{mx[0]:.2f}] Y[{mn[1]:.2f},{mx[1]:.2f}] Z[{mn[2]:.2f},{mx[2]:.2f}] "
    f"size X={mx[0]-mn[0]:.2f} Y={mx[1]-mn[1]:.2f} Z={mx[2]-mn[2]:.2f}")

# --- wire each material into an Emission of its base colour / texture, for an EMIT bake
for m in obj.data.materials:
    if m is None: continue
    if not m.use_nodes: m.use_nodes = True
    nt = m.node_tree
    bsdf = next((n for n in nt.nodes if n.type=='BSDF_PRINCIPLED'), None)
    out  = next((n for n in nt.nodes if n.type=='OUTPUT_MATERIAL'), None) or nt.nodes.new('ShaderNodeOutputMaterial')
    emit = nt.nodes.new('ShaderNodeEmission')
    if bsdf:
        bc = bsdf.inputs['Base Color']
        if bc.is_linked:
            nt.links.new(bc.links[0].from_socket, emit.inputs['Color'])
        else:
            emit.inputs['Color'].default_value = bc.default_value
    else:
        emit.inputs['Color'].default_value = (0.8,0.8,0.8,1)
    nt.links.new(emit.outputs['Emission'], out.inputs['Surface'])

# --- Smart UV unwrap (new combined UV) ---
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
# light triangulation so ObjectEditor-style export is clean + smart project robust
bpy.ops.mesh.quads_convert_to_tris(quad_method='BEAUTY', ngon_method='BEAUTY')
bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.002)
bpy.ops.object.mode_set(mode='OBJECT')

# --- bake EMIT to one 2048 atlas ---
img = bpy.data.images.new(OUTBASE+"_atlas", 2048, 2048, alpha=False)
for m in obj.data.materials:
    if m is None: continue
    nt = m.node_tree
    tnode = nt.nodes.new('ShaderNodeTexImage'); tnode.image = img
    tnode.select = True; nt.nodes.active = tnode
sc = bpy.context.scene
sc.render.engine = 'CYCLES'
try: sc.cycles.device = 'GPU'
except Exception: pass
sc.cycles.samples = 4
sc.render.bake.use_clear = True
sc.render.bake.margin = 6
log("baking EMIT 2048 ...")
bpy.ops.object.bake(type='EMIT')
img.filepath_raw = OUT_TEX; img.file_format = 'PNG'; img.save()

# --- export OBJ (triangulated, with UVs/normals) ---
bpy.ops.wm.obj_export(
    filepath=OUT_OBJ, export_selected_objects=True, export_uv=True,
    export_normals=True, export_materials=False, export_triangulated_mesh=True,
    forward_axis='Y', up_axis='Z', apply_modifiers=True,
)
me = obj.data
ntri = sum((len(p.vertices)-2) for p in me.polygons)
summary = {"verts": len(me.vertices), "tris": ntri,
           "size": [round(mx[0]-mn[0],2), round(mx[1]-mn[1],2), round(mx[2]-mn[2],2)]}
log("DONE", json.dumps(summary))
