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

"""The basic program concept that encapsulates a per-step runnable."""
import abc
import collections
import contextlib
import dataclasses
import queue
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import jax
from jax.experimental import multihost_utils
from jax import monitoring
import numpy as np

from absl import flags
from absl import logging
from etils import epath
from paxml import io_utils
from paxml import metric_utils
from paxml import partitioning
from paxml import tasks_lib
from paxml import trainer_lib
from paxml import train_states
from paxml import seqio_input
from paxml import summary_utils
from praxis import base_hyperparams
from praxis import base_input
from praxis import base_layer
from praxis import pytypes
from praxis import py_utils
import tensorflow.compat.v2 as tf

from paxml import profiling  # mapped to internal

JTensor = pytypes.JTensor
NestedJTensor = pytypes.NestedJTensor
NestedPartitionSpec = pytypes.NestedPartitionSpec
NestedShapeDtypeLike = pytypes.NestedShapeDtypeLike
PRNGKey = pytypes.PRNGKey
SummaryDict = pytypes.SummaryDict
WeightedScalars = pytypes.WeightedScalars
EvaluationMode = io_utils.EvaluationMode
SummaryWriter = tf.summary.SummaryWriter

NestedMap = py_utils.NestedMap
TrainState = train_states.TrainState
instantiate = base_hyperparams.instantiate

_INIT_TIME = time.time()


def get_eval_train_state(task: tasks_lib.SingleTask, state: TrainState):
  task_p = task.hparams
  if task_p.train.eval_use_ema_states:
    if not tasks_lib.has_ema(task_p):
      raise ValueError(
          'eval_use_ema_states is requested but the '
          'learner does not seem to have ema enabled'
      )
    eval_state = tasks_lib.extract_ema(state).to_eval_state()
    logging.debug('  Converted train state to eval with ema state.')
  else:
    eval_state = state.to_eval_state()
  return eval_state


def _summary_base_dir(job_log_dir: epath.Path) -> epath.Path:
  return job_log_dir / 'summaries'


def _train_log_interval_steps(
    train_p: tasks_lib.SingleTask.TrainHParams,
) -> int:
  """Returns the interval to log train outputs."""
  if train_p.log_train_output_interval_steps is not None:
    return train_p.log_train_output_interval_steps
  else:
    return train_p.summary_interval_steps


@dataclasses.dataclass
class ProgramOutput:
  # The train state that's potentially modified by the program.
  # For example, a train program is expected to update the state to reflect
  # optimizer updates, while a eval program is expected to keep the state as is.
  state: TrainState
  # Auxiliary dictionary that contains any information that program intends to
  # feedback to outer loop.
  aux: NestedMap


class Program(metaclass=abc.ABCMeta):
  """The basic interface for a program."""

  # TODO(laigd): add a unified setup() method here.

  @abc.abstractmethod
  def should_run(self, state: TrainState, train_step: int) -> bool:
    """Whether .run() should be called at `state` and `train_step`."""

  @abc.abstractmethod
  def run(self, state: TrainState, train_step: int) -> ProgramOutput:
    """Returns the program on given state and train step."""

  @abc.abstractmethod
  def shutdown(self) -> None:
    """Runs any necessary cleanup."""


class _InflightQueue:
  """Tracks and limits the number of inflight computations."""

  def __init__(self, max_inflight: int):
    self._inflight_queue = None
    if max_inflight > 0:
      self._inflight_queue = queue.Queue(maxsize=max_inflight)

  def add_computation(self, computation: JTensor):
    """Adds a pending on-device computation."""
    if self._inflight_queue:
      self._inflight_queue.put(computation)

  def wait_for_next(self):
    """If the queue is full, wait for the next computation to finish."""
    if self._inflight_queue and self._inflight_queue.full():
      self._inflight_queue.get().block_until_ready()

  def wait_for_all(self):
    """Wait for all inflight computations to finish."""
    if self._inflight_queue:
      while not self._inflight_queue.empty():
        self._inflight_queue.get().block_until_ready()


