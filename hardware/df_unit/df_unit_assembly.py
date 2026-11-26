"""
df_unit_assembly.py - the complete B210 direction-finder field unit on its tripod.

CAD brief
- World frame: origin on the ground under the tripod centre; +Z up; +Y = boresight (towards
  point A); +X = right as seen from behind the unit (radar convention: right = +).
- Printed parts (own generators, own STEP/STL): antenna_bracket.py, b210_hanger_clamp.py.
- Bought parts are simplified ENVELOPES (no vendor STEP available offline), sized from typical
  datasheets: VERT2450-style dual-band whip (~195 mm), SMA bulkhead jacks, 12 mm laser module,
  B210 in a 105 x 40 x 165 mm metal enclosure, camera tripod with a 28 mm centre column.
- Placement datums:
    tripod head top            z = HEAD_TOP_Z      (bracket hub bottom sits here, 1/4-20 stud)
    antenna seat (plate top)   z = HEAD_TOP_Z + 18 (antenna_bracket.PLATE_TOP)
    clamp ring mid-height      z = CLAMP_Z on the column axis
    enclosure front face       against the clamp plate back face (y = -PLATE_BACK_Y)
  => antenna feed points at about 1.45 m, whip midpoint about 1.55 m.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build123d import (Align, Box, BuildLine, BuildPart, BuildSketch, Circle, Color, Cylinder,
                       Location, Locations, Plane, RegularPolygon, Sphere, Spline, Vector,
                       extrude, sweep)
from cadpy.assembly import AssemblyHelper

import antenna_bracket as AB
import b210_hanger_clamp as HC

# ---------------------------------------------------------------- parameters (mm)
HEAD_TOP_Z = 1420.0
HEAD_D, HEAD_H = 50.0, 40.0
STUD_D, STUD_L = 6.35, 8.0
COLUMN_D = HC.COLUMN_D
COLUMN_Z0 = 950.0
SPIDER_D, SPIDER_Z0, SPIDER_H = 90.0, 1120.0, 40.0
LEG_D = 25.0
LEG_TOP_R = 40.0                 # legs start on the spider at this radius
FOOT_R = 620.0                   # footprint radius on the ground
LEG_AZIMUTHS = (-90.0, 30.0, 150.0)   # one leg straight back, two forward-left/right

CLAMP_Z = 1300.0
ENC_W, ENC_D, ENC_H = 105.0, 40.0, 165.0
ENC_TOP_Z = HEAD_TOP_Z - HEAD_H          # enclosure top level with the head's underside
PORT_X = 25.0                            # RF A / RF B ports on the enclosure top
PORT_D, PORT_H = 9.0, 6.0

ANT_HEX_AF, ANT_HEX_H = 8.0, 8.0
ANT_HINGE_D, ANT_HINGE_H = 12.0, 16.0
ANT_WHIP_D, ANT_WHIP_L = 10.0, 166.0
SMA_NUT_T = 2.5
SMA_BODY_D, SMA_BODY_BELOW = 6.35, 16.0
SMA_REAR_D = 8.0

LASER_L = 35.0
LASER_FRONT_OUT = 5.0            # how far the module sticks out of the holder's front
BEAM_L = 600.0                   # visual indicator only
CABLE_D = 3.6                    # RG316 SMA jumpers
USB_D = 4.5

C_PRINT = Color(0.91, 0.45, 0.16)      # orange PETG
C_DARK = Color(0.13, 0.16, 0.19)
C_METAL = Color(0.62, 0.66, 0.70)
C_BRASS = Color(0.80, 0.68, 0.35)
C_ENC = Color(0.20, 0.27, 0.33)
C_CABLE = Color(0.10, 0.10, 0.10)
C_USB = Color(0.18, 0.61, 0.58)
C_BEAM = Color(0.95, 0.15, 0.10)


# ---------------------------------------------------------------- helpers
def rod(p0, p1, d):
    """Cylinder of diameter d from point p0 to point p1."""
    p0, p1 = Vector(*p0), Vector(*p1)
    v = p1 - p0
    cyl = Cylinder(d / 2, v.length, align=(Align.CENTER, Align.CENTER, Align.MIN))
    return cyl.moved(Location(Plane(origin=p0, z_dir=v)))


def tube_along(points, d):
    """Round cable swept along a spline through the points."""
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


# ---------------------------------------------------------------- bought-part envelopes
def make_antenna():
    """VERT2450-style whip. Origin = centre of its SMA base face; +Z along the whip."""
    parts = [hex_prism(ANT_HEX_AF, ANT_HEX_H),
             Cylinder(ANT_HINGE_D / 2, ANT_HINGE_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
             .moved(Location((0, 0, ANT_HEX_H))),
             Cylinder(ANT_WHIP_D / 2, ANT_WHIP_L, align=(Align.CENTER, Align.CENTER, Align.MIN))
             .moved(Location((0, 0, ANT_HEX_H + ANT_HINGE_H))),
             Sphere(ANT_WHIP_D / 2).moved(Location((0, 0, ANT_HEX_H + ANT_HINGE_H + ANT_WHIP_L)))]
    s = parts[0]
    for p in parts[1:]:
        s = s.fuse(p)
    return s.clean()


def make_sma_bulkhead():
    """SMA bulkhead jack. Origin = on its axis at the plate's TOP face; plate is AB.PLATE_T thick."""
    s = Cylinder(SMA_BODY_D / 2, SMA_BODY_BELOW + 6, align=(Align.CENTER, Align.CENTER, Align.MIN)) \
        .moved(Location((0, 0, -SMA_BODY_BELOW)))
    s = s.fuse(hex_prism(8.0, SMA_NUT_T, 0.0))                       # top nut on the plate
    s = s.fuse(hex_prism(8.0, SMA_NUT_T, -AB.PLATE_T - SMA_NUT_T))   # bottom nut under the plate
    s = s.fuse(Cylinder(SMA_REAR_D / 2, SMA_BODY_BELOW - AB.PLATE_T - SMA_NUT_T,
                        align=(Align.CENTER, Align.CENTER, Align.MIN))
               .moved(Location((0, 0, -SMA_BODY_BELOW))))
    return s.clean()


