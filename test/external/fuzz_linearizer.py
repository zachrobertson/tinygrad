import random, traceback, ctypes
from typing import List, Tuple, DefaultDict
import numpy as np
from collections import defaultdict
from extra.optimization.helpers import load_worlds, ast_str_to_lin
from tinygrad.codegen.linearizer import Linearizer, UOp
from tinygrad.codegen.kernel import Opt
from tinygrad.features.search import get_linearizer_actions, bufs_from_lin
from tinygrad.tensor import Tensor
from tinygrad.features.graph import print_tree
from tinygrad.helpers import getenv, from_mv, prod, colored, Context
from tinygrad.device import Device, Compiled
from tinygrad.lazy import LazyBuffer
from tinygrad.ops import LazyOp

def tuplize_uops(uops:List[UOp]) -> Tuple: return tuple([(x.uop, x.dtype, tuple(uops.index(x) for x in x.vin), x.arg) for x in uops])

device = Device[Device.DEFAULT]

def get_fuzz_rawbufs(lin):
  rawbufs = bufs_from_lin(lin)

  # Reallocate output buffer with additional area to detect out-of-bounds writes.
  RED_AREA_SIZE = 1024 if isinstance(device, Compiled) else 0
  rawbufs[0] = get_fuzz_rawbuf_like(rawbufs[0], zero=True, size=rawbufs[0].size+RED_AREA_SIZE)
  with Context(DEBUG=0):
    for rawbuf in rawbufs[1:]:
      t = Tensor.uniform((rawbuf.size,), dtype=rawbuf.dtype)
      if isinstance(ld:=t.realize().lazydata, LazyBuffer) and ld.realized: rawbuf.copyin(ld.realized.as_buffer())
  return rawbufs

def get_fuzz_rawbuf_like(rawbuf, zero=False, size=None):
  rawbuf = type(rawbuf)(Device.DEFAULT, rawbuf.size if size is None else size, rawbuf.dtype)
  if zero:
    with Context(DEBUG=0):
      mv = memoryview(bytearray(rawbuf.size * rawbuf.dtype.itemsize))
      ctypes.memset(from_mv(mv), 0, len(mv))
      rawbuf.copyin(mv)
  return rawbuf

def run_linearizer(lin: Linearizer, rawbufs=None, var_vals=None):
  if rawbufs is None: rawbufs = bufs_from_lin(lin)
  if var_vals is None: var_vals = {v: v.min for v in lin.ast[0].vars()}

  # TODO: images needs required_optimization
  try:
    prg = device.to_program(lin)
  except Exception:
    traceback.print_exc()
    return "COMPILE_ERROR"

  try:
    prg(rawbufs, var_vals, wait=True, do_update_stats=False)
  except Exception:
    traceback.print_exc()
    return "EXEC_ERROR"

  return "PASS"

def compare_linearizer(lin: Linearizer, rawbufs=None, var_vals=None, ground_truth=None, rtol=1e-2, atol=1e-2):
  try:
    if rawbufs is None:
      rawbufs = get_fuzz_rawbufs(lin)
    else:
      rawbufs[0] = get_fuzz_rawbuf_like(rawbufs[0], zero=True) # get a new output buffer
  except BaseException:
    return ("RAWBUFS_ERROR", rawbufs, var_vals, ground_truth,)
  if var_vals is None: var_vals = {v: random.randint(v.min, v.max if isinstance(v.max, int) else v.min) for v in lin.ast[0].vars()}
  if ground_truth is None:
    unoptimized = Linearizer(*lin.ast)
    unoptimized.required_optimizations()
    if run_linearizer(unoptimized, rawbufs, var_vals) != "PASS":
      return ("BASELINE_ERROR", rawbufs, var_vals, ground_truth,)
    ground_truth = np.frombuffer(rawbufs[0].as_buffer(), rawbufs[0].dtype.np).copy()

  if (run_msg := run_linearizer(lin, rawbufs, var_vals)) != "PASS":
    return (run_msg, rawbufs, var_vals, ground_truth,)
  result = np.frombuffer(rawbufs[0].as_buffer(), rawbufs[0].dtype.np)
  return ("PASS" if np.allclose(result, ground_truth, rtol=rtol, atol=atol) else "COMPARE_ERROR", rawbufs, var_vals, ground_truth,)

