"""
pod_top.py - printable antenna top plate of the direction-finder pod.

Bolts onto pod_base.py's four pillars (4 x M3 x 16). Carries the two SMA bulkheads exactly
ANTENNA_SPACING apart and a 12 mm laser on the boresight.

CAD brief
- Plate PLATE_X x (base length + overhang) x PLATE_T. The SMA bulkheads sit on a rear overhang,
  behind the enclosure, so their rear bodies hang in free air and the jumpers to the B210's
  RF A / RF B jacks stay a few cm long.
- Laser holder (12 mm bore + sight groove) at the front edge, centred on the boresight.
- Local frame: origin = centre of the plate's BOTTOM face at the base's centre line, i.e. the
  same X/Y as pod_base.py; +Y boresight; +X right seen from behind; +Z up.
- Print: plate face down, no supports.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build123d import (Align, Axis, Box, BuildPart, Cylinder, Location, Locations, Mode, fillet)

import pod_base as PB

ANTENNA_SPACING = 60.0              # lambda/2 at 2.48 GHz: no wrap-around at any angle
PLATE_X, PLATE_T = 140.0, 8.0
REAR_OVERHANG = 26.0                # past the base's back end, for the bulkheads
FRONT_Y = PB.BASE_L / 2
BACK_Y = -PB.BASE_L / 2 - REAR_OVERHANG
SMA_Y = -PB.BASE_L / 2 - REAR_OVERHANG / 2
SMA_HOLE_D = 6.6
LASER_D = 12.0
LASER_BORE = LASER_D + 0.3
HOLDER_W, HOLDER_L, HOLDER_H = 22.0, 20.0, 24.0
NOTCH_W, NOTCH_D = 2.0, 1.5
M3_CLEAR = 3.4
CORNER_R = 5.0
LASER_Z = PLATE_T + HOLDER_H / 2    # laser axis above the plate's bottom face


def make_top():
    plate_len = FRONT_Y - BACK_Y
    yc = (FRONT_Y + BACK_Y) / 2
    with BuildPart() as bp:
        with Locations((0, yc, 0)):
            Box(PLATE_X, plate_len, PLATE_T, align=(Align.CENTER, Align.CENTER, Align.MIN))
        fillet(bp.edges().filter_by(Axis.Z), radius=CORNER_R)
        # laser holder at the front edge
        hy = FRONT_Y - HOLDER_L / 2
        with Locations((0, hy, PLATE_T - 0.5)):
            Box(HOLDER_W, HOLDER_L, HOLDER_H + 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
        with Locations(Location((0, hy, LASER_Z), (90, 0, 0))):
            Cylinder(LASER_BORE / 2, HOLDER_L + 2, mode=Mode.SUBTRACT)
        with Locations((0, hy, PLATE_T + HOLDER_H - NOTCH_D)):
            Box(NOTCH_W, HOLDER_L + 2, NOTCH_D + 1, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)
        # SMA bulkheads on the rear overhang: RF A left (-X), RF B right (+X)
        with Locations((-ANTENNA_SPACING / 2, SMA_Y, -1), (ANTENNA_SPACING / 2, SMA_Y, -1)):
            Cylinder(SMA_HOLE_D / 2, PLATE_T + 2, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)
        # M3 clearance over the base pillars
        with Locations(*[(sx * PB.PILLAR_X, sy * PB.PILLAR_Y, -1) for sx in (-1, 1) for sy in (-1, 1)]):
            Cylinder(M3_CLEAR / 2, PLATE_T + 2, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)
    part = bp.part
    part.label = "pod_top"
    return part


def gen_step():
    return make_top()
