"""The scaled dot-product attention mechanism defined in Vaswani et al. (2017).

The attention energies are computed as dot products between the query vector
and the key vector. The query vector is scaled down by the square root of its
dimensionality. This attention function has no trainable parameters.

See arxiv.org/abs/1706.03762
"""
import math
from typing import Tuple, List, NamedTuple

import tensorflow as tf
import numpy as np
from typeguard import check_argument_types

from neuralmonkey.nn.utils import dropout
from neuralmonkey.attention.base_attention import (
    BaseAttention, Attendable, get_attention_states, get_attention_mask)

# pylint: disable=invalid-name
MultiHeadLoopStateTA = NamedTuple("MultiHeadLoopStateTA",
                                  [("contexts", tf.TensorArray),
                                   ("head_weights", List[tf.TensorArray])])
# pylint: enable=invalid-name


def split_for_heads(x: tf.Tensor, n_heads: int, head_dim: int) -> tf.Tensor:
    """Split last dimension of 3D vector of shape (batch, time, dim) and return
    a 4D vector with shape (batch, n_heads, time, dim/n_heads)"""
    x_shape = tf.shape(x)
    x_4d = tf.reshape(tf.expand_dims(x, 2),
                      [x_shape[0], x_shape[1], n_heads, head_dim])

    return tf.transpose(x_4d, perm=[0, 2, 1, 3])


def mask_weights(weights_4d: tf.Tensor, mask: tf.Tensor) -> tf.Tensor:
    """Apply mask to softmax weights and renormalize"""
    # attention mask shape: batch, time(k)
    # weights_all shape: batch, head, time(q), time(k)
    weights_all = weights_4d * tf.expand_dims(tf.expand_dims(mask, 1), 1)

    # normalization along time(k)
    # norm shape: batch, head, time(q), 1
    norm = tf.reduce_sum(weights_all, 3, keep_dims=True) + 1e-8
    return weights_all / norm


def mask_future(energies: tf.Tensor) -> tf.Tensor:
    """Mask energies of keys to the right of the query"""
    triangular_mask = tf.matrix_band_part(tf.ones_like(energies), -1, 0)
    mask_area = tf.equal(triangular_mask, 1)
    masked_value = tf.fill(tf.shape(energies), -np.inf)
    return tf.where(mask_area, energies, masked_value)


