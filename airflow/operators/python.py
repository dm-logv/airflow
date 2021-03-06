#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import functools
import inspect
import os
import pickle
import re
import sys
import types
from inspect import signature
from itertools import islice
from tempfile import TemporaryDirectory
from textwrap import dedent
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import dill

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator
from airflow.models.dag import DAG, DagContext
from airflow.models.skipmixin import SkipMixin
from airflow.models.xcom_arg import XComArg
from airflow.utils.decorators import apply_defaults
from airflow.utils.process_utils import execute_in_subprocess
from airflow.utils.python_virtualenv import prepare_virtualenv


class PythonOperator(BaseOperator):
    """
    Executes a Python callable

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:PythonOperator`

    :param python_callable: A reference to an object that is callable
    :type python_callable: python callable
    :param op_kwargs: a dictionary of keyword arguments that will get unpacked
        in your function
    :type op_kwargs: dict (templated)
    :param op_args: a list of positional arguments that will get unpacked when
        calling your callable
    :type op_args: list (templated)
    :param templates_dict: a dictionary where the values are templates that
        will get templated by the Airflow engine sometime between
        ``__init__`` and ``execute`` takes place and are made available
        in your callable's context after the template has been applied. (templated)
    :type templates_dict: dict[str]
    :param templates_exts: a list of file extensions to resolve while
        processing templated fields, for examples ``['.sql', '.hql']``
    :type templates_exts: list[str]
    """
    template_fields = ('templates_dict', 'op_args', 'op_kwargs')
    ui_color = '#ffefeb'

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects(e.g protobuf).
    shallow_copy_attrs = ('python_callable', 'op_kwargs',)

    @apply_defaults
    def __init__(
        self,
        python_callable: Callable,
        op_args: Optional[List] = None,
        op_kwargs: Optional[Dict] = None,
        templates_dict: Optional[Dict] = None,
        templates_exts: Optional[List[str]] = None,
        *args,
        **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        if not callable(python_callable):
            raise AirflowException('`python_callable` param must be callable')
        self.python_callable = python_callable
        self.op_args = op_args or []
        self.op_kwargs = op_kwargs or {}
        self.templates_dict = templates_dict
        if templates_exts:
            self.template_ext = templates_exts

    @staticmethod
    def determine_op_kwargs(python_callable: Callable,
                            context: Dict,
                            num_op_args: int = 0) -> Dict:
        """
        Function that will inspect the signature of a python_callable to determine which
        values need to be passed to the function.

        :param python_callable: The function that you want to invoke
        :param context: The context provided by the execute method of the Operator/Sensor
        :param num_op_args: The number of op_args provided, so we know how many to skip
        :return: The op_args dictionary which contains the values that are compatible with the Callable
        """
        context_keys = context.keys()
        sig = signature(python_callable).parameters.items()
        op_args_names = islice(sig, num_op_args)
        for name, _ in op_args_names:
            # Check if it is part of the context
            if name in context_keys:
                # Raise an exception to let the user know that the keyword is reserved
                raise ValueError(
                    "The key {} in the op_args is part of the context, and therefore reserved".format(name)
                )

        if any(str(param).startswith("**") for _, param in sig):
            # If there is a ** argument then just dump everything.
            op_kwargs = context
        else:
            # If there is only for example, an execution_date, then pass only these in :-)
            op_kwargs = {
                name: context[name]
                for name, _ in sig
                if name in context  # If it isn't available on the context, then ignore
            }
        return op_kwargs

    def execute(self, context: Dict):
        context.update(self.op_kwargs)
        context['templates_dict'] = self.templates_dict

        self.op_kwargs = PythonOperator.determine_op_kwargs(self.python_callable, context, len(self.op_args))

        return_value = self.execute_callable()
        self.log.info("Done. Returned value was: %s", return_value)
        return return_value

    def execute_callable(self):
        """
        Calls the python callable with the given arguments.

        :return: the return value of the call.
        :rtype: any
        """
        return self.python_callable(*self.op_args, **self.op_kwargs)


class _PythonFunctionalOperator(BaseOperator):
    """
    Wraps a Python callable and captures args/kwargs when called for execution.

    :param python_callable: A reference to an object that is callable
    :type python_callable: python callable
    :param op_kwargs: a dictionary of keyword arguments that will get unpacked
        in your function (templated)
    :type op_kwargs: dict
    :param op_args: a list of positional arguments that will get unpacked when
        calling your callable (templated)
    :type op_args: list
    :param multiple_outputs: if set, function return value will be
        unrolled to multiple XCom values. Dict will unroll to xcom values with keys as keys.
        Defaults to False.
    :type multiple_outputs: bool
    """

    template_fields = ('op_args', 'op_kwargs')
    ui_color = PythonOperator.ui_color

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects (e.g protobuf).
    shallow_copy_attrs = ('python_callable',)

    @apply_defaults
    def __init__(
        self,
        python_callable: Callable,
        task_id: str,
        op_args: Tuple[Any],
        op_kwargs: Dict[str, Any],
        multiple_outputs: bool = False,
        **kwargs
    ) -> None:
        kwargs['task_id'] = self._get_unique_task_id(task_id, kwargs.get('dag', None))
        super().__init__(**kwargs)
        self.python_callable = python_callable

        # Check that arguments can be binded
        signature(python_callable).bind(*op_args, **op_kwargs)
        self.multiple_outputs = multiple_outputs
        self.op_args = op_args
        self.op_kwargs = op_kwargs

    @staticmethod
    def _get_unique_task_id(task_id: str, dag: Optional[DAG] = None) -> str:
        """
        Generate unique task id given a DAG (or if run in a DAG context)
        Ids are generated by appending a unique number to the end of
        the original task id.

        Example:
          task_id
          task_id__1
          task_id__2
          ...
          task_id__20
        """
        dag = dag or DagContext.get_current_dag()
        if not dag or task_id not in dag.task_ids:
            return task_id
        core = re.split(r'__\d+$', task_id)[0]
        suffixes = sorted(
            [int(re.split(r'^.+__', task_id)[1])
             for task_id in dag.task_ids
             if re.match(rf'^{core}__\d+$', task_id)]
        )
        if not suffixes:
            return f'{core}__1'
        return f'{core}__{suffixes[-1] + 1}'

    @staticmethod
    def validate_python_callable(python_callable):
        """
        Validate that python callable can be wrapped by operator.
        Raises exception if invalid.

        :param python_callable: Python object to be validated
        :raises: TypeError, AirflowException
        """
        if not callable(python_callable):
            raise TypeError('`python_callable` param must be callable')
        if 'self' in signature(python_callable).parameters.keys():
            raise AirflowException('@task does not support methods')

    def execute(self, context: Dict):
        return_value = self.python_callable(*self.op_args, **self.op_kwargs)
        self.log.debug("Done. Returned value was: %s", return_value)
        if not self.multiple_outputs:
            return return_value
        if isinstance(return_value, dict):
            for key in return_value.keys():
                if not isinstance(key, str):
                    raise AirflowException('Returned dictionary keys must be strings when using '
                                           f'multiple_outputs, found {key} ({type(key)}) instead')
            for key, value in return_value.items():
                self.xcom_push(context, key, value)
        else:
            raise AirflowException(f'Returned output was type {type(return_value)} expected dictionary '
                                   'for multiple_outputs')
        return return_value


def task(python_callable: Optional[Callable] = None, multiple_outputs: bool = False, **kwargs):
    """
    Python operator decorator. Wraps a function into an Airflow operator.
    Accepts kwargs for operator kwarg. Can be reused in a single DAG.

    :param python_callable: Function to decorate
    :type python_callable: Optional[Callable]
    :param multiple_outputs: if set, function return value will be
        unrolled to multiple XCom values. List/Tuples will unroll to xcom values
        with index as key. Dict will unroll to xcom values with keys as XCom keys.
        Defaults to False.
    :type multiple_outputs: bool

    """
    def wrapper(f):
        """
        Python wrapper to generate PythonFunctionalOperator out of simple python functions.
        Used for Airflow functional interface
        """
        _PythonFunctionalOperator.validate_python_callable(f)
        kwargs.setdefault('task_id', f.__name__)

        @functools.wraps(f)
        def factory(*args, **f_kwargs):
            op = _PythonFunctionalOperator(python_callable=f, op_args=args, op_kwargs=f_kwargs,
                                           multiple_outputs=multiple_outputs, **kwargs)
            return XComArg(op)
        return factory
    if callable(python_callable):
        return wrapper(python_callable)
    elif python_callable is not None:
        raise AirflowException('No args allowed while using @task, use kwargs instead')
    return wrapper


class BranchPythonOperator(PythonOperator, SkipMixin):
    """
    Allows a workflow to "branch" or follow a path following the execution
    of this task.

    It derives the PythonOperator and expects a Python function that returns
    a single task_id or list of task_ids to follow. The task_id(s) returned
    should point to a task directly downstream from {self}. All other "branches"
    or directly downstream tasks are marked with a state of ``skipped`` so that
    these paths can't move forward. The ``skipped`` states are propagated
    downstream to allow for the DAG state to fill up and the DAG run's state
    to be inferred.
    """

    def execute(self, context: Dict):
        branch = super().execute(context)
        self.skip_all_except(context['ti'], branch)
        return branch


class ShortCircuitOperator(PythonOperator, SkipMixin):
    """
    Allows a workflow to continue only if a condition is met. Otherwise, the
    workflow "short-circuits" and downstream tasks are skipped.

    The ShortCircuitOperator is derived from the PythonOperator. It evaluates a
    condition and short-circuits the workflow if the condition is False. Any
    downstream tasks are marked with a state of "skipped". If the condition is
    True, downstream tasks proceed as normal.

    The condition is determined by the result of `python_callable`.
    """

    def execute(self, context: Dict):
        condition = super().execute(context)
        self.log.info("Condition result is %s", condition)

        if condition:
            self.log.info('Proceeding with downstream tasks...')
            return

        self.log.info('Skipping downstream tasks...')

        downstream_tasks = context['task'].get_flat_relatives(upstream=False)
        self.log.debug("Downstream task_ids %s", downstream_tasks)

        if downstream_tasks:
            self.skip(context['dag_run'], context['ti'].execution_date, downstream_tasks)

        self.log.info("Done.")


class PythonVirtualenvOperator(PythonOperator):
    """
    Allows one to run a function in a virtualenv that is created and destroyed
    automatically (with certain caveats).

    The function must be defined using def, and not be
    part of a class. All imports must happen inside the function
    and no variables outside of the scope may be referenced. A global scope
    variable named virtualenv_string_args will be available (populated by
    string_args). In addition, one can pass stuff through op_args and op_kwargs, and one
    can use a return value.
    Note that if your virtualenv runs in a different Python major version than Airflow,
    you cannot use return values, op_args, or op_kwargs. You can use string_args though.

    :param python_callable: A python function with no references to outside variables,
        defined with def, which will be run in a virtualenv
    :type python_callable: function
    :param requirements: A list of requirements as specified in a pip install command
    :type requirements: list[str]
    :param python_version: The Python version to run the virtualenv with. Note that
        both 2 and 2.7 are acceptable forms.
    :type python_version: str
    :param use_dill: Whether to use dill to serialize
        the args and result (pickle is default). This allow more complex types
        but requires you to include dill in your requirements.
    :type use_dill: bool
    :param system_site_packages: Whether to include
        system_site_packages in your virtualenv.
        See virtualenv documentation for more information.
    :type system_site_packages: bool
    :param op_args: A list of positional arguments to pass to python_callable.
    :type op_kwargs: list
    :param op_kwargs: A dict of keyword arguments to pass to python_callable.
    :type op_kwargs: dict
    :param string_args: Strings that are present in the global var virtualenv_string_args,
        available to python_callable at runtime as a list[str]. Note that args are split
        by newline.
    :type string_args: list[str]
    :param templates_dict: a dictionary where the values are templates that
        will get templated by the Airflow engine sometime between
        ``__init__`` and ``execute`` takes place and are made available
        in your callable's context after the template has been applied
    :type templates_dict: dict of str
    :param templates_exts: a list of file extensions to resolve while
        processing templated fields, for examples ``['.sql', '.hql']``
    :type templates_exts: list[str]
    """

    @apply_defaults
    def __init__(  # pylint: disable=too-many-arguments
        self,
        python_callable: Callable,
        requirements: Optional[Iterable[str]] = None,
        python_version: Optional[str] = None,
        use_dill: bool = False,
        system_site_packages: bool = True,
        op_args: Optional[Iterable] = None,
        op_kwargs: Optional[Dict] = None,
        string_args: Optional[Iterable[str]] = None,
        templates_dict: Optional[Dict] = None,
        templates_exts: Optional[Iterable[str]] = None,
        *args,
        **kwargs
    ):
        super().__init__(
            python_callable=python_callable,
            op_args=op_args,
            op_kwargs=op_kwargs,
            templates_dict=templates_dict,
            templates_exts=templates_exts,
            *args,
            **kwargs)
        self.requirements = requirements or []
        self.string_args = string_args or []
        self.python_version = python_version
        self.use_dill = use_dill
        self.system_site_packages = system_site_packages
        # check that dill is present if needed
        dill_in_requirements = map(lambda x: x.lower().startswith('dill'),
                                   self.requirements)
        if (not system_site_packages) and use_dill and not any(dill_in_requirements):
            raise AirflowException('If using dill, dill must be in the environment ' +
                                   'either via system_site_packages or requirements')
        # check that a function is passed, and that it is not a lambda
        if (not isinstance(self.python_callable,
                           types.FunctionType) or (self.python_callable.__name__ ==
                                                   (lambda x: 0).__name__)):
            raise AirflowException('{} only supports functions for python_callable arg'.format(
                self.__class__.__name__))
        # check that args are passed iff python major version matches
        if (python_version is not None and
           str(python_version)[0] != str(sys.version_info[0]) and
           self._pass_op_args()):
            raise AirflowException("Passing op_args or op_kwargs is not supported across "
                                   "different Python major versions "
                                   "for PythonVirtualenvOperator. "
                                   "Please use string_args.")

    def execute_callable(self):
        with TemporaryDirectory(prefix='venv') as tmp_dir:
            if self.templates_dict:
                self.op_kwargs['templates_dict'] = self.templates_dict
            # generate filenames
            input_filename = os.path.join(tmp_dir, 'script.in')
            output_filename = os.path.join(tmp_dir, 'script.out')
            string_args_filename = os.path.join(tmp_dir, 'string_args.txt')
            script_filename = os.path.join(tmp_dir, 'script.py')

            # set up virtualenv
            python_bin = 'python' + str(self.python_version) if self.python_version else None
            prepare_virtualenv(
                venv_directory=tmp_dir,
                python_bin=python_bin,
                system_site_packages=self.system_site_packages,
                requirements=self.requirements,
            )

            self._write_args(input_filename)
            self._write_script(script_filename)
            self._write_string_args(string_args_filename)

            # execute command in virtualenv
            execute_in_subprocess(
                self._generate_python_cmd(tmp_dir,
                                          script_filename,
                                          input_filename,
                                          output_filename,
                                          string_args_filename))
            return self._read_result(output_filename)

    def _pass_op_args(self):
        # we should only pass op_args if any are given to us
        return len(self.op_args) + len(self.op_kwargs) > 0

    def _write_string_args(self, filename):
        # writes string_args to a file, which are read line by line
        with open(filename, 'w') as file:
            file.write('\n'.join(map(str, self.string_args)))

    def _write_args(self, input_filename):
        # serialize args to file
        if self._pass_op_args():
            with open(input_filename, 'wb') as file:
                arg_dict = ({'args': self.op_args, 'kwargs': self.op_kwargs})
                if self.use_dill:
                    dill.dump(arg_dict, file)
                else:
                    pickle.dump(arg_dict, file)

    def _read_result(self, output_filename):
        if os.stat(output_filename).st_size == 0:
            return None
        with open(output_filename, 'rb') as file:
            try:
                if self.use_dill:
                    return dill.load(file)
                else:
                    return pickle.load(file)
            except ValueError:
                self.log.error("Error deserializing result. "
                               "Note that result deserialization "
                               "is not supported across major Python versions.")
                raise

    def _write_script(self, script_filename):
        with open(script_filename, 'w') as file:
            python_code = self._generate_python_code()
            self.log.debug('Writing code to file\n %s', python_code)
            file.write(python_code)

    @staticmethod
    def _generate_python_cmd(tmp_dir, script_filename,
                             input_filename, output_filename, string_args_filename):
        # direct path alleviates need to activate
        return ['{}/bin/python'.format(tmp_dir), script_filename,
                input_filename, output_filename, string_args_filename]

    def _generate_python_code(self):
        if self.use_dill:
            pickling_library = 'dill'
        else:
            pickling_library = 'pickle'

        # dont try to read pickle if we didnt pass anything
        if self._pass_op_args():
            load_args_line = 'with open(sys.argv[1], "rb") as file: arg_dict = {}.load(file)' \
                .format(pickling_library)
        else:
            load_args_line = 'arg_dict = {"args": [], "kwargs": {}}'

        # no indents in original code so we can accept
        # any type of indents in the original function
        # we deserialize args, call function, serialize result if necessary
        return dedent("""\
        import {pickling_library}
        import sys
        {load_args_code}
        args = arg_dict["args"]
        kwargs = arg_dict["kwargs"]
        with open(sys.argv[3], 'r') as file:
            virtualenv_string_args = list(map(lambda x: x.strip(), list(file)))
        {python_callable_lines}
        res = {python_callable_name}(*args, **kwargs)
        with open(sys.argv[2], 'wb') as file:
            res is not None and {pickling_library}.dump(res, file)
        """).format(load_args_code=load_args_line,
                    python_callable_lines=dedent(inspect.getsource(self.python_callable)),
                    python_callable_name=self.python_callable.__name__,
                    pickling_library=pickling_library)
