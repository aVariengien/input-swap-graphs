# %%
import copy
import dataclasses
import itertools
import os
import pickle
import random
import random as rd
import time
from copy import deepcopy
from functools import partial
from pathlib import Path
from pprint import pprint
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union


import datasets
import einops
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import plotly.express as px
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import tqdm.auto as tqdm
import transformer_lens
import transformer_lens.utils as utils
from attrs import define, field

import swap_graphs as sgraph

from swap_graphs.datasets.ioi.ioi_dataset import (
    NAMES_GENDER,
    IOIDataset,
    check_tokenizer,
)
from swap_graphs.datasets.ioi.ioi_utils import (
    get_ioi_features_dict,
    logit_diff,
    logit_diff_comp,
    probs,
    assert_model_perf_ioi,
)
from IPython import get_ipython  # type: ignore
from jaxtyping import Float, Int
from names_generator import generate_name
from swap_graphs.core import (
    ActivationStore,
    CompMetric,
    ModelComponent,
    SwapGraph,
    WildPosition,
    find_important_components,
    SgraphDataset,
    compute_clustering_metrics,
)
from torch.utils.data import DataLoader
from transformer_lens import (
    ActivationCache,
    FactoredMatrix,
    HookedTransformer,
    HookedTransformerConfig,
)
from transformer_lens.hook_points import (  # Hooking utilities
    HookedRootModule,
    HookPoint,
)
from transformer_lens.loading_from_pretrained import OFFICIAL_MODEL_NAMES
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
from swap_graphs.utils import (
    KL_div_sim,
    L2_dist,
    L2_dist_in_context,
    imshow,
    line,
    plotHistLogLog,
    print_gpu_mem,
    save_object,
    scatter,
    show_attn,
    get_components_at_position,
    load_object,
    wrap_str,
    show_mtx,
)
from swap_graphs.core import SgraphDataset, SwapGraph, break_long_str

from tqdm import tqdm

import fire
import json
from typing import Literal

torch.set_grad_enabled(False)


from swap_graphs.datasets.nano_qa.nano_qa_dataset import (
    NanoQADataset,
    evaluate_model,
    get_nano_qa_features_dict,
)
from swap_graphs.datasets.nano_qa.nano_qa_utils import print_performance_table


