import math
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "src" / "assets"
ASSET_DIR.mkdir(parents=True, exist_ok=True)


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def mat(name, color, metallic=0.0, roughness=0.35, alpha=1.0):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = color
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Alpha"].default_value = alpha
    material.blend_method = "BLEND"
    material.use_screen_refraction = True
    return material


def add_uv_sphere(name, loc, scale, material, segments=48, ring_count=24):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=ring_count, location=loc)
    obj = bpy.context.object
    obj.name = name
    obj.scale = scale
    obj.data.materials.append(material)
    return obj


def add_capsule(name, loc, radius, depth, material, rotation=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(location=loc, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = (radius, radius, depth)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    bevel = obj.modifiers.new("soft suit bevel", "BEVEL")
    bevel.width = radius * 0.5
    bevel.segments = 18
    obj.modifiers.new("suit smooth", "WEIGHTED_NORMAL")
    obj.data.materials.append(material)
    return obj


def add_torus(name, loc, major, minor, material, rotation=(0, 0, 0)):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, major_segments=96, minor_segments=18, location=loc, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(material)
    return obj


def build_astronaut(name, accent, secondary, output_name, pose_shift=0.0):
    clear_scene()

    white = mat("soft ceramic suit", (0.86, 0.92, 1.0, 1), roughness=0.42)
    glass = mat("dark reflective visor", (0.025, 0.04, 0.075, 0.92), metallic=0.0, roughness=0.08, alpha=0.92)
    accent_mat = mat("emissive accent", accent, metallic=0.12, roughness=0.18)
    secondary_mat = mat("secondary glow", secondary, metallic=0.0, roughness=0.2)
    shadow = mat("deep seams", (0.06, 0.08, 0.13, 1), roughness=0.5)

    body = add_uv_sphere("rounded eva torso", (0, 0, 0.0), (0.72, 0.5, 0.88), white)
    helmet = add_uv_sphere("helmet shell", (0, 0, 1.02), (0.58, 0.58, 0.58), white)
    visor = add_uv_sphere("glass visor", (0, -0.48, 1.03), (0.36, 0.08, 0.24), glass, 64, 16)
    visor.rotation_euler[0] = math.radians(4)

    add_torus("helmet ring", (0, -0.01, 0.62), 0.51, 0.035, accent_mat, (math.radians(90), 0, 0))
    add_torus("waist orbit belt", (0, -0.01, -0.47), 0.47, 0.026, secondary_mat, (math.radians(90), 0, 0))

    add_capsule("left arm", (-0.66, -0.02, 0.23), 0.24, 0.98, white, (0, math.radians(22 + pose_shift), math.radians(-18)))
    add_capsule("right arm", (0.66, -0.02, 0.22), 0.24, 0.98, white, (0, math.radians(-22 - pose_shift), math.radians(18)))
    add_uv_sphere("left glove", (-0.96, -0.18, -0.14), (0.18, 0.18, 0.18), accent_mat)
    add_uv_sphere("right glove", (0.96, -0.18, -0.14), (0.18, 0.18, 0.18), accent_mat)

    add_capsule("left boot leg", (-0.25, -0.02, -0.88), 0.25, 0.74, white, (math.radians(8), math.radians(-8), 0))
    add_capsule("right boot leg", (0.25, -0.02, -0.88), 0.25, 0.74, white, (math.radians(-8), math.radians(8), 0))
    add_uv_sphere("left boot", (-0.31, -0.19, -1.31), (0.21, 0.16, 0.15), shadow)
    add_uv_sphere("right boot", (0.31, -0.19, -1.31), (0.21, 0.16, 0.15), shadow)

    pack = add_capsule("life support backpack", (0, 0.4, 0.15), 0.62, 0.42, shadow, (math.radians(90), 0, 0))
    pack.scale.x = 0.72
    add_uv_sphere("chest light", (0.0, -0.46, 0.1), (0.13, 0.045, 0.13), accent_mat)
    add_uv_sphere("visor glint", (-0.12, -0.545, 1.14), (0.08, 0.018, 0.05), secondary_mat)

    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            try:
                bpy.ops.object.shade_smooth()
            except Exception:
                pass
            obj.select_set(False)

    bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0, 0, 0))
    target = bpy.context.object
    for obj in bpy.context.scene.objects:
        if obj.name != target.name:
            obj.rotation_euler[2] += math.radians(pose_shift)
            obj.scale *= 0.76

    bpy.ops.object.light_add(type="AREA", location=(0, -4.2, 4.0))
    key = bpy.context.object
    key.name = f"{name} key light"
    key.data.energy = 650
    key.data.size = 5.0

    bpy.ops.object.light_add(type="POINT", location=(-2.4, -2.2, 1.6))
    rim = bpy.context.object
    rim.name = f"{name} neon rim"
    rim.data.energy = 130
    rim.data.color = accent[:3]

    bpy.ops.object.camera_add(location=(0, -7.2, 0.42), rotation=(math.radians(86), 0, 0))
    camera = bpy.context.object
    bpy.context.scene.camera = camera
    camera.data.lens = 56
    camera.data.dof.use_dof = True
    camera.data.dof.focus_distance = 5.0
    camera.data.dof.aperture_fstop = 6.5

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 96
    scene.view_settings.view_transform = "Filmic"
    scene.view_settings.look = "Medium High Contrast"
    scene.render.film_transparent = True
    scene.render.resolution_x = 720
    scene.render.resolution_y = 720
    scene.render.filepath = str(ASSET_DIR / output_name)
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    bpy.ops.render.render(write_still=True)


build_astronaut("Raka", (0.25, 0.95, 1.0, 1), (0.58, 0.73, 1.0, 1), "raka-astronaut.png", -8)
build_astronaut("Clara", (1.0, 0.38, 0.86, 1), (0.78, 0.64, 1.0, 1), "clara-astronaut.png", 8)
