# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2018, 2019.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

""" run circuits functions """

import sys
import logging
import time
import copy
import os
import uuid

import numpy as np
from qiskit.providers import BaseBackend, JobStatus, JobError
from qiskit.providers.jobstatus import JOB_FINAL_STATES
from qiskit.providers.basicaer import BasicAerJob
from qiskit.qobj import QasmQobj
from qiskit.aqua.aqua_error import AquaError
from qiskit.aqua.utils.backend_utils import (is_aer_provider,
                                             is_basicaer_provider,
                                             is_simulator_backend,
                                             is_local_backend)

MAX_CIRCUITS_PER_JOB = os.environ.get('QISKIT_AQUA_MAX_CIRCUITS_PER_JOB', None)

logger = logging.getLogger(__name__)


def find_regs_by_name(circuit, name, qreg=True):
    """Find the registers in the circuits.

    Args:
        circuit (QuantumCircuit): the quantum circuit.
        name (str): name of register
        qreg (bool): quantum or classical register

    Returns:
        QuantumRegister or ClassicalRegister or None: if not found, return None.

    """
    found_reg = None
    regs = circuit.qregs if qreg else circuit.cregs
    for reg in regs:
        if reg.name == name:
            found_reg = reg
            break
    return found_reg


def _combine_result_objects(results):
    """Temporary helper function.

    TODO:
        This function would be removed after Terra supports job with infinite circuits.
    """
    if len(results) == 1:
        return results[0]

    new_result = copy.deepcopy(results[0])

    for idx in range(1, len(results)):
        new_result.results.extend(results[idx].results)

    return new_result


# pylint: disable=invalid-name
def maybe_add_aer_expectation_instruction(qobj, options):
    """
    Add aer expectation instruction if `expectation` in the options.
    Args:
        qobj (QasmQobj): qobj
        options (dict): the setting for aer expectation instruction
    Returns:
        QasmQobj: a mutated qobj with aer expectation instruction inserted
    """
    if 'expectation' in options:
        from qiskit.providers.aer.utils.qobj_utils \
            import snapshot_instr, append_instr, get_instr_pos
        # add others, how to derive the correct used number of qubits?
        # the compiled qobj could be wrong if coupling map is used.
        params = options['expectation']['params']
        num_qubits = options['expectation']['num_qubits']

        for idx in range(len(qobj.experiments)):
            # if multiple params are provided, we assume
            # that each circuit is corresponding one param
            # otherwise, params are used for all circuits.
            param_idx = idx if len(params) > 1 else 0
            snapshot_pos = get_instr_pos(qobj, idx, 'snapshot')
            if not snapshot_pos:  # does not append the instruction yet.
                new_ins = snapshot_instr('expectation_value_pauli', 'test',
                                         list(range(num_qubits)), params=params[param_idx])
                qobj = append_instr(qobj, idx, new_ins)
            else:
                for i in snapshot_pos:  # update all expectation_value_snapshot
                    if qobj.experiments[idx].instructions[i].type == 'expectation_value_pauli':
                        qobj.experiments[idx].instructions[i].params = params[param_idx]
    return qobj


def _split_qobj_to_qobjs(qobj, chunk_size):
    qobjs = []
    num_chunks = int(np.ceil(len(qobj.experiments) / chunk_size))
    if num_chunks == 1:
        qobjs = [qobj]
    else:
        if isinstance(qobj, QasmQobj):
            qobj_template = QasmQobj(qobj_id=qobj.qobj_id,
                                     config=qobj.config, experiments=[], header=qobj.header)
            for i in range(num_chunks):
                temp_qobj = copy.deepcopy(qobj_template)
                temp_qobj.qobj_id = str(uuid.uuid4())
                temp_qobj.experiments = qobj.experiments[i * chunk_size:(i + 1) * chunk_size]
                qobjs.append(temp_qobj)
        else:
            raise AquaError("Only support QasmQobj now.")

    return qobjs


def _safe_submit_qobj(qobj, backend, backend_options, noise_config, skip_qobj_validation):
    # assure get job ids
    while True:
        job = run_on_backend(backend, qobj, backend_options=backend_options,
                             noise_config=noise_config,
                             skip_qobj_validation=skip_qobj_validation)
        try:
            job_id = job.job_id()
            break
        except JobError as ex:
            logger.warning("FAILURE: Can not get job id, Resubmit the qobj to get job id."
                           "Terra job error: %s ", ex)
        except Exception as ex:  # pylint: disable=broad-except
            logger.warning("FAILURE: Can not get job id, Resubmit the qobj to get job id."
                           "Error: %s ", ex)

    return job, job_id


def _safe_get_job_status(job, job_id):

    while True:
        try:
            job_status = job.status()
            break
        except JobError as ex:
            logger.warning("FAILURE: job id: %s, "
                           "status: 'FAIL_TO_GET_STATUS' "
                           "Terra job error: %s", job_id, ex)
            time.sleep(5)
        except Exception as ex:  # pylint: disable=broad-except
            raise AquaError("FAILURE: job id: {}, "
                            "status: 'FAIL_TO_GET_STATUS' "
                            "Unknown error: ({})".format(job_id, ex)) from ex
    return job_status


