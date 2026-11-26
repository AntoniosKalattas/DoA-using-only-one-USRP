#!/usr/bin/env python3
"""
ble_decode.py - decode BLE advertising packets from raw IQ (no BLE chip needed).

Used by live_radar_capture.py so every detected burst can be tied to a real
device: advertiser address (AdvA) + manufacturer (Apple, Microsoft, ...) +
Apple Continuity message type. That turns anonymous "BLE 7" blips into
"Apple iPhone/Mac (Nearby) 5C:1A:..", which is how you find *your* phone.

Pipeline for one burst (complex baseband, centred on the advertising channel):
  FM discriminator -> remove CFO (median) -> for each of the `sps` sample phases:
  slice bits, search the advertising access address 0x8E89BED6, de-whiten with
  the channel index, read header/length, check the 24-bit CRC (init 0x555555).
Only CRC-valid packets are returned, so labels are never guessed.
"""

from concurrent.futures import ThreadPoolExecutor

import numpy as np

_POOL = ThreadPoolExecutor(max_workers=4)     # numpy releases the GIL in convolve/correlate

ADV_AA = 0x8E89BED6
AA_BITS = np.array([(ADV_AA >> i) & 1 for i in range(32)], dtype=np.int8)   # LSB first on air

PDU_TYPES = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_NONCONN_IND", 3: "SCAN_REQ",
             4: "SCAN_RSP", 5: "CONNECT_IND", 6: "ADV_SCAN_IND", 7: "ADV_EXT_IND"}

COMPANIES = {0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google",
             0x0087: "Garmin", 0x0157: "Huami", 0x038F: "Xiaomi", 0x02E5: "Espressif",
             0x0059: "Nordic", 0x000F: "Broadcom", 0x0171: "Amazon", 0x0310: "SGL Italia",
             0x027D: "Huawei", 0x0499: "Ruuvi", 0x0822: "Tile"}

APPLE_TYPES = {0x01: "iBeacon-old", 0x02: "iBeacon", 0x03: "AirPrint", 0x05: "AirDrop",
               0x06: "HomeKit", 0x07: "AirPods", 0x08: "Siri", 0x09: "AirPlay",
               0x0A: "AirPlay-src", 0x0B: "Watch", 0x0C: "Handoff", 0x0D: "Hotspot",
               0x0E: "Hotspot", 0x0F: "NearbyAction", 0x10: "Nearby", 0x12: "FindMy",
               0x16: "Nearby"}


def channel_for_freq(freq_hz):
    """BLE channel index for a centre frequency (advertising: 37/38/39)."""
    f = round(freq_hz / 1e6)
    if f == 2402:
        return 37
    if f == 2426:
        return 38
    if f == 2480:
        return 39
    rf = (f - 2402) // 2               # RF channel 0..39
    return rf - 1 if rf <= 11 else rf - 2   # data channel index


def _whitening(chan, n):
    """Whitening sequence (x^7 + x^4 + 1), register seeded with 1 | chan bits."""
    reg = [1] + [(chan >> (5 - i)) & 1 for i in range(6)]   # pos0..pos6
    out = np.empty(n, dtype=np.int8)
    for k in range(n):
        w = reg[6]
        out[k] = w
        reg = [w, reg[0], reg[1], reg[2], reg[3] ^ w, reg[4], reg[5]]
    return out


_WHITE = {}


def whitening(chan, n):
    seq = _WHITE.get(chan)
    if seq is None or len(seq) < n:
        seq = _WHITE[chan] = _whitening(chan, max(n, 8 * (2 + 255 + 3)))
    return seq[:n]


def bits_to_bytes(bits):
    n = len(bits) // 8
    b = bits[:n * 8].reshape(n, 8).astype(np.uint8)
    return (b * (1 << np.arange(8, dtype=np.uint8))).sum(1).astype(np.uint8)


def _crc_table():
    tab = []
    for byte in range(256):
        state = 0
        for i in range(8):
            fb = ((state >> 23) & 1) ^ ((byte >> i) & 1)
            state = (state << 1) & 0xFFFFFF
            if fb:
                state ^= 0x00065B      # x^10+x^9+x^6+x^4+x^3+x+1
        tab.append(state)
    return tab


def _rev8(v):
    return int(f"{v:08b}"[::-1], 2)