def make_laser():
    """12 mm laser module, axis along +Y. Origin = centre of its front face."""
    return Cylinder(AB.LASER_D / 2, LASER_L, align=(Align.CENTER, Align.CENTER, Align.MAX)) \
        .moved(Location((0, 0, 0), (-90, 0, 0)))


def make_enclosure():
    """B210 in a metal box, standing upright. Origin = centre of its FRONT (+Y) face."""
    box = Box(ENC_W, ENC_D, ENC_H, align=(Align.CENTER, Align.MAX, Align.CENTER))
    for sx in (-1, 1):   # RF A / RF B SMA jacks on the top face
        box = box.fuse(Cylinder(PORT_D / 2, PORT_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
                       .moved(Location((sx * PORT_X, -ENC_D / 2, ENC_H / 2))))
    box = box.fuse(Box(14, 10, 8, align=(Align.CENTER, Align.CENTER, Align.MAX))   # USB 3 socket
                   .moved(Location((0, -ENC_D / 2, -ENC_H / 2))))
    return box.clean()


def make_tripod():
    parts = []
    head = Cylinder(HEAD_D / 2, HEAD_H, align=(Align.CENTER, Align.CENTER, Align.MAX)) \
        .moved(Location((0, 0, HEAD_TOP_Z)))
    stud = Cylinder(STUD_D / 2, STUD_L, align=(Align.CENTER, Align.CENTER, Align.MIN)) \
        .moved(Location((0, 0, HEAD_TOP_Z)))
    column = Cylinder(COLUMN_D / 2, HEAD_TOP_Z - HEAD_H - COLUMN_Z0,
                      align=(Align.CENTER, Align.CENTER, Align.MIN)).moved(Location((0, 0, COLUMN_Z0)))
    spider = Cylinder(SPIDER_D / 2, SPIDER_H, align=(Align.CENTER, Align.CENTER, Align.MIN)) \
        .moved(Location((0, 0, SPIDER_Z0)))
    parts += [("tripod_head", head, C_DARK), ("tripod_stud_quarter20", stud, C_METAL),
              ("tripod_centre_column", column, C_METAL), ("tripod_spider", spider, C_DARK)]
    zl = SPIDER_Z0 + SPIDER_H / 2
    for i, az in enumerate(LEG_AZIMUTHS):
        a = math.radians(az)
        top = (LEG_TOP_R * math.cos(a), LEG_TOP_R * math.sin(a), zl)
        foot = (FOOT_R * math.cos(a), FOOT_R * math.sin(a), LEG_D / 2)
        parts.append((f"tripod_leg:{['back', 'front_right', 'front_left'][i]}", rod(top, foot, LEG_D), C_DARK))
        parts.append((f"tripod_foot:{['back', 'front_right', 'front_left'][i]}",
                      Sphere(LEG_D * 0.7).moved(Location(foot)), C_DARK))
    return parts


# ---------------------------------------------------------------- assembly
def build():
    asm = AssemblyHelper("df_field_unit")

    # tripod (root, fixed)
    for name, shape, color in make_tripod():
        asm.add(shape, name, color=color)

    # printed antenna bracket on the head (hub bottom = head top)
    bracket_loc = Location((0, 0, HEAD_TOP_Z))
    asm.add(AB.make_bracket().moved(bracket_loc), "antenna_bracket", color=C_PRINT)
    plate_top = HEAD_TOP_Z + AB.PLATE_TOP

    # SMA bulkheads + antennas: RF A left (-X), RF B right (+X)
    for side, sx in (("rf_a_left", -1), ("rf_b_right", 1)):
        x = sx * AB.ANTENNA_SPACING / 2
        asm.add(make_sma_bulkhead().moved(Location((x, AB.SMA_Y, plate_top))), "sma_bulkhead", side,
                color=C_BRASS)
        asm.add(make_antenna().moved(Location((x, AB.SMA_Y, plate_top + SMA_NUT_T))), "antenna_vert2450", side,
                color=C_DARK)

    # laser module in the holder, front face LASER_FRONT_OUT past the plate's front edge
    laser_front_y = AB.PLATE_Y / 2 + LASER_FRONT_OUT
    laser_z = HEAD_TOP_Z + AB.LASER_Z
    asm.add(make_laser().moved(Location((0, laser_front_y, laser_z))), "laser_module", color=C_METAL)
    asm.add(rod((0, laser_front_y, laser_z), (0, laser_front_y + BEAM_L, laser_z), 2.0),
            "laser_beam_indicator", color=C_BEAM)

    # hanger clamp on the column, enclosure on its back plate
    asm.add(HC.make_clamp().moved(Location((0, 0, CLAMP_Z))), "b210_hanger_clamp", color=C_PRINT)
    enc_center_z = ENC_TOP_Z - ENC_H / 2
    enc_front_y = -HC.PLATE_BACK_Y
    asm.add(make_enclosure().moved(Location((0, enc_front_y, enc_center_z))), "b210_enclosure", color=C_ENC)

    # SMA jumpers: bulkhead rear end -> enclosure top jack
    enc_mid_y = enc_front_y - ENC_D / 2
    bulk_bottom = plate_top - SMA_BODY_BELOW
    port_top = ENC_TOP_Z + PORT_H
    for side, sx in (("rf_a_left", -1), ("rf_b_right", 1)):
        xb, xp = sx * AB.ANTENNA_SPACING / 2, sx * PORT_X
        pts = [(xb, AB.SMA_Y, bulk_bottom), (xb, AB.SMA_Y - 2, bulk_bottom - 14),
               (0.5 * (xb + xp), 0.5 * (AB.SMA_Y + enc_mid_y), port_top + 18),
               (xp, enc_mid_y, port_top + 12), (xp, enc_mid_y, port_top)]
        asm.add(tube_along(pts, CABLE_D), "sma_jumper", side, color=C_CABLE)

    # USB 3 cable from the enclosure bottom down the back leg (offset 40 mm to its right)
    usb0 = (0, enc_mid_y, enc_center_z - ENC_H / 2 - 8)
    asm.add(tube_along([usb0, (32, enc_mid_y - 10, usb0[2] - 80), (40, -300, 620), (40, -560, 90),
                        (40, -760, USB_D / 2)], USB_D), "usb3_cable", color=C_USB)
    return asm.build()


def gen_step():
    return build()
