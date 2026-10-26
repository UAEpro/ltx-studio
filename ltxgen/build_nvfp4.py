"""Build only the `nvfp4_cpp` extension of LTX-2's ltx-kernels on Windows (MSVC + CUDA).

The upstream setup.py is Linux-only; NVFP4 just needs 3 sources + cuBLASLt.
Run with the LTX-2 venv python from an x64 MSVC environment:
    D:\\video\\LTX-2\\.venv\\Scripts\\python.exe build_nvfp4.py build_ext --build-lib <site-packages>
"""
import os
from pathlib import Path

os.environ.setdefault("TORCH_CUDA_ARCH_LIST", "12.0")
from setuptools import setup  # noqa: E402
from torch.utils.cpp_extension import CUDA_HOME, BuildExtension, CUDAExtension  # noqa: E402

KROOT = Path(__file__).resolve().parent.parent / "LTX-2" / "packages" / "ltx-kernels"


def src(p: str) -> str:
    return str(KROOT / p)


setup(
    name="nvfp4_cpp_win",
    ext_modules=[
        CUDAExtension(
            name="nvfp4_cpp",
            sources=[src("csrc/nvfp4/api.cpp"), src("csrc/nvfp4/gemm.cpp"), src("csrc/nvfp4/quantize.cu")],
            include_dirs=[src("csrc/nvfp4"), f"{CUDA_HOME}/include"],
            libraries=["cublasLt"],
            extra_compile_args={
                "cxx": ["/O2", "/std:c++20", "/permissive-", "/Zc:__cplusplus"],
                "nvcc": [
                    "-O3", "-std=c++20", "-Xcompiler", "/Zc:__cplusplus", "-Xcompiler", "/Zc:preprocessor", "--expt-relaxed-constexpr", "--expt-extended-lambda",
                    "-U__CUDA_NO_HALF_OPERATORS__", "-U__CUDA_NO_HALF_CONVERSIONS__",
                    "-U__CUDA_NO_BFLOAT16_CONVERSIONS__", "-DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK",
                    "-gencode=arch=compute_120a,code=sm_120a",
                ],
            },
        )
    ],
    cmdclass={"build_ext": BuildExtension},
)
