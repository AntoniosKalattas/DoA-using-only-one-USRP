"""
df_pod_assembly.py - the direction-finder POD on a standard camera tripod.

The whole receiver is one pod (pod_base.py + pod_top.py, bolted together) that screws onto the
tripod's own 1/4"-20 camera screw. Nothing is clamped to the column any more.

CAD brief
- World frame: origin on the ground under the tripod centre; +Z up; +Y = boresight (towards
  point A); +X = right seen from behind (radar convention: right = +).
- Printed: pod_base (cradle + captive 1/4"-20 nut), pod_top (antennas 60 mm apart + laser).
- Bought parts are size ENVELOPES: VERT2450-style whips (~195 mm), SMA bulkhead jacks, 12 mm laser,
  B210 in a 105 x 165 x 40 mm metal enclosure (RF A/B jacks on its back end, USB 3 on the front),
  1/4"-20 hex nut, camera tripod with the head top at HEAD_TOP_Z.
- Datums: pod_base bottom face = tripod head top; pod_top bottom = pod_base.TOP_SEAT_Z;
  enclosure bottom = pod floor top; antenna seat = pod_top top face.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build123d import (Align, Box, BuildLine, BuildPart, BuildSketch, Circle, Color, Cylinder,
                       Location, Plane, RegularPolygon, Sphere, Spline, Vector, extrude, sweep)
from cadpy.assembly import AssemblyHelper

import pod_base as PB
import pod_top as PT

# ---------------------------------------------------------------- tripod (standard camera tripod)
HEAD_TOP_Z = 1420.0
HEAD_D, HEAD_H = 50.0, 40.0
STUD_D, STUD_L = 6.35, 6.0
COLUMN_D, COLUMN_Z0 = 28.0, 950.0
SPIDER_D, SPIDER_Z0, SPIDER_H = 90.0, 1120.0, 40.0
LEG_D, LEG_TOP_R, FOOT_R = 25.0, 40.0, 620.0
LEG_AZIMUTHS = (-90.0, 30.0, 150.0)

# ---------------------------------------------------------------- bought parts
ANT_HEX_AF, ANT_HEX_H = 8.0, 8.0
ANT_HINGE_D, ANT_HINGE_H = 12.0, 16.0
ANT_WHIP_D, ANT_WHIP_L = 10.0, 166.0
SMA_NUT_T, SMA_BODY_D, SMA_BODY_BELOW, SMA_REAR_D = 2.5, 6.35, 16.0, 8.0
PORT_X, PORT_D, PORT_L = 25.0, 9.0, 8.0          # enclosure RF jacks on its back (-Y) end
LASER_L, LASER_FRONT_OUT, BEAM_L = 35.0, 5.0, 600.0
CABLE_D, USB_D = 3.6, 4.5

C_PRINT = Color(0.91, 0.45, 0.16)
C_PRINT2 = Color(0.95, 0.62, 0.25)
C_DARK = Color(0.13, 0.16, 0.19)
C_METAL = Color(0.62, 0.66, 0.70)
C_BRASS = Color(0.80, 0.68, 0.35)
C_ENC = Color(0.20, 0.27, 0.33)
C_CABLE = Color(0.10, 0.10, 0.10)
C_USB = Color(0.18, 0.61, 0.58)
C_BEAM = Color(0.95, 0.15, 0.10)


def rod(p0, p1, d):
    p0, p1 = Vector(*p0), Vector(*p1)
    v = p1 - p0
    return Cylinder(d / 2, v.length, align=(Align.CENTER, Align.CENTER, Align.MIN)) \
        .moved(Location(Plane(origin=p0, z_dir=v)))


def tube_along(points, d):
    pts = [Vector(*p) for p in points]
    with BuildPart() as bp:
        with BuildLine() as path:
            Spline(*pts)
        with BuildSketch(Plane(origin=pts[0], z_dir=path.line % 0)):
            Circle(d / 2)
        sweep(path=path.line)
    return bp.part


def hex_prism(af, h, z0=0.0):
    with BuildPart() as bp:
        with BuildSketch(Plane.XY.offset(z0)):
            RegularPolygon(af / math.sqrt(3), 6)
        extrude(amount=h)
    return bp.part


def make_antenna():
    """Origin = centre of the SMA base face; +Z along the whip."""
    s = hex_prism(ANT_HEX_AF, ANT_HEX_H)
    s = s.fuse(Cylinder(ANT_HINGE_D / 2, ANT_HINGE_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
               .moved(Location((0, 0, ANT_HEX_H))))
    s = s.fuse(Cylinder(ANT_WHIP_D / 2, ANT_WHIP_L, align=(Align.CENTER, Align.CENTER, Align.MIN))
               .moved(Location((0, 0, ANT_HEX_H + ANT_HINGE_H))))
    s = s.fuse(Sphere(ANT_WHIP_D / 2).moved(Location((0, 0, ANT_HEX_H + ANT_HINGE_H + ANT_WHIP_L))))
    return s.clean()


def make_sma_bulkhead():
    """Origin = on its axis at the plate's TOP face."""
    s = Cylinder(SMA_BODY_D / 2, SMA_BODY_BELOW + 6, align=(Align.CENTER, Align.CENTER, Align.MIN)) \
        .moved(Location((0, 0, -SMA_BODY_BELOW)))
    s = s.fuse(hex_prism(8.0, SMA_NUT_T, 0.0))
    s = s.fuse(hex_prism(8.0, SMA_NUT_T, -PT.PLATE_T - SMA_NUT_T))
    s = s.fuse(Cylinder(SMA_REAR_D / 2, SMA_BODY_BELOW - PT.PLATE_T - SMA_NUT_T,
                        align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((0, 0, -SMA_BODY_BELOW))))
    return s.clean()


