# Copyright 2019-2021 kubeflow.org.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ast
import inspect
import json
import os
import re
import tarfile
import textwrap
import uuid
import zipfile
import copy
from collections import defaultdict
import collections
from os import environ as env
from typing import Callable, List, Text, Dict, Any
import hashlib
from kubernetes.client.models import V1Volume
from kubernetes import client

import yaml
# Kubeflow Pipeline imports
from kfp import dsl
from kfp.compiler._default_transformers import add_pod_env
from kfp.compiler.compiler import Compiler
from kfp.components.structures import InputSpec
from kfp.dsl._for_loop import LoopArguments
from kfp.dsl._metadata import _extract_pipeline_metadata
# KFP-Tekton imports
from kfp_tekton.compiler import __tekton_api_version__ as tekton_api_version
from kfp_tekton.compiler._data_passing_rewriter import fix_big_data_passing, fix_big_data_passing_using_volume, BIG_DATA_PATH_FORMAT
from kfp_tekton.compiler._k8s_helper import convert_k8s_obj_to_json, sanitize_k8s_name, sanitize_k8s_object
from kfp_tekton.compiler._op_to_template import _op_to_template
from kfp_tekton.compiler._tekton_handler import _handle_tekton_pipeline_variables, _handle_tekton_custom_task, _process_argo_vars
from kfp_tekton.compiler.pipeline_utils import TektonPipelineConf
from kfp_tekton.compiler.yaml_utils import dump_yaml
from kfp_tekton.tekton import TEKTON_CUSTOM_TASK_IMAGES, DEFAULT_CONDITION_OUTPUT_KEYWORD, \
  LOOP_PIPELINE_NAME_LENGTH, LOOP_GROUP_NAME_LENGTH, AddOnGroup

DEFAULT_ARTIFACT_BUCKET = env.get('DEFAULT_ARTIFACT_BUCKET', 'mlpipeline')
DEFAULT_ARTIFACT_ENDPOINT = env.get('DEFAULT_ARTIFACT_ENDPOINT', 'minio-service.kubeflow:9000')
DEFAULT_ARTIFACT_ENDPOINT_SCHEME = env.get('DEFAULT_ARTIFACT_ENDPOINT_SCHEME', 'http://')
# DISABLE_CEL_CONDITION should be True until CEL is officially merged into Tekton main API.
DISABLE_CEL_CONDITION = True
# Default finally extension is 5 minutes
DEFAULT_FINALLY_SECONDS = 300


def _get_super_condition_template(image_name="python:3.9.17-alpine3.18"):

  python_script = textwrap.dedent('''\
    import sys
    input1=str.rstrip(sys.argv[1])
    input2=str.rstrip(sys.argv[2])
    try:
      input1=int(input1)
      input2=int(input2)
    except:
      input1=str(input1)
    %(s)s="true" if (input1 $(inputs.params.operator) input2) else "false"
    f = open("/tekton/results/%(s)s", "w")
    f.write(%(s)s)
    f.close()
    '''
    % {'s': DEFAULT_CONDITION_OUTPUT_KEYWORD})

  template = {
    'results': [
      {'name': DEFAULT_CONDITION_OUTPUT_KEYWORD,
       'type': 'string',
       'description': 'Conditional task %s' % DEFAULT_CONDITION_OUTPUT_KEYWORD
       }
    ],
    'params': [
      {'name': 'operand1'},
      {'name': 'operand2'},
      {'name': 'operator'}
    ],
    'steps': [{
      'name': 'main',
      'command': ['sh', '-ec', 'program_path=$(mktemp); printf "%s" "$0" > "$program_path";  python3 -u "$program_path" "$1" "$2"'],
      'args': [python_script, '$(inputs.params.operand1)', '$(inputs.params.operand2)'],
      'image': image_name,
    }]
  }

  return template


def _get_cel_condition_template():
  template = {
    "name": "cel_condition",
    "apiVersion": "cel.tekton.dev/v1alpha1",
    "kind": "CEL"
  }

  return template


