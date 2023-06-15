from __future__ import annotations

# %%
from swap_graphs.datasets.nano_qa.questions import (
    QUESTIONS,
    QUESTION_PROMPT,
    gen_question_prompt,
)
from swap_graphs.datasets.nano_qa.nanostories import NANOSTORIES
from swap_graphs.datasets.nano_qa.nanostories_5_val import NANOSTORIES_5_VALS
from swap_graphs.datasets.nano_qa.narrative_variables import (
    QUERRIED_NARRATIVE_VARIABLES_PRETTY_NAMES,
)
from swap_graphs.datasets.nano_qa.nano_qa_utils import (
    check_tokenizer_nanoQA,
    print_performance_table,
)


from swap_graphs.utils import wrap_str

from swap_graphs.core import objects_to_unique_ids, objects_to_strings, WildPosition

import attrs
from typing import Dict, List, Optional, Callable, Union
from jaxtyping import Float
from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast
import numpy as np
import torch
from transformer_lens import (
    ActivationCache,
    FactoredMatrix,
    HookedTransformer,
    HookedTransformerConfig,
)
import transformer_lens
from tqdm import tqdm
import time
from pprint import pprint

from copy import deepcopy

torch.set_grad_enabled(False)


def sample_questions(
    all_questions: List[Dict[str, str]], querried_variables: List[str]
):
    """return a list of question filtered according to the querried variables"""
    return [
        q
        for q in all_questions
        if any([v in q["querried_variable"] for v in querried_variables])
    ]


@attrs.define
class NanoQADataset:
    """Dataset for QA on nanostories.
    querried_variables contains a list of the variables that are querried in the questions. If not set, all variables can be querried.
    K_shot defines the number of examples included in context. (currently not implem, only K_shot=0 is supported)
    """

    nb_samples: int = attrs.field()
    tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast] = attrs.field()
    seed: int = attrs.field(default=42)
    K_shot: int = attrs.field(default=1)
    name: str = attrs.field(default="")
    nb_variable_values: int = attrs.field(default=2)
    rng = attrs.field(init=False)
    nanostories: List[Dict] = attrs.field(init=False)
    questions: List[Dict[str, str]] = attrs.field(init=False)
    querried_variables: Optional[List[str]] = attrs.field(init=True, default=None)
    answer_texts: List[str] = attrs.field(init=False)
    prompts_text: List[str] = attrs.field(init=False)
    answer_tokens: List[int] = attrs.field(init=False)
    answer_first_token_texts: List[str] = attrs.field(init=False)
    word_idx: Dict[str, List[int]] = attrs.field(init=False)
    prompts_tok: torch.Tensor = attrs.field(init=False)

    def __attrs_post_init__(self):
        self.rng = np.random.default_rng(self.seed)
        
        assert self.nb_variable_values in [2, 5], "Only 2 or 5 variable values are supported"
        if self.nb_variable_values == 2:
            all_nanostories = NANOSTORIES
        elif self.nb_variable_values == 5:
            all_nanostories = NANOSTORIES_5_VALS
        
        nanostories = list(self.rng.choice(np.array(all_nanostories), size=self.nb_samples))

        if self.querried_variables is not None:
            for v in self.querried_variables:
                assert (
                    v in QUERRIED_NARRATIVE_VARIABLES_PRETTY_NAMES
                ), "Invalid querried variable."

            possible_questions = sample_questions(QUESTIONS, self.querried_variables)
        else:
            self.querried_variables = list(
                QUERRIED_NARRATIVE_VARIABLES_PRETTY_NAMES.keys()
            )
            possible_questions = QUESTIONS
        assert self.querried_variables is not None

        questions = list(
            self.rng.choice(np.array(possible_questions), size=self.nb_samples)
        )

        self.build_from_questions_and_stories(questions, nanostories)

    def build_from_questions_and_stories(
        self, questions: List[Dict[str, str]], nanostories: List[Dict]
    ):
        assert check_tokenizer_nanoQA(
            self.tokenizer
        ), "Your tokenizer is such that there are collision in the first token of the answer."

        self.questions = questions
        self.nanostories = nanostories
        self.answer_texts = []
        self.answer_tokens = []
        self.prompts_text = []
        self.answer_first_token_texts = []
        self.word_idx = {"END": []}

        for i in range(self.nb_samples):
            querried_variable = self.questions[i]["querried_variable"]
            answer_str = (
                " " + self.nanostories[i]["seed"][querried_variable]
            )  ##we add the leading space
            answer_tokens = torch.tensor(self.tokenizer([answer_str])["input_ids"])[
                0
            ]  # possibly multiple tokens

            self.answer_tokens.append(int(answer_tokens[0]))
            self.answer_first_token_texts.append(
                self.tokenizer.decode([answer_tokens[0]])
            )
            self.answer_texts.append(answer_str)

            qa_prompt_text = gen_question_prompt(
                self.nanostories[i]["story"], self.questions[i]
            )
            self.prompts_text.append(qa_prompt_text)

            self.word_idx["END"].append(
                len(torch.tensor(self.tokenizer([qa_prompt_text])["input_ids"])[0]) - 1
            )
        self.prompts_tok = torch.tensor(
            self.tokenizer(self.prompts_text, padding=True)["input_ids"]
        )

    def __len__(self):
        return self.nb_samples

    def permute_querried_variable(self, permutation: Dict[str, str]):
        """Create a new dataset where the querried variables are permuted according to the permutation dict."""
        child_dataset = deepcopy(self)

        new_questions = []
        for q in self.questions:
            new_q_var = permutation[q["querried_variable"]]
            new_questions.append(
                self.rng.choice(sample_questions(QUESTIONS, [new_q_var]))
            )

        child_dataset.build_from_questions_and_stories(new_questions, self.nanostories)
        return child_dataset

        # 1) the prompts, questions,
        
    def question_from(self, question_dataset: NanoQADataset, name: Optional[str] = None) -> NanoQADataset:
        """Create a new dataset where the questions are taken from the question_dataset and the stories form the current one."""
        child_dataset = deepcopy(self)
        child_dataset.build_from_questions_and_stories(question_dataset.questions, self.nanostories)
        if name is not None:
            child_dataset.name = name
        else:
            child_dataset.name = self.name + "_question_from_" +question_dataset.name
        return child_dataset