def make_laser():
    """Axis along +Y. Origin = centre of its front face."""
    return Cylinder(PT.LASER_D / 2, LASER_L, align=(Align.CENTER, Align.CENTER, Align.MAX)) \
        .moved(Location((0, 0, 0), (-90, 0, 0)))


def make_enclosure():
    """B210 in its metal box, lying flat. Origin = centre of its BOTTOM face."""
    box = Box(PB.ENC_W, PB.ENC_L, PB.ENC_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for sx in (-1, 1):   # RF A / RF B jacks on the back end
        box = box.fuse(Cylinder(PORT_D / 2, PORT_L, align=(Align.CENTER, Align.CENTER, Align.MIN))
                       .moved(Location((sx * PORT_X, -PB.ENC_L / 2, PB.ENC_H / 2), (90, 0, 0))))
    box = box.fuse(Box(14, 8, 8, align=(Align.CENTER, Align.MIN, Align.CENTER))   # USB 3 socket, front
                   .moved(Location((0, PB.ENC_L / 2, PB.ENC_H / 2))))
    return box.clean()


def make_hex_nut():
    """1/4"-20 hex nut. Origin = centre of its bottom face."""
    return hex_prism(11.11, 5.6).cut(Cylinder(3.2, 8, align=(Align.CENTER, Align.CENTER, Align.MIN))
                                     .moved(Location((0, 0, -1))))


def make_tripod():
    parts = [("tripod_head", Cylinder(HEAD_D / 2, HEAD_H, align=(Align.CENTER, Align.CENTER, Align.MAX))
              .moved(Location((0, 0, HEAD_TOP_Z))), C_DARK),
             ("tripod_camera_screw_quarter20", Cylinder(STUD_D / 2, STUD_L, align=(Align.CENTER, Align.CENTER, Align.MIN))
              .moved(Location((0, 0, HEAD_TOP_Z))), C_METAL),
             ("tripod_centre_column", Cylinder(COLUMN_D / 2, HEAD_TOP_Z - HEAD_H - COLUMN_Z0,
                                               align=(Align.CENTER, Align.CENTER, Align.MIN))
              .moved(Location((0, 0, COLUMN_Z0))), C_METAL),
             ("tripod_spider", Cylinder(SPIDER_D / 2, SPIDER_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
              .moved(Location((0, 0, SPIDER_Z0))), C_DARK)]
    zl = SPIDER_Z0 + SPIDER_H / 2
    for name, az in zip(("back", "front_right", "front_left"), LEG_AZIMUTHS):
        a = math.radians(az)
        top = (LEG_TOP_R * math.cos(a), LEG_TOP_R * math.sin(a), zl)
        foot = (FOOT_R * math.cos(a), FOOT_R * math.sin(a), LEG_D / 2)
        parts.append((f"tripod_leg:{name}", rod(top, foot, LEG_D), C_DARK))
        parts.append((f"tripod_foot:{name}", Sphere(LEG_D * 0.7).moved(Location(foot)), C_DARK))
    return parts


def build():
    asm = AssemblyHelper("df_pod_on_tripod")
    for name, shape, color in make_tripod():
        asm.add(shape, name, color=color)

    z0 = HEAD_TOP_Z                                     # pod bottom sits on the tripod head
    asm.add(PB.make_base().moved(Location((0, 0, z0))), "pod_base", color=C_PRINT)
    asm.add(make_hex_nut().moved(Location((0, 0, z0 + PB.FLOOR_T - PB.NUT_T))), "hex_nut_quarter20",
            color=C_METAL)
    asm.add(make_enclosure().moved(Location((0, 0, z0 + PB.FLOOR_T))), "b210_enclosure", color=C_ENC)
    top_z = z0 + PB.TOP_SEAT_Z
    asm.add(PT.make_top().moved(Location((0, 0, top_z))), "pod_top", color=C_PRINT2)
    plate_top = top_z + PT.PLATE_T

    for side, sx in (("rf_a_left", -1), ("rf_b_right", 1)):
        x = sx * PT.ANTENNA_SPACING / 2
        asm.add(make_sma_bulkhead().moved(Location((x, PT.SMA_Y, plate_top))), "sma_bulkhead", side, color=C_BRASS)
        asm.add(make_antenna().moved(Location((x, PT.SMA_Y, plate_top + SMA_NUT_T))), "antenna_vert2450", side,
                color=C_DARK)
        # jumper: enclosure back-end jack -> loop behind -> bulkhead rear end
        port = (sx * PORT_X, -PB.ENC_L / 2 - PORT_L, z0 + PB.FLOOR_T + PB.ENC_H / 2)
        bulk = (x, PT.SMA_Y, plate_top - SMA_BODY_BELOW)
        pts = [port, (sx * PORT_X, port[1] - 10, port[2] - 2), (sx * 29, PT.SMA_Y - 14, port[2] + 6),
               (x, PT.SMA_Y - 6, bulk[2] - 8), bulk]
        asm.add(tube_along(pts, CABLE_D), "sma_jumper", side, color=C_CABLE)

    laser_front_y = PT.FRONT_Y + LASER_FRONT_OUT
    laser_z = top_z + PT.LASER_Z
    asm.add(make_laser().moved(Location((0, laser_front_y, laser_z))), "laser_module", color=C_METAL)
    asm.add(rod((0, laser_front_y, laser_z), (0, laser_front_y + BEAM_L, laser_z), 2.0),
            "laser_beam_indicator", color=C_BEAM)

    usb0 = (0, PB.ENC_L / 2 + 8, z0 + PB.FLOOR_T + PB.ENC_H / 2)
    asm.add(tube_along([usb0, (0, usb0[1] + 30, usb0[2] - 30), (-60, 200, 900), (-200, 300, 300),
                        (-260, 330, USB_D / 2)], USB_D), "usb3_cable", color=C_USB)
    return asm.build()


def gen_step():
    return build()