_REV8 = [_rev8(v) for v in range(256)]
# The LFSR takes bits LSB-first; feeding bit-reversed bytes makes it a plain MSB-first
# table CRC:  state' = (state << 8) ^ T[(state >> 16) ^ rev(byte)]
_CRC_T = [0] * 256
for _b, _v in enumerate(_crc_table()):
    _CRC_T[_REV8[_b]] = _v


def crc24(data, init=0x555555):
    """BLE CRC over PDU bytes. Returns the 3 CRC bytes in on-air order."""
    state = init
    for byte in data:
        state = ((state << 8) & 0xFFFFFF) ^ _CRC_T[((state >> 16) ^ _REV8[byte]) & 0xFF]
    # Transmitted MSB of state first; each on-air byte is LSB-first
    return bytes((_REV8[(state >> 16) & 0xFF], _REV8[(state >> 8) & 0xFF], _REV8[state & 0xFF]))


def fm_demod(x):
    d = np.angle(x[1:] * np.conj(x[:-1]))
    return d - np.median(d)            # remove carrier frequency offset


def find_packets(x, sps, chan, max_pkts=4):
    """Decode CRC-valid advertising packets in complex burst x. Returns list of dicts."""
    if len(x) < sps * 80:
        return []
    d = fm_demod(x)
    white = whitening(chan, 8 * (2 + 255 + 3))
    found, seen_starts = [], set()
    for off in range(sps):
        # integrate each symbol over its sps samples for SNR
        n_sym = (len(d) - off) // sps
        if n_sym < 80:
            continue
        sym = d[off:off + n_sym * sps].reshape(n_sym, sps).sum(1)
        bits = (sym > 0).astype(np.int8)
        # access-address search, allow 1 bit error
        win = np.lib.stride_tricks.sliding_window_view(bits, 32)
        errs = (win != AA_BITS).sum(1)
        for s in np.flatnonzero(errs <= 1):
            p0 = s + 32                                    # PDU starts after AA
            if p0 + 16 > len(bits):
                continue
            hdr = bits_to_bytes(bits[p0:p0 + 16] ^ white[:16])
            plen = int(hdr[1]) & 0xFF
            if plen < 6 or plen > 37 + 218:
                continue
            nbits = (2 + plen + 3) * 8
            if p0 + nbits > len(bits):
                continue
            raw = bits_to_bytes(bits[p0:p0 + nbits] ^ white[:nbits])
            pdu, crc = bytes(raw[:2 + plen]), bytes(raw[2 + plen:])
            if crc24(pdu) != crc:
                continue
            key = (s * sps + off) // (sps * 4)
            if key in seen_starts:
                continue
            seen_starts.add(key)
            pkt = parse_pdu(pdu)
            pkt["sample"] = int(s * sps + off)
            found.append(pkt)
            if len(found) >= max_pkts:
                return found
        if found:
            return found
    return found


# --------------------------------------------------------------------------- sensitive scanner
# find_packets() above needs an energy detector to hand it a clean burst first, and it
# demodulates the full 4 MHz of noise from one antenna. BleScanner instead:
#   1. band-limits both antennas to the 1 MHz BLE channel (~5 dB less noise into the FM detector),
#   2. combines the antennas inside the discriminator (sum of x[n]*conj(x[n-1]) over antennas:
#      power-weighted, needs no phase calibration, survives one antenna sitting in a null),
#   3. finds packets by correlating with the 40 known bits (preamble + access address)
#      anywhere in the stream - no power threshold, so weak packets, packets glued to a
#      Wi-Fi burst and packets crossing a block boundary are all still found,
#   4. accepts CRC-valid packets, plus packets that fail CRC by a few bits but match a
#      packet this same device sent (CRC-valid) moments ago. Beacons repeat the identical
#      packet, so a far-away phone keeps being recognised below the CRC limit.
PRE_AA = np.concatenate((np.array([0, 1, 0, 1, 0, 1, 0, 1], dtype=np.int8), AA_BITS))   # on-air order
_T = 2.0 * PRE_AA - 1.0
_TZ = _T - _T.mean()                 # zero-mean template -> correlation ignores carrier offset
_TZ_E = float(np.sum(_TZ ** 2))
N_SYNC = len(PRE_AA)
MAX_PDU_BITS = 8 * (2 + 255 + 3)


def channel_taps(rate, cutoff_hz=600e3, ntaps=None):
    """Windowed-sinc low-pass for the 1 MHz BLE channel (+ room for carrier offset)."""
    if ntaps is None:
        ntaps = int(round(rate / 1e6)) * 6 + 1
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = np.sinc(2 * cutoff_hz / rate * n) * np.hamming(ntaps)
    return (h / h.sum()).astype(np.float32)


