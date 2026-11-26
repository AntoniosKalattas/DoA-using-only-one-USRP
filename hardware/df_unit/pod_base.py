"""
pod_base.py - printable base cradle of the direction-finder pod.

The pod is ONE unit that screws onto any camera tripod (1/4"-20 screw): this base holds the B210
in its metal enclosure, and pod_top.py (antennas + laser) bolts onto its four pillars.

CAD brief
- Floor with a captive 1/4"-20 hex nut (7/16" AF) in a pocket at the centre of mass, so the
  tripod screw threads straight in (alternative: drill 5.1 mm and tap 1/4-20, or a heat-set insert).
- Cavity for the enclosure ENC_W x ENC_L x ENC_H (+ CLEAR) between two low side rails; two
  strap slots through the floor (velcro strap over the enclosure).
- Four corner pillars outside the cavity, M3 holes (self-tap 2.8 mm or heat-set) for the top plate.
- Open front and back ends for the SMA / USB cables.
- Local frame (also the tripod mating datum): origin = centre of the BOTTOM face; +Y = boresight
  (towards point A); +X = right seen from behind; +Z up.
- Print: PETG/ASA, floor down, no supports, 4 perimeters, 40 % infill. Drop the nut in, then
  print-pause or press it in after.
"""
from build123d import (Align, Box, BuildPart, Cylinder, Location, Locations, Mode, Plane,
                       RegularPolygon, BuildSketch, extrude, fillet, Axis)

# B210 in its enclosure (measure yours!)
ENC_W, ENC_L, ENC_H = 105.0, 165.0, 40.0
CLEAR = 1.0
CAV_W, CAV_L = ENC_W + 2 * CLEAR, ENC_L + 2 * CLEAR

FLOOR_T = 10.0
PILLAR = 12.0                       # square pillar side
PILLAR_H = ENC_H + 2.0              # pillar top = top-plate seat (2 mm air above the enclosure)
RAIL_H, RAIL_T = 6.0, 3.0           # side rails that keep the enclosure from sliding sideways
BASE_W = CAV_W + 2 * PILLAR         # 131
BASE_L = CAV_L + 4.0                # 171
CORNER_R = 5.0

NUT_AF = 11.11 + 0.35               # 7/16" hex + print clearance
NUT_T = 5.6 + 0.4
SCREW_D = 6.6                       # 1/4" clearance
STRAP_W, STRAP_SLOT = 26.0, 3.5     # velcro strap slots (width x gap)
STRAP_Y = 45.0
M3_PILOT = 2.8                      # self-tapping M3 (or 4.0 for M3 heat-set inserts)
M3_DEPTH = 14.0

PILLAR_X = CAV_W / 2 + PILLAR / 2
PILLAR_Y = BASE_L / 2 - PILLAR / 2
TOP_SEAT_Z = FLOOR_T + PILLAR_H     # where pod_top.py's underside sits


def make_base():
    with BuildPart() as bp:
        Box(BASE_W, BASE_L, FLOOR_T, align=(Align.CENTER, Align.CENTER, Align.MIN))
        fillet(bp.edges().filter_by(Axis.Z), radius=CORNER_R)
        # side rails
        with Locations((-(CAV_W / 2 + RAIL_T / 2), 0, FLOOR_T), ((CAV_W / 2 + RAIL_T / 2), 0, FLOOR_T)):
            Box(RAIL_T, CAV_L - 2 * PILLAR, RAIL_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
        # corner pillars
        pillars = [(sx * PILLAR_X, sy * PILLAR_Y, FLOOR_T) for sx in (-1, 1) for sy in (-1, 1)]
        with Locations(*pillars):
            Box(PILLAR, PILLAR, PILLAR_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
        # 1/4"-20 screw clearance through the floor + hex nut pocket from the top
        with Locations((0, 0, -1)):
            Cylinder(SCREW_D / 2, FLOOR_T + 2, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)
        with BuildSketch(Plane.XY.offset(FLOOR_T - NUT_T)):
            RegularPolygon(NUT_AF / 3 ** 0.5, 6)
        extrude(amount=NUT_T + 1, mode=Mode.SUBTRACT)
        # strap slots (front and back of the nut)
        with Locations((0, STRAP_Y, -1), (0, -STRAP_Y, -1)):
            Box(STRAP_W, STRAP_SLOT, FLOOR_T + 2, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)
        # M3 pilots in the pillar tops
        with Locations(*[(x, y, TOP_SEAT_Z - M3_DEPTH) for x, y, _ in pillars]):
            Cylinder(M3_PILOT / 2, M3_DEPTH + 1, align=(Align.CENTER, Align.CENTER, Align.MIN), mode=Mode.SUBTRACT)
    part = bp.part
    part.label = "pod_base"
    return part


def gen_step():
    return make_base()
