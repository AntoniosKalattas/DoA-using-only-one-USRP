#!/usr/bin/env python3
"""
Active energy vs. drone proximity — side-by-side comparison with effective_size.

active_energy = sum(|s_n|^2) for all above-threshold samples in a 10 ms window.
It combines BOTH power AND duration, making it more robust than either alone
when OcuSync hops partially overlap the capture window.

Layout per flight:
  Top panel    — effective_size_bytes (kB)   [same as packet_size_distance_proxy.py]
  Bottom panel — active_energy (linear)
Both share the same time axis so agreement / conflict is immediately visible.
"""

import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from pathlib import Path

BASE        = Path("/root/projects/ReplayAttack/iq_datasets/Real Flight")
OUT         = BASE / "april16_fingerprint_analysis"
OUT.mkdir(exist_ok=True)

NOISE_FLOOR = -51.9
SMOOTH_WIN  = 150     # ~1.5 s

FLIGHTS = {
    "Apr 16 — Flight 1": BASE / "real_flight_10ms_20260416_110239.csv",
    "Apr 16 — Flight 2": BASE / "real_flight_10ms_20260416_111305.csv",
}

# ── helper ────────────────────────────────────────────────────────────────────
def nanroll_mean(arr, w):
    out = np.full_like(arr, np.nan)
    half = w // 2
    for i in range(len(arr)):
        seg  = arr[max(0, i - half): i + half + 1]
        good = seg[~np.isnan(seg)]
        if len(good):
            out[i] = good.mean()
    return out

def zone_fill(ax, t, curve, lo, hi, p25, p75):
    """Shade under smoothed curve by close/mid/far zone."""
    ax.fill_between(t, lo, curve, where=curve >= p75,
                    color='#2ecc71', alpha=0.22, zorder=3, interpolate=True)
    ax.fill_between(t, lo, curve, where=curve <= p25,
                    color='#e74c3c', alpha=0.22, zorder=3, interpolate=True)
    ax.fill_between(t, lo, curve,
                    where=((curve > p25) & (curve < p75)),
                    color='#f39c12', alpha=0.10, zorder=3, interpolate=True)

def scatter_dots(ax, t, gated, p25, p75):
    for i in range(len(t)):
        if not np.isnan(gated[i]):
            c = ('#27ae60' if gated[i] > p75
                 else '#e74c3c' if gated[i] < p25
                 else '#95a5a6')
            ax.scatter(t[i], gated[i], color=c, s=9,
                       alpha=0.35, zorder=2, linewidths=0)

def threshold_lines(ax, p25, p75, p50, unit):
    ax.axhline(p75, color='#27ae60', lw=1.2, linestyle='--', alpha=0.7,
               label=f'75th pct ({p75:.2g} {unit}) — close')
    ax.axhline(p25, color='#e74c3c', lw=1.2, linestyle='--', alpha=0.7,
               label=f'25th pct ({p25:.2g} {unit}) — far')
    ax.axhline(p50, color='#7f8c8d', lw=1.0, linestyle=':', alpha=0.6,
               label=f'Median ({p50:.2g} {unit})')

