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

"""Checkpointing-related utilities to handle TrainState instances."""

from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple, cast

from absl import logging
from etils import epath
import flax.serialization
import jax
import optax
import orbax.checkpoint
from paxml import checkpoint_managers
from paxml import checkpoint_paths
from paxml import checkpoint_types
from paxml import train_states
from praxis import py_utils
from praxis import pytypes


CHECKPOINT_PREFIX = checkpoint_paths.CHECKPOINT_PREFIX

CheckpointType = checkpoint_types.CheckpointType
AsyncCheckpointer = orbax.checkpoint.AsyncCheckpointer
Checkpointer = orbax.checkpoint.Checkpointer
JTensorOrPartitionSpec = pytypes.JTensorOrPartitionSpec
PyTree = Any

latest_checkpoint = checkpoint_paths.latest_checkpoint
retrieve_latest_checkpoint_step = (
    checkpoint_paths.retrieve_latest_checkpoint_step
)
retrieve_checkpoint_type = checkpoint_types.retrieve_checkpoint_type
make_checkpoint_step_dir = checkpoint_paths.make_checkpoint_step_dir
get_step_from_checkpoint_asset = checkpoint_paths.get_step_from_checkpoint_asset
maybe_update_checkpoint_type = checkpoint_types.maybe_update_checkpoint_type
is_checkpoint_asset = checkpoint_paths.is_checkpoint_asset


def get_checkpointer(
    checkpoint_type: CheckpointType,
    async_checkpointer: Optional[AsyncCheckpointer] = None,
    enforce_restore_shape_check: bool = False,
    tensorstore_use_ocdbt: bool = False,
) -> Checkpointer:
  """Creates an appropriate Checkpointer for given CheckpointType."""
  if async_checkpointer is not None:
    return async_checkpointer
  if checkpoint_type in {CheckpointType.GDA, CheckpointType.GDA_VERSION_SUBDIR}:
    checkpointer = orbax.checkpoint.Checkpointer(
        PaxCheckpointHandler(
            enforce_restore_shape_check=enforce_restore_shape_check,
            use_ocdbt=tensorstore_use_ocdbt,
        )
    )
  elif checkpoint_type == CheckpointType.FLAX:
    checkpointer = FlaxCheckpointer(FlaxCheckpointHandler())
  else:
    raise ValueError(f'Unexpected checkpoint_type `{checkpoint_type}`.')
  return checkpointer


def _get_shape_dtype_struct(nested: Any) -> Any:
  return jax.tree_util.tree_map(
      lambda x: jax.ShapeDtypeStruct(x.shape, x.dtype), nested
  )


def save_checkpoint(
    train_state: train_states.TrainState,
    checkpoint_dir: epath.PathLike,
    overwrite: bool = False,
    checkpoint_type: CheckpointType = CheckpointType.FLAX,
    state_specs: Optional[train_states.TrainState] = None,
    async_checkpointer: Optional[AsyncCheckpointer] = None,
    train_state_unpadded_shape_dtype_struct: Optional[
        train_states.TrainState
    ] = None,
    tensorstore_use_ocdbt: bool = False,
) -> checkpoint_managers.OrbaxCheckpointManager:
  """Saves a checkpoint into the provided base directory.

  This is typically called on a replicated TrainState instance.

  Args:
    train_state: The TrainState instance to save.
    checkpoint_dir: The base directory from where to retrieve checkpoints.
    overwrite: Whether to overwrite existing checkpoints files if a checkpoint
      at the current or a later step already exists.
    checkpoint_type: The checkpoint type (implementation) to save. Either
      `FLAX`, `GDA` or `PERSISTENCE`.
    state_specs: Currently unused.
    async_checkpointer: When async checkpointing and Orbax are enabled, allows
      training to continue when checkpointing is going on as checkpointing
      happens in a different thread.
    train_state_unpadded_shape_dtype_struct: jax.ShapeDtypeStruct of the
      unpadded train state.
    tensorstore_use_ocdbt: Enables Tensorstore OCDBT format.

  Returns:
    An OrbaxCheckpointManager object.

  Raises:
    ValueError: If the global step has an unexpected shape, if `state_specs`
    is not specified for persistence-based checkpointing, if
    `checkpoint_type` is invalid, or if unpadded shapes/dtypes are not provided
    for version > 1.
  """
  del state_specs

  checkpoint_dir = epath.Path(checkpoint_dir)
  step = int(py_utils.maybe_unreplicate_for_fully_replicated(train_state.step))

  checkpointer = get_checkpointer(
      checkpoint_type,
      async_checkpointer=async_checkpointer,
      tensorstore_use_ocdbt=tensorstore_use_ocdbt,
  )
  checkpoint_manager = checkpoint_managers.OrbaxCheckpointManager(
      checkpoint_dir,
      checkpointer,
      options=checkpoint_managers.CheckpointManagerOptions(create=True),
      checkpoint_type=checkpoint_type,
      tensorstore_use_ocdbt=tensorstore_use_ocdbt,
  )
  if (
      checkpoint_manager.version > 1
      and train_state_unpadded_shape_dtype_struct is None
  ):
    logging.warning(
        """train_state_unpadded_shape_dtype_struct is not provided. Saving the
        shapes of train_state  as the unpadded shapes."""
    )
    train_state_unpadded_shape_dtype_struct = _get_shape_dtype_struct(
        train_state
    )
  checkpoint_manager.save(
      step,
      train_state,
      train_state_unpadded_shape_dtype_struct,
      force=overwrite,
  )
  return checkpoint_manager