def auto_sgraph(
    model_name: str,
    head_subpart: str = "z",
    include_mlp: bool = True,
    proportion_to_sgraph: float = 1.0,
    batch_size: int = 200,
    batch_size_sgraph: int = 200,
    nb_sample_eval: int = 200,
    nb_datapoints_sgraph: int = 100,
    xp_path: str = "../xp",
    dataset_name: Literal["IOI", "nanoQA"] = "IOI",
    restart_xp_name: Optional[str] = None,
):
    """
    Run swap graph on components of a model.

    head_subpart: subpart of the head to patch. Can be either z, q, k or v.
    include_mlp: whether to include the mlp in the swap graph. It's always their output that is patched: they are not influenced by the head_subpart param.
    proportion_to_graph: proportion of the components the most important to compute sgraph on.
    nb_sample: number of patching experiments for the structural step to find the important components
    xp_path: path to the folder where the results will be saved
    batch_size: batch size for building the swap graph
    """
    assert dataset_name in [
        "IOI",
        "nanoQA",
    ], "dataset_name must be either IOI or nanoQA"

    # %%

    COMP_METRIC = "KL"
    PATCHED_POSITION = "END"

    if restart_xp_name is None:
        if not os.path.exists(xp_path):
            os.mkdir(xp_path)

        xp_name = (
            model_name.replace("/", "-")
            + "-"
            + head_subpart
            + "-"
            + dataset_name
            + "-"
            + generate_name(seed=int(time.clock_gettime(0)))
        )

        xp_path = os.path.join(xp_path, xp_name)
        os.mkdir(xp_path)

        fig_path = os.path.join(xp_path, "figs")
        os.mkdir(fig_path)

        print(f"Experiment name: {xp_name} -- Experiment path: {xp_path}")
    else:
        xp_name = restart_xp_name
        assert type(xp_name) == str
        xp_path = os.path.join(xp_path, xp_name)
        fig_path = os.path.join(xp_path, "figs")
        print(
            f"Loading pre-existing data - Experiment name: {xp_name} -- Experiment path: {xp_path}"
        )

    date = time.strftime("%Hh%Mm%Ss %d-%m-%Y")  # add time stamp to the experiments
    open(os.path.join(xp_path, date), "a").close()

    # %% create config file

    config = {}
    config["model_name"] = model_name
    config["head_subpart"] = head_subpart
    config["include_mlp"] = include_mlp
    config["proportion_to_sgraph"] = proportion_to_sgraph
    config["batch_size"] = batch_size
    config["batch_size_sgraph"] = batch_size_sgraph
    config["nb_sample_eval"] = nb_sample_eval
    config["nb_datapoints_sgraph"] = nb_datapoints_sgraph
    config["xp_path"] = xp_path
    config["xp_name"] = xp_name
    config["dataset_name"] = dataset_name
    config["COMP_METRIC"] = COMP_METRIC
    config["PATCHED_POSITION"] = PATCHED_POSITION
    config["date"] = date

    loaded_comp_metric = False
    loaded_all_data = False
    comp_metric_res = None
    all_data = None
    if restart_xp_name is not None:
        old_config = load_object(xp_path, "config.pkl")

        for k in old_config:
            assert k in config, f"Key {k} not in config"
            if k not in ["xp_path", "xp_name", "date"]:
                assert (
                    config[k] == old_config[k]
                ), f"Key {k} has different value in config - {config[k]} vs {old_config[k]}"

        if os.path.exists(os.path.join(xp_path, "comp_metric.pkl")):
            comp_metric_res = load_object(xp_path, "comp_metric.pkl")
            print("Loaded comp_metric from restart folder")
            loaded_comp_metric = True

        if os.path.exists(os.path.join(xp_path, "sgraph_dataset.pkl")):
            sgraph_dataset = load_object(xp_path, "sgraph_dataset.pkl")
            dataset = load_object(xp_path, "dataset.pkl")
            print("Loaded datasets from restart folder")

        if os.path.exists(os.path.join(xp_path, "all_data.pkl")):
            all_data = load_object(xp_path, "all_data.pkl")
            print("Loaded all_data from restart folder")
            loaded_all_data = True

    if restart_xp_name is None:
        save_object(config, xp_path, "config.pkl")

    # %%
    ### Find important components by ressampling ablation

    print("loading model ...")
    model = HookedTransformer.from_pretrained(model_name, device="cuda")

    if dataset_name == "IOI":
        assert check_tokenizer(
            model.tokenizer
        ), "The tokenizer is tokenizing some word into two tokens."
        dataset = IOIDataset(
            N=nb_datapoints_sgraph,
            seed=42,
            wild_template=False,
            nb_names=5,
            tokenizer=model.tokenizer,
        )
        assert_model_perf_ioi(model, dataset)

        feature_dict = get_ioi_features_dict(dataset)
        sgraph_dataset = SgraphDataset(
            tok_dataset=dataset.prompts_tok,
            str_dataset=dataset.prompts_text,
            feature_dict=feature_dict,
        )

    elif (
        dataset_name == "nanoQA"
    ):  # Define the dataset, check the model performance on it and create the sgraph dataset
        dataset = NanoQADataset(
            nb_samples=nb_datapoints_sgraph,
            tokenizer=model.tokenizer,  # type: ignore
            seed=43,
            querried_variables=[
                "character_name",
                "city",
                # "character_occupation",
                # "season",
                # "day_time",
            ],
        )

        d = evaluate_model(model, dataset, batch_size=batch_size)
        for querried_feature in dataset.querried_variables:  # type: ignore
            assert d[f"{querried_feature}_top1_mean"] > 0.5

        print_performance_table(d)

        print("Model performance on the nanoQA dataset is good")

        feature_dict = get_nano_qa_features_dict(dataset)
        sgraph_dataset = SgraphDataset(
            tok_dataset=dataset.prompts_tok,
            str_dataset=dataset.prompts_text,
            feature_dict=feature_dict,
        )

    else:
        raise ValueError("Unknown dataset_name")

    if COMP_METRIC == "KL":
        comp_metric: CompMetric = partial(
            KL_div_sim,
            position_to_evaluate=WildPosition(dataset.word_idx["END"], label="END"),  # type: ignore
        )
    elif COMP_METRIC == "LDiff":
        comp_metric: CompMetric = partial(logit_diff_comp, ioi_dataset=dataset, keep_sign=True)  # type: ignore
    else:
        raise ValueError("Unknown comp_metric")

    components_to_search = get_components_at_position(
        position=WildPosition(
            dataset.word_idx[PATCHED_POSITION], label=PATCHED_POSITION
        ),
        nb_layers=model.cfg.n_layers,
        nb_heads=model.cfg.n_heads,
        include_mlp=include_mlp,
        head_subpart=head_subpart,
    )
    if not loaded_comp_metric:
        results = find_important_components(
            model=model,
            dataset=dataset.prompts_tok,
            nb_samples=nb_sample_eval,
            batch_size=batch_size,
            comp_metric=comp_metric,
            components_to_search=components_to_search,
            verbose=False,
            output_shape=(model.cfg.n_layers, model.cfg.n_heads + 1),
            force_cache_all=False,  # if true, will cache all the results in memory, faster but more memory intensive
        )
        if include_mlp:
            sec_dim = model.cfg.n_heads + 1
        else:
            sec_dim = model.cfg.n_heads

        save_object(
            torch.cat(results).reshape(model.cfg.n_layers, sec_dim, nb_sample_eval),
            xp_path,
            "comp_metric.pkl",
        )
        comp_metric_res = torch.cat(results).reshape(
            model.cfg.n_layers, sec_dim, nb_sample_eval
        )

        # %%
    assert (
        comp_metric_res is not None
    ), "comp_metric_res is None"  # ensure we loaded correclty the comp_metric_res or computed it

    mean_results = comp_metric_res.mean(2).cpu()

    try:
        show_mtx(
            mean_results,
            title=f"Average component importance {model_name} on {dataset_name} at {PATCHED_POSITION}",
            nb_heads=model.cfg.n_heads,
            display=False,
            save_path=fig_path,
            color_map_label="Avg. KL div. after uniform resampling",
        )
    except:
        print("Could not save figure")

    nb_component_to_sgraph = int(len(components_to_search) * proportion_to_sgraph)
    important_idx = mean_results.flatten().argsort()
    sorted_components = [components_to_search[i] for i in important_idx][::-1]
    important_components = sorted_components[:nb_component_to_sgraph]

    print(f"Number of components for sgraph: {len(important_components)}")

    if loaded_all_data:
        assert all_data is not None, "all_data is None"
        print(f"Found {len(all_data)} components in all_data")

        c_to_compute = []
        for c in important_components:
            if str(c) not in all_data:
                c_to_compute.append(c)
        print(
            f"Start running s graphs on the {len(c_to_compute)} remaining components ..."
        )
        important_components = c_to_compute

    # %%

    # %%

    save_object(sgraph_dataset, xp_path, "sgraph_dataset.pkl")
    save_object(dataset, xp_path, "dataset.pkl")

    if not loaded_all_data:
        all_data = {}
    else:
        assert type(all_data) == dict, "all_data is not a dict"

    for i in tqdm(range(len(important_components))):
        print(len(all_data))  # TODO: remove
        c = important_components[i]
        sgraph = SwapGraph(
            model=model,
            tok_dataset=dataset.prompts_tok,
            comp_metric=comp_metric,
            batch_size=batch_size_sgraph,
            proba_edge=1.0,
            patchedComponents=[c],
        )
        sgraph.build(verbose=False, progress_bar=False)
        sgraph.compute_weights()
        sgraph.compute_communities()

        component_data = {}
        component_data["clustering_metrics"] = compute_clustering_metrics(sgraph)
        component_data["feature_metrics"] = sgraph_dataset.compute_feature_rand(sgraph)
        component_data["sgraph_edges"] = sgraph.raw_edges
        component_data["commu"] = sgraph.commu_labels

        # deepcopy the component data
        all_data[str(c)] = deepcopy(component_data)

        # create html plot for the graph
        largest_rand_feature, max_rand_idx = max(
            component_data["feature_metrics"]["rand"].items(), key=lambda x: x[1]
        )
        title = wrap_str(
            f"<b>{sgraph.patchedComponents[0]}</b> Average CompMetric: {np.mean(sgraph.all_comp_metrics):.2f} (#{sorted_components.index(c)}), Rand idx commu-{largest_rand_feature}: {max_rand_idx:.2f}, modularity: {component_data['clustering_metrics']['modularity']:.2f}",
            max_line_len=70,
        )

        sgraph.show_html(
            sgraph_dataset,
            feature_to_show="all",
            title=title,
            display=False,
            save_path=fig_path,
            color_discrete=True,
        )
        if i % 2 == 0:  # save every 2 iterations
            save_object(all_data, xp_path, "all_data.pkl")
    save_object(all_data, xp_path, "all_data.pkl")


if __name__ == "__main__":
    fire.Fire(auto_sgraph)
# %%