class BaseTrainProgram(Program):
  """A lean interface of a basic train program.

  Users should inherit from BaseTrainProgram and implement methods required to
  form a custom train program.

  TODO(hthu): Write a custom program example.
  """

  def __init__(self):
    # States to set in self.setup().
    self._task = None
    self._train_input = None
    self._partitioner = None
    self._train_prng_seed = None
    self._eval_prng_seed = None
    self._initial_step = -1

    # States to initialize lazily in self.setup().
    self._train_unpadded_global_batch_size = None
    self._profiler = None
    self._train_summary_writer = None
    self._train_summary_handler = None
    self._eval_train_summary_handler = None
    self._train_summary_last_time = None
    self._train_summary_last_step = None
    # Used to limit the number of inflight training steps.
    self._pending_train_losses: _InflightQueue = None

    # Other states used during training.
    self._first_step_completion_time = None
    self._init_duration_set = False

    # Used to enter context of various summary writer at .setup().
    self._exitstack = contextlib.ExitStack()

  @property
  def train_input(self) -> base_input.BaseInput:
    assert self._train_input
    return self._train_input

  @property
  def summary_writer(self) -> SummaryWriter:
    assert self._train_summary_writer
    return self._train_summary_writer

  def setup(
      self,
      task: tasks_lib.SingleTask,
      train_input: base_input.BaseInput,
      partitioner: partitioning.Partitioner,
      job_log_dir: epath.Path,
      # TODO(laigd): it should take a root prng key and split it.
      train_prng_seed: pytypes.PRNGKey,
      eval_prng_seed: pytypes.PRNGKey,
      init_step: int,
  ) -> None:
    self._task = task
    self._train_input = train_input
    self._partitioner = partitioner
    self._train_prng_seed = train_prng_seed
    self._eval_prng_seed = eval_prng_seed
    self._initial_step = init_step

    # Creates the train summary writer and handler.
    summary_base_dir = _summary_base_dir(job_log_dir)
    summary_train_dir = summary_base_dir / 'train'
    self._train_summary_writer = self._exitstack.enter_context(
        summary_utils.get_summary_writer(summary_train_dir)
    )
    train_p = self._task.hparams.train
    self._train_summary_handler = summary_utils.SummaryHandler(
        self._train_summary_writer,
        train_p.summary_interval_steps,
        accumulate_interval_steps=train_p.summary_accumulate_interval_steps,
        log_interval_steps=_train_log_interval_steps(train_p),
        is_async=bool(train_p.device_sync_interval_steps),
        name='training',
    )

    # Creates the summary writer and handler for eval on train input.
    if not train_p.eval_skip_train:
      summary_eval_train_dir = summary_base_dir / 'eval_train'
      eval_train_summary_writer = self._exitstack.enter_context(
          summary_utils.get_summary_writer(summary_eval_train_dir)
      )
      self._eval_train_summary_handler = summary_utils.SummaryHandler(
          eval_train_summary_writer,
          train_p.summary_interval_steps,
          accumulate_interval_steps=train_p.summary_accumulate_interval_steps,
          name='eval',
      )

    # Initializes other states.
    self._train_unpadded_global_batch_size = (
        train_input.hparams.cls.get_global_batch_size(train_input.hparams)
    )
    self._profiler = profiling.Profiler(
        num_steps=train_p.profiler_num_steps,
        min_duration_sec=train_p.profiler_min_duration_sec,
        max_num_hosts=train_p.profiler_max_num_hosts,
    )
    self._train_summary_last_time = time.time()
    self._train_summary_last_step = init_step - 1
    self._pending_train_losses = _InflightQueue(train_p.max_inflight_steps)

  def should_run(self, state: TrainState, train_step: int) -> bool:
    return train_step < self._task.hparams.train.num_train_steps

  # TODO(laigd): further split this into smaller modules and add program APIs
  # correspondingly.
  def run(self, state: TrainState, train_step: int) -> ProgramOutput:
    train_p = self._task.hparams.train
    logging.debug('  Retrieving inputs.')
    model_inputs = self._train_input.get_next_padded()
    model_inputs = self._partitioner.preprocess_inputs(
        self._train_input,
        model_inputs,
        self.train_input_partition_spec,
    )
    logging.debug('  Retrieved inputs.')

    # Waits if it reaches max inflight steps. We do this after retrieving the
    # inputs to maximize efficiency.
    self._pending_train_losses.wait_for_next()

    profiler_capture_step = train_p.profiler_capture_step
    do_profile = profiler_capture_step is not None
    if do_profile and train_step - self._initial_step == profiler_capture_step:
      self._profiler.capture_async()

    logging.debug('  Performing train_step().')
    with jax.profiler.StepTraceAnnotation('train', step_num=train_step):
      with py_utils.timeit() as train_period:
        (
            new_state,
            loss,
            weighted_scalars,
            per_example_out,
            summary_tensors,
        ) = self.train_step(
            state,
            self._train_prng_seed,
            model_inputs,
            self._train_unpadded_global_batch_size,
        )
      del state  # Unused anymore.
    logging.debug(
        '  Completed train_step() in %f seconds.', train_period.elapsed
    )
    self._pending_train_losses.add_computation(loss)
    if train_step == self._initial_step:
      self._first_step_completion_time = time.time()

    if do_profile and train_step - self._initial_step < profiler_capture_step:
      self._profiler.update_step_moving_mean(train_period.elapsed)

    new_train_step, steps_per_sec = self._maybe_write_summaries(
        new_state,
        train_step,
        loss,
        weighted_scalars,
        summary_tensors,
        per_example_out,
    )
    logging.debug('  Writing summaries (attempt).')

    # Run eval at regular step interval.
    # While the eval ones below are post-model weight updates, hence we use the
    # new step counter new_train_step.
    eval_train_metrics = None
    if (
        train_p.eval_interval_steps
        and new_train_step % train_p.eval_interval_steps == 0
    ):
      eval_train_metrics = self._maybe_run_eval_train(new_state, new_train_step)

    return ProgramOutput(
        new_state,
        aux=NestedMap(
            loss=loss,
            weighted_scalars=weighted_scalars,
            new_train_step=new_train_step,
            steps_per_sec=steps_per_sec,
            eval_train_metrics=eval_train_metrics,
        ),
    )

  @abc.abstractmethod
  def train_step(
      self,
      state: train_states.TrainState,
      prng_key: PRNGKey,
      inputs: NestedJTensor,
      unpadded_global_batch_size: int,
  ) -> Tuple[TrainState, JTensor, WeightedScalars, NestedMap, SummaryDict]:
    """The train step function."""

  @abc.abstractmethod
  def eval_train_step(
      self,
      state: train_states.TrainState,
      prng_key: PRNGKey,
      inputs: NestedJTensor,
      unpadded_global_batch_size: int,
  ) -> Tuple[JTensor, WeightedScalars, NestedMap, SummaryDict]:
    """The eval step function on training inputs."""

  @property
  @abc.abstractmethod
  def train_input_partition_spec(self) -> Optional[NestedPartitionSpec]:
    """The partition spec for the model training inputs."""

  def _maybe_write_summaries(
      self,
      new_state: TrainState,
      train_step: int,
      loss,
      weighted_scalars,
      summary_tensors,
      per_example_out,
  ):
    new_train_step = train_step + 1
    steps_per_sec = None

    train_p = self._task.hparams.train
    if train_p.device_sync_interval_steps:
      should_sync_device = (
          new_train_step % train_p.device_sync_interval_steps
      ) == 0
    else:
      should_sync_device = self._train_summary_handler.should_write(
          new_train_step
      )
    if should_sync_device:
      new_train_step, steps_per_sec = self._sync_device(new_state, train_step)

    # Note: Train metrics are currently reported at train_step + 1, while these
    # training metrics/summaries are pre-model weight updates.
    # TODO(b/264635784): Update the logic to pass train_step instead.
    self._train_summary_handler.process(
        new_train_step,
        loss,
        weighted_scalars,
        summary_tensors,
        per_example_out=per_example_out,
        steps_per_sec=steps_per_sec,
    )
    logging.debug('  Wrote summaries (attempted).')

    return new_train_step, steps_per_sec

  def _sync_device(self, new_state: TrainState, train_step: int):
    # Synchronize train_step. This is performed at a fixed interval to avoid
    # a gap between steps.
    new_train_step = int(
        py_utils.maybe_unreplicate_for_fully_replicated(new_state.step)
    )
    steps_per_sec = self._compute_steps_per_sec(
        train_step, self._train_summary_last_time, self._train_summary_last_step
    )
    logging.info('steps/sec: %f', steps_per_sec)
    self._train_summary_last_time = time.time()
    self._train_summary_last_step = train_step

    if not self._init_duration_set:
      # Find estimated timestamp before the first execution call.
      # This enables us to include the first step's compile time but exclude
      # its execution time from the init duration.
      estimated_execute_duration = 1 / steps_per_sec
      first_step_execute_time = (
          self._first_step_completion_time - estimated_execute_duration
      )
      init_duration = first_step_execute_time - _INIT_TIME
      monitoring.record_event_duration_secs(
          '/jax/pax/init/time_before_first_step_secs', init_duration
      )
      self._init_duration_set = True
    return new_train_step, steps_per_sec

  def _compute_steps_per_sec(
      self, train_step, summary_last_time, summary_last_step
  ):
    """Computes the number of training steps per second."""
    # Note: This function doesn't account for the time spent on running
    # interleaved evaluation (if any) and/or evaluation on the training batch.
    # It's, hence, merely a raw underestimate.
    duration_sec = time.time() - summary_last_time
    num_steps = train_step - summary_last_step
    steps_per_sec = num_steps / duration_sec
    return steps_per_sec

  def _maybe_run_eval_train(self, new_state: TrainState, new_train_step: int):
    train_p = self._task.hparams.train
    eval_train_metrics = None

    if train_p.eval_skip_train:
      logging.debug('  train_p.eval_skip_train is True. Skipping eval_train.')
    else:
      logging.debug('  Retrieving eval model_inputs.')
      eval_inputs = self._train_input.peek_padded()
      if eval_inputs is None:
        logging.debug('  eval_inputs is None. Skipping eval_train.')
      else:
        logging.debug('  Retrieved eval model_inputs.')
        logging.debug('  Performing eval_step() runs on training split.')
        eval_inputs = self._partitioner.preprocess_inputs(
            self._train_input, eval_inputs, self.train_input_partition_spec
        )

        eval_state = get_eval_train_state(self._task, new_state)
        loss, weighted_scalars, _, summary_tensors = self.eval_train_step(
            eval_state,
            self._eval_prng_seed,
            eval_inputs,
            self._train_unpadded_global_batch_size,
        )
        logging.debug('  Completed eval_step() runs on training split.')
        if self._eval_train_summary_handler.process(
            new_train_step, loss, weighted_scalars, summary_tensors
        ):
          logging.debug('  Wrote eval summaries.')
        eval_train_metrics = metric_utils.as_float_dict(weighted_scalars)
    return eval_train_metrics

  # TODO(laigd): remove this.
  @property
  def train_unpadded_global_batch_size(self) -> int:
    return self._train_unpadded_global_batch_size

  def shutdown(self) -> None:
    self._pending_train_losses.wait_for_all()
    self._train_summary_handler.close()
    if self._eval_train_summary_handler:
      self._eval_train_summary_handler.close()
    self._exitstack.close()


