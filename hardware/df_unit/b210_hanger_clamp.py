"""
b210_hanger_clamp.py - printable clamp that hangs the B210 enclosure on the tripod's centre column.

CAD brief
- Split ring around the centre column (COLUMN_D), tightened by one M4 bolt through two ears
  on the front (+Y) side; a mounting plate on the back (-Y) side takes the enclosure on 4 x M3.
- Local frame: origin on the column axis at the ring's mid-height; +Z up; +Y forward
  (same as the antenna bracket); the enclosure mounts against the plate's back face
  at y = -PLATE_BACK_Y.
- Print: PETG/ASA, ring axis vertical (prints without supports), 5 perimeters.
"""
from build123d import (Align, Box, BuildPart, Cylinder, Location, Locations, Mode)

COLUMN_D = 28.0
RING_ID = COLUMN_D + 0.5
RING_OD = 44.0
RING_H = 24.0
SLOT_W = 2.5
EAR_W, EAR_L = 10.0, 12.0          # each ear: x thickness, y length past the ring
M4_D = 4.5
NECK_W = 22.0
PLATE_W, PLATE_H, PLATE_T = 70.0, 60.0, 4.0
PLATE_FRONT_Y = 28.0                # plate front face at y = -28
PLATE_BACK_Y = PLATE_FRONT_Y + PLATE_T
M3_D = 3.4
M3_PITCH_X, M3_PITCH_Z = 50.0, 40.0


def make_clamp():
    zc = (Align.CENTER, Align.CENTER, Align.CENTER)
    with BuildPart() as bp:
        Cylinder(RING_OD / 2, RING_H, align=zc)
        # ears on the front for the clamping bolt
        with Locations((-(SLOT_W / 2 + EAR_W / 2), RING_OD / 2 + EAR_L / 2 - 4, 0),
                       ((SLOT_W / 2 + EAR_W / 2), RING_OD / 2 + EAR_L / 2 - 4, 0)):
            Box(EAR_W, EAR_L + 8, RING_H, align=zc)
        # neck + mounting plate on the back
        neck_len = PLATE_FRONT_Y - RING_OD / 2 + 4
        with Locations((0, -(RING_OD / 2 - 4 + neck_len / 2), 0)):
            Box(NECK_W, neck_len, RING_H, align=zc)
        with Locations((0, -(PLATE_FRONT_Y + PLATE_T / 2), 0)):
            Box(PLATE_W, PLATE_T, PLATE_H, align=zc)
        # bore for the column
        Cylinder(RING_ID / 2, RING_H + 2, align=zc, mode=Mode.SUBTRACT)
        # split slot through the front
        with Locations((0, RING_OD / 2 + 2, 0)):
            Box(SLOT_W, RING_OD / 2 + 12, RING_H + 2, align=zc, mode=Mode.SUBTRACT)
        # M4 clamp bolt across the ears (axis X)
        with Locations(Location((0, RING_OD / 2 + EAR_L / 2, 0), (0, 90, 0))):
            Cylinder(M4_D / 2, 2 * EAR_W + SLOT_W + 4, align=zc, mode=Mode.SUBTRACT)
        # 4 x M3 for the enclosure (axis Y through the plate)
        pts = [(sx * M3_PITCH_X / 2, -(PLATE_FRONT_Y + PLATE_T / 2), sz * M3_PITCH_Z / 2)
               for sx in (-1, 1) for sz in (-1, 1)]
        with Locations(*[Location(p, (90, 0, 0)) for p in pts]):
            Cylinder(M3_D / 2, PLATE_T + 2, align=zc, mode=Mode.SUBTRACT)
    part = bp.part
    part.label = "b210_hanger_clamp"
    return part


def gen_step():
    return make_clamp()