def restore_checkpoint(
    state_global_shapes: train_states.TrainState,
    checkpoint_dir: epath.PathLike,
    global_mesh: Optional[jax.sharding.Mesh] = None,
    checkpoint_type: CheckpointType = CheckpointType.FLAX,
    state_specs: Optional[train_states.TrainState] = None,
    step: Optional[int] = None,
    enforce_restore_shape_check: bool = False,
    state_unpadded_shape_dtype_struct: Optional[train_states.TrainState] = None,
    tensorstore_use_ocdbt: bool = False,
) -> Optional[train_states.TrainState]:
  """Restores a checkpoint from the provided base directory.

  This is typically called on an unreplicated TrainState instance.

  Args:
    state_global_shapes: The TrainState with variable names and corresponding
      ShapeDtypeStruct.
    checkpoint_dir: The base directory from where to retrieve checkpoints.
    global_mesh: The global mesh representing devices across multiple processes.
    checkpoint_type: The checkpoint type (implementation) to restore. Either
      `FLAX`, `GDA` or `PERSISTENCE`.
    state_specs: If using a GDA-based checkpoint, the partition specs
      corresponding to this TrainState instance to restore.
    step: Step number to load a checkpoint from or None to load the latest.
    enforce_restore_shape_check: Raises an error if restore shapes do not match
      checkpoint shapes.
    state_unpadded_shape_dtype_struct: jax.ShapeDtypeStruct of the unpadded
      state.
    tensorstore_use_ocdbt: Enables Tensorstore OCDBT format.

  Returns:
    A restored `TrainState` instance. If no step specified and no checkpoint
    files present, return None.

  Raises:
    ValueError: When a mismatch between the current checkpoint structure and
    the saved checkpoint one is detected.
  """
  checkpoint_dir = epath.Path(checkpoint_dir)
  if not checkpoint_dir.exists():
    return None
  checkpointer = get_checkpointer(
      checkpoint_type,
      enforce_restore_shape_check=enforce_restore_shape_check,
      tensorstore_use_ocdbt=tensorstore_use_ocdbt,
  )
  checkpoint_manager = checkpoint_managers.OrbaxCheckpointManager(
      checkpoint_dir,
      checkpointer,
      checkpoint_type=checkpoint_type,
      tensorstore_use_ocdbt=tensorstore_use_ocdbt,
  )
  if step is None:
    step = checkpoint_manager.latest_step()
    if step is None:
      logging.info('No checkpoint found for restore in %s.', checkpoint_dir)
      return None
  restore_args = None
  if checkpoint_type == CheckpointType.GDA:
    restore_args = {
        'specs': state_specs,
        'mesh': global_mesh,
    }
  return checkpoint_manager.restore(
      step,
      state_global_shapes,
      state_unpadded_shape_dtype_struct,
      restore_kwargs=restore_args,
  )