class TektonCompiler(Compiler):
  """DSL Compiler to generate Tekton YAML.

  It compiles DSL pipeline functions into workflow yaml. Example usage:
  ```python
  @dsl.pipeline(
    name='name',
    description='description'
  )
  def my_pipeline(a: int = 1, b: str = "default value"):
    ...

  TektonCompiler().compile(my_pipeline, 'path/to/workflow.yaml')
  ```
  """

  def __init__(self, **kwargs):
    # Input and output artifacts are hash maps for metadata tracking.
    # artifact_items is the artifact dependency map
    # loops_pipeline recorde the loop tasks information for each loops
    # produce_taskspec Produces task spec as part of Tekton pipelineRuns
    self.input_artifacts = {}
    self.output_artifacts = {}
    self.artifact_items = {}
    self.loops_pipeline = {}
    self.addon_groups = {}
    self.recursive_tasks = []
    self.custom_task_crs = []
    self.uuid = self._get_unique_id_code()
    self._group_names = []
    self.pipeline_labels = {'pipelines.kubeflow.org/pipelinename': '', 'pipelines.kubeflow.org/generation': ''}
    self.pipeline_annotations = {'tekton.dev/template': ''}
    self.tekton_inline_spec = True
    self.resource_in_separate_yaml = False
    self.produce_taskspec = True
    self.security_context = None
    self.automount_service_account_token = None
    self.group_names = {}
    self.pipeline_env = {}
    self.pipeline_workspaces = {}
    self.task_workspaces = {}
    self.generate_component_spec_annotations = True
    self.condition_image_name = "python:3.9.17-alpine3.18"
    super().__init__(**kwargs)

  def _set_pipeline_conf(self, tekton_pipeline_conf: TektonPipelineConf):
    self.pipeline_labels = tekton_pipeline_conf.pipeline_labels
    self.pipeline_labels['pipelines.kubeflow.org/pipelinename'] = ''
    self.pipeline_labels['pipelines.kubeflow.org/generation'] = ''
    self.pipeline_annotations = tekton_pipeline_conf.pipeline_annotations
    self.pipeline_annotations['tekton.dev/template'] = ''
    self.tekton_inline_spec = tekton_pipeline_conf.tekton_inline_spec
    self.resource_in_separate_yaml = tekton_pipeline_conf.resource_in_separate_yaml
    self.security_context = tekton_pipeline_conf.security_context
    self.automount_service_account_token = tekton_pipeline_conf.automount_service_account_token
    self.pipeline_env = tekton_pipeline_conf.pipeline_env
    self.pipeline_workspaces = tekton_pipeline_conf.pipeline_workspaces
    self.generate_component_spec_annotations = tekton_pipeline_conf.generate_component_spec_annotations
    self.condition_image_name = tekton_pipeline_conf.condition_image_name

  def _resolve_value_or_reference(self, value_or_reference, potential_references):
    """_resolve_value_or_reference resolves values and PipelineParams, which could be task parameters or input parameters.
    Args:
      value_or_reference: value or reference to be resolved. It could be basic python types or PipelineParam
      potential_references(dict{str->str}): a dictionary of parameter names to task names
      """
    if isinstance(value_or_reference, dsl.PipelineParam):
      parameter_name = value_or_reference.full_name
      task_names = [task_name for param_name, task_name in potential_references if param_name == parameter_name]
      if task_names:
        task_name = task_names[0]
        # When the task_name is None, the parameter comes directly from ancient ancesters
        # instead of parents. Thus, it is resolved as the input parameter in the current group.
        if task_name is None:
          return '$(params.%s)' % parameter_name
        else:
          return '$(params.%s)' % task_name
      else:
        return '$(params.%s)' % parameter_name
    else:
      return str(value_or_reference)

  def _get_groups(self, root_group):
    """Helper function to get all groups (not including ops) in a pipeline."""

    def _get_groups_helper(group):
      groups = {group.name: group}
      for g in group.groups:
        groups.update(_get_groups_helper(g))
      return groups

    return _get_groups_helper(root_group)

  @staticmethod
  def _get_unique_id_code():
    return uuid.uuid4().hex[:5]

  def _group_to_dag_template(self, group, inputs, outputs, dependencies, pipeline_name, group_type, opsgroups):
    """Generate template given an OpsGroup.
    inputs, outputs, dependencies are all helper dicts.
    """
    # Generate GroupOp template
    sub_group = group
    # For loop and recursion id appends 5 characters, so limit the loop/recusion pipeline_name to 44 char and group_name to 12
    # Group_name is truncated reversely because it has an unique identifier at the end of the name.
    pipeline_name_copy = sanitize_k8s_name(pipeline_name, max_length=LOOP_PIPELINE_NAME_LENGTH)
    sub_group_name_copy = sanitize_k8s_name(sub_group.name, max_length=LOOP_GROUP_NAME_LENGTH, rev_truncate=True)
    self._group_names = [pipeline_name_copy, sub_group_name_copy]
    raw_group_name = '-'.join(self._group_names)
    if self.uuid:
      self._group_names.insert(1, self.uuid)
    # pipeline name (max 40) + loop id (max 5) + group name (max 16) + two connecting dashes (2) = 63 (Max size for CRD names)
    group_name = '-'.join(self._group_names) if group_type == "loop" or \
        group_type == "graph" or group_type == 'addon' else sub_group.name
    self.group_names[raw_group_name] = group_name
    template = {
      'metadata': {
        'name': group_name,
      },
      'spec': {}
    }

    # Generates a pseudo-template unique to conditions due to the catalog condition approach
    # where every condition is an extension of one super-condition
    if isinstance(sub_group, dsl.OpsGroup) and sub_group.type == 'condition':
      subgroup_inputs = inputs.get(group_name, [])
      condition = sub_group.condition

      operand1_value = self._resolve_value_or_reference(condition.operand1, subgroup_inputs)
      operand2_value = self._resolve_value_or_reference(condition.operand2, subgroup_inputs)
      template['kind'] = 'Condition'
      template['spec']['params'] = [
        {'name': 'operand1', 'value': operand1_value, 'type': type(condition.operand1),
        'op_name': getattr(condition.operand1, 'op_name', ''), 'output_name': getattr(condition.operand1, 'name', '')},
        {'name': 'operand2', 'value': operand2_value, 'type': type(condition.operand2),
        'op_name': getattr(condition.operand2, 'op_name', ''), 'output_name': getattr(condition.operand2, 'name', '')},
        {'name': 'operator', 'value': str(condition.operator), 'type': type(condition.operator)}
      ]

    # dsl does not expose Graph so here use sub_group.type to check whether it's graph
    if sub_group.type == "graph":
      # for graph now we just support as a pipeline loop with just 1 iteration
      loop_args_name = "just_one_iteration"
      loop_args_value = ["1"]

      # Special handling for recursive subgroup
      if sub_group.recursive_ref:
        # generate ref graph name
        sub_group_recursive_name_copy = sanitize_k8s_name(sub_group.recursive_ref.name,
                                        max_length=LOOP_GROUP_NAME_LENGTH, rev_truncate=True)
        tmp_group_names = [pipeline_name_copy, sub_group_recursive_name_copy]
        if self.uuid:
          tmp_group_names.insert(1, self.uuid)
        ref_group_name = '-'.join(tmp_group_names)

        # generate params
        params = [{
          "name": loop_args_name,
          "value": loop_args_value
        }]

        # get other input params, for recursion need rename the param name to the refrenced one
        for i in range(len(sub_group.inputs)):
            g_input = sub_group.inputs[i]
            inputRef = sub_group.recursive_ref.inputs[i]
            if g_input.op_name:
              params.append({
                'name': inputRef.full_name,
                'value': '$(tasks.%s.results.%s)' % (g_input.op_name, g_input.name)
              })
            else:
              params.append({
                'name': inputRef.full_name, 'value': '$(params.%s)' % g_input.name
              })

        self.recursive_tasks.append({
          'name': sub_group.name,
          'taskRef': {
            'apiVersion': 'custom.tekton.dev/v1alpha1',
            'kind': 'PipelineLoop',
            'name': ref_group_name
          },
          'params': params
        })
      # normal graph logic start from here
      else:
        self.loops_pipeline[group_name] = {
          'kind': 'loops',
          'loop_args': loop_args_name,
          'loop_sub_args': [],
          'task_list': [],
          'spec': {},
          'depends': []
        }
        # get the dependencies tasks rely on the loop task.
        for depend in dependencies.keys():
          if depend == sub_group.name:
            self.loops_pipeline[group_name]['spec']['runAfter'] = [task for task in dependencies[depend]]
            self.loops_pipeline[group_name]['spec']['runAfter'].sort()
          # for items depend on the graph, it will be handled in custom task handler
          if sub_group.name in dependencies[depend]:
            dependencies[depend].remove(sub_group.name)
            self.loops_pipeline[group_name]['depends'].append({'org': depend, 'runAfter': group_name})
        for op in sub_group.groups + sub_group.ops:
          self.loops_pipeline[group_name]['task_list'].append(sanitize_k8s_name(op.name))
          if hasattr(op, 'type') and op.type == 'condition':
            if op.ops:
              for condition_op in op.ops:
                self.loops_pipeline[group_name]['task_list'].append(sanitize_k8s_name(condition_op.name))
            if op.groups:
              for condition_op in op.groups:
                self.loops_pipeline[group_name]['task_list'].append(sanitize_k8s_name(condition_op.name))
        self.loops_pipeline[group_name]['spec']['name'] = group_name
        self.loops_pipeline[group_name]['spec']['taskRef'] = {
          "apiVersion": "custom.tekton.dev/v1alpha1",
          "kind": "PipelineLoop",
          "name": group_name
        }

        self.loops_pipeline[group_name]['spec']['params'] = [{
          "name": loop_args_name,
          "value": loop_args_value
        }]

        # get other input params
        for input_ in inputs.keys():
          if input_ == sub_group.name:
            for param in inputs[input_]:
              if param[1]:
                replace_str = param[1] + '-'
                self.loops_pipeline[group_name]['spec']['params'].append({
                  'name': param[0], 'value': '$(tasks.%s.results.%s)' % (
                    param[1], sanitize_k8s_name(param[0].replace(replace_str, '', 1), allow_capital=True)
                  )
                })
              if not param[1]:
                self.loops_pipeline[group_name]['spec']['params'].append({
                  'name': param[0], 'value': '$(params.%s)' % param[0]
                })

    def dep_helper(custom_task, sub_group):
      """get the dependencies tasks rely on the custom_task."""
      for depend in dependencies.keys():
        if depend == sub_group.name:
          custom_task['spec']['runAfter'] = [task for task in dependencies[depend]]
          custom_task['spec']['runAfter'].sort()
        if sub_group.name in dependencies[depend]:
          custom_task['depends'].append({'org': depend, 'runAfter': group_name})
      for op in sub_group.groups + sub_group.ops:
        custom_task['task_list'].append(sanitize_k8s_name(op.name))
        # Add all the condition nested ops into the pipeline loop sub-dag
        nested_groups = []
        if hasattr(op, 'type') and op.type == 'condition':
          nested_groups.append(op.name)
          if op.ops:
            for condition_op in op.ops:
              custom_task['task_list'].append(sanitize_k8s_name(condition_op.name))
          # If the nested op is a condition, find all the ops groups that are under the condition block
          # until it reaches the end of the graph.
          while nested_groups:
            nested_group = nested_groups.pop(0)
            opsgroup = opsgroups.get(nested_group, None)
            if opsgroup and isinstance(opsgroup, dsl.OpsGroup) and opsgroup.type == 'condition':
              condi_sub_groups = opsgroup.groups + opsgroup.ops
              for condi_sub_group in condi_sub_groups:
                  custom_task['task_list'].append(sanitize_k8s_name(condi_sub_group.name))
                  nested_groups.append(condi_sub_group.name)

    def input_helper(custom_task, sub_group, param_list):
      """add param from inputs if input is not in param_list"""
      if sub_group.name in inputs:
        for param in inputs[sub_group.name]:
          if param[1] and param[0] not in param_list:
            replace_str = param[1] + '-'
            custom_task['spec']['params'].append({
              'name': param[0], 'value': '$(tasks.%s.results.%s)' % (
                param[1], sanitize_k8s_name(param[0].replace(replace_str, '', 1), allow_capital=True)
              )
            })
          if not param[1] and param[0] not in param_list:
            custom_task['spec']['params'].append({
              'name': param[0], 'value': '$(params.%s)' % param[0]
            })

    def process_pipelineparam(s):
      """
        This function takes a string and replaces all instances of {{pipelineparam:op=<op_name>;name=<param_name>}}
        with the appropriate value.

        If op_name is empty, then the value of the parameter is taken from the pipeline parameters.
        If op_name is not empty, then the value of the parameter is taken from the results of the operation.

        The parameter name is sanitized to be a valid Kubernetes name.
      """
      if "{{pipelineparam" in s:
        pipe_params = re.findall(r"{{pipelineparam:op=([^ \t\n,]*);name=([^ \t\n,]*)}}", s)
        for pipe_param in pipe_params:
          if pipe_param[0] == '':
            s = s.replace("{{pipelineparam:op=%s;name=%s}}" % pipe_param, '$(params.%s)' % pipe_param[1])
          else:
            param_name = sanitize_k8s_name(pipe_param[1], allow_capital=True)
            s = s.replace("{{pipelineparam:op=%s;name=%s}}" % pipe_param, '$(tasks.%s.results.%s)' % (
              sanitize_k8s_name(pipe_param[0]),
              param_name))
      return s

    if isinstance(sub_group, AddOnGroup):
      params = []
      for k, v in sub_group.params.items():
        if isinstance(v, dsl.PipelineParam):
          if v.op_name is None:
            v = '$(params.%s)' % v.name
          else:
            param_name = sanitize_k8s_name(v.name, allow_capital=True)
            v = '$(tasks.%s.results.%s)' % (
              sanitize_k8s_name(v.op_name),
              param_name)
        else:
          if isinstance(v, str):
            v = process_pipelineparam(v)
          else:
            v = str(v)
        params.append({'name': sanitize_k8s_name(k, True), 'value': v})

      self.addon_groups[group_name] = {
        'kind': 'addon',
        'task_list': [],
        'spec': {
          'name': group_name,
          'taskRef': {
            'apiVersion': sub_group.api_version,
            'kind': sub_group.kind,
            'name': group_name,
          },
          'params': params,
        },
        'depends': [],
        '_data': sub_group
      }
      dep_helper(self.addon_groups[group_name], sub_group)
      input_helper(self.addon_groups[group_name], sub_group, sub_group.params)

    if isinstance(sub_group, dsl.ParallelFor):
      self.loops_pipeline[group_name] = {
        'kind': 'loops',
        'loop_args': sub_group.loop_args.full_name,
        'loop_sub_args': [],
        'task_list': [],
        'spec': {},
        'depends': []
      }
      if hasattr(sub_group, 'separator') and sub_group.separator is not None:
        self.loops_pipeline[group_name]['separator'] = sub_group.separator.full_name
      if hasattr(sub_group, 'start') and sub_group.start is not None:
        self.loops_pipeline[group_name]['start'] = sub_group.start
        self.loops_pipeline[group_name]['end'] = sub_group.end
        self.loops_pipeline[group_name]['step'] = sub_group.step
      if hasattr(sub_group, 'call_enumerate') and sub_group.call_enumerate and sub_group.iteration_number is not None:
        self.loops_pipeline[group_name]['iteration_number'] = sub_group.iteration_number.full_name
      for subvarName in sub_group.loop_args.referenced_subvar_names:
        if subvarName != '__iter__':
          self.loops_pipeline[group_name]['loop_sub_args'].append(sub_group.loop_args.full_name + '-subvar-' + subvarName)
      if isinstance(sub_group.loop_args.items_or_pipeline_param, list) and isinstance(
        sub_group.loop_args.items_or_pipeline_param[0], dict):
        for key in sub_group.loop_args.items_or_pipeline_param[0]:
          self.loops_pipeline[group_name]['loop_sub_args'].append(sub_group.loop_args.full_name + '-subvar-' + key)
      # get the dependencies tasks rely on the loop task.
      dep_helper(self.loops_pipeline[group_name], sub_group)
      self.loops_pipeline[group_name]['spec']['name'] = group_name
      self.loops_pipeline[group_name]['spec']['taskRef'] = {
        "apiVersion": "custom.tekton.dev/v1alpha1",
        "kind": "PipelineLoop",
        "name": group_name
      }
      # Handle sub-pipeline metadata
      if hasattr(sub_group, 'pod_annotations') and sub_group.pod_annotations:
        self.loops_pipeline[group_name]['spec']['taskRef']['metadata'] = \
          self.loops_pipeline[group_name]['spec']['taskRef'].setdefault('metadata', {'annotations': {}})
        self.loops_pipeline[group_name]['spec']['taskRef']['metadata']['annotations'] = sub_group.pod_annotations
      if hasattr(sub_group, 'pod_labels') and sub_group.pod_annotations:
        self.loops_pipeline[group_name]['spec']['taskRef']['metadata'] = \
          self.loops_pipeline[group_name]['spec']['taskRef'].setdefault('metadata', {'labels': {}})
        self.loops_pipeline[group_name]['spec']['taskRef']['metadata']['labels'] = sub_group.pod_labels
      if sub_group.items_is_pipeline_param:
        # these loop args are a 'dynamic param' rather than 'static param'.
        # i.e., rather than a static list, they are either the output of another task or were input
        # as global pipeline parameters
        pipeline_param = sub_group.loop_args.items_or_pipeline_param
        if pipeline_param.op_name is None:
          withparam_value = '$(params.%s)' % pipeline_param.name
        else:
          param_name = sanitize_k8s_name(pipeline_param.name, allow_capital=True)
          withparam_value = '$(tasks.%s.results.%s)' % (
            sanitize_k8s_name(pipeline_param.op_name),
            param_name)

        self.loops_pipeline[group_name]['spec']['params'] = [{
          "name": sub_group.loop_args.full_name,
          "value": withparam_value
        }]
      elif hasattr(sub_group, 'items_is_string') and sub_group.items_is_string:
        loop_args_str_value = sub_group.loop_args.to_str_for_task_yaml()
        self.loops_pipeline[group_name]['spec']['params'] = [{
          "name": sub_group.loop_args.full_name,
          "value": loop_args_str_value
        }]
      else:
        # Need to sanitize the dict keys for consistency.
        loop_arg_value = sub_group.loop_args.to_list_for_task_yaml()
        loop_args_str_value = ''
        sanitized_tasks = []
        if isinstance(loop_arg_value[0], dict):
          for argument_set in loop_arg_value:
            c_dict = {}
            for k, v in argument_set.items():
              if isinstance(v, dsl.PipelineParam):
                if v.op_name is None:
                  v = '$(params.%s)' % v.name
                else:
                  param_name = sanitize_k8s_name(v.name, allow_capital=True)
                  v = '$(tasks.%s.results.%s)' % (
                    sanitize_k8s_name(v.op_name),
                    param_name)
              else:
                if isinstance(v, str):
                  v = process_pipelineparam(v)
              c_dict[sanitize_k8s_name(k, True)] = v
            sanitized_tasks.append(c_dict)
          loop_args_str_value = json.dumps(sanitized_tasks, sort_keys=True)
        else:
          for i, value in enumerate(loop_arg_value):
            if isinstance(value, str):
              loop_arg_value[i] = process_pipelineparam(value)
          loop_args_str_value = json.dumps(loop_arg_value)

        self.loops_pipeline[group_name]['spec']['params'] = [{
          "name": sub_group.loop_args.full_name,
          "value": loop_args_str_value
        }]

      # start, end, step params should be added as a parameter
      # isinstance(sub_group.start, dsl.PipelineParam)
      def process_parameter(parameter):
        parameter_value = str(parameter)
        if isinstance(parameter, dsl.PipelineParam):
          if parameter.op_name:
            parameter_value = '$(tasks.' + parameter.op_name + '.results.' + \
                               sanitize_k8s_name(parameter.name, allow_capital=True) + ')'
          else:
            parameter_value = '$(params.' + parameter.name + ')'
        return parameter_value

      if hasattr(sub_group, 'separator') and sub_group.separator is not None:
        # separator should be added as a parameter
        sep_param = {
          "name": sub_group.separator.full_name,
          "value": process_parameter(sub_group.separator.value)
        }
        self.loops_pipeline[group_name]['spec']['params'].append(sep_param)

      if hasattr(sub_group, 'start') and sub_group.start is not None:
        start_param = {
          "name": 'from',
          "value": process_parameter(sub_group.start)
        }
        self.loops_pipeline[group_name]['spec']['params'].append(start_param)
        end_param = {
          "name": 'to',
          "value": process_parameter(sub_group.end)
        }
        self.loops_pipeline[group_name]['spec']['params'].append(end_param)
        if sub_group.step is not None:
          step_param = {
            "name": 'step',
            "value": process_parameter(sub_group.step)
          }
          self.loops_pipeline[group_name]['spec']['params'].append(step_param)

      # get other input params
      input_helper(self.loops_pipeline[group_name], sub_group,
          self.loops_pipeline[group_name]['loop_sub_args'] + [sub_group.loop_args.full_name])
      if sub_group.parallelism is not None and sub_group.parallelism > 0:
        self.loops_pipeline[group_name]['spec']['parallelism'] = sub_group.parallelism

      def insert_extra_config_field(config_name, config_object, extra_field_name):
        # Default allowed values
        config_value_list = ['inline', 'file']
        config_value = config_object.lower()
        # Update the list of allowed values if exist
        if hasattr(sub_group, 'config_value_list'):
          config_value_list = sub_group.config_value_list.get(extra_field_name, config_value_list)
        if config_value in config_value_list:
          self.loops_pipeline[group_name]['spec'][config_name] = config_value
        else:
          raise ValueError("%s value in loop %s must be one of [%s], not %s" %
                           (config_name, group_name, ",".join(config_value_list), config_value))
      if hasattr(sub_group, 'iterate_param_pass_style') and sub_group.iterate_param_pass_style is not None:
        insert_extra_config_field('iterateParamPassStyle', sub_group.iterate_param_pass_style, 'iterate_param_pass_style')
      if hasattr(sub_group, 'item_pass_style') and sub_group.item_pass_style is not None:
        insert_extra_config_field('itemPassStyle', sub_group.item_pass_style, 'item_pass_style')

    return template

  def _create_dag_templates(self, pipeline, op_transformers=None, params=None, op_to_templates_handler=None):
    """Create all groups and ops templates in the pipeline.

    Args:
      pipeline: Pipeline context object to get all the pipeline data from.
      op_transformers: A list of functions that are applied to all ContainerOp instances that are being processed.
      op_to_templates_handler: Handler which converts a base op into a list of argo templates.
    """

    op_to_steps_handler = op_to_templates_handler or (lambda op: [_op_to_template(op,
                                                                  self.output_artifacts,
                                                                  self.artifact_items,
                                                                  self.generate_component_spec_annotations)])
    root_group = pipeline.groups[0]

    # Call the transformation functions before determining the inputs/outputs, otherwise
    # the user would not be able to use pipeline parameters in the container definition
    # (for example as pod labels) - the generated template is invalid.
    for op in pipeline.ops.values():
      for transformer in op_transformers or []:
        transformer(op)

    # Generate core data structures to prepare for argo yaml generation
    #   op_name_to_parent_groups: op name -> list of ancestor groups including the current op
    #   opsgroups: a dictionary of ospgroup.name -> opsgroup
    #   inputs, outputs: group/op names -> list of tuples (full_param_name, producing_op_name)
    #   condition_params: recursive_group/op names -> list of pipelineparam
    #   dependencies: group/op name -> list of dependent groups/ops.
    # Special Handling for the recursive opsgroup
    #   op_name_to_parent_groups also contains the recursive opsgroups
    #   condition_params from _get_condition_params_for_ops also contains the recursive opsgroups
    #   groups does not include the recursive opsgroups
    opsgroups = self._get_groups(root_group)
    op_name_to_parent_groups = self._get_groups_for_ops(root_group)
    opgroup_name_to_parent_groups = self._get_groups_for_opsgroups(root_group)
    condition_params = self._get_condition_params_for_ops(root_group)
    op_name_to_for_loop_op = self._get_for_loop_ops(root_group)
    inputs, outputs = self._get_inputs_outputs(
      pipeline,
      root_group,
      op_name_to_parent_groups,
      opgroup_name_to_parent_groups,
      condition_params,
      op_name_to_for_loop_op,
      opsgroups
    )
    dependencies = self._get_dependencies(
      pipeline,
      root_group,
      op_name_to_parent_groups,
      opgroup_name_to_parent_groups,
      opsgroups,
      condition_params,
    )
    templates = []
    for opsgroup in opsgroups.keys():
      # Conditions and loops will get templates in Tekton
      if opsgroups[opsgroup].type == 'condition':
        template = self._group_to_dag_template(opsgroups[opsgroup], inputs, outputs, dependencies, pipeline.name, "condition", opsgroups)
        templates.append(template)
      if opsgroups[opsgroup].type == 'addon_group':
        self._group_to_dag_template(opsgroups[opsgroup], inputs, outputs, dependencies, pipeline.name, "addon", opsgroups)
      if opsgroups[opsgroup].type == 'for_loop':
        self._group_to_dag_template(opsgroups[opsgroup], inputs, outputs, dependencies, pipeline.name, "loop", opsgroups)
      if opsgroups[opsgroup].type == 'graph':
        self._group_to_dag_template(opsgroups[opsgroup], inputs, outputs, dependencies, pipeline.name, "graph", opsgroups)

    for op in pipeline.ops.values():
      templates.extend(op_to_steps_handler(op))

    return templates

  def _get_dependencies(self, pipeline, root_group, op_groups,
                        opsgroups_groups, opsgroups, condition_params):
      """Get dependent groups and ops for all ops and groups.
      Returns:
        A dict. Key is group/op name, value is a list of dependent groups/ops.
        The dependencies are calculated in the following way: if op2 depends on op1,
        and their ancestors are [root, G1, G2, op1] and [root, G1, G3, G4, op2],
        then G3 is dependent on G2. Basically dependency only exists in the first uncommon
        ancesters in their ancesters chain. Only sibling groups/ops can have dependencies.
      """
      dependencies = defaultdict(set)
      for op in pipeline.ops.values():
          upstream_op_names = set()
          for param in op.inputs + list(condition_params[op.name]):
              if param.op_name:
                  upstream_op_names.add(param.op_name)
          upstream_op_names |= set(op.dependent_names)

          for upstream_op_name in upstream_op_names:
              # the dependent op could be either a BaseOp or an opsgroup
              if upstream_op_name in pipeline.ops:
                  upstream_op = pipeline.ops[upstream_op_name]
              elif upstream_op_name in opsgroups:
                  upstream_op = opsgroups[upstream_op_name]
              elif "for-loop" in upstream_op_name:
                continue
              else:
                  raise ValueError('compiler cannot find the ' +
                                    upstream_op_name)

              upstream_groups, downstream_groups = self._get_uncommon_ancestors(
                  op_groups, opsgroups_groups, upstream_op, op)
              # Convert Argo condition DAG dependency into Tekton condition task dependency
              while len(upstream_groups) > 0 and 'condition-' in upstream_groups[0]:
                upstream_groups.pop(0)
              if len(upstream_groups) > 0:
                dependencies[downstream_groups[0]].add(upstream_groups[0])

      # Generate dependencies based on the recursive opsgroups
      # TODO: refactor the following codes with the above
      def _get_dependency_opsgroup(group, dependencies):
          upstream_op_names = set(
              [dependency.name for dependency in group.dependencies])
          if group.recursive_ref:
              for param in group.inputs + list(condition_params[group.name]):
                  if param.op_name:
                      upstream_op_names.add(param.op_name)

          for op_name in upstream_op_names:
              if op_name in pipeline.ops:
                  upstream_op = pipeline.ops[op_name]
              elif op_name in opsgroups:
                  upstream_op = opsgroups[op_name]
              else:
                  raise ValueError('compiler cannot find the ' + op_name)
              upstream_groups, downstream_groups = \
                self._get_uncommon_ancestors(op_groups, opsgroups_groups, upstream_op, group)
              # Convert Argo condition DAG dependency into Tekton condition task dependency
              while len(upstream_groups) > 0 and 'condition-' in upstream_groups[0]:
                upstream_groups.pop(0)
              if len(upstream_groups) > 0:
                dependencies[downstream_groups[0]].add(upstream_groups[0])

          for subgroup in group.groups:
              _get_dependency_opsgroup(subgroup, dependencies)

      _get_dependency_opsgroup(root_group, dependencies)

      return dependencies

  def _get_inputs_outputs(
          self,
          pipeline,
          root_group,
          op_groups,
          opsgroup_groups,
          condition_params,
          op_name_to_for_loop_op: Dict[Text, dsl.ParallelFor],
          opsgroups: Dict[str, dsl.OpsGroup]
  ):
    """Get inputs and outputs of each group and op.
    Returns:
      A tuple (inputs, outputs).
      inputs and outputs are dicts with key being the group/op names and values being list of
      tuples (param_name, producing_op_name). producing_op_name is the name of the op that
      produces the param. If the param is a pipeline param (no producer op), then
      producing_op_name is None.
    """
    inputs = defaultdict(set)
    outputs = defaultdict(set)

    for op in pipeline.ops.values():
      # op's inputs and all params used in conditions for that op are both considered.
      for param in op.inputs + list(condition_params[op.name]):
        # if the value is already provided (immediate value), then no need to expose
        # it as input for its parent groups.
        if param.value:
          continue
        if param.op_name:
          upstream_op = pipeline.ops.get(param.op_name, None)
          if not upstream_op:
            continue
          upstream_groups, downstream_groups = \
            self._get_uncommon_ancestors(op_groups, opsgroup_groups, upstream_op, op)
          for i, group_name in enumerate(downstream_groups):
            # Important: Changes for Tekton custom tasks
            # Custom task condition are not pods running in Tekton. Thus it should also
            # be considered as the first uncommon downstream group.
            def is_parent_custom_task(index):
              for group_name in downstream_groups[:index]:
                if 'condition-' in group_name:
                  return True
              return False
            if i == 0 or is_parent_custom_task(i):
              # If it is the first uncommon downstream group, then the input comes from
              # the first uncommon upstream group.
              inputs[group_name].add((param.full_name, upstream_groups[0]))
            else:
              # If not the first downstream group, then the input is passed down from
              # its ancestor groups so the upstream group is None.
              inputs[group_name].add((param.full_name, None))
          for i, group_name in enumerate(upstream_groups):
            if i == len(upstream_groups) - 1:
              # If last upstream group, it is an operator and output comes from container.
              outputs[group_name].add((param.full_name, None))
            else:
              # If not last upstream group, output value comes from one of its child.
              outputs[group_name].add((param.full_name, upstream_groups[i + 1]))
        else:
          if not op.is_exit_handler:
            for group_name in op_groups[op.name][::-1]:
              # if group is for loop group and param is that loop's param, then the param
              # is created by that for loop ops_group and it shouldn't be an input to
              # any of its parent groups.
              inputs[group_name].add((param.full_name, None))
              if group_name in op_name_to_for_loop_op:
                # for example:
                #   loop_group.loop_args.name = 'loop-item-param-99ca152e'
                #   param.name =                'loop-item-param-99ca152e--a'
                loop_group = op_name_to_for_loop_op[group_name]
                if loop_group.loop_args.name in param.name:
                  break
                # apply the same rule to iteration_number which is used by enumerate()
                # helper function. it shoudn't be an input to any of its parent groups
                if hasattr(loop_group, 'iteration_number') and loop_group.iteration_number and \
                    loop_group.iteration_number.full_name == param.name:
                  break
              elif group_name in opsgroups and isinstance(opsgroups[group_name], AddOnGroup) and \
                  param.name in opsgroups[group_name].params:
                # if group is AddOnGroup and the param is in its params list, then the param
                # is created by that AddOnGroup and it shouldn't be an input to
                # any of its parent groups.
                break

    # Generate the input/output for recursive opsgroups
    # It propagates the recursive opsgroups IO to their ancester opsgroups
    def _get_inputs_outputs_recursive_opsgroup(group):
      # TODO: refactor the following codes with the above
      if group.recursive_ref:
        params = [(param, False) for param in group.inputs]
        params.extend([(param, True) for param in list(condition_params[group.name])])
        for param, is_condition_param in params:
          if param.value:
            continue
          full_name = param.full_name
          if param.op_name:
            upstream_op = pipeline.ops[param.op_name]
            upstream_groups, downstream_groups = \
              self._get_uncommon_ancestors(op_groups, opsgroup_groups, upstream_op, group)
            for i, g in enumerate(downstream_groups):
              if i == 0:
                inputs[g].add((full_name, upstream_groups[0]))
              # There is no need to pass the condition param as argument to the downstream ops.
              # TODO: this might also apply to ops. add a TODO here and think about it.
              elif i == len(downstream_groups) - 1 and is_condition_param:
                continue
              else:
                # For Tekton, do not append duplicated input parameters
                duplicated_downstream_group = False
                for group_name in inputs[g]:
                  if len(group_name) > 1 and group_name[0] == full_name:
                    duplicated_downstream_group = True
                    break
                if not duplicated_downstream_group:
                  inputs[g].add((full_name, None))
            for i, g in enumerate(upstream_groups):
              if i == len(upstream_groups) - 1:
                outputs[g].add((full_name, None))
              else:
                outputs[g].add((full_name, upstream_groups[i + 1]))
          elif not is_condition_param:
            for g in op_groups[group.name]:
              inputs[g].add((full_name, None))
      for subgroup in group.groups:
        _get_inputs_outputs_recursive_opsgroup(subgroup)

    _get_inputs_outputs_recursive_opsgroup(root_group)

    # Generate the input for SubGraph along with parallelfor
    for sub_graph in opsgroup_groups:
      if sub_graph in op_name_to_for_loop_op:
        # The opsgroup list is sorted with the farthest group as the first and
        # the opsgroup itself as the last. To get the latest opsgroup which is
        # not the opsgroup itself -2 is used.
        parent = opsgroup_groups[sub_graph][-2]
        if parent and parent.startswith('subgraph'):
          # propagate only op's pipeline param from subgraph to parallelfor
          loop_op = op_name_to_for_loop_op[sub_graph]
          pipeline_param = loop_op.loop_args.items_or_pipeline_param
          if loop_op.items_is_pipeline_param and pipeline_param.op_name:
            param_name = '%s-%s' % (
              sanitize_k8s_name(pipeline_param.op_name), pipeline_param.name)
            inputs[parent].add((param_name, pipeline_param.op_name))

    return inputs, outputs

  def _process_resourceOp(self, task_refs, pipeline):
    """
    This function is used to handle resourceOp cases in pipeline.
    It will add the action, merge_strategy, success_condition, failure_condition, set_owner_reference, output to the task params.
    Args:
        task_refs: the task_refs in pipeline.
        pipeline: the pipeline object
    Returns:
        None
    """
    for task in task_refs:
      op = pipeline.ops.get(task['name'])
      if isinstance(op, dsl.ResourceOp):
        action = op.resource.get('action')
        merge_strategy = op.resource.get('merge_strategy')
        success_condition = op.resource.get('successCondition')
        failure_condition = op.resource.get('failureCondition')
        set_owner_reference = op.resource.get('setOwnerReference')
        task['params'] = [tp for tp in task.get('params', []) if tp.get('name') != "image"]
        if not merge_strategy:
          task['params'] = [tp for tp in task.get('params', []) if tp.get('name') != 'merge-strategy']
        if not success_condition:
          task['params'] = [tp for tp in task.get('params', []) if tp.get('name') != 'success-condition']
        if not failure_condition:
          task['params'] = [tp for tp in task.get('params', []) if tp.get('name') != "failure-condition"]
        if not set_owner_reference:
          task['params'] = [tp for tp in task.get('params', []) if tp.get('name') != "set-ownerreference"]
        for tp in task.get('params', []):
          if tp.get('name') == "action" and action:
            tp['value'] = action
          if tp.get('name') == "merge-strategy" and merge_strategy:
            tp['value'] = merge_strategy
          if tp.get('name') == "success-condition" and success_condition:
            tp['value'] = success_condition
          if tp.get('name') == "failure-condition" and failure_condition:
            tp['value'] = failure_condition
          if tp.get('name') == "set-ownerreference" and set_owner_reference:
            tp['value'] = set_owner_reference
          if tp.get('name') == "output":
            output_values = ''
            for value in sorted(list(op.attribute_outputs.items()), key=lambda x: x[0]):
              output_value = textwrap.dedent("""\
                    - name: %s
                      valueFrom: '%s'
              """ % (value[0], value[1]))
              output_values += output_value
            tp['value'] = output_values

  def _create_pipeline_workflow(self, args, pipeline, op_transformers=None, pipeline_conf=None) \
          -> Dict[Text, Any]:
    """Create workflow for the pipeline."""
    # Input Parameters
    params = []
    for arg in args:
      param = {'name': arg.name}
      if arg.value is not None:
        if isinstance(arg.value, (list, tuple, dict)):
          param['default'] = json.dumps(arg.value, sort_keys=True)
        else:
          param['default'] = str(arg.value)
      params.append(param)

    # generate Tekton tasks from pipeline ops
    raw_templates = self._create_dag_templates(pipeline, op_transformers, params)

    # generate task and condition reference list for the Tekton Pipeline
    condition_refs = {}

    task_refs = []
    cel_conditions = {}
    condition_when_refs = {}
    condition_task_refs = {}
    string_condition_refs = {}
    for template in raw_templates:
      if template['kind'] == 'Condition':
        if DISABLE_CEL_CONDITION:
          condition_task_spec = _get_super_condition_template(self.condition_image_name)
        else:
          condition_task_spec = _get_cel_condition_template()

        condition_params = template['spec'].get('params', [])
        if condition_params:
          condition_task_ref = [{
              'name': template['metadata']['name'],
              'params': [{
                  'name': p['name'],
                  'value': p.get('value', '')
                } for p in template['spec'].get('params', [])
              ],

              'taskSpec' if DISABLE_CEL_CONDITION else 'taskRef': condition_task_spec
          }]
          condition_refs[template['metadata']['name']] = [
              {
                'input': '$(tasks.%s.results.%s)' % (template['metadata']['name'], DEFAULT_CONDITION_OUTPUT_KEYWORD),
                'operator': 'in',
                'values': ['true']
              }
            ]
          # Don't use additional task if it's only doing literal string == and !=
          # with CEL custom task output.
          condition_operator = condition_params[2]
          condition_operand1 = condition_params[0]
          condition_operand2 = condition_params[1]
          conditionOp_mapping = {"==": "in", "!=": "notin"}
          if condition_operator.get('value', '') in conditionOp_mapping.keys():
            # Check whether the operand is an output from custom task
            # If so, don't create a new task to verify the condition.
            def is_custom_task_output(operand) -> bool:
              if operand['type'] == dsl.PipelineParam:
                for template in raw_templates:
                  if operand['op_name'] == template['metadata']['name']:
                    for step in template['spec']['steps']:
                      if step['name'] == 'main' and step['image'] in TEKTON_CUSTOM_TASK_IMAGES:
                        return True
              return False
            if is_custom_task_output(condition_operand1) or is_custom_task_output(condition_operand2):
              def map_cel_vars(a):
                if a.get('type', '') == dsl.PipelineParam:
                  op_name = sanitize_k8s_name(a['op_name'])
                  output_name = sanitize_k8s_name(a['output_name'], allow_capital=True)
                  return '$(tasks.%s.results.%s)' % (op_name, output_name)
                else:
                  return a.get('value', '')

              condition_refs[template['metadata']['name']] = [
                  {
                    'input': map_cel_vars(condition_operand1),
                    'operator': conditionOp_mapping[condition_operator['value']],
                    'values': [map_cel_vars(condition_operand2)]
                  }
                ]
              string_condition_refs[template['metadata']['name']] = True
          condition_task_refs[template['metadata']['name']] = condition_task_ref
          condition_when_refs[template['metadata']['name']] = condition_refs[template['metadata']['name']]
      else:
        task_ref = {
            'name': template['metadata']['name'],
            'params': [{
                'name': p['name'],
                'value': p.get('default', '')
              } for p in template['spec'].get('params', [])
            ],
            'taskSpec': template['spec'],
          }
        if template['spec'].get("workspaces"):
          workspaces_spec = []
          for item in template['spec']['workspaces']:
            workspaces_spec.append({"name": item["name"], "workspace": item["name"]})
            self.task_workspaces[item["name"]] = True
          task_ref['workspaces'] = workspaces_spec

        for i in template['spec'].get('steps', []):
          # TODO: change the below conditions to map with a label
          #       or a list of images with optimized actions
          if i.get('image', '') in TEKTON_CUSTOM_TASK_IMAGES:
            custom_task_args = {}
            container_args = i.get('args', [])
            custom_task_command = {}
            container_command = i.get('command', [])

            def find_parameter(arg_list, arg_task_list):
              skip_index = False
              for index, item in enumerate(arg_list):
                if skip_index:
                  skip_index = False
                  continue
                if item.startswith('--'):
                  arg_task_list[item[2:]] = arg_list[index + 1]
                  skip_index = True
            find_parameter(container_args, custom_task_args)
            find_parameter(container_command, custom_task_command)
            non_param_keys = ['name', 'apiVersion', 'kind', 'taskSpec', 'taskRef']
            task_params = []
            command_params = []
            for key, value in custom_task_command.items():
              task_params.append({'name': key, 'value': value})
              # Parameters in command spec get higher priority
              command_params.append(key)
            for key, value in custom_task_args.items():
              if key not in non_param_keys and key not in command_params:
                task_params.append({'name': key, 'value': value})
            task_orig_params = task_ref['params']
            task_ref = {
              'name': template['metadata']['name'],
              'params': task_params,
              # For processing Tekton parameter mapping later on.
              'orig_params': task_orig_params,
              'taskRef': {
                'name': custom_task_args['name'],
                'apiVersion': custom_task_args['apiVersion'],
                'kind': custom_task_args['kind']
              }
            }

            # Only one of --taskRef and --taskSpec allowed.
            if custom_task_args.get('taskRef', '') and custom_task_args.get('taskSpec', ''):
              raise ("Custom task invalid configuration %s, Only one of --taskRef and --taskSpec allowed." % custom_task_args)
            if custom_task_args.get('taskRef', ''):
              try:
                custom_task_cr = {
                  'apiVersion': custom_task_args['apiVersion'],
                  'kind': custom_task_args['kind'],
                  'metadata': {
                    'name': custom_task_args['name']
                  },
                  'spec': ast.literal_eval(custom_task_args['taskRef'])
                }
                for existing_cr in self.custom_task_crs:
                  if existing_cr == custom_task_cr:
                    # Skip duplicated CR resource
                    custom_task_cr = {}
                    break
                if custom_task_cr:
                  self.custom_task_crs.append(custom_task_cr)
              except ValueError:
                raise ("Custom task ref %s is not a valid Python Dictionary" % custom_task_args['taskRef'])
            # Setting --taskRef flag indicates, that spec be inlined.
            if custom_task_args.get('taskSpec', ''):
              try:
                task_ref = {
                  'name': template['metadata']['name'],
                  'params': task_params,
                  'orig_params': task_orig_params,
                  'taskSpec': {
                    'apiVersion': custom_task_args['apiVersion'],
                    'kind': custom_task_args['kind'],
                    'spec': ast.literal_eval(custom_task_args['taskSpec'])
                  }
                }
              except ValueError:
                raise ("Custom task spec %s is not a valid Python Dictionary" % custom_task_args['taskSpec'])
            # Pop custom task artifacts since we have no control of how
            # custom task controller is handling the container/task execution.
            self.artifact_items.pop(template['metadata']['name'], None)
            self.output_artifacts.pop(template['metadata']['name'], None)
            break
        if task_ref.get('taskSpec', ''):
          task_ref['taskSpec']['metadata'] = task_ref['taskSpec'].get('metadata', {})
          task_labels = template['metadata'].get('labels', {})
          cache_default = self.pipeline_labels.get('pipelines.kubeflow.org/cache_enabled', 'true')
          task_labels['pipelines.kubeflow.org/cache_enabled'] = task_labels.get('pipelines.kubeflow.org/cache_enabled', cache_default)
          task_annotations = template['metadata'].get('annotations', {})

          # Updata default metadata at the end.
          if task_labels:
            task_ref['taskSpec']['metadata']['labels'] = task_labels
          if task_annotations:
            task_ref['taskSpec']['metadata']['annotations'] = task_annotations

        task_refs.append(task_ref)

    # process input parameters from upstream tasks for conditions and pair conditions with their ancestor conditions
    opsgroup_stack = [pipeline.groups[0]]
    condition_stack = [None]
    while opsgroup_stack:
      cur_opsgroup = opsgroup_stack.pop()
      most_recent_condition = condition_stack.pop()

      if cur_opsgroup.type == 'condition':
        condition_task_ref = condition_task_refs[cur_opsgroup.name][0]
        condition = cur_opsgroup.condition
        input_params = []
        if not cel_conditions.get(condition_task_ref['name'], None):
          # Process input parameters if needed
          if isinstance(condition.operand1, dsl.PipelineParam):
            if condition.operand1.op_name:
              operand_value = '$(tasks.' + condition.operand1.op_name + '.results.' + \
                               sanitize_k8s_name(condition.operand1.name, allow_capital=True) + ')'
            else:
              operand_value = '$(params.' + condition.operand1.name + ')'
            input_params.append(operand_value)
          if isinstance(condition.operand2, dsl.PipelineParam):
            if condition.operand2.op_name:
              operand_value = '$(tasks.' + condition.operand2.op_name + '.results.' + \
                               sanitize_k8s_name(condition.operand2.name, allow_capital=True) + ')'
            else:
              operand_value = '$(params.' + condition.operand2.name + ')'
            input_params.append(operand_value)
        for param_iter in range(len(input_params)):
          # Add ancestor conditions to the current condition ref
          if most_recent_condition:
            add_ancestor_conditions = True
            # Do not add ancestor conditions if the ancestor is not in the same graph/pipelineloop
            for pipeline_loop in self.loops_pipeline.values():
              if condition_task_ref['name'] in pipeline_loop['task_list']:
                if most_recent_condition not in pipeline_loop['task_list']:
                  add_ancestor_conditions = False
            if add_ancestor_conditions:
              condition_task_ref['when'] = condition_when_refs[most_recent_condition]
          condition_task_ref['params'][param_iter]['value'] = input_params[param_iter]
        if not DISABLE_CEL_CONDITION and not cel_conditions.get(condition_task_ref['name'], None):
          # Type processing are done on the CEL controller since v1 SDK doesn't have value type for conditions.
          # For v2 SDK, it would be better to process the condition value type in the backend compiler.
          var1 = condition_task_ref['params'][0]['value']
          var2 = condition_task_ref['params'][1]['value']
          op = condition_task_ref['params'][2]['value']
          condition_task_ref['params'] = [{
                  'name': DEFAULT_CONDITION_OUTPUT_KEYWORD,
                  'value': " ".join([var1, op, var2])
                }]
        most_recent_condition = cur_opsgroup.name
      opsgroup_stack.extend(cur_opsgroup.groups)
      condition_stack.extend([most_recent_condition for x in range(len(cur_opsgroup.groups))])
    # add task dependencies and add condition refs to the task ref that depends on the condition
    op_name_to_parent_groups = self._get_groups_for_ops(pipeline.groups[0])
    for task in task_refs:
      op = pipeline.ops.get(task['name'])
      parent_group = op_name_to_parent_groups.get(task['name'], [])
      if parent_group:
        if condition_refs.get(parent_group[-2], []):
          task['when'] = condition_refs.get(op_name_to_parent_groups[task['name']][-2], [])
          # Travser the rest of the parent indices to check whether there are nested when conditions
          depended_conditions = []
          # flatten condition ref to list
          condition_task_refs_temp = []
          for condition_task_ref in condition_task_refs.values():
            for ref in condition_task_ref:
              if not string_condition_refs.get(ref['name'], False):
                condition_task_refs_temp.append(ref)

          # Get depended parent when expression
          def get_when_task(input_task_when, depended_conditions):
            when_task_name = re.findall('\$\(tasks.([^ \t\n.:,;{}]+).results.([^ \t\n.:,;{}]+)\)', input_task_when)
            if when_task_name:
              for when_task in task_refs + condition_task_refs_temp:
                if when_task['name'] == when_task_name[0][0]:
                  if when_task.get('when', []):
                    for when_dependent in when_task['when']:
                      if when_dependent.get("input", ""):
                        depended_conditions.append(when_dependent.get("input", ""))
          get_when_task(task['when'][0].get("input", ""), depended_conditions)
          parent_index = -3
          while abs(parent_index) <= len(op_name_to_parent_groups[task['name']]):
            if 'condition-' in op_name_to_parent_groups[task['name']][parent_index]:
              # If the nested when conditions already have parent when dependency, then skip
              for when_exp in condition_refs.get(op_name_to_parent_groups[task['name']][parent_index], []):
                get_when_task(when_exp.get("input", ""), depended_conditions)
                if when_exp.get("input", ""):
                  if when_exp.get("input", "") not in depended_conditions:
                    task['when'].append(when_exp)
                else:
                  task['when'].append(when_exp)
              parent_index -= 1
            else:
              break
      if op != None and op.dependent_names:
        task['runAfter'] = op.dependent_names

    # add condition refs to the recursive refs that depends on the condition
    for recursive_task in self.recursive_tasks:
      parent_group = op_name_to_parent_groups.get(recursive_task['name'], [])
      if parent_group:
        if condition_refs.get(parent_group[-2], []):
          recursive_task['when'] = condition_refs.get(op_name_to_parent_groups[recursive_task['name']][-2], [])
      recursive_task['name'] = sanitize_k8s_name(recursive_task['name'])

    # add condition refs to the pipelineloop refs that depends on the condition
    opgroup_name_to_parent_groups = self._get_groups_for_opsgroups(pipeline.groups[0])
    for loop_task_key in self.loops_pipeline.keys():
      task_name_prefix = '-'.join(self._group_names[:-1] + [""])
      raw_task_key = loop_task_key.replace(task_name_prefix, "", 1)
      for key in opgroup_name_to_parent_groups.keys():
        if raw_task_key in key:
          raw_task_key = key
          break
      parent_group = opgroup_name_to_parent_groups.get(raw_task_key, [])
      if parent_group:
        if condition_refs.get(parent_group[-2], []):
          self.loops_pipeline[loop_task_key]['spec']['when'] = condition_refs.get(parent_group[-2], [])
          # In nested recursive loop, the children of the loop pipeline can be both another loop
          # and the self recursive loop. Thus, we cannot simply pop unnecessary params in one
          # loop pipeline without verifying all the dependent parameters. Because nested recursion
          # can have cycles, the DSL DAG may not represent the full view of all the dependent parameters.
          # TODO: 1. Break any cycle in the nested recursion so it can represent as an acyclic graph.
          #       2. Once the graph is acyclic, check all the children parameters in the loop_task
          #          and pop the unnecessary parameters using the below logic.
          # for i, param in enumerate(self.loops_pipeline[loop_task_key]['spec']["params"]):
          #   if param["value"] == condition_refs.get(parent_group[-2], [])[0]["input"]:
          #     self.loops_pipeline[loop_task_key]['spec']["params"].pop(i)
          #     break

    # process input parameters from upstream tasks
    pipeline_param_names = [p['name'] for p in params]
    loop_args = [self.loops_pipeline[key]['loop_args'] for key in self.loops_pipeline.keys()]
    for key in self.loops_pipeline.keys():
      if self.loops_pipeline[key]['loop_sub_args'] != []:
        loop_args.extend(self.loops_pipeline[key]['loop_sub_args'])
      # borrow loop_args to also include iteration_number param
      # in this case, it would be treated as param
      if 'iteration_number' in self.loops_pipeline[key]:
        loop_args.append(self.loops_pipeline[key].get('iteration_number'))
    for task in task_refs:
      op = pipeline.ops.get(task['name'])
      # Substitute task paramters to the correct Tekton variables.
      # Regular task and custom task have different parameter mapping in Tekton.
      if 'orig_params' in task:  # custom task
        orig_params = [p['name'] for p in task.get('orig_params', [])]
        for tp in task.get('params', []):
          pipeline_params = re.findall('\$\(inputs.params.([^ \t\n.:,;{}]+)\)', tp.get('value', ''))
          # There could be multiple pipeline params in one expression, so we need to map each of them
          # back to the proper tekton variables.
          for pipeline_param in pipeline_params:
            if pipeline_param in orig_params:
              if pipeline_param in pipeline_param_names + loop_args:
                # Do not sanitize Tekton pipeline input parameters, only the output parameters need to be sanitized
                substitute_param = '$(params.%s)' % pipeline_param
                tp['value'] = re.sub('\$\(inputs.params.%s\)' % pipeline_param, substitute_param, tp.get('value', ''))
              else:
                for pp in op.inputs:
                  if pipeline_param == pp.full_name:
                    # Parameters from Tekton results need to be sanitized
                    substitute_param = ''
                    if pp.op_name:
                      substitute_param = '$(tasks.%s.results.%s)' % (sanitize_k8s_name(pp.op_name),
                                                                     sanitize_k8s_name(pp.name, allow_capital=True))
                    else:
                      substitute_param = '$(params.%s)' % pipeline_param
                    tp['value'] = re.sub('\$\(inputs.params.%s\)' % pipeline_param, substitute_param, tp.get('value', ''))
                    break
        # Not necessary for Tekton execution
        task.pop('orig_params', None)
      else:  # regular task
        op = pipeline.ops.get(task['name'])
        for tp in task.get('params', []):
          if tp['name'] in pipeline_param_names + loop_args:
            tp['value'] = '$(params.%s)' % tp['name']
          else:
            for pp in op.inputs:
              if tp['name'] == pp.full_name:
                tp['value'] = '$(tasks.%s.results.%s)' % (pp.op_name, pp.name)
                # Create input artifact tracking annotation
                input_annotation = self.input_artifacts.get(task['name'], [])
                input_annotation.append(
                    {
                        'name': tp['name'],
                        'parent_task': pp.op_name
                    }
                )
                self.input_artifacts[task['name']] = input_annotation
                break

    # add retries params
    for task in task_refs:
      op = pipeline.ops.get(task['name'])
      if op != None and op.num_retries:
        task['retries'] = op.num_retries

    # add timeout params to task_refs, instead of task.
    for task in task_refs:
      op = pipeline.ops.get(task['name'])
      if hasattr(op, 'timeout') and op.timeout > 0:
        task['timeout'] = '%ds' % op.timeout

    # handle resourceOp cases in pipeline
    self._process_resourceOp(task_refs, pipeline)

    # handle exit handler in pipeline
    finally_tasks = []
    for task in task_refs:
      op = pipeline.ops.get(task['name'])
      if op != None and op.is_exit_handler:
        finally_tasks.append(task)
    task_refs = [task for task in task_refs if pipeline.ops.get(task['name']) and not pipeline.ops.get(task['name']).is_exit_handler]

    # Flatten condition task
    condition_task_refs_temp = []
    for condition_task_ref in condition_task_refs.values():
      for ref in condition_task_ref:
        if not string_condition_refs.get(ref['name'], False):
          condition_task_refs_temp.append(ref)
    condition_task_refs = condition_task_refs_temp

    pipeline_run = {
      'apiVersion': tekton_api_version,
      'kind': 'PipelineRun',
      'metadata': {
        'name': sanitize_k8s_name(pipeline.name or 'Pipeline', suffix_space=4),
        # Reflect the list of Tekton pipeline annotations at the top
        'annotations': {
          'tekton.dev/output_artifacts': json.dumps(self.output_artifacts, sort_keys=True),
          'tekton.dev/input_artifacts': json.dumps(self.input_artifacts, sort_keys=True),
          'tekton.dev/artifact_bucket': DEFAULT_ARTIFACT_BUCKET,
          'tekton.dev/artifact_endpoint': DEFAULT_ARTIFACT_ENDPOINT,
          'tekton.dev/artifact_endpoint_scheme': DEFAULT_ARTIFACT_ENDPOINT_SCHEME,
          'tekton.dev/artifact_items': json.dumps(self.artifact_items, sort_keys=True),
          'sidecar.istio.io/inject': 'false'  # disable Istio inject since Tekton cannot run with Istio sidecar
        }
      },
      'spec': {
        'params': [{
            'name': p['name'],
            'value': p.get('default', '')
          } for p in sorted(params, key=lambda x: x['name'])],
        'pipelineSpec': {
          'params': sorted(params, key=lambda x: x['name']),
          'tasks': task_refs + condition_task_refs,
          'finally': finally_tasks
        }
      }
    }

    if env.get('DISABLE_ARTIFACT_TRACKING', 'false').lower() == 'true':
      pipeline_run['metadata']['annotations'].pop('tekton.dev/output_artifacts', None)
      pipeline_run['metadata']['annotations'].pop('tekton.dev/input_artifacts', None)
      pipeline_run['metadata']['annotations'].pop('tekton.dev/artifact_items', None)

    if self.pipeline_labels:
      pipeline_run['metadata']['labels'] = pipeline_run['metadata'].setdefault('labels', {})
      pipeline_run['metadata']['labels'].update(self.pipeline_labels)
      # Remove pipeline level label for 'pipelines.kubeflow.org/cache_enabled' as it overwrites task level label
      pipeline_run['metadata']['labels'].pop('pipelines.kubeflow.org/cache_enabled', None)

    # Add big data passing path format
    self.pipeline_annotations['pipelines.kubeflow.org/big_data_passing_format'] = BIG_DATA_PATH_FORMAT

    if self.pipeline_annotations:
      pipeline_run['metadata'].setdefault('annotations', {})
      pipeline_run['metadata']['annotations'].update(self.pipeline_annotations)

    def python_name_to_yaml_name(name: str):
      return re.sub(r'_([a-z])', lambda x: x.group(1).upper(), name)

    if self.security_context:
      pipeline_run['spec'].setdefault('taskRunTemplate', {})
      pipeline_run['spec']['taskRunTemplate'].setdefault('podTemplate', {})
      for key, value in self.security_context.to_dict().items():
        if value is not None:
          pipeline_run['spec']['taskRunTemplate']['podTemplate']['securityContext'] = \
            pipeline_run['spec']['taskRunTemplate']['podTemplate'].setdefault('securityContext', {})
          pipeline_run['spec']['taskRunTemplate']['podTemplate']['securityContext'][python_name_to_yaml_name(key)] = value
    if self.automount_service_account_token is not None:
      pipeline_run['spec'].setdefault('taskRunTemplate', {})
      pipeline_run['spec']['taskRunTemplate'].setdefault('podTemplate', {})
      pipeline_run['spec']['taskRunTemplate']['podTemplate']['automountServiceAccountToken'] = self.automount_service_account_token
    if self.pipeline_env:
      pipeline_run['spec'].setdefault('taskRunTemplate', {})
      pipeline_run['spec']['taskRunTemplate'].setdefault('podTemplate', {})
      pipeline_run['spec']['taskRunTemplate']['podTemplate'].setdefault('env', [])
      for key, value in self.pipeline_env.items():
        pipeline_run['spec']['taskRunTemplate']['podTemplate']['env'].append({'name': key, 'value': value})

    # Generate TaskRunSpec PodTemplate:s
    task_run_spec = []
    for task in task_refs:

      # TODO: should loop-item tasks be included here?
      if LoopArguments.LOOP_ITEM_NAME_BASE in task['name']:
        task_name = re.sub(r'-%s-.+$' % LoopArguments.LOOP_ITEM_NAME_BASE, '', task['name'])
      else:
        task_name = task['name']
      op = pipeline.ops.get(task_name)
      if not op:
        raise RuntimeError("unable to find op with name '%s'" % task["name"])

      task_spec = {"pipelineTaskName": task['name'],
                   "podTemplate": {}}
      if op.affinity:
        task_spec["podTemplate"]["affinity"] = convert_k8s_obj_to_json(op.affinity)
      if op.tolerations:
        task_spec["podTemplate"]['tolerations'] = op.tolerations
      # process op level node_selector
      if op.node_selector:
        if task_spec["podTemplate"].get('nodeSelector'):
          task_spec["podTemplate"]['nodeSelector'].update(op.node_selector)
        else:
          task_spec["podTemplate"]['nodeSelector'] = op.node_selector
      if bool(task_spec["podTemplate"]):
        task_run_spec.append(task_spec)
    if len(task_run_spec) > 0:
      pipeline_run['spec']['taskRunSpecs'] = task_run_spec

    # add workflow level timeout to pipeline run
    if pipeline.conf.timeout and pipeline.conf.timeout > 0:
      pipeline_run['spec']['timeouts'] = {'pipeline': '0s', 'tasks': '0s'}
      pipeline_run['spec']['timeouts']['tasks'] = '%ds' % pipeline.conf.timeout
      pipeline_run['spec']['timeouts']['pipeline'] = '%ds' % (pipeline.conf.timeout + DEFAULT_FINALLY_SECONDS)
    # generate the Tekton podTemplate for image pull secret
    if len(pipeline.conf.image_pull_secrets) > 0:
      pipeline_run['spec']['taskRunTemplate'] = pipeline_run['spec'].get('taskRunTemplate', {})
      pipeline_run['spec']['taskRunTemplate']['podTemplate'] = pipeline_run['spec']['taskRunTemplate'].get('podTemplate', {})
      pipeline_run['spec']['taskRunTemplate']['podTemplate']['imagePullSecrets'] = [
        {"name": s.name} for s in pipeline.conf.image_pull_secrets]
    # process pipeline level node_selector
    if pipeline_conf and hasattr(pipeline_conf, 'default_pod_node_selector') \
        and len(pipeline_conf.default_pod_node_selector) > 0:
      pipeline_run['spec']['taskRunTemplate'] = pipeline_run['spec'].get('taskRunTemplate', {})
      pipeline_run['spec']['taskRunTemplate']['podTemplate'] = pipeline_run['spec']['taskRunTemplate'].get('podTemplate', {})
      pipeline_run['spec']['taskRunTemplate']['podTemplate']['nodeSelector'] = copy.deepcopy(pipeline_conf.default_pod_node_selector)
    workflow = pipeline_run

    # populate dependend condition for all the runafter tasks
    def populate_runafter_condition(task):
      task_runafter = task.get('runAfter')
      if task_runafter:
        for t in workflow['spec']['pipelineSpec']['tasks']:
          if t['name'] in task_runafter:
            if t.get('when'):
              task.setdefault('when', [])
              for when_item in t['when']:
                if when_item not in task['when']:
                  add_conditions = True
                  # Do not add condition if the condition is not in the same graph/pipelineloop
                  for pipeline_loop in self.loops_pipeline.values():
                    if task['name'] in pipeline_loop['task_list']:
                      task_input = re.findall('\$\(tasks.([^ \t\n.:,;{}]+).results.([^ \t\n.:,;{}]+)\)', when_item['input'])
                      if task_input and task_input[0][0] not in pipeline_loop['task_list']:
                        add_conditions = False
                  if add_conditions:
                    task['when'].append(when_item)

    # search runafter tree logic before populating the condition
    visited_tasks = {}
    task_queue = []
    for task in workflow['spec']['pipelineSpec']['tasks']:
      task_runafter = task.get('runAfter')
      if task_runafter:
        task_queue.append(task)
    while task_queue:
      popped_task = task_queue.pop(0)
      populate_condition = True
      for queued_task in task_queue:
        if queued_task['name'] in popped_task['runAfter'] and len(task_queue) != visited_tasks.get(popped_task['name']):
          visited_tasks[popped_task['name']] = len(task_queue)
          task_queue.append(popped_task)
          populate_condition = False
          break
      if populate_condition:
        populate_runafter_condition(popped_task)

    return workflow

  def _sanitize_and_inject_artifact(self, pipeline: dsl.Pipeline, pipeline_conf=None):
    """Sanitize operator/param names and inject pipeline artifact location."""

    # Sanitize operator names and param names
    sanitized_ops = {}

    for op in pipeline.ops.values():
      if len(op.name) > 57:
        raise ValueError('Input ops cannot be longer than 57 characters. \
             \nOp name: %s' % op.name)
      sanitized_name = sanitize_k8s_name(op.name)
      op.name = sanitized_name
      # check sanitized input params
      for param in op.inputs:
        if param.op_name:
          if len(param.op_name) > 128:
            raise ValueError('Input parameter cannot be longer than 128 characters. \
             \nInput name: %s. \nOp name: %s' % (param.op_name, op.name))
          param.op_name = sanitize_k8s_name(param.op_name, max_length=float('inf'), allow_capital=True)
      # sanitized output params
      for param in op.outputs.values():
        param.name = sanitize_k8s_name(param.name, True)
        if param.op_name:
          param.op_name = sanitize_k8s_name(param.op_name, allow_capital=True)
      if op.output is not None and not isinstance(op.output, dsl._container_op._MultipleOutputsError):
        op.output.name = sanitize_k8s_name(op.output.name, True)
        op.output.op_name = sanitize_k8s_name(op.output.op_name, allow_capital=True)
      if op.dependent_names:
        op.dependent_names = [sanitize_k8s_name(name) for name in op.dependent_names]
      if isinstance(op, dsl.ContainerOp) and op.file_outputs is not None:
        sanitized_file_outputs = {}
        for key in op.file_outputs.keys():
          sanitized_file_outputs[sanitize_k8s_name(key, True)] = op.file_outputs[key]
        op.file_outputs = sanitized_file_outputs
      elif isinstance(op, dsl.ResourceOp) and op.attribute_outputs is not None:
        sanitized_attribute_outputs = {}
        for key in op.attribute_outputs.keys():
          sanitized_attribute_outputs[sanitize_k8s_name(key, True)] = \
            op.attribute_outputs[key]
        op.attribute_outputs = sanitized_attribute_outputs
      if isinstance(op, dsl.ContainerOp) and op.container is not None:
        sanitize_k8s_object(op.container)
      sanitized_ops[sanitized_name] = op
    pipeline.ops = sanitized_ops

  # NOTE: the methods below are "copied" from KFP with changes in the method signatures (only)
  #       to accommodate multiple documents in the YAML output file:
  #         KFP Argo -> Dict[Text, Any]
  #         KFP Tekton -> List[Dict[Text, Any]]

  def _create_workflow(self,
                       pipeline_func: Callable,
                       pipeline_name: Text = None,
                       pipeline_description: Text = None,
                       params_list: List[dsl.PipelineParam] = None,
                       pipeline_conf: dsl.PipelineConf = None,
                       ) -> Dict[Text, Any]:
    """ Internal implementation of create_workflow."""
    params_list = params_list or []
    argspec = inspect.getfullargspec(pipeline_func)

    # Create the arg list with no default values and call pipeline function.
    # Assign type information to the PipelineParam
    pipeline_meta = _extract_pipeline_metadata(pipeline_func)
    pipeline_meta.name = pipeline_name or pipeline_meta.name
    pipeline_meta.description = pipeline_description or pipeline_meta.description
    pipeline_name = sanitize_k8s_name(pipeline_meta.name)

    # Need to first clear the default value of dsl.PipelineParams. Otherwise, it
    # will be resolved immediately in place when being to each component.
    default_param_values = {}
    for param in params_list:
      default_param_values[param.name] = param.value
      param.value = None

    # Currently only allow specifying pipeline params at one place.
    if params_list and pipeline_meta.inputs:
      raise ValueError('Either specify pipeline params in the pipeline function, or in "params_list", but not both.')

    args_list = []
    for arg_name in argspec.args:
      arg_type = None
      for p_input in pipeline_meta.inputs or []:
        if arg_name == p_input.name:
          arg_type = p_input.type
          break
      args_list.append(dsl.PipelineParam(sanitize_k8s_name(arg_name, True), param_type=arg_type))

    with dsl.Pipeline(pipeline_name) as dsl_pipeline:
      pipeline_func(*args_list)

    # Configuration passed to the compiler is overriding. Unfortunately, it is
    # not trivial to detect whether the dsl_pipeline.conf was ever modified.
    pipeline_conf = pipeline_conf or dsl_pipeline.conf

    self._validate_exit_handler(dsl_pipeline)
    self._sanitize_and_inject_artifact(dsl_pipeline, pipeline_conf)

    # Fill in the default values.
    args_list_with_defaults = []
    if pipeline_meta.inputs:
      args_list_with_defaults = [dsl.PipelineParam(sanitize_k8s_name(arg_name, True))
                                 for arg_name in argspec.args]
      if argspec.defaults:
        for arg, default in zip(reversed(args_list_with_defaults), reversed(argspec.defaults)):
          arg.value = default.value if isinstance(default, dsl.PipelineParam) else default
    elif params_list:
      # Or, if args are provided by params_list, fill in pipeline_meta.
      for param in params_list:
        param.value = default_param_values[param.name]

      args_list_with_defaults = params_list
      pipeline_meta.inputs = [
        InputSpec(
            name=param.name,
            type=param.param_type,
            default=param.value) for param in params_list]

    op_transformers = [add_pod_env]

    op_transformers.extend(pipeline_conf.op_transformers)

    workflow = self._create_pipeline_workflow(
        args_list_with_defaults,
        dsl_pipeline,
        op_transformers,
        pipeline_conf,
    )

    workflow = fix_big_data_passing(workflow, self.loops_pipeline, '-'.join(self._group_names[:-1] + [""]))

    if pipeline_conf and pipeline_conf.data_passing_method is not None:
      workflow = fix_big_data_passing_using_volume(workflow, pipeline_conf)

    # Inject user defined pipeline level workspaces
    if self.pipeline_workspaces:
      workflow['spec'].setdefault('workspaces', [])
      workflow['spec']["pipelineSpec"].setdefault('workspaces', [])
      for key, value in self.pipeline_workspaces.items():
        workspaceSpec = {"name": key}
        if isinstance(value[0], V1Volume):
          workspaceSpec = client.ApiClient().sanitize_for_serialization(value[0])
          workspaceSpec["name"] = key
        else:
          workspaceSpec['volumeClaimTemplate'] = {"spec": client.ApiClient().sanitize_for_serialization(value[0])}
        if value[1]:
          workspaceSpec['subPath'] = value[1]
        workflow['spec']['workspaces'].append(workspaceSpec)
        workflow['spec']["pipelineSpec"]['workspaces'].append({"name": workspaceSpec["name"]})
    
    workspace_list = [workspace['name'] for workspace in workflow['spec']["pipelineSpec"].get("workspaces", [])]
    for key in self.task_workspaces:
      if key not in workspace_list:
        raise ValueError("Missing workspace %s in the Tekton pipeline config object." % key)

    if pipeline_conf and pipeline_conf.timeout > 0:
      workflow['spec'].setdefault('timeouts', {'pipeline': '0s', 'tasks': '0s'})
      workflow['spec']['timeouts']['tasks'] = '%ds' % pipeline_conf.timeout
      workflow['spec']['timeouts']['pipeline'] = '%ds' % (pipeline_conf.timeout + DEFAULT_FINALLY_SECONDS)

    workflow.setdefault('metadata', {}).setdefault('annotations', {})['pipelines.kubeflow.org/pipeline_spec'] = \
      json.dumps(pipeline_meta.to_dict(), sort_keys=True)

    # recursively strip empty structures, DANGER: this may remove necessary empty elements ?!
    def remove_empty_elements(obj) -> dict:
      if not isinstance(obj, (dict, list)):
        return obj
      if isinstance(obj, list):
        return [remove_empty_elements(o) for o in obj if o != []]
      return {k: remove_empty_elements(v) for k, v in obj.items()
              if v != []}

    workflow = remove_empty_elements(workflow)

    return workflow

  def compile(self,
              pipeline_func,
              package_path,
              type_check=True,
              pipeline_conf: dsl.PipelineConf = None,
              tekton_pipeline_conf: TektonPipelineConf = None):
    """Compile the given pipeline function into workflow yaml.
    Args:
      pipeline_func: pipeline functions with @dsl.pipeline decorator.
      package_path: the output workflow tar.gz file path. for example, "~/a.tar.gz"
      type_check: whether to enable the type check or not, default: True.
      pipeline_conf: PipelineConf instance. Can specify op transforms,
                     image pull secrets and other pipeline-level configuration options.
                     Overrides any configuration that may be set by the pipeline.
    """
    if tekton_pipeline_conf:
      self._set_pipeline_conf(tekton_pipeline_conf)
    super().compile(pipeline_func, package_path, type_check, pipeline_conf=pipeline_conf)

  @staticmethod
  def _write_workflow(workflow: Dict[Text, Any],
                      package_path: Text = None):
    """Dump pipeline workflow into yaml spec and write out in the format specified by the user.

    Args:
      workflow: Workflow spec of the pipeline, dict.
      package_path: file path to be written. If not specified, a yaml_text string
        will be returned.
    """

    yaml_text = ""
    pipeline_run = workflow
    if pipeline_run.get("spec", {}) and pipeline_run["spec"].get("pipelineSpec", {}) and \
      pipeline_run["spec"]["pipelineSpec"].get("tasks", []):
      yaml_text = dump_yaml(_handle_tekton_pipeline_variables(pipeline_run))
    else:
      yaml_text = dump_yaml(workflow)

    # Convert Argo variables to Tekton variables.
    yaml_text = _process_argo_vars(yaml_text)
    unsupported_vars = re.findall(r"{{[^ \t\n.:,;{}]+\.[^ \t\n:,;{}]+}}", yaml_text)
    if unsupported_vars:
      raise ValueError('These Argo variables are not supported in Tekton Pipeline: %s' % ", ".join(str(v) for v in set(unsupported_vars)))
    if '{{pipelineparam' in yaml_text:
      raise RuntimeError(
          'Internal compiler error: Found unresolved PipelineParam. '
          'Please create a new issue at https://github.com/kubeflow/kfp-tekton/issues '
          'attaching the pipeline DSL code and the pipeline YAML.')

    if package_path is None:
      return yaml_text

    if package_path.endswith('.tar.gz') or package_path.endswith('.tgz'):
      from contextlib import closing
      from io import BytesIO
      with tarfile.open(package_path, "w:gz") as tar:
          with closing(BytesIO(yaml_text.encode())) as yaml_file:
            tarinfo = tarfile.TarInfo('pipeline.yaml')
            tarinfo.size = len(yaml_file.getvalue())
            tar.addfile(tarinfo, fileobj=yaml_file)
    elif package_path.endswith('.zip'):
      with zipfile.ZipFile(package_path, "w") as zip:
        zipinfo = zipfile.ZipInfo('pipeline.yaml')
        zipinfo.compress_type = zipfile.ZIP_DEFLATED
        zip.writestr(zipinfo, yaml_text)
    elif package_path.endswith('.yaml') or package_path.endswith('.yml'):
      with open(package_path, 'w') as yaml_file:
        yaml_file.write(yaml_text)
    else:
      raise ValueError(
          'The output path %s should end with one of the following formats: '
          '[.tar.gz, .tgz, .zip, .yaml, .yml]' % package_path)

  def prepare_workflow(self,
                       pipeline_func: Callable,
                       pipeline_name: Text = None,
                       pipeline_description: Text = None,
                       params_list: List[dsl.PipelineParam] = None,
                       pipeline_conf: dsl.PipelineConf = None,
                       ):
    """Compile the given pipeline function and return a python Dict."""

    workflow = self._create_workflow(
      pipeline_func,
      pipeline_name,
      pipeline_description,
      params_list,
      pipeline_conf)

    # Separate loop workflow from the main workflow
    custom_opsgroup_crs = []
    if self.loops_pipeline or self.addon_groups:
      # get custom tasks from self.loops_pipeline and self.addon_groups
      custom_tasks = {**self.loops_pipeline, **self.addon_groups}
      custom_opsgroup_crs, workflow = _handle_tekton_custom_task(custom_tasks, workflow, self.recursive_tasks, self._group_names)
      if workflow['spec'].get('workspaces', []):
        for custom_opsgroup_cr in custom_opsgroup_crs:
          custom_opsgroup_cr['spec']['workspaces'] = workflow['spec'].get('workspaces', [])
          custom_opsgroup_cr['spec']['pipelineSpec']['workspaces'] = [
            {'name': workspace['name']} for workspace in workflow['spec'].get('workspaces', [])]
      inlined_as_taskSpec: List[Text] = []
      recursive_tasks_names: List[Text] = [x['taskRef'].get('name', "") for x in self.recursive_tasks]
      if self.tekton_inline_spec:
        # Step 1. inline all the custom_opsgroup_crs as they may refer to each other.
        for i in range(len(custom_opsgroup_crs)):
          if 'pipelineSpec' in custom_opsgroup_crs[i]['spec']:
            if 'params' in custom_opsgroup_crs[i]['spec']['pipelineSpec']:
              # Preserve order of params, required by tests.
              custom_opsgroup_crs[i]['spec']['pipelineSpec']['params'] =\
                sorted(custom_opsgroup_crs[i]['spec']['pipelineSpec']['params'], key=lambda kv: (kv['name']))
            t, e = self._inline_tasks(custom_opsgroup_crs[i]['spec']['pipelineSpec']['tasks'],
                                                custom_opsgroup_crs, recursive_tasks_names)
            if e:
              custom_opsgroup_crs[i]['spec']['pipelineSpec']['tasks'] = t
              inlined_as_taskSpec.extend(e)
        # Step 2. inline custom_opsgroup_crs in the workflow
        workflow_tasks, e = self._inline_tasks(workflow['spec']['pipelineSpec']['tasks'],
                                                         custom_opsgroup_crs, recursive_tasks_names)
        inlined_as_taskSpec.extend(e)

        # Step 3. handle AddOnGroup
        updated_workflow_tasks = []
        for task in workflow_tasks:
          add_on = self.addon_groups.get(task['name'])
          if add_on and add_on.get('_data') and isinstance(add_on.get('_data'), AddOnGroup) \
                and hasattr(add_on.get('_data'), 'is_finally'):

              addon_group_: AddOnGroup = add_on.get('_data')
              task['params'] = addon_group_.post_params(task.get('params', []))
              if not len(task['params']):
                task.pop('params')

              # inject labels and annotations
              metadata = task['taskSpec'].get('metadata', {})
              if len(addon_group_.annotations):
                metadata['annotations'] = metadata.get('annotations', {})
                metadata['annotations'].update(addon_group_.annotations)
                task['taskSpec']['metadata'] = metadata

              if len(addon_group_.labels):
                metadata['labels'] = metadata.get('labels', {})
                metadata['labels'].update(addon_group_.labels)
                task['taskSpec']['metadata'] = metadata

              # check if there is any custom_opsgroup has finally attribute
              if addon_group_.is_finally:
                workflow['spec']['pipelineSpec']['finally'] = workflow['spec']['pipelineSpec'].get('finally', [])
                # TODO: need to remove some properties that can't be used in 'finally'?
                task.pop('runAfter', None)
                workflow['spec']['pipelineSpec']['finally'].append(task)
                continue

          updated_workflow_tasks.append(task)

        workflow['spec']['pipelineSpec']['tasks'] = updated_workflow_tasks
        # Preserve order of params, required by tests.
        if 'params' in workflow['spec']:
          workflow['spec']['params'] = sorted(workflow['spec']['params'], key=lambda kv: (kv['name']))
        custom_opsgroup_crs = [cr for cr in custom_opsgroup_crs if cr['metadata'].get("name", "") not in inlined_as_taskSpec]
    return custom_opsgroup_crs, workflow

  def _create_and_write_workflow(self,
                                 pipeline_func: Callable,
                                 pipeline_name: Text = None,
                                 pipeline_description: Text = None,
                                 params_list: List[dsl.PipelineParam] = None,
                                 pipeline_conf: dsl.PipelineConf = None,
                                 package_path: Text = None,
                                 ) -> None:
    """Compile the given pipeline function and dump it to specified file format."""

    pipeline_loop_crs, workflow = self.prepare_workflow(
      pipeline_func,
      pipeline_name,
      pipeline_description,
      params_list,
      pipeline_conf)

    # create cr yaml for only those pipelineLoop cr which could not be converted to inlined spec.
    loop_package_annotations = []
    for i in range(len(pipeline_loop_crs)):
      if pipeline_loop_crs[i]['metadata'].get('name', ""):
        if self.resource_in_separate_yaml:
          TektonCompiler._write_workflow(workflow=pipeline_loop_crs[i],
                                         package_path=os.path.splitext(package_path)[0] +
                                                      "_pipelineloop_cr" + str(i + 1) + '.yaml')
        else:
          pipeline_loop_cr = TektonCompiler._write_workflow(workflow=collections.OrderedDict(pipeline_loop_crs[i]))
          loop_package_annotations.append(yaml.load(pipeline_loop_cr, Loader=yaml.FullLoader))
    if loop_package_annotations:
      workflow['metadata']['annotations']['tekton.dev/resource_templates'] = json.dumps(loop_package_annotations,
                                                                                        sort_keys=True)
    # Need to compiles after all the CRs being processed.
    # Convert taskspec into task templates if specified.
    if not self.produce_taskspec:
      component_sha = {}
      for task in workflow['spec']['pipelineSpec']['tasks']:
        if task.get('taskSpec'):
          component_spec_digest = hashlib.sha1(json.dumps(task['taskSpec'], sort_keys=True).encode()).hexdigest()
          if component_spec_digest not in component_sha.keys():
            task_template = {}
            task_template['metadata'] = {}
            if task['taskSpec'].get('metadata', None):
              task_template['metadata'] = task['taskSpec'].pop('metadata', None)
            if task['taskSpec'].get('apiVersion', None) and task['taskSpec'].get('kind', None):
              task_template['apiVersion'] = task['taskSpec']['apiVersion']
              task_template['kind'] = task['taskSpec']['kind']
            else:
              task_template['apiVersion'] = tekton_api_version
              task_template['kind'] = 'Task'
            task_template['spec'] = task['taskSpec']
            task_template['metadata']['name'] = component_spec_digest
            component_sha[component_spec_digest] = task_template
          task.pop("taskSpec", None)
          task['taskRef'] = {'name': component_spec_digest}
      # Output task templates into individual files if specified, else append task templates to annotations
      if self.resource_in_separate_yaml:
        for key, value in component_sha.items():
          TektonCompiler._write_workflow(workflow=value,
                                         package_path=os.path.splitext(package_path)[0] +
                                         key + '.yaml')
      else:
        resource_templates = workflow['metadata']['annotations'].get('tekton.dev/resource_templates', [])
        if resource_templates:
          resource_templates = json.loads(resource_templates)
        for value in component_sha.values():
          resource_templates.append(value)
        if resource_templates:
          workflow['metadata']['annotations']['tekton.dev/resource_templates'] = json.dumps(resource_templates,
                                                                                            sort_keys=True)

    # Inject uuid to loop parameter task name if exist
    if self.uuid:
      for k, v in self.group_names.items():
        if workflow['spec'].get('pipelineSpec'):
          for task in workflow['spec']['pipelineSpec']['tasks']:
            for param in task.get('params', []):
              if isinstance(param['value'], str):
                param['value'] = param['value'].replace(k, v)
    TektonCompiler._write_workflow(workflow=workflow, package_path=package_path)   # Tekton change

    # Separate custom task CR from the main workflow
    for i in range(len(self.custom_task_crs)):
      TektonCompiler._write_workflow(workflow=self.custom_task_crs[i],
                                    package_path=os.path.splitext(package_path)[0] +
                                                  "_customtask_cr" + str(i + 1) + '.yaml')
    _validate_workflow(workflow)

  def _inline_tasks(self, tasks: List[Dict[Text, Any]], crs: List[Dict[Text, Any]], recursive_tasks: List[Text]):
    """
      Scan all the `tasks` and for each taskRef in `tasks` resolve it in `crs`
       and inline them as taskSpec.
       return tasks with all the taskRef -> taskSpec resolved.
       list of names of the taskRef that were successfully converted.
    """
    workflow_tasks = tasks.copy()
    inlined_as_taskSpec = []
    for j in range(len(workflow_tasks)):
      if 'params' in workflow_tasks[j]:
        # Preserve order of params, required by tests.
        workflow_tasks[j]['params'] = sorted(workflow_tasks[j]['params'], key=lambda kv: (kv['name']))
      if 'taskRef' in workflow_tasks[j]:
        wf_taskRef = workflow_tasks[j]['taskRef']
        if 'name' in wf_taskRef and \
                wf_taskRef['name'] not in recursive_tasks:  # we do not inline recursive tasks.
          cr_apiVersion = wf_taskRef['apiVersion']
          cr_kind = wf_taskRef['kind']
          cr_ref_name = wf_taskRef['name']
          for i in range(len(crs)):
            if crs[i]['metadata'].get('name', "") == cr_ref_name:
              workflow_tasks[j]['taskSpec'] = \
                {'apiVersion': cr_apiVersion, 'kind': cr_kind,
                 'spec': crs[i]['spec']}
              inlined_as_taskSpec.append(cr_ref_name)
              workflow_tasks[j].pop('taskRef')
      if 'taskSpec' in workflow_tasks[j]:
        workflow_tasks[j]['taskSpec']['metadata'] = workflow_tasks[j]['taskSpec'].get('metadata', {})
        task_labels = workflow_tasks[j]['taskSpec']['metadata'].get('labels', {})
        cache_default = self.pipeline_labels.get('pipelines.kubeflow.org/cache_enabled', 'true')
        task_labels['pipelines.kubeflow.org/cache_enabled'] = task_labels.get('pipelines.kubeflow.org/cache_enabled', cache_default)
        workflow_tasks[j]['taskSpec']['metadata']['labels'] = task_labels
    return workflow_tasks, inlined_as_taskSpec