def run_qobj(qobj, backend, qjob_config=None, backend_options=None,
             noise_config=None, skip_qobj_validation=False, job_callback=None):
    """
    An execution wrapper with Qiskit-Terra, with job auto recover capability.

    The auto-recovery feature is only applied for non-simulator backend.
    This wrapper will try to get the result no matter how long it takes.

    Args:
        qobj (QasmQobj): qobj to execute
        backend (BaseBackend): backend instance
        qjob_config (dict, optional): configuration for quantum job object
        backend_options (dict, optional): configuration for simulator
        noise_config (dict, optional): configuration for noise model
        skip_qobj_validation (bool, optional): Bypass Qobj validation to decrease submission time,
                                               only works for Aer and BasicAer providers
        job_callback (Callable, optional): callback used in querying info of the submitted job, and
                                           providing the following arguments:
                                            job_id, job_status, queue_position, job

    Returns:
        Result: Result object

    Raises:
        ValueError: invalid backend
        AquaError: Any error except for JobError raised by Qiskit Terra
    """
    qjob_config = qjob_config or {}
    backend_options = backend_options or {}
    noise_config = noise_config or {}

    if backend is None or not isinstance(backend, BaseBackend):
        raise ValueError('Backend is missing or not an instance of BaseBackend')

    with_autorecover = not is_simulator_backend(backend)

    if MAX_CIRCUITS_PER_JOB is not None:
        max_circuits_per_job = int(MAX_CIRCUITS_PER_JOB)
    else:
        if is_local_backend(backend):
            max_circuits_per_job = sys.maxsize
        else:
            max_circuits_per_job = backend.configuration().max_experiments

    # split qobj if it exceeds the payload of the backend

    qobjs = _split_qobj_to_qobjs(qobj, max_circuits_per_job)
    jobs = []
    job_ids = []
    for qob in qobjs:
        job, job_id = _safe_submit_qobj(qob, backend,
                                        backend_options, noise_config, skip_qobj_validation)
        job_ids.append(job_id)
        jobs.append(job)

    results = []
    if with_autorecover:
        logger.info("Backend status: %s", backend.status())
        logger.info("There are %s jobs are submitted.", len(jobs))
        logger.info("All job ids:\n%s", job_ids)
        for idx, _ in enumerate(jobs):
            job = jobs[idx]
            job_id = job_ids[idx]
            while True:
                logger.info("Running %s-th qobj, job id: %s", idx, job_id)
                # try to get result if possible
                while True:
                    job_status = _safe_get_job_status(job, job_id)
                    queue_position = 0
                    if job_status in JOB_FINAL_STATES:
                        # do callback again after the job is in the final states
                        if job_callback is not None:
                            job_callback(job_id, job_status, queue_position, job)
                        break
                    if job_status == JobStatus.QUEUED:
                        queue_position = job.queue_position()
                        logger.info("Job id: %s is queued at position %s", job_id, queue_position)
                    else:
                        logger.info("Job id: %s, status: %s", job_id, job_status)
                    if job_callback is not None:
                        job_callback(job_id, job_status, queue_position, job)
                    time.sleep(qjob_config['wait'])

                # get result after the status is DONE
                if job_status == JobStatus.DONE:
                    while True:
                        result = job.result(**qjob_config)
                        if result.success:
                            results.append(result)
                            logger.info("COMPLETED the %s-th qobj, job id: %s", idx, job_id)
                            break

                        logger.warning("FAILURE: Job id: %s", job_id)
                        logger.warning("Job (%s) is completed anyway, retrieve result "
                                       "from backend again.", job_id)
                        job = backend.retrieve_job(job_id)
                    break
                # for other cases, resubmit the qobj until the result is available.
                # since if there is no result returned, there is no way algorithm can do any process
                # get back the qobj first to avoid for job is consumed
                qobj = job.qobj()
                if job_status == JobStatus.CANCELLED:
                    logger.warning("FAILURE: Job id: %s is cancelled. Re-submit the Qobj.",
                                   job_id)
                elif job_status == JobStatus.ERROR:
                    logger.warning("FAILURE: Job id: %s encounters the error. "
                                   "Error is : %s. Re-submit the Qobj.",
                                   job_id, job.error_message())
                else:
                    logging.warning("FAILURE: Job id: %s. Unknown status: %s. "
                                    "Re-submit the Qobj.", job_id, job_status)

                job, job_id = _safe_submit_qobj(qobj, backend,
                                                backend_options,
                                                noise_config, skip_qobj_validation)
                jobs[idx] = job
                job_ids[idx] = job_id
    else:
        results = []
        for job in jobs:
            results.append(job.result(**qjob_config))

    result = _combine_result_objects(results) if results else None

    return result


# skip_qobj_validation = True does what backend.run
# and aerjob.submit do, but without qobj validation.
def run_on_backend(backend, qobj, backend_options=None,
                   noise_config=None, skip_qobj_validation=False):
    """ run on backend """
    if skip_qobj_validation:
        job_id = str(uuid.uuid4())
        if is_aer_provider(backend):
            from qiskit.providers.aer.aerjob import AerJob
            temp_backend_options = \
                backend_options['backend_options'] if backend_options != {} else None
            temp_noise_config = noise_config['noise_model'] if noise_config != {} else None
            job = AerJob(backend, job_id,
                         backend._run_job, qobj, temp_backend_options, temp_noise_config, False)
            job._future = job._executor.submit(job._fn, job._job_id, job._qobj, *job._args)
        elif is_basicaer_provider(backend):
            backend._set_options(qobj_config=qobj.config, **backend_options)
            job = BasicAerJob(backend, job_id, backend._run_job, qobj)
            job._future = job._executor.submit(job._fn, job._job_id, job._qobj)
        else:
            logger.info(
                "Can't skip qobj validation for the %s provider.",
                backend.provider().__class__.__name__)
            job = backend.run(qobj, **backend_options, **noise_config)
        return job
    else:
        job = backend.run(qobj, **backend_options, **noise_config)
        return job
