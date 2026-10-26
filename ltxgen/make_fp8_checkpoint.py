"""Write an fp8-pre-cast copy of an LTX-2.5 bf16 transformer (same result as `--quantization fp8-cast`, half the
bytes to stream per denoising step). Casts exactly the Linears fp8-cast would cast at load time:
transformer_blocks.*{.to_q,.to_k,.to_v,.to_out.0,ff.net.0.proj,ff.net.2}.{weight,bias}; everything else unchanged.

Run with the LTX-2 venv:  LTX-2\\.venv\\Scripts\\python.exe make_fp8_checkpoint.py SRC.safetensors DST.safetensors
Streams tensor by tensor (constant memory)."""
import json
import struct
import sys

import torch
from safetensors import safe_open

SUFFIXES = (".to_q", ".to_k", ".to_v", ".to_out.0", "ff.net.0.proj", "ff.net.2")
DT = {torch.bfloat16: "BF16", torch.float32: "F32", torch.float16: "F16", torch.float8_e4m3fn: "F8_E4M3",
      torch.uint8: "U8", torch.int64: "I64", torch.int32: "I32"}


def casts(key: str) -> bool:
    if "transformer_blocks." not in key:
        return False
    for end in (".weight", ".bias"):
        if key.endswith(end) and key[: -len(end)].endswith(SUFFIXES):
            return True
    return False


def main(src: str, dst: str) -> None:
    with safe_open(src, framework="pt", device="cpu") as f:
        meta = f.metadata() or {}
        keys = list(f.keys())
        header, offset, plan = {}, 0, []
        for k in keys:
            sl = f.get_slice(k)
            shape = list(sl.get_shape())
            src_dt = sl.get_dtype()
            out_dt = "F8_E4M3" if casts(k) else src_dt
            itemsize = {"F8_E4M3": 1, "U8": 1, "BF16": 2, "F16": 2, "F32": 4, "I64": 8, "I32": 4}[out_dt]
            n = itemsize
            for d in shape:
                n *= d
            header[k] = {"dtype": out_dt, "shape": shape, "data_offsets": [offset, offset + n]}
            plan.append((k, out_dt))
            offset += n
        header["__metadata__"] = meta
        hb = json.dumps(header, separators=(",", ":")).encode()
        hb += b" " * ((8 - len(hb) % 8) % 8)
        with open(dst, "wb") as out:
            out.write(struct.pack("<Q", len(hb)))
            out.write(hb)
            for i, (k, out_dt) in enumerate(plan):
                t = f.get_tensor(k)
                if out_dt == "F8_E4M3":
                    t = t.to(torch.float8_e4m3fn)
                out.write(t.contiguous().view(torch.uint8).numpy().tobytes())
                if i % 200 == 0:
                    print(f"{i}/{len(plan)}", flush=True)
    n_cast = sum(1 for _, d in plan if d == "F8_E4M3")
    print(json.dumps({"ok": True, "tensors": len(plan), "cast_fp8": n_cast, "bytes": offset}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
