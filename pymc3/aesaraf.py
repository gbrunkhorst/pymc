#   Copyright 2020 The PyMC Developers
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import aesara
import numpy as np

from aesara import scalar
from aesara import tensor as at
from aesara.gradient import grad
from aesara.graph.basic import Apply, graph_inputs
from aesara.graph.op import Op
from aesara.sandbox.rng_mrg import MRG_RandomStream as RandomStream
from aesara.tensor.elemwise import Elemwise
from aesara.tensor.var import TensorVariable

from pymc3.data import GeneratorAdapter
from pymc3.vartypes import continuous_types, int_types, typefilter

__all__ = [
    "gradient",
    "hessian",
    "hessian_diag",
    "inputvars",
    "cont_inputs",
    "floatX",
    "intX",
    "smartfloatX",
    "jacobian",
    "CallableTensor",
    "join_nonshared_inputs",
    "make_shared_replacements",
    "generator",
    "set_at_rng",
    "at_rng",
    "take_along_axis",
]


def inputvars(a):
    """
    Get the inputs into a aesara variables

    Parameters
    ----------
        a: aesara variable

    Returns
    -------
        r: list of tensor variables that are inputs
    """
    return [v for v in graph_inputs(makeiter(a)) if isinstance(v, TensorVariable)]


def cont_inputs(f):
    """
    Get the continuous inputs into a aesara variables

    Parameters
    ----------
        a: aesara variable

    Returns
    -------
        r: list of tensor variables that are continuous inputs
    """
    return typefilter(inputvars(f), continuous_types)


def floatX(X):
    """
    Convert a aesara tensor or numpy array to aesara.config.floatX type.
    """
    try:
        return X.astype(aesara.config.floatX)
    except AttributeError:
        # Scalar passed
        return np.asarray(X, dtype=aesara.config.floatX)


_conversion_map = {"float64": "int32", "float32": "int16", "float16": "int8", "float8": "int8"}


def intX(X):
    """
    Convert a aesara tensor or numpy array to aesara.tensor.int32 type.
    """
    intX = _conversion_map[aesara.config.floatX]
    try:
        return X.astype(intX)
    except AttributeError:
        # Scalar passed
        return np.asarray(X, dtype=intX)


def smartfloatX(x):
    """
    Converts numpy float values to floatX and leaves values of other types unchanged.
    """
    if str(x.dtype).startswith("float"):
        x = floatX(x)
    return x


"""
Aesara derivative functions
"""


def gradient1(f, v):
    """flat gradient of f wrt v"""
    return at.flatten(grad(f, v, disconnected_inputs="warn"))


empty_gradient = at.zeros(0, dtype="float32")


def gradient(f, vars=None):
    if vars is None:
        vars = cont_inputs(f)

    if vars:
        return at.concatenate([gradient1(f, v) for v in vars], axis=0)
    else:
        return empty_gradient


def jacobian1(f, v):
    """jacobian of f wrt v"""
    f = at.flatten(f)
    idx = at.arange(f.shape[0], dtype="int32")

    def grad_i(i):
        return gradient1(f[i], v)

    return aesara.map(grad_i, idx)[0]


def jacobian(f, vars=None):
    if vars is None:
        vars = cont_inputs(f)

    if vars:
        return at.concatenate([jacobian1(f, v) for v in vars], axis=1)
    else:
        return empty_gradient


def jacobian_diag(f, x):
    idx = at.arange(f.shape[0], dtype="int32")

    def grad_ii(i):
        return grad(f[i], x)[i]

    return aesara.scan(grad_ii, sequences=[idx], n_steps=f.shape[0], name="jacobian_diag")[0]


@aesara.config.change_flags(compute_test_value="ignore")
def hessian(f, vars=None):
    return -jacobian(gradient(f, vars), vars)


@aesara.config.change_flags(compute_test_value="ignore")
def hessian_diag1(f, v):
    g = gradient1(f, v)
    idx = at.arange(g.shape[0], dtype="int32")

    def hess_ii(i):
        return gradient1(g[i], v)[i]

    return aesara.map(hess_ii, idx)[0]


@aesara.config.change_flags(compute_test_value="ignore")
def hessian_diag(f, vars=None):
    if vars is None:
        vars = cont_inputs(f)

    if vars:
        return -at.concatenate([hessian_diag1(f, v) for v in vars], axis=0)
    else:
        return empty_gradient


def makeiter(a):
    if isinstance(a, (tuple, list)):
        return a
    else:
        return [a]