TokDataset = Float[torch.Tensor, "batch seq"]


def get_nano_qa_features_dict(
    nano_qa_dataset: NanoQADataset,
) -> Dict[str, List[str]]:
    querried_variable = objects_to_strings(
        [
            nano_qa_dataset.questions[i]["querried_variable"]
            for i in range(len(nano_qa_dataset))
        ]
    )

    questions = [
        nano_qa_dataset.questions[i]["question"] for i in range(len(nano_qa_dataset))
    ]
    last_word = [
        nano_qa_dataset.questions[i]["answer_prefix"].split(" ")[-1]
        for i in range(len(nano_qa_dataset))
    ]
    answer_first_token = objects_to_strings(nano_qa_dataset.answer_first_token_texts)

    nb_token_answer = objects_to_strings(
        [
            len(
                torch.tensor(
                    nano_qa_dataset.tokenizer([nano_qa_dataset.answer_texts[i]])[
                        "input_ids"
                    ]
                )[0]
            )
            for i in range(len(nano_qa_dataset))
        ]
    )

    feature_dict = {
        "querried_variable": querried_variable,
        "answer_first_token": answer_first_token,
        "nb_token_answer": nb_token_answer,
        "questions": questions,
        "last_word": last_word,
    }

    for narr_variable in nano_qa_dataset.nanostories[0][
        "seed"
    ].keys():  # add every variable in the seed to the feature dict
        values = [
            nano_qa_dataset.nanostories[i]["seed"][narr_variable]
            for i in range(len(nano_qa_dataset))
        ]

        if narr_variable == "master_story":
            values, _ = objects_to_unique_ids(values)

        feature_dict[narr_variable] = objects_to_strings(values)
    return feature_dict


