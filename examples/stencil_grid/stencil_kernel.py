"""
This version was taken from the stencil_specializer project and has all asp stuff removed
in order to work on a direct c-tree llvm implementation

The main driver, intercepts the kernel() call and invokes the other components.

Stencil kernel classes are subclassed from the StencilKernel class
defined here. At initialization time, the text of the kernel() method
is parsed into a Python AST, then converted into a StencilModel by
stencil_python_front_end. The kernel() function is replaced by
shadow_kernel(), which intercepts future calls to kernel().

During each call to kernel(), stencil_unroll_neighbor_iter is called
to unroll neighbor loops, stencil_convert is invoked to convert the
model to C++, and an external compiler tool is invoked to generate a
binary which then efficiently completes executing the call. The binary
is cached for future calls.
"""

import numpy
import math
import inspect
import ast
# from examples.stencil_grid.stencil_python_front_end import *
# from examples.stencil_grid.stencil_unroll_neighbor_iter import *
# from examples.stencil_grid.stencil_optimize_cpp import *
# from examples.stencil_grid.stencil_convert import *
import copy
from copy import deepcopy

import ctree.transformations as transform
from ctree.jit import LazySpecializedFunction
from ctree.c.types import *
from ctree.frontend import get_ast
from ctree.visitors import NodeTransformer
from ctree.c.nodes import *
from ctree.omp.nodes import *
from ctree.dotgen import to_dot


import logging

logging.basicConfig(filename='tmp.txt',
                            filemode='w',
                            format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%H:%M:%S',
                            level=20)


class SpecializedTranslator(LazySpecializedFunction):
  def __init__(self, func, entry_point, input_grids, output_grid):
    self.input_grids = input_grids
    self.output_grid = output_grid
    super().__init__( get_ast(func), entry_point )

  def args_to_subconfig(self, args):
    conf = ()
    for arg in args:
        conf += ((len(arg), arg.dtype, arg.ndim, arg.shape),)
    return conf

  def transform(self, tree, program_config):
    """Convert the Python AST to a C AST."""
    kernel_sig = [Void()]
    for arg in program_config[0]:
        kernel_sig.append(NdPointer(arg[1], arg[2], arg[3]))

    tree = StencilTransformer(self.input_grids, self.output_grid).visit(tree)
    tree = transform.PyBasicConversions().visit(tree)
    params = tree.find(FunctionDecl, name="kernel").params
    params.pop(0)
    self.gen_array_macro_definition(tree, params)
    tree.find(FunctionDecl, name="kernel").set_typesig(kernel_sig)
    # print(ast.dump(tree))
    return tree

  def gen_array_macro_definition(self, tree, arg_names):
    first_for = tree.find(For)
    for index, arg in enumerate(self.input_grids + (self.output_grid, )):
        defname = "_%s_array_macro" % arg_names[index]
        params = "(" + ','.join(["_d"+str(x) for x in range(arg.dim)]) + ")"
        calc = "((_d%d)" % (arg.dim - 1)
        for x in range(arg.dim - 1):
            calc += "+((_d%s) * %s)" % (str(x), str(int(arg.data.strides[x]/arg.data.itemsize)))
        calc += ")"
        first_for.insert_before(Define(defname+params, calc))