class SingleTaskTrainProgram(BaseTrainProgram):
  """Train program that assumes a single task on a single dataset."""

  def __init__(self):
    super().__init__()

    # Train step function information.
    self._train_step_created = False
    self._train_step_fn = None
    self._train_step_input_partition_spec = None

    # Eval train step function information. Note since this eval step runs on
    # training inputs, it'll have the same input shapes/dtypes and partition
    # spec as the train step function.
    self._eval_train_step_created = False
    self._eval_train_step_fn = None

  def _get_train_step(self) -> Tuple[Any, Optional[NestedPartitionSpec]]:
    """Creates the train step info (if not done before) and returns them."""
    if not self._train_step_created:
      self._train_step_fn, self._train_step_input_partition_spec = (
          self._partitioner.partition(
              trainer_lib.train_step_single_learner,
              self._partitioner.train_inputs_shape_dtype,
              is_eval=False,
          )
      )
      self._train_step_created = True
    return self._train_step_fn, self._train_step_input_partition_spec

  def _get_eval_train_step(self) -> Any:
    """Creates the train step info (if not done before) and returns them."""
    if not self._eval_train_step_created:
      # TODO(pax): Support auto-sharding for eval step. In this case, we would
      # have to fix the sharding of the input to be the same as what's derived
      # from the train_step.

      # Ignores the returned input partition spec. It should be the same as
      # self.train_input_partition_spec since the input shapes are the same.
      self._eval_train_step_fn, _ = self._partitioner.partition(
          trainer_lib.eval_step_single_learner,
          self._partitioner.train_inputs_shape_dtype,  # Train input shapes.
          is_eval=True,
      )
      self._eval_train_step_created = True
    return self._eval_train_step_fn

  def train_step(
      self,
      state: train_states.TrainState,
      prng_key: PRNGKey,
      inputs: NestedJTensor,
      unpadded_global_batch_size: int,
  ) -> Tuple[TrainState, JTensor, WeightedScalars, NestedMap, SummaryDict]:
    """The train step function."""
    train_step, _ = self._get_train_step()
    return train_step(state, prng_key, inputs, unpadded_global_batch_size)

  def eval_train_step(
      self,
      state: train_states.TrainState,
      prng_key: PRNGKey,
      inputs: NestedJTensor,
      unpadded_global_batch_size: int,
  ) -> Tuple[JTensor, WeightedScalars, NestedMap, SummaryDict]:
    """The eval step function on trianing inputs."""
    eval_train_step = self._get_eval_train_step()
    return eval_train_step(state, prng_key, inputs, unpadded_global_batch_size)

  @property
  def train_input_partition_spec(self) -> Optional[NestedPartitionSpec]:
    """The partition spec for the model training inputs."""
    _, input_partition_spec = self._get_train_step()
    return input_partition_spec