def reregister_type_handlers(
    tensorstore_metadata_key: Optional[str] = None,
    tensorstore_use_ocdbt: bool = False,
) -> Any:
  """Registers overrides to Orbax TypeHandlers to set Pax-specific properties."""
  if tensorstore_metadata_key is None and not tensorstore_use_ocdbt:
    return

  ts_context = None
  if tensorstore_use_ocdbt:
    ts_context, ocdbt_coordinator_server = (
        orbax.checkpoint.type_handlers.create_coordinator_server_and_context()
    )
  orbax.checkpoint.type_handlers.register_standard_handlers_with_options(
      metadata_key=tensorstore_metadata_key,
      use_ocdbt=tensorstore_use_ocdbt,
      ts_context=ts_context,
  )
  return ocdbt_coordinator_server


def _extract_nested_prefix_names(
    state: train_states.TrainState,
) -> train_states.TrainState:
  """Extracts prefix names from a TrainState data structure."""
  # CNS doesn't support square bracket in filenames.
  key_separator = '.'
  left_separator = '_'
  right_separator = ''
  return train_states.TrainState(
      step=py_utils.extract_prefixed_keys_from_nested_map(
          state.step,
          'step',
          key_separator=key_separator,
          left_separator=left_separator,
          right_separator=right_separator,
      ),
      mdl_vars=py_utils.extract_prefixed_keys_from_nested_map(
          state.mdl_vars,
          'mdl_vars',
          key_separator=key_separator,
          left_separator=left_separator,
          right_separator=right_separator,
      ),
      opt_states=py_utils.extract_prefixed_keys_from_nested_map(
          state.opt_states,
          'opt_states',
          key_separator=key_separator,
          left_separator=left_separator,
          right_separator=right_separator,
          is_leaf=py_utils.is_optax_masked_node,
      ),
  )


def _masked_node_to_none(mask: Any, value: Any) -> Any:
  """Return value when `mask` is not a MaskedNode, or MaskedNode otherwise."""
  if py_utils.is_optax_masked_node(mask):
    return optax.MaskedNode()
  return value


def _tensorstore_prepare(
    train_state: train_states.TrainState,
    state_specs: Optional[train_states.TrainState] = None,
) -> Tuple[
    Sequence[JTensorOrPartitionSpec],
    Sequence[str],
    Optional[Sequence[JTensorOrPartitionSpec]],
]:
  """Prepares data prior to saving/restoring it from/to TensorStore.

  Args:
    train_state: A partitioned train_state that is a Pytree of
      GlobalDeviceArray.
    state_specs: [optional] The partition specs corresponding to this TrainState
      instance, when it is used for checkpoint restoring.

  Returns:
    A 3-tuple (flattened_traine_state, flattened_nested_names, out), where:
    - flattened_traine_state: A flattened version of the train state, where all
      MaskedNode instances have been filtered out.
    - flattened_nested_names: A flattened version of the nested names, where all
      entries corresponding to MaskedNode have been filtered out.
    - out: Either None when the input state_specs is None or the flattened
      version of the state_specs, where all entries corresponding to MaskedNode
      instances have been filtered out.
  """
  # This replaces MaskedNode instances by None values ...
  train_state_none = jax.tree_map(
      _masked_node_to_none,
      train_state,
      train_state,
      is_leaf=py_utils.is_optax_masked_node,
  )
  if state_specs is not None:
    state_specs_none = jax.tree_map(
        _masked_node_to_none,
        train_state,
        state_specs,
        is_leaf=py_utils.is_optax_masked_node,
    )
  # ... that are filtered out when calling jax.tree_util.tree_flatten() here.
  flattened_train_state, _ = jax.tree_util.tree_flatten(train_state_none)
  if state_specs is not None:
    flattened_state_specs, _ = jax.tree_util.tree_flatten(state_specs_none)
  else:
    flattened_state_specs = None

  # _extract_nested_prefix_names() also replaces MaskedNode instances by None
  # values ...
  nested_names = _extract_nested_prefix_names(train_state)
  # ... that are filtered out when calling jax.tree_util.tree_flatten() here.
  flattened_nested_names, _ = jax.tree_util.tree_flatten(nested_names)
  return flattened_train_state, flattened_nested_names, flattened_state_specs


