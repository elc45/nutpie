from typing import Dict, List, Tuple, Callable, Optional
from dataclasses import dataclass

import numpy as np
import fastprogress
import arviz
import pyarrow
from numpy.typing import DTypeLike, NDArray
import xarray as xr
import pandas as pd

from . import lib


@dataclass(frozen=True)
class CompiledModel:
    n_dim: int
    dims: Dict[str, Tuple[str, ...]]
    coords: Dict[str, xr.IndexVariable]
    shapes: Dict[str, Tuple[int, ...]]

    def _make_sampler(self, *args, **kwargs):
        raise NotImplementedError()


def _trace_to_arviz(traces, n_tune, shapes, **kwargs):
    n_draws = len(traces[0][0])
    n_chains = len(traces)

    data_dict = {}
    data_dict_tune = {}
    stats_dict = {}
    stats_dict_tune = {}

    draw_batches = []
    stats_batches = []
    for (draws, stats) in traces:
        draw_batches.append(pyarrow.RecordBatch.from_struct_array(draws))
        stats_batches.append(pyarrow.RecordBatch.from_struct_array(stats))

    table = pyarrow.Table.from_batches(draw_batches)
    table_stats = pyarrow.Table.from_batches(stats_batches)
    for name, col in zip(table.column_names, table.columns):
        data = col.combine_chunks().values.to_numpy()
        data = data.reshape((n_chains, n_draws) + tuple(shapes[name]))
        data_dict[name] = data[:, n_tune:]
        data_dict_tune[name] = data[:, :n_tune]

    for name, col in zip(table_stats.column_names, table_stats.columns):
        if name in ["chain", "draw"]:
            continue
        col = col.combine_chunks()
        if hasattr(col, "values"):
            data = col.values.to_numpy(False)
            last_shape = (-1,)
        else:
            data = col.to_numpy(False)
            last_shape = ()
        data = data.reshape((n_chains, n_draws) + last_shape)
        stats_dict[name] = data[:, n_tune:]
        stats_dict_tune[name] = data[:, :n_tune]

    return arviz.from_dict(
        data_dict,
        sample_stats=stats_dict,
        warmup_posterior=data_dict_tune,
        warmup_sample_stats=stats_dict_tune,
        **kwargs,
    )


def sample(
    compiled_model: CompiledModel,
    *,
    draws: int = 1000,
    tune: int = 300,
    chains: int = 6,
    cores: int = 6,
    seed: Optional[int] = None,
    num_try_init=200,
    save_warmup: bool = True,
    progress_bar: bool = True,
    init_mean: Optional[np.ndarray] = None,
    **kwargs,
) -> arviz.InferenceData:
    """Sample the posterior distribution for a compiled model.

    Parameters
    ----------
    draws: int
        The number of draws after tuning in each chain.
    tune: int
        The number of tuning (warmup) draws in each chain.
    chains: int
        The number of chains to sample.
    cores: int
        The number of chains that should run in parallel.
    seed: int
        Seed for the randomness in sampling.
    num_try_init: int
        The number if initial positions for each chain to try.
        Fail if we can't find a valid initializion point after
        this many tries.
    save_warmup: bool
        Wether to save the tuning (warmup) statistics and
        posterior draws in the output dataset.
    store_divergences: bool
        If true, store the exact locations where diverging
        transitions happend in the sampler stats.
    progress_bar: bool
        If true, display the progress bar (default)
    init_mean: ndarray
        Initialize the chains using jittered values around this
        point on the transformed parameter space. Defaults to
        zeros.
    store_unconstrained: bool
        If True, store each draw in the unconstrained (transformed)
        space in the sample stats.
    store_gradient: bool
        If True, store the logp gradient of each draw in the unconstrained
        space in the sample stats.
    store_mass_matrix: bool
        If True, store the current mass matrix at each draw in
        the sample stats.
    target_accept: float between 0 and 1, default 0.8
        Adapt the step size of the integrator so that the average
        acceptance probability of the draws is `target_accept`.
        Larger values will decrease the step size of the integrator,
        which can help when sampling models with bad geometry.
    maxdepth: int, default=10
        The maximum depth of the tree for each draw. The maximum
        number of gradient evaluations for each draw will
        be 2 ^ maxdepth.
    **kwargs
        Pass additional arguments to nutpie.lib.PySamplerArgs

    Returns
    -------
    trace : arviz.InferenceData
        An ArviZ ``InferenceData`` object that contains the samples.
    """
    settings = lib.PySamplerArgs()
    settings.num_tune = tune
    settings.num_draws = draws

    for name, val in kwargs.items():
        setattr(settings, name, val)

    if init_mean is None:
        init_mean = np.zeros(compiled_model.n_dim)

    sampler = compiled_model._make_sampler(settings, init_mean, chains, cores, seed)

    try:
        num_divs = 0
        chains_tuning = chains
        bar = fastprogress.progress_bar(
            sampler,
            total=chains * (draws + tune),
            display=progress_bar,
        )
        try:
            for info in bar:
                if info.draw == tune - 1:
                    chains_tuning -= 1
                if info.is_diverging and info.draw > tune:
                    num_divs += 1
                bar.comment = (
                    f" Chains in warmup: {chains_tuning}, Divergences: {num_divs}"
                )
        except KeyboardInterrupt:
            pass
    finally:
        results = sampler.finalize()

    return _trace_to_arviz(
        results,
        tune,
        compiled_model.shapes,
        dims={name: list(dim) for name, dim in compiled_model.dims.items()},
        coords={name: pd.Index(vals) for name, vals in compiled_model.coords.items()},
        save_warmup=save_warmup
    )
