# -*- coding: utf-8 -*-
"""
ipysnobal: the Python implementation of iSnobal

This is not a replica of iSnobal but my interpretation and
porting to Python.  See pysnobal.exact for more direct
interpretation

Authors: Scott Havens, Micah Sandusky
"""

try:
    from pysnobal import snobal
except Exception as e:
    print(e)
    print('pysnobal not installed, ignoring')

import pandas as pd
import sys
import numpy as np
import copy
from smrf.utils import utils

try:
    from Queue import Queue  # , Empty, Full
except:
    from queue import Queue  # , Empty, Full

import threading
from awsm.interface import initialize_model as initmodel
from awsm.interface import pysnobal_io as io_mod
from awsm.data.init_model import zero_crash_depths

C_TO_K = 273.16
FREEZE = C_TO_K

# Kelvin to Celcius
K_TO_C = lambda x: x - FREEZE

# ###############################################################
# ########## Functions for interfacing with smrf run ############
# ###############################################################


def init_from_smrf(myawsm, mysmrf=None, dem=None):
    """
    mimic the main.c from the Snobal model

    Args:
        myawsm: AWSM instance
        mysmrf: SMRF isntance
        dem:    digital elevation data
    """

    # parse the input arguments
    options, point_run = initmodel.get_args(myawsm)

    # get the timestep info
    params, tstep_info = initmodel.get_tstep_info(options['constants'],
                                                  options,
                                                  myawsm.mass_thresh)

    if dem is None:
        dem = myawsm.topo.dem

    # get init params
    init = myawsm.myinit.init

    output_rec = initmodel.initialize(params, tstep_info, init)

    # create the output files
    io_mod.output_files(options, init, myawsm.start_date, myawsm)

    return options, params, tstep_info, init, output_rec