def _tensorstore_reconstruct(
    state_global_shapes: train_states.TrainState,
    restored_train_state: Sequence[JTensorOrPartitionSpec],
) -> train_states.TrainState:
  """Reconstructs a nested train state including MaskedNode.

  Args:
    state_global_shapes: The original nested train state with GDAs, which
      includes MaskedNode entries.
    restored_train_state: A flattened version of the restored train state, which
      does not include any MaskedNode entry.

  Returns:
    A nested version of `restored_train_state` after adding back the MaskedNode
    instances, based on the original structure of `state_global_shapes`.
  """
  c = 0
  restored_flattened_train_state = []
  flattened_state_global_shapes, treedef = jax.tree_util.tree_flatten(
      state_global_shapes
  )
  for l in flattened_state_global_shapes:
    if py_utils.is_optax_masked_node(l):
      restored_flattened_train_state.append(optax.MaskedNode())
    else:
      restored_flattened_train_state.append(restored_train_state[c])
      c += 1
  assert c == len(restored_train_state)
  return jax.tree_util.tree_unflatten(treedef, restored_flattened_train_state)


def _check_restored_shapes(restored: Any, expected: Any):
  def _check(a, b):
    if a.shape != b.shape:
      raise ValueError(
          f'Restored parameter shape mismatch: {a.shape} (checkpoint) vs.'
          f' {b.shape} (expected).'
      )

  jax.tree_util.tree_map(_check, restored, expected)


class PaxCheckpointHandler(orbax.checkpoint.PyTreeCheckpointHandler):
  """PaxCheckpointHandler override for Pax GDA checkpointing.

  Allows setting parameter names manually, which would normally be extracted
  from the train state itself. This is somewhat hacky, and we will aim to remove
  it eventually (see below).

  TODO(cpgaffney) Rework _extract_nested_prefix_names to allow extracting names
  from a state dict.
  """

  _param_names: PyTree = None

  def __init__(
      self, *args, enforce_restore_shape_check: bool = False, **kwargs
  ):
    self._enforce_restore_shape_check = enforce_restore_shape_check
    super().__init__(*args, **kwargs)

  def _set_param_names(self, param_names: PyTree):
    self._param_names = param_names

  def _get_param_names(self, item: PyTree) -> PyTree:
    if self._param_names is None:
      return super()._get_param_names(item)
    return self._param_names

  async def _write_aggregate_file(
      self,
      directory: epath.Path,
      item: PyTree,
      param_infos: PyTree,
      save_args: PyTree,
  ):
    """Skip writing msgpack file for Pax since this file would be unused."""
    if self._use_ocdbt:
      await super()._write_aggregate_file(
          directory, item, param_infos, save_args
      )

  async def async_save(
      self,
      directory: epath.Path,
      item: PyTree,
      save_args: Optional[PyTree] = None,
      version: Optional[float] = None,
  ) -> Any:
    """Filters optax.MaskedNode before calling superclass async_save."""
    if version is None:
      raise ValueError('Expected version for sxaving.')
    if self._use_ocdbt:
      self._set_param_names(None)
    else:
      item, flattened_nested_names, _ = _tensorstore_prepare(item)
      # At that point, the flattened entries do not contain any reference to
      # MaskedNode's.
      self._set_param_names(flattened_nested_names)
    return await super().async_save(directory, item, save_args=save_args)

  def restore(
      self,
      directory: epath.Path,
      item: Optional[PyTree] = None,
      specs: Optional[PyTree] = None,
      mesh: Optional[jax.sharding.Mesh] = None,
      version: Optional[float] = None,
      transforms: Optional[PyTree] = None,
  ) -> PyTree:
    """Restores by filtering optax.MaskedNode and adding it back after calling superclass restore."""
    if version is None:
      raise ValueError('Expected version for restoration.')
    is_ocdbt_checkpoint = orbax.checkpoint.type_handlers.is_ocdbt_checkpoint(
        directory
    )
    if is_ocdbt_checkpoint and not self._use_ocdbt:
      raise ValueError(
          'Must enable `tensorstore_use_ocdbt` in order to load OCDBT format'
          ' checkpoint.'
      )
    if is_ocdbt_checkpoint:
      reference_train_state, reference_state_specs = (item, specs)
    else:
      reference_train_state, reference_nested_names, reference_state_specs = (
          _tensorstore_prepare(item, specs)
      )
      # At that point, the flattened entries do not contain any reference to
      # MaskedNode's.
      # Consequently, we restore the checkpoint that does not contain any
      # reference to MaskedNode's.
      self._set_param_names(reference_nested_names)

    def create_restore_args(pspec, shape_struct):
      # Providing `None` indicates that the shape should be restored exactly as
      # saved.
      restore_shape = (
          None if self._enforce_restore_shape_check else shape_struct.shape
      )
      return orbax.checkpoint.ArrayRestoreArgs(
          restore_type=jax.Array,
          mesh=mesh,
          mesh_axes=pspec,
          global_shape=restore_shape,
          dtype=shape_struct.dtype,
      )

    restore_args = jax.tree_map(
        create_restore_args, reference_state_specs, reference_train_state
    )
    restored_train_state = super().restore(
        directory,
        item=reference_train_state,
        restore_args=restore_args,
        transforms=transforms,
    )
    if self._enforce_restore_shape_check:
      _check_restored_shapes(restored_train_state, reference_train_state)

    if not is_ocdbt_checkpoint:
      # We add back the MaskedNode entries into the pytree.
      restored_train_state = _tensorstore_reconstruct(
          item, restored_train_state
      )

    return restored_train_state

  def structure(self, directory: epath.Path) -> PyTree:
    # Use msgpack file if it exists.
    # Check for _use_ocdbt, since the msgpack file should only exist if the
    # checkpoint was written with OCDBT.
    if self._use_ocdbt and (directory / self._aggregate_filename).exists():
      return super().structure(directory)
    # Otherwise, rely on implicit structure from directories.
    return jax.tree_util.tree_map(
        orbax.checkpoint.utils.leaf_placeholder,
        flax.serialization.to_state_dict(self._param_names),
    )


