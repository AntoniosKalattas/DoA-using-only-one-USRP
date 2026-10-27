import numpy as np

# ----------------------------------------------------------------------------
# Array geometry defaults for the USRP B210 operating at the DJI 5.8 GHz band.
#   lambda = c / f = 3e8 / 5.8e9  = 0.0517 m   (wavelength at 5.8 GHz)
#   d      = lambda / 2           = 0.02585 m  (half-wavelength spacing, d/l=0.5)
# Note: a single B210 has only 2 coherent RX channels, so the native array is
# M = 2 elements (resolves one source). More elements require multiple
# phase-synchronised B210s (MIMO cable) or a dedicated array front end.
# ----------------------------------------------------------------------------
SPEED_OF_LIGHT = 299_792_458.0
B210_FREQ_HZ = 5.8e9
B210_WAVELENGTH_M = SPEED_OF_LIGHT / B210_FREQ_HZ      # 0.0517 m
B210_SPACING_M = B210_WAVELENGTH_M / 2.0               # 0.02585 m (lambda/2)
B210_RX_CHANNELS = 2


def find_angle(samples, times=None, wave_length=B210_WAVELENGTH_M,
               distance_d=B210_SPACING_M, number_of_drones=1):
    # wave_length / distance_d / number_of_drones are parameters (defaults set
    # for the USRP B210 at 5.8 GHz) so the estimator can be driven from the CLI.

    # Number of antennas/sensors M (assuming samples has shape (N_snapshots, M_antennas))
    num_elements = samples.shape[1]

    Rxx = calculate_spatial_covariance_matrix_rxx(samples)
    eigenvalues, eigenvectors = getEigenValues(Rxx)

    noice_subspace = eigenvectors[:, number_of_drones:]

    steeringVec = steeringVector(wave_length, distance_d, num_elements)
    angles_deg, p_music = evaluate_spectrum(steeringVec, noice_subspace)

    estimated_angle = get_peak_detection(angles_deg, p_music)
    print(estimated_angle)
    return estimated_angle


def calculate_spatial_covariance_matrix_rxx(samples):
    # Assuming samples is shape (N_snapshots, M_antennas)
    # Note: If your steering vector uses exp(-1j * ...), Rxx = (X^T @ X.conj()) / N or (X.conj().T @ X) / N
    # depending on sign convention of incoming phase. Standard column-snapshot x(n): x(n) @ x(n)^H -> samples.T @ samples.conj()
    return (samples.T @ samples.conj()) / len(samples)


def getEigenValues(Rxx):
    eigenvalues, eigenvectors = np.linalg.eigh(Rxx)
    eigenvalues = eigenvalues[::-1]
    eigenvectors = eigenvectors[:, ::-1]
    return eigenvalues, eigenvectors


def steeringVector(wave_length, distance_d, num_elements):
    angles_deg = np.arange(-90, 91)  # -90 to 90 inclusive
    angles_rad = np.deg2rad(angles_deg)
    m = np.arange(num_elements)[:, np.newaxis]  # Shape (M, 1)
    return np.exp(-1j * (2 * np.pi * distance_d / wave_length) * m * np.sin(angles_rad))


def evaluate_spectrum(steeringVector, noice_subspace):
    angles_deg = np.arange(-90, 91)
    p_music = []
    if steeringVector.shape[0] != len(angles_deg):
        steeringVector = steeringVector.T
    for a in steeringVector:
        a = a[:, np.newaxis]
        a_H = a.conj().T
        denom = a_H @ noice_subspace @ noice_subspace.conj().T @ a
        p_music.append(1.0 / np.abs(denom[0, 0]))
    p_music = np.array(p_music)
    return angles_deg, p_music


def get_peak_detection(angles_deg, spectrum):
    peak_idx = np.argmax(spectrum)
    return angles_deg[peak_idx]


