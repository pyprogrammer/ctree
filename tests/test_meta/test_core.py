import unittest
import numpy as np
from ctree.meta.core import meta
from .array_add import array_add


class TestMetaDecorator(unittest.TestCase):
    def _check_arrays_equal(self, actual, expected):
        try:
            np.testing.assert_array_almost_equal(actual, expected)
        except AssertionError as e:
            self.fail("Arrays not almost equal\n{}".format(e))

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
        self._check_arrays_equal(func(a, b), a + b + a)

    @unittest.skip("not implemented")
    def test_dataflow_2(self):
        @meta
        def func(a, b):
            c = array_add(a, b)
            d = array_add(c, b)
            return array_add(d, c)

        a = np.random.rand(256, 256).astype(np.float32) * 100
        b = np.random.rand(256, 256).astype(np.float32) * 100
        actual = func(a, b)
        c = a + b
        d = c + b
        expected = d + c
        self._check_arrays_equal(actual, expected)