class QueueIsnobal(threading.Thread):
    """
    Takes values from the queue and uses them to run iPySnobal
    """

    def __init__(self, queue, date_time, thread_variables, awsm_output_vars,
                 options, params, tstep_info, init,
                 output_rec, nx, ny, soil_temp, logger, tzi,
                 updater=None):
        """
        Args:
            queue:      dictionary of the queue
            date_time:  array of date_time
            thread_variables: list of threaded variables
            awsm_output_vars: list of variables to output
            options:    dictionary of Snobal options
            params:     dictionary of Snobal params
            tstep_info: dictionary of info for Snobal timesteps
            init:       dictionary of init info for Snobal
            output_rec: dictionary to store Snobal variables between timesteps
            nx:         number of points in X direction
            ny:         number of points in y direction
            soil_temp:  uniform soil temperature (float)
            logger:     initialized AWSM logger
            tzi:        time zone information
            updater:    depth updater
        """

        threading.Thread.__init__(self, name='isnobal')
        self.queue = queue
        self.date_time = date_time
        self.thread_variables = thread_variables
        self.awsm_output_vars = awsm_output_vars
        self.options = options
        self.params = params
        self.tstep_info = tstep_info
        self.init = init
        self.output_rec = output_rec
        self.nx = nx
        self.ny = ny
        self.soil_temp = soil_temp
        self.nthreads = self.options['output']['nthreads']
        self.tzinfo = tzi
        self.updater = updater

        # get AWSM logger
        self._logger = logger
        self._logger.debug('Initialized iPySnobal thread')

    def run(self):
        """
        mimic the main.c from the Snobal model. Runs Pysnobal while recieving
        forcing data from SMRF queue.

        """
        force_variables = ['thermal', 'air_temp', 'vapor_pressure', 'wind_speed',
                           'net_solar', 'soil_temp', 'precip', 'percent_snow',
                           'snow_density', 'precip_temp']

        # loop through the input
        # do_data_tstep needs two input records so only go
        # to the last record-1

        data_tstep = self.tstep_info[0]['time_step']
        timeSinceOut = 0.0
        tmp_date = self.date_time[0].replace(tzinfo=self.tzinfo)
        wyhr = utils.water_day(tmp_date)[0] * 24.0
        start_step = wyhr  # if restart then it would be higher if this were iSnobal
        # start_step = 0 # if restart then it would be higher if this were iSnobal
        step_time = start_step * data_tstep
        # step_time = start_step * 60.0

        self.output_rec['current_time'] = step_time * np.ones(self.output_rec['elevation'].shape)
        self.output_rec['time_since_out'] = timeSinceOut * np.ones(self.output_rec['elevation'].shape)

        # map function from these values to the ones requried by snobal
        map_val = {'air_temp': 'T_a', 'net_solar': 'S_n', 'thermal': 'I_lw',
                   'vapor_pressure': 'e_a', 'wind_speed': 'u',
                   'soil_temp': 'T_g', 'precip': 'm_pp',
                   'percent_snow': 'percent_snow', 'snow_density': 'rho_snow',
                   'precip_temp': 'T_pp'}

        # get first timestep
        input1 = {}
        for v in force_variables:
            if v in self.queue.keys():

                data = self.queue[v].get(self.date_time[0], block=True, timeout=None)
                if data is None:
                    data = np.zeros((self.ny, self.nx))
                    self._logger.info('No data from smrf to iSnobal for {} in {}'
                                      .format(v, self.date_time[0]))
                    input1[map_val[v]] = data
                else:
                    input1[map_val[v]] = data
            elif v != 'soil_temp':
                self._logger.error('Value not in keys: {}'.format(v))

        # set ground temp
        input1['T_g'] = self.soil_temp*np.ones((self.ny, self.nx))

        input1['T_a'] += FREEZE
        input1['T_pp'] += FREEZE
        input1['T_g'] += FREEZE

        # tell queue we assigned all the variables
        self.queue['isnobal'].put([self.date_time[0], True])
        self._logger.info('Finished initializing first timestep for iPySnobal')

        j = 1
        # for tstep in options['time']['date_time'][953:958]:
        for tstep in self.date_time[1:]:
            # get the output variables then pass to the function
            # this avoids zeroing of the energetics every timestep
            first_step = j
            input2 = {}
            for v in force_variables:
                if v in self.queue.keys():
                    # get variable from smrf queue
                    data = self.queue[v].get(tstep, block=True, timeout=None)
                    if data is None:

                        data = np.zeros((self.ny, self.nx))
                        self._logger.info('No data from smrf to iSnobal for {} in {}'.format(v, tstep))
                        input2[map_val[v]] = data
                    else:
                        input2[map_val[v]] = data
            # set ground temp
            input2['T_g'] = self.soil_temp*np.ones((self.ny, self.nx))
            # convert variables to Kelvin
            input2['T_a'] += FREEZE
            input2['T_pp'] += FREEZE
            input2['T_g'] += FREEZE

            first_step = j
            if self.updater is not None:
                if tstep.tz_localize(None) in self.updater.update_dates:
                    self.output_rec = \
                        self.updater.do_update_pysnobal(self.output_rec,
                                                        tstep.tz_localize(None))
                    first_step = 1

            self._logger.info('running PySnobal for timestep: {}'.format(tstep))
            rt = snobal_with_error_handle(self._logger, input1, input2,
                                          self.output_rec, self.tstep_info,
                                          self.options['constants'],
                                          self.params, first_step=first_step,
                                          nthreads=self.nthreads)
            # rt = snobal.do_tstep_grid(input1, input2,
            #                           self.output_rec,
            #                           self.tstep_info,
            #                           self.options['constants'],
            #                           self.params,
            #                           first_step=first_step,
            #                           nthreads=self.nthreads)

            if rt != -1:
                self.logger.error('ipysnobal error on time step {}, pixel {}'
                                  .format(tstep, rt))
                break

            self._logger.info('Finished timestep: {}'.format(tstep))
            input1 = input2.copy()

            # output at the frequency and the last time step
            if ((j)*(data_tstep/3600.0) % self.options['output']['frequency'] == 0)\
                    or (j == len(self.options['time']['date_time']) - 1):
                io_mod.output_timestep(self.output_rec, tstep, self.options,
                                       self.awsm_output_vars)
                self.output_rec['time_since_out'] = \
                    np.zeros(self.output_rec['elevation'].shape)

            j += 1

            # put the value into the output queue so clean knows it's done
            self.queue['isnobal'].put([tstep, True])

            # self._logger.debug('%s iSnobal run from queues' % tstep)