class MultiHeadAttention(BaseAttention):

    def __init__(self,
                 name: str,
                 n_heads: int,
                 keys_encoder: Attendable,
                 values_encoder: Attendable = None,
                 dropout_keep_prob: float = 1.0,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        check_argument_types()
        BaseAttention.__init__(self, name, save_checkpoint, load_checkpoint)

        self.n_heads = n_heads
        self.dropout_keep_prob = dropout_keep_prob

        if self.n_heads <= 0:
            raise ValueError("Number of heads must be greater than zero.")

        if self.dropout_keep_prob <= 0.0 or self.dropout_keep_prob > 1.0:
            raise ValueError("Dropout keep prob must be inside (0,1].")

        if values_encoder is None:
            values_encoder = keys_encoder

        self.attention_keys = get_attention_states(keys_encoder)
        self.attention_values = get_attention_states(values_encoder)
        self.attention_mask = get_attention_mask(keys_encoder)

        self._dimension = self.attention_keys.get_shape()[-1].value

        if self._dimension % self.n_heads != 0:
            raise ValueError("Model dimension ({}) must be divisible by the "
                             "number of attention heads ({})"
                             .format(self._dimension, self.n_heads))

        self._head_dim = int(self._dimension / self.n_heads)
        self._scaling_factor = 1 / math.sqrt(self._head_dim)

    def attention(self,
                  query: tf.Tensor,
                  decoder_prev_state: tf.Tensor,
                  decoder_input: tf.Tensor,
                  loop_state: MultiHeadLoopStateTA,
                  step: tf.Tensor) -> Tuple[tf.Tensor, MultiHeadLoopStateTA]:

        # transform (batch, query_size) to (batch, 1, query_size)
        # context is (batch, 1, value_size)
        # weights is (batch, head, 1, time(keys))
        context_3d, weights_4d = self.attention_4d(tf.expand_dims(query, 1))

        # head_weights_3d is HEAD-wise list of (batch, 1, 1, time(keys))
        head_weights_3d = tf.split(weights_4d, self.n_heads, axis=1)

        context = tf.squeeze(context_3d, axis=1)
        head_weights = [tf.squeeze(w, axis=[1, 2]) for w in head_weights_3d]

        next_contexts = loop_state.contexts.write(step, context)
        next_head_weights = [loop_state.head_weights[i].write(step,
                                                              head_weights[i])
                             for i in range(self.n_heads)]

        next_loop_state = MultiHeadLoopStateTA(
            contexts=next_contexts,
            head_weights=next_head_weights)

        return context, next_loop_state

    def attention_4d(self, query_3d: tf.Tensor,
                     masked: bool = False) -> tf.Tensor:
        if self.n_heads > 1:
            # Linearly project queries, keys and vals, then split
            # query_proj of shape batch, time(q), self._dimension (=q_channels)
            query_proj = tf.layers.dense(
                query_3d, self._dimension, name="query_proj")
            keys_proj = tf.layers.dense(
                self.attention_keys, self._dimension, name="keys_proj")
            vals_proj = tf.layers.dense(
                self.attention_values, self._dimension, name="vals_proj")

        else:
            query_proj = query_3d
            keys_proj = self.attention_keys
            vals_proj = self.attention_values

        # Shapes:
        # query:  batch, time(q), k_channels
        # keys:   batch, time(k), k_channels
        # values: batch, time(k), v_channels
        # Outputs:
        # context: batch, time(q), v_channels
        # weights: batch, time(q), time(k)

        # Scale first:
        query_scaled = query_proj * self._scaling_factor

        # Reshape the k_channels dimension to the number of heads
        query = split_for_heads(query_scaled, self.n_heads, self._head_dim)
        keys = split_for_heads(keys_proj, self.n_heads, self._head_dim)
        values = split_for_heads(vals_proj, self.n_heads, self._head_dim)

        # For dot-product, we use matrix multiplication
        # shape: batch, head, time(q), time(k) (k_channels is the matmul axis)
        energies = tf.matmul(query, keys, transpose_b=True)

        # To protect the attention from looking ahead of time, we must
        # replace the energies of future keys with negative infinity
        # We use lower triangular matrix and basic tf where tricks
        if masked:
            energies = mask_future(energies)

        # Softmax along the last axis
        # shape: batch, head, time(q), time(k)
        weights_4d = tf.nn.softmax(energies)

        if self.attention_mask is not None:
            weights_4d = mask_weights(weights_4d, self.attention_mask)

        # apply dropout to the weights (Attention Dropout)
        weights_4d = dropout(
            weights_4d, self.dropout_keep_prob, self.train_mode)

        # 1. expand weights_4d to shape batch, head, time(q), time(k), 1
        # 2. expand values to shape batch, head, 1, time(k), head_dim
        # 3. element-wise multiplication broadcasts that to
        #    shape: batch, head, time(q), time(k), head_dim
        # 4. sum along the time(k) axis
        context_4d = tf.reduce_sum(
            tf.expand_dims(weights_4d, 4) * tf.expand_dims(values, 2), 3)

        # transpose and reshape to shape [batch, time(q), v_channels]
        context_shape = tf.shape(context_4d)
        context_3d = tf.reshape(
            tf.transpose(context_4d, perm=[0, 2, 1, 3]),
            [context_shape[0], context_shape[2], self._dimension])

        context_3d = tf.layers.dense(
            context_3d, self._dimension, name="output_proj")

        return context_3d, weights_4d

    def initial_loop_state(self) -> MultiHeadLoopStateTA:
        return MultiHeadLoopStateTA(
            contexts=tf.TensorArray(
                dtype=tf.float32, size=0, dynamic_size=True,
                name="contexts"),
            head_weights=[tf.TensorArray(
                dtype=tf.float32, size=0, dynamic_size=True,
                name="distributions_head{}".format(i), clear_after_read=False)
                          for i in range(self.n_heads)])

    def finalize_loop(self, key: str,
                      last_loop_state: MultiHeadLoopStateTA) -> None:
        for i in range(self.n_heads):
            head_weights = last_loop_state.head_weights[i].stack()
            self.histories["{}_head{}".format(key, i)] = head_weights

    @property
    def context_vector_size(self) -> int:
        return self.attention_values.get_shape()[-1].value

    def visualize_attention(self, key: str) -> None:
        for i in range(self.n_heads):
            head_key = "{}_head{}".format(key, i)
            if head_key not in self.histories:
                raise ValueError(
                    "Key {} not among attention histories".format(head_key))

            alignments = tf.expand_dims(
                tf.transpose(self.histories[head_key], perm=[1, 2, 0]), -1)

            tf.summary.image("{}_head{}".format(self.name, i), alignments,
                             collections=["summary_att_plots"],
                             max_outputs=256)


class ScaledDotProdAttention(MultiHeadAttention):

    def __init__(self,
                 name: str,
                 keys_encoder: Attendable,
                 values_encoder: Attendable = None,
                 dropout_keep_prob: float = 1.0,
                 save_checkpoint: str = None,
                 load_checkpoint: str = None) -> None:
        check_argument_types()
        MultiHeadAttention.__init__(
            self, name, 1, keys_encoder, values_encoder, dropout_keep_prob,
            save_checkpoint, load_checkpoint)