def can_load_written_outputs(
    basedir: epath.Path, pname: str, mode: EvaluationMode, step: int
) -> bool:
  """Returns whether we can load the eval/decoder outputs already."""
  success = np.array([0], dtype=np.int32)
  if jax.process_index() == 0:
    try:
      outputs = io_utils.load_outputs(basedir, pname, mode.value, step)
      success[0] = len(outputs)
    except Exception:  # pylint: disable=broad-except
      pass
  out = multihost_utils.broadcast_one_to_all(success)
  return out[0] > 0


def get_filename(
    step: Union[base_layer.JTensorOrPartitionSpec, int], prefix: str
) -> str:
  """Returns a filename for the given step."""
  step_num = py_utils.maybe_unreplicate_for_fully_replicated(step)
  return f'{prefix}_out_{step_num}_shard_{jax.process_index()}'


def safe_write_key_value_pairs(
    filename: epath.PathLike,
    key_value_pairs: Sequence[Tuple[Optional[str], Any]],
    cast_to_ndarray: bool = True,
    write_pickle: bool = True,
) -> None:
  try:
    io_utils.write_key_value_pairs(
        filename, key_value_pairs, cast_to_ndarray, write_pickle
    )
  except TypeError:
    logging.warning('Not serializable.')


def _maybe_write_scoring_outputs(
    output_dir: epath.Path,
    step: int,
    scoring_outputs: Sequence[Tuple[str, Any]],
) -> None:
  """Writes model scoring outputs to disk from leader process."""
  if jax.process_index() != 0 or flags.FLAGS.pax_only_aggregate_summaries:
    return

  fq_fname = output_dir / get_filename(step, EvaluationMode.EVAL.value)
  fq_fname.parent.mkdir(parents=True, exist_ok=True)

  logging.info(
      'Writing eval outputs to %s with %d entries',
      fq_fname,
      len(scoring_outputs),
  )

  safe_write_key_value_pairs(fq_fname, scoring_outputs)