class FlaxCheckpointHandler(orbax.checkpoint.PyTreeCheckpointHandler):
  """Override to process checkpoints in Flax format.

  Should only be used in conjunction with FlaxCheckpointer.
  """

  async def async_save(
      self,
      directory: epath.Path,
      item: PyTree,
      save_args: Optional[PyTree] = None,
      version: Optional[float] = None,
  ) -> Any:
    if version is None:
      raise ValueError('Expected version for saving.')
    # Extract/flatten data structure to store to disk. Flax requires a flattened
    # data structure to be passed to the checkpointer.
    # Keep bprop_masked_node to be consistent with restore. This allows us to
    # restore a legacy checkpoint which uses placeholder tensors instead of mask
    # nodes.
    flattened_state, pytree_state = jax.tree_util.tree_flatten(
        jax.device_get(item), is_leaf=py_utils.is_bprop_masked_node
    )
    checkpoint_target = {
        'flattened_state': flattened_state,
        # Saves a serialized version of the pytree structure to detect potential
        # mismatch caused by different versions of saver/restorer.
        'str_pytree_state': str(pytree_state),
    }
    assert save_args is None
    save_args = jax.tree_util.tree_map(
        lambda _: orbax.checkpoint.SaveArgs(aggregate=True), checkpoint_target
    )
    return await super().async_save(
        directory, checkpoint_target, save_args=save_args
    )

  def restore(
      self,
      directory: epath.Path,
      item: Optional[PyTree] = None,
      restore_args: Optional[PyTree] = None,
      transforms: Optional[PyTree] = None,
      transforms_default_to_original: bool = True,
      version: Optional[float] = None,
  ) -> PyTree:
    if version is None:
      raise ValueError('Expected version for restoration.')
    # Input the same data structure as in save_checkpoint().
    flattened_state, pytree_state = jax.tree_util.tree_flatten(
        item, is_leaf=py_utils.is_bprop_masked_node
    )
    str_pytree_state = str(pytree_state)
    input_target = {
        'flattened_state': flattened_state,
        'str_pytree_state': str_pytree_state,
    }
    restored_target = super().restore(directory, input_target)
    # Flax restore_checkpoint returned input_target unchanged if
    # no step specified and no checkpoint files present.
    if restored_target is input_target:
      return None
    restored_state = restored_target['flattened_state']
    restored_str_pytree_state = restored_target['str_pytree_state']
    if restored_str_pytree_state != str_pytree_state:
      # Could be spurious due to abbreviation of treedef printing added in
      # https://github.com/tensorflow/tensorflow/commit/aa21adc148c98c76f54ba5932ce34cf59da538c4
      logging.warning(
          (
              'A possible mismatch (could be spurious) between the saved '
              'checkpoint structure and the current one has been detected '
              '(%s vs %s).'
          ),
          restored_str_pytree_state,
          str_pytree_state,
      )
    restored = jax.tree_util.tree_unflatten(pytree_state, restored_state)
    # With bprop var inclusion, legacy checkpoint still has placeholder tensors
    # the optimizer state.
    restored = jax.tree_util.tree_map(
        lambda x, y: x if py_utils.is_bprop_masked_node(x) else y,
        item,
        restored,
        is_leaf=py_utils.is_bprop_masked_node,
    )
    return restored