class PySnobal():
    """
    Takes values from the SMRF and uses them to run iPySnobal in non-threaded
    implimentation
    """

    def __init__(self, date_time, variable_list, awsm_output_vars,
                 options, params, tstep_info, init,
                 output_rec, nx, ny, soil_temp, logger, tzi):
        """
        Args:
            date_time:  array of date_time
            variable_list: list of forcing variables to recieve from smrf
            output_vars:    list of variables to output
            options:    dictionary of Snobal options
            params:     dictionary of Snobal params
            tstep_info: dictionary of info for Snobal timesteps
            init:       dictionary of init info for Snobal
            output_rec: dictionary to store Snobal variables between timesteps
            nx:         number of points in X direction
            ny:         number of points in y direction
            soil_temp:  uniform soil temperature (float)
            logger:     initialized AWSM logger
            tzi:        time zone information
        """

        self.date_time = date_time
        self.variable_list = variable_list
        self.awsm_output_vars = awsm_output_vars
        self.options = options
        self.params = params
        self.tstep_info = tstep_info
        self.init = init
        self.output_rec = output_rec
        self.nx = nx
        self.ny = ny
        self.soil_temp = soil_temp
        self.nthreads = self.options['output']['nthreads']
        self.tzinfo = tzi

        # map function from these values to the ones requried by snobal
        self.map_val = {'air_temp': 'T_a', 'net_solar': 'S_n', 'thermal': 'I_lw',
                        'vapor_pressure': 'e_a', 'wind_speed': 'u',
                        'soil_temp': 'T_g', 'precip': 'm_pp',
                        'percent_snow': 'percent_snow',
                        'snow_density': 'rho_snow',
                        'precip_temp': 'T_pp'}

        # get AWSM logger
        self._logger = logger
        self._logger.debug('Initialized iPySnobal thread')

    def run_single_fist_step(self, s):
        """
        mimic the main.c from the Snobal model. Recieves forcing data from SMRF
        in non-threaded application and initializes very first step.

        Args:
            s:  smrf class instance

        """

        # loop through the input
        # do_data_tstep needs two input records so only go
        # to the last record-1

        self.data_tstep = self.tstep_info[0]['time_step']
        self.timeSinceOut = 0.0
        tmp_date = self.date_time[0].replace(tzinfo=self.tzinfo)
        wyhr = utils.water_day(tmp_date)[0] * 24.0
        start_step = wyhr  # if restart then it would be higher if this were iSnobal
        # start_step = 0 # if restart then it would be higher if this were iSnobal
        step_time = start_step * self.data_tstep
        # step_time = start_step * 60.0

        self.output_rec['current_time'] = step_time * np.ones(self.output_rec['elevation'].shape)
        self.output_rec['time_since_out'] = self.timeSinceOut * np.ones(self.output_rec['elevation'].shape)

        # get first timestep
        self.input1 = {}
        for var, v in self.variable_list.items():
                # get the data desired
                data = getattr(s.distribute[v['module']], v['variable'])

                if data is None:
                    data = np.zeros((self.ny, self.nx))
                    self._logger.info('No data from smrf to iSnobal for {} in {}'.format(v, self.date_time[0]))
                    self.input1[self.map_val[var]] = data
                else:
                    self.input1[self.map_val[var]] = data

        # set ground temp
        self.input1['T_g'] = self.soil_temp*np.ones((self.ny, self.nx))

        self.input1['T_a'] += FREEZE
        self.input1['T_pp'] += FREEZE
        self.input1['T_g'] += FREEZE

        # for counting how many steps since the start of the run
        self.j = 1

        self._logger.info('Finished initializing first timestep for iPySnobal')

    def run_single(self, tstep, s, updater=None):
        """
        Runs each timestep of Pysnobal when running with SMRF in non-threaded
        application.

        Args:
            tstep: datetime timestep
            s:     smrf class instance
            updater: depth updater class

        """
        # pbar = progressbar.ProgressBar(max_value=len(options['time']['date_time']))

        self.input2 = {}
        for var, v in self.variable_list.items():
            # get the data desired
            data = getattr(s.distribute[v['module']], v['variable'])
            if data is None:

                data = np.zeros((self.ny, self.nx))
                self._logger.info('No data from smrf to iSnobal for {} in {}'
                                  .format(v, tstep))
                self.input2[self.map_val[var]] = data
            else:
                self.input2[self.map_val[var]] = data
        # set ground temp
        self.input2['T_g'] = self.soil_temp*np.ones((self.ny, self.nx))
        # convert variables to Kelvin
        self.input2['T_a'] += FREEZE
        self.input2['T_pp'] += FREEZE
        self.input2['T_g'] += FREEZE

        first_step = self.j

        # update depth if necessary
        if updater is not None:
            if tstep.tz_localize(None) in updater.update_dates:
                print('doing that update thing')
                self.output_rec = \
                    updater.do_update_pysnobal(self.output_rec, tstep.tz_localize(None))
                first_step = 1


        self._logger.info('running PySnobal for timestep: {}'.format(tstep))
        rt = snobal_with_error_handle(self._logger, self.input1, self.input2,
                                      self.output_rec, self.tstep_info,
                                      self.options['constants'],
                                      self.params, first_step=first_step,
                                      nthreads=self.nthreads)
        # rt = snobal.do_tstep_grid(self.input1, self.input2, self.output_rec,
        #                           self.tstep_info, self.options['constants'],
        #                           self.params, first_step=first_step,
        #                           nthreads=self.nthreads)

        if rt != -1:
            self.logger.error('ipysnobal error on time step {}, pixel {}'
                              .format(tstep, rt))
            sys.exit()

        self._logger.info('Finished timestep: {}'.format(tstep))
        self.input1 = self.input2.copy()

        # output at the frequency and the last time step
        if ((self.j)*(self.data_tstep/3600.0) % self.options['output']['frequency'] == 0)\
                or (self.j == len(self.options['time']['date_time']) - 1):
            io_mod.output_timestep(self.output_rec, tstep, self.options,
                                   self.awsm_output_vars)
            self.output_rec['time_since_out'] = np.zeros(self.output_rec['elevation'].shape)

        self.j += 1