class BaseEvalProgram(Program):

  def __init__(self, input_p: base_input.BaseInput.HParams):
    self._input_p = input_p

    # States to set in self.setup()
    self._task = None
    self._partitioner: partitioning.Partitioner = None
    self._job_log_dir = None
    self._eval_prng_seed = None

    # States to initialize lazily in self.setup()
    self._eval_input_pipeline = None
    self._name = None
    self._eval_unpadded_global_batch_size: int = None
    self._eval_num_steps: int = None
    self._eval_summary_writer = None

    # Used to enter context of the summary writer at .setup().
    self._exitstack = contextlib.ExitStack()

  @property
  def eval_input(self) -> base_input.BaseInput:
    assert self._eval_input_pipeline
    return self._eval_input_pipeline

  def setup(
      self,
      task: tasks_lib.SingleTask,
      partitioner: partitioning.Partitioner,
      job_log_dir: epath.Path,
      eval_prng_seed: pytypes.PRNGKey,
      summary_base_dir: Optional[epath.Path] = None,
  ) -> None:
    self._task = task
    self._partitioner = partitioner
    self._job_log_dir = job_log_dir
    self._eval_prng_seed = eval_prng_seed

    # Updates the input config with runtime information.
    if self._input_p.num_infeed_hosts == 0:
      self._input_p.num_infeed_hosts = jax.process_count()
    self._input_p.infeed_host_index = jax.process_index()

    # Creates the eval input pipeline.
    logging.debug('Initializing eval_input pipeline : %s', self._input_p)
    self._eval_input_pipeline = instantiate(
        self._partitioner.preprocess_input_params(self._input_p)
    )
    self._name = self.eval_input.name
    self._eval_unpadded_global_batch_size = (
        self._eval_input_pipeline.get_global_batch_size(
            self._eval_input_pipeline.hparams
        )
    )
    self._eval_num_steps = (
        -1
        if self._input_p.reset_for_eval
        else self._input_p.eval_loop_num_batches
    )

    # Creates the eval summary writer.
    if not summary_base_dir:
      summary_base_dir = _summary_base_dir(job_log_dir)
    summary_dir = summary_base_dir / f'eval_test_{self.eval_input.name}'
    self._eval_summary_writer = self._exitstack.enter_context(
        summary_utils.get_summary_writer(summary_dir)
    )

  def should_run(self, state: TrainState, train_step: int) -> bool:
    # TODO(laigd): implement and use this.
    raise NotImplementedError()

  def run(self, state: TrainState, train_step: int) -> ProgramOutput:
    if can_load_written_outputs(
        self._job_log_dir, self._name, EvaluationMode.EVAL, train_step
    ):
      logging.info(
          'Eval on %s at train step %d already done, skipping.',
          self._name,
          train_step,
      )
      return ProgramOutput(
          state,
          aux=NestedMap(
              eval_metrics=None, eval_scoring_metrics=None, num_eval_steps=0
          ),
      )

    logging.info(
        'Starting eval %s with num_steps=%d', self._name, self._eval_num_steps
    )
    num_steps, loss, summary_tensors, metrics, per_example_scores = (
        self._run_eval_loop(state)
    )
    logging.info('Finished eval on %s', self._name)

    # Flatten scoring outputs to simplify input for metrics eval computation.
    # Constructs a new flattened array of single example outputs from original
    # array containing batches of outputs.
    flat_scoring_outputs = []
    for batch in per_example_scores:
      for ex in py_utils.tree_unstack(batch, 0):
        flat_scoring_outputs.append((py_utils.get_enumeration_id(ex), ex))
    eval_scoring_metrics = None
    output_dir = (
        self._job_log_dir / f'{EvaluationMode.EVAL.value}_out' / self._name
    )

    # TODO(laigd): consider adding a method for this for subclass to overwrite.
    if seqio_input.should_process_outputs(self.eval_input):
      eval_scoring_metrics = seqio_input.process_outputs(
          self.eval_input,
          flat_scoring_outputs,
          self._eval_summary_writer,
          seqio_input.MetricType.SCORE,
          train_step,
          output_dir,
      )

    loss = np.array(loss)
    for k in summary_tensors:
      summary_tensors[k] = np.array([np.asarray(t) for t in summary_tensors[k]])
    loss = np.mean(loss, axis=0)
    logging.info(
        'train_step: %d, eval test %s loss: %s', train_step, self._name, loss
    )

    for key, values in metrics.items():
      # `metric_utils.as_float` computes the average from a list of weighted
      # scalars.
      weighted_average = metric_utils.as_float(values)
      sum_metric_weights = np.sum(np.stack([v[1] for v in values])).item()
      logging.info(
          '  %s=%f (weight=%f)', key, weighted_average, sum_metric_weights
      )
    summary_utils.write_summary_entry(
        self._eval_summary_writer, train_step, loss, metrics, summary_tensors
    )
    _maybe_write_scoring_outputs(output_dir, train_step, flat_scoring_outputs)

    return ProgramOutput(
        state,
        aux=NestedMap(
            eval_metrics=metric_utils.as_float_dict(metrics),
            eval_scoring_metrics=eval_scoring_metrics,
            num_eval_steps=num_steps,
        ),
    )

  def _run_eval_loop(self, state: TrainState):
    losses = []
    summary_tensor_dict = {}
    metrics = collections.defaultdict(list)
    per_example_scores = []

    step_num = 0
    # self._eval_num_steps < 0 indicates running until input out of range.
    while self._eval_num_steps < 0 or step_num < self._eval_num_steps:
      try:
        eval_inputs = self.eval_input.get_next_padded()
      except (tf.errors.OutOfRangeError, StopIteration):
        if self._eval_num_steps > 0:
          raise
        logging.info('Data exhausted (%s) after %d steps', self._name, step_num)
        self.eval_input.reset()
        break

      step_num += 1
      eval_inputs = self._partitioner.preprocess_inputs(
          self.eval_input, eval_inputs, self.eval_input_partition_spec
      )
      loss, weighted_scalars, per_example_out, summary_tensors = self.eval_step(
          state,
          self._eval_prng_seed,
          eval_inputs,
          self._eval_unpadded_global_batch_size,
      )
      logging.info('Finished eval step %d for %s', step_num, self._name)
      loss, weighted_scalars, per_example_out, summary_tensors = (
          py_utils.maybe_unreplicate_for_fully_replicated(out)
          for out in (loss, weighted_scalars, per_example_out, summary_tensors)
      )

      losses += [loss]
      for k, v in summary_utils.flatten_summary_dict(summary_tensors):
        if k in summary_tensor_dict:
          summary_tensor_dict[k] += [v]
        else:
          summary_tensor_dict[k] = [v]
      for k in weighted_scalars:
        metrics[k].append(weighted_scalars[k])
      per_example_scores.append(jax.tree_map(np.asarray, per_example_out))

    return step_num, losses, summary_tensor_dict, metrics, per_example_scores

  @abc.abstractmethod
  def eval_step(
      self,
      state: train_states.TrainState,
      prng_key: PRNGKey,
      inputs: NestedJTensor,
      unpadded_global_batch_size: int,
  ) -> Tuple[JTensor, WeightedScalars, NestedMap, SummaryDict]:
    """The eval step function."""

  @property
  @abc.abstractmethod
  def eval_input_partition_spec(self) -> Optional[NestedPartitionSpec]:
    """The partition spec for the eval inputs."""

  def shutdown(self) -> None:
    self._exitstack.close()