def channel_filter(X, taps):
    rows = list(_POOL.map(lambda x: np.convolve(x, taps, mode="same"), X)) if len(X) > 1 else \
        [np.convolve(X[0], taps, mode="same")]
    return np.stack(rows).astype(np.complex64, copy=False)


def soft_symbols(Xf, sps):
    """s[n] = FM-discriminator output integrated over one symbol starting at sample n,
    antennas combined before the angle (so the stronger antenna dominates)."""
    z = (Xf[:, 1:] * np.conj(Xf[:, :-1])).sum(axis=0)
    cs = np.concatenate(([0.0], np.cumsum(np.angle(z), dtype=np.float64)))
    return cs[sps:] - cs[:-sps]


def sync_search(s, sps, thr):
    """Normalised correlation of s with preamble+AA at every sample offset.
    Returns [(sample_index, rho, amplitude)] of local maxima above thr."""
    n_out = len(s) - (N_SYNC - 1) * sps
    if n_out <= 0:
        return []
    rho = np.zeros(n_out)
    amp = np.zeros(n_out)

    def one_phase(p):
        sp = s[p::sps]
        if len(sp) < N_SYNC:
            return
        c = np.correlate(sp, _TZ, mode="valid")
        c1 = np.concatenate(([0.0], np.cumsum(sp)))
        c2 = np.concatenate(([0.0], np.cumsum(sp * sp)))
        s1 = c1[N_SYNC:] - c1[:-N_SYNC]
        var = (c2[N_SYNC:] - c2[:-N_SYNC]) - s1 * s1 / N_SYNC
        r = c / np.sqrt(_TZ_E * np.maximum(var, 1e-12))
        m = len(rho[p::sps])
        rho[p::sps] = r[:m]
        amp[p::sps] = c[:m] / _TZ_E

    list(_POOL.map(one_phase, range(sps)))
    idx = np.flatnonzero(rho > thr)
    if len(idx) == 0:
        return []
    out = []
    for grp in np.split(idx, np.flatnonzero(np.diff(idx) > sps) + 1):   # one peak per cluster
        n0 = int(grp[np.argmax(rho[grp])])
        out.append((n0, float(rho[n0]), float(amp[n0])))
    return out


