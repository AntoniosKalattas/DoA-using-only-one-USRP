"""
antenna_bracket.py - printable antenna bracket for the B210 field direction finder.

CAD brief
- Holds two SMA bulkhead jacks exactly ANTENNA_SPACING apart (60.0 mm = lambda/2 at 2.48 GHz),
  a 12 mm laser module on the boresight, and sits on a tripod via a 1/4-20 heat-set insert.
- Local frame (also the tripod-head mating datum):
    origin = centre of the hub boss's bottom face (sits on the tripod head)
    +Y     = boresight / forward (towards point A)
    +X     = right as seen from behind the unit (matches the radar: right = +)
    +Z     = up
- RF A antenna hole at -X (left), RF B at +X (right).
- Print: PETG/ASA, 4 perimeters, 40 % infill, plate face down. Insert: 1/4-20 heat-set
  (8.0 mm hole, ~12 mm long) pressed in from below.
"""
from build123d import (Align, Box, BuildPart, Cylinder, Location, Locations, Mode, Plane,
                       Axis, Rot, add, fillet, chamfer)

# ---- parameters (mm)
PLATE_X, PLATE_Y, PLATE_T = 140.0, 40.0, 8.0
HUB_D, HUB_H = 30.0, 10.0                 # boss under the plate that sits on the tripod head
ANTENNA_SPACING = 60.0                     # centre-to-centre of the two SMA holes
SMA_HOLE_D = 6.6                           # 1/4-36 UNS SMA bulkhead thread (6.35) + clearance
SMA_Y = -6.0                               # holes slightly behind centre, laser in front
INSERT_D, INSERT_DEPTH = 8.0, 12.0         # 1/4-20 heat-set insert pilot
LASER_D = 12.0                             # laser module barrel
LASER_BORE = LASER_D + 0.3
HOLDER_W, HOLDER_Y0, HOLDER_H = 22.0, 4.0, 24.0   # holder block: x +-11, y 4..20, above plate
NOTCH_W, NOTCH_D = 2.0, 1.5                # sight groove on top of the holder
CORNER_R = 4.0

PLATE_Z0 = HUB_H                           # plate bottom
PLATE_TOP = HUB_H + PLATE_T                # plate top (antenna / bulkhead nut seat)
LASER_Z = PLATE_TOP + HOLDER_H / 2         # laser axis height in local frame


def make_bracket():
    with BuildPart() as bp:
        # plate
        with Locations((0, 0, PLATE_Z0)):
            Box(PLATE_X, PLATE_Y, PLATE_T, align=(Align.CENTER, Align.CENTER, Align.MIN))
        fillet(bp.edges().filter_by(Axis.Z), radius=CORNER_R)
        # hub boss under the plate
        Cylinder(HUB_D / 2, HUB_H + 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
        # laser holder block on top, front centre
        holder_len = PLATE_Y / 2 - HOLDER_Y0
        with Locations((0, HOLDER_Y0 + holder_len / 2, PLATE_TOP - 0.5)):
            Box(HOLDER_W, holder_len, HOLDER_H + 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
        # SMA bulkhead holes (through plate)
        with Locations((-ANTENNA_SPACING / 2, SMA_Y, PLATE_Z0 - 1), (ANTENNA_SPACING / 2, SMA_Y, PLATE_Z0 - 1)):
            Cylinder(SMA_HOLE_D / 2, PLATE_T + 2, align=(Align.CENTER, Align.CENTER, Align.MIN),
                     mode=Mode.SUBTRACT)
        # 1/4-20 heat-set insert pilot from below
        with Locations((0, 0, -1)):
            Cylinder(INSERT_D / 2, INSERT_DEPTH + 1, align=(Align.CENTER, Align.CENTER, Align.MIN),
                     mode=Mode.SUBTRACT)
        # laser bore along +Y through the holder
        with Locations(Location((0, HOLDER_Y0 + holder_len / 2, LASER_Z), (90, 0, 0))):
            Cylinder(LASER_BORE / 2, holder_len + 2, mode=Mode.SUBTRACT)
        # sight groove along the top of the holder (backup aiming without the laser)
        with Locations((0, HOLDER_Y0 + holder_len / 2, PLATE_TOP + HOLDER_H - NOTCH_D)):
            Box(NOTCH_W, holder_len + 2, NOTCH_D + 1, align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT)
    part = bp.part
    part.label = "antenna_bracket"
    return part


def gen_step():
    return make_bracket()
