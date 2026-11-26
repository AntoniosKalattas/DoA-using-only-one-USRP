#!/usr/bin/env python3
"""
ble_bench.py - measure how many BLE advertising packets the receiver chain catches.

  synth : simulated phone beacon (GFSK, 2 antennas, noise, carrier offset) at a sweep of
          signal levels -> detection rate of the old chain (energy burst -> decode one
          antenna) vs the BleScanner chain used by live_radar_capture.py now.
  file  : the same comparison on a raw 2-channel dump from native/b210_stream
          (b210_stream --block 80000 ... > dump.fc32).

No hardware needed for synth:   python3 ble_bench.py synth
"""
import argparse
import time
from collections import Counter

import numpy as np

from ble_decode import (BleScanner, find_packets, describe, channel_for_freq, whitening, crc24, PRE_AA)


def old_find_bursts(X, rate, snr_db=12.0, min_us=60.0, max_us=600.0, smooth_us=10.0):
    """The previous energy detector (kept here only as the baseline)."""
    env = (np.abs(X) ** 2).sum(axis=0)
    k = max(1, int(rate * smooth_us * 1e-6))
    sm = np.convolve(env, np.ones(k) / k, mode="same")
    on = sm > np.median(sm) * 10 ** (snr_db / 10.0)
    d = np.diff(np.concatenate(([0], on.astype(np.int8), [0])))
    starts, stops = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    lo, hi = rate * min_us * 1e-6, rate * max_us * 1e-6
    return [(s + k, e - k) for s, e in zip(starts, stops)
            if s != 0 and e < X.shape[1] and lo <= (e - s) <= hi and (e - s) > 3 * k]


def old_chain(X, rate, sps, chan):
    out = []
    for s, e in old_find_bursts(X, rate):
        wide = X[:, max(0, s - 40 * sps):min(X.shape[1], e + 40 * sps)]
        pk = find_packets(wide[0], sps, chan, max_pkts=1)
        if not pk and wide.shape[0] > 1:
            pk = find_packets(wide[1], sps, chan, max_pkts=1)
        if pk:
            out.append(pk[0])
    return out


# --------------------------------------------------------------------------- synthetic beacon
def beacon_bits(chan, tag=1, addr=bytes.fromhex("c0ffee123456")):
    """On-air bits of the USRP Beacon app's ADV_NONCONN_IND (mfr data 0xFFFF 'USRP'+tag)."""
    ad = bytes([8, 0xFF, 0xFF, 0xFF]) + b"USRP" + bytes([tag])
    payload = addr[::-1] + ad
    pdu = bytes([0x42, len(payload)]) + payload
    raw = pdu + crc24(pdu)
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little").astype(np.int8)
    return np.concatenate((PRE_AA, bits ^ whitening(chan, len(bits))))


def gfsk(bits, sps, bt=0.5, h=0.5):
    t = np.arange(-2 * sps, 2 * sps + 1) / sps
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    g = np.exp(-t ** 2 / (2 * sigma ** 2))
    g /= g.sum()
    f = np.convolve(np.repeat(2.0 * bits - 1.0, sps), g, mode="same")
    return np.exp(1j * np.cumsum(np.pi * h / sps * f)).astype(np.complex64)


def synth_block(rng, wave, n, starts, snr_db, rate, cfo_hz, ant_phase_deg, ant1_db):
    """Noise power = 1 over the full sample rate; snr_db is signal/noise in the 1 MHz channel."""
    sps = rate / 1e6
    X = (rng.standard_normal((2, n)) + 1j * rng.standard_normal((2, n))).astype(np.complex64) / np.sqrt(2)
    amp = np.sqrt(10 ** (snr_db / 10.0) / sps)
    for s in starts:
        k = np.arange(len(wave))
        w = wave * np.exp(1j * (2 * np.pi * cfo_hz / rate * k + rng.uniform(0, 2 * np.pi)))
        X[0, s:s + len(wave)] += amp * w
        X[1, s:s + len(wave)] += amp * 10 ** (ant1_db / 20.0) * w * np.exp(-1j * np.radians(ant_phase_deg))
    return X