class FlaxCheckpointer(orbax.checkpoint.Checkpointer):
  """Allows restoring legacy Flax checkpoints, which are not directories.

  Should only be used in conjunction with FlaxCheckpointHandler.
  """

  def restore(
      self,
      directory: epath.PathLike,
      *args,
      item: Optional[Any] = None,
      **kwargs,
  ) -> Any:
    if not isinstance(self._handler, FlaxCheckpointHandler):
      raise ValueError('Unsupported handler for FlaxCheckpointer.')
    self._handler = cast(FlaxCheckpointHandler, self._handler)
    directory = epath.Path(directory)
    original_aggregate_filename = self._handler._aggregate_filename  # pylint: disable=protected-access
    # If is_file, then the checkpoint is in legacy format, not saved with orbax.
    # Orbax checkpoints are directories containing a file called 'checkpoint'.
    if directory.is_file():
      # The msgpack file is actually the "directory".
      self._handler._aggregate_filename = directory.name  # pylint: disable=protected-access
      directory = directory.parent
    result = super().restore(directory, *args, item=item, **kwargs)
    # Reset aggregate_filename back to normal.
    self._handler._aggregate_filename = (  # pylint: disable=protected-access
        original_aggregate_filename
    )
    return result


class BaseInputCheckpointHandler(orbax.checkpoint.CheckpointHandler):
  """A CheckpointHandler implementation that handles a tf.data BaseInput (sub)class.

  Useful for distributed input where the data iterator on the server cannot be
  accessed from the client, and thus we cannot call .save() and .restore() with
  input._iter like other implementations of DatasetCheckpointHandler.
  """

  def save(self, directory: epath.Path, item: Any):
    """Saves the given item.

    Args:
      directory: save location directory.
      item: a BaseInput to be saved, which must have save() implemented.
    """
    checkpoint_path = (
        directory / f'process_{jax.process_index()}-of-{jax.process_count()}'
    )
    item.save(checkpoint_path)

  def restore(self, directory: epath.Path, item: Any = None) -> None:
    """Restores the given item.

    Args:
      directory: restore location directory.
      item: a BaseInput to be restored, which must have restore() implemented.
        Not Optional (declared as optional to conform to
        orbax.checkpoint.CheckpointHandler superclass)
    """
    if item is None:
      raise ValueError('Must provide item to restore')
    if not directory.exists():
      raise ValueError(f'Checkpoint dir {directory} does not exist.')
    checkpoint_path = (
        directory / f'process_{jax.process_index()}-of-{jax.process_count()}'
    )
    item.restore(checkpoint_path)

  def structure(self, directory: epath.Path) -> Any:
    """Unimplemented. See parent class."""
    return NotImplementedError