# ----------------------------------------------------------------------------
# Synthetic array-signal generator (for validating the estimator)
# ----------------------------------------------------------------------------
def make_synthetic(num_elements, true_angle_deg, snapshots,
                   snr_db, wave_length, distance_d, seed=None):
    """Simulate a uniform linear array receiving ONE narrowband source from
    true_angle_deg, with complex white noise at the given SNR. Returns an array
    of shape (snapshots, num_elements) using the SAME phase convention as
    steeringVector(), so a correct estimator recovers the angle."""
    rng = np.random.default_rng(seed)
    m = np.arange(num_elements)
    theta = np.deg2rad(true_angle_deg)
    # steering vector for the true direction (matches steeringVector's sign)
    a = np.exp(-1j * (2 * np.pi * distance_d / wave_length) * m * np.sin(theta))

    # unit-power complex source signal, one value per snapshot
    s = (rng.standard_normal(snapshots) + 1j * rng.standard_normal(snapshots)) / np.sqrt(2)
    signal = np.outer(s, a)                       # (snapshots, num_elements)

    noise_var = 10 ** (-snr_db / 10.0)
    noise = np.sqrt(noise_var / 2.0) * (
        rng.standard_normal(signal.shape) + 1j * rng.standard_normal(signal.shape))
    return signal + noise


