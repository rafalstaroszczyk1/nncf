# Copyright (c) 2023 Intel Corporation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import openvino as ov
from datasets import load_dataset
from memory_profiler import memory_usage
from optimum.intel.openvino import OVModelForCausalLM
from transformers import AutoTokenizer
from whowhatbench import Evaluator

import nncf
from tests.post_training.pipelines.base import BackendType
from tests.post_training.pipelines.base import BaseTestPipeline
from tests.post_training.pipelines.base import StatsFromOutput
from tests.shared.paths import TEST_ROOT


@dataclass
class WCTimeStats(StatsFromOutput):
    """
    Contains statistics that are parsed from the stdout of Weight Compression tests.
    """

    time_stat_collection: Optional[str] = None
    time_mixed_precision: Optional[str] = None
    time_awq: Optional[str] = None
    time_apply_compression: Optional[str] = None

    STAT_NAMES = ["Stat. collection time", "Mixed-Precision search time", "AWQ time", "Apply Compression time"]
    VAR_NAMES = ["time_stat_collection", "time_mixed_precision", "time_awq", "time_apply_compression"]
    REGEX_PREFIX = [
        "Statistics collection",
        "Mixed-Precision assignment",
        "Applying AWQ",
        "Applying Weight Compression",
    ]

    def fill(self, stdout: str) -> None:
        time_regex = r".*•\s(.*)\s•.*"
        for line in stdout.splitlines():
            for attr_name, prefix_regex in zip(self.VAR_NAMES, self.REGEX_PREFIX):
                match = re.search(r"{}{}".format(prefix_regex, time_regex), line)
                if match:
                    setattr(self, attr_name, match.group(1))
                continue

    def get_stats(self) -> Dict[str, str]:
        VARS = [getattr(self, name) for name in self.VAR_NAMES]
        return dict(zip(self.STAT_NAMES, VARS))


class LMWeightCompression(BaseTestPipeline):
    """Pipeline for casual language models from Hugging Face repository"""

    OV_MODEL_NAME = "openvino_model.xml"

    def prepare_model(self) -> None:
        if not (self.fp32_model_dir / self.OV_MODEL_NAME).exists():
            # export by model_id
            self.model_hf = OVModelForCausalLM.from_pretrained(
                self.model_id, export=True, load_in_8bit=False, compile=False, stateful=False
            )
            self._dump_model_fp32()
        else:
            # no export, load from IR. Applicable for sequential run of test cases in local environment.
            self.model_hf = OVModelForCausalLM.from_pretrained(
                self.fp32_model_dir, trust_remote_code=True, load_in_8bit=False, compile=False, stateful=False
            )
        self.model = self.model_hf.model

    def prepare_preprocessor(self) -> None:
        self.preprocessor = AutoTokenizer.from_pretrained(self.model_id)

    def get_transform_calibration_fn(self):
        def transform_fn(data):
            tokenized_text = self.preprocessor(data["text"], return_tensors="np")
            input_ids = tokenized_text["input_ids"]
            attention_mask = tokenized_text["attention_mask"]

            inputs = {}
            inputs["input_ids"] = input_ids
            inputs["attention_mask"] = tokenized_text["attention_mask"]
            position_ids = np.cumsum(attention_mask, axis=1) - 1
            position_ids[attention_mask == 0] = 1

            # The magic forms KV cache as model inputs
            batch_size = input_ids.shape[0]
            for input_name in self.model_hf.key_value_input_names:
                model_inputs = self.model.input(input_name)
                shape = model_inputs.get_partial_shape()
                shape[0] = batch_size
                if shape[2].is_dynamic:
                    shape[2] = 0
                else:
                    shape[1] = 0
                inputs[input_name] = ov.Tensor(model_inputs.get_element_type(), shape.get_shape())

            inputs["position_ids"] = position_ids
            return inputs

        return transform_fn

    def prepare_calibration_dataset(self):
        dataset = load_dataset("wikitext", "wikitext-2-v1", split="train", revision="b08601e")
        dataset = dataset.filter(lambda example: len(example["text"]) > 80)
        self.calibration_dataset = nncf.Dataset(dataset, self.get_transform_calibration_fn())

    def cleanup_cache(self):
        dir_with_cache = "model_cache"
        dirs_to_remove = [self.output_model_dir / dir_with_cache, self.fp32_model_dir / dir_with_cache]
        for dir_to_remove in dirs_to_remove:
            if dir_to_remove.exists():
                shutil.rmtree(dir_to_remove)

    def compress(self) -> None:
        if self.backend == BackendType.FP32:
            return

        print("Weight compression...")
        start_time = time.perf_counter()
        self.run_info.compression_memory_usage = memory_usage(self._compress, max_usage=True)
        self.run_info.time_compression = time.perf_counter() - start_time

    def collect_data_from_stdout(self, stdout: str):
        stats = WCTimeStats()
        stats.fill(stdout)
        self.run_info.stats_from_output = stats

    def save_compressed_model(self) -> None:
        if self.backend == BackendType.FP32:
            return
        ov.serialize(self.model, self.output_model_dir / self.OV_MODEL_NAME)
        self.model_hf._save_config(self.output_model_dir)

    def get_num_compressed(self) -> None:
        pass

    def run_bench(self) -> None:
        pass

    def _dump_model_fp32(self) -> None:
        """
        Dump IRs of fp32 models, to help debugging. The test cases may share the same fp32 model, therefore it is saved
        to the dedicated shared folder.
        """
        self.model_hf.save_pretrained(self.fp32_model_dir)
        self.model_hf._save_config(self.fp32_model_dir)

    def _compress(self):
        """
        Actual call of weight compression
        """
        self.compressed_model = nncf.compress_weights(
            self.model,
            dataset=self.calibration_dataset,
            **self.compression_params,
        )

    def _validate(self):
        core = ov.Core()

        if os.environ.get("CPU_THREADS_NUM"):
            # Set CPU_THREADS_NUM for OpenVINO inference
            cpu_threads_num = os.environ.get("CPU_THREADS_NUM")
            core.set_property("CPU", properties={"CPU_THREADS_NUM": str(cpu_threads_num)})

        gt_data_path = TEST_ROOT / "post_training" / "data" / "wwb_ref_answers" / self.fp32_model_name / "ref_qa.csv"
        gt_data_path.parent.mkdir(parents=True, exist_ok=True)
        if os.getenv("NNCF_TEST_REGEN_DOT") is not None:
            print("Collection ground-truth reference data")
            model_gold = OVModelForCausalLM.from_pretrained(
                self.fp32_model_dir, trust_remote_code=True, load_in_8bit=False, compile=False, stateful=False
            )
            evaluator = Evaluator(base_model=model_gold, tokenizer=self.preprocessor, metrics=("similarity",))
            evaluator.dump_gt(str(gt_data_path))
            print("Saving ground-truth validation data:", gt_data_path.resolve())
        else:
            print("Loading existing ground-truth validation data:", gt_data_path.resolve())
            evaluator = Evaluator(
                tokenizer=self.preprocessor, gt_data=gt_data_path, test_data=str(gt_data_path), metrics=("similarity",)
            )

        compressed_model_hf = self.model_hf
        if self.backend != BackendType.FP32:
            compressed_model_hf = OVModelForCausalLM.from_pretrained(
                self.output_model_dir, trust_remote_code=True, load_in_8bit=False, compile=False, stateful=False
            )
        print("Evaluation of the target model")
        _, all_metrics = evaluator.score(compressed_model_hf)
        similarity = all_metrics["similarity"][0]
        self.run_info.metric_name = "Similarity"
        self.run_info.metric_value = round(similarity, 5)
