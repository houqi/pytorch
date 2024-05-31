import functools
import importlib
import torch
import torch.utils._pytree as pytree
from torch._inductor import config
from torch._inductor.runtime.hints import TRITON_MAX_BLOCK
from torch._inductor.runtime.runtime_utils import is_power_of_2
from torch._inductor.test_case import TestCase as InductorTestCase
from torch.testing._internal.inductor_utils import (
    HAS_GPU,
    GPU_TYPE,
    skip_windows_ci,
)
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
    subtest,
)
from torch._inductor.utils import run_and_get_code
from typing import Any, Callable, Tuple, Type, Union
import unittest


requires_gpu = functools.partial(unittest.skipIf, not HAS_GPU, "requires gpu")

skip_windows_ci(__name__, __file__)

importlib.import_module("filelock")


@instantiate_parametrized_tests
class TritonBlockPointerTest(InductorTestCase):

    def run_and_compare(self, func: Callable[..., Any], *args, compile_kwargs: dict = None):
        """
        Runs the module through Inductor, comparing to eager reference.
        """
        if compile_kwargs is None:
            compile_kwargs = {}

        def flatten_tensors(tensors):
            flat, spec = pytree.tree_flatten(tensors)
            return flat

        compiled = torch.compile(func, backend="inductor", **compile_kwargs)

        ref_tensors = flatten_tensors(func(*args))
        result, code = run_and_get_code(compiled, *args)
        actual_tensors = flatten_tensors(result)

        for ref, actual in zip(ref_tensors, actual_tensors):
            self.assertTrue(torch.allclose(ref, actual))

        return result, code

    @requires_gpu()
    @config.patch("triton.use_block_ptr", True)
    @parametrize(
        "full_size,view_size,stride,offset,require_block_ptr",
        [
            args for args in (
                ((64, 32, 32), (32, 16, 8), None, None, True),
                ((16, 8, 8, 8), (8, 8, 4, 2), None, None, True),
                ((8, 8), (4, 4), None, 10, True), # Storage offset
                ((8, 8), (4, 4), (16, 2), None, True), # Non-default strides
                ((15, 9), (8, 8), None, None, True), # Non-power-of-2 full dims
                ((15, 9), (15, 3), None, None, False), # Non-power-of-2 view dims
                ((1, 1, 1), (1, 1, 1), None, None, False), # Scalar
            )
        ],
    )
    def test_strided_block_ptr(self,
                               full_size: Tuple[int],
                               view_size: Tuple[int],
                               stride: Union[Tuple[int], None],
                               offset: Union[int, None],
                               require_block_ptr: bool):
        """
        Test generating strided ND block pointers.

        If `require_block_ptr=True`, the generated code must contain block
        pointers. However, ND block pointers are not supported for all shapes. So
        we also test some odd shapes with `require_block_ptr=False`, to ensure that
        block pointer analysis does not break these cases.
        """
        def view(full: torch.Tensor):
            # Use the original tensor's stride by default
            nonlocal stride
            if stride is None:
                stride = full.stride()

            return torch.as_strided(full, view_size, stride, storage_offset=offset)

        def foo(x, y):
            x, y = tuple(view(tensor) for tensor in (x, y))
            return x + y

        device = torch.device(GPU_TYPE)
        args = [torch.randn(full_size).to(device) for arg_idx in range(2)]

        result, (code,) = self.run_and_compare(foo, *args)

        # Optionally check for block pointers
        if require_block_ptr:
            num_block_ptrs = code.count("tl.make_block_ptr")
            self.assertEqual(num_block_ptrs, 3)

    @requires_gpu()
    @config.patch("triton.use_block_ptr", True)
    def test_different_sized_blocks(self):
        """
        Test that we can generate strided block pointers when inputs have different
        shapes.
        """
        def foo(x, y):
            return x + 1, y * 2

        device = torch.device(GPU_TYPE)
        full_size = (16, 16)
        full = torch.randn(full_size).to(device)
        x_size = (8, 8)
        y_size = (4, 4)
        x, y = tuple(torch.as_strided(full, size, full.stride()) for size in (x_size, y_size))

        # Check that input sizes are not the same
        self.assertNotEqual(x_size, y_size)

        result, (code,) = self.run_and_compare(foo, x, y)

        # Expect 4 block pointers: 2 inputs and 2 outputs
        num_block_ptrs = code.count("tl.make_block_ptr")
        self.assertEqual(num_block_ptrs, 4)

    @requires_gpu()
    @config.patch("triton.use_block_ptr", True)
    def test_partial_block_pointer(self):
        """
        Test mixing block pointers with non-structured pointers.
        """
        def foo(*args):
            return sum(torch.sum(arg) for arg in args)

        device = torch.device(GPU_TYPE)
        full_size = (15, 15) # Not a power of 2
        view_size = (8, 8)
        full = torch.randn(full_size).to(device)
        view = torch.as_strided(full, view_size, full.stride())

        result, (code,) = self.run_and_compare(foo, full, view)

        # Expect 1 block pointer: view
        num_block_ptrs = code.count("tl.make_block_ptr")
        self.assertEqual(num_block_ptrs, 1)

    @requires_gpu()
    @config.patch("triton.use_block_ptr", True)
    def test_multiple_max_block_non_power_of_2(self):
        """
        Check that we support dims of size n * MAX_BLOCK, where n is any positive integer, not
        necessarily a power of 2.
        """
        def foo(x):
            return x - 1

        device = torch.device(GPU_TYPE)
        max_block = TRITON_MAX_BLOCK["X"]
        full_size = (3 * max_block, 3)
        view_size = (3 * max_block, 2)
        full = torch.randn(full_size).to(device)
        view = torch.as_strided(full, view_size, full.stride())

        # Check that we're using dims that aren't all powers of 2
        have_np2_dim = not all(is_power_of_2(dim) for dim in view_size)
        self.assertTrue(have_np2_dim)

        # Check that we need more than one stride to represent the tensor
        nontrivial_dims = [dim for dim in view_size if dim > 1]
        self.assertTrue(len(nontrivial_dims) > 1)

        result, (code,) = self.run_and_compare(foo, view)

        # Expect 2 block pointers: input and output
        num_block_ptrs = code.count("tl.make_block_ptr")
        self.assertEqual(num_block_ptrs, 2)

    @requires_gpu()
    @config.patch("triton.use_block_ptr", True)
    def test_dynamic_shapes_generic(self):
        """
        Test a generic strided block with dynamic shapes. Block pointers are not
        expected. This only checks that the analysis doesn't break this case.
        """
        def foo(x, y):
            return x / y

        device = torch.device(GPU_TYPE)
        full_size = (8,8)
        view_size = (4,4)
        full = torch.randn(full_size).to(device)
        view = torch.as_strided(full, view_size, full.stride())

        result, (code,) = self.run_and_compare(foo, view, view, compile_kwargs={'dynamic': True})

    @unittest.skip(reason="Dynamo tracing error")
    @requires_gpu()
    @config.patch("triton.use_block_ptr", True)
    def test_dynamic_shapes_multiple_max_block(self):
        """
        Test dynamic shapes, where we know the shape is a multiple of the max block
        size. We should be able to generate a block pointer for this case.
        """
        max_block = TRITON_MAX_BLOCK["X"]

        def foo(x):
            tile_dims = (3 * max_block * x.shape[0], 3 * x.shape[1])
            view_size = (3 * max_block * x.shape[0], 2 * x.shape[1])
            full = x.tile(tile_dims)
            view = torch.as_strided(full, view_size, full.stride())
            result = view + view

            return result

        device = torch.device(GPU_TYPE)
        x_size = (1, 1)
        x = torch.randn(x_size).to(device)

        result, (code,) = self.run_and_compare(x, compile_kwargs={'dynamic': True})

        # Expect 3 block pointers: 2 inputs and output
        num_block_ptrs = code.count("tl.make_block_ptr")
        self.assertEqual(num_block_ptrs, 3)


if __name__ == "__main__":
    from torch._inductor.test_case import run_tests

    if HAS_GPU:
        run_tests(needs="filelock")
