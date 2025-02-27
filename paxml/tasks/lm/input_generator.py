# coding=utf-8
# Copyright 2022 The Pax Authors.
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

"""Language model input generator."""

from __future__ import annotations

import ast
import dataclasses
from typing import Any, List, Optional, Union

from absl import logging
import jax
from lingvo.core import base_input_generator
from lingvo.core import layers as lingvo_layers
from lingvo.core import ops
from praxis import base_input
from praxis import pax_fiddle
from praxis import py_utils
from praxis import pytypes
import tensorflow.compat.v2 as tf

NestedMap = py_utils.NestedMap


def make_masked_ml_data_augmenter(**kwargs):
  p = lingvo_layers.MaskedLmDataAugmenter.Params()
  for k, v in kwargs.items():
    setattr(p, k, v)
  return p


class TFRecordBertInput(base_input.BaseInput):
  """Input generator reading TFRecords of ids for MLPerf eval.

  Attributes:
    input_file: String, path of an input file.
    max_sequence_length: Maximum number of tokens to be present in a single
      example.
    max_predictions_per_seq: Maximum number of tokens that can be masked per
      example.
    eos_token_id: id for EOS token.
    eval_data_size: The number of examples in the eval data. Set to 0 for
      unknown.
    file_buffer_size: How many records are buffered for random shuffling.
    enable_packing: Whether to pack multiple documents on the same row.
    prepacking_batch_size: Only used when p.enable_packing is set. Batch size
      before packing. Note that this does not affect post-packing batch size but
      may have a minor effect on how tight the packed output is.
    remask: Whether to re-apply the masking on-the-fly. Should only be used on
      the training data.
    mlm_augmenter: params for masking. Only used when p.remask=True.
    num_samples: For accounting purposes only.
  """

  # https://github.com/mlcommons/training/tree/master/language_model/tensorflow/bert#tfrecord-features
  input_file: Optional[Union[str, List[str]]] = None
  max_sequence_length: int = 512
  max_predictions_per_seq: int = 76
  eos_token_id: int = 102
  eval_data_size: int = 10000
  file_buffer_size: int = 10000
  enable_packing: bool = False
  prepacking_batch_size: int = 1 << 14
  remask: bool = False
  # Note that this is a TF class with lingvo-style params.
  mlm_augmenter: py_utils.InstantiableParams = pax_fiddle.instance_field(
      lambda **kwargs: make_masked_ml_data_augmenter(**kwargs)  # pylint: disable=unnecessary-lambda
  )
  num_samples: int = -1
  mlm: Any = dataclasses.field(init=False, repr=False)
  _dataset: Any = dataclasses.field(init=False, repr=False)
  _iterator: Any = dataclasses.field(init=False, repr=False)

  def __post_init__(self):
    if not self.is_training:
      self.reset_for_eval = True
      self.enable_packing = False
      self.remask = False
    if isinstance(self.input_file, str):
      self.input_file = [self.input_file]
    super().__post_init__()

    if self.remask:
      mlm_p = self.mlm_augmenter.Copy()
      mlm_p.name = 'mlm_augmenter'
      mlm_p.dtype = tf.float32
      mlm_p.fprop_dtype = tf.float32
      logging.info('mlm_p=%s', mlm_p.ToText())
      self.mlm = mlm_p.Instantiate()

    self._dataset = self._gen_dataset()
    self._iterator = iter(self._dataset)

  def get_next(self) -> NestedMap:
    """Returns a batch with .labels, .masked_ids, and .masked_pos."""
    ret = self._iterator.get_next()
    return jax.tree_util.tree_map(lambda x: x.numpy(), ret)

  def reset(self) -> None:
    self._iterator = iter(self._dataset)

  def _parse_record(self, record) -> NestedMap:
    """Reads and parses a single record."""
    name_to_features = {
        'input_ids': tf.io.FixedLenFeature(
            [self.max_sequence_length], tf.int64
        ),
        'input_mask': tf.io.FixedLenFeature(
            [self.max_sequence_length], tf.int64
        ),
        'masked_lm_positions': tf.io.FixedLenFeature(
            [self.max_predictions_per_seq], tf.int64
        ),
        'masked_lm_ids': tf.io.FixedLenFeature(
            [self.max_predictions_per_seq], tf.int64
        ),
        'masked_lm_weights': tf.io.FixedLenFeature(
            [self.max_predictions_per_seq], tf.float32
        ),
    }
    example = tf.io.parse_single_example(record, name_to_features)
    mask_length = tf.cast(
        tf.reduce_sum(example['masked_lm_weights']), dtype=tf.int32)
    masked_lm_positions = tf.slice(example['masked_lm_positions'], [0],
                                   [mask_length])
    masked_lm_ids = tf.cast(
        tf.slice(example['masked_lm_ids'], [0], [mask_length]), dtype=tf.int32)
    ret = py_utils.NestedMap()
    ret.masked_ids = tf.cast(example['input_ids'], dtype=tf.int32)
    # Get back non-masked, original ids.
    ret.labels = tf.tensor_scatter_nd_update(
        tensor=ret.masked_ids,
        indices=tf.reshape(masked_lm_positions, [-1, 1]),
        updates=masked_lm_ids)
    ret.masked_pos = tf.tensor_scatter_nd_update(
        tensor=tf.zeros_like(ret.masked_ids, dtype=tf.float32),
        indices=tf.reshape(masked_lm_positions, [-1, 1]),
        updates=tf.ones_like(masked_lm_ids, dtype=tf.float32))
    ret.segment_ids = tf.cast(example['input_mask'], dtype=tf.float32)

    first_eos_idx = tf.where(tf.math.equal(ret.labels, self.eos_token_id))[0][0]

    def remove_first_eos(x):
      # We remove the element at position `first_eos_idx`, and pad with 0
      # to keep length unchanged.
      zero = tf.constant(0, shape=(1,), dtype=x.dtype)
      return tf.concat([x[:first_eos_idx], x[first_eos_idx + 1:], zero], axis=0)

    ret = ret.Transform(remove_first_eos)
    ret.paddings = 1.0 - ret.segment_ids
    pos = tf.cast(tf.range(self.max_sequence_length), dtype=tf.float32)
    ret.segment_pos = tf.cast(ret.segment_ids * pos, dtype=tf.int32)

    if self.remask:
      new_masked_ids, new_masked_pos = self.mlm.FProp(None, ret.labels,
                                                      ret.paddings)
      ret.masked_ids = new_masked_ids
      ret.masked_pos = new_masked_pos
    return ret

  def _all_paddings_batch(self) -> NestedMap:
    shape = [self.batch_size, self.max_sequence_length]
    ret = py_utils.NestedMap()
    ret.labels = tf.zeros(shape, dtype=tf.int32)
    ret.masked_ids = ret.labels
    ret.segment_pos = ret.labels
    ret.masked_pos = tf.zeros(shape, dtype=tf.float32)
    ret.segment_ids = ret.masked_pos
    ret.paddings = 1.0 - ret.segment_ids
    return ret

  def _pad_to_even_length(self, dataset: tf.data.Dataset) -> tf.data.Dataset:
    n = self.num_infeed_hosts
    if n <= 1:
      return dataset
    # pad with all paddings batch so that the total number of elements in
    # `dataset` can be evenly divided by n.
    if self.eval_data_size < 1:
      # dataset.cardinality() returns unknown, so we first materialize all
      # data.
      total_batches = len(list(dataset.as_numpy_iterator()))
    else:
      total_batches = (
          self.eval_data_size + self.batch_size - 1
      ) // self.batch_size
    if total_batches % n == 0:
      return dataset
    per_host_batches = (total_batches + n - 1) // n
    num_pad_batches = per_host_batches * n - total_batches
    pad_batches = tf.data.Dataset.from_tensors(
        self._all_paddings_batch()).repeat(num_pad_batches)
    return dataset.concatenate(pad_batches)

  def _pad_to_batch_size(self, batch: NestedMap) -> NestedMap:

    def pad(key, t):
      constant_v = 0
      if t.dtype.is_floating and key.endswith('.paddings'):
        constant_v = 1.0
      need = self.batch_size - (t.shape[0] or tf.shape(t)[0])
      padded = tf.pad(t, [[0, need], [0, 0]], 'CONSTANT', constant_v)
      return padded

    return batch.TransformWithKey(pad)

  def _ensure_shape(self, batch: NestedMap) -> NestedMap:
    p = self.hparams

    def ensure(x):
      x = tf.ensure_shape(x, [p.batch_size, p.max_sequence_length])
      return x

    return batch.Transform(ensure)

  def _gen_dataset(self) -> tf.data.Dataset:
    file_patterns = list(
        map(py_utils.sharded_file_pattern_to_glob, self.input_file)
    )
    files = tf.data.Dataset.list_files(file_patterns, shuffle=False)
    if self.is_training:
      # For training data, each host will only use a non-overlapping subset
      # of the training files.
      # This logic is specific to the mlperf training data, which has exactly
      # 1024 shards. Other implementations might opt to shard after reading
      # all input files, in which case one must not shuffle before sharding.
      num_files = len(list(files.as_numpy_iterator()))
      if num_files % self.num_infeed_hosts != 0:
        raise ValueError(
            'Input files sharding not supported: we require the number of files'
            f' {num_files} to evenly divide num_infeed_hosts='
            f'{self.num_infeed_hosts} so we can shard at file level.'
        )
      files = files.shard(
          num_shards=self.num_infeed_hosts, index=self.infeed_host_index
      )
      logging.info('Reading input from files: %s',
                   b', '.join(list(files.as_numpy_iterator())))

    shuffle = self.is_training
    dataset = files.interleave(
        tf.data.TFRecordDataset,
        cycle_length=tf.data.AUTOTUNE if shuffle else 1,
        num_parallel_calls=tf.data.AUTOTUNE if shuffle else 1)
    if shuffle:
      dataset = dataset.shuffle(
          self.file_buffer_size, seed=self.input_random_seed
      )
    dataset = dataset.repeat(-1 if shuffle else 1)
    dataset = dataset.map(self._parse_record)

    if self.enable_packing:
      dataset = (
          dataset.batch(
              self.prepacking_batch_size,
              drop_remainder=True,
              num_parallel_calls=tf.data.AUTOTUNE,
          )
          .map(self._pack, num_parallel_calls=tf.data.AUTOTUNE)
          .unbatch()
          .shuffle(self.file_buffer_size, seed=self.input_random_seed)
      )

    dataset = dataset.batch(batch_size=self.batch_size, drop_remainder=shuffle)
    if not shuffle:
      dataset = dataset.map(self._pad_to_batch_size)

    if not self.is_training:
      # For the eval data, each infeed host will only see a non-overlapping
      # shard of the data, since eval data is always read sequentially.
      # We need to ensure that all hosts see an equal number of batches.
      dataset = self._pad_to_even_length(dataset)
      dataset = dataset.shard(
          num_shards=self.num_infeed_hosts, index=self.infeed_host_index
      )

    dataset = dataset.map(self._ensure_shape)
    dataset = dataset.prefetch(tf.data.AUTOTUNE)
    return dataset

  def _pack(self, batch_in: NestedMap) -> NestedMap:
    """Packs a given batch, which changes the batch size."""

    actual_seq_len = tf.math.reduce_sum(
        tf.cast(batch_in.segment_ids, tf.int32), axis=1)
    (segment_ids, segment_pos, indices_in_input, _, _, _) = ops.pack_sequences(
        actual_seq_len,
        actual_seq_len,
        packed_batch_size=0,
        packed_src_seq_len=self.max_sequence_length,
        packed_tgt_seq_len=self.max_sequence_length,
    )

    def apply_packing(x):
      return ops.apply_packing(x, 0, segment_ids, indices_in_input)

    batch_out = batch_in.DeepCopy()
    batch_out = batch_out.Transform(apply_packing)
    batch_out.paddings = ops.apply_packing(batch_in.paddings, 1, segment_ids,
                                           indices_in_input)
    batch_out.segment_ids = tf.cast(segment_ids, tf.float32)
    batch_out.segment_pos = segment_pos

    return batch_out


