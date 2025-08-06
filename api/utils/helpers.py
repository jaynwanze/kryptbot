
def _sf(x, default: float = 0.0) -> float:
    """Safe float: handles '', None, and non-numeric gracefully."""
    try:
        if x is None: 
            return float(default)
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "":
            return float(default)
        return float(s)
    except Exception:
        return float(default)
