"""Multi-Slice Language Model configurations on the T5/C4 dataset."""

from absl import logging
import jax
from jax import numpy as jnp
from paxml import base_experiment
from paxml import experiment_registry
from paxml import tasks_lib
from paxml.tasks.lm import model_params
from paxml.tasks.lm.params import c4
from paxml.tasks.lm.params import lm_cloud
from praxis import layers
from praxis import pax_fiddle


@experiment_registry.register
class C4Spmd22BAdamMaxText(c4.C4SpmdAdam):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 1 * 4 * 2 * 16 = 128
  """

  NUM_LAYERS = 48
  MODEL_DIMS = 6144
  HIDDEN_DIMS = MODEL_DIMS * 4
  NUM_HEADS = 24
  DIMS_PER_HEAD = 256
  PERCORE_BATCH_SIZE = 16
  MAX_SEQ_LEN = 1024
  VOCAB_SIZE = 32768

  FPROP_DTYPE = jnp.bfloat16
  USE_REPEATED_LAYER = True
  SUMMARY_INTERVAL_STEPS = 10
  CHECKPOINT_POLICY = layers.AutodiffCheckpointType.SAVE_NOTHING


@experiment_registry.register
class C4Spmd22BAdam1xv4_128(C4Spmd22BAdamMaxText):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 1 * 64 * 1 * 16 = 1024
  """

  ICI_MESH_SHAPE = [1, 64, 1]
  PERCORE_BATCH_SIZE = 16
  CHECKPOINT_POLICY = layers.AutodiffCheckpointType.SAVE_NOTHING


@experiment_registry.register
class C4Spmd22BAdam2xv4_128(C4Spmd22BAdam1xv4_128):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 2 * 1 * 64 * 1 * 16 = 2048
  """

  DCN_MESH_SHAPE = [2, 1, 1]


@experiment_registry.register
class C4Spmd22BAdam4xv4_128(C4Spmd22BAdam1xv4_128):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 4 * 1 * 64 * 1 * 16 = 4096
  """

  DCN_MESH_SHAPE = [4, 1, 1]


@experiment_registry.register
class C4Spmd22BAdam1xv4_128LimitSteps(C4Spmd22BAdam1xv4_128):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 1 * 64 * 1 * 16 = 1024
  """

  def task(self) -> pax_fiddle.Config[tasks_lib.SingleTask]:
    task_p = super().task()
    task_p.train.num_train_steps = 300
    return task_p


@experiment_registry.register
class C4Spmd22BAdam2xv4_128LimitSteps(C4Spmd22BAdam2xv4_128):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 2 * 1 * 64 * 1 * 16 = 2048
  """

  def task(self) -> pax_fiddle.Config[tasks_lib.SingleTask]:
    task_p = super().task()
    task_p.train.num_train_steps = 300
    return task_p


@experiment_registry.register
class C4Spmd22BAdam4xv4_128LimitSteps(C4Spmd22BAdam4xv4_128):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 4 * 1 * 64 * 1 * 16 = 4096
  """

  def task(self) -> pax_fiddle.Config[tasks_lib.SingleTask]:
    task_p = super().task()
    task_p.train.num_train_steps = 300
    return task_p


@experiment_registry.register
class C4Spmd22BAdam1xv4_384(C4Spmd22BAdamMaxText):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 1 * 192 * 1 * 10 = 1920
  """

  NUM_LAYERS = 24
  MODEL_DIMS = 9216
  HIDDEN_DIMS = MODEL_DIMS * 4
  NUM_HEADS = 24
  DIMS_PER_HEAD = 384
  PERCORE_BATCH_SIZE = 10
  MAX_SEQ_LEN = 1024
  VOCAB_SIZE = 32768

  FPROP_DTYPE = jnp.bfloat16
  USE_REPEATED_LAYER = True
  SUMMARY_INTERVAL_STEPS = 10
  CHECKPOINT_POLICY = layers.AutodiffCheckpointType.SAVE_NOTHING

  ICI_MESH_SHAPE = [1, 192, 1]


@experiment_registry.register
class C4Spmd22BAdam2xv4_384(C4Spmd22BAdam1xv4_384):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 2 * 1 * 192 * 1 * 10 = 3840
  """

  DCN_MESH_SHAPE = [2, 1, 1]
  CHECKPOINT_POLICY = layers.AutodiffCheckpointType.SAVE_NOTHING


@experiment_registry.register
class C4Spmd22BAdam1xv4_8(C4Spmd22BAdamMaxText):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 1 * 4 * 1 * 16 = 64
  """

  NUM_LAYERS = 16
  MODEL_DIMS = 2048
  HIDDEN_DIMS = MODEL_DIMS * 4
  NUM_HEADS = 8
  DIMS_PER_HEAD = 256
  PERCORE_BATCH_SIZE = 16
  MAX_SEQ_LEN = 1024
  VOCAB_SIZE = 32768

  ICI_MESH_SHAPE = [1, 4, 1]
  PERCORE_BATCH_SIZE = 16
  CHECKPOINT_POLICY = layers.AutodiffCheckpointType.SAVE_NOTHING


@experiment_registry.register
class C4Spmd22BAdam2xv4_8(C4Spmd22BAdam1xv4_8):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 2 * 1 * 4 * 1 * 16 = 128
  """

  DCN_MESH_SHAPE = [2, 1, 1]


@experiment_registry.register
class C4Spmd22BAdam4xv4_8(C4Spmd22BAdam1xv4_8):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 4 * 1 * 4 * 1 * 16 = 256
  """

  DCN_MESH_SHAPE = [4, 1, 1]


@experiment_registry.register
class C4Spmd22BAdam1xv4_8LimitSteps(C4Spmd22BAdam1xv4_8):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 2 * 1 * 4 * 1 * 16 = 128
  """

  def task(self) -> pax_fiddle.Config[tasks_lib.SingleTask]:
    task_p = super().task()
    task_p.train.num_train_steps = 300
    return task_p


@experiment_registry.register
class C4Spmd22BAdam2xv4_8LimitSteps(C4Spmd22BAdam2xv4_8):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 2 * 1 * 4 * 1 * 16 = 128
  """

  def task(self) -> pax_fiddle.Config[tasks_lib.SingleTask]:
    task_p = super().task()
    task_p.train.num_train_steps = 300
    return task_p


@experiment_registry.register
class C4Spmd22BAdam4xv4_8LimitSteps(C4Spmd22BAdam4xv4_8):
  """GPT-3 config with 22B params.

  Model Parameters: Global batch size = 4 * 1 * 4 * 1 * 16 = 256
  """

  def task(self) -> pax_fiddle.Config[tasks_lib.SingleTask]:
    task_p = super().task()
    task_p.train.num_train_steps = 300
    return task_p