class StencilTransformer(NodeTransformer):


    def __init__(self, input_grids, output_grid):
        # TODO: Give these wrapper classes?
        self.input_grids = input_grids
        self.output_grid = output_grid
        self.ghost_depth = output_grid.ghost_depth
        self.next_fresh_var = 0
        self.var_list = []
        self.input_dict = {}
        super(StencilTransformer, self).__init__()

    def visit_FunctionDef(self, node):
        for index, arg in enumerate(node.args.args[1:]):
            if index < len(self.input_grids):
                self.input_dict[arg.arg] = self.input_grids[index]
            else:
                self.output_grid_name = arg.arg
        node.body = list(map(self.visit, node.body))
        return node

    def gen_fresh_var(self):
        self.next_fresh_var += 1
        return "x%d" % self.next_fresh_var

    def visit_For(self, node):
        if type(node.iter) is ast.Call and \
           type(node.iter.func) is ast.Attribute:
            if node.iter.func.attr is 'interior_points':
                dim = len(self.output_grid.shape)
                self.kernel_target = node.target.id
                curr_node = None
                ret_node = None
                for d in range(dim):
                    initial = Constant(self.ghost_depth)
                    end = Constant(self.output_grid.shape[d] - self.ghost_depth - 1)
                    target = SymbolRef(self.gen_fresh_var())
                    self.var_list.append(target.name)
                    if d == 0:
                        curr_node = For(Assign(SymbolRef(target.name, Int()), initial),
                                       LtE(target, end),
                                       PostInc(target),
                                       []
                                    )
                        ret_node = curr_node
                    elif d == dim - 2:
                        next_node = [OmpParallelFor(), For(Assign(SymbolRef(target.name, Int()), initial),
                                       LtE(target, end),
                                       PostInc(target),
                                       []
                                    )]
                        curr_node.body = next_node
                        curr_node = next_node[1]
                    elif d == dim - 1:
                        next_node = [OmpIvDep(), For(Assign(SymbolRef(target.name, Int()),
                            initial),
                                       LtE(target, end),
                                       PostInc(target),
                                       []
                                    )]
                        curr_node.body = next_node
                        curr_node = next_node[1]
                    else:
                        curr_node.body = [For(Assign(SymbolRef(target.name, Int()), initial),
                                       LtE(target, end),
                                       PostInc(target),
                                       []
                                    )]
                        curr_node = curr_node.body[0]
                curr_node.body = self.visit(node.body[0])
                self.kernel_target = None
                return ret_node
            elif node.iter.func.attr is 'neighbors':
                neighbors_id = node.iter.args[1].n
                grid_name = node.iter.func.value.id
                grid = self.input_dict[grid_name]
                zero_point = tuple([0 for x in range(grid.dim)])
                neighbors = grid.neighbor_definition[neighbors_id]
                neighbors_of_grid = node.iter.args[0].id
                self.neighbor_target = node.target.id
                self.neighbor_grid_name = grid_name
                body = []
                self.output_index = self.gen_fresh_var()
                body.append(Assign(SymbolRef(self.output_index, Int()), self.gen_array_macro(self.output_grid_name, [SymbolRef(x) for x in self.var_list])))
                statement = node.body[0]
                for x in grid.neighbors(zero_point, neighbors_id):
                    self.offset_list = list(x)
                    for statement in node.body:
                        body.append(self.visit(deepcopy(statement)))
                self.neighbor_target = None
                return body
        return node

    # Handle array references
    def visit_Subscript(self, node):
        grid_name = node.value.id
        target = node.slice.value
        if isinstance(target, ast.Name):
            target = target.id
            if grid_name is self.output_grid_name and target == self.kernel_target:
                return ArrayRef(SymbolRef(self.output_grid_name), SymbolRef(self.output_index))
            elif grid_name in self.input_dict and target == self.kernel_target:
                grid = self.input_dict[grid_name]
                zero_point = tuple([0 for x in range(grid.dim)])
                index = self.gen_array_macro(grid_name, list(map(lambda x, y: Add(SymbolRef(x), SymbolRef(y)), self.var_list, zero_point)))
                return ArrayRef(SymbolRef(grid_name), index)
            elif grid_name == self.neighbor_grid_name:
                index = self.gen_array_macro(grid_name, list(map(lambda x, y: Add(SymbolRef(x), SymbolRef(y)), self.var_list, self.offset_list)))
                return ArrayRef(SymbolRef(grid_name), index)
        elif isinstance(target, ast.Call):
            return ArrayRef(SymbolRef(grid_name), self.visit(target))
        return node

    def visit_Call(self, node):
        if node.func.id == 'distance':
            zero_point = tuple([0 for x in range(len(self.offset_list))])
            return Constant(self.distance(zero_point, self.offset_list))
        elif node.func.id == 'int':
            return Cast(Int(), self.visit(node.args[0]))
        node.args = list(map(self.visit, node.args))
        return node

    def distance(self, x, y):
        return math.sqrt(sum([(x[i]-y[i])**2 for i in range(0, len(x))]))

    def gen_array_macro(self, arg, point):
        name = "_%s_array_macro" % arg
        return FunctionCall(SymbolRef(name), point)

    def visit_AugAssign(self, node):
        # print(ast.dump(node))
        return AddAssign(self.visit(node.target), self.visit(node.value))



