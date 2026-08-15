#!/usr/bin/env python3
"""
Calibration fitter v2 — global system matrix M + TRIPLE validation.

MODEL (v2, matches firmware >=1.2 PAIR/TRIPLE modes):
  All three emitters are identical WS2812s. For sensor i, pair p, LED color j:
      S_i = sum_p (cosθ/d_{i,p}²) · B_{p,j} · m_j          (m_j = M column j)
  where d_{i,p} is the distance from sensor i to pair p's LED.
  In a rigid joint the dominant path is the vertex partner pair (i == pair_map[p]).

CALIBRATION (single-emitter PAIR modes):
  PAIR p, color j lit alone -> partner sensor i = pair_map[p]:
      S_i - S_amb = (cosθ/d_p²) · 255 · m_j
      => m_j = (d_p²/(255·cosθ)) · (S_i - S_amb)   (proof Eq 17, now exact:
          exactly one emitter contributes)

  d_p is the caliper gap at the partner sensor's pillar (vertex geometry).

RUNTIME (TRIPLE mode): pair p burns color p (R,G,B).
      y = M⁻¹ · (S_i - S_amb)  ->  y_p = 255·cosθ/d_p²  (only pair p's term survives)
      => d̂_p = sqrt(255·cosθ / y_p)

VALIDATION: TRIPLE rows in the CSV are held out from the fit; the fitter
reports d̂ vs caliper truth per position using only M and the TRIPLE frame.

Usage:
  python3 calibrate_fit.py calibration.csv --pair-map 1,0,2 [--output M_matrix.csv]
"""
import numpy as np, csv, sys, math, argparse

def load_rows(path):
    with open(path) as f:
        return list(csv.DictReader(f))

def fit_M(rows, pair_map):
    """Fit global M (3x3) from PAIR rows. Returns M, n_used."""
    inv_pair = {s: p for p, s in enumerate(pair_map)}  # sensor -> pair
    est = [[] for _ in range(3)]  # per color column
    used = 0
    for r in rows:
        if not r["mode"].startswith("PAIR"): continue
        pair = int(r["pair"]); color = "RGB".index(r["color"]); si = pair_map[pair]
        d = float(r[f"d{si}"])                    # partner pillar gap
        cos_t = float(r["cos_theta"])
        S = np.array([float(r[f"S{si}_R"]), float(r[f"S{si}_G"]), float(r[f"S{si}_B"])])
        amb_rows = [x for x in rows if x["mode"]=="OFF" and x["position"]==r["position"]
                    and x.get("rep")==r.get("rep")]
        amb = np.array([float(amb_rows[0][f"S{si}_R"]), float(amb_rows[0][f"S{si}_G"]),
                        float(amb_rows[0][f"S{si}_B"])]) if amb_rows else np.zeros(3)
        m_j = (d**2 / (255.0 * cos_t)) * (S - amb)
        est[color].append(m_j); used += 1
    M = np.column_stack([np.mean(est[j], axis=0) if est[j] else np.zeros(3) for j in range(3)])
    return M, used

def analyze(M):
    print("\n=== Global system matrix M ===")
    for i in range(3): print(f"  [{M[i,0]:9.1f} {M[i,1]:9.1f} {M[i,2]:9.1f}]  # {'RGB'[i]} sensor ch")
    sv = np.linalg.svd(M, compute_uv=False)
    kappa = sv[0]/sv[-1] if sv[-1] > 0 else np.inf
    print(f"  kappa(M) = {kappa:.2f}")
    for i in range(3):
        off = np.delete(np.abs(M[i]), i)
        print(f"  diag_dom {'RGB'[i]}: {abs(M[i,i])/off.max():.1f}x")
    if abs(np.linalg.det(M)) < 1e-12:
        print("  SINGULAR — cannot invert"); return None, kappa
    W = np.linalg.inv(M)
    print(f"  W·M offdiag max = {np.abs(W@M - np.eye(3)).max():.2e} (exact zero-forcing)")
    return W, kappa

def validate_triple(rows, pair_map, W):
    """Held-out TRIPLE validation: d̂_p = sqrt(255·cosθ/y_p) vs caliper d_p."""
    print("\n=== TRIPLE-mode validation (held out from fit) ===")
    print(f"  {'pos':>3} {'pair':>4} {'d_true':>7} {'d_hat':>7} {'err':>6}")
    errs = []
    for r in rows:
        if r["mode"] != "TRIPLE": continue
        cos_t = float(r["cos_theta"])
        pos = r["position"]
        amb_rows = [x for x in rows if x["mode"]=="OFF" and x["position"]==pos
                    and x.get("rep")==r.get("rep")]
        for p in range(3):
            si = pair_map[p]
            S = np.array([float(r[f"S{si}_R"]), float(r[f"S{si}_G"]), float(r[f"S{si}_B"])])
            if amb_rows:
                S -= np.array([float(amb_rows[0][f"S{si}_R"]), float(amb_rows[0][f"S{si}_G"]),
                               float(amb_rows[0][f"S{si}_B"])])
            y = W @ S
            if y[p] <= 0: continue
            d_hat = math.sqrt(255.0 * cos_t / y[p])
            d_true = float(r[f"d{si}"])
            errs.append(abs(d_hat - d_true))
            print(f"  {pos:>3} {p:>4} {d_true:>7.1f} {d_hat:>7.1f} {d_hat-d_true:>6.2f}")
    if errs:
        e = np.array(errs)
        print(f"\n  MAE={e.mean():.2f} mm  RMSE={np.sqrt((e**2).mean()):.2f} mm  max={e.max():.2f} mm  (n={len(e)})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--pair-map", default="1,0,2", help="pair->sensor map, comma sep (from collector)")
    ap.add_argument("--output", "-o", default="M_matrix.csv")
    args = ap.parse_args()
    pair_map = [int(x) for x in args.pair_map.split(",")]

    rows = load_rows(args.csv)
    n_pair = sum(1 for r in rows if r["mode"].startswith("PAIR"))
    print(f"Loaded {len(rows)} rows ({n_pair} PAIR fit rows, "
          f"{sum(1 for r in rows if r['mode']=='TRIPLE')} TRIPLE validation rows)")

    M, used = fit_M(rows, pair_map)
    print(f"Fit M from {used} single-emitter rows")
    W, kappa = analyze(M)
    if W is None: sys.exit(1)

    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["R", "G", "B"])
        for i in range(3): w.writerow([f"{M[i,j]:.4f}" for j in range(3)])
    print(f"\nM matrix -> {args.output}")

    validate_triple(rows, pair_map, W)

if __name__ == "__main__":
    main()