def run_synth(args):
    rate, sps, chan = args.rate, int(round(args.rate / 1e6)), channel_for_freq(args.freq)
    wave = gfsk(beacon_bits(chan), sps)
    n = int(rate * 0.02)
    rng = np.random.default_rng(args.seed)
    print(f"Simulated USRP Beacon packet ({len(wave) / rate * 1e6:.0f} us), 2 antennas, {args.packets} packets per level.")
    print("SNR = signal / noise inside the 1 MHz BLE channel. 'known' = recognised by matching the")
    print("packet the same device sent earlier (CRC failed by a few bits).\n")
    print(f"  {'SNR dB':>6} | {'old chain':>9} | {'new CRC-valid':>13} | {'new +known':>10} | false")
    for snr in args.snr:
        sc = BleScanner(rate, chan)
        # the beacon has been heard before at close range (that is how you calibrate)
        sc.feed(synth_block(rng, wave, n, [2000], 30.0, rate, 0.0, 0.0, 0.0), 0.0)
        sc.n_crc = sc.n_fixed = 0
        sent = old = new_crc = new_all = false = 0
        blk = 0
        while sent < args.packets:
            starts = [int(rng.integers(500, n - len(wave) - 500))]
            cfo = rng.uniform(-args.cfo, args.cfo)
            X = synth_block(rng, wave, n, starts, snr, rate, cfo, rng.uniform(-180, 180), args.ant1_db)
            sent += len(starts)
            old += sum(1 for p in old_chain(X, rate, sps, chan) if p.get("usrp_tag") == 1)
            _, L, pk = sc.feed(X, blk * 0.02)
            for p in pk:
                ok = p.get("usrp_tag") == 1 and abs(p["sample"] - starts[0]) <= 2 * sps
                new_all += ok
                new_crc += ok and p["crc_ok"]
                false += not ok
            blk += 1
        print(f"  {snr:6.1f} | {100 * old / sent:8.1f}% | {100 * new_crc / sent:12.1f}% | {100 * new_all / sent:9.1f}% | {false}")
    # false alarms on pure noise, and CPU
    sc = BleScanner(rate, chan)
    sc.feed(synth_block(rng, wave, n, [2000], 30.0, rate, 0.0, 0.0, 0.0), 0.0)
    t0, fa = time.perf_counter(), 0
    for i in range(args.noise_blocks):
        fa += len(sc.feed(synth_block(rng, wave, n, [], 0, rate, 0, 0, 0), 1 + i * 0.02)[2])
    dt = (time.perf_counter() - t0) / args.noise_blocks
    print(f"\nNoise only: {fa} false packets in {args.noise_blocks * 0.02:.0f} s of noise "
          f"({sc.n_cand} sync candidates rejected).")


def run_file(args):
    rate, sps, chan = args.rate, int(round(args.rate / 1e6)), channel_for_freq(args.freq)
    raw = np.memmap(args.file, dtype=np.complex64, mode="r")
    nb = len(raw) // (args.block * 2)
    if args.seconds:
        nb = min(nb, int(args.seconds * rate / args.block))
    sc = BleScanner(rate, chan)
    c_old, c_new, c_fix = Counter(), Counter(), Counter()
    t_old = t_new = 0.0
    for i in range(nb):
        X = np.array(raw[i * 2 * args.block:(i + 1) * 2 * args.block]).reshape(2, args.block)
        t0 = time.perf_counter()
        for p in old_chain(X, rate, sps, chan):
            c_old[(p["addr"], describe(p))] += 1
        t1 = time.perf_counter()
        for p in sc.feed(X, i * args.block / rate)[2]:
            (c_new if p["crc_ok"] else c_fix)[(p["addr"], describe(p))] += 1
        t_old, t_new = t_old + t1 - t0, t_new + time.perf_counter() - t1
    dur = nb * args.block / rate
    print(f"{dur:.1f} s of IQ, BLE ch {chan}.  CPU per second of signal: old {t_old / dur:.2f} s, new {t_new / dur:.2f} s")
    print(f"packets: old {sum(c_old.values())}   new {sum(c_new.values())} CRC-valid + {sum(c_fix.values())} known-packet matches\n")
    print(f"  {'old':>5} {'new':>5} {'+known':>6}  device")
    for k in sorted(set(c_old) | set(c_new) | set(c_fix), key=lambda k: -(c_new[k] + c_fix[k])):
        print(f"  {c_old[k]:5d} {c_new[k]:5d} {c_fix[k]:6d}  {k[0]}  {k[1]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("synth")
    a.add_argument("--snr", type=float, nargs="+", default=[20, 16, 14, 12, 10, 8, 6, 4])
    a.add_argument("--packets", type=int, default=300)
    a.add_argument("--cfo", type=float, default=60e3, help="max carrier offset Hz (+-)")
    a.add_argument("--ant1-db", type=float, default=0.0, help="level of antenna 2 relative to antenna 1")
    a.add_argument("--noise-blocks", type=int, default=500)
    a.add_argument("--seed", type=int, default=1)
    b = sub.add_parser("file")
    b.add_argument("file")
    b.add_argument("--block", type=int, default=80000)
    b.add_argument("--seconds", type=float, default=0.0)
    for q in (a, b):
        q.add_argument("--rate", type=float, default=4e6)
        q.add_argument("--freq", type=float, default=2.480e9)
    args = ap.parse_args()
    run_synth(args) if args.mode == "synth" else run_file(args)
