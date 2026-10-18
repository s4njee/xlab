"""Small, dependency-free statistics for paired block designs.

Everything here works on plain lists of floats. The paired analysis is done on
log-ratios, so an estimate is a ratio (arm / control) and its interval is the
exponentiated interval of the mean log-ratio.
"""
import itertools
import math
import random
import statistics

ALPHA = 0.05


# -- distributions -------------------------------------------------------------

def _beta_cf(a, b, x):
    """Continued fraction for the regularized incomplete beta (modified Lentz)."""
    tiny, eps = 1e-300, 1e-15
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def beta_inc(a, b, x):
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_cf(a, b, x) / a
    return 1.0 - front * _beta_cf(b, a, 1.0 - x) / b


def t_cdf(t, df):
    x = df / (df + t * t)
    tail = 0.5 * beta_inc(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_quantile(p, df):
    """Inverse Student t CDF by bisection (accurate to ~1e-10, fast enough for reports)."""
    if not 0.0 < p < 1.0:
        raise ValueError('p must be in (0, 1)')
    if p == 0.5:
        return 0.0
    lo, hi = -1.0, 1.0
    while t_cdf(lo, df) > p:
        lo *= 2.0
    while t_cdf(hi, df) < p:
        hi *= 2.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2.0


def z_quantile(p):
    return statistics.NormalDist().inv_cdf(p)


# -- one-sample procedures on paired differences -------------------------------

def t_interval(values, alpha=ALPHA):
    """Mean and two-sided (1 - alpha) t interval. Zero spread gives a point interval."""
    n = len(values)
    mean = statistics.fmean(values)
    if n < 2:
        return mean, None, None
    sd = statistics.stdev(values)
    half = t_quantile(1.0 - alpha / 2.0, n - 1) * sd / math.sqrt(n)
    return mean, mean - half, mean + half


def _signed_rank_counts(n):
    """counts[w] = number of sign assignments of ranks 1..n whose positive-rank sum is w."""
    counts = [1]
    for rank in range(1, n + 1):
        grown = counts + [0] * rank
        for w, c in enumerate(counts):
            grown[w + rank] += c
        counts = grown
    return counts


def hodges_lehmann(values, alpha=ALPHA):
    """Hodges-Lehmann location estimate with its distribution-free (Wilcoxon) interval.

    The bounds are None when there are too few values for any interval at this level."""
    n = len(values)
    walsh = sorted((values[i] + values[j]) / 2.0 for i in range(n) for j in range(i, n))
    estimate = statistics.median(walsh)
    if n < 2:
        return estimate, None, None
    counts = _signed_rank_counts(n)
    total = 2 ** n
    cumulative, c = 0, 0
    for w, count in enumerate(counts):  # largest c with P(T+ <= c - 1) <= alpha / 2
        if (cumulative + count) / total > alpha / 2.0:
            c = w
            break
        cumulative += count
    if c < 1:
        return estimate, None, None
    m = len(walsh)
    return estimate, walsh[c - 1], walsh[m - c]


def sign_flip_p(values, samples=20000, seed=0):
    """Two-sided permutation p-value for a zero mean: exact up to 16 values, sampled above."""
    n = len(values)
    if n == 0:
        return None
    observed = abs(sum(values)) - 1e-12
    if n <= 16:
        hits = sum(1 for signs in itertools.product((1, -1), repeat=n)
                   if abs(sum(s * v for s, v in zip(signs, values))) >= observed)
        return hits / 2 ** n
    rng = random.Random(seed)
    hits = sum(1 for _ in range(samples)
               if abs(sum(v if rng.random() < 0.5 else -v for v in values)) >= observed)
    return (hits + 1) / (samples + 1)


def holm(p_values):
    """Holm step-down adjustment; None entries are passed through."""
    indexed = sorted((p, i) for i, p in enumerate(p_values) if p is not None)
    adjusted = list(p_values)
    running, m = 0.0, len(indexed)
    for rank, (p, i) in enumerate(indexed):
        running = max(running, min(1.0, (m - rank) * p))
        adjusted[i] = running
    return adjusted


def blocks_needed(sigma, delta, alpha=ALPHA, power=0.8):
    """Blocks for a paired test to detect a ratio of (1 + delta) given the sd of log-ratios."""
    if not sigma or delta <= 0:
        return None
    effect = math.log1p(delta)
    return max(2, math.ceil(((z_quantile(1 - alpha / 2) + z_quantile(power)) * sigma / effect) ** 2))


# -- verdicts -------------------------------------------------------------------

def classify(low, high, delta, better):
    """Verdict for a ratio interval (arm / control).

    better = "lower": a ratio below 1 is an improvement (times, bytes, cost).
    better = "higher": a ratio above 1 is an improvement (throughput, yield)."""
    if low is None or high is None:
        return 'inconclusive'
    up, down = 1.0 + delta, 1.0 / (1.0 + delta)
    if down <= low and high <= up:
        return 'equivalent'
    below, above = high < down, low > up
    if not (below or above):
        return 'inconclusive'
    if better == 'higher':
        return 'better' if above else 'worse'
    if better == 'lower':
        return 'better' if below else 'worse'
    return 'lower' if below else 'higher'


def describe(values):
    """Descriptive summary of raw values: n, mean, sd, cv, median, min, max, and a
    geometric mean with its t interval when every value is positive."""
    n = len(values)
    if not n:
        return {'n': 0}
    out = {'n': n, 'mean': statistics.fmean(values), 'median': statistics.median(values),
           'min': min(values), 'max': max(values)}
    out['sd'] = statistics.stdev(values) if n > 1 else 0.0
    out['cv'] = out['sd'] / out['mean'] if out['mean'] else None
    if all(v > 0 for v in values):
        mean, lo, hi = t_interval([math.log(v) for v in values])
        out['gmean'] = math.exp(mean)
        out['gmean_low'] = math.exp(lo) if lo is not None else None
        out['gmean_high'] = math.exp(hi) if hi is not None else None
    else:
        mean, lo, hi = t_interval(values)
        out['mean_low'], out['mean_high'] = lo, hi
    return out


def pareto_front(points, better_x, better_y):
    """Indices of non-dominated points. points: list of (x, y); better_*: "lower" | "higher"."""
    sx = 1 if better_x == 'higher' else -1
    sy = 1 if better_y == 'higher' else -1
    front = []
    for i, (x, y) in enumerate(points):
        dominated = False
        for j, (u, v) in enumerate(points):
            if i == j:
                continue
            if sx * u >= sx * x and sy * v >= sy * y and (sx * u > sx * x or sy * v > sy * y):
                dominated = True
                break
        if not dominated:
            front.append(i)
    return front