def _validate_workflow(workflow: Dict[Text, Any]):

  # verify that all names and labels conform to kubernetes naming standards
  #   https://kubernetes.io/docs/concepts/overview/working-with-objects/names/
  #   https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/

  def _find_items(obj, search_key, current_path="", results_dict=dict()) -> dict:
    if isinstance(obj, dict):
      if search_key in obj:
        results_dict.update({("%s.%s" % (current_path, search_key)).lstrip("."): obj[search_key]})
      for k, v in obj.items():
        _find_items(v, search_key, "%s.%s" % (current_path, k), results_dict)
    elif isinstance(obj, list):
      for i, list_item in enumerate(obj):
        _find_items(list_item, search_key, "%s[%i]" % (current_path, i), results_dict)
    return results_dict

  non_k8s_names = {path: name for path, name in _find_items(workflow, "name").items()
                   if "metadata" in path and name != sanitize_k8s_name(name, max_length=253)
                   or "param" in path and name != sanitize_k8s_name(name, allow_capital_underscore=True, max_length=253)}

  non_k8s_labels = {path: k_v_dict for path, k_v_dict in _find_items(workflow, "labels", "", {}).items()
                    if "metadata" in path and
                    any([k != sanitize_k8s_name(k, allow_capital_underscore=True, allow_dot=True, allow_slash=True, max_length=253) or
                         v != sanitize_k8s_name(v, allow_capital_underscore=True, allow_dot=True)
                         for k, v in k_v_dict.items()])}

  non_k8s_annotations = {path: k_v_dict for path, k_v_dict in _find_items(workflow, "annotations", "", {}).items()
                         if "metadata" in path and
                         any([k != sanitize_k8s_name(k, allow_capital_underscore=True, allow_dot=True, allow_slash=True, max_length=253)
                              for k in k_v_dict.keys()])}

  error_msg_tmplt = textwrap.dedent("""\
    Internal compiler error: Found non-compliant Kubernetes %s:
    %s
    Please create a new issue at https://github.com/kubeflow/kfp-tekton/issues
    attaching the pipeline DSL code and the pipeline YAML.""")

  if non_k8s_names:
    raise RuntimeError(error_msg_tmplt % ("names", json.dumps(non_k8s_names, sort_keys=False, indent=2)))

  if non_k8s_labels:
    raise RuntimeError(error_msg_tmplt % ("labels", json.dumps(non_k8s_labels, sort_keys=False, indent=2)))

  if non_k8s_annotations:
    raise RuntimeError(error_msg_tmplt % ("annotations", json.dumps(non_k8s_annotations, sort_keys=False, indent=2)))