class SyntheticLmData(base_input_generator.BaseInputGenerator):
  """Generated synthetic data with packed_input lm formats."""

  @classmethod
  def HParams(cls) -> py_utils.InstantiableParams:
    """Defaults params for input generators."""
    p = super().Params()
    p.Define('seq_len', 0, 'Number of tokens in one example')
    return p

  def _InputBatch(self):
    p = self.params
    targets = tf.ones([p.batch_size, p.seq_len], dtype=tf.int32)
    input_batch = py_utils.NestedMap()
    input_batch.ids = targets  # equivalent to tf.roll(targets, 1, axis=1)
    input_batch.paddings = tf.zeros_like(targets)
    input_batch.weights = tf.ones_like(targets)
    input_batch.labels = targets
    # segment_id = 0 meant padded tokens
    # e.g., if we have two segments packed into one sentence with paddings
    # segment_ids = 1, 1, 1, 1, 2, 2, 2, 2, 0, 0
    # segment_pos = 0, 1, 2, 3, 0, 1, 2, 3, 0, 0
    input_batch.segment_ids = targets
    input_batch.segment_pos = tf.tile(
        tf.range(0, p.seq_len)[tf.newaxis, :], [p.batch_size, 1])
    return input_batch


class TextInput(base_input.BaseInput):
  """Input generator reading plain text used for eval.

  Each row in the batch corresponds to a line in the input file. This input
  raises out of range after all input data are returned at least once. Depends
  on the number of infeed hosts and batch size, duplicate input is returned
  to pad to full, synchronized batches on all infeed hosts.

  Attributes:
    input_file: String, path of a (small) input file.
    tokenizer: Lingvo tokenizer param.
    max_sequence_length: Maximum number of tokens to be present in a single
      example.
    num_samples: Number of items contained in the input. 0 for dynamically
      determined (slower).
    bytes_repr: Whether the texts are written as bytes representation, e.g. b'Q:
      Who directed?\n\nA:'
  """
  input_file: Optional[str] = None
  tokenizer: Optional[py_utils.InstantiableParams] = None
  max_sequence_length: int = 512
  num_samples: int = 0
  bytes_repr: bool = True
  tokenizer_inst: Any = dataclasses.field(init=False, repr=False)
  _actual_num_samples: Any = dataclasses.field(init=False, repr=False)
  _dataset: Any = dataclasses.field(init=False, repr=False)
  _iterator: Any = dataclasses.field(init=False, repr=False)

  def __post_init__(self):
    super().__post_init__()
    self.tokenizer_inst = self.tokenizer.Instantiate()
    self._actual_num_samples = None
    self._dataset = self._gen_dataset()
    self._iterator = iter(self._dataset)

  def get_next(self) -> NestedMap:
    """Returns a batch with .ids, .paddings, and .labels."""
    ret = self._iterator.get_next()
    return jax.tree_util.tree_map(lambda x: x.numpy(), ret)

  def reset(self) -> None:
    self._iterator = iter(self._dataset)

  @property
  def computed_num_samples(self):
    """Number of samples contained in the dataset."""
    if self._actual_num_samples is not None:
      return self._actual_num_samples
    if self.num_samples > 0:
      self._actual_num_samples = self.num_samples
    else:
      lines = tf.data.TextLineDataset(self.input_file)
      self._actual_num_samples = len(list(lines.as_numpy_iterator()))
    return self._actual_num_samples

  def _num_to_truncate(self):
    """Smallest multiple of global batch size that covers the entire data."""
    n = self.num_infeed_hosts * self.batch_size
    num_global_batches = (self.computed_num_samples + n - 1) // n
    return num_global_batches * n

  def ids_to_strings(self, ids: pytypes.NpTensor,
                     lengths: pytypes.NpTensor) -> List[str]:
    bytes_list = self.tokenizer_inst.IdsToStrings(ids, lengths).numpy()
    return [b.decode('utf-8') for b in bytes_list]

  def _to_nested_map(self, text) -> py_utils.NestedMap:
    ids, labels, paddings = self.tokenizer_inst.StringsToIds(
        text, max_length=self.max_sequence_length
    )
    # Unfortunately some tokenizers don't return the correct paddings.
    # We recompute it by looking at when the labels sequence terminates.
    indices = tf.where(tf.math.equal(labels, self.tokenizer_inst.eos_id))
    lengths = tf.math.segment_min(indices[:, 1], indices[:, 0]) + 1
    new_paddings = tf.cast(
        1.0
        - tf.sequence_mask(
            lengths, maxlen=self.max_sequence_length, dtype=paddings.dtype
        ),
        dtype=paddings.dtype,
    )
    weights = 1. - new_paddings
    return py_utils.NestedMap(
        ids=ids, labels=labels, paddings=new_paddings, weights=weights)

  def _remove_bytes_repr(self, ds):

    def eval_bytes(s):
      return ast.literal_eval(s.numpy().decode())

    def tf_eval_bytes(x):
      x_shape = x.shape
      y = tf.py_function(eval_bytes, [x], tf.string)
      y.set_shape(x_shape)
      return y

    return ds.map(tf_eval_bytes)

  def _gen_dataset(self) -> tf.data.Dataset:
    lines = tf.data.TextLineDataset(self.input_file)
    if self.bytes_repr:
      lines = self._remove_bytes_repr(lines)
    num_repeat = self._num_to_truncate() // self.computed_num_samples + 1
    lines = lines.repeat(num_repeat).take(self._num_to_truncate())
    lines = lines.shard(
        num_shards=self.num_infeed_hosts, index=self.infeed_host_index
    )
    lines = lines.batch(self.batch_size)
    return lines.map(self._to_nested_map)
