import copy
import torch
import pytest

from allennlp.common import Params
from allennlp.common import cached_transformers
from allennlp.common.testing import assert_equal_parameters, AllenNlpTestCase

# from allennlp.modules.transformer.t5 import T5Attention
from allennlp.modules.transformer.general_attention import T5Attention

from transformers.models.t5.configuration_t5 import T5Config
from transformers.models.t5.modeling_t5 import T5Attention as HFT5Attention

PARAMS_DICT = {
    "hidden_size": 6,
    "num_heads": 2,
    "key_value_proj_dim": 3,
    "dropout": 0.0,
    "relative_attention_num_buckets": 2,
}


class TestT5Attention(AllenNlpTestCase):
    def test_can_construct_from_params(self):

        params_dict = {key: val for key, val in PARAMS_DICT.items()}

        params = Params(copy.deepcopy(params_dict))

        t5_attention = T5Attention.from_params(params)

        assert t5_attention.num_attention_heads == params_dict["num_heads"]
        assert t5_attention.attention_head_size == params_dict["key_value_proj_dim"]

        assert (
            t5_attention.all_head_size
            == params_dict["num_heads"] * params_dict["key_value_proj_dim"]
        )

        assert t5_attention.query.in_features == params_dict["hidden_size"]
        assert t5_attention.key.in_features == params_dict["hidden_size"]
        assert t5_attention.value.in_features == params_dict["hidden_size"]
        assert t5_attention.output.in_features == params_dict["hidden_size"]

        assert t5_attention.dropout == params_dict["dropout"]

    def test_forward_against_huggingface_output(self):
        hidden_states = torch.randn(2, 3, 6)
        attention_mask = torch.tensor([[0, 1, 0], [1, 1, 0]])

        hf_kwargs = {
            "d_model": PARAMS_DICT["hidden_size"],
            "d_kv": PARAMS_DICT["key_value_proj_dim"],
            "num_heads": PARAMS_DICT["num_heads"],
            "relative_attention_num_buckets": PARAMS_DICT["relative_attention_num_buckets"],
            "dropout_rate": PARAMS_DICT["dropout"],
        }

        torch.manual_seed(1234)
        hf_module = HFT5Attention(T5Config(**hf_kwargs), has_relative_attention_bias=False)

        torch.manual_seed(1234)

        params = copy.deepcopy(PARAMS_DICT)
        params["normalize"] = False  # only for this test.
        t5_attention = T5Attention(**params)

        # setting to eval mode to avoid non-deterministic dropout.
        t5_attention = t5_attention.eval()
        hf_module = hf_module.eval()

        output = t5_attention.forward(hidden_states, mask=attention_mask)
        attention_mask_hf = (attention_mask == 0).view((2, 1, 1, 3)).expand(2, 2, 3, 3) * -10e5
        hf_output = hf_module.forward(hidden_states, mask=attention_mask_hf)

        hs = output.hidden_states

        assert torch.allclose(hs, hf_output[0])

    @pytest.mark.parametrize(
        "pretrained_name",
        [
            "t5-small",
        ],
    )
    def test_loading_from_pretrained_weights_using_model_name(self, pretrained_name):

        torch.manual_seed(1234)
        pretrained = cached_transformers.get(pretrained_name, False)

        pretrained_module = pretrained.encoder.block[0].layer[0].SelfAttention

        module = T5Attention.from_pretrained_module(pretrained_name)

        mapping = {
            val: key
            for key, val in module._construct_default_mapping(
                pretrained_module, "huggingface", {}
            ).items()
        }
        assert_equal_parameters(pretrained_module, module, mapping=mapping)