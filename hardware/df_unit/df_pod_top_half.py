"""
df_pod_top_half.py - the upper half of df_pod_assembly: everything from the tripod's middle up
(centre column, head, camera screw, pod with B210, antennas, jumpers, laser), without the legs,
spider and the long USB cable. Same world frame and placements as df_pod_assembly.py, so the
two files line up; only the parts below the middle are left out and the USB cable is cut short.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cadpy.assembly import AssemblyHelper

import df_pod_assembly as A
import pod_base as PB

DROP = ("tripod_leg", "tripod_foot", "tripod_spider", "usb3_cable", "laser_beam_indicator")
USB_STUB_DROP = 120.0      # how far the shortened USB cable hangs below the pod


def build():
    full = A.build()
    asm = AssemblyHelper("df_pod_top_half")
    for child in full.children:
        if not child.label.startswith(DROP):
            asm.add(child, child.label, color=getattr(child, "color", None))
    # short laser beam and USB stub, so the view stays compact
    z0 = A.HEAD_TOP_Z
    laser_y = A.PT.FRONT_Y + A.LASER_FRONT_OUT
    laser_z = z0 + PB.TOP_SEAT_Z + A.PT.LASER_Z
    asm.add(A.rod((0, laser_y, laser_z), (0, laser_y + 150, laser_z), 2.0), "laser_beam_indicator",
            color=A.C_BEAM)
    usb0 = (0, PB.ENC_L / 2 + 8, z0 + PB.FLOOR_T + PB.ENC_H / 2)
    asm.add(A.tube_along([usb0, (0, usb0[1] + 25, usb0[2] - 25), (0, usb0[1] + 35, usb0[2] - USB_STUB_DROP)],
                         A.USB_D), "usb3_cable_stub", color=A.C_USB)
    return asm.build()


def gen_step():
    return build()
