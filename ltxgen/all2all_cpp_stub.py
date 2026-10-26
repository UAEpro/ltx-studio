"""Windows stub for ltx-kernels' Linux-only multi-GPU all2all extension (single-GPU host)."""


class All2All:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("all2all_cpp is not available on Windows (multi-GPU sequence parallel only).")