class IdentityOp(scalar.UnaryScalarOp):
    @staticmethod
    def st_impl(x):
        return x

    def impl(self, x):
        return x

    def grad(self, inp, grads):
        return grads

    def c_code(self, node, name, inp, out, sub):
        return "{z} = {x};".format(x=inp[0], z=out[0])

    def __eq__(self, other):
        return isinstance(self, type(other))

    def __hash__(self):
        return hash(type(self))


def make_shared_replacements(vars, model):
    """
    Makes shared replacements for all *other* variables than the ones passed.

    This way functions can be called many times without setting unchanging variables. Allows us
    to use func.trust_input by removing the need for DictToArrayBijection and kwargs.

    Parameters
    ----------
    vars: list of variables not to make shared
    model: model

    Returns
    -------
    Dict of variable -> new shared variable
    """
    othervars = set(model.vars) - set(vars)
    return {
        var: aesara.shared(
            var.tag.test_value, var.name + "_shared", broadcastable=var.broadcastable
        )
        for var in othervars
    }


def join_nonshared_inputs(xs, vars, shared, make_shared=False):
    """
    Takes a list of aesara Variables and joins their non shared inputs into a single input.

    Parameters
    ----------
    xs: list of aesara tensors
    vars: list of variables to join

    Returns
    -------
    tensors, inarray
    tensors: list of same tensors but with inarray as input
    inarray: vector of inputs
    """
    if not vars:
        raise ValueError("Empty list of variables.")

    joined = at.concatenate([var.ravel() for var in vars])

    if not make_shared:
        tensor_type = joined.type
        inarray = tensor_type("inarray")
    else:
        inarray = aesara.shared(joined.tag.test_value, "inarray")

    inarray.tag.test_value = joined.tag.test_value

    replace = {}
    last_idx = 0
    for var in vars:
        arr_len = at.prod(var.shape)
        replace[var] = reshape_t(inarray[last_idx : last_idx + arr_len], var.shape).astype(
            var.dtype
        )
        last_idx += arr_len

    replace.update(shared)

    xs_special = [aesara.clone_replace(x, replace, strict=False) for x in xs]
    return xs_special, inarray


def reshape_t(x, shape):
    """Work around fact that x.reshape(()) doesn't work"""
    if shape != ():
        return x.reshape(shape)
    else:
        return x[0]


class CallableTensor:
    """Turns a symbolic variable with one input into a function that returns symbolic arguments
    with the one variable replaced with the input.
    """

    def __init__(self, tensor):
        self.tensor = tensor

    def __call__(self, input):
        """Replaces the single input of symbolic variable to be the passed argument.

        Parameters
        ----------
        input: TensorVariable
        """
        (oldinput,) = inputvars(self.tensor)
        return aesara.clone_replace(self.tensor, {oldinput: input}, strict=False)


scalar_identity = IdentityOp(scalar.upgrade_to_float, name="scalar_identity")
identity = Elemwise(scalar_identity, name="identity")


class GeneratorOp(Op):
    """
    Generator Op is designed for storing python generators inside aesara graph.

    __call__ creates TensorVariable
        It has 2 new methods
        - var.set_gen(gen): sets new generator
        - var.set_default(value): sets new default value (None erases default value)

    If generator is exhausted, variable will produce default value if it is not None,
    else raises `StopIteration` exception that can be caught on runtime.

    Parameters
    ----------
    gen: generator that implements __next__ (py3) or next (py2) method
        and yields np.arrays with same types
    default: np.array with the same type as generator produces
    """

    __props__ = ("generator",)

    def __init__(self, gen, default=None):
        super().__init__()
        if not isinstance(gen, GeneratorAdapter):
            gen = GeneratorAdapter(gen)
        self.generator = gen
        self.set_default(default)

    def make_node(self, *inputs):
        gen_var = self.generator.make_variable(self)
        return Apply(self, [], [gen_var])

    def perform(self, node, inputs, output_storage, params=None):
        if self.default is not None:
            output_storage[0][0] = next(self.generator, self.default)
        else:
            output_storage[0][0] = next(self.generator)

    def do_constant_folding(self, fgraph, node):
        return False

    __call__ = aesara.config.change_flags(compute_test_value="off")(Op.__call__)

    def set_gen(self, gen):
        if not isinstance(gen, GeneratorAdapter):
            gen = GeneratorAdapter(gen)
        if not gen.tensortype == self.generator.tensortype:
            raise ValueError("New generator should yield the same type")
        self.generator = gen

    def set_default(self, value):
        if value is None:
            self.default = None
        else:
            value = np.asarray(value, self.generator.tensortype.dtype)
            t1 = (False,) * value.ndim
            t2 = self.generator.tensortype.broadcastable
            if not t1 == t2:
                raise ValueError("Default value should have the same type as generator")
            self.default = value


