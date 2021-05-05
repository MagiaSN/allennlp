import math
from typing import Optional, Dict, Union, Tuple
from dataclasses import dataclass
import torch
import torch.nn.functional as F

from allennlp.common import FromParams
from allennlp.modules.attention import Attention
from allennlp.modules.transformer.transformer_module import TransformerModule
from allennlp.modules.transformer.util import apply_mask

# Unfortunately mypy is insane, so we have to wrap these in unions.
FloatT = Union[torch.FloatTensor]
IntT = Union[torch.IntTensor]
BoolT = Union[torch.BoolTensor]


@dataclass
class KeyValueState:
    key_state: FloatT
    value_state: FloatT


@dataclass
class GeneralSelfAttentionOutput:
    """
    Encapsulates the outputs of the `GeneralSelfAttention` module.
    """

    hidden_states: FloatT
    key_value_state: Optional[Tuple[FloatT, FloatT]] = None
    position_bias: Optional[FloatT] = None
    attention_probs: Optional[FloatT] = None


class GeneralSelfAttention(TransformerModule, FromParams):
    """
    TODO
    """

    def __init__(
        self,
        hidden_size: int = 512,
        attention_head_size: int = 64,
        num_attention_heads: int = 8,
        scoring_func: str = "scaled_dot_product",
        output_linear: bool = False,
        dropout: float = 0.0,
        bias: bool = True,
        normalize_weights: bool = False,
        is_decoder: bool = False,
        is_cross_attention: bool = False,
        has_relative_attention_bias: bool = False,
        relative_attention_num_buckets: int = 32,
    ):

        super().__init__()

        if hidden_size % num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (hidden_size, num_attention_heads)
            )

        if is_cross_attention:
            assert is_decoder, "The attention layer can be a cross-attention layer only "
            "if it is within a decoder."

        self.hidden_size = hidden_size
        self.num_attention_heads = num_attention_heads
        self.attention_head_size = attention_head_size
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = torch.nn.Linear(hidden_size, self.all_head_size, bias=bias)
        self.key = torch.nn.Linear(hidden_size, self.all_head_size, bias=bias)
        self.value = torch.nn.Linear(hidden_size, self.all_head_size, bias=bias)

        if output_linear:
            self.output = torch.nn.Linear(hidden_size, self.all_head_size, bias=bias)

        self.scoring_func = scoring_func
        if self.scoring_func in ["additive", "linear", "bilinear"]:
            self.attn = Attention.by_name(self.scoring_func)(hidden_size, hidden_size)
        elif self.scoring_func == "scaled_dot_product":
            self.attn = Attention.by_name(self.scoring_func)(self.attention_head_size, False)
        else:
            self.attn = Attention.by_name(self.scoring_func)()

        self.has_relative_attention_bias = has_relative_attention_bias
        self.relative_attention_num_buckets = relative_attention_num_buckets

        if self.has_relative_attention_bias:
            self.relative_attention_bias = torch.nn.Embedding(
                self.relative_attention_num_buckets, self.num_attention_heads
            )

        self.dropout = dropout

        self.is_decoder = is_decoder
        self.is_cross_attention = is_cross_attention

        if normalize_weights:
            self._normalize()

    def _normalize(self):
        self.query.weight.data.normal_(
            mean=0.0, std=(self.hidden_size * self.attention_head_size) ** -0.5
        )
        self.key.weight.data.normal_(mean=0.0, std=self.hidden_size ** -0.5)
        self.value.weight.data.normal_(mean=0.0, std=self.hidden_size ** -0.5)

        if hasattr(self, "output"):
            self.output.weight.data.normal_(
                mean=0.0, std=(self.num_attention_heads * self.attention_head_size) ** -0.5
            )

        if hasattr(self, "has_relative_attention_bias") and self.has_relative_attention_bias:
            self.relative_attention_bias.weight.data.normal_(mean=0.0, std=self.hidden_size ** -0.5)

    def _transpose_for_scores(self, x: torch.Tensor):
        new_x_shape = x.size()[:-1] + (
            self.num_attention_heads,
            self.attention_head_size,
        )
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def _query_layer(self, query_states: torch.Tensor):
        mixed_query_layer = self.query(query_states)
        query_layer = self._transpose_for_scores(mixed_query_layer)
        return query_layer

    def _project(
        self,
        query_states: torch.Tensor,
        layer: torch.nn.Linear,
        source_states: Optional[torch.Tensor] = None,
        past_key_or_value_states: Optional[torch.Tensor] = None,
    ):
        if self.is_decoder:
            if self.is_cross_attention:
                if past_key_or_value_states is None:
                    assert source_states is not None, "Encoder final state needs to be passed."
                    query_states = source_states
                else:
                    return past_key_or_value_states

        layer_output = layer(query_states)
        layer_output = self._transpose_for_scores(layer_output)
        if self.is_decoder:
            layer_output = torch.cat([past_key_or_value_states, layer_output], dim=2)

        return layer_output

    def _position_bias(
        self,
        position_bias,
        seq_lengths,
        past_key_states,
        attention_scores,
    ):
        seq_length, real_seq_length, key_length = seq_lengths

        if position_bias is None:
            if self.has_relative_attention_bias:
                position_bias = self.compute_bias(real_seq_length, key_length)
            else:
                position_bias = torch.zeros(
                    (1, self.num_attention_heads, real_seq_length, key_length),
                    device=attention_scores.device,
                    dtype=attention_scores.dtype,
                )

            # if key and values are already calculated
            # we want only the last query position bias
            if past_key_states is not None:
                position_bias = position_bias[:, :, -seq_length:, :]
        return position_bias

    def _get_attention_probs(
        self,
        query_layer: torch.Tensor,
        key_layer: torch.Tensor,
        attention_mask: torch.Tensor,
        head_mask: torch.Tensor,
        position_bias: Optional[torch.Tensor] = None,
        seq_lengths: Optional[Tuple[int, int, int]] = None,
        past_key_states: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        attention_scores = self.attn(query_layer, key_layer.transpose(-1, -2))

        # return attention_scores

        position_bias = self._position_bias(
            position_bias, seq_lengths, past_key_states, attention_scores
        )

        if position_bias is not None:
            if attention_mask is not None:
                # Shape: (batch_size, num_heads, seq_length, key_length)
                position_bias = apply_mask(position_bias, attention_mask)
            attention_scores += position_bias
        else:
            if attention_mask is not None:
                attention_scores = apply_mask(attention_scores, attention_mask)

        attention_probs = torch.nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = F.dropout(attention_probs, p=self.dropout, training=self.training)

        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        return attention_probs, position_bias

    def _output_layer(self, attention_probs: torch.Tensor, value_layer: torch.Tensor):
        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        if hasattr(self, "output"):
            context_layer = self.output(context_layer)

        return context_layer

    def _get_lengths(self, query_states, past_key_states, source_states):

        seq_length = query_states.shape[1]
        effective_seq_len = seq_length

        key_length = seq_length

        if past_key_states is not None:
            # TODO: query_length from up the stack: move logic here.
            # TODO: clarify the logic here.
            effective_seq_len += past_key_states.shape[2]
            if self.is_cross_attention:
                key_length = source_states.shape[1]

        return (seq_length, effective_seq_len, key_length)

    def forward(
        self,
        query_states: torch.Tensor,
        past_key_states: Optional[torch.Tensor] = None,
        past_value_states: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.BoolTensor] = None,
        source_states: Optional[torch.Tensor] = None,
        source_attention_mask: Optional[torch.BoolTensor] = None,
        head_mask: Optional[torch.Tensor] = None,
        position_bias: Optional[torch.Tensor] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
    ):
        """
        query_states : `torch.Tensor`
            Shape `batch_size x seq_len x hidden_dim`
        past_key_states : `torch.Tensor`, optional
            Shape `batch_size x seq_len x hidden_dim`
            These are the key_states from the previous step of the decoder.
        past_value_states : `torch.Tensor`, optional
            Shape `batch_size x seq_len x hidden_dim`
            These are the value_states from the previous step of the decoder.
        attention_mask : `torch.BoolTensor`, optional
            Shape `batch_size x seq_len`
        source_states : `torch.Tensor`, optional
            Shape `batch_size x source_seq_len x hidden_dim`
            This is from the final state of attention over the source (encoder);
            it is passed when this module is being used for cross-attention.
        source_attention_mask : `torch.BoolTensor`, optional
            Shape `batch_size x source_seq_len`
        head_mask : `torch.BoolTensor`, optional
        position_bias : `torch.Tensor`, optional
        output_attentions : `bool`
            Whether to also return the attention probabilities, default = `False`

        !!! Note
            `source_states` needs to be passed in case of cross-attention.

        """
        query_layer = self._query_layer(query_states)
        key_layer = self._project(
            query_states,
            self.key,
            source_states,
            past_key_states,
        )

        value_layer = self._project(
            query_states,
            self.value,
            source_states,
            past_value_states,
        )

        if self.is_cross_attention:
            attention_mask = source_attention_mask

        seq_lengths = self._get_lengths(query_states, past_key_states, source_states)

        attention_probs, position_bias = self._get_attention_probs(
            query_layer,
            key_layer,
            attention_mask,
            head_mask,
            position_bias,
            seq_lengths,
            past_key_states,
        )

        context_layer = self._output_layer(attention_probs, value_layer)

        present_key_value_state = (
            (key_layer, value_layer) if (self.is_decoder and use_cache) else None
        )
        outputs = GeneralSelfAttentionOutput(
            context_layer, present_key_value_state, position_bias, attention_probs
        )

        return outputs

    @staticmethod
    def _relative_position_bucket(
        relative_position: IntT,
        bidirectional: bool = True,
        num_buckets: int = 32,
        max_distance: int = 128,
    ) -> IntT:
        """
        Adapted from Mesh Tensorflow:
        https://github.com/tensorflow/mesh/blob/0cb87fe07da627bf0b7e60475d59f95ed6b5be3d/mesh_tensorflow/transformer/transformer_layers.py#L593

        Translate relative position to a bucket number for relative attention. The relative position is defined as
        memory_position - query_position, i.e. the distance in tokens from the attending position to the
        attended-to position. If bidirectional=False, then positive relative positions are invalid. We use smaller
        buckets for small absolute relative_position and larger buckets for larger absolute relative_positions. All
        relative positions >=max_distance map to the same bucket. All relative positions <=-max_distance map to the
        same bucket. This should allow for more graceful generalization to longer sequences than the model has been
        trained on.

        Args:
            relative_position: an int32 Tensor
            bidirectional: a boolean - whether the attention is bidirectional
            num_buckets: an integer
            max_distance: an integer

        Returns:
            a Tensor with the same shape as relative_position, containing int32 values in the range
            [0, num_buckets)
        """
        relative_buckets = relative_position.new_zeros(relative_position.shape)
        if bidirectional:
            num_buckets //= 2
            relative_buckets += (relative_position > 0).to(torch.long) * num_buckets
            relative_position = torch.abs(relative_position)
        else:
            relative_position = -torch.min(relative_position, torch.zeros_like(relative_position))
        # now relative_position is in the range [0, inf)

        # half of the buckets are for exact increments in positions
        max_exact = num_buckets // 2
        is_small = relative_position < max_exact

        # The other half of the buckets are for logarithmically bigger bins in positions up to max_distance
        relative_postion_if_large = max_exact + (
            torch.log(relative_position.float() / max_exact)
            / math.log(max_distance / max_exact)
            * (num_buckets - max_exact)
        ).to(torch.long)
        relative_postion_if_large = torch.min(
            relative_postion_if_large, torch.full_like(relative_postion_if_large, num_buckets - 1)
        )

        relative_buckets += torch.where(is_small, relative_position, relative_postion_if_large)
        return relative_buckets

    def compute_bias(self, query_length: int, key_length: int) -> FloatT:
        """ Compute binned relative position bias """
        context_position = torch.arange(query_length, dtype=torch.long)[:, None]
        memory_position = torch.arange(key_length, dtype=torch.long)[None, :]
        relative_position = memory_position - context_position  # shape (query_length, key_length)
        relative_position_bucket = self._relative_position_bucket(
            relative_position,  # shape (query_length, key_length)
            bidirectional=(not self.is_decoder),
            num_buckets=self.relative_attention_num_buckets,
        )
        relative_position_bucket = relative_position_bucket.to(
            self.relative_attention_bias.weight.device
        )
        values = self.relative_attention_bias(
            relative_position_bucket
        )  # shape (query_length, key_length, num_heads)
        values = values.permute([2, 0, 1]).unsqueeze(
            0
        )  # shape (1, num_heads, query_length, key_length)
        return values