def fuzz_linearizer(lin: Linearizer):
  SEED = getenv("SEED", 42)
  random.seed(SEED)
  np.random.seed(SEED)
  for op in lin.ast: print_tree(op)
  print(lin.colored_shape())
  seen_uops = {}
  last_lins = [lin]
  failures:DefaultDict[str, List[Tuple[Tuple[LazyOp,...],List[Opt]]]] = defaultdict(list)
  rawbufs, var_vals, ground_truth = None, None, None

  FUZZ_BEAM = getenv("FUZZ_BEAM", 0)
  FUZZ_MAX_SIZE = getenv("FUZZ_MAX_SIZE", 0)
  if FUZZ_MAX_SIZE > 0 and prod(lin.full_shape) > FUZZ_MAX_SIZE:
    print("skipping large kernel")
    return failures

  for depth in range(getenv("DEPTH", 1 if FUZZ_BEAM else 10)):
    next_lins = []
    for lin in last_lins:
      actions = get_linearizer_actions(lin, include_0=False)
      if FUZZ_BEAM: print(f"testing {lin.applied_opts=} with {len(actions)} actions")
      if not actions: continue

      test_lins = list(actions.values())
      if not FUZZ_BEAM: test_lins = [random.choice(test_lins)]

      for test_lin in test_lins:
        if not FUZZ_BEAM and test_lin.applied_opts: print(f"applied opts: {test_lin.applied_opts}")

        # stop if kernel uops repeat
        tuops = tuplize_uops(test_lin.linearize().uops.uops)
        if tuops in seen_uops:
          continue
        seen_uops[tuops] = tuple(test_lin.applied_opts)

        if not FUZZ_BEAM: print(test_lin.colored_shape())

        (msg, rawbufs, var_vals, ground_truth) = compare_linearizer(test_lin, rawbufs, var_vals, ground_truth)
        if msg != "PASS":
          print(test_lin.ast)
          print(test_lin.applied_opts)
          print(msg)
          failures[msg].append((test_lin.ast, test_lin.applied_opts))
          continue

        next_lins.append(test_lin)

    last_lins = next_lins
    if FUZZ_BEAM: print(f"depth={depth} total_lins={len(last_lins)} {failures=}")
  return failures

if __name__ == "__main__":
  ast_strs = load_worlds(filter_reduce=False, filter_novariable=False)
  print(f"{len(ast_strs)=}")
  tested = 0
  failed_ids = []
  failures = defaultdict(list)
  for i, ast in enumerate(ast_strs[:getenv("FUZZ_N", len(ast_strs))]):
    if (nth := getenv("FUZZ_NTH", -1)) != -1 and i != nth: continue
    if "dtypes.image" in ast and Device.DEFAULT != "GPU": continue  # IMAGE is only for GPU
    print(f"testing ast {i}")
    tested += 1
    lin = ast_str_to_lin(ast)

    fuzz_failures = fuzz_linearizer(lin)
    if fuzz_failures: failed_ids.append(i)
    for k, v in fuzz_failures.items():
      for f in v:
        failures[k].append(f)

  for msg, errors in failures.items():
    for i, (ast, opts) in enumerate(errors):
      print(f"{msg} {i} AST: {ast}")
      print(f"{msg} {i} OPTS: {opts}\n")

  print(f"{tested=}")
  if failures:
    print(f"{failed_ids=}")
    for msg, errors in failures.items():
      print(f"{msg}: {len(errors)}")
  else:
    print(colored("all passed", "green"))
