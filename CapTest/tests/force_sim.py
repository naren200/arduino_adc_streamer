"""Synthetic switched-RC circuit for the force-model tests.

Each visit: V_k = V_pred(V_before) + dv_k, where dv_k is the voltage rise caused by the charge
the sensor generated since the previous visit (dQ / Coff). The raw residual is then exactly
dv_k, so the proxy dQ = Ctotal * e equals (Ctotal / Coff) times the true charge.
"""

import numpy as np

from captest import force_model as fm

REFS = [(2, i) for i in range(5)]


def make_models(c_total_pf=75.0, r_bias=1e6, v_d=1.25, c0=0.08, c1=0.02, qv=0.01, refs=REFS):
    return {ref: fm.RouteModel(ref, r_bias, c_total_pf * 1e-12 * (1 + 0.1 * i), v_d, c0, c1, qv)
            for i, ref in enumerate(refs)}


def steady_v(model, tc, qv=None):
    """Voltage of the fixed point of the model map (no charge generation)."""
    qv = model.qv if qv is None else qv
    decay = np.exp(-tc / model.tau)
    d = 0.0
    for _ in range(3000):
        d = decay * ((1 - model.c0 - model.c1 * d) * d + qv)
    return model.v_d - d


def simulate(models, t, force, tc=20e-6, d33_true=600e-12, coff_pf=60.0, weights=None,
             noise_v=0.0, qv_err=0.0, seed=1, lam=1.0, v_leak=None, creep_v_per_s=0.0,
             creep_window=None):
    """Samples dict ref -> {t, volts, tc} for visit times ``t`` (s) and applied force (N).

    qv_err:  the circuit's true qv differs from the model's (a constant residual bias).
    lam:     true off-state relaxation V_before = Vleak + (V_prev - Vleak) * lam.
    creep_v_per_s: extra voltage rise per second unrelated to force (reference drift).
    The charge generated between visits is d33_true * dF, shared by ``weights`` over routes.
    """
    rng = np.random.default_rng(seed)
    refs = list(models)
    weights = weights or {r: 1.0 / len(refs) for r in refs}
    force = np.asarray(force, dtype=float)
    tc_arr = np.broadcast_to(np.asarray(tc, dtype=float), np.shape(t)).copy()
    dF = np.diff(force, prepend=force[0])
    out = {}
    for ref in refs:
        m = models[ref]
        decays = np.exp(-tc_arr / m.tau)
        coff = coff_pf * 1e-12
        dv = d33_true * dF * weights[ref] / coff
        dt = np.diff(t, prepend=t[0] - (t[1] - t[0]))
        active = np.ones(len(t), dtype=bool) if creep_window is None else (
            (np.asarray(t) >= creep_window[0]) & (np.asarray(t) <= creep_window[1]))
        dv = dv + creep_v_per_s * dt * active
        leak_ref = m.v_d if v_leak is None else v_leak
        v = np.empty(len(t))
        prev = steady_v(m, tc_arr[0], m.qv + qv_err)
        for k in range(len(t)):
            before = leak_ref + (prev - leak_ref) * lam if lam < 1.0 else prev
            d_prev = m.v_d - before
            pred = m.v_d - decays[k] * ((1 - m.c0 - m.c1 * d_prev) * d_prev + m.qv + qv_err)
            prev = pred + dv[k] + (rng.normal(0.0, noise_v) if noise_v else 0.0)
            v[k] = prev
        out[ref] = {"t": np.asarray(t, dtype=float), "volts": v, "tc": tc_arr.copy()}
    return out


def trial_force(t, events, g=9.80665):
    """Piecewise-constant force from (time_s, delta_mass_g) events (instant steps)."""
    f = np.zeros(len(t))
    for when, grams in events:
        f[np.asarray(t) >= when] += grams * 1e-3 * g
    return f