class T5Attention(GeneralSelfAttention):

    # _relevant_module = ["encoder.block.0.layer.0.self_attention"]
    _huggingface_mapping = {
        "q": "query",
        "k": "key",
        "v": "value",
        "o": "output",
        "layers": "layer",
    }

    def __init__(
        self,
        is_decoder: bool = False,
        hidden_size: int = 512,
        key_value_proj_dim: int = 64,
        num_heads: int = 8,
        has_relative_attention_bias: bool = False,
        relative_attention_num_buckets: int = 32,
        dropout: float = 0.1,
        normalize: bool = True,
        is_cross_attention: bool = False,
    ):

        super().__init__(
            hidden_size=hidden_size,
            attention_head_size=key_value_proj_dim,
            num_attention_heads=num_heads,
            output_linear=True,
            scoring_func="scaled_dot_product",
            dropout=dropout,
            bias=False,
            normalize_weights=normalize,
            is_decoder=is_decoder,
            is_cross_attention=is_cross_attention,
            has_relative_attention_bias=has_relative_attention_bias,
            relative_attention_num_buckets=relative_attention_num_buckets,
        )

        self.attn = Attention.by_name(self.scoring_func)(1, False)

    def forward(  # type: ignore
        self,
        hidden_states: torch.Tensor,
        mask: Optional[torch.BoolTensor] = None,
        key_value_states: Optional[FloatT] = None,
        position_bias: Optional[FloatT] = None,
        past_key_value: Optional[
            Tuple[FloatT, FloatT]
        ] = None,  # this is used when taking decoding steps.
        layer_head_mask: Optional[BoolT] = None,
        query_length: Optional[int] = None,  # only relevant in cross-attention.
        use_cache: bool = False,
        output_attentions: bool = False,
    ) -> GeneralSelfAttentionOutput:
        """
        Self-attention (if key_value_states is None) or attention over source sentence (provided by
        key_value_states).
        """
        if past_key_value:
            past_key_states = past_key_value[0]
            past_value_states = past_key_value[1]
        else:
            past_key_states = None
            past_value_states = None

        outputs = super().forward(
            query_states=hidden_states,
            past_key_states=past_key_states,
            past_value_states=past_value_states,
            attention_mask=mask,
            source_states=key_value_states,
            source_attention_mask=None,  # TODO: is this a bug in current T5 code?
            head_mask=layer_head_mask,
            position_bias=position_bias,
            output_attentions=output_attentions,
        )

        return outputs