def generator(gen, default=None):
    """
    Generator variable with possibility to set default value and new generator.
    If generator is exhausted variable will produce default value if it is not None,
    else raises `StopIteration` exception that can be caught on runtime.

    Parameters
    ----------
    gen: generator that implements __next__ (py3) or next (py2) method
        and yields np.arrays with same types
    default: np.array with the same type as generator produces

    Returns
    -------
    TensorVariable
        It has 2 new methods
        - var.set_gen(gen): sets new generator
        - var.set_default(value): sets new default value (None erases default value)
    """
    return GeneratorOp(gen, default)()


_at_rng = RandomStream()


def at_rng(random_seed=None):
    """
    Get the package-level random number generator or new with specified seed.

    Parameters
    ----------
    random_seed: int
        If not None
        returns *new* aesara random generator without replacing package global one

    Returns
    -------
    `aesara.tensor.random.utils.RandomStream` instance
        `aesara.tensor.random.utils.RandomStream`
        instance passed to the most recent call of `set_at_rng`
    """
    if random_seed is None:
        return _at_rng
    else:
        ret = RandomStream(random_seed)
        return ret


def set_at_rng(new_rng):
    """
    Set the package-level random number generator.

    Parameters
    ----------
    new_rng: `aesara.tensor.random.utils.RandomStream` instance
        The random number generator to use.
    """
    # pylint: disable=global-statement
    global _at_rng
    # pylint: enable=global-statement
    if isinstance(new_rng, int):
        new_rng = RandomStream(new_rng)
    _at_rng = new_rng


def floatX_array(x):
    return floatX(np.array(x))


def ix_(*args):
    """
    Aesara np.ix_ analog

    See numpy.lib.index_tricks.ix_ for reference
    """
    out = []
    nd = len(args)
    for k, new in enumerate(args):
        if new is None:
            out.append(slice(None))
        new = at.as_tensor(new)
        if new.ndim != 1:
            raise ValueError("Cross index must be 1 dimensional")
        new = new.reshape((1,) * k + (new.size,) + (1,) * (nd - k - 1))
        out.append(new)
    return tuple(out)


def largest_common_dtype(tensors):
    dtypes = {
        str(t.dtype) if hasattr(t, "dtype") else smartfloatX(np.asarray(t)).dtype for t in tensors
    }
    return np.stack([np.ones((), dtype=dtype) for dtype in dtypes]).dtype


def _make_along_axis_idx(arr_shape, indices, axis):
    # compute dimensions to iterate over
    if str(indices.dtype) not in int_types:
        raise IndexError("`indices` must be an integer array")
    shape_ones = (1,) * indices.ndim
    dest_dims = list(range(axis)) + [None] + list(range(axis + 1, indices.ndim))

    # build a fancy index, consisting of orthogonal aranges, with the
    # requested index inserted at the right location
    fancy_index = []
    for dim, n in zip(dest_dims, arr_shape):
        if dim is None:
            fancy_index.append(indices)
        else:
            ind_shape = shape_ones[:dim] + (-1,) + shape_ones[dim + 1 :]
            fancy_index.append(at.arange(n).reshape(ind_shape))

    return tuple(fancy_index)


def take_along_axis(arr, indices, axis=0):
    """Take values from the input array by matching 1d index and data slices.

    This iterates over matching 1d slices oriented along the specified axis in
    the index and data arrays, and uses the former to look up values in the
    latter. These slices can be different lengths.

    Functions returning an index along an axis, like argsort and argpartition,
    produce suitable indices for this function.
    """
    arr = at.as_tensor_variable(arr)
    indices = at.as_tensor_variable(indices)
    # normalize inputs
    if axis is None:
        arr = arr.flatten()
        arr_shape = (len(arr),)  # flatiter has no .shape
        _axis = 0
    else:
        if axis < 0:
            _axis = arr.ndim + axis
        else:
            _axis = axis
        if _axis < 0 or _axis >= arr.ndim:
            raise ValueError(
                "Supplied `axis` value {} is out of bounds of an array with "
                "ndim = {}".format(axis, arr.ndim)
            )
        arr_shape = arr.shape
    if arr.ndim != indices.ndim:
        raise ValueError("`indices` and `arr` must have the same number of dimensions")

    # use the fancy index
    return arr[_make_along_axis_idx(arr_shape, indices, _axis)]