class SingleTaskEvalProgram(BaseEvalProgram):
  """Eval program that assumes a single task on a single dataset."""

  def __init__(
      self,
      input_p: base_input.BaseInput.HParams,
  ):
    super().__init__(input_p)

    # Eval step function information.
    self._eval_step_created = False
    self._eval_step_fn = None
    self._eval_step_input_spec = None

  def _get_eval_step(self) -> Tuple[Any, Optional[NestedPartitionSpec]]:
    """Creates the eval step info if not done before."""
    if not self._eval_step_created:
      # A bit of unfortunate conditioning but we have to branch out pmap/pjit
      # case here -- As Pmap can simply take the train_inputs_shape_dtype from
      # the partitioner whearas Pjit need to actually look at current eval input
      # and get shape from there.
      input_shape_dtype = self._partitioner.train_inputs_shape_dtype
      if isinstance(
          self._partitioner,
          (
              partitioning.PjitPartitioner,
              partitioning.AutoShardingPjitPartitioner,
          ),
      ):
        # Instantiate a stanalone pipeline for one-time use to get sample inputs
        # since the peek_padded() can return None if the pipeline is exhausted.
        # This can happen when the input_pipeline is used before the partitioned
        # step function is invoked as we do it lazily.
        cloned_input_p = self.eval_input.hparams.clone()
        # Note that the hparams from eval_input is already preprocessed by
        # partitioner, so we don't need to do another adjustment here.
        cloned_pipeline: base_input.BaseInput = instantiate(cloned_input_p)
        input_shape_dtype = jax.tree_map(
            py_utils.get_global_input_shape_dtype,
            cloned_pipeline.get_next_padded(),
        )
        # delete one-time usages.
        del cloned_pipeline, cloned_input_p

      # TODO(laigd): Get rid of inputs_shape_dtype here.
      self._eval_step_fn, self._eval_step_input_spec = (
          self._partitioner.partition(
              trainer_lib.eval_step_single_learner,
              inputs_shape_dtype=input_shape_dtype,
              is_eval=True,
          )
      )
      self._eval_step_created = True
    return self._eval_step_fn, self._eval_step_input_spec

  def eval_step(
      self,
      state: train_states.TrainState,
      prng_key: PRNGKey,
      inputs: NestedJTensor,
      unpadded_global_batch_size: int,
  ) -> Tuple[JTensor, WeightedScalars, NestedMap, SummaryDict]:
    """The eval step function."""
    eval_step, _ = self._get_eval_step()
    return eval_step(state, prng_key, inputs, unpadded_global_batch_size)

  @property
  def eval_input_partition_spec(self) -> Optional[NestedPartitionSpec]:
    """The partition spec for the eval inputs."""
    _, input_partition_spec = self._get_eval_step()
    return input_partition_spec