def annotate_peak_trough(ax, t, smooth, unit=''):
    if np.all(np.isnan(smooth)):
        return
    pi = int(np.nanargmax(smooth))
    ti = int(np.nanargmin(smooth))
    pv, tv = smooth[pi], smooth[ti]
    spread = np.nanmax(smooth) - np.nanmin(smooth)
    ax.annotate(f'Closest\n({pv:.2g}{unit})',
                xy=(t[pi], pv), xytext=(t[pi], pv + spread * 0.15),
                ha='center', fontsize=8.5, color='#27ae60', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#27ae60', lw=1.4))
    ax.annotate(f'Furthest\n({tv:.2g}{unit})',
                xy=(t[ti], tv), xytext=(t[ti], tv - spread * 0.18),
                ha='center', fontsize=8.5, color='#e74c3c', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#e74c3c', lw=1.4))

# ── figure: 2 flights × 2 panels ─────────────────────────────────────────────
n = len(FLIGHTS)
fig = plt.figure(figsize=(14, 5.5 * n))
fig.suptitle(
    'Effective Packet Size vs. Active Energy as Drone Proximity Indicators\n'
    'OcuSync 2.0 — April 16, 2026  (gain=70 dB · 2 MHz BW · 5.845 GHz)',
    fontsize=13, fontweight='bold'
)

outer = gridspec.GridSpec(n, 1, figure=fig, hspace=0.45)

for flight_idx, (label, path) in enumerate(FLIGHTS.items()):
    rows     = list(csv.DictReader(open(path)))
    t        = np.arange(len(rows)) * 10 / 1000.0
    mxpw     = np.array([float(r['max_power_db'])          for r in rows])
    eff_sz   = np.array([float(r['effective_size_bytes'])   for r in rows]) / 1000.0   # kB
    act_en   = np.array([float(r['active_energy'])          for r in rows])
    detected = mxpw > NOISE_FLOOR

    eff_g  = np.where(detected, eff_sz,  np.nan)
    act_g  = np.where(detected, act_en,  np.nan)

    eff_sm = nanroll_mean(eff_g, SMOOTH_WIN)
    act_sm = nanroll_mean(act_g, SMOOTH_WIN)

    v_eff  = eff_g[~np.isnan(eff_g)];  v_act = act_g[~np.isnan(act_g)]
    e_p25, e_p50, e_p75 = np.percentile(v_eff, [25, 50, 75])
    a_p25, a_p50, a_p75 = np.percentile(v_act, [25, 50, 75])

    det_pct = 100 * detected.mean()

    inner = gridspec.GridSpecFromSubplotSpec(
        2, 1, subplot_spec=outer[flight_idx], hspace=0.35
    )
    ax_eff = fig.add_subplot(inner[0])
    ax_act = fig.add_subplot(inner[1], sharex=ax_eff)

    flight_title = label
    ax_eff.set_title(flight_title, fontsize=12, fontweight='bold', pad=8)

    # ── effective size panel ──────────────────────────────────────────────────
    ax_eff.set_facecolor('#f8f9fa')
    e_pad = (v_eff.max() - v_eff.min()) * 0.15
    ax_eff.set_ylim(v_eff.min() - e_pad, v_eff.max() + e_pad + 15)

    ax_eff.axhspan(e_p75, v_eff.max() + 20, color='#2ecc71', alpha=0.10, zorder=0)
    ax_eff.axhspan(0,     e_p25,             color='#e74c3c', alpha=0.10, zorder=0)
    threshold_lines(ax_eff, e_p25, e_p75, e_p50, 'kB')
    scatter_dots(ax_eff, t, eff_g, e_p25, e_p75)
    zone_fill(ax_eff, t, eff_sm, v_eff.min() - e_pad, v_eff.max(), e_p25, e_p75)
    ax_eff.plot(t, eff_sm, color='#2c3e50', lw=2.5, zorder=4,
                label='Rolling mean')
    annotate_peak_trough(ax_eff, t, eff_sm, ' kB')
    ax_eff.set_ylabel('Effective size (kB)', fontsize=10)
    ax_eff.legend(loc='upper right', fontsize=7.5, framealpha=0.9, ncol=2)
    ax_eff.grid(True, alpha=0.18)
    ax_eff.text(0.01, 0.97,
                f'Signal windows: {det_pct:.1f}%  ·  each dot = 1 × 10 ms window',
                transform=ax_eff.transAxes, fontsize=8, va='top',
                bbox=dict(boxstyle='round,pad=0.25', fc='white', ec='#ccc', alpha=0.85))

    # ── active energy panel ───────────────────────────────────────────────────
    # Clip y-axis to 98th percentile to suppress extreme outlier spikes
    a_cap  = np.percentile(v_act, 98)
    v_act_clipped = v_act[v_act <= a_cap]
    a_p25, a_p50, a_p75 = np.percentile(v_act_clipped, [25, 50, 75])
    act_sm_clipped = np.clip(act_sm, None, a_cap)

    ax_act.set_facecolor('#f8f9fa')
    a_pad = (a_cap - v_act.min()) * 0.12
    ax_act.set_ylim(max(0, v_act.min() - a_pad), a_cap + a_pad)

    ax_act.axhspan(a_p75, a_cap + a_pad, color='#2ecc71', alpha=0.10, zorder=0)
    ax_act.axhspan(0,     a_p25,         color='#e74c3c', alpha=0.10, zorder=0)
    threshold_lines(ax_act, a_p25, a_p75, a_p50, '')
    scatter_dots(ax_act, t, np.clip(act_g, None, a_cap), a_p25, a_p75)
    zone_fill(ax_act, t, act_sm_clipped, max(0, v_act.min() - a_pad), a_cap, a_p25, a_p75)
    ax_act.plot(t, act_sm_clipped, color='#2c3e50', lw=2.5, zorder=4,
                label='Rolling mean')
    annotate_peak_trough(ax_act, t, act_sm_clipped)
    ax_act.set_ylabel('Active energy (Σ|s|²)', fontsize=10)
    ax_act.set_xlabel('Time (s)', fontsize=10)
    ax_act.legend(loc='upper right', fontsize=7.5, framealpha=0.9, ncol=2)
    ax_act.grid(True, alpha=0.18)

    ax_eff.set_xlim(t[0], t[-1])

    # Proximity direction labels on y-axes
    for ax, ymax, ymin in [(ax_eff, v_eff.max() + e_pad + 12, v_eff.min() - e_pad),
                            (ax_act, a_cap + a_pad,            max(0, v_act.min() - a_pad))]:
        ax.text(-0.01, 0.99, '▲ closer', transform=ax.transAxes,
                fontsize=7.5, color='#27ae60', va='top', ha='right', fontweight='bold')
        ax.text(-0.01, 0.01, '▼ further', transform=ax.transAxes,
                fontsize=7.5, color='#e74c3c', va='bottom', ha='right', fontweight='bold')

plt.tight_layout(rect=[0, 0, 1, 0.96])
out_path = OUT / "active_energy_distance_proxy.png"
plt.savefig(out_path, dpi=150, bbox_inches='tight')
print(f"Saved: {out_path}")