def evaluate_model(
    model: HookedTransformer,
    nano_qa_dataset: NanoQADataset,
    logits: Optional[torch.Tensor] = None,
    end_position: Optional[WildPosition] = None,
    batch_size=10,
    all=False,
    print=False,
):
    if logits is None:
        logits = torch.zeros(
            (nano_qa_dataset.nb_samples, model.cfg.n_ctx, model.cfg.d_vocab_out)
        )

        for i in tqdm(range(0, nano_qa_dataset.nb_samples, batch_size)):
            batch = nano_qa_dataset.prompts_text[i : i + batch_size]
            batch_logits = model(batch, prepend_bos=False)
            logits[i : i + len(batch), : batch_logits.shape[1]] = model(
                batch, prepend_bos=False
            )
    if end_position is None:
        end_position = WildPosition(position=nano_qa_dataset.word_idx["END"], label="END")

    end_logits = logits[
        torch.arange(nano_qa_dataset.nb_samples), end_position.position
    ]

    end_probs = torch.nn.functional.softmax(end_logits, dim=-1).cpu()

    questions_types = list(QUERRIED_NARRATIVE_VARIABLES_PRETTY_NAMES.keys())
    perf_per_variable = {questions_types[i]: {} for i in range(len(questions_types))}

    for question_type in perf_per_variable:
        perf_per_variable[question_type]["prob"] = []
        perf_per_variable[question_type]["rank"] = []
        perf_per_variable[question_type]["top1"] = []

    for i in range(nano_qa_dataset.nb_samples):
        question_type = nano_qa_dataset.questions[i]["querried_variable"]
        perf_per_variable[question_type]["prob"].append(
            end_probs[i][nano_qa_dataset.answer_tokens[i]].item()
        )

        rank = (
            torch.where(
                torch.argsort(end_probs[i], descending=True)
                == nano_qa_dataset.answer_tokens[i]
            )[0][0]
            + 1
        )

        top1 = int(
            torch.argsort(end_probs[i], descending=True)[0]
            == nano_qa_dataset.answer_tokens[i]
        )

        perf_per_variable[question_type]["rank"].append(rank)

        perf_per_variable[question_type]["top1"].append(top1)

    summary_perf_per_variable = {}
    for question_type in perf_per_variable:
        for metric in perf_per_variable[question_type]:
            summary_perf_per_variable[question_type + "_" + metric + "_mean"] = np.mean(
                perf_per_variable[question_type][metric]
            )
            summary_perf_per_variable[question_type + "_" + metric + "_std"] = np.std(
                perf_per_variable[question_type][metric]
            )

            summary_perf_per_variable[
                question_type + "_" + metric + "_all"
            ] = perf_per_variable[question_type][metric]

    return summary_perf_per_variable


def compute_random_guess(nano_qa_dataset: NanoQADataset):
    random_guess = {}
    for q in nano_qa_dataset.querried_variables:
        nb_values = len(set([nano_qa_dataset.answer_first_token_texts[i] for i in range(len(nano_qa_dataset)) if nano_qa_dataset.questions[i]["querried_variable"] == q]))
        random_guess[f"{q}_top1_random_guess"] = 1 / nb_values
        random_guess[f"{q}_prob_random_guess"] = 1 / nb_values
        random_guess[f"{q}_rank_random_guess"] = nb_values/2
    return random_guess


# %%
if __name__ == "__main__":
    from transformers import AutoTokenizer

    model = HookedTransformer.from_pretrained("gpt2-small", device="cuda")

    nano_qa_dataset = NanoQADataset(nb_samples=100, tokenizer=model.tokenizer, seed=43)

    f_dict = get_nano_qa_features_dict(nano_qa_dataset)

    t1 = time.time()
    d = evaluate_model(model, nano_qa_dataset, batch_size=5)  # type: ignore
    print_performance_table(d)
    t2 = time.time()
    print(t2 - t1)
    # %%

    # %%

    # %%
    model_name = "EleutherAI/Pythia-2.8b"  # "EleutherAI/gpt-neo-2.7B"  #

    model = HookedTransformer.from_pretrained("EleutherAI/Pythia-2.8b", device="cuda")

    # %% manual inspection
    for i in range(10):
        print("===========")
        print(wrap_str(nano_qa_dataset.prompts_text[i]))
        print()

        transformer_lens.utils.test_prompt(
            prompt=nano_qa_dataset.prompts_text[i],
            answer=nano_qa_dataset.answer_first_token_texts[i],
            model=model,
            prepend_space_to_answer=False,
            print_details=True,
            prepend_bos=False,
            top_k=10,
        )

    # %%
    evaluate_model(model, nano_qa_dataset, batch_size=100)
    # %%