# may want to make this inherit from something else...
class StencilKernel(object):
    def __init__(self, with_cilk=False):
        # we want to raise an exception if there is no kernel()
        # method defined.
        try:
            dir(self).index("kernel")
        except ValueError:
            raise Exception("No kernel method defined.")

        # get text of kernel() method and parse into a StencilModel
        self.kernel_src = inspect.getsource(self.kernel)
        # print(self.kernel_src)
        self.kernel_ast = ast.parse(self.remove_indentation(self.kernel_src))
        # print(ast.dump(self.kernel_ast, include_attributes=True))

        # self.model = StencilPythonFrontEnd().parse(self.kernel_ast)
        # print(ast.dump(self.model, include_attributes=True))

        self.model = self.kernel
        # print(self.new_kernel)


        self.pure_python = False
        self.pure_python_kernel = self.kernel
        self.should_unroll = True
        self.should_cacheblock = False
        self.block_size = 1

        # replace kernel with shadow version
        self.kernel = self.shadow_kernel

        self.specialized_sizes = None
        self.with_cilk = with_cilk

    def remove_indentation(self, src):
        return src.lstrip()

    def add_libraries(self, mod):
        # these are necessary includes, includedirs, and init statements to use the numpy library
        mod.add_library("numpy",[numpy.get_include()+"/numpy"])
        mod.add_header("arrayobject.h")
        mod.add_to_init([cpp_ast.Statement("import_array();")])
        if self.with_cilk:
            mod.module.add_to_preamble([cpp_ast.Include("cilk/cilk.h", True)])


    def shadow_kernel(self, *args):
        if self.pure_python:
            return self.pure_python_kernel(*args)

        myargs = [y.data for y in args]
        self.model = SpecializedTranslator(self.model, "kernel", args[0:-1], args[-1])
        return self.model(*myargs)

        #FIXME: instead of doing this short-circuit, we should use the Asp infrastructure to
        # do it, by passing in a lambda that does this check
        # if already specialized to these sizes, just run
        if self.specialized_sizes and self.specialized_sizes == [y.shape for y in args]:
            debug_print("match!")
            self.mod.kernel(*[y.data for y in args])
            return

        # otherwise, do the first-run flow

        # ask asp infrastructure for machine and platform info, including if cilk+ is available
        #FIXME: implement.  set self.with_cilk=true if cilk is available

        input_grids = args[0:-1]
        output_grid = args[-1]
        model = copy.deepcopy(self.model)
        model = StencilUnrollNeighborIter(model, input_grids, output_grid).run()

        # depending on whether cilk is available, we choose which converter to use
        if not self.with_cilk:
            Converter = StencilConvertAST
        else:
            Converter = StencilConvertASTCilk

        # generate variant with no unrolling, then generate variants for various unrollings
        base_variant = Converter(model, input_grids, output_grid).run()
        variants = [base_variant]
        variant_names = ["kernel"]

        # we only cache block if the size is large enough for blocking
        # or if the user has told us to

        if (len(args[0].shape) > 1 and args[0].shape[0] > 128):
            self.should_cacheblock = True
            self.block_sizes = [16, 32, 48, 64, 128, 160, 192, 256]
        else:
            self.should_cacheblock = False
            self.block_sizes = []

        if self.should_cacheblock and self.should_unroll:
            import itertools
            for b in list(set(itertools.permutations(self.block_sizes, len(args[0].shape)-1))):
                for u in [1,2,4,8]:
                    # ensure the unrolling is valid for the given blocking

                    #if b[len(b)-1] >= u:
                    if args[0].shape[len(args[0].shape)-1] >= u:
                        c = list(b)
                        c.append(1)
                        #variants.append(Converter(model, input_grids, output_grid, unroll_factor=u, block_factor=c).run())

                        variant = StencilOptimizeCpp(copy.deepcopy(base_variant), output_grid.shape, unroll_factor=u, block_factor=c).run()
                        variants.append(variant)
                        variant_names.append("kernel_block_%s_unroll_%s" % ('_'.join([str(y) for y in c]) ,u))

                        debug_print("ADDING BLOCKED")

        if self.should_unroll:
            for x in [2,4,8,16]: #,32,64]:
                check_valid = max(map(
                    # FIXME: is this the right way to figure out valid unrollings?
                    lambda y: (y.shape[-1]-2*y.ghost_depth) % x,
                    args))

                if check_valid == 0:
                    debug_print("APPENDING VARIANT %s" % x)
                    variants.append(StencilOptimizeCpp(copy.deepcopy(base_variant), output_grid.shape, unroll_factor=x).run())
                    variant_names.append("kernel_unroll_%s" % x)

        debug_print(variant_names)
        from asp.jit import asp_module

        mod = self.mod = asp_module.ASPModule()
        self.add_libraries(mod)

        self.set_compiler_flags(mod)
        mod.add_function("kernel", variants, variant_names)

        # package arguments and do the call
        myargs = [y.data for y in args]
        mod.kernel(*myargs)

        # save parameter sizes for next run
        self.specialized_sizes = [x.shape for x in args]

    def set_compiler_flags(self, mod):
        import asp.config

        if self.with_cilk or asp.config.CompilerDetector().detect("icc"):
            mod.backends["c++"].toolchain.cc = "icc"
            mod.backends["c++"].toolchain.cflags += ["-intel-extensions", "-fast", "-restrict"]
            # original, below is chick debugging mac mod.backends["c++"].toolchain.cflags += ["-openmp", "-fno-fnalias", "-fno-alias"]
            mod.backends["c++"].toolchain.cflags += ["-fno-fnalias", "-fno-alias"]
            mod.backends["c++"].toolchain.cflags += ["-I/usr/include/x86_64-linux-gnu"]
            mod.backends["c++"].toolchain.cflags.remove('-fwrapv')
            mod.backends["c++"].toolchain.cflags.remove('-O2')
            mod.backends["c++"].toolchain.cflags.remove('-g')
            mod.backends["c++"].toolchain.cflags.remove('-g')
            mod.backends["c++"].toolchain.cflags.remove('-fno-strict-aliasing')
        else:
            # original, mac debugging by chick            mod.backends["c++"].toolchain.cflags += ["-fopenmp", "-O3", "-msse3", "-Wno-unknown-pragmas"]
            mod.backends["c++"].toolchain.cflags += ["-O3", "-msse3", "-Wno-unknown-pragmas"]

        if mod.backends["c++"].toolchain.cflags.count('-Os') > 0:
            mod.backends["c++"].toolchain.cflags.remove('-Os')
        if mod.backends["c++"].toolchain.cflags.count('-O2') > 0:
            mod.backends["c++"].toolchain.cflags.remove('-O2')
        debug_print("toolchain" + str(mod.backends["c++"].toolchain.cflags))