class BleScanner:
    """Streaming BLE advertising-packet detector for 1..M coherent antennas.

    feed(X) takes the next raw block (n_ch, N) and returns (Xf, L, packets):
      Xf  = channel-filtered buffer = last `overlap` samples of the previous block + this block
      L   = number of carried-over samples at the start of Xf
      packets = parse_pdu() dicts + start/stop (sample indices into Xf), rho (sync quality),
                crc_ok, fixed (bit errors repaired by matching a known packet; 0 if CRC ok)."""

    def __init__(self, rate, chan, thr=0.62, overlap_us=450.0, template_ttl=120.0,
                 max_templates=48, max_fix=0.06, max_cand=60):
        self.sps = int(round(rate / 1e6))
        self.rate, self.chan, self.thr = rate, chan, thr
        self.taps = channel_taps(rate)
        self.white = whitening(chan, MAX_PDU_BITS)
        self.overlap = int(rate * overlap_us * 1e-6)
        self.tail = None
        self.ttl, self.max_templates, self.max_fix, self.max_cand = template_ttl, max_templates, max_fix, max_cand
        self.templates = {}          # raw pdu+crc bytes -> {"bits", "pkt", "t"}
        self.n_cand = self.n_crc = self.n_fixed = 0

    # -- one candidate ---------------------------------------------------------------
    def _slice(self, s, n0, a):
        k = min(N_SYNC + MAX_PDU_BITS, (len(s) - n0 - 1) // self.sps + 1)
        if k < N_SYNC + 16 + 24:
            return None
        sym = s[n0:n0 + k * self.sps:self.sps]
        dc = sym[:N_SYNC].mean() - a * _T.mean()            # carrier offset seen through the known bits
        return ((sym[N_SYNC:] - dc) > 0).astype(np.int8) ^ self.white[:k - N_SYNC]

    def _crc_decode(self, bits):
        """bits: de-whitened PDU bits. Returns (pdu, nbits) if header is sane and CRC matches."""
        hdr = bits_to_bytes(bits[:16])
        plen, ptype = int(hdr[1]), int(hdr[0]) & 0x0F
        if plen < 6 or (ptype != 7 and plen > 37):
            return None
        nbits = (2 + plen + 3) * 8
        if nbits > len(bits):
            return None
        raw = bytes(bits_to_bytes(bits[:nbits]))
        if crc24(raw[:-3]) != raw[-3:]:
            return None
        return raw, nbits

    def _template_match(self, bits):
        best = None
        for raw, tp in self.templates.items():
            tb = tp["bits"]
            if len(tb) > len(bits):
                continue
            nerr = int(np.count_nonzero(bits[:len(tb)] != tb))
            if nerr <= self.max_fix * len(tb) and (best is None or nerr < best[1]):
                best = (raw, nerr)
        return best

    # -- stream ----------------------------------------------------------------------
    def feed(self, X, now=0.0):
        X = np.atleast_2d(X)
        if self.tail is None:
            self.tail = np.zeros((X.shape[0], 0), dtype=X.dtype)
        buf = np.concatenate((self.tail, X), axis=1)
        L = self.tail.shape[1]
        self.tail = buf[:, -self.overlap:] if self.overlap else buf[:, :0]
        Xf = channel_filter(buf, self.taps)
        s = soft_symbols(Xf, self.sps)
        cands = sync_search(s, self.sps, self.thr)
        if len(cands) > self.max_cand:                       # bound CPU in dense interference
            cands = sorted(sorted(cands, key=lambda c: -c[1])[:self.max_cand])
        pkts, busy_until = [], -1
        for n0, rho, a in cands:
            if n0 < busy_until:
                continue                                     # inside a packet we already decoded
            self.n_cand += 1
            got = bits0 = None
            for dn in (0, 1, -1):                            # timing neighbours if CRC fails
                if n0 + dn < 0:
                    continue
                bits = self._slice(s, n0 + dn, a)
                if bits is None:
                    break
                res = self._crc_decode(bits)
                if res:
                    got = (res[0], res[1], 0, n0 + dn)
                    break
                if dn == 0:
                    bits0 = bits
            if got is None and bits0 is not None and self.templates:
                m = self._template_match(bits0)
                if m:
                    got = (m[0], len(m[0]) * 8, max(1, m[1]), n0)
            if got is None:
                continue
            raw, nbits, nerr, start = got
            stop = start + (N_SYNC + nbits) * self.sps
            busy_until = stop
            if stop <= L:
                continue                                     # fully inside the overlap: reported last block
            if nerr == 0:
                self.n_crc += 1
                tp = self.templates.pop(raw, None) or {"bits": np.unpackbits(
                    np.frombuffer(raw, dtype=np.uint8), bitorder="little").astype(np.int8),
                    "pkt": parse_pdu(raw[:-3])}
                tp["t"] = now
                self.templates[raw] = tp                     # re-insert = most recent last
                if len(self.templates) > self.max_templates:
                    self.templates.pop(next(iter(self.templates)))
            else:
                self.n_fixed += 1
                tp = self.templates[raw]
            pkt = dict(tp["pkt"], start=int(start), stop=int(min(stop, Xf.shape[1])), sample=int(start - L),
                       rho=round(rho, 3), crc_ok=nerr == 0, fixed=int(nerr))
            pkts.append(pkt)
        if self.ttl and self.templates:
            for raw in [r for r, tp in self.templates.items() if now - tp["t"] > self.ttl]:
                del self.templates[raw]
        return Xf, L, pkts


def parse_pdu(pdu):
    h0, plen = pdu[0], pdu[1]
    ptype = h0 & 0x0F
    tx_random = bool(h0 & 0x40)
    payload = pdu[2:2 + plen]
    out = {"pdu": PDU_TYPES.get(ptype, f"type{ptype}"), "random": tx_random,
           "addr": ":".join(f"{b:02X}" for b in payload[5::-1]) if len(payload) >= 6 else "?",
           "company": None, "vendor": None, "apple": None, "name": None, "len": plen}
    if ptype == 7 and len(payload) >= 2:
        # ADV_EXT_IND: common extended payload. AdvA only if flag bit0 is set;
        # on the primary channel it is usually absent (ADI + AuxPtr only).
        hlen = payload[0] & 0x3F
        flags = payload[1] if hlen >= 1 else 0
        k = 2
        out["addr"] = None
        if flags & 0x01 and len(payload) >= k + 6:
            out["addr"] = ":".join(f"{b:02X}" for b in payload[k:k + 6][::-1])
            k += 6
        if flags & 0x02:
            k += 6
        if flags & 0x04:
            k += 1
        if flags & 0x08 and len(payload) >= k + 2:
            sid = (payload[k + 1] >> 4) & 0x0F
            if out["addr"] is None:
                out["addr"] = f"EXT-SID{sid}"
        if out["addr"] is None:
            out["addr"] = "EXT-?"
        out["vendor"] = "BLE5-ext"
    if ptype in (0, 2, 4, 6) and len(payload) > 6:
        ad = payload[6:]
        i = 0
        while i + 1 < len(ad):
            L = ad[i]
            if L == 0 or i + 1 + L > len(ad):
                break
            t, v = ad[i + 1], ad[i + 2:i + 1 + L]
            if t == 0xFF and len(v) >= 2:
                cid = v[0] | (v[1] << 8)
                out["company"] = cid
                out["vendor"] = COMPANIES.get(cid, f"0x{cid:04X}")
                if cid == 0x004C and len(v) >= 3:
                    out["apple"] = APPLE_TYPES.get(v[2], f"0x{v[2]:02X}")
                if cid == 0xFFFF and len(v) >= 7 and bytes(v[2:6]) == b"USRP":
                    out["usrp_tag"] = int(v[6])          # our own Android test beacon
                    out["vendor"] = "USRP Beacon"
            elif t in (0x08, 0x09):
                try:
                    out["name"] = bytes(v).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    pass
            elif t in (0x03, 0x16) and len(v) >= 2 and out["vendor"] is None:
                uuid = v[0] | (v[1] << 8)
                if uuid == 0xFE9F:
                    out["vendor"] = "Google"
                elif uuid == 0xFD6F:
                    out["vendor"] = "ExposureNotif"
                elif uuid == 0xFEED or uuid == 0xFEEC:
                    out["vendor"] = "Tile"
            i += 1 + L
        # iPhone version of the USRP Beacon app: iOS can't send manufacturer data,
        # so it advertises the local name "USRP-<tag>" instead.
        nm = out["name"]
        if out.get("usrp_tag") is None and nm and nm.startswith("USRP-") and nm[5:].isdigit():
            tag = int(nm[5:])
            if tag < 256:
                out["usrp_tag"] = tag
                out["vendor"] = "USRP Beacon (iOS)"
    return out


def describe(pkt):
    """Short human label, e.g. 'Apple Nearby' / 'Microsoft' / 'Galaxy Buds'."""
    if pkt.get("usrp_tag") is not None:
        return f"USRP Beacon #{pkt['usrp_tag']}"
    if pkt.get("name"):
        return pkt["name"]
    if pkt.get("apple"):
        return "Apple " + pkt["apple"]
    if pkt.get("vendor"):
        return pkt["vendor"]
    return "BLE " + pkt["pdu"]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Decode BLE adv packets from a b210_stream raw dump")
    ap.add_argument("file")
    ap.add_argument("--rate", type=float, default=4e6)
    ap.add_argument("--freq", type=float, default=2.426e9)
    ap.add_argument("--block", type=int, default=80000)
    ap.add_argument("--channels", type=int, default=2)
    args = ap.parse_args()
    sps = int(round(args.rate / 1e6))
    chan = channel_for_freq(args.freq)
    raw = np.fromfile(args.file, dtype=np.complex64)
    nb = len(raw) // (args.block * args.channels)
    X = raw[:nb * args.block * args.channels].reshape(nb, args.channels, args.block)
    X = X.transpose(1, 0, 2).reshape(args.channels, -1)
    env = np.convolve(np.abs(X[0]) ** 2, np.ones(40) / 40, "same")
    on = env > np.median(env) * 10 ** 1.2
    dd = np.diff(np.concatenate(([0], on.astype(np.int8), [0])))
    st, en = np.flatnonzero(dd == 1), np.flatnonzero(dd == -1)
    from collections import Counter
    c, tot = Counter(), 0
    for s, e in zip(st, en):
        if e - s < 200:
            continue
        tot += 1
        seg = X[:, max(0, s - 40):e + 40]
        pk = find_packets(seg[0], sps, chan) or find_packets(seg[1], sps, chan)
        for p in pk:
            c[(p["addr"], describe(p))] += 1
    print(f"bursts {tot}  decoded {sum(c.values())}")
    for (a, dsc), n in c.most_common(30):
        print(f"  {n:4d}  {a}  {dsc}")
