import unittest
import numpy as np
import pycl as cl
import ctypes as ct

from ctree.meta.core import meta
from ctree.jit import LazySpecializedFunction, ConcreteSpecializedFunction
from ctree.nodes import Project
from ctree.c.nodes import FunctionCall, FunctionDecl, SymbolRef, Constant, \
    Assign, ArrayRef, Add, Ref, CFile
from ctree.templates.nodes import StringTemplate
from ctree.ocl.nodes import OclFile
from ctree.ocl.macros import clSetKernelArg, get_global_id, NULL
import ctree.np


class OclFunc(ConcreteSpecializedFunction):
    def __init__(self):
        self.context = cl.clCreateContextFromType()
        self.queue = cl.clCreateCommandQueue(self.context)

    def finalize(self, kernel, tree, entry_name, entry_type):
        self.kernel = kernel
        self._c_function = self._compile(entry_name, tree, entry_type)
        return self

    def __call__(self, A, B):
        a_buf, evt = cl.buffer_from_ndarray(self.queue, A, blocking=False)
        evt.wait()
        b_buf, evt = cl.buffer_from_ndarray(self.queue, B, blocking=False)
        evt.wait()
        C = np.zeros_like(A)
        c_buf, evt = cl.buffer_from_ndarray(self.queue, C, blocking=False)
        evt.wait()
        self._c_function(self.queue, self.kernel, a_buf, b_buf, c_buf)
        _, evt = cl.buffer_to_ndarray(self.queue, c_buf, C)
        evt.wait()
        return C


class OclAdd(LazySpecializedFunction):
    def args_to_subconfig(self, args):
        A = args[0]
        return tuple(np.ctypeslib.ndpointer(A.dtype, A.ndim, A.shape)
                     for _ in args + (args[0], ))

    def transform(self, tree, program_config):
        arg_cfg = program_config[0]
        A = arg_cfg[0]
        B = arg_cfg[1]
        C = arg_cfg[2]
        len_A = np.prod(A._shape_)
        # inner_type = A.__dtype__.type()

        kernel = FunctionDecl(
            None, "add_kernel",
            params=[SymbolRef("A", A()).set_global(),
                    SymbolRef("B", B()).set_global(),
                    SymbolRef("C", C()).set_global()],
            defn=[Assign(ArrayRef(SymbolRef("C"), get_global_id(0)),
                         Add(ArrayRef(SymbolRef('A'), get_global_id(0)),
                             ArrayRef(SymbolRef('B'), get_global_id(0))))]
        ).set_kernel()

        file = OclFile("kernel", [kernel])

        control = [
            StringTemplate("""
                #ifdef __APPLE__
                #include <OpenCL/opencl.h>
                #else
                #include <CL/cl.h>
                #endif
            """),
            FunctionDecl(
                None, 'control', params=[SymbolRef('queue', cl.cl_command_queue()),
                              SymbolRef('kernel', cl.cl_kernel()),
                              SymbolRef('a', cl.cl_mem()),
                              SymbolRef('b', cl.cl_mem()),
                              SymbolRef('c', cl.cl_mem())],
                defn=[
                    Assign(SymbolRef('global', ct.c_ulong()), Constant(len_A)),
                    Assign(SymbolRef('local', ct.c_ulong()), Constant(32)),
                    clSetKernelArg('kernel', 0, ct.sizeof(cl.cl_mem), 'a'),
                    clSetKernelArg('kernel', 1, ct.sizeof(cl.cl_mem), 'b'),
                    clSetKernelArg('kernel', 2, ct.sizeof(cl.cl_mem), 'c'),
                    FunctionCall(SymbolRef('clEnqueueNDRangeKernel'),
                                 [SymbolRef('queue'), SymbolRef('kernel'),
                                  Constant(1), Constant(0),
                                  Ref(SymbolRef('global')),
                                  Ref(SymbolRef('local')), Constant(0),
                                  NULL(), NULL()]),
                    FunctionCall(SymbolRef('clFinish'), [SymbolRef('queue')])
                ])
        ]

        proj = Project([file, CFile('control', control)])
        print(proj.files[1])
        fn = OclFunc()
        program = cl.clCreateProgramWithSource(fn.context,
                                               kernel.codegen()).build()
        ptr = program['add_kernel']
        entry_type = ct.CFUNCTYPE(None, cl.cl_command_queue, cl.cl_kernel,
                                  cl.cl_mem, cl.cl_mem, cl.cl_mem)
        return fn.finalize(ptr, proj, "control", entry_type)

array_add = OclAdd(None)


class TestMetaDecorator(unittest.TestCase):
    def test_simple(self):
        @meta
        def func(a):
            return a + 3

        self.assertEqual(func(3), 6)

    def test_dataflow(self):
        @meta
        def func(a, b):
            c = array_add(a, b)
            return array_add(c, a)

        a = np.random.rand(256, 256).astype(np.float32) * 100
        b = np.random.rand(256, 256).astype(np.float32) * 100
        try:
            np.testing.assert_array_almost_equal(func(a, b), a + b + a)
        except AssertionError as e:
            self.fail("Arrays not almost equal\n{}".format(e))