def main():
    import argparse

    p = argparse.ArgumentParser(
        description="MUSIC direction-of-arrival estimator (DJI RF thesis).")
    sub = p.add_subparsers(dest="mode", required=True)

    syn = sub.add_parser("synthetic",
                         help="Generate a synthetic array signal and estimate its angle.")
    syn.add_argument("--antennas", type=int, default=B210_RX_CHANNELS,
                     help="Array elements M (default 2 = B210 RX channels)")
    syn.add_argument("--angle", type=float, default=20.0, help="True source angle in deg (default 20)")
    syn.add_argument("--snapshots", type=int, default=1000, help="Number of snapshots N (default 1000)")
    syn.add_argument("--snr", type=float, default=10.0, help="SNR in dB (default 10)")
    syn.add_argument("--sources", type=int, default=1, help="Number of sources / drones (default 1)")
    syn.add_argument("--wavelength", type=float, default=B210_WAVELENGTH_M,
                     help="Wavelength in m (default 0.0517 = 5.8 GHz)")
    syn.add_argument("--spacing", type=float, default=B210_SPACING_M,
                     help="Element spacing d in m (default 0.02585 = lambda/2)")
    syn.add_argument("--seed", type=int, default=None, help="RNG seed for reproducibility")

    fil = sub.add_parser("file",
                         help="Load array IQ from a .npy of shape (N_snapshots, M_antennas).")
    fil.add_argument("--input", required=True, help="Path to complex .npy file")
    fil.add_argument("--sources", type=int, default=1, help="Number of sources / drones (default 1)")
    fil.add_argument("--wavelength", type=float, default=B210_WAVELENGTH_M,
                     help="Wavelength in m (default 0.0517 = 5.8 GHz)")
    fil.add_argument("--spacing", type=float, default=B210_SPACING_M,
                     help="Element spacing d in m (default 0.02585 = lambda/2)")

    swp = sub.add_parser("sweep",
                         help="Sweep a grid of true angles and report accuracy (validation table).")
    swp.add_argument("--antennas", type=int, default=B210_RX_CHANNELS,
                     help="Array elements M (default 2 = B210 RX channels)")
    swp.add_argument("--start", type=float, default=-60.0, help="First true angle deg (default -60)")
    swp.add_argument("--stop", type=float, default=60.0, help="Last true angle deg (default 60)")
    swp.add_argument("--step", type=float, default=10.0, help="Angle step deg (default 10)")
    swp.add_argument("--trials", type=int, default=20, help="Noise realisations per angle (default 20)")
    swp.add_argument("--snapshots", type=int, default=1000, help="Number of snapshots N (default 1000)")
    swp.add_argument("--snr", type=float, default=10.0, help="SNR in dB (default 10)")
    swp.add_argument("--sources", type=int, default=1, help="Number of sources / drones (default 1)")
    swp.add_argument("--wavelength", type=float, default=B210_WAVELENGTH_M,
                     help="Wavelength in m (default 0.0517 = 5.8 GHz)")
    swp.add_argument("--spacing", type=float, default=B210_SPACING_M,
                     help="Element spacing d in m (default 0.02585 = lambda/2)")
    swp.add_argument("--seed", type=int, default=None, help="Base RNG seed for reproducibility")

    args = p.parse_args()

    if args.mode == "synthetic":
        print(f"[MUSIC] synthetic ULA: M={args.antennas}, N={args.snapshots}, "
              f"SNR={args.snr} dB, true angle={args.angle} deg")
        print(f"[MUSIC] geometry: d/lambda = {args.spacing/args.wavelength:.3f}")
        samples = make_synthetic(args.antennas, args.angle, args.snapshots,
                                 args.snr, args.wavelength, args.spacing, args.seed)
        est = find_angle(samples, wave_length=args.wavelength,
                         distance_d=args.spacing, number_of_drones=args.sources)
        print(f"[MUSIC] estimated angle = {est} deg")
        print(f"[MUSIC] error           = {abs(est - args.angle):.2f} deg")

    elif args.mode == "file":
        samples = np.load(args.input)
        print(f"[MUSIC] loaded {args.input}, shape={samples.shape}, dtype={samples.dtype}")
        if samples.ndim != 2 or samples.shape[1] < 2:
            print("[MUSIC] ERROR: need a 2-D complex array of shape "
                  "(N_snapshots, M_antennas) with M >= 2. "
                  "Single-channel/feature data cannot be used for DoA.")
            return
        est = find_angle(samples, wave_length=args.wavelength,
                         distance_d=args.spacing, number_of_drones=args.sources)
        print(f"[MUSIC] estimated angle = {est} deg")

    elif args.mode == "sweep":
        n = int(round((args.stop - args.start) / args.step)) + 1
        true_angles = [args.start + i * args.step for i in range(n)]
        print(f"[MUSIC] sweep: M={args.antennas}, N={args.snapshots}, SNR={args.snr} dB, "
              f"{args.trials} trial(s)/angle")
        print(f"[MUSIC] geometry: d/lambda = {args.spacing/args.wavelength:.3f}")
        print()
        print(f"  {'true(deg)':>10} {'est(deg)':>9} {'mean_err':>9} {'max_err':>8}")
        print(f"  {'-'*10} {'-'*9} {'-'*9} {'-'*8}")

        import io
        import contextlib
        all_errs = []
        for ai, true_deg in enumerate(true_angles):
            errs, last_est = [], None
            for t in range(args.trials):
                seed = None if args.seed is None else args.seed * 10000 + ai * 100 + t
                samples = make_synthetic(args.antennas, true_deg, args.snapshots,
                                         args.snr, args.wavelength, args.spacing, seed)
                # find_angle prints its own line; keep the sweep table clean.
                with contextlib.redirect_stdout(io.StringIO()):
                    est = find_angle(samples, wave_length=args.wavelength,
                                     distance_d=args.spacing, number_of_drones=args.sources)
                last_est = est
                errs.append(abs(est - true_deg))
            mean_err = sum(errs) / len(errs)
            max_err = max(errs)
            all_errs.extend(errs)
            print(f"  {true_deg:>10.1f} {last_est:>9} {mean_err:>9.2f} {max_err:>8.2f}")

        mean_all = sum(all_errs) / len(all_errs)
        rmse = (sum(e * e for e in all_errs) / len(all_errs)) ** 0.5
        print()
        print(f"[MUSIC] summary over {len(all_errs)} runs:  "
              f"mean_err={mean_all:.2f} deg  RMSE={rmse:.2f} deg  max_err={max(all_errs):.2f} deg")


if __name__ == "__main__":
    main()