"""Launch an ltx_pipelines CLI module with local runtime patches (upstream repo stays untouched).

Usage: python run_pipeline.py <module-name> [pipeline args...]   e.g. run_pipeline.py distilled --prompt ...

Patches (both needed to run the 18.7 GB NVFP4 transformer on a 16 GB GPU):
1. allow block streaming (--offload cpu|disk) with the NVFP4 pre-quantized transformer;
2. keep uint8 / fp32-scalar dtypes in streamed block layouts (see _derive_layout below) - without it the
   render "succeeds" but is pure noise.

Patch 1 detail: allow block streaming (--offload cpu|disk) with the NVFP4 pre-quantized transformer.
Upstream refuses any non-fp8-cast policy because streamed LoRA fusion cannot emit companion
keys (.weight_scale). The streaming builder itself takes block layouts from the quantized meta
model, so NVFP4 blocks stream fine as long as no LoRA has to be fused into them. This is what
lets the 18.7 GB NVFP4 transformer run on a 16 GB GPU.
"""
import runpy
import sys

from ltx_core.block_streaming import DISK_CPU_SLOTS, StreamingModelBuilder
from ltx_core.loader.fuse_loras import bf16_fuse_rule
from ltx_pipelines.utils.blocks import diffusion_stage as ds
from ltx_pipelines.utils.types import OffloadMode

_orig = ds.DiffusionStage._build_streaming_builder


def _build_streaming_builder(*, checkpoint_path, configurator, model_sd_ops, loras, quantization, registry,
                             offload_mode):
    if quantization is None or quantization.fuse_rule is ds.fp8_cast_fuse_rule or loras:
        return _orig(checkpoint_path=checkpoint_path, configurator=configurator, model_sd_ops=model_sd_ops,
                     loras=loras, quantization=quantization, registry=registry, offload_mode=offload_mode)
    return StreamingModelBuilder(
        model_class_configurator=configurator,
        model_path=checkpoint_path,
        model_sd_ops=model_sd_ops,
        loras=loras,
        registry=registry,
        fuse_rule=bf16_fuse_rule,  # never invoked: no LoRAs
        blocks_attr="transformer_blocks",
        blocks_prefix="transformer_blocks",
        cpu_slots_count=DISK_CPU_SLOTS if offload_mode == OffloadMode.DISK else None,
    )


ds.DiffusionStage._build_streaming_builder = staticmethod(_build_streaming_builder)


# Patch 2: keep the real dtypes of quantized tensors in streamed block layouts. Upstream coerces every
# non-FP8 tensor to the compute dtype (bf16), which silently turns NVFP4's packed uint8 weights and
# fp32 per-tensor scales (input_scale / weight_scale_2) into garbage -> the model outputs pure noise.
import torch  # noqa: E402
from ltx_core.block_streaming import builder as _bs_builder  # noqa: E402
from ltx_core.block_streaming import utils as _bs_utils  # noqa: E402


def _derive_layout(tensors, dtype=None):
    out = {}
    for name, t in tensors.items():
        keep = (dtype is None or t.dtype in _bs_utils.FP8_DTYPES or not t.dtype.is_floating_point
                or (t.dtype == torch.float32 and t.numel() == 1))
        out[name] = (t.shape, t.dtype if keep else dtype)
    return out


_bs_builder.derive_layout = _derive_layout


# Patch 3: stream blocks with plain positioned file reads instead of a safetensors mmap. On Windows the
# streaming reader's mmap plus the loader's second mmap of the same file must both fit in RAM + pagefile;
# for the 42 GB bf16 transformer (quality preset) they don't, and the second open silently yields
# "Attempted to access the data pointer on an invalid python storage".
import json  # noqa: E402
import struct  # noqa: E402
import threading  # noqa: E402

_ST_DTYPES = {"F64": torch.float64, "F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16,
              "F8_E4M3": torch.float8_e4m3fn, "F8_E5M2": torch.float8_e5m2, "U8": torch.uint8, "I8": torch.int8,
              "I16": torch.int16, "I32": torch.int32, "I64": torch.int64, "BOOL": torch.bool}


class _FileTensorReader:
    """Same interface as ltx_core.block_streaming.disk.DiskTensorReader, without memory mapping."""

    def __init__(self, paths: list[str]) -> None:
        self._paths = list(paths)
        self._index: dict[str, tuple[int, int, int, str, list[int]]] = {}
        for i, p in enumerate(self._paths):
            with open(p, "rb") as fh:
                n = struct.unpack("<Q", fh.read(8))[0]
                hdr = json.loads(fh.read(n))
            hdr.pop("__metadata__", None)
            for k, v in hdr.items():
                a, b = v["data_offsets"]
                self._index[k] = (i, 8 + n + a, b - a, v["dtype"], v["shape"])
        self._local = threading.local()

    def _handle(self, i: int):
        hs = getattr(self._local, "handles", None)
        if hs is None:
            hs = self._local.handles = {}
        if i not in hs:
            hs[i] = open(self._paths[i], "rb", buffering=0)  # noqa: SIM115
        return hs[i]

    def get_tensor(self, key: str) -> torch.Tensor:
        i, off, nbytes, dt, shape = self._index[key]
        buf = torch.empty(nbytes, dtype=torch.uint8)
        fh = self._handle(i)
        fh.seek(off)
        view, got = memoryview(buf.numpy()), 0
        while got < nbytes:
            r = fh.readinto(view[got:])
            if not r:
                raise OSError(f"short read for {key}")
            got += r
        return buf.view(_ST_DTYPES[dt]).reshape(shape)

    def close(self) -> None:
        self._index.clear()

    def __contains__(self, key: str) -> bool:
        return key in self._index

    def __iter__(self):
        return iter(self._index)


_MmapReader = _bs_builder.DiskTensorReader
_MMAP_MAX_BYTES = 24 * 1024**3  # below this, two mmaps fit comfortably and mmap (page cache) is faster


def _pick_reader(paths: list[str]):
    import os
    total = sum(os.path.getsize(p) for p in paths)
    return _MmapReader(paths) if total <= _MMAP_MAX_BYTES else _FileTensorReader(paths)


_bs_builder.DiskTensorReader = _pick_reader

if __name__ == "__main__":
    module = sys.argv[1]
    sys.argv = [f"ltx_pipelines.{module}", *sys.argv[2:]]
    runpy.run_module(f"ltx_pipelines.{module}", run_name="__main__", alter_sys=True)
