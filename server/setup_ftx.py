"""Сборка модуля _ftx (FT8/FT4 на основе ft8_lib): python setup_ftx.py build_ext --inplace"""
import glob
import os
import sys

from setuptools import Extension, setup

here = os.path.dirname(os.path.abspath(__file__))
lib = os.path.join('ftx', 'lib')
src = (['ftx/_ftxmodule.c'] + sorted(glob.glob(os.path.join(lib, 'ft8', '*.c'))) +
       [os.path.join(lib, 'common', 'monitor.c'), os.path.join(lib, 'fft', 'kiss_fft.c'),
        os.path.join(lib, 'fft', 'kiss_fftr.c')])
args = ['/O2', '/std:c11', '/D_USE_MATH_DEFINES', '/D_CRT_SECURE_NO_WARNINGS'] if sys.platform == 'win32' else \
       ['-O3', '-std=gnu11', '-w']
setup(name='ftx', version='1.0', ext_modules=[Extension('_ftx', src, include_dirs=[lib], extra_compile_args=args)],
      script_args=sys.argv[1:] or ['build_ext', '--inplace'])
