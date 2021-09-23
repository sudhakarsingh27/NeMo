# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GPT-2 model."""

import torch

from apex import mpu

from nemo.collections.nlp.modules.common.megatron.enums import AttnMaskType
from nemo.collections.nlp.modules.common.megatron.language_model import get_language_model, parallel_lm_logits
from nemo.collections.nlp.modules.common.megatron.module import MegatronModule
from nemo.collections.nlp.modules.common.megatron.utils import init_method_normal, scaled_init_method_normal


def post_language_model_processing(
    lm_output,
    labels,
    logit_weights,
    get_key_value,
    parallel_output,
    forward_method_parallel_output,
    fp16_lm_cross_entropy,
):
    if get_key_value:
        lm_output, presents = lm_output

    # Output.
    if forward_method_parallel_output is not None:
        parallel_output = forward_method_parallel_output
    output = parallel_lm_logits(lm_output, logit_weights, parallel_output)

    if get_key_value:
        output = [output, presents]

    if labels is None:
        return output
    else:
        if fp16_lm_cross_entropy:
            assert output.dtype == torch.half
            loss = mpu.vocab_parallel_cross_entropy(output, labels)
        else:
            loss = mpu.vocab_parallel_cross_entropy(output.float(), labels)
        return loss


class GPTModel(MegatronModule):
    """GPT-2 Language model."""

    def __init__(
        self,
        vocab_size,
        hidden_size,
        max_position_embeddings,
        num_layers,
        num_tokentypes=0,
        parallel_output=True,
        pre_process=True,
        post_process=True,
        init_method_std=0.02,
        fp16_lm_cross_entropy=False,
        use_cpu_initialization=False,
        hidden_dropout=0.1,
        bf16=False,
        fp32_residual_connection=False,
        activations_checkpoint_method=None,
        activations_checkpoint_num_layers=1,
        layernorm_epsilon=1e-5,
    ):
        super(GPTModel, self).__init__()

        self.parallel_output = parallel_output
        self.pre_process = pre_process
        self.post_process = post_process
        self.fp16_lm_cross_entropy = fp16_lm_cross_entropy

        self.language_model, self._language_model_key = get_language_model(
            vocab_size=vocab_size,
            num_tokentypes=num_tokentypes,
            max_position_embeddings=max_position_embeddings,
            add_pooler=False,
            encoder_attn_mask_type=AttnMaskType.causal,
            init_method=init_method_normal(init_method_std),
            scaled_init_method=scaled_init_method_normal(init_method_std, num_layers),
            pre_process=self.pre_process,
            post_process=self.post_process,
            init_method_std=init_method_std,
            num_layers=num_layers,
            use_cpu_initialization=use_cpu_initialization,
            hidden_size=hidden_size,
            hidden_dropout=hidden_dropout,
            bf16=bf16,
            fp32_residual_connection=fp32_residual_connection,
            activations_checkpoint_method=activations_checkpoint_method,
            activations_checkpoint_num_layers=activations_checkpoint_num_layers,
            layernorm_epsilon=layernorm_epsilon,
        )

        self.initialize_word_embeddings(init_method_normal)

    def set_input_tensor(self, input_tensor):
        """See megatron.model.transformer.set_input_tensor()"""
        self.language_model.set_input_tensor(input_tensor)

    def forward(
        self,
        input_ids,
        position_ids,
        attention_mask,
        labels=None,
        tokentype_ids=None,
        layer_past=None,
        get_key_value=False,
        forward_method_parallel_output=None,
    ):

        lm_output = self.language_model(
            input_ids, position_ids, attention_mask, layer_past=layer_past, get_key_value=get_key_value
        )

        if self.post_process:
            return post_language_model_processing(
                lm_output,
                labels,
                self.word_embeddings_weight(),
                get_key_value,
                self.parallel_output,
                forward_method_parallel_output,
                self.fp16_lm_cross_entropy,
            )
        else:
            return lm_output

    def state_dict_for_save_checkpoint(self, destination=None, prefix='', keep_vars=False):

        state_dict_ = {}
        state_dict_[self._language_model_key] = self.language_model.state_dict_for_save_checkpoint(
            destination, prefix, keep_vars
        )
        # Save word_embeddings.
        if self.post_process and not self.pre_process:
            state_dict_[self._word_embeddings_for_head_key] = self.word_embeddings.state_dict(
                destination, prefix, keep_vars
            )
        return state_dict_

    def load_state_dict(self, state_dict, strict=True):
        """Customized load."""

        # Load word_embeddings.
        if self.post_process and not self.pre_process:
            self.word_embeddings.load_state_dict(state_dict[self._word_embeddings_for_head_key], strict=strict)
        if self._language_model_key in state_dict:
            state_dict = state_dict[self._language_model_key]
        self.language_model.load_state_dict(state_dict, strict=strict)