class SelfAttention(GeneralSelfAttention):
    """
    This module computes the self-attention, similar to the architecture in BERT. Additionally, the attention
    scoring function can be specified.
    Details in the paper:
    [BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding, Devlin et al, 2019]
    (https://api.semanticscholar.org/CorpusID:52967399)

    # Parameters

    hidden_size: `int`
    num_attention_heads: `int`
    dropout: `float` (default = `0.0`)
    scoring_func: `str` (default = `scaled_dot_product`)
        The name of the attention-calculating function to be used.
        Eg. `additive`, `linear`, etc. For a complete list, please check :mod:`allennlp.modules.attention`.
    """

    _relevant_module = ["encoder.layers.0.attention.self", "encoder.layers.0.attention"]
    _huggingface_mapping = {"layer": "layers"}

    def __init__(
        self,
        hidden_size: int,
        num_attention_heads: int,
        dropout: float = 0.0,
        scoring_func: str = "scaled_dot_product",
        output_linear: bool = False,
    ):

        attention_head_size = int(hidden_size / num_attention_heads)

        super().__init__(
            hidden_size=hidden_size,
            attention_head_size=attention_head_size,
            num_attention_heads=num_attention_heads,
            scoring_func=scoring_func,
            output_linear=output_linear,
            dropout=dropout,
            bias=True,
        )

    def forward(self, *args, **kwargs):
        outputs = super().forward(*args, **kwargs)
        if outputs.attention_probs is not None:
            return (outputs.hidden_states, outputs.attention_probs)
        return (outputs.hidden_states,)

    @classmethod
    def _get_mapping(
        cls, pretrained_module=None, source="huggingface", mapping: Optional[Dict[str, str]] = None
    ):
        combined_mapping = {}
        if "huggingface" in source:
            combined_mapping.update(cls._huggingface_mapping)
        if mapping is not None:
            combined_mapping.update(mapping)
        if pretrained_module is not None:
            for name, _ in pretrained_module.named_modules():
                if "q_lin" in name:
                    combined_mapping["q_lin"] = "query"
                    combined_mapping["k_lin"] = "key"
                    combined_mapping["v_lin"] = "value"
                    combined_mapping["out_lin"] = "output"
                    combined_mapping["transformer"] = "encoder"
                    break
        return combined_mapping

    @classmethod
    def _get_input_arguments(
        cls,
        pretrained_module: torch.nn.Module,
        source="huggingface",
        mapping: Optional[Dict[str, str]] = None,
        **kwargs,
    ):
        submodules = cls._get_mapped_submodules(pretrained_module, source, mapping)
        final_kwargs = {}

        final_kwargs["hidden_size"] = submodules["query"].in_features
        if hasattr(submodules[""], "num_attention_heads"):
            final_kwargs["num_attention_heads"] = submodules[""].num_attention_heads
        elif hasattr(submodules[""], "n_heads"):
            final_kwargs["num_attention_heads"] = submodules[""].n_heads
            final_kwargs["output_linear"] = True  # Since this is the distilbert case.
        else:
            raise AttributeError("Cannot find a relevant attribute for number of heads.")

        final_kwargs["dropout"] = submodules["dropout"].p

        final_kwargs.update(**kwargs)

        return final_kwargs