def snobal_with_error_handle(logger, input1, input2, output_rec, tstep_info,
                             constants, params, first_step, nthreads):
    """
    Function to run snobal from pysnobal package while handling and correcting
    some of the standard crashes that occur. Crashes can occur due to unstable
    fluxes in a shallow snopack and/or high winds. They can also occur due to
    sub-par input data and must be handled before program is run. This routine
    represents some of the standard error handling by the USDA ARS NWRC snow
    team and not a perfect solution to any error from the iSnobal core code.

    Args:
        logger:
        input1:
        input2:
        output_rec:
        tstep_info:
        constants:
        params:
        first_step:
        nthreads:

    Returns:
        rt: success or failure from snobal time step
        nsteps_mass_change: number of consecutive succesful timesteps run with
                            the mass changed

    """
    fn_lst = [do_nothing, handle_depth, handle_depth, handle_mass]
    # save a copy of the tstep info
    tstep_info_cp = copy.deepcopy(tstep_info)

    for idf, fn in enumerate(fn_lst):
        if idf == 0:
            mass_count_change = 0
            depth_thresh = 0.0
        elif idf == 1:
            mass_count_change = 0
            depth_thresh = 0.05
        elif idf == 2:
            mass_count_change = 0
            depth_thresh = 0.07
        else:
            mass_count_change += 1
            depth_thresh = 0.0
            if mass_count_change >= 73:
                raise Exception('Too many consecutive timesteps with increased mass thresholds')
        # print(idf, fn)
        try:
            # change parameters to avoid crash
            fn(logger, output_rec, tstep_info, depth_thresh)
            # run snobal
            rt = snobal.do_tstep_grid(input1, input2, output_rec,
                                      tstep_info, constants,
                                      params, first_step=first_step,
                                      nthreads=nthreads)

            win = True
            break

        except:
            win = False
            pass

    if not win:
        raise Exception('Was not able to succesffuly conplete iPySnobal timestep')
    # reset tstepinfo in case changed
    tstep_info = tstep_info_cp

    return rt


def handle_depth(logger, output_rec, tstep_info, depth_thresh):
    """
    function to zero low snowpack to avoid crash

    Args:
        logger: logger instance
        output_rec: state variables for snobal
        tstep_info: time step params for isnobal
        depth_thresh: threshold of depths to zero

    returns:
        Edits output_rec
    """
    # m_s = output_rec['m_s']
    T_s_0 = output_rec['T_s_0']
    T_s_l = output_rec['T_s_l']
    T_s = output_rec['T_s']
    h2o_sat = output_rec['h2o_sat']
    z_s = output_rec['z_s']
    rho = output_rec['rho']

    # zero out low depths
    new_values = zero_crash_depths(logger, depth_thresh, z_s, rho, T_s_0,
                                   T_s_l, T_s, h2o_sat)

    # update output_rec model state
    output_rec['T_s_0'] = new_values['T_s_0']
    output_rec['T_s_l'] = new_values['T_s_l']
    output_rec['T_s'] = new_values['T_s']
    output_rec['h2o_sat'] = new_values['h2o_sat']
    output_rec['z_s'] = new_values['z_s']
    output_rec['rho'] = new_values['rho']


def handle_mass(logger, output_rec, tstep_info, depth_thresh):
    """
    function to zero low snowpack to avoid crash

    Args:
        logger: logger instance
        output_rec: state variables for snobal
        tstep_info: time step params for isnobal
        depth_thresh: threshold of depths to zero

    returns:
        Edits tstep_info
    """

    # reset threasholds
    tstep_info[NORMAL_TSTEP]['threshold'] = 60
    tstep_info[MEDIUM_TSTEP]['threshold'] = 20
    tstep_info[SMALL_TSTEP]['threshold'] = 10


def do_nothing(logger, output_rec, tstep_info, depth_thresh):
    """
    function does nothing so snobal runs normally
    """
    # do nothing
    nothing = True
